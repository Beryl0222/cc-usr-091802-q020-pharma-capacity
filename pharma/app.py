"""应用服务：把命令校验后追加为事件，并提供双时态查询。

写路径只做一件事——校验并追加不可变事件；所有状态由事件回放得到。
读路径按用户权限折叠世界，再调用 ``engine`` 的纯函数。
"""

from typing import List, Optional

from . import events as E
from .engine import gate_report, impact_analysis, occupancy_violation, region_summary
from .store import EventStore
from .world import build_world

VALID_DEP_KINDS = {"line", "equipment", "platform", "license", "supplier"}
VALID_ACTIONS = {"wait", "switch", "release"}


class DomainError(Exception):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"error": self.code, "message": self.message, "details": self.details}


class Application:
    def __init__(self, store: EventStore):
        self.store = store

    # ---- 内部工具 -----------------------------------------------------------

    def _world(self, as_of: str, user_id: Optional[str] = None,
               as_of_record: Optional[str] = None):
        include_confidential = self._can_see_commercial(user_id, as_of)
        return build_world(self.store, as_of, as_of_record,
                           include_confidential=include_confidential)

    def _can_see_commercial(self, user_id: Optional[str], as_of: str) -> bool:
        if not user_id:
            return False
        # 用最新世界查用户权限（权限只增不减地反映当前授权状态）
        latest = build_world(self.store, "9999-12-31")
        user = latest.users.get(user_id)
        return bool(user and user.commercial_access)

    def _user(self, user_id: str):
        latest = build_world(self.store, "9999-12-31")
        user = latest.users.get(user_id)
        if user is None:
            raise DomainError("unknown-user", f"用户不存在：{user_id}")
        return user

    def _require_responsible_or_admin(self, user_id: str, project):
        user = self._user(user_id)
        if "admin" in user.roles:
            return
        if project.responsible and project.responsible != user_id:
            raise DomainError(
                "forbidden",
                f"该处置须由项目负责人 {project.responsible} 确认，当前用户 {user_id} 无权操作")
        if not project.responsible:
            raise DomainError("no-responsible", "项目尚未指定负责人，无法确认处置")

    def _append(self, etype, payload, occurred_at, user_id,
                confidential=False, recorded_at=None):
        self.store.append(E.Event(
            type=etype, payload=payload, occurred_at=occurred_at,
            recorded_at=recorded_at, actor=user_id, confidential=confidential))

    # ---- 登记类命令 ---------------------------------------------------------

    def register_user(self, user_id, name, roles=None, commercial_access=False,
                      occurred_at="2026-01-01", **_):
        self._append(E.USER_REGISTERED,
                     {"id": user_id, "name": name, "roles": roles or [],
                      "commercial_access": commercial_access},
                     occurred_at, user_id)
        return {"id": user_id}

    def register_entity(self, user_id, entity_id, name, park=None,
                        occurred_at="2026-01-01", **_):
        self._append(E.ENTITY_REGISTERED,
                     {"id": entity_id, "name": name, "park": park},
                     occurred_at, user_id)
        return {"id": entity_id}

    def acquire_entity(self, user_id, entity_id, acquired_by,
                       occurred_at, **_):
        """企业并购：自 occurred_at 生效，形成新版本归属。"""
        self._append(E.ENTITY_ACQUIRED,
                     {"entity_id": entity_id, "acquired_by": acquired_by},
                     occurred_at, user_id)
        return {"entity_id": entity_id, "acquired_by": acquired_by,
                "effective_date": occurred_at}

    def register_facility(self, user_id, facility_id, name, park,
                          occurred_at="2026-01-01", **_):
        self._append(E.FACILITY_REGISTERED,
                     {"id": facility_id, "name": name, "park": park},
                     occurred_at, user_id)
        return {"id": facility_id}

    def register_line(self, user_id, line_id, name, facility_id, unit, qpy,
                      capabilities=None, occurred_at="2026-01-01", **_):
        world = build_world(self.store, occurred_at)
        if facility_id not in world.facilities:
            raise DomainError("unknown-facility", f"厂房不存在：{facility_id}")
        self._append(E.LINE_REGISTERED,
                     {"id": line_id, "name": name, "facility_id": facility_id,
                      "capacity": {"unit": unit, "qpy": float(qpy)},
                      "capabilities": capabilities or []},
                     occurred_at, user_id)
        return {"id": line_id}

    def relocate_line(self, user_id, line_id, facility_id, occurred_at, **_):
        """产线跨园区迁移：自生效日起换厂房，历史版本保留。"""
        world = build_world(self.store, occurred_at)
        if line_id not in world.lines:
            raise DomainError("unknown-line", f"产线不存在：{line_id}")
        if facility_id not in world.facilities:
            raise DomainError("unknown-facility", f"厂房不存在：{facility_id}")
        self._append(E.LINE_RELOCATED,
                     {"line_id": line_id, "facility_id": facility_id},
                     occurred_at, user_id)
        return {"line_id": line_id, "facility_id": facility_id,
                "version": world.lines[line_id].version + 1}

    def register_equipment(self, user_id, equipment_id, name, facility_id,
                           capabilities=None, status="pending",
                           installed_at=None, occurred_at="2026-01-01", **_):
        world = build_world(self.store, occurred_at)
        if facility_id not in world.facilities:
            raise DomainError("unknown-facility", f"厂房不存在：{facility_id}")
        self._append(E.EQUIPMENT_REGISTERED,
                     {"id": equipment_id, "name": name, "facility_id": facility_id,
                      "capabilities": capabilities or [], "status": status,
                      "installed_at": installed_at},
                     occurred_at, user_id)
        return {"id": equipment_id}

    def set_equipment_status(self, user_id, equipment_id, status, occurred_at, **_):
        if status not in ("pending", "installed", "removed"):
            raise DomainError("bad-status", f"设备状态非法：{status}")
        world = build_world(self.store, occurred_at)
        if equipment_id not in world.equipment:
            raise DomainError("unknown-equipment", f"设备不存在：{equipment_id}")
        self._append(E.EQUIPMENT_STATUS_CHANGED,
                     {"equipment_id": equipment_id, "status": status},
                     occurred_at, user_id)
        return {"equipment_id": equipment_id, "status": status}

    def register_platform(self, user_id, platform_id, name, park=None,
                          capabilities=None, occurred_at="2026-01-01", **_):
        self._append(E.PLATFORM_REGISTERED,
                     {"id": platform_id, "name": name, "park": park,
                      "capabilities": capabilities or []},
                     occurred_at, user_id)
        return {"id": platform_id}

    def declare_license(self, user_id, license_id, name, status="pending",
                        entity_id=None, project_id=None, scope="",
                        approved_at=None, occurred_at="2026-01-01", **_):
        if status not in ("pending", "approved", "rejected"):
            raise DomainError("bad-status", f"许可状态非法：{status}")
        self._append(E.LICENSE_DECLARED,
                     {"id": license_id, "name": name, "status": status,
                      "entity_id": entity_id, "project_id": project_id,
                      "scope": scope, "approved_at": approved_at},
                     occurred_at, user_id)
        return {"id": license_id}

    def register_supplier(self, user_id, supplier_id, name, capabilities=None,
                          status="active", occurred_at="2026-01-01", **_):
        self._append(E.SUPPLIER_REGISTERED,
                     {"id": supplier_id, "name": name,
                      "capabilities": capabilities or [], "status": status},
                     occurred_at, user_id)
        return {"id": supplier_id}

    def set_supplier_status(self, user_id, supplier_id, status, occurred_at, **_):
        if status not in ("active", "disrupted"):
            raise DomainError("bad-status", f"供应商状态非法：{status}")
        world = build_world(self.store, occurred_at)
        if supplier_id not in world.suppliers:
            raise DomainError("unknown-supplier", f"供应商不存在：{supplier_id}")
        self._append(E.SUPPLIER_STATUS_CHANGED,
                     {"supplier_id": supplier_id, "status": status},
                     occurred_at, user_id)
        return {"supplier_id": supplier_id, "status": status}

    # ---- 项目 ---------------------------------------------------------------

    def register_project(self, user_id, project_id, name, entity_id, park,
                         partner_parks=None, product_type="", responsible=None,
                         target_year=None, investment=None, target_revenue=None,
                         attribution=None, occurred_at="2026-01-01", **_):
        world = build_world(self.store, occurred_at)
        if entity_id not in world.entities:
            raise DomainError("unknown-entity", f"企业主体不存在：{entity_id}")
        if responsible and responsible not in world.users:
            raise DomainError("unknown-user", f"负责人不存在：{responsible}")
        self._append(E.PROJECT_REGISTERED,
                     {"id": project_id, "name": name, "entity_id": entity_id,
                      "park": park, "partner_parks": partner_parks or [],
                      "product_type": product_type, "responsible": responsible,
                      "target_year": target_year, "investment": investment,
                      "target_revenue": target_revenue,
                      "attribution": attribution or {}},
                     occurred_at, user_id)
        return {"id": project_id, "stage": "registered"}

    def declare_dependencies(self, user_id, project_id, dependencies,
                             occurred_at="2026-01-01", **_):
        world = build_world(self.store, occurred_at)
        if project_id not in world.projects:
            raise DomainError("unknown-project", f"项目不存在：{project_id}")
        norm = []
        for d in dependencies:
            if d["kind"] not in VALID_DEP_KINDS:
                raise DomainError("bad-dependency", f"依赖类型非法：{d['kind']}")
            norm.append({"kind": d["kind"], "ref_id": d["ref_id"],
                         "required": d.get("required", True),
                         "note": d.get("note", "")})
        self._append(E.DEPENDENCY_DECLARED,
                     {"project_id": project_id, "dependencies": norm},
                     occurred_at, user_id)
        return {"project_id": project_id, "dependencies": len(norm)}

    def declare_occupancy(self, user_id, occupancy_id, project_id, line_id,
                          year, unit, qty, occurred_at, recorded_at=None, **_):
        """申报共享产线的年度能力占用；超售（含其他项目已占用量）将被拒绝。"""
        world = build_world(self.store, occurred_at)
        if project_id not in world.projects:
            raise DomainError("unknown-project", f"项目不存在：{project_id}")
        violation = occupancy_violation(world, line_id, int(year), unit,
                                        float(qty), ignore_occupancy_id=occupancy_id)
        if violation:
            raise DomainError("occupancy-rejected",
                               "共享产线能力占用未通过：" + violation["reason"],
                               violation)
        self._append(E.OCCUPANCY_DECLARED,
                     {"id": occupancy_id, "project_id": project_id,
                      "line_id": line_id, "year": int(year),
                      "unit": unit, "qty": float(qty)},
                     occurred_at, user_id, recorded_at=recorded_at)
        return {"id": occupancy_id, "line_id": line_id, "year": int(year),
                "qty": float(qty)}

    def declare_milestone(self, user_id, project_id, milestone_id, name, due,
                          occurred_at, **_):
        """承诺里程碑；同一 id 再次申报即延期升版，从 occurred_at 生效。"""
        world = build_world(self.store, occurred_at)
        if project_id not in world.projects:
            raise DomainError("unknown-project", f"项目不存在：{project_id}")
        existing = world.projects[project_id].milestones.get(milestone_id)
        self._append(E.MILESTONE_DECLARED,
                     {"project_id": project_id, "milestone_id": milestone_id,
                      "name": name, "due": due},
                     occurred_at, user_id)
        return {"milestone_id": milestone_id, "due": due,
                "version": (existing.version + 1) if existing else 0}

    def resolve_milestone(self, user_id, project_id, milestone_id, status,
                          evidence_id=None, occurred_at=None, **_):
        occurred_at = occurred_at or "2026-01-01"
        world = build_world(self.store, occurred_at)
        project = world.projects.get(project_id)
        if project is None:
            raise DomainError("unknown-project", f"项目不存在：{project_id}")
        if milestone_id not in project.milestones:
            raise DomainError("unknown-milestone", f"里程碑不存在：{milestone_id}")
        if status not in ("passed", "failed"):
            raise DomainError("bad-status", f"里程碑结论非法：{status}")
        if status == "passed" and not evidence_id:
            raise DomainError("evidence-required", "里程碑通过必须绑定证据")
        self._append(E.MILESTONE_RESOLVED,
                     {"project_id": project_id, "milestone_id": milestone_id,
                      "status": status, "evidence_id": evidence_id},
                     occurred_at, user_id)
        return {"milestone_id": milestone_id, "status": status}

    # ---- 证据：三类来源严格分开 ---------------------------------------------

    def record_evidence(self, user_id, evidence_id, kind, title, project_id,
                        occurred_at, confidential=False, detail=None,
                        recorded_at=None, **_):
        if kind not in E.EVIDENCE_KINDS:
            raise DomainError("bad-evidence-kind",
                              f"证据类别非法：{kind}，允许：{E.EVIDENCE_KINDS}")
        world = build_world(self.store, occurred_at)
        if project_id not in world.projects:
            raise DomainError("unknown-project", f"项目不存在：{project_id}")
        # 融资新闻永远不能充当投产/验收证据，单独标记并拒绝越类登记
        self._append(E.EVIDENCE_RECORDED,
                     {"id": evidence_id, "kind": kind, "title": title,
                      "project_id": project_id,
                      "confidential": bool(confidential),
                      "detail": detail or {}},
                     occurred_at, user_id,
                     confidential=bool(confidential), recorded_at=recorded_at)
        return {"id": evidence_id, "kind": kind}

    # ---- 阶段闸门 -----------------------------------------------------------

    def advance_stage(self, user_id, project_id, target_stage, occurred_at, **_):
        """进入下一阶段。许可未获批/设备未到位/验收未完成/无实际产能核证时拒绝。"""
        world = build_world(self.store, occurred_at)
        project = world.projects.get(project_id)
        if project is None:
            raise DomainError("unknown-project", f"项目不存在：{project_id}")
        report = gate_report(world, project)
        if target_stage != report["next_stage"]:
            raise DomainError(
                "stage-skip",
                f"项目当前阶段 {report['current_stage']}，"
                f"下一阶段只能是 {report['next_stage']}，不能越级到 {target_stage}")
        if not report["can_advance"]:
            raise DomainError(
                "gate-blocked",
                f"闸门未通过，不能进入 {target_stage}",
                {"blockers": report["blockers"]})
        self._require_responsible_or_admin(user_id, project)
        evidence_ids = []
        for ids in report["evidence"].values():
            evidence_ids += ids
        self._append(E.GATE_DECIDED,
                     {"project_id": project_id, "stage": target_stage,
                      "evidence_ids": sorted(set(evidence_ids))},
                     occurred_at, user_id)
        return {"project_id": project_id, "stage": target_stage, "decided_by": user_id}

    # ---- 口径与年报 ---------------------------------------------------------

    def revise_caliber(self, user_id, caliber_id, name, effective_date,
                       count_min_stage="planned",
                       require_audited_capacity=False, **_):
        if count_min_stage not in E.STAGES:
            raise DomainError("bad-stage", f"口径最低阶段非法：{count_min_stage}")
        self._append(E.CALIBER_REVISED,
                     {"id": caliber_id, "name": name,
                      "effective_date": effective_date,
                      "count_min_stage": count_min_stage,
                      "require_audited_capacity": require_audited_capacity},
                     effective_date, user_id)
        return {"id": caliber_id, "effective_date": effective_date}

    def publish_report(self, user_id, report_id, year, as_of, title="",
                       published_at=None, **_):
        """发布年度报告：冻结记录时刻，之后补录的证据不影响该报告。"""
        self._append(E.REPORT_PUBLISHED,
                     {"report_id": report_id, "year": int(year), "as_of": as_of,
                      "title": title},
                     as_of, user_id, recorded_at=published_at)
        return {"report_id": report_id, "year": int(year),
                "as_of": as_of, "published_at": published_at}

    # ---- 影响处置（负责人确认）---------------------------------------------

    def handle_disruption(self, user_id, node_kind, node_id, project_id, action,
                          occurred_at, alternative=None, note="", **_):
        if node_kind not in VALID_DEP_KINDS:
            raise DomainError("bad-dependency", f"节点类型非法：{node_kind}")
        if action not in VALID_ACTIONS:
            raise DomainError("bad-action",
                              f"处置动作非法：{action}，允许：{sorted(VALID_ACTIONS)}")
        world = build_world(self.store, occurred_at)
        project = world.projects.get(project_id)
        if project is None:
            raise DomainError("unknown-project", f"项目不存在：{project_id}")
        if (node_kind, node_id) not in project.dependencies:
            raise DomainError(
                "not-a-dependency",
                f"项目并未依赖 {node_kind}:{node_id}，无需处置")
        self._require_responsible_or_admin(user_id, project)
        if action == "switch":
            if not alternative or "kind" not in alternative or "ref_id" not in alternative:
                raise DomainError("alternative-required",
                                  "切换资源必须给出 alternative{kind, ref_id}")
            alt = self._check_alternative(world, node_kind, alternative)
            alternative = alt
        self._append(E.DISRUPTION_HANDLED,
                     {"node_kind": node_kind, "node_id": node_id,
                      "project_id": project_id, "action": action,
                      "alternative": alternative, "note": note},
                     occurred_at, user_id)
        return {"project_id": project_id, "action": action,
                "alternative": alternative, "decided_by": user_id}

    @staticmethod
    def _check_alternative(world, node_kind, alternative):
        kind, ref_id = alternative["kind"], alternative["ref_id"]
        if kind == "supplier":
            sup = world.suppliers.get(ref_id)
            if sup is None:
                raise DomainError("unknown-alternative", f"备选供应商不存在：{ref_id}")
            if sup.status != "active":
                raise DomainError("alternative-unavailable",
                                  f"备选供应商 {ref_id} 当前状态 {sup.status}")
            return {"kind": "supplier", "ref_id": ref_id, "name": sup.name}
        if kind == "line":
            ln = world.lines.get(ref_id)
            if ln is None:
                raise DomainError("unknown-alternative", f"备选产线不存在：{ref_id}")
            return {"kind": "line", "ref_id": ref_id, "name": ln.name}
        if kind == "equipment":
            eq = world.equipment.get(ref_id)
            if eq is None or eq.status != "installed":
                raise DomainError("alternative-unavailable",
                                  f"备选设备 {ref_id} 不可用")
            return {"kind": "equipment", "ref_id": ref_id, "name": eq.name}
        raise DomainError("bad-alternative", f"不支持的备选类型：{kind}")

    # ---- 查询 ---------------------------------------------------------------

    def project_detail(self, project_id, as_of, user_id=None):
        world = self._world(as_of, user_id)
        project = world.projects.get(project_id)
        if project is None:
            raise DomainError("unknown-project", f"项目不存在：{project_id}")
        from .engine import _project_drilldown
        return _project_drilldown(world, project, int(as_of[:4]))

    def project_gate(self, project_id, as_of, user_id=None):
        world = self._world(as_of, user_id)
        project = world.projects.get(project_id)
        if project is None:
            raise DomainError("unknown-project", f"项目不存在：{project_id}")
        return gate_report(world, project)

    def summary(self, parks, year, as_of, user_id=None):
        world = self._world(as_of, user_id)
        return region_summary(world, list(parks), int(year))

    def impact(self, node_kind, node_id, as_of, user_id=None, until=None):
        world = self._world(as_of, user_id)
        return impact_analysis(world, node_kind, node_id, until)

    def line_capacity(self, line_id, year, as_of, user_id=None):
        world = self._world(as_of, user_id)
        if line_id not in world.lines:
            raise DomainError("unknown-line", f"产线不存在：{line_id}")
        free = world.free_capacity(line_id, int(year))
        free["occupants"] = [
            {"project_id": o.project_id, "qty": o.qty, "unit": o.unit}
            for o in world.line_occupancies(line_id, int(year))]
        return free

    def reproduce_report(self, report_id, user_id=None):
        """按报告发布当时的证据原样复现（双时态：as_of + 发布时刻截断）。"""
        latest = build_world(self.store, "9999-12-31")
        pub = latest.report_publications.get(report_id)
        if pub is None:
            raise DomainError("unknown-report", f"报告不存在：{report_id}")
        world = self._world(pub["as_of"], user_id, as_of_record=pub["published_at"])
        parks = sorted({p.park for p in world.projects.values()})
        return {
            "report": pub,
            "reproduced_at_record_boundary": pub["published_at"],
            "summary": region_summary(world, parks, int(pub["year"])),
        }


COMMANDS = {
    "register_user": Application.register_user,
    "register_entity": Application.register_entity,
    "acquire_entity": Application.acquire_entity,
    "register_facility": Application.register_facility,
    "register_line": Application.register_line,
    "relocate_line": Application.relocate_line,
    "register_equipment": Application.register_equipment,
    "set_equipment_status": Application.set_equipment_status,
    "register_platform": Application.register_platform,
    "declare_license": Application.declare_license,
    "register_supplier": Application.register_supplier,
    "set_supplier_status": Application.set_supplier_status,
    "register_project": Application.register_project,
    "declare_dependencies": Application.declare_dependencies,
    "declare_occupancy": Application.declare_occupancy,
    "declare_milestone": Application.declare_milestone,
    "resolve_milestone": Application.resolve_milestone,
    "record_evidence": Application.record_evidence,
    "advance_stage": Application.advance_stage,
    "revise_caliber": Application.revise_caliber,
    "publish_report": Application.publish_report,
    "handle_disruption": Application.handle_disruption,
}
