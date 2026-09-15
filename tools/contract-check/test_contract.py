import copy
import json
import unittest

from validate import CONTRACT, DAY, MAX_BYTES, Invalid, encode, project, validate, validate_snapshot


class ContractTests(unittest.TestCase):
    def fixture(self, name="normal"):
        return validate((CONTRACT / "fixtures" / f"{name}.json").read_bytes())

    def rejected(self, value, code):
        with self.assertRaises(Invalid) as caught:
            validate(encode(value))
        self.assertEqual(caught.exception.code, code)

    def test_stale_threshold_and_response_time(self):
        original = self.fixture()
        observed = original["observed_at"]
        self.assertEqual(project(original, observed + 179)["status"], "ok")
        old = project(original, observed + 180)
        self.assertEqual((old["status"], old["reason"], old["age_seconds"]),
                         ("stale", "too_old", 180))
        self.assertEqual(old["observed_at"], observed)
        self.assertEqual(old["updated_at"], original["updated_at"])
        self.assertEqual(original["age_seconds"], 0)

    def test_source_failure_retains_observation(self):
        original = self.fixture("auth-failed")
        later = project(original, original["as_of"] + 60)
        self.assertEqual(later["reason"], "source_error")
        self.assertEqual(later["age_seconds"], 120)
        for key in ("observed_at", "remaining_percent", "reset_at", "resets_available"):
            self.assertEqual(later[key], original[key])

    def test_stored_publication_and_api_response_are_distinct(self):
        original = self.fixture()
        validate_snapshot(encode(original))
        validate_snapshot(encode(self.fixture("unavailable-auth")))
        for value in (project(original, original["as_of"] + 1), self.fixture("unavailable")):
            with self.assertRaises(Invalid) as caught:
                validate_snapshot(encode(value))
            self.assertEqual(caught.exception.code, "semantics")

    def test_first_failure_requires_attempt_time(self):
        value = self.fixture("unavailable")
        value["source_error"] = "auth_failed"
        self.rejected(value, "semantics")

    def test_future_slot_opens_as_unknown_without_source(self):
        original = self.fixture()
        start = original["days"][3]["start_at"]
        before = project(original, start - 1)
        after = project(original, start)
        self.assertEqual(before["days"][3]["coverage"], "future")
        self.assertEqual(after["days"][3]["coverage"], "unknown")
        self.assertIsNone(after["days"][3]["used_delta_pp"])
        self.assertEqual(after["days"][:3], original["days"][:3])

    def test_time_alone_cannot_reset_cycle(self):
        original = self.fixture()
        after = project(original, original["reset_at"])
        self.assertEqual(after["reason"], "reset_due")
        self.assertEqual(after["cycle"], original["cycle"])
        self.assertEqual(after["remaining_percent"], original["remaining_percent"])
        self.assertNotIn("future", [d["coverage"] for d in after["days"]])

    def test_unknown_stays_unknown(self):
        original = self.fixture("unavailable")
        later = project(original, original["as_of"] + DAY)
        self.assertEqual(later["status"], "unavailable")
        self.assertIsNone(later["age_seconds"])
        self.assertTrue(all(d["used_delta_pp"] is None for d in later["days"]))

    def test_zero_count_is_not_missing(self):
        self.assertEqual(self.fixture("zero-remaining")["resets_available"], 0)
        self.assertIsNone(self.fixture("unknown-history")["resets_available"])

    def test_backwards_clock_is_stale(self):
        original = self.fixture()
        self.assertEqual(project(original, original["observed_at"] - 6)["reason"], "clock_error")
        allowed = project(original, original["observed_at"] - 5)
        self.assertEqual((allowed["status"], allowed["age_seconds"]), ("ok", 0))

    def test_backwards_clock_after_failed_publication_preserves_unknown(self):
        original = self.fixture()
        start = original["days"][3]["start_at"]
        published = project(original, start + 60)
        published.update(updated_at=start + 60, source_error="source_timeout", reason="source_error")
        validate_snapshot(encode(published))
        backwards = project(published, start - 60)
        self.assertEqual(backwards["reason"], "clock_error")
        self.assertEqual(backwards["days"][3]["coverage"], "unknown")
        self.assertIsNone(backwards["days"][3]["used_delta_pp"])

    def test_dst_slots_have_fixed_duration_not_unique_weekdays(self):
        spring, fall = self.fixture("dst-spring"), self.fixture("dst-fall")
        for item in (spring, fall):
            starts = [d["start_at"] for d in item["days"]]
            self.assertTrue(all(b - a == DAY for a, b in zip(starts, starts[1:])))
        self.assertEqual([d["label"] for d in fall["days"]][:2], ["Su", "Su"])
        self.assertEqual([d["label"] for d in spring["days"]][:2], ["Sa", "M"])

    def test_raw_limit_counts_whitespace(self):
        raw = encode(self.fixture())
        validate(raw + b" " * (MAX_BYTES - len(raw)))
        with self.assertRaises(Invalid) as caught:
            validate(raw + b" " * (MAX_BYTES + 1 - len(raw)))
        self.assertEqual(caught.exception.code, "oversized")

    def test_field_mutations_cannot_bypass_semantics(self):
        original = self.fixture()
        mutations = [
            ("age_seconds", None), ("status", "unavailable"),
            ("reason", "too_old"), ("scope", None),
            ("observed_at", None), ("updated_at", None),
        ]
        for key, bad in mutations:
            with self.subTest(key=key):
                value = copy.deepcopy(original)
                value[key] = bad
                self.rejected(value, "semantics")

    def test_missing_and_extra_fields_are_rejected(self):
        original = self.fixture()
        for key in original:
            with self.subTest(key=key):
                value = copy.deepcopy(original)
                del value[key]
                self.rejected(value, "schema")
        original["account_id"] = "must-not-export"
        self.rejected(original, "schema")

    def test_strict_json_and_numbers(self):
        for raw in (b'{"x":1,"x":2}', b'{}{}', b'{"x":Infinity}', b'{"x":1e999}', b'\xff'):
            with self.subTest(raw=raw[:40]), self.assertRaises(Invalid) as caught:
                validate(raw)
            self.assertEqual(caught.exception.code, "malformed")
        # Parsers may reject deep nesting themselves or parse it and reject the
        # array root against the schema. Neither outcome may escape as a crash.
        with self.assertRaises(Invalid) as caught:
            validate(b'[' * 2000 + b']' * 2000)
        self.assertIn(caught.exception.code, ("malformed", "schema"))
        value = self.fixture()
        value["version"] = True
        self.rejected(value, "version")

    def test_total_daily_delta_cannot_exceed_quota(self):
        value = self.fixture()
        value["days"][0]["used_delta_pp"] = 90
        self.rejected(value, "semantics")

    def test_snapshot_reencoding_remains_under_cap(self):
        manifest = json.loads((CONTRACT / "fixtures/manifest.json").read_text())
        for case in manifest:
            if case["expect"] == "valid" and case["kind"] == "usage":
                raw = (CONTRACT / "fixtures" / case["file"]).read_bytes()
                value = validate(raw)
                validate(encode(value))
                self.assertLessEqual(len(encode(value)), MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
