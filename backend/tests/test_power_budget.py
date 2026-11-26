"""站点级分时功率预算场景：两种分配方式、并发接入、电池降额释放、限额修订与紧急处置。

题面固定数据（见 app/seed.py）：
- 站点 2「临港 P3」：变压器上限 180kW，P1/P2/P3 三支 CCS 120kW 全兼容；
- 车辆 R1/R2/R3（id=5/6/7）：保证至少 60kW、希望 90kW。
"""

import threading

from tests.conftest import dt

SITE = 2
R1, R2, R3 = 5, 6, 7
V4 = 4
P1, P2, P3 = 5, 6, 7
T0 = dt("18:00")
T5 = dt("18:05")
T10 = dt("18:10")


def K(key: str) -> str:
    # ConfirmRequest 要求幂等键至少 8 个字符，统一补齐
    return key if len(key) >= 8 else key + "-key"


def _confirm(client, vehicle, policy, key, minp=60, exp=90, minutes=30,
             mode="CONNECTOR"):
    return client.post("/api/reservations", json={
        "vehicle_id": vehicle,
        "site_id": SITE,
        "start_at": T0,
        "duration_minutes": minutes,
        "min_power_kw": minp,
        "expected_power_kw": exp,
        "allocation_policy": policy,
        "mode": mode,
        "idempotency_key": K(key),
    })


def _start(client, rid, key):
    return client.post(f"/api/reservations/{rid}/start",
                       json={"idempotency_key": K(key)})


def _reading(client, rid, key, available=60, derated=True, at=T5):
    return client.post(f"/api/reservations/{rid}/power-reading", json={
        "current_available_kw": available,
        "battery_derated": derated,
        "note": "BMS 电池限功率回报",
        "reading_at": at,
        "idempotency_key": K(key),
    })


def _board(client, at=T0):
    r = client.get(f"/api/sites/{SITE}/power-board", params={"at": at})
    assert r.status_code == 200
    return r.json()


def _session(board, rid):
    return next(s for s in board["sessions"] if s["reservation_id"] == rid)


# ---------- 1. 期望整段预留：两辆占满 180，第三辆无法确认 ----------

def test_expected_policy_two_accepted_third_rejected_with_budget_detail(client):
    r1 = _confirm(client, R1, "RESERVE_EXPECTED", "pw-exp-1")
    r2 = _confirm(client, R2, "RESERVE_EXPECTED", "pw-exp-2")
    r3 = _confirm(client, R3, "RESERVE_EXPECTED", "pw-exp-3")

    assert r1.status_code == 200 and r2.status_code == 200
    won_guns = {r1.json()["connector_id"], r2.json()["connector_id"]}
    assert won_guns <= {P1, P2, P3} and len(won_guns) == 2
    assert r3.status_code == 409
    detail = r3.json()["detail"]
    assert "变压器" in detail["message"]
    budget = detail["budget"]
    assert budget["policy"] == "RESERVE_EXPECTED"
    # 最紧张切片：已按期望预留 180，剩余 0，本单还要 90
    tight = budget["tightest"]
    assert tight["transformer_limit_kw"] == 180
    assert tight["hard_committed_kw"] == 180
    assert tight["remaining_kw"] == 0
    assert tight["ok"] is False


def test_expected_policy_concurrent_three_exactly_two_win(client):
    from fastapi.testclient import TestClient
    from app.main import app

    barrier = threading.Barrier(3)
    results = []

    def worker(vid, key):
        with TestClient(app) as c:
            barrier.wait()
            results.append(_confirm(c, vid, "RESERVE_EXPECTED", key).status_code)

    threads = [
        threading.Thread(target=worker, args=(R1, "pw-cexp-1")),
        threading.Thread(target=worker, args=(R2, "pw-cexp-2")),
        threading.Thread(target=worker, args=(R3, "pw-cexp-3")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 200, 409]
    listing = client.get("/api/reservations").json()
    site2 = [r for r in listing if r["site_id"] == SITE]
    assert len(site2) == 2  # 并发下也绝不超卖


def test_expected_policy_derate_does_not_release_booking_quota(client):
    """期望预留模式：R1 提前降功率到 60，空出的额度仍不能让第三辆按期望确认。"""
    r1 = _confirm(client, R1, "RESERVE_EXPECTED", "pw-der-exp-1")
    r2 = _confirm(client, R2, "RESERVE_EXPECTED", "pw-der-exp-2")
    rid1 = r1.json()["id"]
    _start(client, rid1, "pw-der-exp-start")
    rep = _reading(client, rid1, "pw-der-exp-read", available=60)
    assert rep.status_code == 200

    # 即使 R1 实际只接受 60，期望预留口径仍占 90：第三辆期望单依旧被拒
    r3 = _confirm(client, R3, "RESERVE_EXPECTED", "pw-der-exp-3")
    assert r3.status_code == 409
    assert r3.json()["detail"]["budget"]["policy"] == "RESERVE_EXPECTED"


# ---------- 2. 最低保证模式：三辆 60 正好 180 ----------

def test_guaranteed_policy_three_accepted_fourth_rejected(client):
    r1 = _confirm(client, R1, "GUARANTEED_MIN", "pw-gm-1")
    r2 = _confirm(client, R2, "GUARANTEED_MIN", "pw-gm-2")
    r3 = _confirm(client, R3, "GUARANTEED_MIN", "pw-gm-3")
    assert all(r.status_code == 200 for r in (r1, r2, r3))

    fourth = _confirm(client, V4, "GUARANTEED_MIN", "pw-gm-4", minp=60, exp=90)
    assert fourth.status_code == 409
    assert "最低保证预算不足" in fourth.json()["detail"]["message"]


def test_guaranteed_policy_concurrent_three_all_accepted(client):
    from fastapi.testclient import TestClient
    from app.main import app

    barrier = threading.Barrier(3)
    results = []

    def worker(vid, key):
        with TestClient(app) as c:
            barrier.wait()
            resp = _confirm(c, vid, "GUARANTEED_MIN", key)
            results.append((resp.status_code, resp.json().get("connector_id")))

    threads = [
        threading.Thread(target=worker, args=(R1, "pw-cgm-1")),
        threading.Thread(target=worker, args=(R2, "pw-cgm-2")),
        threading.Thread(target=worker, args=(R3, "pw-cgm-3")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(s for s, _ in results) == [200, 200, 200]
    # 三支枪各分配一支，不重叠
    guns = sorted(cid for _, cid in results)
    assert guns == [P1, P2, P3]


def test_board_shows_limit_guarantee_allocation_and_zero_remaining(client):
    ids = [_confirm(client, v, "GUARANTEED_MIN", f"pw-b-{v}").json()["id"]
           for v in (R1, R2, R3)]
    for i, rid in enumerate(ids):
        assert _start(client, rid, f"pw-b-start-{i}").status_code == 200

    board = _board(client)
    assert board["transformer_limit_kw"] == 180
    assert board["guaranteed_committed_kw"] == 180
    assert board["budget_headroom_kw"] == 0
    assert board["currently_allocated_kw"] == 180
    assert board["power_remaining_now_kw"] == 0
    for rid in ids:
        row = _session(board, rid)
        assert row["guaranteed_min_kw"] == 60
        assert row["allocated_kw"] == 60
        assert row["grid_shortfall_kw"] == 0
        assert row["connector_code"] in {"P1", "P2", "P3"}
        assert row["connector_limit_kw"] == 120  # 单枪限制同时给出


# ---------- 3. R1 电池限功率只接受 60：不得误报缺额 ----------

def test_r1_battery_derate_to_60_is_not_a_shortfall(client):
    ids = [_confirm(client, v, "GUARANTEED_MIN", f"pw-r1-{v}").json()["id"]
           for v in (R1, R2, R3)]
    for i, rid in enumerate(ids):
        _start(client, rid, f"pw-r1-start-{i}")

    resp = _reading(client, ids[0], "pw-r1-read")
    assert resp.status_code == 200
    row = _session(resp.json()["board"], ids[0])
    assert row["battery_derated"] is True
    assert row["reported_available_kw"] == 60
    assert row["allocated_kw"] == 60
    assert row["grid_shortfall_kw"] == 0
    assert row["open_ops_case_id"] is None

    # 工单队列为空：系统没有把正常状态显示成处置
    cases = client.get("/api/operations/ops-cases").json()
    assert cases == []

    # 司机能收到一条“调度结果不变、不存在缺额”的变化/说明记录
    detail = client.get(f"/api/reservations/{ids[0]}").json()
    logs = detail["allocation_logs"]
    assert logs, "应至少有一条功率分配/调度记录"
    assert any("不存在缺额" in log["basis"] for log in logs)


def test_derated_float_is_reallocated_to_another_session(client):
    """降额释放的上浮额度可被仍有上浮空间的其他会话吃掉。"""
    # 两辆车：最低 60、期望/受电上限 120。180 预算下先各上浮到 90。
    r1 = _confirm(client, R1, "GUARANTEED_MIN", "pw-fl-1", exp=120)
    r2 = _confirm(client, R2, "GUARANTEED_MIN", "pw-fl-2", exp=120)
    id1, id2 = r1.json()["id"], r2.json()["id"]
    _start(client, id1, "pw-fl-s1")
    _start(client, id2, "pw-fl-s2")

    before = _board(client)
    assert _session(before, id1)["allocated_kw"] == 90
    assert _session(before, id2)["allocated_kw"] == 90

    # R1 电池限到 60：释放的预算让 R2 从 90 上浮到 120（吃满其期望/枪上限）
    resp = _reading(client, id1, "pw-fl-read", available=60)
    after = resp.json()["board"]
    assert _session(after, id1)["allocated_kw"] == 60
    assert _session(after, id2)["allocated_kw"] == 120
    assert after["currently_allocated_kw"] == 180
    assert after["power_remaining_now_kw"] == 0

    r2detail = client.get(f"/api/reservations/{id2}").json()
    assert any((log["previous_kw"] or 0) == 90 and log["allocated_kw"] == 120
               for log in r2detail["allocation_logs"])


# ---------- 4. 取消/完成释放保证额度，新车可进入 ----------

def test_cancel_releases_guarantee_headroom_for_new_booking(client):
    r1 = _confirm(client, R1, "GUARANTEED_MIN", "pw-rel-1")
    r2 = _confirm(client, R2, "GUARANTEED_MIN", "pw-rel-2")
    r3 = _confirm(client, R3, "GUARANTEED_MIN", "pw-rel-3")
    assert all(r.status_code == 200 for r in (r1, r2, r3))
    blocked = _confirm(client, V4, "GUARANTEED_MIN", "pw-rel-4")
    assert blocked.status_code == 409

    cancel = client.post(f"/api/reservations/{r3.json()['id']}/cancel",
                         json={"idempotency_key": "pw-rel-cancel"})
    assert cancel.status_code == 200

    again = _confirm(client, V4, "GUARANTEED_MIN", "pw-rel-5")
    assert again.status_code == 200  # R3 的 60kW 保证额度已释放


# ---------- 5. 站点限额修订：重检所有重叠会话 ----------

def _revise(client, key, limit, emergency, at=T10, start=T0, end=dt("19:00")):
    return client.post("/api/operations/power-limits", json={
        "site_id": SITE,
        "start_at": start,
        "end_at": end,
        "transformer_limit_kw": limit,
        "emergency": emergency,
        "reason": "限额修订回放",
        "reallocate_at": at,
        "idempotency_key": K(key),
    })


def test_normal_revision_that_keeps_guarantees_rechecks_all(client):
    ids = [_confirm(client, v, "GUARANTEED_MIN", f"pw-rv-{v}").json()["id"]
           for v in (R1, R2, R3)]
    for i, rid in enumerate(ids):
        _start(client, rid, f"pw-rv-start-{i}")

    # 180 -> 180 不变：三笔重叠会话都被重检且保证守住
    resp = _revise(client, "pw-rv-ok", 180, False)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["recheck"]) == 3
    assert all(x["decision"] == "GUARANTEE_HELD" for x in body["recheck"])
    assert body["cases"] == []


def test_non_emergency_revision_breaking_guarantees_is_rejected(client):
    for v in (R1, R2, R3):
        _confirm(client, v, "GUARANTEED_MIN", f"pw-rvn-{v}")
    resp = _revise(client, "pw-rvn-lim", 150, False)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "emergency=true" in detail["message"]
    assert detail["affected"], "必须列出受影响车辆"


def test_emergency_revision_creates_ops_cases_for_specific_vehicles(client):
    refs = []
    ids = []
    for v in (R1, R2, R3):
        r = _confirm(client, v, "GUARANTEED_MIN", f"pw-em-{v}")
        ids.append(r.json()["id"])
        refs.append(r.json()["ref"])
    for i, rid in enumerate(ids):
        _start(client, rid, f"pw-em-start-{i}")

    # 180 -> 100：按确认先后，R1 保 60、R2 缺 20、R3 缺 60
    resp = _revise(client, "pw-em-lim", 100, True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_ref = {x["ref"]: x for x in body["recheck"]}
    affected_refs = {x["ref"] for x in body["recheck"]
                     if x["decision"] == "SHORTFALL_TO_OPS"}
    # 前两笔确认的车中 R2 开始出现缺口，R3 全缺；R1 守住
    assert refs[0] not in affected_refs
    assert refs[2] in affected_refs
    assert len(body["cases"]) == 2
    case_rids = {c["reservation_id"] for c in body["cases"]}
    assert ids[1] in case_rids and ids[2] in case_rids

    # 运行面板进入紧急态并给出具体缺口与工单
    board = _board(client, at=T10)
    assert board["emergency"] is True
    assert board["grid_shortfall_total_kw"] == 80
    r3row = _session(board, ids[2])
    assert r3row["allocated_kw"] == 0
    assert r3row["grid_shortfall_kw"] == 60
    assert r3row["open_ops_case_id"] is not None

    # 运营端工单队列可见且不是“正常”
    open_cases = client.get("/api/operations/ops-cases?status=OPEN").json()
    assert len(open_cases) == 2


def test_emergency_case_can_be_resolved_with_note(client):
    ids = [_confirm(client, v, "GUARANTEED_MIN", f"pw-rs-{v}").json()["id"]
           for v in (R1, R2, R3)]
    _revise(client, "pw-rs-lim", 150, True)  # 仅最后一辆缺 30
    cases = client.get("/api/operations/ops-cases?status=OPEN").json()
    assert len(cases) == 1
    cid = cases[0]["id"]

    done = client.post(f"/api/operations/ops-cases/{cid}/resolve", json={
        "resolution_note": "已现场协调降功率/改派，并向司机发放补偿",
        "idempotency_key": "pw-rs-resolve",
    })
    assert done.status_code == 200
    assert done.json()["status"] == "RESOLVED"
    assert "补偿" in done.json()["resolution_note"]

    assert client.get("/api/operations/ops-cases?status=OPEN").json() == []


# ---------- 6. 分时预算查询接口 ----------

def test_power_budget_endpoint_reports_limit_guarantee_and_float(client):
    _confirm(client, R1, "GUARANTEED_MIN", "pw-seg-1")
    resp = client.get(f"/api/sites/{SITE}/power-budget", params={
        "start_at": T0, "duration_minutes": 60,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["transformer_limit_kw"] == 180
    seg = data["segments"][0]
    assert seg["guaranteed_committed_kw"] == 60
    assert seg["acceptable_float_kw"] == 120  # 180 - 60
    assert seg["expected_reserved_kw"] == 90
