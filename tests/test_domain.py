"""领域规则契约测试。"""

import os
import tempfile
import unittest

from pharma.app import Application, DomainError
from pharma.scenario import build_demo
from pharma.store import EventStore
from pharma.world import build_world


def fresh():
    store = EventStore()
    app = Application(store)
    app.register_user("admin", "推进组", ["admin"], occurred_at="2026-01-01")
    app.register_user("owner", "负责人", [], commercial_access=True,
                      occurred_at="2026-01-01")
    app.register_user("other", "无关人员", [], occurred_at="2026-01-01")
    app.register_entity("admin", "e1", "华星", "张江", "2026-01-01")
    app.register_facility("admin", "f1", "一号厂房", "张江", "2026-01-01")
    app.register_line("admin", "l1", "共享灌装线", "f1", "vial/年",
                      10_000_000, ["fill"], "2026-01-01")
    return store, app


def make_project(app, pid="p1", responsible="admin", **kw):
    app.register_equipment("admin", f"q-{pid}", "灌装机", "f1", ["fill"],
                           status="installed", installed_at="2026-01-01",
                           occurred_at="2026-01-01")
    app.declare_license("admin", f"lic-{pid}", "生产许可", status="pending",
                        entity_id="e1", occurred_at="2026-01-01")
    app.register_project("admin", pid, "项目" + pid, "e1", "张江",
                         responsible=responsible, investment=10.0,
                         target_revenue=120.0, occurred_at="2026-01-01", **kw)
    app.declare_dependencies("admin", pid, [
        {"kind": "line", "ref_id": "l1"},
        {"kind": "equipment", "ref_id": f"q-{pid}"},
        {"kind": "license", "ref_id": f"lic-{pid}"},
    ], "2026-01-01")
    app.advance_stage("admin", pid, "planned", "2026-01-02")


class GateTest(unittest.TestCase):
    def test_financing_news_is_not_commissioning(self):
        _, app = fresh()
        make_project(app, "p1")
        app.record_evidence("admin", "ev-fin", "financing-news",
                            "完成巨额融资", "p1", "2026-02-01")
        with self.assertRaises(DomainError) as ctx:
            app.advance_stage("admin", "p1", "equipment", "2026-02-02")
        self.assertEqual(ctx.exception.code, "gate-blocked")
        reqs = {b["requirement"] for b in ctx.exception.details["blockers"]}
        self.assertIn("investment-agreement", reqs)

    def test_cannot_skip_stages(self):
        _, app = fresh()
        make_project(app, "p1")
        # registered 之后只能 planned；不能直接 equipment
        with self.assertRaises(DomainError):
            app.advance_stage("admin", "p1", "accepted", "2026-02-02")

    def test_full_gate_chain(self):
        _, app = fresh()
        make_project(app, "p1")
        # equipment：投资协议 + 设备已到位（make_project 已 installed）
        app.record_evidence("admin", "ev-ia", "investment-agreement",
                            "投资协议", "p1", "2026-02-01")
        app.advance_stage("admin", "p1", "equipment", "2026-03-01")
        # accepted：验收材料
        with self.assertRaises(DomainError) as ctx:
            app.advance_stage("admin", "p1", "accepted", "2026-04-01")
        self.assertIn("acceptance",
                      {b["requirement"] for b in ctx.exception.details["blockers"]})
        app.record_evidence("admin", "ev-ac", "acceptance", "验收材料",
                            "p1", "2026-04-10")
        app.advance_stage("admin", "p1", "accepted", "2026-04-15")
        # licensed：许可未获批
        with self.assertRaises(DomainError):
            app.advance_stage("admin", "p1", "licensed", "2026-05-01")
        app.declare_license("admin", "lic-p1", "生产许可", status="approved",
                            entity_id="e1", approved_at="2026-05-10",
                            occurred_at="2026-05-10")
        app.advance_stage("admin", "p1", "licensed", "2026-05-15")
        # operational：实际产能核证
        with self.assertRaises(DomainError):
            app.advance_stage("admin", "p1", "operational", "2026-06-01")
        app.record_evidence("admin", "ev-cap", "capacity-audit", "核产报告",
                            "p1", "2026-06-10",
                            detail={"lines": [{"line_id": "l1", "year": 2027,
                                               "qty": 9_000_000}]})
        app.advance_stage("admin", "p1", "operational", "2026-06-20")
        self.assertEqual(app.project_gate("p1", "2026-07-01")["current_stage"],
                         "operational")

    def test_equipment_not_in_place_blocks(self):
        _, app = fresh()
        app.register_equipment("admin", "q2", "冻干机", "f1", ["lyo"],
                               status="pending", occurred_at="2026-01-01")
        app.declare_license("admin", "lic2", "许可", status="approved",
                            entity_id="e1", occurred_at="2026-01-01")
        app.register_project("admin", "p2", "项目2", "e1", "张江",
                             responsible="admin", investment=5,
                             target_revenue=10, occurred_at="2026-01-01")
        app.declare_dependencies("admin", "p2", [
            {"kind": "equipment", "ref_id": "q2"},
            {"kind": "license", "ref_id": "lic2"}], "2026-01-01")
        app.advance_stage("admin", "p2", "planned", "2026-01-02")
        app.record_evidence("admin", "ia2", "investment-agreement", "协议",
                            "p2", "2026-01-05")
        with self.assertRaises(DomainError) as ctx:
            app.advance_stage("admin", "p2", "equipment", "2026-02-01")
        self.assertIn("equipment-in-place",
                      {b["requirement"] for b in ctx.exception.details["blockers"]})


class OccupancyTest(unittest.TestCase):
    def test_overcapacity_rejected_and_dedup(self):
        _, app = fresh()
        make_project(app, "p1")
        make_project(app, "p2")
        app.declare_occupancy("admin", "o1", "p1", "l1", 2029,
                              "vial/年", 7_000_000, "2026-02-01")
        with self.assertRaises(DomainError) as ctx:
            app.declare_occupancy("admin", "o2", "p2", "l1", 2029,
                                  "vial/年", 4_000_000, "2026-02-01")
        self.assertEqual(ctx.exception.code, "occupancy-rejected")
        self.assertEqual(ctx.exception.details["free"], 3_000_000)
        # 600 万恰好可以
        app.declare_occupancy("admin", "o2", "p2", "l1", 2029,
                              "vial/年", 3_000_000, "2026-02-01")
        cap = app.line_capacity("l1", 2029, "2026-03-01")
        self.assertEqual(cap["committed"], 10_000_000)
        occupants = {o["project_id"] for o in cap["occupants"]}
        self.assertEqual(occupants, {"p1", "p2"})

    def test_unit_mismatch(self):
        _, app = fresh()
        make_project(app, "p1")
        with self.assertRaises(DomainError) as ctx:
            app.declare_occupancy("admin", "o1", "p1", "l1", 2029,
                                  "kg/年", 100, "2026-02-01")
        self.assertEqual(ctx.exception.details["reason"], "unit-mismatch")

    def test_occupancy_revision_is_versioned_history_preserved(self):
        _, app = fresh()
        make_project(app, "p1")
        app.declare_occupancy("admin", "o1", "p1", "l1", 2029,
                              "vial/年", 8_000_000, "2026-02-01")
        # 2027 年调减为 500 万（同一占用 id 重新申报，新版本生效）
        app.declare_occupancy("admin", "o1", "p1", "l1", 2029,
                              "vial/年", 5_000_000, "2027-01-01")
        self.assertEqual(app.line_capacity("l1", 2029, "2027-06-01")["committed"],
                         5_000_000)
        # 2026 年的世界仍只看到旧版承诺
        self.assertEqual(app.line_capacity("l1", 2029, "2026-06-01")["committed"],
                         8_000_000)


class SummaryTest(unittest.TestCase):
    def test_cross_park_counted_once_and_split(self):
        _, app = fresh()
        make_project(app, "p1",
                     partner_parks=["苏州"],
                     attribution={"张江": 0.7, "苏州": 0.3})
        app.revise_caliber("admin", "cal", "申报口径", "2026-01-01",
                           "planned", False)
        s = app.summary(["张江", "苏州"], 2029, "2026-03-01")
        self.assertEqual(s["totals"]["project_count"], 1)
        self.assertEqual(s["cross_park_projects"], ["p1"])
        self.assertEqual(s["park_breakdown"]["张江"]["counted_revenue"], 84.0)
        self.assertEqual(s["park_breakdown"]["苏州"]["counted_revenue"], 36.0)

    def test_caliber_revision_changes_totals(self):
        _, app = fresh()
        make_project(app, "p1")
        app.revise_caliber("admin", "c1", "申报口径", "2026-01-01",
                           "planned", False)
        s1 = app.summary(["张江"], 2029, "2026-03-01")
        self.assertEqual(s1["totals"]["counted_project_count"], 1)
        app.revise_caliber("admin", "c2", "兑现口径", "2030-01-01",
                           "operational", True)
        s2 = app.summary(["张江"], 2029, "2030-03-01")
        self.assertEqual(s2["totals"]["counted_project_count"], 0)
        self.assertEqual(s2["totals"]["declared_target_revenue"], 120.0)

    def test_merger_groups_enterprise_dedup(self):
        store, app = fresh()
        app.register_entity("admin", "e2", "泰来", "苏州", "2026-01-01")
        make_project(app, "p1")
        app.register_project("admin", "p2", "泰来项目", "e2", "苏州",
                             responsible="admin", investment=2,
                             target_revenue=30, occurred_at="2026-01-01")
        app.advance_stage("admin", "p2", "planned", "2026-01-02")
        app.revise_caliber("admin", "cal", "申报口径", "2026-01-01",
                           "planned", False)
        # 并购前：两家集团
        w = build_world(store, "2028-12-31")
        s = app.summary(["张江", "苏州"], 2029, "2028-12-31")
        names = {e["entity_id"] for e in s["enterprises"]}
        self.assertEqual(names, {"e1", "e2"})
        # 并购自 2029-06 生效：合并为一个百亿集团
        app.acquire_entity("admin", "e2", "e1", "2029-06-01")
        s2 = app.summary(["张江", "苏州"], 2029, "2029-07-01")
        self.assertEqual([e["entity_id"] for e in s2["enterprises"]], ["e1"])
        self.assertEqual(s2["enterprises"][0]["counted_revenue"], 150.0)
        self.assertTrue(s2["enterprises"][0]["is_10b_enterprise"])
        # 历史时点复算仍是两家
        s3 = app.summary(["张江", "苏州"], 2029, "2029-05-01")
        self.assertEqual(len(s3["enterprises"]), 2)

    def test_audited_capacity_overrides_declared_in_basis(self):
        _, app = fresh()
        make_project(app, "p1")
        app.declare_occupancy("admin", "o1", "p1", "l1", 2030,
                              "vial/年", 9_000_000, "2026-02-01")
        app.record_evidence("admin", "cap", "capacity-audit", "核产",
                            "p1", "2029-05-01",
                            detail={"lines": [{"line_id": "l1", "year": 2030,
                                               "qty": 7_200_000}]})
        d = app.project_detail("p1", "2030-06-01")
        basis = d["capacity_basis"][0]
        self.assertEqual(basis["declared_qty"], 9_000_000)
        self.assertEqual(basis["counted_qty"], 7_200_000)
        self.assertEqual(basis["audited"]["evidence_id"], "cap")


class BitemporalReportTest(unittest.TestCase):
    def test_report_reproduction_excludes_late_recorded_evidence(self):
        store, app = fresh()
        make_project(app, "p1")
        app.record_evidence("admin", "ev-ac", "acceptance", "验收材料",
                            "p1", "2028-12-01",
                            recorded_at="2028-12-20T08:00:00")
        app.revise_caliber("admin", "cal", "申报口径", "2026-01-01",
                           "planned", False)
        app.publish_report("admin", "r2028", 2028, "2028-12-31", "2028 年报",
                           published_at="2029-02-01T09:00:00")
        # 年报发布后才补录一份验收补充材料（业务日期 2028-11）
        app.record_evidence("admin", "ev-ac-late", "acceptance", "验收补充",
                            "p1", "2028-11-01",
                            recorded_at="2029-03-10T10:00:00")
        rep = app.reproduce_report("r2028")
        row = rep["summary"]["drilldown"][0]
        self.assertEqual(row["gate"]["evidence"]["acceptance"], ["ev-ac"])
        # 即时视图能看到补录证据
        now = app.project_detail("p1", "2028-12-31")
        self.assertEqual(set(now["gate"]["evidence"]["acceptance"]),
                         {"ev-ac", "ev-ac-late"})

    def test_line_relocation_forms_version(self):
        _, app = fresh()
        app.register_facility("admin", "f2", "临港厂房", "临港", "2026-01-01")
        before = app.line_capacity("l1", 2029, "2029-06-01")
        self.assertEqual(before["line_id"], "l1")
        app.relocate_line("admin", "l1", "f2", "2029-07-01")
        w_after = build_world(app.store, "2029-08-01")
        w_before = build_world(app.store, "2029-06-01")
        self.assertEqual(w_after.lines["l1"].facility_id, "f2")
        self.assertEqual(w_after.lines["l1"].version, 1)
        self.assertEqual(w_before.lines["l1"].facility_id, "f1")

    def test_milestone_delay_versions_and_breach_blocks(self):
        _, app = fresh()
        make_project(app, "p1")
        app.declare_milestone("admin", "p1", "m1", "许可取得", "2027-01-01",
                              "2026-06-01")
        self.assertEqual(app.project_detail("p1", "2026-07-01")["milestones"][0]["version"], 0)
        app.declare_milestone("admin", "p1", "m1", "许可取得（延期）",
                              "2028-01-01", "2027-02-01")
        m = app.project_detail("p1", "2027-03-01")["milestones"][0]
        self.assertEqual(m["version"], 1)
        self.assertEqual(m["due"], "2028-01-01")
        # 延期后 2027 年中不算逾期
        gate = app.project_gate("p1", "2027-06-01")
        self.assertFalse(any(b["ref"] == "m1" for b in gate["blockers"]))
        # 到 2028-06 仍未通过 -> 承诺缺口
        gate2 = app.project_gate("p1", "2028-06-01")
        self.assertTrue(any(b["ref"] == "m1" for b in gate2["blockers"]))


class AccessControlTest(unittest.TestCase):
    def test_commercial_evidence_requires_authorization(self):
        _, app = fresh()
        make_project(app, "p1")
        app.record_evidence("admin", "secret", "investment-agreement",
                            "商业秘密投资协议", "p1", "2026-03-01",
                            confidential=True)
        anon = app.project_detail("p1", "2026-04-01")  # 无 user
        stranger = app.project_detail("p1", "2026-04-01", user_id="other")
        authed = app.project_detail("p1", "2026-04-01", user_id="owner")
        self.assertNotIn("secret", anon["gate"]["evidence"]["investment-agreement"])
        self.assertNotIn("secret", stranger["gate"]["evidence"]["investment-agreement"])
        self.assertIn("secret", authed["gate"]["evidence"]["investment-agreement"])


class ImpactTest(unittest.TestCase):
    def test_disruption_impact_and_owner_confirmation(self):
        _, app = fresh()
        app.register_supplier("admin", "s1", "供应商甲", ["api"],
                              occurred_at="2026-01-01")
        app.register_supplier("admin", "s2", "供应商乙", ["api"],
                              occurred_at="2026-01-01")
        make_project(app, "p1", responsible="owner")
        app.declare_dependencies("admin", "p1", [
            {"kind": "supplier", "ref_id": "s1"}], "2026-01-03")
        app.set_supplier_status("admin", "s1", "disrupted", "2027-05-01")
        imp = app.impact("supplier", "s1", "2027-05-02")
        self.assertEqual(imp["affected_project_count"], 1)
        self.assertTrue(imp["affected"][0]["pending_owner_confirmation"])
        self.assertIn({"kind": "supplier", "ref_id": "s2", "name": "供应商乙",
                       "capabilities": ["api"]}, imp["alternatives"])
        gap_types = {g["type"] for g in imp["affected"][0]["gaps"]}
        self.assertIn("dependency-unavailable", gap_types)
        # 非负责人不能确认处置
        with self.assertRaises(DomainError) as ctx:
            app.handle_disruption("other", "supplier", "s1", "p1", "switch",
                                  "2027-05-03",
                                  alternative={"kind": "supplier", "ref_id": "s2"})
        self.assertEqual(ctx.exception.code, "forbidden")
        # 切换必须给可选项
        with self.assertRaises(DomainError):
            app.handle_disruption("owner", "supplier", "s1", "p1", "switch",
                                  "2027-05-03")
        # 负责人确认后：缺口标记已处置
        app.handle_disruption("owner", "supplier", "s1", "p1", "switch",
                              "2027-05-03",
                              alternative={"kind": "supplier", "ref_id": "s2"})
        imp2 = app.impact("supplier", "s1", "2027-05-04")
        self.assertFalse(imp2["affected"][0]["pending_owner_confirmation"])
        handled = imp2["affected"][0]["handled_dispositions"][0]
        self.assertEqual(handled["action"], "switch")
        self.assertEqual(handled["alternative"]["ref_id"], "s2")

    def test_license_delay_impact_is_expedite_not_replace(self):
        _, app = fresh()
        make_project(app, "p1")
        imp = app.impact("license", "lic-p1", "2027-05-02")
        self.assertEqual(imp["alternatives"][0]["kind"], "action")


class DemoScenarioTest(unittest.TestCase):
    def test_demo_rejected_cases_and_totals(self):
        store = EventStore()
        info = build_demo(store)
        codes = {r["error"] for r in info["rejected_by_policy"]}
        self.assertEqual(codes, {"gate-blocked", "occupancy-rejected"})
        app = Application(store)
        s = app.summary(["张江", "苏州", "临港"], 2029, "2030-03-01")
        # 申报 260 亿，兑现口径只计已投产且核产的 120 亿
        self.assertEqual(s["totals"]["declared_target_revenue"], 260.0)
        self.assertEqual(s["totals"]["counted_target_revenue"], 120.0)
        self.assertEqual(s["totals"]["counted_project_count"], 1)
        self.assertEqual(s["cross_park_projects"], ["p-a"])
        # 2028 年报可复现且包含当时的两个项目
        rep = app.reproduce_report("r2028")
        self.assertEqual(rep["summary"]["totals"]["project_count"], 2)


class PersistenceTest(unittest.TestCase):
    def test_reload_replays_events_and_avoids_id_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            store = EventStore(path)
            app = Application(store)
            app.register_user("admin", "推进组", ["admin"], occurred_at="2026-01-01")
            max_before = max(e.id for e in store.all())
            del store, app
            # 重新打开：历史事件完整回放，新事件 id 不与旧 id 冲突
            store2 = EventStore(path)
            app2 = Application(store2)
            self.assertEqual(len(store2.all()), 1)
            app2.register_entity("admin", "e1", "华星", "张江", "2026-01-02")
            new_id = store2.all()[-1].id
            self.assertGreater(new_id, max_before)
            self.assertEqual(len({e.id for e in store2.all()}), 2)


if __name__ == "__main__":
    unittest.main()
