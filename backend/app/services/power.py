"""站点级分时功率预算引擎。

两条主线：

1. 预约确认时的“整段预算”校验（budget_segments / assert_confirm_budget）：
   - 每个分时切片记录变压器上限、已确认最低保证合计、期望预留合计与可上浮空间；
   - RESERVE_EXPECTED：按每车期望功率整段硬预留，两辆 90kW 即占满 180kW；
   - GUARANTEED_MIN：只把明确承诺的最低值作为硬占用，三辆 60kW 恰好 180kW。

2. 充电开始后的实时分配（compute_board / reallocate / record_reading）：
   - 先保障所有重叠会话的最低保证，再把剩余预算按 max-min 水位法分配为上浮；
   - 车辆每 5 分钟回报当前可接受功率/电池降额，上限随之下降，腾出的预算可被
     其他会话或新接入会话使用；
   - 电网紧急限额使最低保证总和超过变压器上限时，按确认先后确定性削减，
     产生具体受影响车辆清单与运营处置工单，而不是显示正常。
"""

from dataclasses import dataclass, field
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..enums import (
    HOLDING_STATUSES,
    AllocationPolicy,
    OpsCaseKind,
    OpsCaseStatus,
    PowerReportKind,
    ReservationStatus,
)
from ..models import (
    Connector,
    OpsCase,
    PowerAllocationLog,
    PowerReading,
    Reservation,
    Site,
    SitePowerLimit,
    Vehicle,
)
from .common import LOCK_NS_SITE, add_event

EPS = 0.011  # kW，Numeric(?,2) 精度下的比较容差


def fnum(v) -> float:
    return float(v) if v is not None else 0.0


def r2(x: float) -> float:
    return round(float(x) + 0.0, 2)


# ---------- 站点分时限额 ----------

def list_limit_rows(db: Session, site_id: int) -> list[SitePowerLimit]:
    return list(db.scalars(
        select(SitePowerLimit)
        .where(SitePowerLimit.site_id == site_id)
        .order_by(SitePowerLimit.start_at, SitePowerLimit.id)
    ).all())


def limit_at(rows: list[SitePowerLimit], site: Site, t: datetime) -> tuple[float, SitePowerLimit | None]:
    """某时刻适用的变压器上限：与区间重叠的修订行中以最新修订（id 最大）为准。"""
    applicable = [r for r in rows if r.start_at <= t < r.end_at]
    if applicable:
        row = max(applicable, key=lambda r: r.id)
        return fnum(row.transformer_limit_kw), row
    return fnum(site.transformer_limit_kw), None


def connector_power_at(db: Session, connector: Connector | None, at: datetime) -> float | None:
    """枪口在某一时刻的有效功率上限（额定功率叠加生效中的降额/停用）。"""
    if connector is None:
        return None
    power = fnum(connector.rated_power_kw)
    for m in connector.maintenance_windows:
        m_end = m.ended_at or m.planned_end_at
        if m.start_at <= at < m_end:
            from ..enums import MaintenanceKind
            if m.kind == MaintenanceKind.OUTAGE:
                return 0.0
            if m.effective_power_kw is not None:
                power = min(power, fnum(m.effective_power_kw))
    return power


# ---------- 整段分时预算 ----------

@dataclass
class BudgetSegment:
    start_at: datetime
    end_at: datetime
    limit_kw: float
    limit_row_id: int | None
    # 与该切片重叠的持有容量预约
    reservations: list[Reservation] = field(default_factory=list)

    @property
    def guaranteed_kw(self) -> float:
        """所有已确认车辆的明确最低保证合计（硬承诺）。"""
        return r2(sum(fnum(r.min_power_kw) for r in self.reservations))

    @property
    def expected_reserved_kw(self) -> float:
        """若全部按期望预留时的合计（参考口径）。"""
        return r2(sum(fnum(r.expected_power_kw) for r in self.reservations))

    @property
    def hard_committed_kw(self) -> float:
        """实际售卖口径下的硬占用：期望预留单按期望，最低保证单按最低值。"""
        total = 0.0
        for r in self.reservations:
            if r.allocation_policy == AllocationPolicy.RESERVE_EXPECTED:
                total += fnum(r.expected_power_kw)
            else:
                total += fnum(r.min_power_kw)
        return r2(total)

    @property
    def acceptable_float_kw(self) -> float:
        """变压器上限扣除最低保证后，最多还能接受的上浮总量。"""
        return r2(max(0.0, self.limit_kw - self.guaranteed_kw))


def budget_segments(db: Session, site: Site, ws: datetime, we: datetime) -> list[BudgetSegment]:
    """构造 [ws,we) 内限额/占用状态恒定的预算切片。"""
    rows = list_limit_rows(db, site.id)
    holding = list(db.scalars(
        select(Reservation).where(
            Reservation.site_id == site.id,
            Reservation.status.in_(HOLDING_STATUSES),
            Reservation.start_at < we,
            Reservation.end_at > ws,
        )
    ).all())

    boundaries = {ws, we}
    for row in rows:
        if row.start_at < we and row.end_at > ws:
            boundaries.add(max(row.start_at, ws))
            boundaries.add(min(row.end_at, we))
    for r in holding:
        boundaries.add(max(r.start_at, ws))
        boundaries.add(min(r.end_at, we))
    bounds = sorted(set(b for b in boundaries if ws <= b <= we))

    segments: list[BudgetSegment] = []
    for t0, t1 in zip(bounds, bounds[1:]):
        if t0 == t1:
            continue
        limit_kw, row = limit_at(rows, site, t0)
        seg = BudgetSegment(
            start_at=t0, end_at=t1, limit_kw=limit_kw,
            limit_row_id=row.id if row else None,
        )
        seg.reservations = [
            r for r in holding if r.start_at < t1 and r.end_at > t0
        ]
        segments.append(seg)
    return segments


def assert_confirm_budget(
    db: Session,
    site: Site,
    start: datetime,
    end: datetime,
    min_kw: float,
    expected_kw: float,
    policy: AllocationPolicy,
) -> dict:
    """确认事务内的站点预算校验。必须已持有站点咨询锁。

    返回最紧张切片的预算解释；不满足时抛 409（含逐切片原因）。
    """
    segments = budget_segments(db, site, start, end)
    candidate_block = expected_kw if policy == AllocationPolicy.RESERVE_EXPECTED else min_kw

    slice_views: list[dict] = []
    rejection: dict | None = None
    for seg in segments:
        guaranteed = seg.guaranteed_kw
        hard = seg.hard_committed_kw
        if policy == AllocationPolicy.RESERVE_EXPECTED:
            used = hard
            after = r2(used + expected_kw)
            ok = after <= seg.limit_kw + EPS
            rule = "期望功率整段预留"
        else:
            used = guaranteed
            after = r2(guaranteed + min_kw)
            ok = after <= seg.limit_kw + EPS
            rule = "只硬承诺最低值，上浮按 5 分钟读数动态分配"

        view = {
            "start_at": seg.start_at,
            "end_at": seg.end_at,
            "transformer_limit_kw": seg.limit_kw,
            "guaranteed_committed_kw": guaranteed,
            "hard_committed_kw": r2(used),
            "candidate_block_kw": r2(candidate_block),
            "hard_committed_after_kw": after,
            "remaining_kw": r2(seg.limit_kw - used),
            "acceptable_float_kw": seg.acceptable_float_kw,
            "rule": rule,
            "ok": ok,
        }
        slice_views.append(view)
        if not ok and rejection is None:
            rejection = view

    summary = {
        "policy": policy.value,
        "candidate_min_kw": min_kw,
        "candidate_expected_kw": expected_kw,
        "candidate_block_kw": r2(candidate_block),
        "transformer_limit_kw": fnum(site.transformer_limit_kw),
        "segments": slice_views,
        "tightest": min(slice_views, key=lambda v: v["remaining_kw"], default=None),
    }
    if rejection is not None:
        if policy == AllocationPolicy.RESERVE_EXPECTED:
            message = (
                f"站点变压器预算不足：{rejection['start_at']:%H:%M}-{rejection['end_at']:%H:%M} "
                f"上限 {rejection['transformer_limit_kw']:g}kW，已按期望预留 "
                f"{rejection['hard_committed_kw']:g}kW，本单还需整段预留 "
                f"{expected_kw:g}kW（剩余 {rejection['remaining_kw']:g}kW）。"
                f"即使仍有枪口显示空闲，也不能超卖站点总功率。"
            )
        else:
            message = (
                f"站点最低保证预算不足：{rejection['start_at']:%H:%M}-{rejection['end_at']:%H:%M} "
                f"上限 {rejection['transformer_limit_kw']:g}kW，已确认车辆最低保证合计 "
                f"{rejection['guaranteed_committed_kw']:g}kW，本单还需保证 {min_kw:g}kW。"
            )
        raise HTTPException(409, {
            "message": message,
            "budget": summary,
        })
    return summary


# ---------- 实时分配（max-min 水位法 + 紧急削减） ----------

@dataclass
class Participant:
    reservation: Reservation
    floor_kw: float
    ceiling_kw: float
    connector_limit_kw: float | None
    reported_available_kw: float | None
    battery_derated: bool
    is_charging: bool
    base_kw: float = 0.0
    allocated_kw: float = 0.0
    grid_shortfall_kw: float = 0.0
    vehicle_limited: bool = False


def _latest_reading_before(
    readings: list[PowerReading], at: datetime
) -> PowerReading | None:
    eligible = [x for x in readings if x.reading_at <= at]
    return max(eligible, key=lambda x: (x.reading_at, x.id), default=None)


def build_participants(db: Session, site: Site, at: datetime) -> tuple[float, list[Participant], float | None]:
    limit_kw, _ = limit_at(list_limit_rows(db, site.id), site, at)

    reservations = list(db.scalars(
        select(Reservation).where(
            Reservation.site_id == site.id,
            Reservation.status.in_(HOLDING_STATUSES),
            Reservation.start_at <= at,
            Reservation.end_at > at,
        ).order_by(Reservation.id)
    ).all())

    connector_ids = {r.connector_id for r in reservations if r.connector_id}
    connectors: dict[int, Connector] = {}
    if connector_ids:
        for c in db.scalars(select(Connector).where(Connector.id.in_(connector_ids))).all():
            connectors[c.id] = c

    participants: list[Participant] = []
    for r in reservations:
        vehicle = db.get(Vehicle, r.vehicle_id)
        floor_kw = fnum(r.min_power_kw)
        expected_kw = fnum(r.expected_power_kw)
        gun_limit = connector_power_at(db, connectors.get(r.connector_id), at)
        vehicle_max = fnum(vehicle.max_power_kw) if vehicle else expected_kw

        readings = list(r.power_readings)
        latest = _latest_reading_before(readings, at)
        reported = fnum(latest.current_available_kw) if latest else None
        battery_derated = bool(latest.battery_derated) if latest else bool(r.battery_derated)

        ceiling = min(expected_kw, vehicle_max)
        if gun_limit is not None:
            ceiling = min(ceiling, gun_limit)
        if reported is not None:
            ceiling = min(ceiling, reported)
        ceiling = max(ceiling, 0.0)

        p = Participant(
            reservation=r,
            floor_kw=floor_kw,
            ceiling_kw=r2(ceiling),
            connector_limit_kw=gun_limit,
            reported_available_kw=reported,
            battery_derated=battery_derated,
            is_charging=(r.status == ReservationStatus.CHARGING),
        )
        # 车辆受电上限已低于承诺最低值：最多只能给到它能接受的功率
        p.base_kw = r2(min(floor_kw, ceiling))
        p.vehicle_limited = ceiling + EPS < floor_kw
        participants.append(p)

    return limit_kw, participants, None


def allocate_power(limit_kw: float, participants: list[Participant]) -> None:
    """就地计算每个参与者的 allocated_kw / grid_shortfall_kw。"""
    base_sum = r2(sum(p.base_kw for p in participants))

    if base_sum > limit_kw + EPS:
        # 电网紧急：连“可实际送达的最低基底”都放不下。
        # 按确认先后（id 升序）确定性保障，后确认车辆承担缺口。
        remaining = limit_kw
        for p in sorted(participants, key=lambda x: x.reservation.id):
            give = r2(max(0.0, min(p.base_kw, remaining)))
            p.allocated_kw = give
            remaining = r2(remaining - give)
            # 仅把电网侧削减计为缺额；车辆自身 BMS 不接受的部分不算站点缺额
            p.grid_shortfall_kw = r2(max(0.0, p.base_kw - give))
        return

    # 正常：先落基底（最低保证与车辆上限的较小值），再 max-min 分配剩余预算
    for p in participants:
        p.allocated_kw = p.base_kw
    remaining = r2(limit_kw - base_sum)

    # 只有真正在充电的会话接收上浮；未开始的预约只保留最低保证额度
    pool = [p for p in participants if p.is_charging and p.ceiling_kw > p.allocated_kw + EPS]
    while remaining > EPS and pool:
        room = {id(p): r2(p.ceiling_kw - p.allocated_kw) for p in pool}
        step = min(room.values())
        cost = r2(step * len(pool))
        if cost <= remaining + EPS:
            for p in pool:
                p.allocated_kw = r2(p.allocated_kw + step)
            remaining = r2(remaining - cost)
            pool = [p for p in pool if p.ceiling_kw > p.allocated_kw + EPS]
        else:
            share = r2(remaining / len(pool))
            for p in pool:
                p.allocated_kw = r2(min(p.ceiling_kw, p.allocated_kw + share))
            remaining = 0.0
            break

    for p in participants:
        p.grid_shortfall_kw = 0.0


def _board_row(db: Session, site: Site, at: datetime, limit_kw: float, p: Participant) -> dict:
    r = p.reservation
    open_case = db.scalar(
        select(OpsCase).where(
            OpsCase.reservation_id == r.id,
            OpsCase.status != OpsCaseStatus.RESOLVED,
        ).order_by(OpsCase.id.desc())
    )
    connector = db.get(Connector, r.connector_id) if r.connector_id else None
    return {
        "reservation_id": r.id,
        "ref": r.ref,
        "vehicle_id": r.vehicle_id,
        "status": r.status.value,
        "policy": r.allocation_policy.value,
        "connector_id": r.connector_id,
        "connector_code": connector.code if connector else None,
        "connector_limit_kw": r2(p.connector_limit_kw) if p.connector_limit_kw is not None else None,
        "guaranteed_min_kw": r2(p.floor_kw),
        "expected_kw": fnum(r.expected_power_kw),
        "reported_available_kw": p.reported_available_kw,
        "battery_derated": p.battery_derated,
        "is_charging": p.is_charging,
        "allocated_kw": r2(p.allocated_kw) if p.is_charging else None,
        "earmarked_floor_kw": r2(p.floor_kw),
        "grid_shortfall_kw": r2(p.grid_shortfall_kw),
        "vehicle_limited": p.vehicle_limited,
        "open_ops_case_id": open_case.id if open_case else None,
        "ops_case_status": open_case.status.value if open_case else None,
    }


def compute_board(db: Session, site_id: int, at: datetime) -> dict:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    limit_kw, participants, _ = build_participants(db, site, at)
    allocate_power(limit_kw, participants)

    rows = [_board_row(db, site, at, limit_kw, p) for p in participants]
    guaranteed_total = r2(sum(p.floor_kw for p in participants))
    allocated_total = r2(sum(p.allocated_kw for p in participants if p.is_charging))
    shortfall_total = r2(sum(p.grid_shortfall_kw for p in participants))
    return {
        "site_id": site_id,
        "site_name": site.name,
        "at": at,
        "allocation_policy": site.allocation_policy.value,
        "transformer_limit_kw": r2(limit_kw),
        "guaranteed_committed_kw": guaranteed_total,
        "budget_headroom_kw": r2(limit_kw - guaranteed_total),
        "currently_allocated_kw": allocated_total,
        "power_remaining_now_kw": r2(limit_kw - allocated_total),
        "grid_shortfall_total_kw": shortfall_total,
        "emergency": shortfall_total > EPS,
        "sessions": rows,
    }


# ---------- 分配持久化与变化记录 ----------

def _write_allocation_log(
    db: Session,
    r: Reservation,
    trigger: str,
    previous: float | None,
    allocated: float,
    site_limit: float,
    committed: float,
    remaining: float,
    basis: str,
) -> None:
    db.add(PowerAllocationLog(
        reservation_id=r.id,
        trigger=trigger,
        previous_kw=previous,
        allocated_kw=allocated,
        guaranteed_min_kw=fnum(r.min_power_kw),
        expected_kw=fnum(r.expected_power_kw),
        site_limit_kw=site_limit,
        site_committed_kw=committed,
        site_remaining_kw=remaining,
        basis=basis,
    ))
    db.flush()


def reallocate(
    db: Session,
    site_id: int,
    at: datetime,
    trigger: str = "REALLOCATE",
    focus_id: int | None = None,
) -> dict:
    """重算并持久化当前充电会话的分配，为变化的会话写司机可见记录。

    调用方应已持有（或本函数自行获取）站点咨询锁；这里自取一次，
    pg 咨询锁在同一事务内可重入，安全。
    """
    db.execute(text("SELECT pg_advisory_xact_lock(:ns, :sid)"),
               {"ns": LOCK_NS_SITE, "sid": site_id})
    site = db.get(Site, site_id)
    limit_kw, participants, _ = build_participants(db, site, at)
    allocate_power(limit_kw, participants)

    allocated_total = r2(sum(p.allocated_kw for p in participants if p.is_charging))
    remaining = r2(limit_kw - allocated_total)

    for p in participants:
        r = p.reservation
        if not p.is_charging:
            continue
        new_alloc = r2(p.allocated_kw)
        old_alloc = fnum(r.allocated_power_kw) if r.allocated_power_kw is not None else None
        changed = old_alloc is None or abs(old_alloc - new_alloc) > EPS
        focus = focus_id == r.id

        if changed:
            r.allocated_power_kw = new_alloc
            parts = [
                f"站点变压器上限 {limit_kw:g}kW；"
                f"先保障各车最低保证，再按实时受电上限做剩余预算动态分配"
            ]
            if p.battery_derated:
                parts.append(
                    f"车辆已报电池限功率，当前最多接受 {p.reported_available_kw:g}kW，"
                    f"上浮空间相应释放"
                )
            if p.grid_shortfall_kw > EPS:
                parts.append(
                    f"电网紧急限额下最低保证存在 {p.grid_shortfall_kw:g}kW 缺口，"
                    f"已转运营处置"
                )
            basis = "；".join(parts) + "。"
            _write_allocation_log(
                db, r, trigger, old_alloc, new_alloc,
                limit_kw, allocated_total, remaining, basis,
            )
            add_event(db, r, "POWER_ALLOCATED", {
                "trigger": trigger,
                "previous_kw": old_alloc,
                "allocated_kw": new_alloc,
                "guaranteed_min_kw": fnum(r.min_power_kw),
                "site_limit_kw": limit_kw,
                "site_remaining_kw": remaining,
                "basis": basis,
            })
        elif focus:
            # 目标车主动上报但分配未变：仍给司机一条可核对的调度说明
            if p.grid_shortfall_kw <= EPS and not p.vehicle_limited:
                basis = (
                    f"本次读数后调度结果不变：当前分配 {new_alloc:g}kW，"
                    f"不低于明确承诺的最低保证 {p.floor_kw:g}kW，"
                    f"不存在缺额；站点实时剩余 {remaining:g}kW。"
                )
            elif p.vehicle_limited:
                basis = (
                    f"车辆 BMS 当前仅接受 {p.ceiling_kw:g}kW（低于承诺最低 "
                    f"{p.floor_kw:g}kW），属车辆受电限制，不是站点功率缺额。"
                )
            else:
                basis = f"当前存在 {p.grid_shortfall_kw:g}kW 电网缺口，已转运营处置。"
            _write_allocation_log(
                db, r, trigger, old_alloc if old_alloc is not None else new_alloc,
                new_alloc, limit_kw, allocated_total, remaining, basis,
            )
            add_event(db, r, "POWER_REPORT_PROCESSED", {
                "trigger": trigger,
                "allocated_kw": new_alloc,
                "guaranteed_min_kw": p.floor_kw,
                "shortfall_kw": p.grid_shortfall_kw,
                "basis": basis,
            })

    db.flush()
    return compute_board(db, site_id, at)


# ---------- 车辆功率/电池降额回报 ----------

def record_reading(db: Session, reservation_id: int, req) -> tuple[Reservation, dict]:
    r = db.get(Reservation, reservation_id)
    if r is None:
        raise HTTPException(404, "预约不存在")
    if r.status != ReservationStatus.CHARGING:
        raise HTTPException(409, "只有开始充电后的会话才能上报实时功率与电池降额")

    db.execute(text("SELECT pg_advisory_xact_lock(:ns, :sid)"),
               {"ns": LOCK_NS_SITE, "sid": r.site_id})

    at = req.reading_at or datetime.now()
    kind = PowerReportKind.BATTERY_DERATE if req.battery_derated else PowerReportKind.READING
    reading = PowerReading(
        reservation_id=r.id,
        kind=kind,
        current_available_kw=req.current_available_kw,
        actual_power_kw=req.actual_power_kw,
        battery_derated=req.battery_derated,
        note=req.note or "",
        reading_at=at,
    )
    db.add(reading)
    r.current_available_power_kw = req.current_available_kw
    r.battery_derated = req.battery_derated
    r.last_reading_at = at
    db.flush()

    add_event(db, r, "VEHICLE_POWER_REPORT", {
        "kind": kind.value,
        "reading_at": at,
        "current_available_kw": req.current_available_kw,
        "actual_power_kw": req.actual_power_kw,
        "battery_derated": req.battery_derated,
        "note": req.note,
    })

    board = reallocate(
        db, r.site_id, at,
        trigger="BATTERY_DERATE" if req.battery_derated else "READING",
        focus_id=r.id,
    )
    return r, board


# ---------- 站点限额修订与运营处置 ----------

def _curtail_segment(db: Session, site: Site, seg: BudgetSegment) -> list[dict]:
    """对一个最低保证放不下的切片，按确认先后确定性削减，返回受影响车辆。"""
    rows: list[dict] = []
    remaining = seg.limit_kw
    ordered = sorted(seg.reservations, key=lambda x: x.id)
    for r in ordered:
        floor = fnum(r.min_power_kw)
        give = r2(max(0.0, min(floor, remaining)))
        remaining = r2(remaining - give)
        shortfall = r2(floor - give)
        if shortfall > EPS:
            rows.append({
                "reservation_id": r.id,
                "ref": r.ref,
                "status": r.status.value,
                "guaranteed_min_kw": floor,
                "allocatable_kw": give,
                "shortfall_kw": shortfall,
                "segment_start": seg.start_at,
                "segment_end": seg.end_at,
            })
    return rows


def revise_site_limit(db: Session, req) -> tuple[SitePowerLimit, list[dict], list[OpsCase], dict | None]:
    site = db.get(Site, req.site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    if req.start_at >= req.end_at:
        raise HTTPException(422, "限额时段开始必须早于结束")
    if req.transformer_limit_kw <= 0:
        raise HTTPException(422, "变压器上限必须为正")

    db.execute(text("SELECT pg_advisory_xact_lock(:ns, :sid)"),
               {"ns": LOCK_NS_SITE, "sid": site.id})

    revision_ref = f"REV-{datetime.now():%m%d%H%M%S}"
    row = SitePowerLimit(
        site_id=site.id,
        start_at=req.start_at,
        end_at=req.end_at,
        transformer_limit_kw=req.transformer_limit_kw,
        emergency=req.emergency,
        reason=req.reason or "",
        revision_ref=revision_ref,
    )
    db.add(row)
    db.flush()

    # 用修订后的限额重查整个时段的预算切片
    segments = budget_segments(db, site, req.start_at, req.end_at)

    # 所有重叠会话的逐项复核结论
    recheck: list[dict] = []
    affected_map: dict[int, dict] = {}
    for seg in segments:
        if seg.guaranteed_kw > seg.limit_kw + EPS:
            for item in _curtail_segment(db, site, seg):
                aid = item["reservation_id"]
                if aid not in affected_map:
                    affected_map[aid] = {
                        "reservation_id": aid,
                        "ref": item["ref"],
                        "status": item["status"],
                        "guaranteed_min_kw": item["guaranteed_min_kw"],
                        "worst_shortfall_kw": item["shortfall_kw"],
                        "segments": [],
                    }
                affected_map[aid]["worst_shortfall_kw"] = r2(max(
                    affected_map[aid]["worst_shortfall_kw"], item["shortfall_kw"]
                ))
                affected_map[aid]["segments"].append({
                    "start_at": item["segment_start"],
                    "end_at": item["segment_end"],
                    "shortfall_kw": item["shortfall_kw"],
                })

    overlapping_ids = set()
    for seg in segments:
        for r in seg.reservations:
            overlapping_ids.add(r.id)
    for rid in sorted(overlapping_ids):
        r = db.get(Reservation, rid)
        hit = affected_map.get(rid)
        recheck.append({
            "reservation_id": rid,
            "ref": r.ref,
            "status": r.status.value,
            "guaranteed_min_kw": fnum(r.min_power_kw),
            "decision": "SHORTFALL_TO_OPS" if hit else "GUARANTEE_HELD",
            "worst_shortfall_kw": hit["worst_shortfall_kw"] if hit else 0.0,
            "reason": (
                f"新限额下最低保证存在 {hit['worst_shortfall_kw']:g}kW 缺口，"
                f"必须转运营处置"
                if hit else "新限额重检通过，明确承诺的最低值仍可全额保障"
            ),
        })

    affected = list(affected_map.values())

    if affected and not req.emergency:
        # 非紧急修订不允许把活动会话压到承诺最低值以下：拒绝本次修订。
        # 抛出后幂等层回滚保存点，刚写入的限额行一并撤销。
        raise HTTPException(409, {
            "message": (
                "该限额修订会使部分车辆的明确最低保证无法满足；"
                "如属外部电网紧急限额，请以 emergency=true 提交，"
                "系统将把具体受影响车辆送入运营处置。"
            ),
            "affected": affected,
            "recheck": recheck,
        })

    cases: list[OpsCase] = []
    if req.emergency and affected:
        for item in affected:
            r = db.get(Reservation, item["reservation_id"])
            case = OpsCase(
                site_id=site.id,
                reservation_id=r.id,
                kind=OpsCaseKind.GRID_EMERGENCY,
                status=OpsCaseStatus.OPEN,
                title=f"电网紧急限额：{r.ref} 最低保证缺口 {item['worst_shortfall_kw']:g}kW",
                detail=__import__("json").dumps({
                    "revision_ref": revision_ref,
                    "limit_row_id": row.id,
                    "new_limit_kw": req.transformer_limit_kw,
                    "reason": req.reason,
                    **item,
                }, ensure_ascii=False, default=str),
                guaranteed_min_kw=item["guaranteed_min_kw"],
                allocated_kw=r2(item["guaranteed_min_kw"] - item["worst_shortfall_kw"]),
                shortfall_kw=item["worst_shortfall_kw"],
            )
            db.add(case)
            db.flush()
            cases.append(case)
            add_event(db, r, "OPS_CASE_OPENED", {
                "case_id": case.id,
                "kind": case.kind.value,
                "shortfall_kw": item["worst_shortfall_kw"],
                "revision_ref": revision_ref,
                "note": "外部电网紧急限额导致最低保证无法全额满足，转入运营处置，面板不显示正常",
            })

    # 在修订生效起点做一次重分配，写司机可见的功率变化/依据
    board = None
    if overlapping_ids:
        board = reallocate(
            db, site.id, req.reallocate_at or req.start_at,
            trigger="LIMIT_REVISION",
        )

    db.flush()
    return row, recheck, cases, board


def resolve_case(db: Session, case_id: int, note: str) -> OpsCase:
    case = db.get(OpsCase, case_id)
    if case is None:
        raise HTTPException(404, "处置工单不存在")
    if case.status == OpsCaseStatus.RESOLVED:
        raise HTTPException(409, f"工单 #{case_id} 已处置关闭")
    case.status = OpsCaseStatus.RESOLVED
    case.resolution_note = note
    case.resolved_at = datetime.now()
    db.flush()
    if case.reservation_id:
        add_event(db, db.get(Reservation, case.reservation_id), "OPS_CASE_RESOLVED", {
            "case_id": case.id,
            "note": note,
        })
    return case
