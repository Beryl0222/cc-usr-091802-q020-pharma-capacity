"""纯领域规则：阶段闸门、能力占用、汇总去重、影响分析。

所有函数只读取 :class:`~pharma.world.World`，不写事件，便于用双时态
回放出的任意历史世界复算。
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from . import events as E
from .world import World, Project

STAGE_ORDER = ["registered"] + list(E.STAGES)


def stage_index(stage: str) -> int:
    return STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 0


# ---- 阶段闸门 -----------------------------------------------------------------

@dataclass
class Blocker:
    requirement: str
    reason: str
    ref: Optional[str] = None
    kind: str = "blocker"            # blocker / warning


def _dependency_status_blockers(world: World, project: Project) -> List[Blocker]:
    out: List[Blocker] = []
    for (kind, ref_id), dep in project.dependencies.items():
        if not dep.required:
            continue
        if kind == "line":
            if ref_id not in world.lines:
                out.append(Blocker("line-exists", "依赖产线尚未登记", ref_id))
        elif kind == "equipment":
            eq = world.equipment.get(ref_id)
            if eq is None:
                out.append(Blocker("equipment-exists", "依赖设备尚未登记", ref_id))
            elif eq.status != "installed":
                out.append(Blocker("equipment-in-place",
                                   f"设备未到位（{eq.status}）", ref_id))
        elif kind == "platform":
            if ref_id not in world.platforms:
                out.append(Blocker("platform-exists", "依赖技术平台尚未登记", ref_id))
        elif kind == "license":
            lic = world.licenses.get(ref_id)
            if lic is None:
                out.append(Blocker("license-exists", "依赖许可尚未申报", ref_id))
            elif lic.status != "approved":
                out.append(Blocker("license-certificate",
                                   f"许可未获批（{lic.status}）", ref_id))
        elif kind == "supplier":
            sup = world.suppliers.get(ref_id)
            if sup is None:
                out.append(Blocker("supplier-exists", "依赖供应商尚未登记", ref_id))
            elif sup.status == "disrupted" and not _switched(world, project.id, kind, ref_id):
                out.append(Blocker("supplier-active",
                                   "关键供应商中断，且尚无负责人确认的替代处置", ref_id))
    return out


def _switched(world: World, project_id: str, kind: str, ref_id: str) -> bool:
    return any(
        d.project_id == project_id and d.node_kind == kind and d.node_id == ref_id
        and d.action == "switch" and d.alternative
        for d in world.dispositions)


def _evidence_ids(world: World, project_id: str, kind: str) -> List[str]:
    return [v.id for v in world.evidence_for(project_id, kind)]


def _overdue_milestones(world: World, project: Project) -> List[Blocker]:
    out = []
    for m in project.milestones.values():
        if m.due <= world.as_of and m.status != "passed":
            out.append(Blocker(
                "milestone-kept",
                f"里程碑「{m.name}」{m.status}（承诺 {m.due}，第 {m.version + 1} 版）",
                m.id))
    return out


def gate_report(world: World, project: Project) -> dict:
    """返回项目逐阶段闸门核对结果。

    投资协议、验收材料、实际产能分别核对各自类别的证据——融资新闻
    （financing-news）不满足任何闸门，因此「刚融资」不会被记为「已投产」。
    """
    dep_blockers = _dependency_status_blockers(world, project)
    milestones = _overdue_milestones(world, project)

    per_stage: Dict[str, List[Blocker]] = {s: [] for s in E.STAGES}
    equipment_reqs = {"line-exists", "equipment-exists", "equipment-in-place",
                      "platform-exists", "supplier-exists", "supplier-active"}
    per_stage["equipment"] += [
        b for b in dep_blockers if b.requirement in equipment_reqs]
    if not _evidence_ids(world, project.id, "investment-agreement"):
        per_stage["equipment"].append(
            Blocker("investment-agreement", "缺少投资协议证据"))
    per_stage["accepted"] += [
        b for b in dep_blockers
        if b.requirement in ("equipment-in-place", "equipment-exists")]
    if not _evidence_ids(world, project.id, "acceptance"):
        per_stage["accepted"].append(Blocker("acceptance", "验收未完成：缺少验收材料证据"))
    per_stage["licensed"] += [
        b for b in dep_blockers if b.requirement in (
            "license-certificate", "license-exists")]
    if not any(k == "license" for k, _ in project.dependencies):
        per_stage["licensed"].append(
            Blocker("license-declared", "项目未声明任何监管许可依赖"))
    per_stage["operational"].append(
        Blocker("capacity-audit", "缺少实际产能核证证据")
        if not _evidence_ids(world, project.id, "capacity-audit")
        else Blocker("capacity-audit", "", kind="ok"))
    if per_stage["operational"][-1].kind == "ok":
        per_stage["operational"].clear()
    # 逾期/失败的承诺里程碑阻止任何越级
    for stage in ("equipment", "accepted", "licensed", "operational"):
        per_stage[stage] += milestones

    current_idx = stage_index(project.stage)
    cumulative: List[Blocker] = []
    next_stage = None
    # STAGE_ORDER 比 STAGES 多一个前置 registered，因此 STAGES 的枚举下标
    # 恰好等于「当前阶段的 STAGE_ORDER 下标」时，该阶段就是下一阶段。
    for idx, stage in enumerate(E.STAGES):
        cumulative = cumulative + per_stage[stage]
        if idx == current_idx:
            next_stage = stage
            break
    # 注册态进入 planned 只需登记信息；进入其后阶段逐道累计核对
    can_advance = next_stage is not None and not cumulative

    return {
        "project_id": project.id,
        "current_stage": project.stage,
        "next_stage": next_stage,
        "can_advance": can_advance,
        "blockers": [b.__dict__ for b in cumulative],
        "stages": {s: [b.__dict__ for b in bs] for s, bs in per_stage.items()},
        "evidence": {
            "investment-agreement": _evidence_ids(world, project.id, "investment-agreement"),
            "acceptance": _evidence_ids(world, project.id, "acceptance"),
            "capacity-audit": _evidence_ids(world, project.id, "capacity-audit"),
            "financing-news": _evidence_ids(world, project.id, "financing-news"),
        },
        "decisions": [
            {"stage": g.stage, "decided_at": g.decided_at, "actor": g.actor,
             "evidence_ids": g.evidence_ids}
            for g in project.gates],
    }


# ---- 能力占用 -----------------------------------------------------------------

def occupancy_violation(world: World, line_id: str, year: int,
                        unit: str, qty: float,
                        ignore_occupancy_id: Optional[str] = None) -> Optional[dict]:
    """校验共享产线在某年度按能力单位的占用是否超售。"""
    line = world.lines.get(line_id)
    if line is None:
        return {"reason": "line-unknown", "ref": line_id}
    if unit != line.capacity["unit"]:
        return {"reason": "unit-mismatch",
                "ref": line_id, "expected": line.capacity["unit"], "got": unit}
    used = 0.0
    occupants = []
    for occ in world.line_occupancies(line_id, year):
        if occ.id == ignore_occupancy_id:
            continue
        used += occ.qty
        occupants.append({"project_id": occ.project_id, "qty": occ.qty})
    capacity = float(line.capacity["qpy"])
    if used + qty > capacity + 1e-9:
        return {"reason": "overcapacity", "line_id": line_id, "year": year,
                "unit": unit, "capacity": capacity, "already_committed": used,
                "requested": qty, "free": capacity - used, "occupants": occupants}
    return None


def audited_capacity(world: World, project_id: str, line_id: str, year: int):
    """取该项目该产线该年度最新一份实际产能核证值；无核证返回 None。"""
    audits = [v for v in world.evidence_for(project_id, "capacity-audit")
              if v.occurred_at <= world.as_of]
    best = None
    for v in sorted(audits, key=lambda x: x.occurred_at):
        for row in v.detail.get("lines", []):
            if row.get("line_id") == line_id and row.get("year") == year:
                best = {"qty": float(row["qty"]), "unit": row.get("unit"),
                        "evidence_id": v.id, "audited_at": v.occurred_at,
                        "source": v.detail.get("source", "")}
    return best


# ---- 规划汇总（去重 + 下钻）---------------------------------------------------

def _counted(world: World, project: Project):
    """项目在当前口径下是否计入规划合计，以及计入的产能/产值依据。"""
    caliber = world.current_caliber()
    min_stage = caliber.count_min_stage if caliber else "planned"
    stage_ok = stage_index(project.stage) >= stage_index(min_stage)
    audits = [v for v in world.evidence_for(project.id, "capacity-audit")]
    audit_ok = True
    if caliber and caliber.require_audited_capacity:
        audit_ok = bool(audits)
    return stage_ok and audit_ok, caliber


def capacity_basis(world: World, project: Project, year: int) -> List[dict]:
    """每个占用的去重产能依据：申报占用 vs 核证实际产能，分别保留来源。"""
    out = []
    for occ in project.occupancies.values():
        if occ.year != year:
            continue
        line = world.lines.get(occ.line_id)
        audited = audited_capacity(world, project.id, occ.line_id, year)
        out.append({
            "line_id": occ.line_id,
            "line_name": line.name if line else None,
            "unit": occ.unit,
            "declared_qty": occ.qty,                 # 投资/申报口径
            "audited": audited,                      # 实际产能来源（可能为 None）
            "counted_qty": audited["qty"] if audited else occ.qty,
            "line_total_capacity": line.capacity["qpy"] if line else None,
        })
    return out


def _project_drilldown(world: World, project: Project, year: int) -> dict:
    counted, caliber = _counted(world, project)
    return {
        "project_id": project.id,
        "name": project.name,
        "entity_id": project.entity_id,
        "group_entity_id": world.entity_group(project.entity_id),
        "park": project.park,
        "partner_parks": project.partner_parks,
        "parks": project.parks(),
        "stage": project.stage,
        "counted": counted,
        "caliber": {"id": caliber.id, "name": caliber.name, "version": caliber.version,
                    "min_stage": caliber.count_min_stage,
                    "require_audited_capacity": caliber.require_audited_capacity}
        if caliber else None,
        "target_revenue": project.target_revenue,
        "investment": project.investment,
        "attribution": project.attribution,
        "capacity_basis": capacity_basis(world, project, year),
        "gate": gate_report(world, project),
        "open_items": [b.__dict__ for b in
                       _dependency_status_blockers(world, project)
                       + _overdue_milestones(world, project)],
        "milestones": [
            {"id": m.id, "name": m.name, "due": m.due, "status": m.status,
             "version": m.version, "resolution": m.resolution,
             "evidence_id": m.evidence_id}
            for m in project.milestones.values()],
    }


def region_summary(world: World, parks: List[str], year: int) -> dict:
    """区域合计：跨园区联合项目在区域中只出现一次。

    园区分项按 attribution 分成（无分成约定时归入牵头园区）；
    区域口径直接按去重项目计，杜绝联合项目两边各算一遍。
    """
    parks = list(parks)
    eligible = [p for p in world.projects.values() if set(p.parks()) & set(parks)]
    seen = set()
    rows = []
    park_revenue = {pk: 0.0 for pk in parks}
    park_investment = {pk: 0.0 for pk in parks}
    park_projects = {pk: [] for pk in parks}
    cross_park = []
    declared_revenue = 0.0
    counted_revenue = 0.0
    counted_investment = 0.0

    for p in sorted(eligible, key=lambda x: x.id):
        if p.id in seen:
            continue
        seen.add(p.id)
        detail = _project_drilldown(world, p, year)
        rows.append(detail)
        declared_revenue += p.target_revenue or 0.0
        if detail["counted"]:
            counted_revenue += p.target_revenue or 0.0
            counted_investment += p.investment or 0.0
        involved = [pk for pk in p.parks() if pk in parks]
        if len(involved) > 1:
            cross_park.append(p.id)
        for pk in involved:
            share = p.attribution.get(pk, 1.0 if pk == p.park else 0.0)
            park_revenue[pk] += (p.target_revenue or 0.0) * share
            park_investment[pk] += (p.investment or 0.0) * share
            park_projects[pk].append(
                {"project_id": p.id, "share": share, "counted_once_in_region": True})

    # 百亿元企业：按 as_of 时点并购后的集团汇总
    groups: Dict[str, float] = {}
    group_names: Dict[str, str] = {}
    detail_by_id = {r["project_id"]: r for r in rows}
    for p in eligible:
        detail = detail_by_id[p.id]
        if not detail["counted"]:
            continue
        gid = world.entity_group(p.entity_id)
        groups[gid] = groups.get(gid, 0.0) + (p.target_revenue or 0.0)
        group_names[gid] = world.entities[gid].name if gid in world.entities else gid
    ten_billion = sorted(
        ({"entity_id": gid, "name": group_names[gid], "counted_revenue": rev,
          "is_10b_enterprise": rev >= 100.0}
         for gid, rev in groups.items()),
        key=lambda r: -r["counted_revenue"])

    # 创新器械、关键技术平台（去重）
    devices = sorted({p.id for p in eligible
                      if p.product_type == "innovative-device"
                      and detail_by_id[p.id]["counted"]})
    counted_projects = {p.id for p in eligible
                        if detail_by_id[p.id]["counted"]}
    platform_ids = sorted({
        ref for p in eligible if p.id in counted_projects
        for kind, ref in p.dependencies if kind == "platform"})

    # 共享产线占用（去重后的占用方清单）
    line_ids = sorted({occ.line_id for p in eligible
                       for occ in p.occupancies.values() if occ.year == year})
    lines = []
    for lid in line_ids:
        free = world.free_capacity(lid, year)
        free["occupants"] = [
            {"project_id": o.project_id, "qty": o.qty}
            for o in world.line_occupancies(lid, year)]
        lines.append(free)

    caliber = world.current_caliber()
    return {
        "as_of": world.as_of,
        "year": year,
        "parks": parks,
        "caliber": {"id": caliber.id, "name": caliber.name,
                    "version": caliber.version,
                    "effective_date": caliber.effective_date,
                    "count_min_stage": caliber.count_min_stage,
                    "require_audited_capacity": caliber.require_audited_capacity}
        if caliber else {"count_min_stage": "planned"},
        "totals": {
            "project_count": len(rows),
            "counted_project_count": len(counted_projects),
            "declared_target_revenue": round(declared_revenue, 4),
            "counted_target_revenue": round(counted_revenue, 4),
            "counted_investment": round(counted_investment, 4),
            "innovative_device_count": len(devices),
            "key_platform_count": len(platform_ids),
        },
        "enterprises": ten_billion,
        "park_breakdown": {
            pk: {"counted_revenue": round(park_revenue[pk], 4),
                 "counted_investment": round(park_investment[pk], 4),
                 "projects": park_projects[pk]}
            for pk in parks},
        "cross_park_projects": cross_park,
        "shared_lines": lines,
        "innovative_devices": devices,
        "key_platforms": [
            {"platform_id": pid,
             "name": world.platforms[pid].name if pid in world.platforms else pid}
            for pid in platform_ids],
        "drilldown": rows,
        "confidential_redacted_events": world.confidential_redacted,
    }


# ---- 影响分析 -----------------------------------------------------------------

def impact_analysis(world: World, kind: str, ref_id: str,
                    until: Optional[str] = None) -> dict:
    """供应商中断 / 审批延迟后沿依赖关系的影响清单。

    系统只负责列影响、缺口和可选项；切换供应商等处置必须由项目负责人
    确认后才进入事件日志（见 Application.handle_disruption）。
    """
    node = _node(world, kind, ref_id)
    until = until or world.as_of
    affected = []
    for p in sorted(world.dependents(kind, ref_id), key=lambda x: x.id):
        gates = gate_report(world, p)
        handled_now = _switched(world, p.id, kind, ref_id)
        gaps = []
        # 直接命中的依赖缺口（不论项目处于哪个阶段）
        node_blockers = [b.__dict__ for b in _dependency_status_blockers(world, p)
                         if b.ref == ref_id]
        if node_blockers and not handled_now:
            gaps.append({"type": "dependency-unavailable",
                         "kind": kind, "ref": ref_id,
                         "blockers": node_blockers})
        for m in p.milestones.values():
            if world.as_of < m.due <= until and m.status == "pending":
                gaps.append({"type": "milestone-at-risk", "ref": m.id,
                             "name": m.name, "due": m.due})
            elif m.due <= world.as_of and m.status != "passed":
                gaps.append({"type": "milestone-broken", "ref": m.id,
                             "name": m.name, "due": m.due})
        for occ in p.occupancies.values():
            if occ.year >= int(world.as_of[:4]):
                gaps.append({"type": "capacity-commitment", "line_id": occ.line_id,
                             "year": occ.year, "unit": occ.unit, "qty": occ.qty})
        # 尚未到终态的项目，中断会卡住后续闸门晋级；已投产项目不再报越级
        if gates["next_stage"] and not gates["can_advance"] \
                and gates["current_stage"] != "operational":
            gaps.append({"type": "stage-blocked",
                         "from": gates["current_stage"],
                         "to": gates["next_stage"],
                         "blockers": gates["blockers"]})
        handled = [
            {"action": d.action, "alternative": d.alternative,
             "decided_at": d.decided_at, "actor": d.actor, "note": d.note}
            for d in world.dispositions
            if d.project_id == p.id and d.node_kind == kind and d.node_id == ref_id]
        affected.append({
            "project_id": p.id, "name": p.name, "park": p.park,
            "responsible": p.responsible,
            "target_revenue_at_risk": p.target_revenue,
            "gaps": gaps,
            "handled_dispositions": handled,
            "pending_owner_confirmation": not handled,
        })

    alternatives = _alternatives(world, kind, ref_id)
    return {
        "as_of": world.as_of,
        "node": {"kind": kind, "id": ref_id, "name": node.get("name"),
                 "status": node.get("status")},
        "affected_project_count": len(affected),
        "affected": affected,
        "alternatives": alternatives,
        "note": "系统给出受影响范围与可选资源，处置方案需各项目负责人确认。",
    }


def _node(world: World, kind: str, ref_id: str) -> dict:
    if kind == "supplier":
        s = world.suppliers.get(ref_id)
        return {"name": s.name if s else None,
                "status": s.status if s else "unknown"}
    if kind == "license":
        lic = world.licenses.get(ref_id)
        return {"name": lic.name if lic else None,
                "status": lic.status if lic else "unknown"}
    if kind == "equipment":
        eq = world.equipment.get(ref_id)
        return {"name": eq.name if eq else None,
                "status": eq.status if eq else "unknown"}
    if kind == "line":
        ln = world.lines.get(ref_id)
        return {"name": ln.name if ln else None,
                "status": "exists" if ln else "unknown"}
    return {"name": None, "status": "unknown"}


def _alternatives(world: World, kind: str, ref_id: str) -> List[dict]:
    out: List[dict] = []
    if kind == "supplier":
        origin = world.suppliers.get(ref_id)
        wanted = set(origin.capabilities) if origin else set()
        for s in world.suppliers.values():
            if s.id == ref_id or s.status != "active":
                continue
            if wanted and not (wanted & set(s.capabilities)):
                continue
            out.append({"kind": "supplier", "ref_id": s.id, "name": s.name,
                        "capabilities": s.capabilities})
    elif kind == "equipment":
        origin = world.equipment.get(ref_id)
        wanted = set(origin.capabilities) if origin else set()
        for eq in world.equipment.values():
            if eq.id == ref_id or eq.status != "installed":
                continue
            if wanted and not (wanted & set(eq.capabilities)):
                continue
            fac = world.facilities.get(eq.facility_id)
            out.append({"kind": "equipment", "ref_id": eq.id, "name": eq.name,
                        "facility_id": eq.facility_id,
                        "park": fac.park if fac else None,
                        "capabilities": eq.capabilities})
    elif kind == "line":
        origin = world.lines.get(ref_id)
        wanted = set(origin.capabilities) if origin else set()
        current_year = int(world.as_of[:4])
        for ln in world.lines.values():
            if ln.id == ref_id:
                continue
            if wanted and not (wanted & set(ln.capabilities)):
                continue
            free = world.free_capacity(ln.id, current_year)
            if free["free"] > 0:
                fac = world.facilities.get(ln.facility_id)
                out.append({"kind": "line", "ref_id": ln.id, "name": ln.name,
                            "park": fac.park if fac else None,
                            "free_qty": free["free"], "unit": free["unit"]})
    elif kind == "license":
        # 许可不可替代：只能催办/等待，系统不列“可选许可”
        out.append({"kind": "action", "ref_id": "expedite-review",
                    "name": "向监管机构催办审批，里程碑承诺缺口需负责人给出新版本日期"})
    return out
