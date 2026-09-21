"""双时态世界回放。

把事件日志按 ``occurred_at`` 折叠，得到某一业务日期的世界状态；
再用 ``recorded_on_or_before`` 截断记录时间，就能复现历史年度报告。
并购、迁移、延期在折叠结果中表现为实体的新版本，旧版本仍可在更早的
as_of 日期下复现。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import events as E
from .store import EventStore


@dataclass
class Entity:
    id: str
    name: str
    park: Optional[str] = None
    acquired_by: Optional[str] = None          # 最新生效的并购方
    acquired_effective: Optional[str] = None


@dataclass
class Facility:
    id: str
    name: str
    park: str


@dataclass
class Line:
    id: str
    name: str
    facility_id: str
    capacity: Dict[str, object]                # {"unit": ..., "qpy": ...}
    capabilities: List[str] = field(default_factory=list)
    version: int = 0                           # 迁移等修订次数


@dataclass
class Equipment:
    id: str
    name: str
    facility_id: str
    capabilities: List[str] = field(default_factory=list)
    status: str = "installed"                  # installed / pending / removed
    installed_at: Optional[str] = None


@dataclass
class Platform:
    id: str
    name: str
    park: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)


@dataclass
class License:
    id: str
    name: str
    entity_id: Optional[str] = None
    project_id: Optional[str] = None
    scope: str = ""
    status: str = "pending"                    # pending / approved / rejected
    approved_at: Optional[str] = None


@dataclass
class Supplier:
    id: str
    name: str
    capabilities: List[str] = field(default_factory=list)
    status: str = "active"                     # active / disrupted
    disrupted_since: Optional[str] = None


@dataclass
class Dependency:
    kind: str                                  # line/equipment/platform/license/supplier
    ref_id: str
    required: bool = True
    note: str = ""


@dataclass
class Milestone:
    id: str
    name: str
    due: str
    status: str = "pending"                    # pending/passed/failed
    resolution: Optional[str] = None
    evidence_id: Optional[str] = None
    version: int = 0                           # 延期一次即升版


@dataclass
class Evidence:
    id: str
    kind: str
    title: str
    project_id: str
    occurred_at: str
    confidential: bool = False
    detail: Dict[str, object] = field(default_factory=dict)


@dataclass
class Occupancy:
    id: str
    project_id: str
    line_id: str
    year: int
    unit: str
    qty: float


@dataclass
class GateDecision:
    project_id: str
    stage: str
    decided_at: str
    actor: str
    evidence_ids: List[str] = field(default_factory=list)


@dataclass
class Disposition:
    node_kind: str
    node_id: str
    project_id: str
    action: str                                # wait / switch / release
    alternative: Optional[Dict[str, str]]
    decided_at: str
    actor: str
    note: str = ""


@dataclass
class Project:
    id: str
    name: str
    entity_id: str
    park: str                                  # 牵头园区
    partner_parks: List[str] = field(default_factory=list)
    product_type: str = ""
    responsible: Optional[str] = None
    target_year: Optional[int] = None
    investment: Optional[float] = None         # 协议投资额（亿元）
    target_revenue: Optional[float] = None     # 申报达纲产值（亿元）
    attribution: Dict[str, float] = field(default_factory=dict)  # 园区分成
    dependencies: Dict[Tuple[str, str], Dependency] = field(default_factory=dict)
    milestones: Dict[str, Milestone] = field(default_factory=dict)
    occupancies: Dict[str, Occupancy] = field(default_factory=dict)
    gates: List[GateDecision] = field(default_factory=list)

    @property
    def stage(self) -> str:
        decided = {g.stage for g in self.gates}
        for stage in reversed(E.STAGES):
            if stage in decided:
                return stage
        return "registered"

    def parks(self) -> List[str]:
        return [self.park] + [p for p in self.partner_parks if p != self.park]


@dataclass
class User:
    id: str
    name: str
    roles: List[str] = field(default_factory=list)   # planner / park-officer / admin
    commercial_access: bool = False


@dataclass
class Caliber:
    id: str
    effective_date: str
    name: str
    # 进入园区/区域规划合计的最低阶段：planned=按申报口径，accepted=按兑现口径
    count_min_stage: str = "planned"
    # 兑现口径要求的实际产能证据
    require_audited_capacity: bool = False
    version: int = 0


@dataclass
class World:
    as_of: str
    as_of_record: Optional[str]
    entities: Dict[str, Entity] = field(default_factory=dict)
    facilities: Dict[str, Facility] = field(default_factory=dict)
    lines: Dict[str, Line] = field(default_factory=dict)
    equipment: Dict[str, Equipment] = field(default_factory=dict)
    platforms: Dict[str, Platform] = field(default_factory=dict)
    licenses: Dict[str, License] = field(default_factory=dict)
    suppliers: Dict[str, Supplier] = field(default_factory=dict)
    projects: Dict[str, Project] = field(default_factory=dict)
    evidence: Dict[str, Evidence] = field(default_factory=dict)
    calibers: Dict[str, Caliber] = field(default_factory=dict)
    users: Dict[str, User] = field(default_factory=dict)
    dispositions: List[Disposition] = field(default_factory=list)
    report_publications: Dict[str, dict] = field(default_factory=dict)
    confidential_redacted: int = 0

    # ---- 查找与图 -----------------------------------------------------------

    def line_park(self, line: Line) -> str:
        fac = self.facilities.get(line.facility_id)
        return fac.park if fac else ""

    def entity_group(self, entity_id: str) -> str:
        """沿并购链返回 as_of 时点的最终集团主体。"""
        seen: Set[str] = set()
        cur = entity_id
        while cur and cur not in seen:
            seen.add(cur)
            ent = self.entities.get(cur)
            if ent and ent.acquired_by:
                cur = ent.acquired_by
            else:
                break
        return cur

    def dependents(self, kind: str, ref_id: str) -> List[Project]:
        """依赖某资源节点的全部项目（影响分析沿此边传播）。"""
        out = []
        for p in self.projects.values():
            if (kind, ref_id) in p.dependencies:
                out.append(p)
        return out

    def line_occupancies(self, line_id: str, year: Optional[int] = None) -> List[Occupancy]:
        out = []
        for p in self.projects.values():
            for o in p.occupancies.values():
                if o.line_id == line_id and (year is None or o.year == year):
                    out.append(o)
        return out

    def free_capacity(self, line_id: str, year: int) -> Dict[str, object]:
        """共享产线在某年的剩余能力（能力单位口径）。"""
        line = self.lines[line_id]
        used = sum(o.qty for o in self.line_occupancies(line_id, year)
                   if o.unit == line.capacity["unit"])
        qpy = float(line.capacity["qpy"])
        return {
            "line_id": line_id,
            "year": year,
            "unit": line.capacity["unit"],
            "capacity": qpy,
            "committed": used,
            "free": qpy - used,
        }

    def current_caliber(self) -> Optional[Caliber]:
        effective = [c for c in self.calibers.values() if c.effective_date <= self.as_of]
        if not effective:
            return None
        return max(effective, key=lambda c: (c.effective_date, c.version))

    def evidence_for(self, project_id: str, kind: str) -> List[Evidence]:
        return [v for v in self.evidence.values()
                if v.project_id == project_id and v.kind == kind]


def build_world(store: EventStore, as_of: str,
                as_of_record: Optional[str] = None,
                include_confidential: bool = True) -> World:
    """折叠事件日志。

    as_of: 业务生效日上界（valid time）。
    as_of_record: 记录时间上界（transaction time），复现历史报告时使用。
    """
    world = World(as_of=as_of, as_of_record=as_of_record)
    events = store.query(occurred_on_or_before=as_of,
                         recorded_on_or_before=as_of_record,
                         include_confidential=include_confidential)
    if not include_confidential:
        all_count = len(store.query(occurred_on_or_before=as_of,
                                    recorded_on_or_before=as_of_record))
        world.confidential_redacted = all_count - len(events)
    events.sort(key=lambda e: (e.occurred_at, e.id))

    for ev in events:
        p = ev.payload
        t = ev.type
        if t == E.ENTITY_REGISTERED:
            world.entities[p["id"]] = Entity(id=p["id"], name=p["name"], park=p.get("park"))
        elif t == E.ENTITY_ACQUIRED:
            ent = world.entities.get(p["entity_id"])
            if ent:
                ent.acquired_by = p["acquired_by"]
                ent.acquired_effective = ev.occurred_at
        elif t == E.FACILITY_REGISTERED:
            world.facilities[p["id"]] = Facility(p["id"], p["name"], p["park"])
        elif t == E.LINE_REGISTERED:
            world.lines[p["id"]] = Line(
                p["id"], p["name"], p["facility_id"],
                capacity=p["capacity"], capabilities=list(p.get("capabilities", [])))
        elif t == E.LINE_RELOCATED:
            line = world.lines.get(p["line_id"])
            if line:
                line.facility_id = p["facility_id"]
                line.version += 1
        elif t == E.EQUIPMENT_REGISTERED:
            world.equipment[p["id"]] = Equipment(
                p["id"], p["name"], p["facility_id"],
                capabilities=list(p.get("capabilities", [])),
                status=p.get("status", "installed"),
                installed_at=p.get("installed_at"))
        elif t == E.EQUIPMENT_STATUS_CHANGED:
            eq = world.equipment.get(p["equipment_id"])
            if eq:
                eq.status = p["status"]
                if p["status"] == "installed" and not eq.installed_at:
                    eq.installed_at = ev.occurred_at
        elif t == E.PLATFORM_REGISTERED:
            world.platforms[p["id"]] = Platform(
                p["id"], p["name"], p.get("park"), list(p.get("capabilities", [])))
        elif t == E.LICENSE_DECLARED:
            world.licenses[p["id"]] = License(
                p["id"], p["name"], p.get("entity_id"), p.get("project_id"),
                p.get("scope", ""), p.get("status", "pending"), p.get("approved_at"))
        elif t == E.SUPPLIER_REGISTERED:
            world.suppliers[p["id"]] = Supplier(
                p["id"], p["name"], list(p.get("capabilities", [])),
                p.get("status", "active"), p.get("disrupted_since"))
        elif t == E.SUPPLIER_STATUS_CHANGED:
            sup = world.suppliers.get(p["supplier_id"])
            if sup:
                sup.status = p["status"]
                sup.disrupted_since = ev.occurred_at if p["status"] == "disrupted" else None
        elif t == E.PROJECT_REGISTERED:
            world.projects[p["id"]] = Project(
                id=p["id"], name=p["name"], entity_id=p["entity_id"],
                park=p["park"], partner_parks=list(p.get("partner_parks", [])),
                product_type=p.get("product_type", ""),
                responsible=p.get("responsible"),
                target_year=p.get("target_year"),
                investment=p.get("investment"),
                target_revenue=p.get("target_revenue"),
                attribution=dict(p.get("attribution", {})))
        elif t == E.DEPENDENCY_DECLARED:
            proj = world.projects.get(p["project_id"])
            if proj:
                for d in p["dependencies"]:
                    dep = Dependency(d["kind"], d["ref_id"],
                                     d.get("required", True), d.get("note", ""))
                    proj.dependencies[(dep.kind, dep.ref_id)] = dep
        elif t == E.OCCUPANCY_DECLARED:
            proj = world.projects.get(p["project_id"])
            if proj:
                # 同一占用 id 重新申报即形成新版本：as_of 在新生效日之前时
                # 回放只看到旧值，历史报告因此可复现。
                proj.occupancies[p["id"]] = Occupancy(
                    p["id"], p["project_id"], p["line_id"], p["year"], p["unit"],
                    float(p["qty"]))
        elif t == E.MILESTONE_DECLARED:
            proj = world.projects.get(p["project_id"])
            if proj:
                m = proj.milestones.get(p["milestone_id"])
                if m is None:
                    proj.milestones[p["milestone_id"]] = Milestone(
                        p["milestone_id"], p["name"], p["due"])
                else:
                    m.due = p["due"]
                    m.name = p["name"]
                    m.version += 1
        elif t == E.MILESTONE_RESOLVED:
            proj = world.projects.get(p["project_id"])
            if proj:
                m = proj.milestones.get(p["milestone_id"])
                if m:
                    m.status = p["status"]
                    m.resolution = ev.occurred_at
                    m.evidence_id = p.get("evidence_id")
        elif t == E.GATE_DECIDED:
            proj = world.projects.get(p["project_id"])
            if proj:
                proj.gates.append(GateDecision(
                    p["project_id"], p["stage"], ev.occurred_at,
                    ev.actor or "", p.get("evidence_ids", [])))
        elif t == E.EVIDENCE_RECORDED:
            world.evidence[p["id"]] = Evidence(
                p["id"], p["kind"], p["title"], p["project_id"],
                ev.occurred_at, p.get("confidential", False),
                dict(p.get("detail", {})))
        elif t == E.CALIBER_REVISED:
            cid = p["id"]
            cal = world.calibers.get(cid)
            if cal is None:
                world.calibers[cid] = Caliber(
                    cid, p["effective_date"], p["name"],
                    p.get("count_min_stage", "planned"),
                    p.get("require_audited_capacity", False))
            else:
                cal.effective_date = p["effective_date"]
                cal.name = p["name"]
                cal.count_min_stage = p.get("count_min_stage", cal.count_min_stage)
                cal.require_audited_capacity = p.get(
                    "require_audited_capacity", cal.require_audited_capacity)
                cal.version += 1
        elif t == E.USER_REGISTERED:
            world.users[p["id"]] = User(
                p["id"], p["name"], list(p.get("roles", [])),
                p.get("commercial_access", False))
        elif t == E.REPORT_PUBLISHED:
            world.report_publications[p["report_id"]] = {
                "report_id": p["report_id"], "year": p["year"],
                "as_of": p["as_of"], "published_at": ev.recorded_at,
                "title": p.get("title", "")}
        elif t == E.DISRUPTION_HANDLED:
            world.dispositions.append(Disposition(
                p["node_kind"], p["node_id"], p["project_id"], p["action"],
                p.get("alternative"), ev.occurred_at, ev.actor or "", p.get("note", "")))
    return world
