import enum


class ConnectorType(str, enum.Enum):
    CCS = "CCS"
    CHADEMO = "CHAdeMO"
    GBT = "GBT"


class MaintenanceKind(str, enum.Enum):
    DERATE = "DERATE"          # 降额：枪口可用但功率下调
    OUTAGE = "OUTAGE"          # 停用：窗口内完全不可用


class ReservationMode(str, enum.Enum):
    CONNECTOR = "CONNECTOR"    # 固定枪口：确认即锁定具体枪口
    POOL = "POOL"              # 资源池：只承诺名额，到站再分配枪口


class ReservationStatus(str, enum.Enum):
    CONFIRMED = "CONFIRMED"                    # 已确认，持有容量
    ARRIVED = "ARRIVED"                        # 已到站（池模式此时分配枪口）
    CHARGING = "CHARGING"                      # 充电中，预约管理不可直接释放
    COMPLETED = "COMPLETED"                    # 已完成
    CANCELLED = "CANCELLED"                    # 已取消，释放容量
    NO_SHOW = "NO_SHOW"                        # 迟到失约，释放容量
    NEEDS_RESELECTION = "NEEDS_RESELECTION"    # 维护影响：需改选
    MANUAL_CONTACT = "MANUAL_CONTACT"          # 维护影响：需人工联系


# 仍持有服务容量、会在容量计算中占位的状态
HOLDING_STATUSES = (
    ReservationStatus.CONFIRMED,
    ReservationStatus.ARRIVED,
    ReservationStatus.CHARGING,
)


class ServiceActionKind(str, enum.Enum):
    ALTERNATIVE_CONNECTOR = "ALTERNATIVE_CONNECTOR"  # 客服指定替代枪口
    FEE_ADJUSTMENT = "FEE_ADJUSTMENT"                # 费用调整
    NOTE = "NOTE"                                    # 人工联系备注


class ImpactDecision(str, enum.Enum):
    UNCHANGED = "UNCHANGED"                      # 仍能履约，订单不动
    NEEDS_RESELECTION = "NEEDS_RESELECTION"      # 进入改选
    MANUAL_CONTACT = "MANUAL_CONTACT"            # 已到站，人工联系
    PROTECTED_CHARGING = "PROTECTED_CHARGING"    # 充电中，受保护不可释放


class AllocationPolicy(str, enum.Enum):
    """站点级功率预算的两种售卖/分配方式。

    - RESERVE_EXPECTED：按每辆车预约时的期望功率为整段预留，稳定可预期，
      但车辆电池提前降功率后空出的额度不能被后续预约/会话利用。
    - GUARANTEED_MIN：只把明确承诺的最低保证计入硬占用，剩余预算进入
      可分配池，按车辆每 5 分钟读数动态重分配，提升同站可接入数量。
    """

    RESERVE_EXPECTED = "RESERVE_EXPECTED"
    GUARANTEED_MIN = "GUARANTEED_MIN"


class PowerReportKind(str, enum.Enum):
    """车辆/充电桩上报的实时功率状态。"""

    READING = "READING"                  # 常规 5 分钟读数
    BATTERY_DERATE = "BATTERY_DERATE"    # 车辆进入电池限功率状态


class OpsCaseStatus(str, enum.Enum):
    OPEN = "OPEN"                # 待运营处置
    HANDLING = "HANDLING"        # 处置中
    RESOLVED = "RESOLVED"        # 已处置


class OpsCaseKind(str, enum.Enum):
    GRID_EMERGENCY = "GRID_EMERGENCY"        # 电网紧急限额，最低保证无法全部满足
    BUDGET_REVISION = "BUDGET_REVISION"      # 站点功率上限修订影响
    POWER_SHORTFALL = "POWER_SHORTFALL"       # 实时分配低于明确承诺的最低值
