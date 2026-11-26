"""预约用例：确认/取消/迟到/到站/开始充电的状态机与并发控制。

并发安全：确认事务先取站点级 pg_advisory_xact_lock，再在同一事务内
重新计算重叠时段容量后落单，两笔并发请求必然串行，后到者看到先到者
已提交的占用，因此同一份服务能力不会被卖出两次。
幂等回放由路由层（app.deps）统一负责，本模块只实现业务本身。
"""

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..enums import AllocationPolicy, ReservationMode, ReservationStatus
from ..models import Reservation, ReservationEvent, Site, Vehicle
from ..schemas import ConfirmRequest
from .capacity import evaluate_site, find_connector_eval
from .common import LOCK_NS_RESERVATION, LOCK_NS_SITE
from .power import assert_confirm_budget


def _body_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _add_event(db: Session, reservation: Reservation, event_type: str, detail: dict) -> None:
    event = ReservationEvent(
        reservation_id=reservation.id,
        event_type=event_type,
        detail=json.dumps(detail, ensure_ascii=False, default=str),
    )
    reservation.events.append(event)
    db.add(event)
    # 显式刷新以生成 id/created_at，保证当次响应即可读到完整事件流
    db.flush()


def _new_ref() -> str:
    return f"R-{uuid4().hex[:8].upper()}"


def _promise_text(code, gun_power, site, policy, min_kw, expected_kw) -> str:
    if policy == AllocationPolicy.RESERVE_EXPECTED:
        budget_rule = f"按期望 {expected_kw:g}kW 为整段预留站点预算"
    else:
        budget_rule = f"仅硬承诺最低 {min_kw:g}kW，剩余预算按 5 分钟读数动态分配"
    return (
        f"锁定枪口 {code}，单枪窗内有效功率不低于 {gun_power:g}kW；"
        f"同时受站点变压器上限 {float(site.transformer_limit_kw):g}kW 总预算约束，{budget_rule}"
    )


# ---------- 确认 ----------

def confirm_reservation(db: Session, req: ConfirmRequest) -> Reservation:
    vehicle = db.get(Vehicle, req.vehicle_id)
    if vehicle is None:
        raise HTTPException(404, "车辆不存在")
    site = db.get(Site, req.site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    if req.duration_minutes <= 0:
        raise HTTPException(422, "停留时长必须为正")

    start = req.start_at
    end = datetime.fromtimestamp(start.timestamp() + req.duration_minutes * 60)

    # 站点级串行化：同站并发确认在此排队
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :sid)"),
        {"ns": LOCK_NS_SITE, "sid": site.id},
    )

    # 期望功率与预算策略：请求可显式指定（用于两种分配方式回放），
    # 否则期望=最低值、策略取站点当前策略。
    expected_kw = (
        float(req.expected_power_kw)
        if getattr(req, "expected_power_kw", None) is not None
        else float(req.min_power_kw)
    )
    if expected_kw + 1e-9 < float(req.min_power_kw):
        raise HTTPException(422, "期望功率不能低于明确承诺的最低功率")
    policy = (
        req.allocation_policy
        if getattr(req, "allocation_policy", None) is not None
        else site.allocation_policy
    )

    # 站点级分时功率预算校验：即使每支枪都空闲，也不能把变压器总容量超卖。
    budget_summary = assert_confirm_budget(
        db, site, start, end, float(req.min_power_kw), expected_kw, policy
    )

    evaluation = evaluate_site(db, site.id, vehicle, start, end, req.min_power_kw)

    connector_id: int | None = None
    snapshot: dict

    if req.mode == ReservationMode.CONNECTOR:
        chosen = None
        if req.preferred_connector_id is not None:
            chosen = find_connector_eval(evaluation.connector_evals, req.preferred_connector_id)
            if chosen is None:
                raise HTTPException(404, "指定枪口不在该站点")
            if not chosen.eligible:
                raise HTTPException(
                    409,
                    {"message": "指定枪口无法满足本预约", "reasons": chosen.reasons},
                )
        else:
            for ev in evaluation.connector_evals:
                if ev.eligible:
                    chosen = ev
                    break
        if chosen is None:
            raise HTTPException(
                409,
                {
                    "message": "该时间窗内没有可确认的合格枪口（候选搜索不构成订单）",
                    "connector_reasons": [
                        {"connector_code": ev.connector.code, "reasons": ev.reasons}
                        for ev in evaluation.connector_evals
                    ],
                },
            )
        connector_id = chosen.connector.id
        snapshot = {
            "mode": req.mode.value,
            "connector_id": chosen.connector.id,
            "connector_code": chosen.connector.code,
            "connector_type": chosen.connector.connector_type,
            "connector_rated_power_kw": float(chosen.connector.rated_power_kw),
            "min_power_kw": req.min_power_kw,
            "expected_power_kw": expected_kw,
            "allocation_policy": policy.value,
            "site_transformer_limit_kw": float(site.transformer_limit_kw),
            "power_budget": budget_summary,
            "effective_power_kw": chosen.min_power,
            "accessible": chosen.connector.accessible,
            "promise": _promise_text(chosen.connector.code, chosen.min_power,
                                     site, policy, req.min_power_kw, expected_kw),
        }
    else:
        if evaluation.confirmable_pool_slots < 1:
            raise HTTPException(
                409,
                {
                    "message": "资源池内已无可同时履约的合格名额",
                    "confirmable_slots": 0,
                    "connector_reasons": [
                        {"connector_code": ev.connector.code, "reasons": ev.reasons}
                        for ev in evaluation.connector_evals
                    ],
                },
            )
        snapshot = {
            "mode": req.mode.value,
            "connector_id": None,
            "connector_code": None,
            "connector_type": vehicle.connector_type,
            "min_power_kw": req.min_power_kw,
            "expected_power_kw": expected_kw,
            "allocation_policy": policy.value,
            "site_transformer_limit_kw": float(site.transformer_limit_kw),
            "power_budget": budget_summary,
            "accessible_required": vehicle.accessible,
            "confirmable_slots_at_booking": evaluation.confirmable_pool_slots,
            "promise": (
                f"站点资源池名额 1 个（当时池内可同时履约名额 "
                f"{evaluation.confirmable_pool_slots}），到站再分配具体枪口；"
                f"站点变压器上限 {float(site.transformer_limit_kw):g}kW，"
                + (f"按期望 {expected_kw:g}kW 整段预留预算"
                   if policy == AllocationPolicy.RESERVE_EXPECTED
                   else f"硬承诺最低 {req.min_power_kw:g}kW，余量动态分配")
            ),
        }

    reservation = Reservation(
        ref=_new_ref(),
        vehicle_id=vehicle.id,
        site_id=site.id,
        mode=req.mode,
        status=ReservationStatus.CONFIRMED,
        start_at=start,
        end_at=end,
        min_power_kw=req.min_power_kw,
        expected_power_kw=expected_kw,
        allocation_policy=policy,
        connector_id=connector_id,
        confirmation_snapshot=json.dumps(snapshot, ensure_ascii=False, default=str),
    )
    db.add(reservation)
    db.flush()
    _add_event(db, reservation, "CONFIRMED", {"snapshot": snapshot})
    return reservation


# ---------- 状态转换 ----------

ALLOWED_TRANSITIONS = {
    "cancel": {ReservationStatus.CONFIRMED: ReservationStatus.CANCELLED},
    "arrive": {ReservationStatus.CONFIRMED: ReservationStatus.ARRIVED},
    "start_charging": {
        ReservationStatus.CONFIRMED: ReservationStatus.CHARGING,
        ReservationStatus.ARRIVED: ReservationStatus.CHARGING,
    },
    "no_show": {ReservationStatus.CONFIRMED: ReservationStatus.NO_SHOW},
    "complete": {ReservationStatus.CHARGING: ReservationStatus.COMPLETED},
}


def _locked_reservation(db: Session, reservation_id: int) -> Reservation:
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :rid)"),
        {"ns": LOCK_NS_RESERVATION, "rid": reservation_id},
    )
    reservation = db.get(Reservation, reservation_id)
    if reservation is None:
        raise HTTPException(404, "预约不存在")
    return reservation


def _transition(db: Session, reservation_id: int, action: str, build) -> Reservation:
    reservation = _locked_reservation(db, reservation_id)
    allowed = ALLOWED_TRANSITIONS[action]
    if reservation.status not in allowed:
        raise HTTPException(
            409,
            f"预约 {reservation.ref} 当前状态 {reservation.status.value} 不允许 {action}",
        )
    build(reservation)
    db.flush()
    return reservation


def cancel(db: Session, reservation_id: int) -> Reservation:
    def build(r: Reservation) -> None:
        r.status = ReservationStatus.CANCELLED
        # 取消即释放：connector_id 保留用于审计，但容量引擎只统计 HOLDING 状态
        _add_event(db, r, "CANCELLED", {
            "released_connector_id": r.connector_id,
            "note": "容量已释放，可被新预约使用",
        })
    return _transition(db, reservation_id, "cancel", build)


def mark_no_show(db: Session, reservation_id: int) -> Reservation:
    def build(r: Reservation) -> None:
        if datetime.now() < r.start_at:
            raise HTTPException(409, "预约开始时间未到，不能标记迟到；如需放弃请取消")
        r.status = ReservationStatus.NO_SHOW
        _add_event(db, r, "NO_SHOW", {"released_connector_id": r.connector_id})
    return _transition(db, reservation_id, "no_show", build)


def arrive(db: Session, reservation_id: int) -> Reservation:
    def build(r: Reservation) -> None:
        if r.mode == ReservationMode.POOL and r.connector_id is None:
            # 到站分配：在当前真实设备状态下挑一支合格且未锁定的枪
            vehicle = db.get(Vehicle, r.vehicle_id)
            evaluation = evaluate_site(
                db, r.site_id, vehicle, r.start_at, r.end_at, float(r.min_power_kw)
            )
            for ev in evaluation.connector_evals:
                # eligible 已包含接口/无障碍/功率/未被锁定
                if ev.eligible:
                    r.connector_id = ev.connector.id
                    break
            if r.connector_id is None:
                raise HTTPException(
                    409,
                    {
                        "message": "到站时资源池已无合格枪口，请等待人工联系或改选",
                        "connector_reasons": [
                            {"connector_code": ev.connector.code, "reasons": ev.reasons}
                            for ev in evaluation.connector_evals
                        ],
                    },
                )
        r.status = ReservationStatus.ARRIVED
        _add_event(db, r, "ARRIVED", {"assigned_connector_id": r.connector_id})
    return _transition(db, reservation_id, "arrive", build)


def start_charging(db: Session, reservation_id: int) -> Reservation:
    def build(r: Reservation) -> None:
        if r.connector_id is None:
            raise HTTPException(409, "资源池预约尚未分配枪口，无法开始充电")
        r.status = ReservationStatus.CHARGING
        # 初始分配不低于明确承诺的最低值；随后由车辆 5 分钟读数驱动动态重分配。
        if r.allocated_power_kw is None:
            r.allocated_power_kw = r.min_power_kw
        _add_event(db, r, "CHARGING_STARTED", {
            "connector_id": r.connector_id,
            "initial_allocated_kw": float(r.allocated_power_kw),
            "guaranteed_min_kw": float(r.min_power_kw),
        })
        # 以预约时段起点作为确定性回放时刻做一次全站分配（同事务咨询锁可重入）
        from .power import reallocate
        reallocate(db, r.site_id, r.start_at, trigger="SESSION_START", focus_id=r.id)
    return _transition(db, reservation_id, "start_charging", build)


def complete(db: Session, reservation_id: int) -> Reservation:
    def build(r: Reservation) -> None:
        r.status = ReservationStatus.COMPLETED
        _add_event(db, r, "COMPLETED", {"connector_id": r.connector_id})
    return _transition(db, reservation_id, "complete", build)
