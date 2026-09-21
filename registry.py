"""项目依赖服务的注册表与业务逻辑层。

承载全部业务规则，与 HTTP 无关，可直接被测试或命令行调用：

* :meth:`Registry.advance` —— 阶段门禁，许可/设备/验收不满足即拒绝越级；
* :meth:`Registry.conflicts` —— 共享产线按「时间 × 能力单位」检测超额占用；
* :meth:`Registry.totals` —— 园区 / 区域合计，跨园区联合项目去重一次，
  规划值、预订产能、兑现产能分列，逐项可下钻；
* :meth:`Registry.new_version` —— 并购、迁移、延期、口径修订自生效日形成新版本；
* :meth:`Registry.reproduce_report` —— 已发布年报按当时证据快照复现；
* :meth:`Registry.analyze_impact` —— 供应商中断/审批延迟的影响传播与可选资源；
* :meth:`Registry.confirm_disposition` —— 处置必须由负责人确认；
* 商业材料按授权脱敏。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from domain import (
    AsOf,
    GateError,
    Interval,
    ProjectState,
    STAGES,
    annualized_capacity,
    can_advance,
    capacity_units,
    interval_from,
    latest_version,
    parse_date,
    satisfied_gates,
    select_version,
    stage_index,
)

#: 允许形成新版本的事件类型。
VERSION_EVENTS = {"created", "merger", "relocation", "extension", "revision", "stage_advance"}

#: 证据处于开放状态（尚未获批/核验完成）时进入待核事项。
OPEN_EVIDENCE_STATUS = {"submitted", "under_review", "pending", "shipped", "installed"}

COMMERCIAL_ROLE = "commercial"
ADMIN_ROLE = "admin"


class Registry:
    """从一份 JSON 数据装配的项目依赖注册表。"""

    def __init__(self, data: dict[str, Any], source_path: str | Path | None = None):
        self.data = data
        self.source_path = str(source_path) if source_path else None
        ctx = data.get("context", {})
        #: 注册表的「当前」业务日，取自样例上下文，保证历史复现可测试。
        self.today = parse_date(ctx["as_of"]) if ctx.get("as_of") else date.today()
        self.target_year = int(ctx.get("target_year", 2030))
        self._decisions: list[dict[str, Any]] = list(data.get("decisions", []))
        self._dispositions: list[dict[str, Any]] = list(data.get("dispositions", []))
        self._validate()

    # ------------------------------------------------------------------
    # 装配
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "Registry":
        text = Path(path).read_text(encoding="utf-8")
        return cls(json.loads(text), source_path=path)

    def _validate(self) -> None:
        for proj in self.data.get("projects", []):
            if not proj.get("versions"):
                raise ValueError(f"项目 {proj.get('id')} 缺少版本记录")
            events = {v.get("event") for v in proj["versions"]}
            bad = events - VERSION_EVENTS
            if bad:
                raise ValueError(f"项目 {proj['id']} 存在未知版本事件：{sorted(bad)}")

    # ---- 索引 ---------------------------------------------------------

    @property
    def projects(self) -> list[dict[str, Any]]:
        return self.data.get("projects", [])

    def project(self, project_id: str) -> dict[str, Any]:
        for proj in self.projects:
            if proj["id"] == project_id:
                return proj
        raise KeyError(f"未知项目：{project_id}")

    def _index(self, key: str) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self.data.get(key, [])}

    def parks(self) -> dict[str, dict[str, Any]]:
        return self._index("parks")

    def facilities(self) -> dict[str, dict[str, Any]]:
        return self._index("facilities")

    def equipment(self) -> dict[str, dict[str, Any]]:
        return self._index("equipment")

    def platforms(self) -> dict[str, dict[str, Any]]:
        return self._index("platforms")

    def suppliers(self) -> dict[str, dict[str, Any]]:
        return self._index("suppliers")

    def enterprise(self, enterprise_id: str) -> Optional[dict[str, Any]]:
        return self._index("enterprises").get(enterprise_id)

    def park_scope(self, scope: str, scope_id: str) -> list[str]:
        parks = self.parks()
        if scope == "park":
            if scope_id not in parks:
                raise KeyError(f"未知园区：{scope_id}")
            return [scope_id]
        if scope == "region":
            if scope_id == "ALL":
                return list(parks)
            return [pid for pid, p in parks.items() if p.get("region") == scope_id]
        raise ValueError(f"未知合计范围：{scope}")

    # ------------------------------------------------------------------
    # 双时态项目状态
    # ------------------------------------------------------------------

    def current_as_of(self) -> AsOf:
        return AsOf(self.today, self.today)

    def state(self, project_id: str, as_of: AsOf | None = None) -> ProjectState:
        """返回项目在观察点的状态；事后补录的证据/版本不进入历史视图。"""
        proj = self.project(project_id)
        as_of = as_of or self.current_as_of()
        version = select_version(proj["versions"], as_of)
        if version is None:
            raise LookupError(f"项目 {project_id} 在 {as_of.effective} 尚无生效版本")
        evidence = [
            ev
            for ev in proj.get("evidence", [])
            if parse_date(ev["recorded_on"]) <= as_of.recorded
            and parse_date(ev.get("applies_from", ev["recorded_on"])) <= as_of.effective
        ]
        issues = [
            issue
            for issue in proj.get("issues", [])
            if parse_date(issue["raised_on"]) <= as_of.recorded
        ]
        return ProjectState(
            project_id=project_id,
            version=version,
            stage=version["stage"],
            evidence=evidence,
            bookings=list(version.get("bookings", [])),
            open_issues=[i for i in issues if not i.get("resolved_on")
                         or parse_date(i["resolved_on"]) > as_of.recorded],
        )

    # ------------------------------------------------------------------
    # 阶段门禁
    # ------------------------------------------------------------------

    def advance(self, project_id: str, decided_on: str | date | None = None) -> dict[str, Any]:
        """尝试把项目推进到下一阶段。

        门禁不满足时抛出 :class:`GateError`，越级被拒绝，并留下被拦截的阶段决定；
        成功时形成 ``stage_advance`` 新版本（生效日=记录日=决定日）。
        """
        on = parse_date(decided_on) if decided_on else self.today
        state = self.state(project_id, AsOf(on, on))
        stage = state.stage
        if stage == STAGES[-1]:
            raise ValueError(f"项目 {project_id} 已处于最终阶段（投产），无可推进阶段")
        ok, missing = can_advance(stage, state.satisfied)
        decision = {
            "project_id": project_id,
            "on": on.isoformat(),
            "from_stage": stage,
            "to_stage": STAGES[stage_index(stage) + 1] if ok else stage,
            "approved": ok,
            "missing": missing,
        }
        self._decisions.append(decision)
        if not ok:
            raise GateError(project_id, stage, missing)

        proj = self.project(project_id)
        new_version = dict(state.version)
        new_version.update(
            {
                "version": max(int(v.get("version", 1)) for v in proj["versions"]) + 1,
                "event": "stage_advance",
                "effective_on": on.isoformat(),
                "recorded_on": on.isoformat(),
                "stage": decision["to_stage"],
                "note": f"阶段推进：{stage} → {decision['to_stage']}",
            }
        )
        proj["versions"].append(new_version)
        return {"decision": decision, "state": self.state(project_id, AsOf(on, on))}

    def decisions(self, project_id: str | None = None,
                  as_of: AsOf | None = None) -> list[dict[str, Any]]:
        rows = self._decisions
        if project_id is not None:
            rows = [d for d in rows if d["project_id"] == project_id]
        if as_of is not None:
            rows = [d for d in rows if parse_date(d["on"]) <= as_of.recorded]
        return list(rows)

    # ------------------------------------------------------------------
    # 新版本事件：并购 / 迁移 / 延期 / 口径修订
    # ------------------------------------------------------------------

    def new_version(
        self,
        project_id: str,
        event: str,
        effective_on: str | date,
        changes: dict[str, Any],
        *,
        recorded_on: str | date | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """从生效时起形成新版本；历史版本与已发布报告不受影响。"""
        if event not in VERSION_EVENTS - {"created", "stage_advance"}:
            raise ValueError(f"不允许外部写入的版本事件：{event}")
        eff = parse_date(effective_on)
        rec = parse_date(recorded_on) if recorded_on else self.today
        if eff < self.today and event != "revision":
            # 并购/迁移/延期可以预先登记，但生效日不得早于当前业务日；
            # 口径修订允许追溯载入历史数据。
            raise ValueError(f"{event} 的生效日 {eff} 早于当前业务日 {self.today}")
        proj = self.project(project_id)
        base = latest_version(proj["versions"], on=eff) or select_version(
            proj["versions"], self.current_as_of()
        )
        if base is None:
            raise LookupError(f"项目 {project_id} 缺少可继承的基础版本")
        new = dict(base)
        new.update(changes)
        new.update(
            {
                "version": max(int(v.get("version", 1)) for v in proj["versions"]) + 1,
                "event": event,
                "effective_on": eff.isoformat(),
                "recorded_on": rec.isoformat(),
                "note": note or new.get("note", ""),
            }
        )
        proj["versions"].append(new)
        proj["versions"].sort(key=lambda v: (parse_date(v["effective_on"]),
                                             parse_date(v["recorded_on"]),
                                             int(v.get("version", 1))))
        return new

    # ------------------------------------------------------------------
    # 共享资源占用
    # ------------------------------------------------------------------

    def _all_bookings(self, as_of: AsOf | None = None) -> list[dict[str, Any]]:
        as_of = as_of or self.current_as_of()
        rows = []
        for proj in self.projects:
            try:
                state = self.state(proj["id"], as_of)
            except LookupError:
                continue
            for booking in state.bookings:
                rows.append({**booking, "project_id": proj["id"]})
        return rows

    def conflicts(self, as_of: AsOf | None = None) -> list[dict[str, Any]]:
        """同一设施时间重叠且能力单位之和超过供给的占用冲突。"""
        as_of = as_of or self.current_as_of()
        facilities = self.facilities()
        by_facility: dict[str, list[dict[str, Any]]] = {}
        for row in self._all_bookings(as_of):
            by_facility.setdefault(row["facility_id"], []).append(row)

        found: list[dict[str, Any]] = []
        for facility_id, rows in by_facility.items():
            facility = facilities[facility_id]
            cap = float(facility["units_per_day"])
            rows = sorted(rows, key=lambda r: r["start"])
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    a, b = rows[i], rows[j]
                    ia, ib = interval_from(a), interval_from(b)
                    overlap = ia.intersection(ib)
                    if overlap is None:
                        continue
                    requested = float(a["units_per_day"]) + float(b["units_per_day"])
                    if requested > cap + 1e-9:
                        found.append(
                            {
                                "facility_id": facility_id,
                                "facility_name": facility.get("name", facility_id),
                                "interval": {"start": overlap.start.isoformat(),
                                             "end": overlap.end.isoformat()},
                                "projects": sorted({a["project_id"], b["project_id"]}),
                                "requested_units_per_day": requested,
                                "capacity_units_per_day": cap,
                            }
                        )
        return found

    def facility_occupancy(self, facility_id: str, on_date: str | date | None = None,
                           as_of: AsOf | None = None) -> dict[str, Any]:
        facility = self.facilities()[facility_id]
        day_date = parse_date(on_date) if on_date else self.today
        day = Interval(day_date, day_date + timedelta(days=1))
        rows = []
        for row in self._all_bookings(as_of):
            if row["facility_id"] != facility_id:
                continue
            iv = interval_from(row)
            if iv.start <= day.start < iv.end:
                rows.append(row)
        used = sum(float(r["units_per_day"]) for r in rows)
        cap = float(facility["units_per_day"])
        return {
            "facility_id": facility_id,
            "date": day.start.isoformat(),
            "capacity_units_per_day": cap,
            "used_units_per_day": used,
            "free_units_per_day": max(0.0, cap - used),
            "projects": sorted({r["project_id"] for r in rows}),
        }

    # ------------------------------------------------------------------
    # 规划合计（去重 + 可下钻）
    # ------------------------------------------------------------------

    def _caliber(self, as_of: AsOf, caliber_id: str | None) -> dict[str, Any]:
        calibers = self.data.get("calibers", [])
        if caliber_id:
            for c in calibers:
                if c["id"] == caliber_id:
                    return c
            raise KeyError(f"未知统计口径：{caliber_id}")
        effective = [c for c in calibers if parse_date(c["effective_on"]) <= as_of.effective]
        if not effective:
            return {"id": "implicit", "min_stage": "planning",
                    "count_unsubstantiated": True}
        return max(effective, key=lambda c: parse_date(c["effective_on"]))

    @staticmethod
    def _year_interval(year: int) -> Interval:
        return Interval(date(year, 1, 1), date(year + 1, 1, 1))

    def _booking_basis(self, version: dict[str, Any], year: int) -> float:
        """版本的产能预订依据：目标年内各预订区间可占用的年化能力单位。"""
        year_iv = self._year_interval(year)
        total = 0.0
        for booking in version.get("bookings", []):
            iv = interval_from(booking)
            overlap = iv.intersection(year_iv)
            if overlap:
                total += capacity_units(overlap, float(booking["units_per_day"]))
        return total

    def _delivered_basis(self, state: ProjectState) -> float:
        """兑现产能：取最近一次已核验的实际产能证据，按计量窗口年化（当前运行率）。

        规划目标年的合计行里，这一列回答"现在真正兑现了多少"，
        与规划申报值、预订产能依据并列；试生产记录未经核验不计入。
        """
        verified = [
            ev for ev in state.evidence
            if ev.get("kind") == "actual_capacity" and ev.get("status") == "verified"
        ]
        if not verified:
            return 0.0
        ev = max(verified, key=lambda e: parse_date(e["recorded_on"]))
        return annualized_capacity(float(ev["amount"]), days=int(ev["window_days"]))

    def _pending_items(self, state: ProjectState, basis: float, claimed: float,
                       conflicts: list[dict[str, Any]], as_of: AsOf,
                       dispositions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        pid = state.project_id
        items: list[dict[str, Any]] = []
        for gate in state.missing_exit_gates():
            items.append({"type": "missing_gate", "gate": gate, "project_id": pid})
        for ev in state.evidence:
            if ev.get("kind") == "funding_news":
                items.append({"type": "news_not_evidence", "evidence_id": ev.get("id"),
                              "project_id": pid, "detail": "融资/发布消息不计为投产证据"})
            elif ev.get("status") in OPEN_EVIDENCE_STATUS:
                items.append({"type": "evidence_pending", "evidence_id": ev.get("id"),
                              "kind": ev.get("kind"), "status": ev.get("status"),
                              "project_id": pid})
        if claimed > basis + 1e-9:
            items.append({"type": "unsubstantiated_capacity", "project_id": pid,
                          "claimed": claimed, "booking_basis": basis,
                          "gap": claimed - basis})
        for c in conflicts:
            if pid in c["projects"]:
                items.append({"type": "facility_conflict", "project_id": pid,
                              "facility_id": c["facility_id"], "interval": c["interval"]})
        for issue in state.open_issues:
            items.append({"type": "open_issue", "project_id": pid,
                          "issue_id": issue["id"], "kind": issue["kind"],
                          "since": issue["raised_on"]})
        for d in dispositions or []:
            if d.get("status") == "proposed":
                items.append({"type": "disposition_unconfirmed", "project_id": pid,
                              "issue_id": d.get("issue_id")})
        return items

    def totals(self, scope: str = "region", scope_id: str = "ALL",
               year: int | None = None, as_of: AsOf | None = None,
               caliber_id: str | None = None) -> dict[str, Any]:
        """园区/区域 2030（或指定年）合计：联合项目只出现一次。"""
        as_of = as_of or self.current_as_of()
        year = year or self.target_year
        caliber = self._caliber(as_of, caliber_id)
        park_ids = set(self.park_scope(scope, scope_id))
        conflicts = self.conflicts(as_of)
        min_stage = stage_index(caliber.get("min_stage", "planning"))

        entries: dict[str, dict[str, Any]] = {}
        for proj in self.projects:
            try:
                state = self.state(proj["id"], as_of)
            except LookupError:
                continue
            v = state.version
            if int(v.get("target_year", self.target_year)) != year:
                continue
            if not set(v.get("park_ids", [])) & park_ids:
                continue
            if stage_index(state.stage) < min_stage:
                continue
            # 跨园区联合项目：以项目 id 去重，区域合计只出现一次。
            claimed = float(v.get("planned_output", 0))
            basis = self._booking_basis(v, year)
            reserved = min(claimed, basis)
            delivered = self._delivered_basis(state)
            decisions = self.decisions(proj["id"], as_of)
            dispositions = [
                d for d in self._dispositions
                if d.get("project_id") == proj["id"]
                and parse_date(d["decided_on"]) <= as_of.recorded
            ]
            entries[proj["id"]] = {
                "project_id": proj["id"],
                "name": v.get("name", proj.get("name", proj["id"])),
                "enterprise_id": v.get("enterprise_id"),
                "stage": state.stage,
                "park_ids": v.get("park_ids", []),
                "joint": bool(v.get("joint")),
                "claimed": claimed,
                "booking_basis": basis,
                "reserved": reserved,
                "delivered": delivered,
                "counted_in_headline": True,
                "capacity_basis": [
                    {
                        "facility_id": b["facility_id"],
                        "interval": {"start": b["start"], "end": b["end"]},
                        "units_per_day": b["units_per_day"],
                        "annualized_units_in_year": (
                            lambda iv: capacity_units(
                                iv, float(b["units_per_day"])
                            ) if iv else 0.0
                        )(interval_from(b).intersection(self._year_interval(year))),
                    }
                    for b in v.get("bookings", [])
                ],
                "decisions": decisions,
                "pending_items": self._pending_items(
                    state, basis, claimed, conflicts, as_of, dispositions),
            }

        if not caliber.get("count_unsubstantiated", True):
            headline = sum(e["reserved"] for e in entries.values())
        else:
            headline = sum(e["claimed"] for e in entries.values())
        rows = sorted(entries.values(), key=lambda e: e["project_id"])
        return {
            "scope": scope,
            "scope_id": scope_id,
            "year": year,
            "as_of": {"effective": as_of.effective.isoformat(),
                      "recorded": as_of.recorded.isoformat()},
            "caliber": {"id": caliber["id"], "name": caliber.get("name", caliber["id"])},
            "project_count": len(rows),
            "joint_project_count": sum(1 for e in rows if e["joint"]),
            "headline_total": headline,
            "claimed_total": sum(e["claimed"] for e in rows),
            "reserved_total": sum(e["reserved"] for e in rows),
            "delivered_total": sum(e["delivered"] for e in rows),
            "facility_conflicts": conflicts,
            "projects": rows,
        }

    # ------------------------------------------------------------------
    # 年度报告复现
    # ------------------------------------------------------------------

    def reports(self) -> list[dict[str, Any]]:
        return list(self.data.get("reports", []))

    def reproduce_report(self, report_id: str) -> dict[str, Any]:
        report = next((r for r in self.reports() if r["id"] == report_id), None)
        if report is None:
            raise KeyError(f"未知报告：{report_id}")
        as_of = AsOf.parse(report["as_of_effective"], report["as_of_recorded"])
        total = self.totals(
            scope=report.get("scope", "region"),
            scope_id=report.get("scope_id", "ALL"),
            year=int(report.get("year", self.target_year)),
            as_of=as_of,
            caliber_id=report.get("caliber"),
        )
        return {
            "report_id": report["id"],
            "title": report.get("title", report["id"]),
            "published_on": report["published_on"],
            "reproduced": True,
            "note": "按发布时的生效日与记录日复现；事后证据不参与。",
            "totals": self.redact_total(total, None),
        }

    # ------------------------------------------------------------------
    # 授权与商业材料脱敏
    # ------------------------------------------------------------------

    def principal(self, principal_id: str | None) -> Optional[dict[str, Any]]:
        if not principal_id:
            return None
        return self.data.get("authorizations", {}).get(principal_id)

    def is_authorized_commercial(self, principal_id: str | None, project_id: str) -> bool:
        principal = self.principal(principal_id)
        if not principal:
            return False
        roles = set(principal.get("roles", []))
        if ADMIN_ROLE in roles:
            return True
        if COMMERCIAL_ROLE not in roles:
            return False
        access = principal.get("projects", [])
        return "*" in access or project_id in access

    def project_detail(self, project_id: str, as_of: AsOf | None = None,
                       principal_id: str | None = None) -> dict[str, Any]:
        state = self.state(project_id, as_of)
        v = state.version
        visible, redacted = [], 0
        for ev in state.evidence:
            if ev.get("commercial") and not self.is_authorized_commercial(principal_id, project_id):
                redacted += 1
                continue
            visible.append(ev)
        return {
            "project_id": project_id,
            "name": v.get("name", project_id),
            "as_of": {"effective": as_of.effective.isoformat(),
                      "recorded": as_of.recorded.isoformat()} if as_of else
                      {"effective": self.today.isoformat(), "recorded": self.today.isoformat()},
            "version": {k: v[k] for k in (
                "version", "event", "effective_on", "recorded_on", "stage",
                "enterprise_id", "park_ids", "joint", "target_year",
                "planned_output", "caliber",
            ) if k in v},
            "enterprise_name": (self.enterprise(v.get("enterprise_id")) or {}).get("name"),
            "milestones": v.get("milestones", []),
            "bookings": v.get("bookings", []),
            "dependencies": v.get("dependencies", {}),
            "evidence": visible,
            "redacted_commercial_evidence": redacted,
            "open_issues": state.open_issues,
            "satisfied_gates": sorted(state.satisfied),
            "missing_gates": state.missing_exit_gates(),
            "decisions": self.decisions(project_id, as_of),
        }

    def redact_total(self, total: dict[str, Any], principal_id: str | None) -> dict[str, Any]:
        """合计下钻结果不含证据正文；商业材料只在项目详情按授权开放。

        阶段决定属于公开留痕信息，连同待核事项一并保留以便下钻核对。
        """
        return total

    # ------------------------------------------------------------------
    # 影响传播：供应商中断 / 审批延迟
    # ------------------------------------------------------------------

    def _dependency_graph(self) -> dict[str, dict[str, Any]]:
        """节点映射：供应商 → 设备；设施/设备/平台/前置项目 → 项目。"""
        graph: dict[str, Any] = {"supplier_equipment": {}, "equipment_projects": {},
                                "supplier_projects": {}, "facility_projects": {},
                                "platform_projects": {}, "prereq_projects": {}}
        for eq_id, eq in self.equipment().items():
            graph["supplier_equipment"].setdefault(eq.get("supplier_id"), set()).add(eq_id)
        for proj in self.projects:
            try:
                state = self.state(proj["id"])
            except LookupError:
                continue
            v = state.version
            deps = v.get("dependencies", {})
            for eq_id in deps.get("equipment", []):
                graph["equipment_projects"].setdefault(eq_id, set()).add(proj["id"])
            for sid in deps.get("suppliers", []):
                graph["supplier_projects"].setdefault(sid, set()).add(proj["id"])
            for b in v.get("bookings", []):
                graph["facility_projects"].setdefault(b["facility_id"], set()).add(proj["id"])
            for plat_id in deps.get("platforms", []):
                graph["platform_projects"].setdefault(plat_id, set()).add(proj["id"])
            for pre in deps.get("prerequisites", []):
                graph["prereq_projects"].setdefault(pre, set()).add(proj["id"])
        return graph

    def _alternatives(self, window: Interval, capability: str | None = None,
                      category: str | None = None,
                      exclude_projects: set[str] | None = None) -> dict[str, list]:
        exclude_projects = exclude_projects or set()
        alt_facilities, alt_suppliers = [], []
        if capability:
            booked_facilities = {
                row["facility_id"]
                for row in self._all_bookings()
                if row["project_id"] in exclude_projects
                and interval_from(row).intersection(window)
            }
            for fac in self.facilities().values():
                if fac.get("capability") != capability:
                    continue
                if fac["id"] in booked_facilities:
                    continue  # 已被受影响项目占用的设施不作为备选
                used = 0.0
                for row in self._all_bookings():
                    if row["facility_id"] != fac["id"]:
                        continue
                    if interval_from(row).intersection(window):
                        used += float(row["units_per_day"])
                free = float(fac["units_per_day"]) - used
                if free > 0:
                    alt_facilities.append({
                        "facility_id": fac["id"], "name": fac.get("name", fac["id"]),
                        "park_id": fac.get("park_id"),
                        "free_units_per_day_in_window": free,
                    })
        if category:
            for sup in self.suppliers().values():
                if sup.get("category") == category and sup.get("status") != "disrupted":
                    alt_suppliers.append({"supplier_id": sup["id"], "name": sup.get("name", sup["id"])})
        return {"facilities": alt_facilities, "suppliers": alt_suppliers}

    def analyze_impact(self, *, supplier_id: str | None = None,
                       project_id: str | None = None, issue_id: str | None = None,
                       window_days: int = 90) -> dict[str, Any]:
        """沿依赖关系列出受影响项目、承诺缺口与可选资源；不做自动处置。"""
        graph = self._dependency_graph()
        onset = self.today
        trigger_kind = "supplier_disruption"
        target_projects: set[str] = set()
        category = capability = None

        if issue_id:
            owner = None
            found_issue = None
            for proj in self.projects:
                for issue in proj.get("issues", []):
                    if issue["id"] == issue_id:
                        owner, found_issue = proj["id"], issue
            if not found_issue:
                raise KeyError(f"未知待核事项：{issue_id}")
            project_id = owner
            supplier_id = found_issue.get("supplier_id") or supplier_id
            trigger_kind = found_issue["kind"]
            onset = parse_date(found_issue["raised_on"])
        if supplier_id:
            supplier = self.suppliers().get(supplier_id)
            if not supplier:
                raise KeyError(f"未知供应商：{supplier_id}")
            trigger_kind = "supplier_disruption"
            category = supplier.get("category")
            onset = parse_date(supplier.get("disrupted_on", self.today.isoformat()))
            for eq_id in graph["supplier_equipment"].get(supplier_id, set()):
                target_projects |= graph["equipment_projects"].get(eq_id, set())
            target_projects |= graph["supplier_projects"].get(supplier_id, set())
        if project_id:
            target_projects.add(project_id)

        # 审批延迟 / 中断沿前置依赖继续向下游传播。
        affected: dict[str, int] = {}
        queue = [(pid, 1) for pid in target_projects]
        while queue:
            pid, level = queue.pop(0)
            if pid in affected and affected[pid] <= level:
                continue
            affected[pid] = level
            for downstream in graph["prereq_projects"].get(pid, set()):
                queue.append((downstream, level + 1))

        window = Interval(onset, onset + timedelta(days=window_days))
        affected_rows = []
        for pid in sorted(affected):
            state = self.state(pid)
            gaps = []
            for ms in state.version.get("milestones", []):
                due = parse_date(ms["due_on"])
                if window.start <= due <= window.end:
                    blocked_by = []
                    gate = ms.get("gate")
                    if gate and gate not in state.satisfied:
                        blocked_by.append(gate)
                    blocked_by.extend(i["id"] for i in state.open_issues)
                    gaps.append({"milestone_id": ms["id"], "name": ms["name"],
                                 "due_on": ms["due_on"], "blocked_by": blocked_by})
            affected_rows.append({
                "project_id": pid,
                "name": state.version.get("name", pid),
                "stage": state.stage,
                "propagation_level": affected[pid],
                "missing_gates": state.missing_exit_gates(),
                "open_issues": [i["id"] for i in state.open_issues],
                "commitment_gaps": gaps,
            })

        if not capability:
            caps: set[str] = set()
            for pid in affected:
                affected_state = self.state(pid)
                for b in affected_state.bookings:
                    fac = self.facilities().get(b["facility_id"])
                    if fac and fac.get("capability"):
                        caps.add(fac["capability"])
            capability = next(iter(caps), None)
        alternatives = self._alternatives(window, capability=capability,
                                          category=category,
                                          exclude_projects=set(affected))
        return {
            "trigger": {"kind": trigger_kind, "supplier_id": supplier_id,
                        "project_id": project_id, "issue_id": issue_id,
                        "onset": onset.isoformat(), "window_days": window_days},
            "affected_projects": affected_rows,
            "alternatives": alternatives,
            "notice": "系统只给出影响与可选资源，处置须由负责人确认。",
        }

    # ------------------------------------------------------------------
    # 处置确认
    # ------------------------------------------------------------------

    def confirm_disposition(self, project_id: str, issue_id: str, owner: str,
                            action: str, *, alternative_id: str | None = None,
                            note: str = "", decided_on: str | date | None = None) -> dict[str, Any]:
        if not owner or not owner.strip():
            raise ValueError("处置必须记录负责人（owner），系统不得自行处置")
        self.project(project_id)  # 校验项目存在
        on = parse_date(decided_on) if decided_on else self.today
        record = {
            "project_id": project_id,
            "issue_id": issue_id,
            "owner": owner.strip(),
            "action": action,
            "alternative_id": alternative_id,
            "note": note,
            "decided_on": on.isoformat(),
            "status": "confirmed",
        }
        self._dispositions.append(record)
        return record

    def dispositions(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id is None:
            return list(self._dispositions)
        return [d for d in self._dispositions if d["project_id"] == project_id]
