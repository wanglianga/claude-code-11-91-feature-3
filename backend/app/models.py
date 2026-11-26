from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .enums import (
    AllocationPolicy,
    MaintenanceKind,
    OpsCaseKind,
    OpsCaseStatus,
    PowerReportKind,
    ReservationMode,
    ReservationStatus,
    ServiceActionKind,
)


class Base(DeclarativeBase):
    pass


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    connector_type: Mapped[str] = mapped_column(String(16))
    max_power_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    accessible: Mapped[bool] = mapped_column(Boolean, default=False)

    reservations: Mapped[list["Reservation"]] = relationship(back_populates="vehicle")


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    location: Mapped[str] = mapped_column(String(128))
    # 站点配电变压器总有功上限（kW）：站点级功率预算的硬顶，
    # 不能把各枪口额定功率相加当成站点余量。
    transformer_limit_kw: Mapped[float] = mapped_column(
        Numeric(8, 2), default=180, server_default="180"
    )
    # 该站点采用的预算分配方式（可由运营端切换/回放）
    allocation_policy: Mapped[AllocationPolicy] = mapped_column(
        Enum(AllocationPolicy),
        default=AllocationPolicy.GUARANTEED_MIN,
        server_default=AllocationPolicy.GUARANTEED_MIN.value,
    )

    connectors: Mapped[list["Connector"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


class Connector(Base):
    __tablename__ = "connectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    code: Mapped[str] = mapped_column(String(16))
    connector_type: Mapped[str] = mapped_column(String(16))
    # 枪口额定/当前可用功率上限（kW）。维护降额通过维护窗口的 effective_power_kw 叠加
    rated_power_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    accessible: Mapped[bool] = mapped_column(Boolean, default=False)
    position_note: Mapped[str] = mapped_column(String(255), default="")

    site: Mapped[Site] = relationship(back_populates="connectors")
    maintenance_windows: Mapped[list["MaintenanceWindow"]] = relationship(
        back_populates="connector", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("site_id", "code", name="uq_connector_site_code"),)


class MaintenanceWindow(Base):
    """运营端记录的降额/停用/恢复生效时段。

    恢复 = 给此前的降额/停用窗口设置实际结束时刻（ended_at），
    窗口本身保留，作为历史审计与“按真实交集判断影响”的依据。
    """

    __tablename__ = "maintenance_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("connectors.id"))
    kind: Mapped[MaintenanceKind] = mapped_column(Enum(MaintenanceKind))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    planned_end_at: Mapped[datetime] = mapped_column(DateTime)
    # 提前恢复时写入实际结束时刻；为空表示仍在生效
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 降额后的功率；停用窗口为空
    effective_power_kw: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    reason: Mapped[str] = mapped_column(String(255), default="")

    connector: Mapped[Connector] = relationship(back_populates="maintenance_windows")


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(32), unique=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    mode: Mapped[ReservationMode] = mapped_column(Enum(ReservationMode))
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus), default=ReservationStatus.CONFIRMED
    )
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    min_power_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    # 期望（希望达到）功率：RESERVE_EXPECTED 策略按它整段预留，如 R1/R2/R3 希望 90kW
    expected_power_kw: Mapped[float] = mapped_column(
        Numeric(6, 2), default=90, server_default="90"
    )
    # 确认时实际采用的分配策略快照（站点策略之后切换不影响已确认订单的解释口径）
    allocation_policy: Mapped[AllocationPolicy] = mapped_column(
        Enum(AllocationPolicy),
        default=AllocationPolicy.GUARANTEED_MIN,
        server_default=AllocationPolicy.GUARANTEED_MIN.value,
    )
    # 调度器当前分配给该会话的功率（kW）；未开始调度前为空
    allocated_power_kw: Mapped[float | None] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    # 车辆最近一次回报的“当前可接受/可用功率”（BMS 上限）
    current_available_power_kw: Mapped[float | None] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    # 车辆是否处于电池限功率（BMS 降额）状态
    battery_derated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # 最近一次功率读数/重分配时刻（演示回放可显式指定读数时间）
    last_reading_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 固定枪口模式：确认时锁定；资源池模式：确认时为空，到站分配
    connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("connectors.id"), nullable=True
    )
    # 确认时的承诺快照（插头/功率/无障碍/锁定枪口或池名额），后续任何变化不得改写
    confirmation_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )

    vehicle: Mapped[Vehicle] = relationship(back_populates="reservations", foreign_keys=[vehicle_id])
    connector: Mapped[Connector | None] = relationship(foreign_keys=[connector_id])
    events: Mapped[list["ReservationEvent"]] = relationship(
        back_populates="reservation",
        cascade="all, delete-orphan",
        order_by="ReservationEvent.id",
    )
    service_actions: Mapped[list["ServiceAction"]] = relationship(
        back_populates="reservation", cascade="all, delete-orphan"
    )
    power_readings: Mapped[list["PowerReading"]] = relationship(
        back_populates="reservation",
        cascade="all, delete-orphan",
        order_by="PowerReading.id",
    )
    allocation_logs: Mapped[list["PowerAllocationLog"]] = relationship(
        back_populates="reservation",
        cascade="all, delete-orphan",
        order_by="PowerAllocationLog.id",
    )
    ops_cases: Mapped[list["OpsCase"]] = relationship(
        back_populates="reservation", cascade="all, delete-orphan"
    )


class ReservationEvent(Base):
    """订单生命周期事件流，所有转换与维护影响均留痕。"""

    __tablename__ = "reservation_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id"))
    event_type: Mapped[str] = mapped_column(String(48))
    detail: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )

    reservation: Mapped[Reservation] = relationship(back_populates="events")


class ServiceAction(Base):
    """客服动作：替代枪口/费用调整必须指向当时的订单与设备状态。"""

    __tablename__ = "service_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id"))
    kind: Mapped[ServiceActionKind] = mapped_column(Enum(ServiceActionKind))
    # 动作发生时的订单状态与设备状态快照（只读留痕）
    order_status_at_action: Mapped[ReservationStatus] = mapped_column(Enum(ReservationStatus))
    connector_state_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    target_connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("connectors.id"), nullable=True
    )
    amount: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )

    reservation: Mapped[Reservation] = relationship(back_populates="service_actions")
    target_connector: Mapped[Connector | None] = relationship(foreign_keys=[target_connector_id])


class IdempotencyRecord(Base):
    """幂等键 -> 首次请求的响应内容；重复提交原样返回。"""

    __tablename__ = "idempotency_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    request_path: Mapped[str] = mapped_column(String(128))
    response_code: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )


class SitePowerLimit(Base):
    """站点级分时变压器上限（功率预算时段）。

    每个时段记录变压器上限；该时段“已确认最低保证合计 / 期望预留合计 /
    当前已分配合计 / 可接受上浮空间”由预算引擎在此记录基础上实时计算，
    并通过 /api/sites/{id}/power-board 与预算快照接口给出。
    限额修订不覆盖旧行：旧时段行保留用于审计与回放，新时段写新行。
    """

    __tablename__ = "site_power_limits"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    transformer_limit_kw: Mapped[float] = mapped_column(Numeric(8, 2))
    # 是否为外部电网紧急限额（触发最低保证无法满足时的运营处置）
    emergency: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    reason: Mapped[str] = mapped_column(String(255), default="")
    # 修订来源备注 / 修订批次号（同一次修订产生的多个时段行共用）
    revision_ref: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )


class PowerReading(Base):
    """充电开始后车辆/桩端上报的实时功率回报（约每 5 分钟一条）。"""

    __tablename__ = "power_readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id"))
    kind: Mapped[PowerReportKind] = mapped_column(Enum(PowerReportKind))
    # 车辆当前可接受功率 / BMS 实时上限（kW）
    current_available_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    # 桩端实际输出功率（kW），可空
    actual_power_kw: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    battery_derated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    note: Mapped[str] = mapped_column(String(255), default="")
    # 读数对应的业务时刻；演示回放可显式指定，缺省取服务端当前时间
    reading_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )

    reservation: Mapped[Reservation] = relationship(back_populates="power_readings")


class PowerAllocationLog(Base):
    """调度器每次分配/重分配后写给司机与运营看的变化与调度依据。"""

    __tablename__ = "power_allocation_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id"))
    # 触发本次分配的来源：CONFIRM / SESSION_START / READING / REALLOCATE /
    # LIMIT_REVISION / RELEASE
    trigger: Mapped[str] = mapped_column(String(32))
    previous_kw: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    allocated_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    guaranteed_min_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    expected_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    # 当时站点变压器上限与预算占用，便于司机理解“为什么是这个功率”
    site_limit_kw: Mapped[float] = mapped_column(Numeric(8, 2))
    site_committed_kw: Mapped[float] = mapped_column(Numeric(8, 2))
    site_remaining_kw: Mapped[float] = mapped_column(Numeric(8, 2))
    basis: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )

    reservation: Mapped[Reservation] = relationship(back_populates="allocation_logs")


class OpsCase(Base):
    """运营处置工单：电网紧急限额/预算修订导致具体车辆最低保证受威胁时建单。

    系统绝不把“最低保证已无法满足”的车辆显示为正常：必须存在 OPEN/HANDLING
    的处置工单，运营端据此逐车处置（限流、改派、补偿等），处置完成才关闭。
    """

    __tablename__ = "ops_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("reservations.id"), nullable=True
    )
    kind: Mapped[OpsCaseKind] = mapped_column(Enum(OpsCaseKind))
    status: Mapped[OpsCaseStatus] = mapped_column(
        Enum(OpsCaseStatus),
        default=OpsCaseStatus.OPEN,
        server_default=OpsCaseStatus.OPEN.value,
    )
    title: Mapped[str] = mapped_column(String(128))
    # 受影响的具体功率缺口等结构化细节（JSON 字符串）
    detail: Mapped[str] = mapped_column(Text, default="{}")
    # 本次事件中该车被保障的最低值与实际可分配值/缺口
    guaranteed_min_kw: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    allocated_kw: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    shortfall_kw: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    resolution_note: Mapped[str] = mapped_column(Text, default="")
    handled_by: Mapped[str] = mapped_column(String(64), default="运营值班")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    reservation: Mapped[Reservation | None] = relationship(back_populates="ops_cases")
