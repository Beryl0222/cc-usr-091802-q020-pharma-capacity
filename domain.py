"""医药园区能力兑现——领域核心。

只包含与存储和传输无关的纯领域原语：

* 项目阶段与进入下一阶段所需的前置条件（门禁）；
* 证据类别（投资协议 / 验收材料 / 实际产能 / 监管资质）；
* 时间区间与"时间 × 能力单位"的占用量计算；
* 双时态版本选择（业务生效日 ``effective_on`` 与系统记录日 ``recorded_on``）；
* 年化产能折算。

业务语义见 README「语义约定」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# 阶段与门禁
# ---------------------------------------------------------------------------

#: 项目阶段的固定顺序，索引即阶段级别，禁止越级。
STAGES: tuple[str, ...] = (
    "planning",      # 规划 / 签约
    "licensing",     # 许可审评
    "equipment",     # 设备到位
    "acceptance",    # 验收 / 验证
    "production",    # 投产
)

#: 每个阶段「完成」所需的前置条件；进入下一阶段时逐项核对。
#: license 未批、equipment 未到位、acceptance 未完成，均不得越级。
STAGE_EXIT_GATES: dict[str, tuple[str, ...]] = {
    "planning": ("investment_agreement",),
    "licensing": ("regulatory_license",),
    "equipment": ("equipment_in_place",),
    "acceptance": ("acceptance_report", "actual_capacity"),
    "production": (),
}

#: 中文展示名（API 与报表使用）。
STAGE_LABELS: dict[str, str] = {
    "planning": "规划签约",
    "licensing": "许可审评",
    "equipment": "设备到位",
    "acceptance": "验收验证",
    "production": "投产",
}

#: 证据类别 -> 所属来源（三类来源分别保留，互不替代）。
EVIDENCE_SOURCES: dict[str, str] = {
    "investment_agreement": "agreement",   # 投资协议：只证明承诺，不证明兑现
    "acceptance_report": "acceptance",     # 验收材料
    "actual_capacity": "capacity",         # 实际产能计量
    "regulatory_license": "regulatory",    # 监管资质
    "equipment_in_place": "acceptance",    # 设备到位以进场验收/校准记录为准
}

#: 证据来源中文展示名。
SOURCE_LABELS: dict[str, str] = {
    "agreement": "投资协议",
    "acceptance": "验收材料",
    "capacity": "实际产能",
    "regulatory": "监管资质",
}

#: 里程碑承诺（融资消息等）只是事件，不属于兑现证据。
NEWS_EVENTS = {"funding_news", "press_release"}


class GateError(ValueError):
    """门禁未满足：项目不能越级进入下一阶段。"""

    def __init__(self, project_id: str, from_stage: str, missing: list[str]):
        self.project_id = project_id
        self.from_stage = from_stage
        self.missing = missing
        super().__init__(
            f"项目 {project_id} 不能从 {STAGE_LABELS[from_stage]} 越级："
            f"缺少前置条件 {'、'.join(missing)}"
        )


def stage_index(stage: str) -> int:
    try:
        return STAGES.index(stage)
    except ValueError as exc:
        raise ValueError(f"未知阶段：{stage}") from exc


def can_advance(stage: str, satisfied_gates: Iterable[str]) -> tuple[bool, list[str]]:
    """返回当前阶段是否可以进入下一阶段，以及未满足的门禁清单。"""
    required = set(STAGE_EXIT_GATES.get(stage, ()))
    missing = sorted(required - set(satisfied_gates))
    return not missing, missing


# ---------------------------------------------------------------------------
# 证据核对
# ---------------------------------------------------------------------------

def satisfied_gates(evidence: Iterable[dict[str, Any]]) -> set[str]:
    """根据证据条目推导已满足的门禁。

    一条证据只有在 ``status`` 为 ``approved`` / ``completed`` / ``verified``
    且带有 ``recorded_on``（已入系统留痕）时才算兑现。
    投资协议在 planning 阶段只凭签署即可成立（承诺来源），但它永远不能
    替代验收或产能证据。
    """
    done: set[str] = set()
    closed_status = {"approved", "completed", "verified"}
    for ev in evidence:
        kind = ev.get("kind")
        if kind not in EVIDENCE_SOURCES:
            continue  # 新闻、宣传材料等不构成任何门禁
        if ev.get("status") in closed_status and ev.get("recorded_on"):
            done.add(kind)
    return done


def evidence_by_source(evidence: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按来源分别保留证据（协议 / 验收 / 产能 / 监管）。"""
    buckets: dict[str, list[dict[str, Any]]] = {
        "agreement": [], "acceptance": [], "capacity": [], "regulatory": [],
    }
    for ev in evidence:
        source = EVIDENCE_SOURCES.get(ev.get("kind", ""))
        if source:
            buckets[source].append(ev)
    return buckets


# ---------------------------------------------------------------------------
# 时间区间与"时间 × 能力单位"占用
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Interval:
    """左闭右开的半开区间 ``[start, end)``。"""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"区间结束早于开始：{self.start} > {self.end}")

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "Interval") -> Optional["Interval"]:
        lo, hi = max(self.start, other.start), min(self.end, other.end)
        return Interval(lo, hi) if lo < hi else None


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def interval_from(booking: dict[str, Any]) -> Interval:
    return Interval(parse_date(booking["start"]), parse_date(booking["end"]))


def capacity_units(interval: Interval, units_per_day: float) -> float:
    """时间区间按能力单位折算的占用量（天 × 每日能力单位）。"""
    return interval.days * units_per_day


def overlap_units(a: Interval, b: Interval, units_per_day: float) -> float:
    overlap = a.intersection(b)
    return capacity_units(overlap, units_per_day) if overlap else 0.0


# ---------------------------------------------------------------------------
# 双时态版本
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AsOf:
    """双时态观察点。

    ``effective``：业务生效日——并购、迁移、延期、口径修订均从生效时起形成新版本；
    ``recorded``：系统记录日——已发布年度报告按"当时系统已知"复现，
                  事后补录的证据不会改写历史。
    """

    effective: date
    recorded: date

    @classmethod
    def parse(cls, effective: str | date, recorded: str | date) -> "AsOf":
        return cls(parse_date(effective), parse_date(recorded))


def select_version(versions: Iterable[dict[str, Any]], as_of: AsOf) -> Optional[dict[str, Any]]:
    """选出观察点下有效的最新版本。

    入选条件：``effective_on <= effective`` 且 ``recorded_on <= recorded``。
    同一观察点命中多个版本时取记录日最新、再取版本号最大者。
    """
    candidates = [
        v
        for v in versions
        if parse_date(v["effective_on"]) <= as_of.effective
        and parse_date(v["recorded_on"]) <= as_of.recorded
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda v: (parse_date(v["recorded_on"]), v.get("version", 1)),
    )


def latest_version(versions: Iterable[dict[str, Any]], *, on: date) -> Optional[dict[str, Any]]:
    """按业务生效日选最新有效版本（不限制记录日），用于当前视图。"""
    candidates = [v for v in versions if parse_date(v["effective_on"]) <= on]
    if not candidates:
        return None
    return max(candidates, key=lambda v: (parse_date(v["effective_on"]), v.get("version", 1)))


# ---------------------------------------------------------------------------
# 产能折算
# ---------------------------------------------------------------------------

def annualized_capacity(
    amount: float,
    *,
    interval: Optional[Interval] = None,
    days: Optional[int] = None,
    basis_days: int = 365,
) -> float:
    """把区间内实际产能折算为年化产能。

    半年实际产出 50 万单位 → 年化约 100 万；规划口径标注的是规划值，
    折算只作用于实际计量区间。
    """
    span = interval.days if interval is not None else days
    if not span:
        raise ValueError("产能折算需要非零的计量天数")
    return amount * basis_days / span


# ---------------------------------------------------------------------------
# 项目依赖快照
# ---------------------------------------------------------------------------

@dataclass
class ProjectState:
    """一个项目在某观察点上的状态（注册表装配后使用）。"""

    project_id: str
    version: dict[str, Any]
    stage: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    bookings: list[dict[str, Any]] = field(default_factory=list)
    open_issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def satisfied(self) -> set[str]:
        return satisfied_gates(self.evidence)

    def missing_exit_gates(self) -> list[str]:
        _, missing = can_advance(self.stage, self.satisfied)
        return missing
