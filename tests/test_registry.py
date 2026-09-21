"""注册表业务规则测试：门禁、占用、去重、版本、复现、授权、影响传播。"""

import unittest

from domain import AsOf, GateError
from registry import Registry

FIXTURE = "fixtures/sample.json"


def load() -> Registry:
    return Registry.load(FIXTURE)


class GateAdvanceTest(unittest.TestCase):
    def setUp(self):
        self.reg = load()

    def test_license_not_approved_blocks_advance(self):
        # 惠鑫：许可 under_review，不能从 licensing 越级
        state = self.reg.state("proj-huixin-fill")
        self.assertEqual(state.stage, "licensing")
        with self.assertRaises(GateError) as ctx:
            self.reg.advance("proj-huixin-fill")
        self.assertEqual(ctx.exception.missing, ["regulatory_license"])
        denied = self.reg.decisions("proj-huixin-fill")[-1]
        self.assertFalse(denied["approved"])
        self.assertEqual(denied["to_stage"], "licensing")

    def test_funding_news_does_not_unblock_production(self):
        state = self.reg.state("proj-huixin-fill")
        kinds = {ev["kind"] for ev in state.evidence}
        self.assertIn("funding_news", kinds)
        self.assertNotIn("regulatory_license", state.satisfied)

    def test_acceptance_waits_on_verified_capacity(self):
        # 博达：验收报告完成，但实际产能 submitted 未核验，不能投产
        with self.assertRaises(GateError) as ctx:
            self.reg.advance("proj-boda-pack")
        self.assertEqual(ctx.exception.missing, ["actual_capacity"])

    def test_successful_advance_creates_version(self):
        # 瑞珂：注册证已批，设备 installed（未校准）→ 仍卡在 equipment 阶段
        state = self.reg.state("proj-ruike-device")
        self.assertEqual(state.stage, "equipment")
        with self.assertRaises(GateError):
            self.reg.advance("proj-ruike-device")

    def test_full_evidence_path_advances(self):
        # 安融已在 production；补一个可推进的构造：给博达补核验产能后可推进
        reg = load()
        reg.project("proj-boda-pack")["evidence"].append({
            "id": "ev-bd-cap-ok", "kind": "actual_capacity",
            "title": "实际产能核验通过", "status": "verified",
            "recorded_on": "2026-09-20", "amount": 90,
            "window_days": 180, "year": 2026, "unit": "万支",
        })
        result = reg.advance("proj-boda-pack", decided_on="2026-09-21")
        self.assertEqual(result["decision"]["approved"], True)
        self.assertEqual(result["state"].stage, "production")


class ReservationConflictTest(unittest.TestCase):
    def setUp(self):
        self.reg = load()

    def test_shared_line_overbooking_detected(self):
        conflicts = self.reg.conflicts()
        line_a = [c for c in conflicts if c["facility_id"] == "fac-line-a"]
        self.assertEqual(len(line_a), 1)
        c = line_a[0]
        self.assertEqual(set(c["projects"]),
                         {"proj-huixin-fill", "proj-ruike-device"})
        self.assertGreater(c["requested_units_per_day"],
                           c["capacity_units_per_day"])

    def test_within_capacity_no_conflict(self):
        conflicts = self.reg.conflicts()
        self.assertEqual([c for c in conflicts if c["facility_id"] == "fac-line-b"],
                         [])

    def test_occupancy_and_free_capacity(self):
        occ = self.reg.facility_occupancy("fac-line-a", "2028-01-01")
        self.assertAlmostEqual(occ["used_units_per_day"], 1.1)
        self.assertAlmostEqual(occ["free_units_per_day"], 0.0)
        occ_free = self.reg.facility_occupancy("fac-line-a", "2026-12-31")
        self.assertAlmostEqual(occ_free["used_units_per_day"], 0.0)
        self.assertAlmostEqual(occ_free["free_units_per_day"], 1.0)

    def test_relocation_resolves_conflict_after_effective_date(self):
        future = AsOf.parse("2029-07-01", "2029-07-01")
        conflicts = self.reg.conflicts(future)
        self.assertEqual([c for c in conflicts if c["facility_id"] == "fac-line-a"],
                         [])

    def test_future_relocation_invisible_today(self):
        # 迁移 2029 才生效，当前视图瑞珂仍占用线A
        state = self.reg.state("proj-ruike-device")
        self.assertEqual(state.bookings[0]["facility_id"], "fac-line-a")


class TotalsDedupTest(unittest.TestCase):
    def setUp(self):
        self.reg = load()

    def test_joint_project_counted_once_regionally(self):
        total = self.reg.totals(scope="region", scope_id="ALL")
        ids = [p["project_id"] for p in total["projects"]]
        self.assertEqual(ids.count("proj-ruike-device"), 1)
        # 各园区分别合计时联合项目在两个园区各可见
        binhai = self.reg.totals(scope="park", scope_id="park-binhai")
        taihu = self.reg.totals(scope="park", scope_id="park-taihu")
        self.assertIn("proj-ruike-device",
                      [p["project_id"] for p in binhai["projects"]])
        self.assertIn("proj-ruike-device",
                      [p["project_id"] for p in taihu["projects"]])
        # 区域项目数 = 各园区项目并集，不重复
        park_union = {p["project_id"] for t in (binhai, taihu) for p in t["projects"]}
        csj = self.reg.totals(scope="region", scope_id="csj")
        self.assertEqual({p["project_id"] for p in csj["projects"]}, park_union)

    def test_three_totals_columns(self):
        total = self.reg.totals(scope="region", scope_id="ALL")
        row = next(p for p in total["projects"]
                   if p["project_id"] == "proj-anrong-fill")
        self.assertEqual(row["claimed"], 180)
        self.assertGreater(row["booking_basis"], 0)
        # 90 万支 / 180 天 → 年化 182.5
        self.assertAlmostEqual(row["delivered"], 182.5)

    def test_drill_down_contains_basis_decisions_pending(self):
        total = self.reg.totals(scope="region", scope_id="ALL")
        huixin = next(p for p in total["projects"]
                      if p["project_id"] == "proj-huixin-fill")
        self.assertTrue(any(i["type"] == "missing_gate"
                            and i["gate"] == "regulatory_license"
                            for i in huixin["pending_items"]))
        self.assertTrue(any(i["type"] == "news_not_evidence"
                            for i in huixin["pending_items"]))
        self.assertTrue(any(i["type"] == "facility_conflict"
                            for i in huixin["pending_items"]))
        self.assertTrue(huixin["capacity_basis"])
        self.assertTrue(any(not d["approved"] for d in huixin["decisions"]))

    def test_revised_caliber_excludes_unsubstantiated(self):
        plan = self.reg.totals(scope="region", scope_id="ALL",
                               caliber_id="cal-2025-plan")
        revised = self.reg.totals(scope="region", scope_id="ALL",
                                  caliber_id="cal-2026-evidenced")
        # 新口径：规划阶段项目不进合计（元泽），惠鑫仅按预订依据计入头条
        yz_plan = [p for p in plan["projects"] if p["project_id"] == "proj-yuanze-cgt"]
        yz_rev = [p for p in revised["projects"] if p["project_id"] == "proj-yuanze-cgt"]
        self.assertTrue(yz_plan)
        self.assertFalse(yz_rev)
        huixin_rev = next(p for p in revised["projects"]
                          if p["project_id"] == "proj-huixin-fill")
        self.assertLess(huixin_rev["reserved"], huixin_rev["claimed"])


class VersionEventsTest(unittest.TestCase):
    def setUp(self):
        self.reg = load()

    def test_extension_creates_effective_version(self):
        # 延期已存在于样例：2026-07-25 记录、2026-08-01 生效
        before = self.reg.state("proj-yuanze-cgt",
                                AsOf.parse("2026-07-31", "2026-07-31"))
        self.assertEqual(before.version["version"], 1)
        after = self.reg.state("proj-yuanze-cgt",
                               AsOf.parse("2026-08-01", "2026-08-01"))
        self.assertEqual(after.version["event"], "extension")
        self.assertEqual(after.version["milestones"][0]["due_on"], "2026-10-01")

    def test_merger_swaps_subject_from_effective_date(self):
        before = self.reg.state("proj-yuanze-cgt",
                                AsOf.parse("2026-12-31", "2026-12-31"))
        self.assertEqual(before.version["enterprise_id"], "ent-yuanze")
        after = self.reg.state("proj-yuanze-cgt",
                               AsOf.parse("2027-01-01", "2027-01-01"))
        self.assertEqual(after.version["enterprise_id"], "ent-beimu")
        self.assertIn("北慕", after.version["name"])

    def test_external_event_validation(self):
        with self.assertRaises(ValueError):
            self.reg.new_version("proj-anrong-fill", "created", "2026-10-01", {})
        with self.assertRaises(ValueError):
            self.reg.new_version("proj-anrong-fill", "merger", "2026-01-01", {})


class ReportReproductionTest(unittest.TestCase):
    def setUp(self):
        self.reg = load()

    def test_2025_report_reproduced_with_then_evidence(self):
        rep = self.reg.reproduce_report("rpt-2025")
        self.assertTrue(rep["reproduced"])
        totals = rep["totals"]
        ids = {p["project_id"] for p in totals["projects"]}
        # 2025-12-31 视角：安融在 acceptance（2025-02 换版），瑞珂在 licensing，
        # 博达/元泽不在长三角口径；2026 年才记录的许可、产能核验都不出现
        anrong = next(p for p in totals["projects"]
                      if p["project_id"] == "proj-anrong-fill")
        self.assertEqual(anrong["stage"], "acceptance")
        self.assertEqual(anrong["delivered"], 0.0)
        # 被拦截的越级决定记录于 2026-08，不出现在 2025 年报
        self.assertNotIn("proj-huixin-fill", ids)

    def test_current_view_differs_from_historical(self):
        rep = self.reg.reproduce_report("rpt-2025")
        current = self.reg.totals(scope="region", scope_id="csj")
        self.assertNotEqual(
            rep["totals"]["headline_total"], current["headline_total"])


class AuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.reg = load()

    def test_commercial_evidence_hidden_without_authorization(self):
        detail = self.reg.project_detail("proj-boda-pack")
        self.assertGreater(detail["redacted_commercial_evidence"], 0)
        self.assertFalse(any(ev.get("commercial") for ev in detail["evidence"]))

    def test_authorized_principal_sees_commercial(self):
        detail = self.reg.project_detail("proj-boda-pack", principal_id="u-bai")
        self.assertEqual(detail["redacted_commercial_evidence"], 0)
        self.assertTrue(any(ev.get("commercial") for ev in detail["evidence"]))

    def test_authorization_is_project_scoped(self):
        # 白雪只授权了博达项目，看其他项目时商业材料仍不可见（构造一份）
        reg = load()
        reg.project("proj-anrong-fill")["evidence"].append({
            "id": "ev-ar-secret", "kind": "acceptance_report",
            "title": "保密商业附录", "status": "completed",
            "recorded_on": "2026-09-01", "commercial": True,
        })
        detail = reg.project_detail("proj-anrong-fill", principal_id="u-bai")
        self.assertTrue(all(not ev.get("commercial") for ev in detail["evidence"]))
        # admin 可见全部
        admin = reg.project_detail("proj-anrong-fill", principal_id="u-lin")
        self.assertTrue(any(ev.get("commercial") for ev in admin["evidence"]))


class ImpactPropagationTest(unittest.TestCase):
    def setUp(self):
        self.reg = load()

    def test_supplier_disruption_lists_affected_and_gaps(self):
        result = self.reg.analyze_impact(supplier_id="sup-nordic")
        affected = {p["project_id"]: p for p in result["affected_projects"]}
        self.assertIn("proj-boda-pack", affected)
        # 元泽以前置项目依赖博达，应作为下游受影响传播到
        self.assertIn("proj-yuanze-cgt", affected)
        self.assertGreater(affected["proj-yuanze-cgt"]["propagation_level"], 1)
        boda = affected["proj-boda-pack"]
        self.assertTrue(any(g["milestone_id"] == "ms-bd-1"
                            for g in boda["commitment_gaps"]))
        # 可选资源：同类替代供应商
        suppliers = {a["supplier_id"] for a in result["alternatives"]["suppliers"]}
        self.assertIn("sup-aosen", suppliers)
        self.assertNotIn("sup-nordic", suppliers)

    def test_issue_trigger_equivalent(self):
        result = self.reg.analyze_impact(issue_id="ISS-001")
        ids = {p["project_id"] for p in result["affected_projects"]}
        self.assertIn("proj-boda-pack", ids)

    def test_disposition_requires_owner(self):
        with self.assertRaises(ValueError):
            self.reg.confirm_disposition(
                "proj-boda-pack", "ISS-001", "", "switch_supplier",
                alternative_id="sup-aosen")

    def test_disposition_recorded_with_owner(self):
        rec = self.reg.confirm_disposition(
            "proj-boda-pack", "ISS-001", "周敏（园区产业推进组）",
            "switch_supplier", alternative_id="sup-aosen",
            note="改用奥森冷链备件，9月底复核产能核验排期")
        self.assertEqual(rec["status"], "confirmed")
        self.assertEqual(rec["owner"], "周敏（园区产业推进组）")
        # 系统本身不自动修改项目阶段或预订——处置由负责人确认后另行执行
        state = self.reg.state("proj-boda-pack")
        self.assertEqual(state.stage, "acceptance")


if __name__ == "__main__":
    unittest.main()
