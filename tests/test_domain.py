"""领域原语单元测试。"""

import unittest
from datetime import date

from domain import (
    AsOf,
    Interval,
    annualized_capacity,
    can_advance,
    capacity_units,
    evidence_by_source,
    overlap_units,
    satisfied_gates,
    select_version,
)


class GateTest(unittest.TestCase):
    def test_exit_gates(self):
        ok, missing = can_advance("licensing", {"investment_agreement"})
        self.assertFalse(ok)
        self.assertEqual(missing, ["regulatory_license"])

        ok, missing = can_advance("licensing",
                                  {"investment_agreement", "regulatory_license"})
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_acceptance_requires_report_and_capacity(self):
        ok, missing = can_advance("acceptance", {"acceptance_report"})
        self.assertFalse(ok)
        self.assertEqual(missing, ["actual_capacity"])

    def test_news_is_not_evidence(self):
        gates = satisfied_gates([
            {"kind": "funding_news", "status": "published", "recorded_on": "2026-05-01"},
            {"kind": "regulatory_license", "status": "under_review",
             "recorded_on": "2026-06-01"},
        ])
        self.assertEqual(gates, set())

    def test_closed_evidence_satisfies_gate(self):
        gates = satisfied_gates([
            {"kind": "regulatory_license", "status": "approved",
             "recorded_on": "2026-03-01"},
        ])
        self.assertEqual(gates, {"regulatory_license"})

    def test_sources_kept_separate(self):
        buckets = evidence_by_source([
            {"kind": "investment_agreement"},
            {"kind": "acceptance_report"},
            {"kind": "actual_capacity"},
            {"kind": "regulatory_license"},
            {"kind": "funding_news"},
        ])
        self.assertEqual({k: len(v) for k, v in buckets.items()},
                         {"agreement": 1, "acceptance": 1, "capacity": 1,
                          "regulatory": 1})


class IntervalTest(unittest.TestCase):
    def test_half_open_overlap(self):
        a = Interval(date(2027, 1, 1), date(2028, 1, 1))
        touching = Interval(date(2028, 1, 1), date(2029, 1, 1))
        self.assertIsNone(a.intersection(touching))
        self.assertFalse(a.overlaps(touching))

    def test_capacity_units(self):
        iv = Interval(date(2030, 1, 1), date(2030, 7, 1))
        self.assertEqual(iv.days, 181)
        self.assertAlmostEqual(capacity_units(iv, 0.55), 99.55)

    def test_overlap_units(self):
        a = Interval(date(2027, 1, 1), date(2030, 7, 1))
        b = Interval(date(2027, 3, 1), date(2030, 12, 31))
        units = overlap_units(a, b, 1.0)
        # 重叠区间 2027-03-01 .. 2030-07-01
        self.assertAlmostEqual(units, (date(2030, 7, 1) - date(2027, 3, 1)).days)

    def test_annualized(self):
        self.assertAlmostEqual(
            annualized_capacity(90, days=180), 182.5)


class VersionSelectionTest(unittest.TestCase):
    VERSIONS = [
        {"version": 1, "effective_on": "2025-01-01", "recorded_on": "2025-01-01"},
        {"version": 2, "effective_on": "2026-08-01", "recorded_on": "2026-07-25"},
        # 事后补录：生效日在年内，但系统记录在年报发布之后
        {"version": 3, "effective_on": "2025-06-01", "recorded_on": "2026-03-01"},
    ]

    def test_historical_report_excludes_later_recorded(self):
        picked = select_version(
            self.VERSIONS, AsOf.parse("2025-12-31", "2025-12-31"))
        self.assertEqual(picked["version"], 1)

    def test_effective_but_unrecorded_future_event_hidden(self):
        # 迁移 2029 年才生效：在 2026 年视图中不可见
        versions = self.VERSIONS + [
            {"version": 4, "effective_on": "2029-06-01", "recorded_on": "2026-09-01"}]
        picked = select_version(versions, AsOf.parse("2026-09-21", "2026-09-21"))
        self.assertEqual(picked["version"], 2)
        picked = select_version(versions, AsOf.parse("2029-07-01", "2029-07-01"))
        self.assertEqual(picked["version"], 4)


if __name__ == "__main__":
    unittest.main()
