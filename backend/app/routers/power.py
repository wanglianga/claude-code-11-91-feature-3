"""站点功率预算、实时分配、车辆读数与运营处置接口。"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import IdempotentConflict, idempotent_endpoint
from ..enums import OpsCaseStatus
from ..models import OpsCase, Site
from ..schemas import (
    OpsCaseOut,
    OpsCaseResolve,
    PowerLimitCreate,
    PowerLimitOut,
    PowerReadingCreate,
    ReallocateRequest,
    ReservationOut,
    SitePolicyUpdate,
)
from ..services import power as svc

router = APIRouter(prefix="/api", tags=["power-budget"])


def _idem(req: Request, db: Session, key: str, payload: dict, work):
    try:
        return idempotent_endpoint(db, key, req.url.path, payload, work)
    except IdempotentConflict as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body)


# ---------- 预算切片与实时分配面板 ----------

@router.get("/sites/{site_id}/power-budget")
def power_budget(
    site_id: int,
    start_at: datetime = Query(...),
    end_at: datetime | None = Query(default=None),
    duration_minutes: int | None = Query(default=None, ge=1, le=1440),
    db: Session = Depends(get_db),
):
    site = db.get(Site, site_id)
    if site is None:
        from fastapi import HTTPException
        raise HTTPException(404, "站点不存在")
    if end_at is None:
        end_at = start_at + timedelta(minutes=duration_minutes or 60)
    segments = svc.budget_segments(db, site, start_at, end_at)
    return {
        "site_id": site_id,
        "transformer_limit_kw": float(site.transformer_limit_kw),
        "allocation_policy": site.allocation_policy.value,
        "segments": [
            {
                "start_at": s.start_at,
                "end_at": s.end_at,
                "transformer_limit_kw": s.limit_kw,
                "limit_revision_id": s.limit_row_id,
                "guaranteed_committed_kw": s.guaranteed_kw,
                "expected_reserved_kw": s.expected_reserved_kw,
                "hard_committed_kw": s.hard_committed_kw,
                "acceptable_float_kw": s.acceptable_float_kw,
                "guarantee_headroom_kw": round(s.limit_kw - s.guaranteed_kw, 2),
                "reservation_refs": [r.ref for r in s.reservations],
            }
            for s in segments
        ],
    }


@router.get("/sites/{site_id}/power-board")
def power_board(
    site_id: int,
    at: datetime = Query(...),
    db: Session = Depends(get_db),
):
    return svc.compute_board(db, site_id, at)


@router.post("/sites/{site_id}/reallocate")
def reallocate(site_id: int, body: ReallocateRequest, request: Request,
               db: Session = Depends(get_db)):
    payload = {"site_id": site_id, **body.model_dump(mode="json")}

    def work() -> dict:
        return svc.reallocate(db, site_id, body.at, trigger="REALLOCATE")

    return _idem(request, db, body.idempotency_key, payload, work)


@router.post("/sites/{site_id}/policy")
def update_site_policy(site_id: int, body: SitePolicyUpdate, db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    if site is None:
        from fastapi import HTTPException
        raise HTTPException(404, "站点不存在")
    site.allocation_policy = body.allocation_policy
    if body.transformer_limit_kw is not None:
        site.transformer_limit_kw = body.transformer_limit_kw
    db.commit()
    db.refresh(site)
    return {
        "site_id": site.id,
        "allocation_policy": site.allocation_policy.value,
        "transformer_limit_kw": float(site.transformer_limit_kw),
    }


# ---------- 车辆实时功率/电池降额回报 ----------

@router.post("/reservations/{reservation_id}/power-reading")
def power_reading(reservation_id: int, body: PowerReadingCreate, request: Request,
                  db: Session = Depends(get_db)):
    payload = {"reservation_id": reservation_id, **body.model_dump(mode="json")}

    def work() -> dict:
        r, board = svc.record_reading(db, reservation_id, body)
        return {
            "reservation": ReservationOut.model_validate(r).model_dump(mode="json"),
            "board": board,
        }

    return _idem(request, db, body.idempotency_key, payload, work)


# ---------- 站点限额修订（运营） ----------

ops_router = APIRouter(prefix="/api/operations", tags=["power-operations"])


@ops_router.post("/power-limits")
def create_power_limit(body: PowerLimitCreate, request: Request, db: Session = Depends(get_db)):
    payload = body.model_dump(mode="json")

    def work() -> dict:
        row, recheck, cases, board = svc.revise_site_limit(db, body)
        return {
            "limit": PowerLimitOut.model_validate(row).model_dump(mode="json"),
            "recheck": recheck,
            "cases": [OpsCaseOut.model_validate(c).model_dump(mode="json") for c in cases],
            "board": board,
        }

    return _idem(request, db, body.idempotency_key, payload, work)


@ops_router.get("/ops-cases", response_model=list[OpsCaseOut])
def list_ops_cases(status: OpsCaseStatus | None = Query(default=None),
                   db: Session = Depends(get_db)):
    stmt = select(OpsCase).order_by(OpsCase.id.desc())
    if status is not None:
        stmt = stmt.where(OpsCase.status == status)
    return list(db.scalars(stmt).all())


@ops_router.post("/demo-reset")
def demo_reset():
    """仅用于隔离演示/评测库：重建并灌回题面种子数据。

    seed() 与主应用共用 engine；重建表后释放连接池，避免长驻进程复用
    旧表结构的 psycopg 预编译计划（cached plan must not change result type）。
    """
    from ..database import engine
    from ..seed import seed
    seed()
    engine.dispose()
    return {"status": "reset-ok"}


@ops_router.post("/ops-cases/{case_id}/resolve", response_model=OpsCaseOut)
def resolve_ops_case(case_id: int, body: OpsCaseResolve, request: Request,
                     db: Session = Depends(get_db)):
    payload = {"case_id": case_id, **body.model_dump(mode="json")}

    def work() -> dict:
        case = svc.resolve_case(db, case_id, body.resolution_note)
        return OpsCaseOut.model_validate(case).model_dump(mode="json")

    return _idem(request, db, body.idempotency_key, payload, work)
