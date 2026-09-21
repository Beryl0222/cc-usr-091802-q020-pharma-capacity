"""合成演示场景。

把需求里点名的每一类问题都落成事件，可作为服务启动样例与测试夹具：

* 同一条共享产线被多个项目重复申报占用 -> 超售被拒、汇总只按占用去重；
* 企业只发了融资消息 -> 没有投资协议/验收/核产证据，阶段无法越级；
* 跨园区联合项目 -> 区域合计只出现一次，园区分项按分成；
* 并购、产线迁移、里程碑延期 -> 自生效日形成新版本；
* 统计口径修订（申报口径 -> 兑现口径）-> 规划合计随之变化；
* 年度报告发布后补录证据 -> 复现报告仍按发布当时证据；
* 商业材料（投资协议等）-> 仅授权用户可见；
* 关键供应商中断 -> 列受影响项目、承诺缺口与备选资源，待负责人确认。
"""

from .app import Application, DomainError


def build_demo(store) -> dict:
    app = Application(store)
    rejected = []

    def try_call(desc, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DomainError as exc:
            rejected.append({"case": desc, "error": exc.code,
                             "message": exc.message, "details": exc.details})
            return None

    # ---- 用户 ---------------------------------------------------------------
    app.register_user("u-admin", "产业推进组", ["admin"], occurred_at="2026-01-01")
    app.register_user("u-zhang", "张工（华星项目负责人）", [],
                      commercial_access=True, occurred_at="2026-01-01")
    app.register_user("u-li", "李经理（泰来项目负责人）", [],
                      occurred_at="2026-01-01")
    app.register_user("u-wang", "王访客（无商业授权）", [],
                      occurred_at="2026-01-01")

    # ---- 园区/主体/资源 ------------------------------------------------------
    app.register_entity("u-admin", "e-huaxin", "华星生物", "张江", "2026-01-01")
    app.register_entity("u-admin", "e-tailai", "泰来制药", "苏州", "2026-01-01")
    app.register_entity("u-admin", "e-huanjin", "焕金生物", "临港", "2029-01-01")

    app.register_facility("u-admin", "f-zj", "张江一号厂房", "张江", "2026-01-01")
    app.register_facility("u-admin", "f-sz", "苏州制剂厂房", "苏州", "2026-01-01")
    app.register_facility("u-admin", "f-lg", "临港共享制造厂房", "临港", "2026-01-01")

    app.register_line("u-admin", "l-fill", "长三角共享灌装线", "f-zj",
                      "vial/年", 10_000_000, ["fill-finish"], "2026-01-01")
    app.register_line("u-admin", "l-sz", "苏州备用灌装线", "f-sz",
                      "vial/年", 4_000_000, ["fill-finish"], "2026-01-01")
    app.register_equipment("u-admin", "q-fill", "高速灌装机", "f-zj",
                           ["fill-finish"], status="installed",
                           installed_at="2026-09-01", occurred_at="2026-09-01")
    app.register_equipment("u-admin", "q-pending", "未到位的冻干机", "f-zj",
                           ["lyophilization"], status="pending",
                           occurred_at="2026-09-01")
    app.register_platform("u-admin", "pf-mrna", "mRNA 药物关键技术平台",
                          "张江", ["mrna", "fill-finish"], "2026-01-01")
    app.declare_license("u-admin", "lic-a", "华星药品生产许可证",
                        status="approved", entity_id="e-huaxin",
                        approved_at="2028-01-20", occurred_at="2028-01-20")
    app.declare_license("u-admin", "lic-b", "泰来创新器械注册证",
                        status="pending", entity_id="e-tailai",
                        occurred_at="2027-03-01")
    app.register_supplier("u-admin", "s-api", "关键原辅料供应商甲",
                          ["sterile-api"], occurred_at="2026-01-01")
    app.register_supplier("u-admin", "s-api2", "关键原辅料供应商乙（备选）",
                          ["sterile-api"], occurred_at="2026-01-01")

    # ---- 项目 A：跨园区联合项目，全流程兑现 ----------------------------------
    app.register_project(
        "u-admin", "p-a", "华星 mRNA 创新制剂", "e-huaxin", "张江",
        partner_parks=["苏州"], product_type="innovative-device",
        responsible="u-zhang", target_year=2030, investment=30.0,
        target_revenue=120.0, attribution={"张江": 0.7, "苏州": 0.3},
        occurred_at="2026-02-01")
    app.declare_dependencies("u-admin", "p-a", [
        {"kind": "line", "ref_id": "l-fill"},
        {"kind": "equipment", "ref_id": "q-fill"},
        {"kind": "platform", "ref_id": "pf-mrna"},
        {"kind": "license", "ref_id": "lic-a"},
    ], "2026-02-01")
    # 共享产线 2029 年占用 600 万 vial
    app.declare_occupancy("u-admin", "occ-a-2029", "p-a", "l-fill",
                          2029, "vial/年", 6_000_000, "2026-03-01")
    app.advance_stage("u-admin", "p-a", "planned", "2026-02-10")
    # 投资协议是商业材料：仅授权人员可见
    app.record_evidence("u-admin", "ev-ia-a", "investment-agreement",
                        "华星投资协议（商业秘密）", "p-a", "2026-06-01",
                        confidential=True,
                        detail={"agreed_investment": 30.0})
    app.advance_stage("u-admin", "p-a", "equipment", "2026-10-01")
    # 验收里程碑承诺 2027-06-30
    app.declare_milestone("u-admin", "p-a", "m-accept", "产线验收",
                          "2027-06-30", "2026-10-05")
    app.record_evidence("u-admin", "ev-accept-a", "acceptance",
                        "共享灌装线验收材料", "p-a", "2027-06-20",
                        detail={"report_no": "YS-2027-006"})
    app.resolve_milestone("u-admin", "p-a", "m-accept", "passed",
                          evidence_id="ev-accept-a", occurred_at="2027-06-25")
    app.advance_stage("u-admin", "p-a", "accepted", "2027-07-05")
    # 产能里程碑原定 2028-02，后延期至 2028-05-15（延期形成新版本）
    app.declare_milestone("u-admin", "p-a", "m-capacity", "实际产能达产",
                          "2028-02-01", "2027-07-10")
    app.declare_milestone("u-admin", "p-a", "m-capacity", "实际产能达产（延期）",
                          "2028-05-15", "2028-01-18")
    app.advance_stage("u-admin", "p-a", "licensed", "2028-02-10")
    app.record_evidence("u-admin", "ev-cap-a1", "capacity-audit",
                        "2029 年度核产报告", "p-a", "2028-05-10",
                        detail={"source": "华东医药核产中心",
                                "lines": [{"line_id": "l-fill", "year": 2029,
                                           "qty": 5_800_000, "unit": "vial/年"}]})
    # 这份补充材料生效于 2028-11，但在年报发布之后才补录进系统
    app.record_evidence("u-admin", "ev-accept-a2", "acceptance",
                        "验收补充检测记录", "p-a", "2028-11-20",
                        recorded_at="2029-03-05T14:00:00",
                        detail={"report_no": "YS-2027-006-BC"})
    app.resolve_milestone("u-admin", "p-a", "m-capacity", "passed",
                          evidence_id="ev-cap-a1", occurred_at="2028-05-12")
    app.advance_stage("u-admin", "p-a", "operational", "2028-06-01")

    # ---- 项目 B：苏州项目，卡在许可，依赖将中断的供应商 ----------------------
    app.register_project(
        "u-admin", "p-b", "泰来创新介入器械", "e-tailai", "苏州",
        product_type="innovative-device", responsible="u-li",
        target_year=2030, investment=8.0, target_revenue=40.0,
        occurred_at="2027-02-01")
    app.declare_dependencies("u-admin", "p-b", [
        {"kind": "line", "ref_id": "l-sz"},
        {"kind": "license", "ref_id": "lic-b"},
        {"kind": "supplier", "ref_id": "s-api"},
    ], "2027-02-01")
    # 再占同一条共享产线 400 万：600+400=1000 万，恰好不超售
    app.declare_occupancy("u-admin", "occ-b-2029", "p-b", "l-fill",
                          2029, "vial/年", 4_000_000, "2027-03-01")
    app.record_evidence("u-admin", "ev-ia-b", "investment-agreement",
                        "泰来投资协议", "p-b", "2027-04-01")
    app.advance_stage("u-admin", "p-b", "planned", "2027-02-15")
    app.advance_stage("u-admin", "p-b", "equipment", "2027-09-01")
    app.record_evidence("u-admin", "ev-accept-b", "acceptance",
                        "泰来产线验收材料", "p-b", "2028-02-20")
    app.advance_stage("u-admin", "p-b", "accepted", "2028-03-10")
    app.declare_milestone("u-admin", "p-b", "m-license", "取得器械注册证",
                          "2029-12-31", "2028-03-15")

    # ---- 项目 C：只有融资新闻，申报百亿却未兑现 ------------------------------
    app.register_project(
        "u-admin", "p-c", "焕金智能药厂", "e-huanjin", "临港",
        product_type="biologics", responsible="u-wang",
        target_year=2030, investment=None, target_revenue=100.0,
        occurred_at="2029-05-01")
    app.declare_dependencies("u-admin", "p-c", [
        {"kind": "equipment", "ref_id": "q-pending"},
    ], "2029-05-01")
    app.advance_stage("u-admin", "p-c", "planned", "2029-05-10")
    # 融资消息：只佐证资金面，永远不能证明投产
    app.record_evidence("u-admin", "ev-fin-c", "financing-news",
                        "焕金生物完成 20 亿元 C 轮融资", "p-c", "2029-12-18",
                        detail={"headline": "完成 C 轮融资 20 亿元"})
    try_call(
        "仅凭融资新闻不能进入设备阶段",
        app.advance_stage, "u-admin", "p-c", "equipment", "2030-01-10")
    try_call(
        "共享产线已无剩余能力，第三个占用被拒绝",
        app.declare_occupancy, "u-admin", "occ-c-2029", "p-c", "l-fill",
        2029, "vial/年", 1_000_000, "2030-01-12")

    # ---- 并购、迁移、口径修订 ------------------------------------------------
    # 2029-06：华星并购泰来；并购前的年报仍按当时股权结构
    app.acquire_entity("u-admin", "e-tailai", "e-huaxin", "2029-06-01")
    # 2029-07：共享灌装线从张江迁移至临港（新版本，旧版本保留）
    app.relocate_line("u-admin", "l-fill", "f-lg", "2029-07-01")

    # 申报口径：进入规划即计入（2026 年起生效）
    app.revise_caliber("u-admin", "cal-declared", "2030 规划申报口径",
                       "2026-01-01", count_min_stage="planned",
                       require_audited_capacity=False)
    # 发布 2028 年度报告（冻结记录时刻 2029-02-10）
    app.publish_report("u-admin", "r2028", 2028, "2028-12-31",
                       "长三角医药园区 2028 年度能力兑现报告",
                       published_at="2029-02-10T09:00:00")
    # 2030 年起改用兑现口径：必须投产且有实际产能核证才计入
    app.revise_caliber("u-admin", "cal-real", "2030 能力兑现口径",
                       "2030-01-01", count_min_stage="operational",
                       require_audited_capacity=True)

    # ---- 供应商中断 ----------------------------------------------------------
    app.set_supplier_status("u-admin", "s-api", "disrupted", "2030-02-15")

    return {
        "users": ["u-admin", "u-zhang", "u-li", "u-wang"],
        "parks": ["张江", "苏州", "临港"],
        "projects": {"p-a": "跨园区联合、已投产",
                     "p-b": "许可未获批、供应商中断",
                     "p-c": "仅有融资新闻"},
        "reports": ["r2028"],
        "rejected_by_policy": rejected,
    }
