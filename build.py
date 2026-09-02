#!/usr/bin/env python3
"""
Bygger en statisk side over U13-turneringer paa Fyn ud fra badmintonplayer.dk's
saesonplan-API, samt en .ics-kalender med turneringer og tilmeldingsfrister.

    python build.py --out docs

Endpointet er udokumenteret. Scriptet fejler hoejlydt hvis svaret ikke ser ud
som forventet, saa en aendring bliver synlig i stedet for at give en tom side.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://badmintonplayer.dk/api/Tournament"
BASE = "https://badmintonplayer.dk"

# Standardvaerdier. Alle kan overstyres paa kommandolinjen, saa samme kode kan
# bygge en U11-side, en anden landsdel osv. uden aendringer i selve scriptet.
REGION_ID = 16          # Fyn
AGEGROUP_ID = 4         # U13
AGEGROUP_LABEL = "U13"
REGION_LABEL = "Fyn"

# Hvor langt frem listen kigger. Et rullende aar daekker resten af saesonen og
# starten paa den naeste - ingen fast slutdato der skal vedligeholdes.
HORIZON_DAYS = 365
TIMEOUT = 30
RETRIES = 3

# Vises hoejeste raekke foerst, som paa badmintonplayer.
CLASS_ORDER = ["E", "M", "A", "B", "C", "D"]

# tournamentLinkType -> (knaptekst, er det primaere handling)
LINK_TYPES = {
    0: ("Tilmeld", True),
    1: ("Invitation", False),
    7: ("Programinfo", False),
    5: ("Spilleprogram", False),
}

MAANEDER = [
    "januar", "februar", "marts", "april", "maj", "juni",
    "juli", "august", "september", "oktober", "november", "december",
]
UGEDAGE = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]


class ApiError(RuntimeError):
    pass


# --- data ---------------------------------------------------------------


def season_id(day: date) -> int:
    """Saesonens startaar. DBF's saeson loeber 1. august - 31. juli, saa alt fra
    og med august hoerer til den saeson der starter i indevaerende aar."""
    return day.year if day.month >= 8 else day.year - 1


def seasons_for(day: date) -> list[int]:
    """Den indevaerende saeson og den naeste. Vi henter altid begge, saa listen
    ikke gaar tom ved saesonskiftet, og saa naeste saesons turneringer dukker op
    saa snart de bliver lagt ind."""
    s = season_id(day)
    return [s, s + 1]


def with_retry(call, *, attempts: int = RETRIES, sleep=time.sleep):
    """Koer call() om igen ved ApiError, med voksende pause imellem. Den daglige
    koersel maa ikke falde paa et enkelt netvaerksglimt."""
    for i in range(attempts):
        try:
            return call()
        except ApiError:
            if i == attempts - 1:
                raise
            sleep(2 ** (i + 1))


def validate_response(data: dict) -> dict:
    """Sikrer at svaret har den form resten af scriptet forventer. Kaldes baade
    for live-svar og for --fixture, saa en daarlig fixture fejler lige saa
    tydeligt som en aendring i API'et."""
    for key in ("tournamentAdmins", "tournaments"):
        if not isinstance(data.get(key), list):
            raise ApiError(
                f"Uventet svarformat: '{key}' mangler. Noegler i svaret: {sorted(data)}"
            )
    return data


def merge_responses(responses: list[dict]) -> dict:
    """Slaar svar fra flere saesoner sammen. Foerste forekomst vinder, og
    raekkefoelgen bevares."""
    admins: list[dict] = []
    rows: list[dict] = []
    seen_admin: set = set()
    seen_row: set = set()
    for data in responses:
        for a in data["tournamentAdmins"]:
            key = a.get("tournamentID")
            if key in seen_admin:
                continue
            seen_admin.add(key)
            admins.append(a)
        for t in data["tournaments"]:
            key = (t.get("tournamentID"), t.get("ageGroupID"), t.get("classCode"))
            if key in seen_row:
                continue
            seen_row.add(key)
            rows.append(t)
    return {"tournamentAdmins": admins, "tournaments": rows}


def fetch_season(season: int, date_from: date, date_to: date, *,
                 region_id: int, age_group_id: int) -> dict:
    payload = {
        "seasonId": season,
        "regionIdList": [region_id],
        "dateFrom": f"{date_from.isoformat()}T00:00:00.000Z",
        "dateTo": f"{date_to.isoformat()}T00:00:00.000Z",
        "ageGroupList": [age_group_id],
        "classIdList": [],
        "clubIds": [],
        "tournamentTypeList": [],
        "firstRow": 0,
        "maxCount": 200,
        "tournamentDatesSearch": 0,
    }
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        method="PATCH",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "u13-fyn-site/1.0 (klubbrug)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ApiError(f"HTTP {exc.code} fra API'et: {exc.read()[:300]!r}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"Kunne ikke naa API'et: {exc.reason}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f"Svaret var ikke JSON: {raw[:300]!r}") from exc

    return validate_response(data)


def fetch(date_from: date, date_to: date, *, region_id: int, age_group_id: int) -> dict:
    """Henter den indevaerende og den naeste saeson og slaar dem sammen. Den
    foerste saeson er paakraevet; fejler en senere (findes fx ikke endnu),
    fortsaetter vi med det vi har."""
    responses = []
    for i, season in enumerate(seasons_for(date_from)):
        try:
            responses.append(
                with_retry(lambda season=season: fetch_season(
                    season, date_from, date_to,
                    region_id=region_id, age_group_id=age_group_id,
                ))
            )
        except ApiError as exc:
            if i == 0:
                raise
            print(f"ADVARSEL: sprang saeson {season} over: {exc}", file=sys.stderr)
    return merge_responses(responses)


def d(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ApiError(f"Kunne ikke laese datoen {value!r} fra API'et") from exc


def parse(data: dict, today: date, *, age_group_id: int) -> list[dict]:
    classes: dict[int, set[str]] = {}
    for row in data["tournaments"]:
        if row.get("ageGroupID") != age_group_id:
            continue
        if row.get("classCode"):
            classes.setdefault(row["tournamentID"], set()).add(row["classCode"])

    rows = []
    for t in data["tournamentAdmins"]:
        tid = t["tournamentID"]
        start, slut, frist = d(t.get("dateFrom")), d(t.get("dateTo")), d(t.get("lastRegistration"))
        if not start or start < today:
            continue

        links = []
        for entry in t.get("tournamentLink") or []:
            meta = LINK_TYPES.get(entry.get("tournamentLinkType"))
            url = entry.get("link")
            if not meta or not entry.get("isAllow") or not url or not str(url).startswith("/"):
                continue
            links.append({"tekst": meta[0], "primaer": meta[1], "url": BASE + url})
        links.sort(key=lambda l: (not l["primaer"], l["tekst"]))

        rows.append(
            {
                "id": tid,
                # Har turneringen sit eget navn ("TPI Badminton Monrad staevne"),
                # er det dét badmintonplayer viser - ikke klubnavnet bag den.
                "klub": t.get("title") or t.get("clubName") or "Turnering",
                "by": (t.get("contactCity") or "").strip(),
                "start": start,
                "slut": slut or start,
                "frist": frist,
                "dage": (frist - today).days if frist else None,
                "raekker": sorted(
                    classes.get(tid, ()), key=lambda c: CLASS_ORDER.index(c) if c in CLASS_ORDER else 99
                ),
                "links": links,
            }
        )
    rows.sort(key=lambda r: (r["start"], r["klub"]))
    return rows


# --- tekst ---------------------------------------------------------------


def dato_tekst(r: dict) -> str:
    a, b = r["start"], r["slut"]
    if a == b:
        return f"{a.day}. {MAANEDER[a.month - 1]}"
    if a.month == b.month:
        return f"{a.day}.–{b.day}. {MAANEDER[a.month - 1]}"
    return f"{a.day}. {MAANEDER[a.month - 1]} – {b.day}. {MAANEDER[b.month - 1]}"


def frist_tekst(r: dict) -> tuple[str, str]:
    """Returnerer (tekst, tilstand)."""
    if r["frist"] is None:
        return "Frist ikke oplyst", "ukendt"
    n = r["dage"]
    naar = f"{r['frist'].day}. {MAANEDER[r['frist'].month - 1]}"
    if n < 0:
        return f"Fristen udløb {naar}", "udloebet"
    if n == 0:
        return f"Sidste frist er i dag, {naar}", "haster"
    if n == 1:
        return f"Frist i morgen, {naar}", "haster"
    if n <= 7:
        return f"Frist om {n} dage, {naar}", "haster"
    if n <= 21:
        return f"Frist om {n} dage, {naar}", "snart"
    return f"Frist {naar}", "god"


# --- kalender ------------------------------------------------------------


def ics(rows: list[dict], now: datetime, *, age_label: str, region_label: str) -> str:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//u13-fyn//badmintonplayer//DA",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{age_label} {region_label} – badminton",
    ]
    for r in rows:
        klub = r["klub"]
        raekker = ", ".join(r["raekker"]) or "?"
        sted = " ({})".format(r["by"]) if r["by"] and r["by"].lower() not in klub.lower() else ""
        link = r["links"][0]["url"] if r["links"] else BASE

        titel = esc("{} {}{}".format(age_label, klub, sted))
        beskrivelse = esc("Rækker: {}".format(raekker))
        out += [
            "BEGIN:VEVENT",
            f"UID:t{r['id']}@u13-fyn",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{r['start']:%Y%m%d}",
            f"DTEND;VALUE=DATE:{r['slut'] + timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{titel}",
            f"DESCRIPTION:{beskrivelse}",
            f"URL:{link}",
            "END:VEVENT",
        ]
        if r["frist"]:
            frist_titel = esc("Sidste frist: {} ({})".format(klub, dato_tekst(r)))
            frist_besk = esc("Tilmeldingsfrist. Rækker: {}".format(raekker))
            out += [
                "BEGIN:VEVENT",
                f"UID:f{r['id']}@u13-fyn",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{r['frist']:%Y%m%d}",
                f"DTEND;VALUE=DATE:{r['frist'] + timedelta(days=1):%Y%m%d}",
                f"SUMMARY:{frist_titel}",
                f"DESCRIPTION:{frist_besk}",
                f"URL:{link}",
                "END:VEVENT",
            ]
    out.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in out) + "\r\n"


def fold(line: str) -> str:
    """RFC 5545: linjer maa fylde hoejst 75 oktetter; resten fortsaetter med et mellemrum."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    dele, rest = [], raw
    grænse = 75
    while len(rest) > grænse:
        skær = grænse
        while skær > 0 and (rest[skær] & 0xC0) == 0x80:  # bryd ikke midt i et tegn
            skær -= 1
        dele.append(rest[:skær].decode("utf-8"))
        rest = rest[skær:]
        grænse = 74
    dele.append(rest.decode("utf-8"))
    return "\r\n ".join(dele)


# --- html ----------------------------------------------------------------

CSS = """
:root{
  color-scheme:light;
  --bg:#f2f6f2;          /* svag banehalgroen paper */
  --card:#ffffff;
  --fg:#182a24;          /* moerk, varm groensort */
  --muted:#586b62;       /* sekundaer tekst */
  --border:#dbe6dd;      /* haarfin groengraa streg */
  --brand:#14532d;       /* dyb skovgroen - toppen */
  --brand-fg:#ffffff;
  --accent:#15803d;      /* groen - maanedsoverskrift, links, fokus */
  --accent-strong:#166534;/* moerkere groen til flader med hvid tekst */
  --haster:#c0392b;      /* roed - haster (<=7 dage) */
  --haster-wash:#fcf0ee; /* svag roed baggrund paa hastende kort */
  --snart:#b45309;       /* rav - snart (<=21 dage) */
  --stille:#93a29a;      /* falmet - udloebet / ukendt */
  --radius:.5rem;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;
  background:var(--bg);
  color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,
    "Apple Color Emoji","Segoe UI Emoji",sans-serif;
  font-size:16px;
  line-height:1.5;
  font-variant-numeric:tabular-nums;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:48rem;margin:0 auto;padding:0 1.1rem 4rem}

/* Toppen: naeste frist er det vigtigste paa siden. */
header{background:var(--brand);color:var(--brand-fg);padding:2rem 0 1.5rem;
  margin-bottom:1.6rem}
header .wrap{padding-bottom:0}
h1{font-size:1.3rem;font-weight:600;letter-spacing:-.015em;margin:0 0 1.2rem}
.naeste{display:block;color:inherit;text-decoration:none;
  border-top:1px solid rgba(255,255,255,.2);padding-top:.9rem}
.naeste .label{font-size:.85rem;color:rgba(255,255,255,.7);margin:0 0 .2rem}
.naeste .stort{font-size:clamp(1.6rem,5.5vw,2.4rem);font-weight:700;line-height:1.1;
  letter-spacing:-.025em;margin:0}
.naeste .hvem{margin:.4rem 0 0;font-size:1rem;color:rgba(255,255,255,.8)}
.naeste:hover .stort,.naeste:focus-visible .stort{text-decoration:underline}
/* Er selve den naeste frist taet paa, saa lad tallet vise det. */
.naeste.haster .stort{color:#ffd7d0}
.naeste.haster .label{color:#ffc9c0}

/* Filtre */
.filtre{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:0 0 1.4rem}
.filtre p{margin:0 .35rem 0 0;font-size:.875rem;color:var(--muted)}
.chip{font:inherit;font-size:.875rem;font-weight:500;border:1px solid var(--border);
  background:var(--card);color:var(--fg);border-radius:2rem;padding:.3rem .85rem;
  cursor:pointer;transition:background-color .12s,border-color .12s}
.chip:hover{border-color:var(--accent);color:var(--accent-strong)}
.chip[aria-pressed="true"]{background:var(--accent-strong);border-color:var(--accent-strong);
  color:#fff}

/* Maaned */
h2{font-size:.8rem;font-weight:700;letter-spacing:.03em;text-transform:uppercase;
  color:var(--accent-strong);margin:2rem 0 .7rem;padding-bottom:.35rem;
  border-bottom:1px solid var(--border)}

/* Turnering */
.t{background:var(--card);border:1px solid var(--border);border-left:4px solid transparent;
  border-radius:var(--radius);padding:.9rem 1.05rem;margin-bottom:.6rem;
  box-shadow:0 1px 2px rgb(20 40 30/.05)}
.t[data-frist="haster"]{border-left-color:var(--haster);background:var(--haster-wash)}
.t[data-frist="snart"]{border-left-color:var(--snart)}
.t[data-frist="udloebet"]{opacity:.55}
.t .naar{font-size:1.1rem;font-weight:700;letter-spacing:-.01em}
.t .klub{margin:.1rem 0 .6rem;font-size:1rem}
.t .klub span{color:var(--muted)}
.raekker{display:flex;flex-wrap:wrap;gap:.3rem;margin:0 0 .7rem;padding:0;list-style:none}
.raekker li{background:#eef5f0;border:1px solid #dae9e0;
  border-radius:calc(var(--radius) - 2px);padding:.12rem .5rem;
  font-size:.8rem;font-weight:500;color:#3f594d}
.frist{font-size:.92rem;font-weight:700;margin:0 0 .8rem}
.frist[data-frist="haster"]{color:var(--haster)}
.frist[data-frist="snart"]{color:var(--snart)}
.frist[data-frist="udloebet"],.frist[data-frist="ukendt"]{color:var(--stille);font-weight:500}
.frist[data-frist="god"]{color:var(--muted);font-weight:500}
.knapper{display:flex;flex-wrap:wrap;gap:.45rem}
.knap{font-size:.875rem;font-weight:500;text-decoration:none;line-height:1.2;
  border-radius:calc(var(--radius) - 2px);padding:.4rem .8rem;border:1px solid var(--border);
  color:var(--fg);background:var(--card);display:inline-block;
  transition:background-color .12s,border-color .12s}
.knap:hover{border-color:var(--accent);color:var(--accent-strong)}
.knap.primaer{background:var(--accent-strong);color:#fff;border-color:var(--accent-strong)}
.knap.primaer:hover{background:var(--brand);border-color:var(--brand);color:#fff}

.tom{background:var(--card);border:1px dashed var(--border);border-radius:var(--radius);
  padding:1.6rem;text-align:center;color:var(--muted)}
footer{margin-top:2.6rem;padding-top:1.1rem;border-top:1px solid var(--border);
  font-size:.875rem;color:var(--muted)}
footer a{color:var(--accent-strong)}
a:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px;
  border-radius:3px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

JS = """
const chips = document.querySelectorAll('.chip[data-raekke]');
const skjul = document.getElementById('skjul-udloebne');
function opdater(){
  const valgte = [...chips].filter(c=>c.getAttribute('aria-pressed')==='true')
                           .map(c=>c.dataset.raekke);
  document.querySelectorAll('.t').forEach(t=>{
    const mine = (t.dataset.raekker||'').split(',').filter(Boolean);
    const passer = !valgte.length || valgte.some(v=>mine.includes(v));
    const udloebet = t.dataset.frist === 'udloebet';
    t.hidden = !passer || (skjul.getAttribute('aria-pressed')==='true' && udloebet);
  });
  document.querySelectorAll('section[data-maaned]').forEach(s=>{
    s.hidden = ![...s.querySelectorAll('.t')].some(t=>!t.hidden);
  });
  const noget = [...document.querySelectorAll('.t')].some(t=>!t.hidden);
  document.getElementById('intet').hidden = noget;
}
function toggle(el){
  el.setAttribute('aria-pressed', el.getAttribute('aria-pressed')==='true' ? 'false':'true');
  opdater();
}
chips.forEach(c=>c.addEventListener('click',()=>toggle(c)));
skjul.addEventListener('click',()=>toggle(skjul));
opdater();
"""


def render(rows: list[dict], today: date, now: datetime, *,
           age_label: str, region_label: str) -> str:
    e = html.escape

    kommende = [r for r in rows if r["dage"] is not None and r["dage"] >= 0]
    naeste = min(kommende, key=lambda r: r["dage"]) if kommende else None

    def by_tekst(r: dict) -> str:
        """Byen udelades naar den allerede staar i klubnavnet."""
        by = r["by"]
        if not by or by.lower() in r["klub"].lower():
            return ""
        return by

    if naeste:
        n = naeste["dage"]
        stort = "Sidste frist er i dag" if n == 0 else (
            "Sidste frist er i morgen" if n == 1 else f"Næste frist om {n} dage"
        )
        maal = naeste["links"][0]["url"] if naeste["links"] else BASE
        f = naeste["frist"]
        _, hero_state = frist_tekst(naeste)
        naar = f"{UGEDAGE[f.weekday()]} den {f.day}. {MAANEDER[f.month - 1]}"
        hvem = f"{naeste['klub']}, {dato_tekst(naeste)} · {age_label} {', '.join(naeste['raekker']) or '?'}"
        hero = (
            f'<a class="naeste {hero_state}" href="{e(maal)}">'
            f'<p class="label">{e(naar)}</p>'
            f'<p class="stort">{e(stort)}</p>'
            f'<p class="hvem">{e(hvem)}</p></a>'
        )
    else:
        hero = """<div class="naeste"><p class="stort">Ingen åbne frister lige nu</p>
        <p class="hvem">Siden opdaterer sig selv, når nye turneringer bliver lagt op.</p></div>"""

    # Raekke-filtre: kun dem der faktisk forekommer.
    findes = sorted({c for r in rows for c in r["raekker"]},
                    key=lambda c: CLASS_ORDER.index(c) if c in CLASS_ORDER else 99)
    chips = "".join(
        f'<button class="chip" type="button" aria-pressed="false" data-raekke="{e(c)}">{e(c)}</button>'
        for c in findes
    )

    dele = []
    sidste = None
    for r in rows:
        n = (r["start"].year, r["start"].month)
        if n != sidste:
            if sidste is not None:
                dele.append("</section>")
            dele.append(
                f'<section data-maaned="{n[0]}-{n[1]:02d}">'
                f"<h2>{e(MAANEDER[n[1] - 1])} {n[0]}</h2>"
            )
            sidste = n

        tekst, tilstand = frist_tekst(r)
        by = by_tekst(r)
        sted = f" <span>· {e(by)}</span>" if by else ""
        raekker = "".join(f"<li>{e(age_label)} {e(c)}</li>" for c in r["raekker"]) or "<li>Rækker ikke oplyst</li>"
        knapper = "".join(
            f'<a class="knap{" primaer" if l["primaer"] else ""}" href="{e(l["url"])}">{e(l["tekst"])}</a>'
            for l in r["links"]
        ) or '<span class="frist" data-frist="ukendt">Ingen links endnu</span>'

        dele.append(f"""<article class="t" data-frist="{tilstand}" data-raekker="{e(','.join(r['raekker']))}">
      <p class="naar">{e(dato_tekst(r))}</p>
      <p class="klub">{e(r['klub'])}{sted}</p>
      <ul class="raekker">{raekker}</ul>
      <p class="frist" data-frist="{tilstand}">{e(tekst)}</p>
      <div class="knapper">{knapper}</div>
    </article>""")
    if sidste is not None:
        dele.append("</section>")

    liste = "\n".join(dele) or ""
    opdateret = now.strftime("%d.%m.%Y kl. %H:%M")

    return f"""<!doctype html>
<html lang="da">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(age_label)}-turneringer på {e(region_label)}</title>
<meta name="description" content="Kommende {e(age_label)}-badmintonturneringer på {e(region_label)} med tilmeldingsfrister og direkte link til tilmelding.">
<style>{CSS}</style>
</head>
<body>
<header><div class="wrap">
  <h1>{e(age_label)}-badminton på {e(region_label)}</h1>
  {hero}
</div></header>

<div class="wrap">
  <div class="filtre">
    <p>Vis række:</p>
    {chips}
    <button class="chip" type="button" aria-pressed="true" id="skjul-udloebne">Skjul udløbne frister</button>
  </div>

  {liste}

  <div class="tom" id="intet" hidden>
    <p>Ingen turneringer passer til de valgte rækker. Slå et filter fra for at se resten.</p>
  </div>

  <footer>
    <p><a href="kalender.ics">Hent kalenderen</a> med turneringer og frister, eller abonnér på
       den, så den følger med af sig selv.</p>
    <p>Data hentes fra sæsonplanen på
       <a href="https://badmintonplayer.dk/DBF/Turnering/SaesonPlan/">badmintonplayer.dk</a>,
       som er Badminton Danmarks. Tilmelding og invitationer sker altid der.
       Opdateret {e(opdateret)}. Siden er ikke officiel.</p>
  </footer>
</div>
<script>{JS}</script>
</body>
</html>
"""


# --- main ----------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("docs"))
    ap.add_argument("--age-group-id", type=int, default=AGEGROUP_ID,
                    help=f"ageGroupID hos badmintonplayer (standard {AGEGROUP_ID} = {AGEGROUP_LABEL}).")
    ap.add_argument("--age-group-label", default=AGEGROUP_LABEL,
                    help="Vises paa siden og i kalenderen.")
    ap.add_argument("--region-id", type=int, default=REGION_ID,
                    help=f"regionIdList hos badmintonplayer (standard {REGION_ID} = {REGION_LABEL}).")
    ap.add_argument("--region-label", default=REGION_LABEL,
                    help="Vises paa siden og i kalenderen.")
    ap.add_argument("--horizon-days", type=int, default=HORIZON_DAYS,
                    help=f"Hvor mange dage frem listen kigger (standard {HORIZON_DAYS}).")
    ap.add_argument(
        "--fixture", type=Path,
        help="Laes gemt JSON i stedet for at kalde API'et. Til test.",
    )
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    today = now.date()
    if args.fixture:
        data = validate_response(json.loads(args.fixture.read_text(encoding="utf-8")))
    else:
        data = fetch(
            today, today + timedelta(days=args.horizon_days),
            region_id=args.region_id, age_group_id=args.age_group_id,
        )
    rows = parse(data, today, age_group_id=args.age_group_id)

    if not rows:
        raise ApiError(
            "API'et svarede korrekt, men uden turneringer. Det er usandsynligt midt "
            "i sæsonen — tjek filtrene (region, årgang, sæson) før du stoler på det."
        )

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(
        render(rows, today, now,
               age_label=args.age_group_label, region_label=args.region_label),
        encoding="utf-8",
    )
    # newline="" saa CRLF ikke bliver oversat paa Windows
    with open(args.out / "kalender.ics", "w", encoding="utf-8", newline="") as fh:
        fh.write(ics(rows, now,
                     age_label=args.age_group_label, region_label=args.region_label))
    (args.out / ".nojekyll").write_text("", encoding="utf-8")

    aabne = sum(1 for r in rows if r["dage"] is not None and r["dage"] >= 0)
    print(f"Skrev {len(rows)} turneringer ({aabne} med åben frist) til {args.out}/")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as exc:
        print(f"FEJL: {exc}", file=sys.stderr)
        sys.exit(1)
