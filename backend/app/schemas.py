from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from .enums import (
    AllocationPolicy,
    ImpactDecision,
    MaintenanceKind,
    OpsCaseKind,
    OpsCaseStatus,
    ReservationMode,
    ReservationStatus,
    ServiceActionKind,
)

# 数据库用 Numeric 保证精度，API 统一以 JSON number 输出
DecimalNum = Annotated[
    Decimal,
    PlainSerializer(lambda v: float(v) if v is not None else None, return_type=float | None),
]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- 基础资源 ----------

class VehicleOut(ORMModel):
    id: int
    name: str
    connector_type: str
    max_power_kw: DecimalNum
    accessible: bool


class ConnectorOut(ORMModel):
    id: int
    code: str
    connector_type: str
    rated_power_kw: DecimalNum
    accessible: bool
    position_note: str


class SiteOut(ORMModel):
    id: int
    name: str
    location: str
    transformer_limit_kw: DecimalNum = 180
    allocation_policy: AllocationPolicy = AllocationPolicy.GUARANTEED_MIN
    connectors: list[ConnectorOut]


# ---------- 候选搜索 ----------

class SearchRequest(BaseModel):
    vehicle_id: int
    site_id: int
    start_at: datetime
    duration_minutes: int = Field(gt=0, le=600)
    min_power_kw: float = Field(gt=0)
    mode: ReservationMode = ReservationMode.CONNECTOR


class RejectReason(BaseModel):
    connector_code: str
    reasons: list[str]


class SegmentInfo(BaseModel):
    start_at: datetime
    end_at: datetime
    available_power_kw: float
    note: str = ""


class ConnectorCandidate(BaseModel):
    connector_id: int
    connector_code: str
    eligible: bool
    effective_power_kw: float          # 时间窗内最差（最小）有效功率
    locked_by_ref: str | None = None   # 被哪笔有效预约占用
    segments: list[SegmentInfo]
    reasons: list[str]                 # 不合格原因


class PoolCandidate(BaseModel):
    site_id: int
    mode: ReservationMode
    eligible: bool
    # 时间窗内可同时履约的最少合格枪数（容量瓶颈）
    confirmable_slots: int
    window_segments: list[SegmentInfo]
    connector_candidates: list[ConnectorCandidate]


# ---------- 预约 ----------

class ConfirmRequest(BaseModel):
    vehicle_id: int
    site_id: int
    start_at: datetime
    duration_minutes: int = Field(gt=0, le=600)
    min_power_kw: float = Field(gt=0)
    # 期望（希望达到）功率；不传时等于最低功率。题面 R1/R2/R3：最低 60、期望 90。
    expected_power_kw: float | None = Field(default=None, gt=0)
    # 本次确认采用的预算策略；不传则取站点当前策略。
    allocation_policy: AllocationPolicy | None = None
    mode: ReservationMode = ReservationMode.CONNECTOR
    # 固定枪口模式可指定偏好枪口；为空时由后端选择一支合格枪并锁住
    preferred_connector_id: int | None = None
    idempotency_key: str = Field(min_length=8, max_length=64)


class ReservationEventOut(ORMModel):
    id: int
    event_type: str
    detail: str
    created_at: datetime


class ServiceActionOut(ORMModel):
    id: int
    kind: ServiceActionKind
    order_status_at_action: ReservationStatus
    connector_state_snapshot: str
    target_connector_id: int | None
    amount: DecimalNum | None
    note: str
    created_at: datetime


class PowerAllocationLogOut(ORMModel):
    id: int
    trigger: str
    previous_kw: DecimalNum | None
    allocated_kw: DecimalNum
    guaranteed_min_kw: DecimalNum
    expected_kw: DecimalNum
    site_limit_kw: DecimalNum
    site_committed_kw: DecimalNum
    site_remaining_kw: DecimalNum
    basis: str
    created_at: datetime


class ReservationOut(ORMModel):
    id: int
    ref: str
    vehicle_id: int
    site_id: int
    mode: ReservationMode
    status: ReservationStatus
    start_at: datetime
    end_at: datetime
    min_power_kw: DecimalNum
    expected_power_kw: DecimalNum = 90
    allocation_policy: AllocationPolicy = AllocationPolicy.GUARANTEED_MIN
    allocated_power_kw: DecimalNum | None = None
    current_available_power_kw: DecimalNum | None = None
    battery_derated: bool = False
    last_reading_at: datetime | None = None
    connector_id: int | None
    confirmation_snapshot: str
    created_at: datetime
    events: list[ReservationEventOut] = []
    service_actions: list[ServiceActionOut] = []
    allocation_logs: list[PowerAllocationLogOut] = []


class TransitionRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=64)


# ---------- 维护 ----------

class MaintenanceCreate(BaseModel):
    connector_id: int
    kind: MaintenanceKind
    start_at: datetime
    planned_end_at: datetime
    effective_power_kw: float | None = None
    reason: str = ""
    idempotency_key: str = Field(min_length=8, max_length=64)


class MaintenanceRecover(BaseModel):
    ended_at: datetime
    idempotency_key: str = Field(min_length=8, max_length=64)


class MaintenanceOut(ORMModel):
    id: int
    connector_id: int
    kind: MaintenanceKind
    start_at: datetime
    planned_end_at: datetime
    ended_at: datetime | None
    effective_power_kw: DecimalNum | None
    reason: str


class ImpactItem(BaseModel):
    reservation_id: int
    ref: str
    status: ReservationStatus
    mode: ReservationMode
    connector_id: int | None
    decision: ImpactDecision
    reason: str
    overlap_start: datetime | None
    overlap_end: datetime | None


class MaintenanceImpactOut(BaseModel):
    maintenance: MaintenanceOut
    impacts: list[ImpactItem]


# ---------- 客服动作 ----------

class ServiceActionCreate(BaseModel):
    reservation_id: int
    kind: ServiceActionKind
    target_connector_id: int | None = None
    amount: float | None = None
    note: str = ""
    idempotency_key: str = Field(min_length=8, max_length=64)


# ---------- 站点功率预算 / 实时分配 ----------

class PowerReadingCreate(BaseModel):
    # 车辆当前可接受功率（BMS 实时上限），开始充电后上报
    current_available_kw: float = Field(gt=0)
    actual_power_kw: float | None = Field(default=None, ge=0)
    battery_derated: bool = False
    note: str = ""
    # 演示回放可显式指定读数业务时刻；不传取服务端当前时间
    reading_at: datetime | None = None
    idempotency_key: str = Field(min_length=8, max_length=64)


class ReallocateRequest(BaseModel):
    at: datetime
    idempotency_key: str = Field(min_length=8, max_length=64)


class SitePolicyUpdate(BaseModel):
    allocation_policy: AllocationPolicy
    transformer_limit_kw: float | None = Field(default=None, gt=0)


class PowerLimitCreate(BaseModel):
    site_id: int
    start_at: datetime
    end_at: datetime
    transformer_limit_kw: float = Field(gt=0)
    emergency: bool = False
    reason: str = ""
    # 修订后立即重分配所使用的时刻；不传用 start_at
    reallocate_at: datetime | None = None
    idempotency_key: str = Field(min_length=8, max_length=64)


class PowerLimitOut(ORMModel):
    id: int
    site_id: int
    start_at: datetime
    end_at: datetime
    transformer_limit_kw: DecimalNum
    emergency: bool
    reason: str
    revision_ref: str
    created_at: datetime


class OpsCaseOut(ORMModel):
    id: int
    site_id: int
    reservation_id: int | None
    kind: OpsCaseKind
    status: OpsCaseStatus
    title: str
    detail: str
    guaranteed_min_kw: DecimalNum | None
    allocated_kw: DecimalNum | None
    shortfall_kw: DecimalNum | None
    resolution_note: str
    handled_by: str
    created_at: datetime
    resolved_at: datetime | None


class OpsCaseResolve(BaseModel):
    resolution_note: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=8, max_length=64)
