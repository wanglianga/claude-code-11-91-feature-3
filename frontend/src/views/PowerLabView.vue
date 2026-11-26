<template>
  <div class="stack">
    <!-- 说明 + 全局控制 -->
    <section class="panel">
      <h2>⚡ 站点级分时功率预算 · 两种分配方式回放台</h2>
      <p class="muted" style="margin:0 0 12px">
        每支枪都显示空闲，不等于变压器还能供应每辆车申请的功率——枪口额定之和不能当成站点余量。
        题面站点：变压器 <strong>180kW</strong>，P1/P2/P3 三支 CCS 兼容枪（单枪 120kW）；
        R1/R2/R3 均<strong>保证 ≥60kW、希望 90kW</strong>。
      </p>
      <div class="row" style="align-items:flex-end">
        <label class="field" style="margin-bottom:0;max-width:220px">
          <span>站点</span>
          <select v-model.number="siteId" @change="loadAll">
            <option v-for="s in sites" :key="s.id" :value="s.id">
              {{ s.name }}（变压器 {{ s.transformer_limit_kw }}kW）
            </option>
          </select>
        </label>
        <label class="field" style="margin-bottom:0;max-width:230px">
          <span>面板回放时刻 at</span>
          <input type="datetime-local" v-model="atTime" @change="boardRef?.load()" />
        </label>
        <button class="secondary" @click="refreshBoard">刷新面板</button>
        <button class="ghost" @click="resetDemo">↺ 重置为题面种子数据</button>
      </div>
      <div v-if="notice" :class="['notice', notice.kind]" style="margin-top:12px">{{ notice.text }}</div>
    </section>

    <!-- 一键回放 -->
    <section class="panel">
      <h2>① 并发接入 / 降额 / 限额修订 一键回放与核对</h2>
      <div class="chip-row">
        <button :disabled="running" @click="runExpected">
          回放 A：按期望 90kW 整段预留（并发 3 辆）
        </button>
        <button :disabled="running" class="secondary" @click="runGuaranteed">
          回放 B：仅保证 60kW + 动态分配（并发 3 辆 + 降额 + 紧急限额）
        </button>
        <button v-if="checks.length" class="ghost" @click="checks = []">清空核对</button>
      </div>
      <p class="muted" style="font-size:12px;margin:6px 0 0">
        两个回放都会先重置数据库，再用浏览器并发发出 3 个确认请求（后端站点咨询锁串行化，绝不超卖）。
      </p>

      <div v-if="checks.length" style="margin-top:12px">
        <h3 style="margin:0 0 8px">核对清单（实际执行结果）</h3>
        <table>
          <thead><tr><th style="width:42px">结果</th><th>核对项</th><th>期望</th><th>实际</th></tr></thead>
          <tbody>
            <tr v-for="(c, i) in checks" :key="i">
              <td>{{ c.pass ? '✅' : '❌' }}</td>
              <td>{{ c.label }}</td>
              <td class="muted">{{ c.expected }}</td>
              <td class="mono">{{ c.actual }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 实时分配面板 -->
    <section class="panel">
      <h2>② 运行面板：单枪限制 · 站点总预算 · 当前分配</h2>
      <PowerBoard ref="boardRef" :site-id="siteId" :at="atTime" />
    </section>

    <div class="grid two" style="grid-template-columns: 1fr 1fr">
      <!-- 车辆读数 + 限额修订 -->
      <section class="stack">
        <div class="panel">
          <h2>③ 车辆功率 / 电池降额回报</h2>
          <div class="row">
            <label class="field">
              <span>预约</span>
              <select v-model="reading.reservation_id">
                <option v-for="r in siteReservations" :key="r.id" :value="r.id">
                  {{ r.ref }} — {{ r.status }}（保证 {{ r.min_power_kw }}）
                </option>
              </select>
            </label>
            <label class="field" style="max-width:150px">
              <span>当前可接受 kW</span>
              <input type="number" v-model.number="reading.current_available_kw" />
            </label>
          </div>
          <div class="row">
            <label class="field" style="max-width:200px">
              <span>读数时刻</span>
              <input type="datetime-local" v-model="reading.reading_at" />
            </label>
            <label class="field" style="flex:0 0 auto;align-self:flex-end">
              <span>&nbsp;</span>
              <label style="display:flex;align-items:center;gap:6px;font-weight:600">
                <input type="checkbox" style="width:auto" v-model="reading.battery_derated" />
                电池限功率
              </label>
            </label>
          </div>
          <button @click="submitReading">提交读数并触发 5 分钟重分配</button>
          <button class="ghost" style="margin-left:8px" @click="quickDerateR1">
            快捷：首单 R1 降额到 60kW @18:05
          </button>
        </div>

        <div class="panel">
          <h2>④ 站点功率上限修订（重检所有重叠会话）</h2>
          <div class="row">
            <label class="field">
              <span>新变压器上限 kW</span>
              <input type="number" v-model.number="limit.transformer_limit_kw" />
            </label>
            <label class="field" style="max-width:210px">
              <span>重分配时刻</span>
              <input type="datetime-local" v-model="limit.reallocate_at" />
            </label>
          </div>
          <div class="row">
            <label class="field">
              <span>限额时段开始</span>
              <input type="datetime-local" v-model="limit.start_at" />
            </label>
            <label class="field">
              <span>结束</span>
              <input type="datetime-local" v-model="limit.end_at" />
            </label>
          </div>
          <label class="field">
            <span>原因</span>
            <input v-model="limit.reason" placeholder="如：外部电网紧急限额 / 计划检修方式" />
          </label>
          <label style="display:flex;align-items:center;gap:8px;font-weight:700;color:var(--bad);margin-bottom:10px">
            <input type="checkbox" style="width:auto" v-model="limit.emergency" />
            外部电网紧急限额（允许最低保证无法全满足，但必须把具体车辆送入运营处置）
          </label>
          <button @click="submitLimit">提交修订并重检</button>
          <div v-if="limitResult" style="margin-top:12px">
            <h3>重检结论</h3>
            <table>
              <thead><tr><th>订单</th><th>判定</th><th>缺口</th></tr></thead>
              <tbody>
                <tr v-for="x in limitResult.recheck" :key="x.reservation_id">
                  <td class="mono">{{ x.ref }}</td>
                  <td>
                    <span :class="['badge', x.decision === 'GUARANTEE_HELD' ? 'ok' : 'bad']">
                      {{ x.decision === 'GUARANTEE_HELD' ? '保证守住' : '需运营处置' }}
                    </span>
                  </td>
                  <td class="mono">{{ x.worst_shortfall_kw }} kW</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- 运营处置工单 + 司机变化记录 -->
      <section class="stack">
        <div class="panel">
          <h2>⑤ 运营处置：受影响车辆工单</h2>
          <button class="ghost small" @click="loadCases">刷新工单</button>
          <table v-if="cases.length" style="margin-top:8px">
            <thead><tr><th>#</th><th>车辆/标题</th><th>缺口</th><th>状态/处置</th></tr></thead>
            <tbody>
              <tr v-for="c in cases" :key="c.id">
                <td>{{ c.id }}</td>
                <td>
                  <strong>{{ c.title }}</strong>
                  <div class="muted" style="font-size:12px">{{ kindLabel(c.kind) }}</div>
                </td>
                <td class="mono bad">{{ c.shortfall_kw ?? '—' }}kW</td>
                <td>
                  <span :class="['badge', caseBadge(c.status)]">{{ caseLabel(c.status) }}</span>
                  <div v-if="c.status !== 'RESOLVED'" style="margin-top:6px">
                    <input v-model="resolveNotes[c.id]" placeholder="处置说明（限流/改派/补偿）" />
                    <button class="small secondary" style="margin-top:4px" @click="resolveCase(c)">
                      确认处置完成
                    </button>
                  </div>
                  <div v-else class="muted" style="font-size:12px">{{ c.resolution_note }}</div>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted">当前没有处置工单——所有车辆的明确最低保证均可用预算满足。</p>
        </div>

        <div class="panel">
          <h2>⑥ 司机收到的实时功率变化与调度依据</h2>
          <label class="field">
            <span>选择订单查看分配记录</span>
            <select v-model="logReservationId" @change="loadLogs">
              <option v-for="r in siteReservations" :key="r.id" :value="r.id">
                {{ r.ref }}（{{ r.allocation_logs.length }} 条）
              </option>
            </select>
          </label>
          <ul v-if="currentLogs.length" class="timeline">
            <li v-for="l in currentLogs" :key="l.id">
              <strong>{{ triggerLabel(l.trigger) }}</strong>
              <span class="muted mono"> · {{ fmt(l.created_at) }}</span>
              <div>
                <span v-if="l.previous_kw != null">{{ l.previous_kw }} → </span>
                <strong>{{ l.allocated_kw }}kW</strong>
                <span class="muted">
                  （保证 {{ l.guaranteed_min_kw }} / 期望 {{ l.expected_kw }}，
                  站点上限 {{ l.site_limit_kw }}，剩余 {{ l.site_remaining_kw }}）
                </span>
              </div>
              <div class="muted" style="font-size:12px">{{ l.basis }}</div>
            </li>
          </ul>
          <p v-else class="muted">开始充电并产生读数/重分配后，这里展示司机端收到的变化记录。</p>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import PowerBoard from '../components/PowerBoard.vue'
import {
  api, newIdempotencyKey, fmtTime,
  CASE_KIND_LABELS, CASE_STATUS_LABELS,
} from '../api.js'

const FRIDAY = '2026-11-27'
const sites = ref([])
const siteId = ref(2)
const atTime = ref(`${FRIDAY}T18:00`)
const boardRef = ref(null)
const notice = ref(null)
const running = ref(false)
const checks = ref([])

const reservations = ref([])
const cases = ref([])
const resolveNotes = reactive({})
const logReservationId = ref(null)
const limitResult = ref(null)

const reading = reactive({
  reservation_id: null,
  current_available_kw: 60,
  battery_derated: true,
  reading_at: `${FRIDAY}T18:05`,
})
const limit = reactive({
  transformer_limit_kw: 100,
  emergency: true,
  start_at: `${FRIDAY}T18:00`,
  end_at: `${FRIDAY}T19:00`,
  reallocate_at: `${FRIDAY}T18:10`,
  reason: '外部电网紧急限额回放',
})

const siteReservations = computed(() =>
  reservations.value.filter((r) => r.site_id === siteId.value)
)
const selectedReservation = computed(() =>
  reservations.value.find((r) => r.id === logReservationId.value)
)
const currentLogs = computed(() => selectedReservation.value?.allocation_logs || [])

onMounted(loadAll)

async function loadAll() {
  sites.value = await api.get('/api/sites')
  await loadReservations()
  await loadCases()
  if (!logReservationId.value && siteReservations.value[0]) {
    logReservationId.value = siteReservations.value[0].id
  }
}

async function loadReservations() {
  reservations.value = await api.get('/api/reservations')
  if (reading.reservation_id == null && siteReservations.value[0]) {
    reading.reservation_id = siteReservations.value[0].id
  }
}

async function loadCases() {
  cases.value = await api.get('/api/operations/ops-cases')
}

async function loadLogs() {
  // 列表已带 allocation_logs；强制刷新一次以拿到最新
  await loadReservations()
}

function refreshBoard() {
  boardRef.value?.load()
}

function fmt(t) { return fmtTime(t) }
function kindLabel(k) { return CASE_KIND_LABELS[k] || k }
function caseLabel(s) { return CASE_STATUS_LABELS[s] || s }
function caseBadge(s) {
  return s === 'RESOLVED' ? 'ok' : s === 'HANDLING' ? 'warn' : 'bad'
}
function triggerLabel(t) {
  return {
    SESSION_START: '开始充电初始分配',
    READING: '5 分钟读数重分配',
    BATTERY_DERATE: '电池降额重分配',
    REALLOCATE: '周期重分配',
    LIMIT_REVISION: '限额修订重分配',
  }[t] || t
}

function addCheck(label, expected, actual, pass) {
  checks.value.push({ label, expected, actual: String(actual), pass })
}

async function resetDemo() {
  notice.value = { kind: 'info', text: '正在重置演示数据…' }
  await api.post('/api/operations/demo-reset')
  checks.value = []
  limitResult.value = null
  await loadAll()
  atTime.value = `${FRIDAY}T18:00`
  notice.value = { kind: 'ok', text: '已重置为题面种子：180kW 站 + P1/P2/P3 + R1/R2/R3' }
  setTimeout(refreshBoard, 150)
}

// 并发确认：浏览器同时发出，后端咨询锁串行化
async function concurrentConfirm(policy) {
  const vehicles = [5, 6, 7]
  const results = await Promise.all(vehicles.map((vid, i) =>
    api.post('/api/reservations', {
      vehicle_id: vid,
      site_id: 2,
      start_at: `${FRIDAY}T18:00:00`,
      duration_minutes: 30,
      min_power_kw: 60,
      expected_power_kw: 90,
      allocation_policy: policy,
      mode: 'CONNECTOR',
      idempotency_key: newIdempotencyKey() + '-' + i,
    }).then(
      (data) => ({ ok: true, status: 200, data }),
      (err) => ({ ok: false, status: err.status, detail: err.detail }),
    )
  ))
  return results
}

async function runExpected() {
  running.value = true
  checks.value = []
  try {
    await api.post('/api/operations/demo-reset')
    await loadAll()
    const results = await concurrentConfirm('RESERVE_EXPECTED')
    const accepted = results.filter((r) => r.ok)
    const rejected = results.find((r) => !r.ok)
    addCheck('并发 3 辆期望 90kW：接受数量', '2 辆', `${accepted.length} 辆`, accepted.length === 2)
    addCheck('第三辆结果', '409 拒绝', rejected?.status, rejected?.status === 409)
    const budget = rejected?.detail?.budget
    addCheck('拒绝原因中的站点已预留/上限', '180 / 180kW',
      budget ? `${budget.tightest.hard_committed_kw} / 上限 ${budget.tightest.transformer_limit_kw}` : '无预算详情',
      !!budget && budget.tightest.hard_committed_kw === 180)
    addCheck('仍有空闲枪也不能超卖', '预算不足提示',
      rejected?.detail?.message?.includes('变压器') ? '已说明站点总预算' : '缺少说明',
      !!rejected?.detail?.message?.includes('变压器'))
    notice.value = { kind: 'ok', text: '期望预留回放完成：2 辆占满 180kW，第 3 辆被站点预算拒绝（枪仍空闲）。' }
    await loadAll()
    atTime.value = `${FRIDAY}T18:00`
    setTimeout(refreshBoard, 120)
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  } finally {
    running.value = false
  }
}

async function startAll(ids) {
  for (const id of ids) {
    await api.post(`/api/reservations/${id}/start`, { idempotency_key: newIdempotencyKey() })
  }
}

async function runGuaranteed() {
  running.value = true
  checks.value = []
  try {
    await api.post('/api/operations/demo-reset')
    await loadAll()

    const results = await concurrentConfirm('GUARANTEED_MIN')
    const accepted = results.filter((r) => r.ok)
    addCheck('并发 3 辆保证 60kW：接受数量', '3 辆', `${accepted.length} 辆`, accepted.length === 3)
    const guns = accepted.map((r) => r.data.connector_id).sort()
    addCheck('三支兼容枪各分配一支', 'P1/P2/P3 (id 5/6/7)', guns.join(','),
      JSON.stringify(guns) === JSON.stringify([5, 6, 7]))

    const ids = accepted.map((r) => r.data.id)
    await startAll(ids)
    await loadReservations()

    atTime.value = `${FRIDAY}T18:00`
    await boardRef.value?.load()
    const b0 = await api.get(`/api/sites/2/power-board?at=${FRIDAY}T18:00:00`)
    addCheck('三车最低保证合计', '180kW', `${b0.guaranteed_committed_kw}kW`,
      b0.guaranteed_committed_kw === 180)
    addCheck('每车当前分配', '均为 60kW',
      b0.sessions.map((s) => s.allocated_kw).join('/'),
      b0.sessions.every((s) => s.allocated_kw === 60))
    addCheck('实时剩余功率', '0kW（正好用满保证预算）',
      `${b0.power_remaining_now_kw}kW`, b0.power_remaining_now_kw === 0)

    // R1 降额到 60
    const r1id = ids[0]
    const rd = await api.post(`/api/reservations/${r1id}/power-reading`, {
      current_available_kw: 60,
      battery_derated: true,
      note: 'R1 BMS 电池限功率',
      reading_at: `${FRIDAY}T18:05:00`,
      idempotency_key: newIdempotencyKey(),
    })
    const r1row = rd.board.sessions.find((s) => s.reservation_id === r1id)
    addCheck('R1 降额后分配/缺口', '60kW / 缺口 0',
      `${r1row.allocated_kw}kW / 缺口 ${r1row.grid_shortfall_kw}`,
      r1row.allocated_kw === 60 && r1row.grid_shortfall_kw === 0)
    addCheck('R1 不被错误送入处置', '无工单',
      r1row.open_ops_case_id == null ? '无工单' : `工单#${r1row.open_ops_case_id}`,
      r1row.open_ops_case_id == null)

    // 紧急限额到 100
    const lim = await api.post('/api/operations/power-limits', {
      site_id: 2,
      start_at: `${FRIDAY}T18:00:00`,
      end_at: `${FRIDAY}T19:00:00`,
      transformer_limit_kw: 100,
      emergency: true,
      reason: '外部电网紧急限额回放',
      reallocate_at: `${FRIDAY}T18:10:00`,
      idempotency_key: newIdempotencyKey(),
    })
    const caseRids = lim.cases.map((c) => c.reservation_id)
    const shortfalls = lim.cases.map((c) => c.shortfall_kw).sort((a, b) => a - b)
    addCheck('紧急限额 100kW：处置工单数量', '2 辆', `${lim.cases.length} 辆`, lim.cases.length === 2)
    addCheck('缺口分别为 20 / 60kW', '20、60', shortfalls.join('、'),
      shortfalls.length === 2 && shortfalls[0] === 20 && shortfalls[1] === 60)
    addCheck('具体受影响车辆被定位', '2 个不同的真实订单',
      lim.recheck.filter((x) => x.decision === 'SHORTFALL_TO_OPS').map((x) => x.ref).join('、'),
      new Set(caseRids).size === 2 && caseRids.every((rid) => ids.includes(rid)))

    atTime.value = `${FRIDAY}T18:10`
    await loadAll()
    setTimeout(refreshBoard, 120)
    if (siteReservations.value[0]) logReservationId.value = r1id
    notice.value = { kind: 'ok', text: '最低保证回放完成：3 车接入、R1 降额无误报、紧急限额已把具体车辆送入运营处置。' }
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  } finally {
    running.value = false
  }
}

async function submitReading() {
  if (!reading.reservation_id) {
    notice.value = { kind: 'warn', text: '请先选择预约' }
    return
  }
  try {
    await api.post(`/api/reservations/${reading.reservation_id}/power-reading`, {
      current_available_kw: reading.current_available_kw,
      actual_power_kw: reading.current_available_kw,
      battery_derated: reading.battery_derated,
      note: '运营台代发读数',
      reading_at: reading.reading_at.length === 16 ? reading.reading_at + ':00' : reading.reading_at,
      idempotency_key: newIdempotencyKey(),
    })
    notice.value = { kind: 'ok', text: '读数已提交，已按最新预算重分配。' }
    await loadReservations()
    refreshBoard()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function quickDerateR1() {
  const r1 = siteReservations.value.find((r) => r.vehicle_id === 5) || siteReservations.value[0]
  if (!r1) {
    notice.value = { kind: 'warn', text: '当前站点没有预约，请先跑回放 B' }
    return
  }
  reading.reservation_id = r1.id
  reading.current_available_kw = 60
  reading.battery_derated = true
  reading.reading_at = `${FRIDAY}T18:05`
  await submitReading()
}

async function submitLimit() {
  try {
    const body = {
      site_id: siteId.value,
      transformer_limit_kw: limit.transformer_limit_kw,
      emergency: limit.emergency,
      reason: limit.reason,
      start_at: limit.start_at + ':00',
      end_at: limit.end_at + ':00',
      reallocate_at: (limit.reallocate_at || limit.start_at) + ':00',
      idempotency_key: newIdempotencyKey(),
    }
    limitResult.value = await api.post('/api/operations/power-limits', body)
    notice.value = limit.emergency
      ? { kind: 'warn', text: '紧急限额已生效，受影响车辆已生成运营处置工单。' }
      : { kind: 'ok', text: '限额修订已通过重检。' }
    await loadAll()
    refreshBoard()
  } catch (e) {
    // 非紧急且会击穿保证：后端 409 并返回 affected/recheck
    limitResult.value = e.detail || null
    notice.value = { kind: 'bad', text: '修订被拒绝：该限额会击穿最低保证；如属电网紧急请勾选紧急。' }
  }
}

async function resolveCase(c) {
  const note = (resolveNotes[c.id] || '').trim()
  if (!note) {
    notice.value = { kind: 'warn', text: '请先填写处置说明' }
    return
  }
  try {
    await api.post(`/api/operations/ops-cases/${c.id}/resolve`, {
      resolution_note: note,
      idempotency_key: newIdempotencyKey(),
    })
    notice.value = { kind: 'ok', text: `工单 #${c.id} 已处置关闭` }
    await loadCases()
    refreshBoard()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}
</script>
