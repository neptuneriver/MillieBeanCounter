"""The daily-sheet parser, on the real (sanitized) email and on variations."""

from __future__ import annotations

import unittest
from datetime import date
from zoneinfo import ZoneInfo

from fakes import fixture

from millie_sync.sheet import clock_text, email_bodies, parse_email, parse_lines, text_to_lines

TZ = ZoneInfo("America/New_York")

OCT_12 = [
    ("11:36 AM", "diaper", "both"),
    ("12:23 PM", "nap", "12:55 PM"),
    ("1:00 PM", "bottle", 5.0),
    ("1:54 PM", "diaper", "both"),
    ("2:40 PM", "nap", "3:26 PM"),
    ("3:30 PM", "bottle", 4.0),
    ("3:45 PM", "diaper", "pee"),
    ("4:28 PM", "diaper", "pee"),
    ("4:35 PM", "nap", "5:24 PM"),
]


def summary(sheet):
    out = []
    for e in sheet.events:
        detail = {"diaper": e.diaper, "nap": e.end and clock_text(e.end), "bottle": e.amount}[e.kind]
        out.append((clock_text(e.start), e.kind, detail))
    return out


def sheet_from(body: str):
    result = parse_lines(text_to_lines(body), TZ)
    return result


HEAD = "Emilia's Daily Sheet\n\nMonday, October 12th, 2026\n\nACTIVITIES\n"


class RealEmailTest(unittest.TestCase):
    def test_forwarded_email_both_bodies_agree(self):
        raw = fixture("forwarded-oct-12.eml")
        bodies = email_bodies(raw)
        self.assertEqual([kind for kind, _ in bodies], ["html", "text"])
        for kind, lines in bodies:
            result = parse_lines(lines, TZ)
            self.assertEqual(len(result.sheets), 1, kind)
            sheet = result.sheets[0]
            self.assertEqual((sheet.child, sheet.day), ("Emilia", date(2026, 10, 12)))
            self.assertEqual(summary(sheet), OCT_12, kind)
            self.assertEqual(sheet.problems, [])

    def test_uses_sheet_date_not_forward_date(self):
        sheet = parse_email(fixture("forwarded-oct-12.eml"), TZ).sheets[0]
        self.assertTrue(all(e.start.date() == date(2026, 10, 12) for e in sheet.events))
        self.assertEqual(sheet.events[0].start.utcoffset().total_seconds(), -4 * 3600)

    def test_bottle_details_and_teacher_comment(self):
        sheet = parse_email(fixture("forwarded-oct-12.eml"), TZ).sheets[0]
        bottles = [e for e in sheet.events if e.kind == "bottle"]
        self.assertEqual(
            [(b.amount, b.offered, b.units, b.milk) for b in bottles],
            [(5.0, 5.25, "oz", "Breast Milk"), (4.0, 5.25, "oz", "Breast Milk")],
        )
        self.assertIsNone(bottles[0].comment)
        self.assertEqual(bottles[1].comment, "And Mrs T.")
        self.assertEqual(sheet.comments, [])

    def test_original_kaymbu_email(self):
        result = parse_email(fixture("original-oct-12.eml"), TZ)
        self.assertEqual(summary(result.sheets[0]), OCT_12)


class VariationsTest(unittest.TestCase):
    def one(self, body: str):
        result = sheet_from(HEAD + body)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.sheets), 1)
        return result.sheets[0]

    def test_formula_and_other_units(self):
        sheet = self.one(
            "Bottle\nOffered 4 oz of formula and took 3.5 oz at 9:05am\nTook 120 ml of breast milk at 11:00 AM\n"
        )
        self.assertEqual(
            [(clock_text(e.start), e.amount, e.units, e.milk, e.offered) for e in sheet.events],
            [("9:05 AM", 3.5, "oz", "Formula", 4.0), ("11:00 AM", 120.0, "ml", "Breast Milk", None)],
        )

    def test_diaper_kinds(self):
        sheet = self.one(
            "Diaper\nWet diaper at 8:00am\nBM diaper at 9:00am\nWet/BM diaper at 10:00am\n"
            "Dry diaper at 11:00am\nBM / Wet diaper at 12:00pm\n"
        )
        self.assertEqual([e.diaper for e in sheet.events], ["pee", "poo", "both", "dry", "both"])

    def test_noon_and_midnight_hours(self):
        sheet = self.one("Nap\nNapped from 11:50am to 12:20pm (30 minutes)\n")
        self.assertEqual((sheet.events[0].start.hour, sheet.events[0].end.hour), (11, 12))

    def test_hour_long_nap_durations(self):
        sheet = self.one(
            "Nap\nNapped from 12:10pm to 1:15pm (1 hour 5 minutes)\nNapped from 2:00pm to 3:00pm (1 hour)\n"
        )
        self.assertEqual([e.minutes for e in sheet.events], [65, 60])

    def test_nap_whose_numbers_disagree_is_not_logged(self):
        sheet = self.one("Nap\nNapped from 12:23pm to 12:55pm (45 minutes)\n")
        self.assertEqual(sheet.events, [])
        self.assertEqual(len(sheet.problems), 1)

    def test_refused_bottle_is_a_note_not_an_entry(self):
        sheet = self.one("Bottle\nOffered 4 ounces of breastmilk and took 0 ounces at 1:00pm\n")
        self.assertEqual(sheet.events, [])
        self.assertEqual(sheet.problems, [])
        self.assertIn("didn't drink any", sheet.notes[0])

    def test_unknown_milk_needs_a_person(self):
        sheet = self.one("Bottle\nOffered 4 ounces of water and took 2 ounces at 1:00pm\n")
        self.assertEqual(sheet.events, [])
        self.assertIn("Couldn't read", sheet.problems[0])

    def test_took_more_than_offered_needs_a_person(self):
        sheet = self.one("Bottle\nOffered 4 ounces of breastmilk and took 5 ounces at 1:00pm\n")
        self.assertEqual(sheet.events, [])
        self.assertIn("more than was offered", sheet.problems[0])

    def test_new_wording_is_flagged_not_guessed(self):
        sheet = self.one(
            "Bottle\nOffered 5 ounces of breastmilk and took 5 ounces at 1:00pm\n"
            "Bottle around 3pm, about 4 oz\nNap\nFell asleep at 2:10pm\n"
        )
        self.assertEqual(len(sheet.events), 1)
        self.assertEqual(len(sheet.problems), 2)

    def test_meals_and_activities_are_ignored_quietly(self):
        sheet = self.one(
            "Bottle\nOffered 5 ounces of breastmilk and took 5 ounces at 1:00pm\n"
            "Meal\nAte all of her sweet potatoes at 11:30am\nMood\nHappy\n"
            "Diaper\nWet diaper at 3:45pm\n"
        )
        self.assertEqual([e.kind for e in sheet.events], ["bottle", "diaper"])
        self.assertEqual(sheet.problems, [])
        self.assertIsNone(sheet.events[0].comment)

    def test_duplicate_lines_are_logged_once(self):
        sheet = self.one("Diaper\nWet diaper at 3:45pm\nWet diaper at 3:45pm\n")
        self.assertEqual(len(sheet.events), 1)
        self.assertIn("Listed twice", sheet.notes[0])

    def test_comment_for_a_nap_has_nowhere_to_go_but_is_kept(self):
        sheet = self.one("Nap\nNapped from 12:23pm to 12:55pm (32 minutes)\nSlept great today!\n")
        self.assertEqual(sheet.events[0].comment, "Slept great today!")

    def test_wrong_weekday_stops_the_sheet(self):
        result = sheet_from("Emilia's Daily Sheet\nTuesday, October 12th, 2026\nDiaper\nWet diaper at 3:45pm\n")
        self.assertEqual(result.sheets, [])
        self.assertIn("wrong weekday", result.errors[0])

    def test_missing_date_stops_the_sheet(self):
        result = sheet_from("Emilia's Daily Sheet\nDiaper\nWet diaper at 3:45pm\n")
        self.assertEqual(result.sheets, [])
        self.assertIn("no date line", result.errors[0])

    def test_two_sheets_in_one_email(self):
        result = sheet_from(
            HEAD + "Diaper\nWet diaper at 3:45pm\n"
            "Emilia's Daily Sheet\nTuesday, October 13th, 2026\nDiaper\nBM diaper at 9:00am\n"
        )
        self.assertEqual([(s.day.day, s.events[0].diaper) for s in result.sheets], [(12, "pee"), (13, "poo")])

    def test_signature_and_links_are_not_read(self):
        sheet = self.one(
            "Nap\nNapped from 4:35pm to 5:24pm (49 minutes)\n<https://example.invalid/x>\n--\n"
            "Parent Name\nWet diaper at 9:00pm\n"
        )
        self.assertEqual([e.kind for e in sheet.events], ["nap"])

    def test_no_sheet_at_all(self):
        result = sheet_from("Hi! Just a note that Friday is picture day.\n")
        self.assertEqual((result.sheets, result.errors), ([], []))


if __name__ == "__main__":
    unittest.main()
