<template>
  <div class="grid two">
    <!-- 左：搜索与确认表单 -->
    <section class="panel">
      <h2>① 选择车辆与充电需求</h2>
      <label class="field">
        <span>车辆（接口 / 最大受电 / 无障碍）</span>
        <select v-model="form.vehicle_id" @change="onVehicleChange">
          <option v-for="v in vehicles" :key="v.id" :value="v.id">
            {{ v.name }} — {{ v.connector_type }} / {{ v.max_power_kw }}kW{{ v.accessible ? ' / 无障碍' : '' }}
          </option>
        </select>
      </label>
      <label class="field">
        <span>站点</span>
        <select v-model="form.site_id">
          <option v-for="s in sites" :key="s.id" :value="s.id">
            {{ s.name }} — {{ s.location }}
          </option>
        </select>
      </label>
      <div class="row">
        <label class="field">
          <span>开始时间</span>
          <input type="datetime-local" v-model="form.start_at" />
        </label>
        <label class="field">
          <span>停留（分钟）</span>
          <input type="number" min="5" v-model.number="form.duration_minutes" />
        </label>
      </div>
      <div class="row">
        <label class="field">
          <span>最低保证功率 kW</span>
          <input type="number" min="1" v-model.number="form.min_power_kw" />
        </label>
        <label class="field">
          <span>期望功率 kW（上浮目标）</span>
          <input type="number" min="1" v-model.number="form.expected_power_kw" />
        </label>
      </div>
      <div class="row">
        <label class="field">
          <span>站点功率预算方式</span>
          <select v-model="form.allocation_policy">
            <option value="GUARANTEED_MIN">仅保证最低值 · 5 分钟动态分配</option>
            <option value="RESERVE_EXPECTED">按期望功率整段预留</option>
          </select>
        </label>
        <label class="field">
          <span>预约粒度</span>
          <select v-model="form.mode">
            <option value="CONNECTOR">固定枪口（锁定枪号）</option>
            <option value="POOL">资源池（到站再分配）</option>
          </select>
        </label>
      </div>
      <div class="row">
        <button @click="search">搜索候选（不构成订单）</button>
        <button class="secondary" @click="confirmBook" :disabled="!searchResult">
          确认预约（事务重算容量）
        </button>
      </div>
      <p class="muted" style="margin-bottom: 0">
        当前幂等键：<span class="mono">{{ idemKey }}</span><br />
        同一键重复提交只会返回首次结果；点击「重放上次请求」可验证幂等。
      </p>
      <div style="margin-top:8px">
        <button class="ghost small" @click="replay">重放上次请求</button>
        <button class="ghost small" @click="rotateKey">换新幂等键</button>
      </div>

      <div v-if="notice" :class="['notice', notice.kind]" style="margin-top:12px">
        {{ notice.text }}
      </div>

      <div v-if="confirmedView" class="budget-explain">
        <h3 style="margin:0 0 6px">本单确认依据：单枪限制 × 站点总预算</h3>
        <p style="margin:0 0 8px">{{ confirmedView.promise }}</p>
        <div class="chip-row" style="margin:0 0 6px">
          <span class="badge info">单枪额定 {{ confirmedView.gunRated }}kW</span>
          <span class="badge info">变压器上限 {{ confirmedView.limit }}kW</span>
          <span class="badge ok">最低保证 {{ confirmedView.min }}kW</span>
          <span class="badge warn">期望 {{ confirmedView.expected }}kW</span>
          <span class="badge muted">{{ policyLabel(confirmedView.policy) }}</span>
        </div>
        <div v-for="(s, i) in confirmedView.segments" :key="i" class="seg">
          {{ fmt(s.start_at) }}–{{ fmt(s.end_at) }}：上限 {{ s.transformer_limit_kw }} /
          已保证 {{ s.guaranteed_committed_kw }} / 本单后硬占用 {{ s.hard_committed_after_kw }}kW
        </div>
      </div>
    </section>

    <!-- 右：候选与订单 -->
    <section class="stack">
      <div class="panel" v-if="searchResult">
        <h2>② 候选结果与限制原因</h2>

        <div v-if="searchResult.mode === 'POOL'" class="pool-hero">
          资源池路线：当前时间窗内
          <span class="big">{{ searchResult.confirmable_slots }}</span>
          个可同时履约的合格名额
          <span :class="['badge', searchResult.eligible ? 'ok' : 'bad']">
            {{ searchResult.eligible ? '可确认' : '无名额' }}
          </span>
          <div v-for="(s, i) in searchResult.window_segments" :key="i" class="seg">
            {{ fmt(s.start_at) }}–{{ fmt(s.end_at) }}：名额 {{ s.available_power_kw }}
            （{{ s.note }}）
          </div>
          <p class="muted" style="margin:8px 0 0">
            池模式确认后暂不指定枪号，按池内同时可履约枪数限量，不会把同一容量重复卖出。
          </p>
        </div>

        <div v-else :class="['notice', searchResult.eligible ? 'ok' : 'bad']">
          固定枪口路线：{{ searchResult.eligible ? '存在可锁定的合格枪口' : '没有可确认的合格枪口' }}
        </div>

        <div
          v-for="c in searchResult.connector_candidates"
          :key="c.connector_id"
          :class="['candidate', c.eligible ? 'eligible' : '']"
        >
          <div class="head">
            <span class="code">枪口 {{ c.connector_code }}</span>
            <span :class="['badge', c.eligible ? 'ok' : 'bad']">
              {{ c.eligible ? '合格' : '不合格' }}
            </span>
            <span v-if="c.locked_by_ref" class="badge warn">
              被 {{ c.locked_by_ref }} 占用
            </span>
            <span class="muted">窗内最差有效功率 {{ c.effective_power_kw }}kW</span>
          </div>
          <div>
            <span v-for="(s, i) in c.segments" :key="i" class="seg">
              {{ fmt(s.start_at) }}–{{ fmt(s.end_at) }}：{{ s.available_power_kw }}kW
            </span>
          </div>
          <ul v-if="c.reasons.length" class="reasons">
            <li v-for="(r, i) in c.reasons" :key="i">{{ r }}</li>
          </ul>
        </div>
      </div>

      <div class="panel">
        <h2>③ 我的预约与状态操作</h2>
        <table v-if="reservations.length">
          <thead>
            <tr>
              <th>订单</th><th>车辆/粒度</th><th>时间窗</th><th>枪口</th>
              <th>功率：保证/期望/当前</th><th>状态</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in reservations" :key="r.id">
              <td>
                <strong class="mono">{{ r.ref }}</strong><br />
                <span class="muted">≥{{ r.min_power_kw }}kW</span>
              </td>
              <td>
                V{{ r.vehicle_id }}<br />
                <span class="badge info">{{ MODE_LABELS[r.mode] }}</span>
              </td>
              <td class="mono">{{ fmt(r.start_at) }}–{{ fmt(r.end_at) }}</td>
              <td>
                <span v-if="r.connector_id">{{ gunCode(r.connector_id) }}</span>
                <span v-else class="muted">到站分配</span>
                <details>
                  <summary>确认快照（含站点预算解释）</summary>
                  <pre class="mono" style="white-space:pre-wrap">{{ pretty(r.confirmation_snapshot) }}</pre>
                </details>
              </td>
              <td>
                <div style="font-size:12px;line-height:1.7">
                  <div>保证 <strong>{{ r.min_power_kw }}</strong> kW</div>
                  <div class="muted">期望 {{ r.expected_power_kw }} kW</div>
                  <div>
                    当前分配
                    <strong :class="r.allocated_power_kw != null ? 'ok-text' : 'muted'">
                      {{ r.allocated_power_kw ?? '待调度' }}
                    </strong>
                  </div>
                  <div v-if="r.battery_derated">
                    <span class="badge warn">电池限功率</span>
                    <span class="muted">车报 {{ r.current_available_power_kw }}kW</span>
                  </div>
                  <div v-if="r.allocation_logs && r.allocation_logs.length">
                    <details>
                      <summary>功率变化（{{ r.allocation_logs.length }}）</summary>
                      <ul class="timeline">
                        <li v-for="l in r.allocation_logs" :key="l.id">
                          <strong>{{ l.previous_kw != null ? l.previous_kw + ' → ' : '' }}{{ l.allocated_kw }}kW</strong>
                          <div class="muted" style="font-size:12px">{{ l.basis }}</div>
                        </li>
                      </ul>
                    </details>
                  </div>
                </div>
              </td>
              <td><span :class="['badge', statusBadge(r.status)]">{{ STATUS_LABELS[r.status] }}</span></td>
              <td>
                <div class="chip-row">
                  <button v-if="r.status === 'CONFIRMED'" class="small" @click="transition(r, 'arrive')">到站</button>
                  <button v-if="['CONFIRMED','ARRIVED'].includes(r.status)" class="small" @click="transition(r, 'start')">开始充电</button>
                  <button v-if="r.status === 'CONFIRMED'" class="small ghost" @click="transition(r, 'cancel')">取消并释放</button>
                  <button v-if="r.status === 'CONFIRMED'" class="small ghost" @click="transition(r, 'no-show')">标记迟到</button>
                  <button v-if="r.status === 'CHARGING'" class="small secondary" @click="transition(r, 'complete')">完成充电</button>
                  <button v-if="r.status === 'CHARGING'" class="small ghost" @click="reportPower(r, 90, false)">报读数 90kW</button>
                  <button v-if="r.status === 'CHARGING'" class="small ghost" @click="reportPower(r, 60, true)">报电池降额 60kW</button>
                </div>
                <details>
                  <summary>事件流（{{ r.events.length }}）</summary>
                  <ul class="timeline">
                    <li v-for="e in r.events" :key="e.id">
                      <strong>{{ e.event_type }}</strong>
                      <div class="muted mono">{{ fmt(e.created_at) }}</div>
                      <pre class="mono" style="white-space:pre-wrap;margin:2px 0 0">{{ pretty(e.detail) }}</pre>
                    </li>
                  </ul>
                </details>
                <details v-if="r.service_actions.length">
                  <summary>客服动作（{{ r.service_actions.length }}）</summary>
                  <ul class="timeline">
                    <li v-for="a in r.service_actions" :key="a.id">
                      <strong>{{ a.kind }}</strong>
                      <div class="muted">订单状态：{{ a.order_status_at_action }}</div>
                      <pre class="mono" style="white-space:pre-wrap">{{ pretty(a.connector_state_snapshot) }}</pre>
                    </li>
                  </ul>
                </details>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">暂无预约。</p>
      </div>
    </section>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { api, MODE_LABELS, newIdempotencyKey, POLICY_LABELS, STATUS_LABELS } from '../api.js'

const vehicles = ref([])
const sites = ref([])
const reservations = ref([])
const searchResult = ref(null)
const notice = ref(null)
const confirmedView = ref(null)
const idemKey = ref(newIdempotencyKey())
let lastRequest = null

function policyLabel(p) { return POLICY_LABELS[p] || p }

const form = reactive({
  vehicle_id: 1,
  site_id: 1,
  start_at: '2026-11-27T18:00',
  duration_minutes: 10,
  min_power_kw: 90,
  expected_power_kw: 90,
  allocation_policy: 'GUARANTEED_MIN',
  mode: 'CONNECTOR',
})

onMounted(async () => {
  vehicles.value = await api.get('/api/vehicles')
  sites.value = await api.get('/api/sites')
  await loadReservations()
})

function onVehicleChange() {
  const v = vehicles.value.find((x) => x.id === form.vehicle_id)
  if (v && Number(form.min_power_kw) > Number(v.max_power_kw)) {
    form.min_power_kw = Number(v.max_power_kw)
  }
}

function payload() {
  return {
    vehicle_id: form.vehicle_id,
    site_id: form.site_id,
    start_at: form.start_at + ':00',
    duration_minutes: form.duration_minutes,
    min_power_kw: form.min_power_kw,
    expected_power_kw: form.expected_power_kw,
    allocation_policy: form.allocation_policy,
    mode: form.mode,
  }
}

async function search() {
  notice.value = null
  try {
    searchResult.value = await api.post('/api/search', payload())
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function confirmBook() {
  notice.value = null
  const body = { ...payload(), idempotency_key: idemKey.value }
  lastRequest = body
  try {
    const r = await api.post('/api/reservations', body)
    let snap = {}
    try { snap = JSON.parse(r.confirmation_snapshot) } catch { /* ignore */ }
    confirmedView.value = {
      promise: snap.promise || '',
      gunRated: snap.connector_rated_power_kw ?? '—',
      limit: snap.site_transformer_limit_kw ?? '—',
      min: r.min_power_kw,
      expected: r.expected_power_kw,
      policy: r.allocation_policy,
      segments: snap.power_budget?.segments || [],
    }
    notice.value = {
      kind: 'ok',
      text: r.mode === 'POOL'
        ? `已确认资源池名额 ${r.ref}（暂不指定枪号，到站分配）`
        : `已确认并锁定枪口 ${gunCode(r.connector_id)}，订单 ${r.ref}`,
    }
    searchResult.value = null
    await loadReservations()
  } catch (e) {
    confirmedView.value = null
    notice.value = { kind: 'bad', text: '确认被拒绝：' + e.message }
  }
}

async function replay() {
  if (!lastRequest) {
    notice.value = { kind: 'warn', text: '还没有已提交的确认请求' }
    return
  }
  notice.value = null
  try {
    const r = await api.post('/api/reservations', lastRequest)
    notice.value = { kind: 'info', text: `幂等重放返回原订单 ${r.ref}，未产生新订单` }
    await loadReservations()
  } catch (e) {
    notice.value = { kind: 'bad', text: '幂等重放返回首次的失败结果：' + e.message }
  }
}

function rotateKey() {
  idemKey.value = newIdempotencyKey()
}

async function transition(r, action) {
  notice.value = null
  try {
    await api.post(`/api/reservations/${r.id}/${action}`, {
      idempotency_key: newIdempotencyKey(),
    })
    await loadReservations()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function reportPower(r, kw, derated) {
  notice.value = null
  try {
    await api.post(`/api/reservations/${r.id}/power-reading`, {
      current_available_kw: kw,
      actual_power_kw: kw,
      battery_derated: derated,
      note: derated ? '司机端上报电池限功率' : '司机端周期读数',
      idempotency_key: newIdempotencyKey(),
    })
    notice.value = {
      kind: 'ok',
      text: derated
        ? `已上报电池降额（最多接受 ${kw}kW），释放的预算可分配给其他会话`
        : `已上报当前可接受功率 ${kw}kW，并完成动态重分配`,
    }
    await loadReservations()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function loadReservations() {
  reservations.value = await api.get('/api/reservations')
}

function gunCode(id) {
  for (const s of sites.value) {
    const c = s.connectors.find((x) => x.id === id)
    if (c) return c.code
  }
  return '#' + id
}
function fmt(t) {
  return t ? t.replace('T', ' ').slice(5, 16) : ''
}
function pretty(s) {
  try { return JSON.stringify(JSON.parse(s), null, 2) } catch { return s }
}
function statusBadge(s) {
  return {
    CONFIRMED: 'ok', ARRIVED: 'info', CHARGING: 'warn', COMPLETED: 'muted',
    CANCELLED: 'muted', NO_SHOW: 'muted',
    NEEDS_RESELECTION: 'bad', MANUAL_CONTACT: 'bad',
  }[s] || 'muted'
}
</script>
