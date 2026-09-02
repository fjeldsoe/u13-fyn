"""Tests for build.py. Kun standardbiblioteket, kør med:

    python -m unittest test_build -v
"""

import re
import unittest
from datetime import date, datetime, timezone

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


def row(tid, age_group_id, class_code):
    return {"tournamentID": tid, "ageGroupID": age_group_id, "classCode": class_code}


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

    def test_class_rows_are_filtered_by_the_requested_age_group(self):
        data = api(
            [admin(1, "2026-10-01")],
            [row(1, 4, "A"), row(1, 3, "B")],
        )
        u13 = build.parse(data, TODAY, age_group_id=4)
        u11 = build.parse(data, TODAY, age_group_id=3)
        self.assertEqual(u13[0]["raekker"], ["A"])
        self.assertEqual(u11[0]["raekker"], ["B"])


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


if __name__ == "__main__":
    unittest.main()
