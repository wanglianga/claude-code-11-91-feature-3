# 充电预约演示系统（Vue 3 + FastAPI + PostgreSQL）

司机按「站点 + 时间窗 + 停留时长 + 最低功率」预约充电，运营方维护枪口并评估对既有预约的影响。
系统同时支持两种预约粒度，并以同一套容量数据给出可核验结论：

- **固定枪口（CONNECTOR）**：确认时锁定一支兼容枪口，位置承诺直观；枪口故障时需迁移订单。
- **资源池（POOL）**：只承诺一个满足插头/功率/无障碍条件的站点名额，到站再分配枪口；
  以「池内同时可履约的枪口数」限制订单，绝不会因为两支枪各自「空闲」就把同一份容量卖两次。
- **站点级分时功率预算（本题新增）**：每支枪都空闲，不等于配电变压器还能供应每辆车申请的功率。
  每个时段记录变压器上限、已确认车辆的最低保证功率合计与可接受上浮空间，确认与运行面板同时解释
  **单枪限制 + 站点总预算 + 当前分配**。支持两种分配方式的并发回放：
  - `RESERVE_EXPECTED`：按预约期望功率整段预留，稳定可预期，但电池提前降功率空出的额度不能给后续车辆；
  - `GUARANTEED_MIN`：只硬承诺最低值，剩余预算按车辆约每 5 分钟的实时受电读数用 max-min 水位法重分配，
    电池限功率后释放的额度可被其他会话使用，同站可接入数量更高。
  - 限额修订重检所有重叠会话：非紧急修订不得击穿任何明确承诺的最低值（否则 409 拒绝）；
    外部电网紧急限额使最低保证无法全满足时，把**具体受影响车辆与缺口**写入运营处置工单，面板进入紧急态，
    绝不显示为正常。

## 一、目录结构

```
.
├── docker-compose.yml          # 一键启动 db + backend + frontend
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── e2e_check.sh            # 原题面场景手工 E2E（16 项检查）
│   ├── e2e_power_check.py      # 本题功率预算真实 HTTP 回放（31 项检查，含 3 并发）
│   ├── app/
│   │   ├── main.py             # FastAPI 入口（含 /docs Swagger UI）
│   │   ├── models.py           # 车辆/站点/枪口/维护/预约/事件/客服/功率预算/读数/分配记录/处置工单/幂等
│   │   ├── enums.py
│   │   ├── schemas.py
│   │   ├── deps.py             # 幂等端点（事务咨询锁 + 首次响应留存与回放）
│   │   ├── seed.py             # 东站 A/B/C/D + 临港 P3 180kW 三枪 + R1/R2/R3
│   │   ├── bootstrap.py        # 容器启动：建表 + 幂等补列，空库才灌种子（可重复执行）
│   │   ├── services/
│   │   │   ├── capacity.py     # 时间切片容量引擎（两种粒度共用，含确定枪口排序）
│   │   │   ├── power.py        # 站点分时功率预算：确认校验/max-min 动态分配/降额释放/限额修订/紧急处置
│   │   │   ├── reservation.py  # 确认/取消/迟到/到站/开始充电 状态机
│   │   │   └── maintenance.py  # 降额/停用/恢复 + 影响判定 + 客服动作
│   │   └── routers/            # resources / search / reservations / operations / power
│   └── tests/test_scenarios.py # 原 20 个 pytest 场景用例
│       └── test_power_budget.py# 本题 14 个功率预算用例（并发/降额释放/限额/紧急处置）
└── frontend/
    ├── Dockerfile              # 多阶段构建：vite build → nginx 静态托管 + API 代理
    ├── nginx.conf
    └── src/
        ├── views/DriverView.vue       # 司机端（含期望/策略、当前分配、快捷功率上报）
        ├── views/OperationsView.vue   # 运营端（含站点预算面板与紧急工单处置）
        ├── views/PowerLabView.vue     # 本题：两种分配方式一键并发回放 + 核对清单
        └── components/PowerBoard.vue  # 单枪限制/站点总预算/每车当前分配条形面板
```

## 二、一键启动（Docker，推荐）

前置：本机已安装 Docker 与 Docker Compose。

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 |
| --- | --- |
| 前端（司机端/运营端两个页签） | http://localhost:8080 |
| 后端 API | http://localhost:8000 |
| Swagger UI（接口文档） | http://localhost:8000/docs |
| PostgreSQL | localhost:5432，库/用户/密码均为 `ccdev` |

后端容器启动时会自动建表，并**仅在空库时**灌入样例数据；重复重启不会清空已有数据。
需要恢复初始演示数据时：

```bash
docker compose exec backend python -m app.seed
```

停止并清理数据卷：

```bash
docker compose down -v
```

## 三、本地开发启动（不使用 Docker）

容器内已预装 Python 3.12 / Node 22 / PostgreSQL 18，下面的命令也适用于普通开发机
（自行准备一个可连接的 PostgreSQL 即可）。

### 1. 后端

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 指向你的 PostgreSQL，默认 postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev
export DATABASE_URL="postgresql+psycopg://用户:密码@主机:5432/库名"
.venv/bin/python -m app.seed           # 重置并灌入样例数据
.venv/bin/uvicorn app.main:app --reload --port 8000
```

### 2. 前端

```bash
cd frontend
npm install
npm run dev      # http://localhost:8080，/api 已代理到 127.0.0.1:8000
```

## 四、测试数据与「账号」说明

本演示系统不设登录认证（无账号体系），打开页面即可操作，预置数据如下，
相当于固定的测试入口：

- **站点**：东站充电站（id=1），四支枪口
  - **A**：CCS / 120kW / 无障碍 —— 已被有效预约 `R-SEEDA01` 在周五 18:00–18:30 占用
  - **B**：CCS / 120kW / 无障碍 —— 18:00–18:10 唯一剩余的 90kW 合格容量
  - **C**：CCS / **60kW** / 普通车位 —— 功率不足
  - **D**：**CHAdeMO** / 120kW —— 与 CCS 车辆接口不兼容
- **车辆**：
  - V1：CCS / 120kW（持有 A 枪预约的车主）
  - V2：CCS / **最大受电仅 60kW**
  - V3：CCS / 100kW / **有无障碍接入要求**
  - V4：CCS / 150kW（发起 90kW 请求的第二位车主）
- **功率预算题面场景（站点 id=2，「临港 P3 功率预算示范站」）**
  - 配电变压器上限 **180kW**；P1/P2/P3 三支 **CCS / 120kW** 枪，对 R 系车全部兼容；
  - **R1 / R2 / R3（车辆 id=5/6/7）**：CCS / 120kW，均**保证最低 60kW、希望达到 90kW**；
  - 已播种一条 `2026-11-27 17:30–21:00`、180kW 的分时限额记录；
  - 关键结论（可用「功率预算回放」页一键复现）：
    - 期望预留：R1+R2 各按 90kW 占满 180kW，**R3 无法确认**（此时 P3 枪仍显示空闲）；
    - 最低保证：R1/R2/R3 各按 60kW 保证接入，**3 辆全部确认**，保证合计 180kW、实时剩余 0；
    - R1 随后报电池限功率、最多接受 60kW：分配仍为 60kW、**缺口 0、不产生处置工单**（不误报缺额）；
    - 紧急限额修订到 100kW：按确认先后保障，后两辆车分别出现 20/60kW 缺口并生成具体运营工单。
- 所有演示时间固定在 **2026-11-27（周五）**，前端表单已预填 18:00。
- 系统无登录/账号体系；上述车辆与站点就是固定测试入口。运营/回放页提供
  `POST /api/operations/demo-reset` 一键重建题面种子，方便反复回放。

## 五、本题验证方式（均已实际执行）

> 以下结论来自本次在开发容器内的真实执行：Python 3.12 venv + 本机 PostgreSQL 18
> （127.0.0.1:5432/ccdev）+ Node 22。当前环境**没有 Docker 守护进程**，因此
> `docker compose up --build` 未在本环境实际跑过；Dockerfile/compose 仅作一键启动交付保留，
> 其等价的本地启动路径与产物构建（`npm run build`）均已实际验证通过。

### 0. 一键重置题面数据并启动

```bash
cd backend
.venv/bin/python -m app.seed
.venv/bin/uvicorn app.main:app --port 8000
```

或在已运行的 API 上 `POST /api/operations/demo-reset`（重建并灌回种子，可重复调用）。

### 1. 后端单元/场景测试：34 项全部通过

```bash
cd backend
.venv/bin/python -m pytest tests/ -q
# 结果：34 passed
```

其中原 `tests/test_scenarios.py` **20 项**全部保持通过（候选与限制原因、两种粒度并发争抢、
幂等成功/失败回放、维护真实交集、池到站分配、充电保护、客服快照、无障碍等，无回归）；
本题新增 `tests/test_power_budget.py` **14 项**，覆盖：

- 期望预留：2 成 1 拒、**真 3 线程并发恰好 2 成 1 拒**、降额也不释放整段期望额度；
- 最低保证：3 成 + 第 4 辆拒、**真 3 线程并发 3 成全上 P1/P2/P3**；
- 面板的上限/保证合计/已分配/实时剩余/每车 60kW/单枪上限 120kW；
- R1 电池降额到 60kW **缺口为 0、无工单**且司机收到“不存在缺额”的记录；
- 降额释放的上浮额度被另一会话 90→120kW 吃掉；
- 取消释放保证额度后新车可确认；
- 限额修订重检 3 笔重叠会话、非紧急击穿保证被 409 拒绝、
  紧急 100kW 精确定位 20/60kW 缺口车辆并建工单、工单可处置关闭；
- 分时预算接口的上限/保证合计/可上浮值（180/60/120）。

> 测试夹具在每个用例 drop/create 表后 `engine.dispose()`，避免 psycopg 连接池复用
> 旧表结构的预编译计划（`cached plan must not change result type`）。

### 2. 原题面场景 E2E：16 项全部通过（真实 HTTP）

先重置数据并启动后端，然后：

```bash
cd backend
.venv/bin/python -m app.seed
.venv/bin/uvicorn app.main:app --port 8000 &
bash e2e_check.sh
# 结果：通过 16 项，失败 0 项
```

> 若 shell 报 `\r` / `invalid option`，是脚本被按 CRLF 检出，执行一次
> `sed -i 's/\r$//' e2e_check.sh` 即可。

### 2b. 本题功率预算 E2E：31 项全部通过（真实 HTTP + 真并发）

```bash
cd backend
.venv/bin/python e2e_power_check.py
# 默认访问 http://127.0.0.1:8000，也可传入 BASE 参数
# 结果：功率预算 E2E：通过 31 项，失败 0 项
```

该脚本会先调用 `demo-reset`，再用线程池对同一时刻**真实并发 3 个 HTTP 确认**，逐项核对：
期望模式 `[200,200,409]` 且拒绝来自 180kW 站点预算；保证模式 3 成全上 P1/P2/P3、
保证/已分配 180、剩余 0、每车 60；R1 降额 60 后缺口 0、无工单、司机记录含“不存在缺额”；
紧急限额 100kW 产生 `[20,60]kW` 缺口与 2 张具体车辆工单、面板紧急态、工单可关闭；
非紧急击穿修订 409；分时预算段 180/60/可上浮 120。

### 3. 前端构建与手工验证

```bash
cd frontend
npm install --registry=https://registry.npmmirror.com
npm run build     # 已实际执行：✓ built，无编译错误
npm run dev       # http://localhost:8080，/api 代理到 127.0.0.1:8000
```

打开 http://localhost:8080 ：

- 新增页签 **「功率预算回放」**：选择分配方式后点“一键回放”，页面真实调用后端完成
  并发确认/开始充电/R1 降额/紧急限额，并展示 ✅/❌ 核对清单；下方 PowerBoard 同时给出
  变压器上限、保证合计、保证余量、当前已分配、实时剩余、电网缺口，以及每车的
  **枪号+单枪上限、最低保证、期望、车报可接受功率、分配条（含保证刻度）、工单状态**；
  同页可提交读数、修订限额、处置工单、查看司机功率变化时间线。
- **司机端**：表单新增“期望功率”“站点功率预算方式”；订单表新增
  “保证/期望/当前分配”列、电池限功率标记、功率变化记录，并可对充电中订单快捷上报
  “读数 90kW / 电池降额 60kW”；确认快照内含完整站点预算解释。搜索仍逐枪展示兼容性与限制原因。
- **运营端**：顶部新增站点功率面板与紧急工单处置队列；原降额/停用/客服动作功能不变。

## 六、关键设计与约束的落实

- **候选搜索不构成订单**：`POST /api/search` 为纯只读；真正占用容量只发生在确认事务里。
- **事务内重算 + 并发安全**：确认先取站点级 `pg_advisory_xact_lock`，
  在同一事务内重新计算所有重叠切片后落单；并发请求被串行化，同一份能力不可能卖两次。
- **池容量不重复售卖**：每个时间切片统计「合格且未锁定的枪数 − 在途池预约数」，
  全程取最小值作为可确认名额。
- **幂等**：每个写操作携带 `idempotency_key`；首次请求（含 409 业务失败）的响应码与
  响应体被持久化，重复提交原样返回，同键不同参数返回 409。
- **状态机**：`CONFIRMED → ARRIVED → CHARGING → COMPLETED`，
  以及 `CANCELLED / NO_SHOW`（释放容量）、`NEEDS_RESELECTION / MANUAL_CONTACT`
  （维护影响）；非法转换返回 409。池模式在「到站」时才分配具体枪口。
- **维护按真实交集判定**：维护窗口与预约按半开区间求交集，交集为空则完全不影响；
  整晚订单遇到中途降额时，已履约区间与待履约区间在原因中分别说明。
  仍满足车辆需求的订单继续履约，不满足的进入改选/人工联系。
- **充电会话保护**：`CHARGING` 订单不能被取消、不能被客服替代枪口流程迁移，
  维护只会给出 `PROTECTED_CHARGING` 判定。
- **历史不可改写**：确认时写入承诺快照（枪号/功率/池名额），之后降额、恢复、
  迁移都不会修改它；客服动作固化当时的订单状态与原枪/目标枪设备快照；
  设备恢复只结束维护窗口并追加 `DEVICE_RECOVERED` 事件，不自动回滚订单状态。
- **站点功率是独立于枪口的第二道容量**：确认时在站点咨询锁内调用
  `assert_confirm_budget`，按分时切片校验变压器总预算，枪口兼容性（搜索/候选）结论不变。
  - `RESERVE_EXPECTED` 的硬占用 = 每单期望功率；`GUARANTEED_MIN` 的硬占用 = 明确最低保证。
  - 实时分配先落各车“最低保证与车辆 BMS 上限的较小值”，再把剩余变压器预算按
    max-min 水位法分配为上浮，且只分给 `CHARGING` 会话；未开始的预约只预留保证额度。
  - 车辆读数（`PowerReading`，可显式传 `reading_at` 便于回放）会压低其受电上限，
    腾出的预算在下一次重分配中流向仍有上浮空间的会话或新接入会话。
  - “缺额”只统计**电网侧**导致的 `保证值−可分配值`；车辆自身 BMS 已不能接受保证值时
    标记 `vehicle_limited` 但不算站点缺额——所以 R1 只接受 60kW（=其保证值）时不会被误报。
  - 限额修订重算整个时段：非紧急且会击穿最低保证的修订直接 409 并返回 affected；
    `emergency=true`（外部电网紧急限额）才落限额，并按确认先后确定性削减，
    为每辆具体受影响车创建 `OpsCase`，面板 `emergency=true` 红色呈现，运营填写说明后关闭。
- **司机变化可追溯**：每次分配变化（以及目标车“结果不变但说明无缺额”）写
  `PowerAllocationLog`（前后值、保证/期望、当时站点上限/已承诺/剩余与中文调度依据），
  同时追加订单事件，司机端订单与回放页时间线均可查。

### 本题新增/使用的主要 API

| 方法 & 路径 | 作用 |
| --- | --- |
| `POST /api/reservations` | 扩展 `expected_power_kw`、`allocation_policy`；确认内增加站点分时预算校验 |
| `GET /api/sites/{id}/power-budget?start_at=&duration_minutes=` | 分时预算段：变压器上限/最低保证合计/期望预留合计/硬占用/可上浮值 |
| `GET /api/sites/{id}/power-board?at=` | 某时刻运行面板：总预算、保证合计、实时剩余、每车枪上限/保证/期望/车报/分配/缺口/工单 |
| `POST /api/reservations/{id}/power-reading` | 开始充电后上报当前可接受功率/实际功率/电池降额，触发重分配（幂等） |
| `POST /api/sites/{id}/reallocate` | 按指定时刻做一次 5 分钟周期重分配（幂等） |
| `POST /api/sites/{id}/policy` | 切换站点默认预算策略/变压器基准上限 |
| `POST /api/operations/power-limits` | 分时限额修订 + 全部重叠会话重检；紧急时生成处置工单（幂等） |
| `GET /api/operations/ops-cases[?status=OPEN]` | 运营处置工单队列 |
| `POST /api/operations/ops-cases/{cid}/resolve` | 填写处置说明并关闭工单（幂等） |
| `POST /api/operations/demo-reset` | 仅演示/评测库：重建并灌回题面种子 |

完整 OpenAPI 文档见运行中的 `http://localhost:8000/docs`。
