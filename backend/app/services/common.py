"""服务层公共助手：订单事件留痕与咨询锁命名空间。"""

import json

from sqlalchemy.orm import Session

from ..models import ReservationEvent

# 咨询锁命名空间（固定常量，避免与其他锁冲突）
LOCK_NS_SITE = 91001
LOCK_NS_RESERVATION = 91002
LOCK_NS_CONNECTOR = 91003


def add_event(db: Session, reservation, event_type: str, detail: dict) -> ReservationEvent:
    event = ReservationEvent(
        reservation_id=reservation.id,
        event_type=event_type,
        detail=json.dumps(detail, ensure_ascii=False, default=str),
    )
    reservation.events.append(event)
    db.add(event)
    # 显式刷新以生成 id/created_at，保证当次响应即可读到完整事件流
    db.flush()
    return event
