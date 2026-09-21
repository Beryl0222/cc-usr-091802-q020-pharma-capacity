"""领域事件定义。

服务采用事件溯源：所有事实（企业并购、产线迁移、里程碑延期、统计口径修订）
都以不可变事件表达。每个事件带两个时间：

* ``occurred_at`` —— 业务生效时间（valid time）：并购/迁移/延期从何时起生效；
* ``recorded_at`` —— 系统记录时间（transaction time）：事件何时进入系统。

双时态回放让「2025 年已发布的年度报告」在 2026 年补录证据后，仍能按发布
当时已知的证据原样复现。
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional
import itertools
import time

# ---- 实体注册 -----------------------------------------------------------------

# 企业主体（并购通过 acquired_by 在生效版本中表达）
ENTITY_REGISTERED = "entity.registered"
# 企业并购：自 occurred_at 起，entity_id 成为 acquired_by 的子公司，
# 规划口径中归属集团（并购前的年报仍按当时股权结构复现）
ENTITY_ACQUIRED = "entity.acquired"
# 厂房
FACILITY_REGISTERED = "facility.registered"
# 共享产线；capacity 形如 {"unit": "vial/年", "qpy": 10000000}
LINE_REGISTERED = "line.registered"
# 产线跨园区迁移：自生效日起所在厂房变更（迁移形成新版本，旧版本保留）
LINE_RELOCATED = "line.relocated"
# 关键设备登记；at_facility 为所在厂房；installed_at 为实际到位日
EQUIPMENT_REGISTERED = "equipment.registered"
# 设备状态：installed / removed（未到位的设备不能支撑 equipment 闸门）
EQUIPMENT_STATUS_CHANGED = "equipment.status.changed"
# 技术平台（关键技术平台）
PLATFORM_REGISTERED = "platform.registered"
# 监管资质：license 状态通过 status 字段（approved/pending）
LICENSE_DECLARED = "license.declared"
# 供应商
SUPPLIER_REGISTERED = "supplier.registered"
SUPPLIER_STATUS_CHANGED = "supplier.status.changed"  # active / disrupted
# 项目（园区归属、联合园区、产品类型）
PROJECT_REGISTERED = "project.registered"
# 项目依赖：产线/设备/平台/资质/供应商/里程碑，及承诺的能力占用
DEPENDENCY_DECLARED = "dependency.declared"
# 依赖解除/修订（整组替换时用 version 覆盖）
OCCUPANCY_DECLARED = "occupancy.declared"  # 按年份×能力单位的占用承诺
# 里程碑承诺与验收决定
MILESTONE_DECLARED = "milestone.declared"
MILESTONE_RESOLVED = "milestone.resolved"  # passed/failed，绑定证据
# 阶段闸门决定（系统给出建议，负责人确认越级/放行）
GATE_DECIDED = "gate.decided"
# 证据登记：投资协议 / 验收材料 / 实际产能 / 融资新闻……
EVIDENCE_RECORDED = "evidence.recorded"
# 统计口径修订（新版本规划口径，自 effective_date 生效）
CALIBER_REVISED = "caliber.revised"
# 年度报告发布（冻结当时的记录时点）
REPORT_PUBLISHED = "report.published"
# 影响处置（供应商中断/审批延迟后的负责人确认）
DISRUPTION_HANDLED = "disruption.handled"
# 用户（角色决定可见范围：commercial_access 才能看商业材料）
USER_REGISTERED = "user.registered"

# ---- 证据类别 -----------------------------------------------------------------

EVIDENCE_KINDS = (
    "investment-agreement",  # 投资协议：承诺
    "acceptance",            # 验收材料：验收完成
    "capacity-audit",        # 实际产能（第三方核产）
    "financing-news",        # 融资消息：只能佐证资金面，不能证明投产
    "license-certificate",   # 监管批件
    "other",
)

# ---- 阶段 ---------------------------------------------------------------------

STAGES = (
    "planned",        # 规划/签约
    "equipment",      # 设备到位
    "accepted",       # 产线验收完成
    "licensed",       # 许可获批
    "operational",    # 投产（实际产能核实）
)

# 每个阶段进入时必须满足的闸门
GATE_REQUIREMENTS = {
    "planned": [],
    "equipment": ["investment-agreement"],
    "accepted": ["equipment-in-place", "acceptance"],
    "licensed": ["license-certificate"],
    "operational": ["capacity-audit"],
}

SEQ = itertools.count(1)


def seed_seq(value: int) -> None:
    """把事件自增序号播种到 value（从磁盘加载后用于避免 id 冲突）。"""
    global SEQ
    SEQ = itertools.count(value + 1)


@dataclass
class Event:
    type: str
    payload: Dict[str, Any]
    occurred_at: str                 # ISO 日期，业务生效时间
    recorded_at: str = None          # ISO 时间戳，系统记录时间（缺省取当前）
    id: int = field(default_factory=lambda: next(SEQ))
    actor: Optional[str] = None      # 操作人/负责人
    confidential: bool = False       # 商业敏感事件（授权可见）

    def __post_init__(self):
        if self.recorded_at is None:
            self.recorded_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Event":
        return Event(**d)
