"""本地样例数据：东站 A/B/C/D 四枪 + 四辆车 + A 枪 18:00-18:30 有效预约。

运行：python -m app.seed
幂等策略：直接 drop/create 全部表后重灌，仅用于本地演示与重置。
"""

import json
from datetime import datetime

from .database import engine, SessionLocal
from .enums import AllocationPolicy, ReservationMode, ReservationStatus
from .models import (
    Base,
    Connector,
    Reservation,
    ReservationEvent,
    Site,
    SitePowerLimit,
    Vehicle,
)

# 固定在“周五”这一天（2026-11-27 为周五），演示统一使用朴素本地时间
FRIDAY = "2026-11-27"


def _dt(hm: str) -> datetime:
    return datetime.fromisoformat(f"{FRIDAY}T{hm}:00")


def seed() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    db = SessionLocal()
    try:
        # ---------- 车辆 ----------
        v1 = Vehicle(
            name="沪A·11111 家用轿车（已持有 A 枪预约）",
            connector_type="CCS", max_power_kw=120, accessible=False,
        )
        v2 = Vehicle(
            name="沪B·22222 小型车（最大受电仅 60kW）",
            connector_type="CCS", max_power_kw=60, accessible=False,
        )
        v3 = Vehicle(
            name="沪C·33333 无障碍车辆（需无障碍车位）",
            connector_type="CCS", max_power_kw=100, accessible=True,
        )
        v4 = Vehicle(
            name="沪D·44444 长续航 SUV（请求 90kW 的第二位车主）",
            connector_type="CCS", max_power_kw=150, accessible=False,
        )
        # 题面功率预算场景三辆车：均保证至少 60kW、希望达到 90kW
        r1 = Vehicle(
            name="R1 网约车（保证 60 / 期望 90，稍后电池限功率）",
            connector_type="CCS", max_power_kw=120, accessible=False,
        )
        r2 = Vehicle(
            name="R2 出租车（保证 60 / 期望 90）",
            connector_type="CCS", max_power_kw=120, accessible=False,
        )
        r3 = Vehicle(
            name="R3 私家车（保证 60 / 期望 90）",
            connector_type="CCS", max_power_kw=120, accessible=False,
        )
        db.add_all([v1, v2, v3, v4, r1, r2, r3])
        db.flush()

        # ---------- 站点与枪口 ----------
        # 东站保留原 16 项场景：四枪额定合计 420kW，变压器给 360kW，
        # 使既有“枪口兼容性/维护”回放不被站点预算干扰。
        site = Site(
            name="东站充电站",
            location="东站地下停车场 B1 层西侧",
            transformer_limit_kw=360,
            allocation_policy=AllocationPolicy.GUARANTEED_MIN,
        )
        db.add(site)
        db.flush()

        connectors = [
            Connector(site_id=site.id, code="A", connector_type="CCS",
                      rated_power_kw=120, accessible=True,
                      position_note="西侧 1 号位，靠通道，无障碍车位"),
            Connector(site_id=site.id, code="B", connector_type="CCS",
                      rated_power_kw=120, accessible=True,
                      position_note="西侧 2 号位，靠通道，无障碍车位"),
            Connector(site_id=site.id, code="C", connector_type="CCS",
                      rated_power_kw=60, accessible=False,
                      position_note="东侧靠墙，普通车位，额定仅 60kW"),
            Connector(site_id=site.id, code="D", connector_type="CHAdeMO",
                      rated_power_kw=120, accessible=False,
                      position_note="东侧柱后，CHAdeMO 接口，与 CCS 车辆不兼容"),
        ]
        db.add_all(connectors)
        db.flush()
        gun_a, gun_b = connectors[0], connectors[1]

        # ---------- A 枪 18:00-18:30 已存在的有效预约 ----------
        snapshot = {
            "mode": ReservationMode.CONNECTOR.value,
            "connector_id": gun_a.id,
            "connector_code": "A",
            "connector_type": "CCS",
            "min_power_kw": 90,
            "effective_power_kw": 120.0,
            "accessible": True,
            "promise": "锁定枪口 A，全程有效功率不低于 120kW",
        }
        existing = Reservation(
            ref="R-SEEDA01",
            vehicle_id=v1.id,
            site_id=site.id,
            mode=ReservationMode.CONNECTOR,
            status=ReservationStatus.CONFIRMED,
            start_at=_dt("18:00"),
            end_at=_dt("18:30"),
            min_power_kw=90,
            connector_id=gun_a.id,
            confirmation_snapshot=json.dumps(snapshot, ensure_ascii=False),
        )
        db.add(existing)
        db.flush()
        db.add(ReservationEvent(
            reservation_id=existing.id,
            event_type="CONFIRMED",
            detail=json.dumps({"snapshot": snapshot}, ensure_ascii=False),
        ))

        # ---------- 题面：180kW 变压器 + 三支均兼容的枪 ----------
        power_site = Site(
            name="临港 P3 功率预算示范站",
            location="临港产业园 P3 停车场（3 支 CCS 兼容枪，变压器上限 180kW）",
            transformer_limit_kw=180,
            # 默认最低保证模式；两种方式都可在回放时按请求显式指定/切换
            allocation_policy=AllocationPolicy.GUARANTEED_MIN,
        )
        db.add(power_site)
        db.flush()

        power_guns = [
            Connector(site_id=power_site.id, code="P1", connector_type="CCS",
                      rated_power_kw=120, accessible=False,
                      position_note="P3 区 1 号，CCS 直流双枪之一"),
            Connector(site_id=power_site.id, code="P2", connector_type="CCS",
                      rated_power_kw=120, accessible=False,
                      position_note="P3 区 2 号，CCS 直流双枪之二"),
            Connector(site_id=power_site.id, code="P3", connector_type="CCS",
                      rated_power_kw=120, accessible=False,
                      position_note="P3 区 3 号，CCS 直流桩"),
        ]
        db.add_all(power_guns)
        db.flush()

        # 晚高峰分时预算时段：17:30–21:00 变压器上限 180kW（题面基准）
        db.add(SitePowerLimit(
            site_id=power_site.id,
            start_at=datetime.fromisoformat(f"{FRIDAY}T17:30:00"),
            end_at=datetime.fromisoformat(f"{FRIDAY}T21:00:00"),
            transformer_limit_kw=180,
            emergency=False,
            reason="晚高峰计划限额：配电变压器上限 180kW",
            revision_ref="SEED-BASE",
        ))

        db.commit()

        print("种子数据完成：")
        print(f"  站点1: {site.name}（id={site.id}，变压器 {float(site.transformer_limit_kw):g}kW），"
              "枪口 A/B=120kW CCS，C=60kW CCS，D=120kW CHAdeMO")
        print(f"  车辆 V1..V4：原维护/容量场景；预约 {existing.ref} 锁定 A 枪 {FRIDAY} 18:00-18:30")
        print(f"  站点2: {power_site.name}（id={power_site.id}，变压器 180kW），P1/P2/P3 三支 CCS 120kW 全兼容")
        print("  车辆 R1/R2/R3（id=5/6/7）：CCS 120kW，保证≥60、期望 90")
        print("  => 期望预留模式：2 辆×90 占满 180，第 3 辆无法确认")
        print("  => 最低保证模式：3 辆×60 正好 180，均可确认")
        print("  => 18:00-18:10 的 90kW 请求：B 是东站唯一剩余合格容量")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
