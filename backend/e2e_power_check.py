#!/usr/bin/env python3
"""题面「站点级分时功率预算」端到端回放（真实 HTTP + 真实 PostgreSQL）。

前置：API 已在 BASE（默认 http://127.0.0.1:8000）运行。
本脚本会先调用 /api/operations/demo-reset 重建题面种子数据，可重复执行。

核对：
  A. 期望预留模式：并发 3 辆 90kW -> 恰好 2 成 1 拒，拒绝来自站点 180kW 预算；
  B. 最低保证模式：并发 3 辆 60kW -> 3 成全上 P1/P2/P3；
     开始充电后每车 60、保证合计/已分配 180、实时剩余 0；
  C. R1 电池降额到 60：分配仍 60、缺口 0、不产生处置工单（不得误报缺额）；
  D. 紧急限额 100kW：精确定位后两辆车（缺口 20/60）并生成运营工单；
  E. 非紧急且会击穿保证的限额修订被 409 拒绝；
  F. 司机能查到“不存在缺额”的功率变化/调度依据记录。
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
FRIDAY = "2026-11-27T18:00:00"
SITE = 2
VEHICLES = [5, 6, 7]

_passed = 0
_failed = 0
_lock = threading.Lock()


def check(desc, cond, actual=""):
    global _passed, _failed
    with _lock:
        if cond:
            _passed += 1
            print(f"  ✅ {desc}" + (f"（{actual}）" if actual != "" else ""))
        else:
            _failed += 1
            print(f"  ❌ {desc}：实际 {actual}")


def call(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


_key_n = 0
_key_lock = threading.Lock()


def idem(prefix):
    global _key_n
    with _key_lock:
        _key_n += 1
        return f"e2e-pw-{prefix}-{_key_n:04d}-key"


def confirm(vid, policy):
    return call("POST", "/api/reservations", {
        "vehicle_id": vid, "site_id": SITE, "start_at": FRIDAY,
        "duration_minutes": 30, "min_power_kw": 60, "expected_power_kw": 90,
        "allocation_policy": policy, "mode": "CONNECTOR",
        "idempotency_key": idem("conf"),
    })


def start(rid):
    return call("POST", f"/api/reservations/{rid}/start",
                {"idempotency_key": idem("start")})


def board(at):
    return call("GET", f"/api/sites/{SITE}/power-board?at={at}")[1]


def reset():
    code, body = call("POST", "/api/operations/demo-reset")
    assert code == 200, f"重置失败 {code} {body}"


def concurrent_confirm(policy):
    # 3 个真实并发 HTTP 请求；后端 pg_advisory_xact_lock 将其串行化
    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(lambda v: confirm(v, policy), VEHICLES))


def main():
    code, _ = call("GET", "/health")
    check("health 200", code == 200, code)

    # ---------- A. 期望预留 ----------
    print("== A. RESERVE_EXPECTED：并发 3 辆期望 90kW ==")
    reset()
    results = concurrent_confirm("RESERVE_EXPECTED")
    statuses = sorted(c for c, _ in results)
    check("并发结果恰为 2×200 + 1×409", statuses == [200, 200, 409], statuses)
    rejected = next(b for c, b in results if c == 409)
    tight = rejected.get("detail", {}).get("budget", {}).get("tightest", {})
    check("拒绝来自站点 180kW 预算且已按期望占满",
          tight.get("transformer_limit_kw") == 180
          and tight.get("hard_committed_kw") == 180
          and tight.get("remaining_kw") == 0,
          f"limit={tight.get('transformer_limit_kw')} "
          f"committed={tight.get('hard_committed_kw')}")
    check("拒绝文案说明不能把枪口额定相加超卖",
          "变压器" in rejected.get("detail", {}).get("message", ""))

    # ---------- B. 最低保证 ----------
    print("== B. GUARANTEED_MIN：并发 3 辆保证 60kW ==")
    reset()
    results = concurrent_confirm("GUARANTEED_MIN")
    check("三辆全部确认", all(c == 200 for c, _ in results),
          sorted(c for c, _ in results))
    guns = sorted(b["connector_id"] for c, b in results if c == 200)
    check("分配到 P1/P2/P3（id 5/6/7）且不重叠", guns == [5, 6, 7], guns)
    ids = [b["id"] for c, b in results if c == 200]

    for rid in ids:
        c, _ = start(rid)
        check(f"订单 {rid} 开始充电", c == 200, c)

    b0 = board(FRIDAY)
    check("最低保证合计 = 180kW", b0["guaranteed_committed_kw"] == 180,
          b0["guaranteed_committed_kw"])
    check("当前已分配 = 180kW", b0["currently_allocated_kw"] == 180,
          b0["currently_allocated_kw"])
    check("实时剩余 = 0kW", b0["power_remaining_now_kw"] == 0,
          b0["power_remaining_now_kw"])
    check("每车分配恰好 60kW",
          [s["allocated_kw"] for s in b0["sessions"]] == [60, 60, 60],
          [s["allocated_kw"] for s in b0["sessions"]])
    check("每车都带单枪上限 120kW",
          all(s["connector_limit_kw"] == 120 for s in b0["sessions"]))

    # ---------- C. R1 降额 ----------
    print("== C. R1 电池限功率只接受 60kW：不得误报缺额 ==")
    r1 = ids[0]
    c, rd = call("POST", f"/api/reservations/{r1}/power-reading", {
        "current_available_kw": 60, "battery_derated": True,
        "note": "R1 BMS 限功率", "reading_at": "2026-11-27T18:05:00",
        "idempotency_key": idem("read"),
    })
    check("读数上报 200", c == 200, c)
    row = next(s for s in rd["board"]["sessions"] if s["reservation_id"] == r1)
    check("R1 分配仍为 60kW", row["allocated_kw"] == 60, row["allocated_kw"])
    check("R1 电网缺口为 0（不误报）", row["grid_shortfall_kw"] == 0,
          row["grid_shortfall_kw"])
    check("R1 无运营工单", row["open_ops_case_id"] is None,
          row["open_ops_case_id"])

    c, detail = call("GET", f"/api/reservations/{r1}")
    bases = " ".join(l["basis"] for l in detail.get("allocation_logs", []))
    check("司机收到“不存在缺额”的调度说明", "不存在缺额" in bases)

    # ---------- D. 紧急限额 ----------
    print("== D. 紧急限额 100kW：具体车辆进运营处置 ==")
    c, lim = call("POST", "/api/operations/power-limits", {
        "site_id": SITE, "start_at": FRIDAY,
        "end_at": "2026-11-27T19:00:00", "transformer_limit_kw": 100,
        "emergency": True, "reason": "外部电网紧急限额 E2E",
        "reallocate_at": "2026-11-27T18:10:00",
        "idempotency_key": idem("lim"),
    })
    check("紧急限额修订 200", c == 200, c)
    shorts = sorted(x["worst_shortfall_kw"] for x in lim["recheck"]
                    if x["decision"] == "SHORTFALL_TO_OPS")
    check("缺口恰为 20/60kW 两辆车", shorts == [20, 60], shorts)
    check("生成 2 张具体车辆工单", len(lim["cases"]) == 2, len(lim["cases"]))
    check("每张工单都指向具体预约",
          all(x["reservation_id"] is not None for x in lim["cases"]))

    b10 = board("2026-11-27T18:10:00")
    check("运行面板进入紧急态", b10["emergency"] is True)
    check("面板缺口合计 80kW", b10["grid_shortfall_total_kw"] == 80,
          b10["grid_shortfall_total_kw"])

    c, cases = call("GET", "/api/operations/ops-cases?status=OPEN")
    check("运营工单队列有 2 张 OPEN", len(cases) == 2, len(cases))
    c, resolved = call("POST",
                       f"/api/operations/ops-cases/{cases[0]['id']}/resolve",
                       {"resolution_note": "现场限流并发放补偿 E2E",
                        "idempotency_key": idem("resolve")})
    check("工单可处置关闭", c == 200 and resolved["status"] == "RESOLVED", c)

    # ---------- E. 非紧急击穿保证被拒 ----------
    print("== E. 非紧急限额若击穿最低保证必须 409 ==")
    c, body = call("POST", "/api/operations/power-limits", {
        "site_id": SITE, "start_at": FRIDAY,
        "end_at": "2026-11-27T18:30:00", "transformer_limit_kw": 120,
        "emergency": False, "reason": "计划下调",
        "idempotency_key": idem("lim2"),
    })
    check("非紧急击穿修订返回 409", c == 409, c)
    check("409 响应列出受影响车辆",
          bool(body.get("detail", {}).get("affected")))

    # ---------- F. 分时预算接口 ----------
    print("== F. 分时预算记录 ==")
    reset()
    c, one = confirm(VEHICLES[0], "GUARANTEED_MIN")
    check("F 前置：一笔 60kW 保证单确认", c == 200, c)
    c, seg = call("GET",
                  f"/api/sites/{SITE}/power-budget?start_at={FRIDAY}"
                  "&duration_minutes=60")
    s0 = seg["segments"][0]
    check("预算段含变压器上限/保证合计/可上浮值",
          s0["transformer_limit_kw"] == 180
          and s0["guaranteed_committed_kw"] == 60
          and s0["acceptable_float_kw"] == 120,
          f"limit={s0['transformer_limit_kw']} "
          f"guaranteed={s0['guaranteed_committed_kw']} "
          f"float={s0['acceptable_float_kw']}")

    print()
    print(f"功率预算 E2E：通过 {_passed} 项，失败 {_failed} 项")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
