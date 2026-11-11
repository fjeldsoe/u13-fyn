"""Tests for build.py. Kun standardbiblioteket, kør med:

    python -m unittest test_build -v
"""

import http.client
import io
import json
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import build


def admin(tid, date_from, *, date_to=None, frist=None, title=None,
          club="Klub", city="", links=None):
    return {
        "tournamentID": tid,
        "dateFrom": f"{date_from}T00:00:00",
        "dateTo": f"{date_to or date_from}T00:00:00",
        "lastRegistration": f"{frist}T00:00:00" if frist else None,
        "title": title,
        "clubName": club,
        "contactCity": city,
        "tournamentLink": links or [],
    }


def row(tid, age_group_id, class_code, *, class_from=None, class_to=None):
    r = {"tournamentID": tid, "ageGroupID": age_group_id, "classCode": class_code}
    if class_from:
        r["classDateFrom"] = f"{class_from}T00:00:00"
    if class_to:
        r["classDateTo"] = f"{class_to}T00:00:00"
    return r


def api(admins, rows):
    return {"tournamentAdmins": admins, "tournaments": rows}


NOW = datetime(2026, 9, 2, 5, 30, tzinfo=timezone.utc)
TODAY = NOW.date()


class SeasonId(unittest.TestCase):
    def test_july_belongs_to_the_season_that_started_last_year(self):
        self.assertEqual(build.season_id(date(2027, 7, 15)), 2026)

    def test_august_starts_the_new_season(self):
        self.assertEqual(build.season_id(date(2027, 8, 1)), 2027)

    def test_december_is_still_the_season_that_started_in_august(self):
        self.assertEqual(build.season_id(date(2026, 12, 31)), 2026)

    def test_january_is_the_season_that_started_the_previous_august(self):
        self.assertEqual(build.season_id(date(2027, 1, 1)), 2026)


class SeasonsFor(unittest.TestCase):
    def test_returns_current_and_next_season(self):
        self.assertEqual(build.seasons_for(date(2026, 9, 1)), [2026, 2027])

    def test_spring_still_reaches_into_the_coming_season(self):
        self.assertEqual(build.seasons_for(date(2027, 3, 1)), [2026, 2027])

    def test_rolls_forward_after_the_august_cutoff(self):
        self.assertEqual(build.seasons_for(date(2027, 8, 1)), [2027, 2028])


class MergeResponses(unittest.TestCase):
    def test_empty_input_yields_empty_lists(self):
        self.assertEqual(
            build.merge_responses([]),
            {"tournamentAdmins": [], "tournaments": []},
        )

    def test_a_tournament_in_both_seasons_is_kept_once(self):
        a = api([admin(1, "2026-09-05")], [row(1, 4, "A")])
        b = api([admin(1, "2026-09-05"), admin(2, "2027-09-05")], [row(1, 4, "A"), row(2, 4, "B")])
        merged = build.merge_responses([a, b])
        self.assertEqual([t["tournamentID"] for t in merged["tournamentAdmins"]], [1, 2])
        self.assertEqual(len(merged["tournaments"]), 2)

    def test_same_tournament_different_class_rows_are_both_kept(self):
        a = api([admin(1, "2026-09-05")], [row(1, 4, "A"), row(1, 4, "B")])
        merged = build.merge_responses([a])
        self.assertEqual(len(merged["tournaments"]), 2)

    def test_first_occurrence_wins_and_order_is_preserved(self):
        a = api([admin(1, "2026-09-05", club="Først")], [])
        b = api([admin(1, "2026-09-05", club="Sidst"), admin(2, "2027-01-01")], [])
        merged = build.merge_responses([a, b])
        self.assertEqual(merged["tournamentAdmins"][0]["clubName"], "Først")
        self.assertEqual([t["tournamentID"] for t in merged["tournamentAdmins"]], [1, 2])


class WithRetry(unittest.TestCase):
    def test_returns_result_once_a_call_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise build.ApiError("blip")
            return "ok"

        out = build.with_retry(flaky, attempts=3, sleep=lambda _: None)
        self.assertEqual(out, "ok")
        self.assertEqual(len(calls), 3)

    def test_reraises_after_the_last_attempt(self):
        calls = []

        def always_fails():
            calls.append(1)
            raise build.ApiError("down")

        with self.assertRaises(build.ApiError):
            build.with_retry(always_fails, attempts=3, sleep=lambda _: None)
        self.assertEqual(len(calls), 3)


class ValidateResponse(unittest.TestCase):
    def test_passes_a_well_formed_response_through(self):
        data = api([admin(1, "2026-09-05")], [row(1, 4, "A")])
        self.assertIs(build.validate_response(data), data)

    def test_missing_key_raises_apierror(self):
        with self.assertRaises(build.ApiError):
            build.validate_response({"tournamentAdmins": []})

    def test_wrong_type_raises_apierror(self):
        with self.assertRaises(build.ApiError):
            build.validate_response({"tournamentAdmins": [], "tournaments": {}})


class ParseDate(unittest.TestCase):
    def test_none_and_empty_are_none(self):
        self.assertIsNone(build.d(None))
        self.assertIsNone(build.d(""))

    def test_reads_the_date_part_of_an_iso_timestamp(self):
        self.assertEqual(build.d("2026-09-05T00:00:00"), date(2026, 9, 5))

    def test_a_non_iso_value_raises_apierror_not_valueerror(self):
        with self.assertRaises(build.ApiError):
            build.d("/Date(1750000000000)/")


class Parse(unittest.TestCase):
    def test_tournaments_before_today_are_dropped(self):
        data = api(
            [admin(1, "2026-08-01"), admin(2, "2026-10-01")],
            [row(1, 4, "A"), row(2, 4, "A")],
        )
        rows = build.parse(data, TODAY, age_group_id=4)
        self.assertEqual([r["id"] for r in rows], [2])

    def test_a_tournament_starting_today_is_kept(self):
        data = api([admin(1, TODAY.isoformat())], [row(1, 4, "A")])
        rows = build.parse(data, TODAY, age_group_id=4)
        self.assertEqual(len(rows), 1)

    def test_et_staevne_i_gang_vises_stadig_paa_dag_to(self):
        # TODAY er 2. september: U13 spiller 1.-2., saa staevnet er ikke slut.
        data = api(
            [admin(1, "2026-09-01", date_to="2026-09-02"), admin(2, "2026-08-31", date_to="2026-09-01")],
            [row(1, 4, "A", class_from="2026-09-01", class_to="2026-09-02"), row(2, 4, "A")],
        )
        rows = build.parse(data, TODAY, age_group_id=4)
        self.assertEqual([r["id"] for r in rows], [1])

    def test_samme_staevne_vises_kun_en_gang(self):
        # Fx naar to saesoner returnerer samme id, en gang som tal og en gang som tekst.
        data = api(
            [admin(1, "2026-09-01", date_to="2026-09-02"), admin("1", "2026-09-01", date_to="2026-09-02")],
            [row(1, 4, "A"), row("1", 4, "A")],
        )
        rows = build.parse(data, TODAY, age_group_id=4)
        self.assertEqual([r["id"] for r in rows], [1])

    def test_class_rows_are_filtered_by_the_requested_age_group(self):
        data = api(
            [admin(1, "2026-10-01")],
            [row(1, 4, "A"), row(1, 3, "B")],
        )
        u13 = build.parse(data, TODAY, age_group_id=4)
        u11 = build.parse(data, TODAY, age_group_id=3)
        self.assertEqual(u13[0]["raekker"], ["A"])
        self.assertEqual(u11[0]["raekker"], ["B"])


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


class DanskTid(unittest.TestCase):
    """EU-reglen: sommertid fra sidste soendag i marts kl. 01:00 UTC til
    sidste soendag i oktober kl. 01:00 UTC. I 2026 er det 29/3 og 25/10."""

    def test_vinter_er_utc_plus_en(self):
        self.assertEqual(build.dansk_tid(utc(2026, 1, 15, 12)).hour, 13)

    def test_sommer_er_utc_plus_to(self):
        self.assertEqual(build.dansk_tid(utc(2026, 7, 15, 12)).hour, 14)

    def test_minuttet_foer_sommertid_starter(self):
        t = build.dansk_tid(utc(2026, 3, 29, 0, 59))
        self.assertEqual((t.hour, t.minute), (1, 59))

    def test_sommertid_starter_paa_slaget(self):
        self.assertEqual(build.dansk_tid(utc(2026, 3, 29, 1)).hour, 3)

    def test_minuttet_foer_sommertid_slutter(self):
        t = build.dansk_tid(utc(2026, 10, 25, 0, 59))
        self.assertEqual((t.hour, t.minute), (2, 59))

    def test_sommertid_slutter_paa_slaget(self):
        self.assertEqual(build.dansk_tid(utc(2026, 10, 25, 1)).hour, 2)

    def test_reglen_gaelder_ogsaa_naeste_aar(self):
        # 2027 skifter 28/3 - beregnet, ikke hardkodet
        self.assertEqual(build.dansk_tid(utc(2027, 3, 28, 1)).hour, 3)
        self.assertEqual(build.dansk_tid(utc(2027, 3, 28, 0, 59)).hour, 1)

    def test_datoen_kan_rulle_over_midnat(self):
        # 23:30 UTC nytaarsaften er allerede 1. januar i Danmark
        self.assertEqual(build.dansk_tid(utc(2026, 12, 31, 23, 30)).date(),
                         date(2027, 1, 1))


class PlayDates(unittest.TestCase):
    def test_uses_the_age_groups_class_dates_when_they_are_narrower(self):
        # Turneringen gaar over to dage, men U13 spiller kun den anden.
        data = api(
            [admin(1, "2026-10-01", date_to="2026-10-02")],
            [row(1, 4, "A", class_from="2026-10-02", class_to="2026-10-02"),
             row(1, 4, "B", class_from="2026-10-02", class_to="2026-10-02")],
        )
        r = build.parse(data, TODAY, age_group_id=4)[0]
        self.assertEqual((r["start"], r["slut"]), (date(2026, 10, 2), date(2026, 10, 2)))

    def test_falls_back_to_tournament_dates_without_class_dates(self):
        data = api(
            [admin(1, "2026-10-01", date_to="2026-10-02")],
            [row(1, 4, "A")],
        )
        r = build.parse(data, TODAY, age_group_id=4)[0]
        self.assertEqual((r["start"], r["slut"]), (date(2026, 10, 1), date(2026, 10, 2)))

    def test_spans_the_union_when_the_class_rows_disagree(self):
        data = api(
            [admin(1, "2026-10-01", date_to="2026-10-02")],
            [row(1, 4, "A", class_from="2026-10-01", class_to="2026-10-02"),
             row(1, 4, "B", class_from="2026-10-02", class_to="2026-10-02")],
        )
        r = build.parse(data, TODAY, age_group_id=4)[0]
        self.assertEqual((r["start"], r["slut"]), (date(2026, 10, 1), date(2026, 10, 2)))

    def test_another_age_groups_class_dates_do_not_leak_in(self):
        data = api(
            [admin(1, "2026-10-01", date_to="2026-10-02")],
            [row(1, 4, "A", class_from="2026-10-02", class_to="2026-10-02"),
             row(1, 3, "A", class_from="2026-10-01", class_to="2026-10-01")],
        )
        r = build.parse(data, TODAY, age_group_id=4)[0]
        self.assertEqual((r["start"], r["slut"]), (date(2026, 10, 2), date(2026, 10, 2)))


class Links(unittest.TestCase):
    def rows(self):
        data = api(
            [admin(1, "2026-10-01", frist="2026-09-25",
                   links=[{"tournamentLinkType": 0, "isAllow": True, "link": "/DBF/x"}])],
            [row(1, 4, "A")],
        )
        return build.parse(data, TODAY, age_group_id=4)

    def test_tournament_button_opens_in_a_new_tab(self):
        out = build.render(self.rows(), TODAY, NOW, age_label="U13", region_label="Fyn")
        knap = re.search(r'<a class="knap[^>]*>', out).group(0)
        self.assertIn('target="_blank"', knap)
        self.assertIn('rel="noopener"', knap)

    def test_hero_link_opens_in_a_new_tab(self):
        out = build.render(self.rows(), TODAY, NOW, age_label="U13", region_label="Fyn")
        hero = re.search(r'<a class="naeste[^>]*>', out).group(0)
        self.assertIn('target="_blank"', hero)
        self.assertIn('rel="noopener"', hero)


class RenderLabels(unittest.TestCase):
    def rows(self):
        data = api([admin(1, "2026-10-01", frist="2026-09-20")], [row(1, 4, "A")])
        return build.parse(data, TODAY, age_group_id=4)

    def test_title_and_heading_use_the_supplied_labels(self):
        out = build.render(self.rows(), TODAY, NOW, age_label="U11", region_label="Jylland")
        self.assertIn("<title>U11-turneringer på Jylland</title>", out)
        self.assertIn("U11-badminton på Jylland", out)

    def test_no_hardcoded_u13_or_fyn_leaks_through(self):
        out = build.render(self.rows(), TODAY, NOW, age_label="U11", region_label="Jylland")
        self.assertNotIn("U13", out)
        self.assertNotIn("Fyn", out)

    def test_output_is_a_full_html_document(self):
        out = build.render(self.rows(), TODAY, NOW, age_label="U13", region_label="Fyn")
        self.assertTrue(out.lstrip().startswith("<!doctype html>"))
        self.assertIn("</html>", out)

    def test_opdateret_vises_i_dansk_tid_ikke_utc(self):
        # NOW er 05:30 UTC i september = 07:30 dansk sommertid.
        out = build.render(self.rows(), TODAY, NOW, age_label="U13", region_label="Fyn")
        self.assertIn("Opdateret 02.09.2026 kl. 07:30", out)
        self.assertNotIn("kl. 05:30", out)


class HeroUrgency(unittest.TestCase):
    def hero_for(self, frist):
        data = api([admin(1, "2026-10-01", frist=frist)], [row(1, 4, "A")])
        rows = build.parse(data, TODAY, age_group_id=4)
        out = build.render(rows, TODAY, NOW, age_label="U13", region_label="Fyn")
        return re.search(r'<a class="naeste([^"]*)"', out).group(1).strip()

    def test_a_deadline_within_a_week_marks_the_hero_urgent(self):
        self.assertEqual(self.hero_for("2026-09-07"), "haster")

    def test_a_distant_deadline_leaves_the_hero_calm(self):
        self.assertEqual(self.hero_for("2026-12-01"), "god")


class IcsLabels(unittest.TestCase):
    def rows(self):
        data = api([admin(1, "2026-10-01", frist="2026-09-20")], [row(1, 4, "A")])
        return build.parse(data, TODAY, age_group_id=4)

    def test_calendar_name_and_event_summary_use_the_labels(self):
        out = build.ics(self.rows(), NOW, age_label="U11", region_label="Jylland")
        self.assertIn("X-WR-CALNAME:U11 Jylland", out)
        self.assertIn("SUMMARY:U11 ", out)

    def test_calendar_is_well_formed(self):
        out = build.ics(self.rows(), NOW, age_label="U13", region_label="Fyn")
        self.assertTrue(out.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("BEGIN:VEVENT", out)
        self.assertTrue(out.rstrip().endswith("END:VCALENDAR"))


class EnkeltKalender(unittest.TestCase):
    """Hvert kort skal kunne lægge stævnet og fristen i kalenderen hver for sig."""

    def en_row(self, frist="2026-09-25"):
        data = api(
            [admin(1, "2026-10-01", frist=frist,
                   links=[{"tournamentLinkType": 0, "isAllow": True, "link": "/DBF/x"}])],
            [row(1, 4, "A")],
        )
        return build.parse(data, TODAY, age_group_id=4)[0]

    def test_kun_staevnet(self):
        out = build.ics([self.en_row()], NOW, age_label="U13", region_label="Fyn",
                        frister=False)
        self.assertIn("UID:t1@u13-fyn", out)
        self.assertNotIn("UID:f1@u13-fyn", out)
        self.assertEqual(out.count("BEGIN:VEVENT"), 1)

    def test_kun_fristen(self):
        out = build.ics([self.en_row()], NOW, age_label="U13", region_label="Fyn",
                        turneringer=False)
        self.assertIn("UID:f1@u13-fyn", out)
        self.assertNotIn("UID:t1@u13-fyn", out)
        self.assertEqual(out.count("BEGIN:VEVENT"), 1)

    def test_begge_dele_er_stadig_standard(self):
        out = build.ics([self.en_row()], NOW, age_label="U13", region_label="Fyn")
        self.assertEqual(out.count("BEGIN:VEVENT"), 2)

    def test_stien_foelger_uid_praefikset(self):
        r = self.en_row()
        self.assertEqual(build.kalender_sti(r, "t"), "kalender/t1.ics")
        self.assertEqual(build.kalender_sti(r, "f"), "kalender/f1.ics")


class KalenderKnapper(unittest.TestCase):
    def rows(self, frist="2026-09-25"):
        data = api(
            [admin(1, "2026-10-01", frist=frist,
                   links=[{"tournamentLinkType": 0, "isAllow": True, "link": "/DBF/x"}])],
            [row(1, 4, "A")],
        )
        return build.parse(data, TODAY, age_group_id=4)

    def render(self, frist="2026-09-25"):
        return build.render(self.rows(frist), TODAY, NOW,
                            age_label="U13", region_label="Fyn")

    def test_kortet_har_knap_til_baade_staevne_og_frist(self):
        out = self.render()
        self.assertIn('href="kalender/t1.ics"', out)
        self.assertIn('href="kalender/f1.ics"', out)

    def test_uden_frist_er_der_kun_staevneknappen(self):
        out = self.render(frist=None)
        self.assertIn('href="kalender/t1.ics"', out)
        self.assertNotIn('href="kalender/f1.ics"', out)

    def test_kalenderknapper_aabner_ikke_i_nyt_faneblad(self):
        # .ics skal overlades til kalender-appen, ikke navigeres til.
        out = self.render()
        knap = re.search(r'<a[^>]*href="kalender/t1\.ics"[^>]*>', out).group(0)
        self.assertNotIn('target="_blank"', knap)


if __name__ == "__main__":
    unittest.main()


class TurneringsId(unittest.TestCase):
    def test_id_som_tekst_bliver_til_tal(self):
        data = api([admin("17660", "2026-10-01")], [row("17660", 4, "A")])
        r = build.parse(data, TODAY, age_group_id=4)[0]
        self.assertEqual(build.kalender_sti(r, "t"), "kalender/t17660.ics")
        self.assertEqual(r["raekker"], ["A"])

    def test_id_der_ikke_er_et_tal_stopper_bygningen(self):
        # Id'et bliver til et filnavn - "../" maa aldrig naa frem til disken.
        data = api([admin("a/../../../x", "2026-10-01")], [])
        with self.assertRaises(build.ApiError):
            build.parse(data, TODAY, age_group_id=4)


class Netvaerksfejl(unittest.TestCase):
    """Fejl midt i svaret skal blive til ApiError, ellers proever with_retry
    aldrig igen."""

    def test_fejl_under_hentning_bliver_til_apierror(self):
        for fejl in (TimeoutError("timed out"), ConnectionResetError("reset"),
                     http.client.IncompleteRead(b"")):
            with self.subTest(fejl=type(fejl).__name__),                     mock.patch("build.urllib.request.urlopen", side_effect=fejl):
                with self.assertRaises(build.ApiError):
                    build.fetch_season(2026, TODAY, TODAY, region_id=16, age_group_id=4)


class Afskaaret(unittest.TestCase):
    def test_flere_turneringer_end_der_kom_med_stopper_bygningen(self):
        # API'et skaerer stille af ved maxCount, men 'count' fortaeller sandheden.
        data = api([admin(1, "2026-10-01")], [])
        data["count"] = 21
        with self.assertRaises(build.ApiError):
            build.validate_response(data)

    def test_count_der_passer_er_fint(self):
        data = api([admin(1, "2026-10-01")], [])
        data["count"] = 1
        self.assertIs(build.validate_response(data), data)


class Main(unittest.TestCase):
    """Hele vejen igennem: hver kalenderknap paa siden skal pege paa en fil,
    der faktisk blev skrevet."""

    def test_alle_kalenderlinks_peger_paa_skrevne_filer(self):
        om = lambda dage: (date.today() + timedelta(days=dage)).isoformat()
        data = api(
            [admin(17660, om(30), frist=om(10)), admin(17661, om(40))],
            [row(17660, 4, "A"), row(17661, 4, "B")],
        )
        with tempfile.TemporaryDirectory() as mappe:
            ud = Path(mappe) / "docs"
            fixture = Path(mappe) / "data.json"
            fixture.write_text(json.dumps(data), encoding="utf-8")
            argv = ["build.py", "--out", str(ud), "--fixture", str(fixture)]
            with mock.patch.object(sys, "argv", argv), mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(build.main(), 0)
            side = (ud / "index.html").read_text(encoding="utf-8")
            links = re.findall(r'href="(kalender[^"]*\.ics)"', side)
            self.assertEqual(sorted(links), sorted([
                "kalender.ics", "kalender/t17660.ics", "kalender/f17660.ics",
                "kalender/t17661.ics",
            ]))
            for link in links:
                self.assertTrue((ud / link).is_file(), link)


class Statistik(unittest.TestCase):
    def setUp(self):
        links = [{"tournamentLinkType": 0, "isAllow": True, "link": "/tilmeld/1"}]
        data = api([admin(1, "2026-09-20", frist="2026-09-10", links=links)], [row(1, 4, "A")])
        rows = build.parse(data, TODAY, age_group_id=4)
        self.side = build.render(rows, TODAY, NOW, age_label="U13", region_label="Fyn")

    def test_goatcounter_er_paa_siden(self):
        self.assertIn('data-goatcounter="https://fjeldsoe.goatcounter.com/count"', self.side)

    def test_alle_links_taeller_klik(self):
        # Hvert <a> paa siden skal have et klik-navn, ellers kan man ikke se hvad der bruges.
        for a in re.findall(r"<a [^>]*>", self.side):
            self.assertIn("data-goatcounter-click=", a, a)
