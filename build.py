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
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://badmintonplayer.dk/api/Tournament"
BASE = "https://badmintonplayer.dk"

REGION_FYN = 16
AGEGROUP_U13 = 4
AGEGROUP_LABEL = "U13"
REGION_LABEL = "Fyn"

MONTHS_AHEAD = 4
TIMEOUT = 30

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
    return day.year if day.month >= 7 else day.year - 1


def fetch(date_from: date, date_to: date) -> dict:
    payload = {
        "seasonId": season_id(date_from),
        "regionIdList": [REGION_FYN],
        "dateFrom": f"{date_from.isoformat()}T00:00:00.000Z",
        "dateTo": f"{date_to.isoformat()}T00:00:00.000Z",
        "ageGroupList": [AGEGROUP_U13],
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

    for key in ("tournamentAdmins", "tournaments"):
        if not isinstance(data.get(key), list):
            raise ApiError(
                f"Uventet svarformat: '{key}' mangler. Noegler i svaret: {sorted(data)}"
            )
    return data


def d(value) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def parse(data: dict, today: date) -> list[dict]:
    classes: dict[int, set[str]] = {}
    for row in data["tournaments"]:
        if row.get("ageGroupID") != AGEGROUP_U13:
            continue
        if row.get("classCode"):
            classes.setdefault(row["tournamentID"], set()).add(row["classCode"])

    rows = []
    for t in data["tournamentAdmins"]:
        tid = t["tournamentID"]
        start, slut, frist = d(t.get("dateFrom")), d(t.get("dateTo")), d(t.get("lastRegistration"))
        if not start:
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


def ics(rows: list[dict], now: datetime) -> str:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//u13-fyn//badmintonplayer//DA",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{AGEGROUP_LABEL} {REGION_LABEL} – badminton",
    ]
    for r in rows:
        klub = r["klub"]
        raekker = ", ".join(r["raekker"]) or "?"
        sted = " ({})".format(r["by"]) if r["by"] and r["by"].lower() not in klub.lower() else ""
        link = r["links"][0]["url"] if r["links"] else BASE

        titel = esc("{} {}{}".format(AGEGROUP_LABEL, klub, sted))
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
  --ink:#12312b;
  --ink-soft:#4a6259;
  --paper:#eef2ec;
  --card:#ffffff;
  --court:#2c6e4f;
  --line:#d4ddd4;
  --haster:#b23a26;
  --snart:#9a6a10;
  --god:#2c6e4f;
  --stille:#7d8a83;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;
  background:var(--paper);
  color:var(--ink);
  font-family:Archivo,"Segoe UI",system-ui,sans-serif;
  font-size:17px;
  line-height:1.5;
  font-variant-numeric:tabular-nums;
}
.wrap{max-width:52rem;margin:0 auto;padding:0 1.1rem 4rem}

/* Toppen: naeste frist er det vigtigste paa siden. */
header{background:var(--court);color:#fff;padding:2.2rem 0 1.6rem;margin-bottom:1.6rem}
header .wrap{padding-bottom:0}
h1{font-size:1.45rem;font-weight:600;letter-spacing:-.01em;margin:0 0 1.3rem}
.naeste{display:block;color:inherit;text-decoration:none;
  border-top:2px solid rgba(255,255,255,.55);padding-top:.9rem}
.naeste .label{font-size:.95rem;color:rgba(255,255,255,.8);margin:0 0 .15rem}
.naeste .stort{font-size:clamp(1.7rem,6vw,2.6rem);font-weight:700;line-height:1.1;
  letter-spacing:-.02em;margin:0}
.naeste .hvem{margin:.35rem 0 0;font-size:1.05rem;color:rgba(255,255,255,.9)}
.naeste:hover .stort,.naeste:focus-visible .stort{text-decoration:underline}

/* Filtre */
.filtre{display:flex;flex-wrap:wrap;gap:.45rem;align-items:center;margin:0 0 1.4rem}
.filtre p{margin:0 .35rem 0 0;font-size:.95rem;color:var(--ink-soft)}
.chip{font:inherit;font-size:.95rem;border:1px solid var(--line);background:var(--card);
  color:var(--ink);border-radius:2rem;padding:.3rem .8rem;cursor:pointer}
.chip[aria-pressed="true"]{background:var(--ink);border-color:var(--ink);color:#fff}

/* Maaned */
h2{font-size:1rem;font-weight:600;color:var(--ink-soft);margin:2rem 0 .7rem;
  padding-bottom:.35rem;border-bottom:1px solid var(--line)}

/* Turnering */
.t{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--stille);
  border-radius:.5rem;padding:.95rem 1.1rem;margin-bottom:.7rem}
.t[data-frist="haster"]{border-left-color:var(--haster)}
.t[data-frist="snart"]{border-left-color:var(--snart)}
.t[data-frist="god"]{border-left-color:var(--god)}
.t[data-frist="udloebet"]{opacity:.62}
.t .naar{font-size:1.15rem;font-weight:700;letter-spacing:-.01em}
.t .klub{margin:.1rem 0 .55rem;font-size:1.05rem}
.t .klub span{color:var(--ink-soft)}
.raekker{display:flex;flex-wrap:wrap;gap:.3rem;margin:0 0 .7rem;padding:0;list-style:none}
.raekker li{border:1px solid var(--line);border-radius:.25rem;padding:.1rem .45rem;
  font-size:.9rem;color:var(--ink-soft)}
.frist{font-size:.98rem;font-weight:600;margin:0 0 .75rem}
.frist[data-frist="haster"]{color:var(--haster)}
.frist[data-frist="snart"]{color:var(--snart)}
.frist[data-frist="udloebet"],.frist[data-frist="ukendt"]{color:var(--stille);font-weight:500}
.frist[data-frist="god"]{color:var(--ink-soft);font-weight:500}
.knapper{display:flex;flex-wrap:wrap;gap:.45rem}
.knap{font-size:.95rem;text-decoration:none;border-radius:.3rem;padding:.35rem .8rem;
  border:1px solid var(--ink);color:var(--ink);display:inline-block}
.knap.primaer{background:var(--ink);color:#fff}
.knap:hover{text-decoration:underline}

.tom{background:var(--card);border:1px dashed var(--line);border-radius:.5rem;
  padding:1.6rem;text-align:center;color:var(--ink-soft)}
footer{margin-top:2.6rem;padding-top:1.1rem;border-top:1px solid var(--line);
  font-size:.92rem;color:var(--ink-soft)}
footer a{color:inherit}
a:focus-visible,button:focus-visible{outline:3px solid var(--court);outline-offset:2px}
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


def render(rows: list[dict], today: date, now: datetime) -> str:
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
        naar = f"{UGEDAGE[f.weekday()]} den {f.day}. {MAANEDER[f.month - 1]}"
        hvem = f"{naeste['klub']}, {dato_tekst(naeste)} · U13 {', '.join(naeste['raekker']) or '?'}"
        hero = (
            f'<a class="naeste" href="{e(maal)}">'
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
        raekker = "".join(f"<li>U13 {e(c)}</li>" for c in r["raekker"]) or "<li>Rækker ikke oplyst</li>"
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
<title>U13-turneringer på Fyn</title>
<meta name="description" content="Kommende U13-badmintonturneringer på Fyn med tilmeldingsfrister og direkte link til tilmelding.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<header><div class="wrap">
  <h1>U13-badminton på Fyn</h1>
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
    <p><a href="u13-fyn.ics">Hent kalenderen</a> med turneringer og frister, eller abonnér på
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("docs"))
    ap.add_argument("--months", type=int, default=MONTHS_AHEAD)
    ap.add_argument(
        "--fixture", type=Path,
        help="Laes gemt JSON i stedet for at kalde API'et. Til test.",
    )
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    today = now.date()
    data = (
        json.loads(args.fixture.read_text(encoding="utf-8"))
        if args.fixture
        else fetch(today, today + timedelta(days=31 * args.months))
    )
    rows = parse(data, today)

    if not rows:
        raise ApiError(
            "API'et svarede korrekt, men uden turneringer. Det er usandsynligt midt "
            "i sæsonen — tjek filtrene (region, årgang, sæson) før du stoler på det."
        )

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(render(rows, today, now), encoding="utf-8")
    # newline="" saa CRLF ikke bliver oversat paa Windows
    with open(args.out / "u13-fyn.ics", "w", encoding="utf-8", newline="") as fh:
        fh.write(ics(rows, now))
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
