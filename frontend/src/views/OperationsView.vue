<template>
  <div class="stack">
    <!-- 站点级功率预算与电网紧急限额处置 -->
    <section class="panel">
      <h2>⚡ 站点分时功率预算 · 当前分配与紧急处置
        <RouterLink to="/power-lab" class="small" style="float:right;font-weight:600">
          前往两种分配方式一键回放台 →
        </RouterLink>
      </h2>
      <div class="row" style="align-items:flex-end;margin-bottom:6px">
        <label class="field" style="margin-bottom:0;max-width:300px">
          <span>站点</span>
          <select v-model.number="powerSiteId" @change="powerBoard?.load()">
            <option v-for="s in sites" :key="s.id" :value="s.id">
              {{ s.name }}（变压器 {{ s.transformer_limit_kw }}kW / {{ policyLabel(s.allocation_policy) }}）
            </option>
          </select>
        </label>
        <label class="field" style="margin-bottom:0;max-width:230px">
          <span>面板时刻</span>
          <input type="datetime-local" v-model="powerAt" @change="powerBoard?.load()" />
        </label>
        <button class="secondary" style="max-width:140px" @click="powerBoard?.load(); loadOpsCases()">刷新</button>
      </div>
      <PowerBoard ref="powerBoard" :site-id="powerSiteId" :at="powerAt" />

      <h3>电网紧急限额 / 预算缺额处置工单（{{ opsCases.length }}）</h3>
      <table v-if="opsCases.length">
        <thead><tr><th>#</th><th>标题</th><th>缺口</th><th>状态</th><th>处置</th></tr></thead>
        <tbody>
          <tr v-for="c in opsCases" :key="c.id">
            <td>{{ c.id }}</td>
            <td>{{ c.title }}<div class="muted" style="font-size:12px">{{ caseKindLabel(c.kind) }}</div></td>
            <td class="mono bad">{{ c.shortfall_kw ?? '—' }}kW</td>
            <td><span :class="['badge', c.status === 'RESOLVED' ? 'ok' : 'bad']">{{ caseStatusLabel(c.status) }}</span></td>
            <td>
              <template v-if="c.status !== 'RESOLVED'">
                <input v-model="opsNotes[c.id]" placeholder="处置说明：限流/改派/补偿" />
                <button class="small secondary" style="margin-top:4px" @click="resolveOps(c)">确认处置</button>
              </template>
              <span v-else class="muted">{{ c.resolution_note }}</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">暂无功率类处置工单：所有活动会话的明确最低保证都在站点预算内。</p>
    </section>

  <div class="grid two">
    <!-- 左：维护操作 -->
    <section class="stack">
      <div class="panel">
        <h2>① 降额 / 停用维护窗口</h2>
        <label class="field">
          <span>枪口</span>
          <select v-model="mw.connector_id">
            <template v-for="s in sites" :key="s.id">
              <option v-for="c in s.connectors" :key="c.id" :value="c.id">
                {{ s.name }} · {{ c.code }}（{{ c.connector_type }} / {{ c.rated_power_kw }}kW{{ c.accessible ? ' / 无障碍' : '' }}）
              </option>
            </template>
          </select>
        </label>
        <div class="row">
          <label class="field">
            <span>类型</span>
            <select v-model="mw.kind">
              <option value="DERATE">降额（仍可用，功率下调）</option>
              <option value="OUTAGE">停用（窗口内不可用）</option>
            </select>
          </label>
          <label class="field" v-if="mw.kind === 'DERATE'">
            <span>降额后功率 kW</span>
            <input type="number" v-model.number="mw.effective_power_kw" />
          </label>
        </div>
        <div class="row">
          <label class="field">
            <span>生效开始</span>
            <input type="datetime-local" v-model="mw.start_at" />
          </label>
          <label class="field">
            <span>计划结束</span>
            <input type="datetime-local" v-model="mw.planned_end_at" />
          </label>
        </div>
        <label class="field">
          <span>原因</span>
          <input v-model="mw.reason" placeholder="如：模块高温降额 / 紧急检修" />
        </label>
        <button @click="openMaintenance">提交并评估受影响预约</button>

        <div v-if="notice" :class="['notice', notice.kind]" style="margin-top:12px">
          {{ notice.text }}
        </div>

        <div v-if="lastImpact" style="margin-top:14px">
          <h3>本次维护影响判定（按真实时间交集）</h3>
          <p class="muted" v-if="!lastImpact.impacts.length">
            没有与维护时段重叠的在途预约——已结束的预约不会被撤销。
          </p>
          <table v-else>
            <thead><tr><th>订单</th><th>交集</th><th>判定</th><th>原因</th></tr></thead>
            <tbody>
              <tr v-for="i in lastImpact.impacts" :key="i.reservation_id">
                <td class="mono">{{ i.ref }}<br />
                  <span class="badge info">{{ MODE_LABELS[i.mode] }}</span></td>
                <td class="mono">{{ fmt(i.overlap_start) }}–{{ fmt(i.overlap_end) }}</td>
                <td><span :class="['badge', impactBadge(i.decision)]">{{ IMPACT_LABELS[i.decision] }}</span></td>
                <td class="muted" style="max-width:320px">{{ i.reason }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <h2>② 恢复设备（不改写历史确认）</h2>
        <table v-if="activeWindows.length">
          <thead><tr><th>#</th><th>枪口</th><th>窗口</th><th>恢复时刻</th><th></th></tr></thead>
          <tbody>
            <tr v-for="w in activeWindows" :key="w.id">
              <td>{{ w.id }}</td>
              <td>{{ gunCode(w.connector_id) }} · {{ w.kind === 'DERATE' ? `降额 ${w.effective_power_kw}kW` : '停用' }}</td>
              <td class="mono">{{ fmt(w.start_at) }}–{{ fmt(w.planned_end_at) }}</td>
              <td><input type="datetime-local" v-model="recoverAt[w.id]" /></td>
              <td><button class="small secondary" @click="recover(w)">恢复</button></td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">当前没有生效中的维护窗口。</p>
      </div>
    </section>

    <!-- 右：预约与客服动作 -->
    <section class="stack">
      <div class="panel">
        <h2>③ 在途预约与维护状态</h2>
        <table>
          <thead>
            <tr><th>订单</th><th>状态</th><th>时间窗</th><th>枪/池</th><th>事件流</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in reservations" :key="r.id">
              <td class="mono">{{ r.ref }}<br /><span class="muted">≥{{ r.min_power_kw }}kW</span></td>
              <td><span :class="['badge', statusBadge(r.status)]">{{ STATUS_LABELS[r.status] }}</span></td>
              <td class="mono">{{ fmt(r.start_at) }}–{{ fmt(r.end_at) }}</td>
              <td>
                <span v-if="r.connector_id">{{ gunCode(r.connector_id) }}</span>
                <span v-else class="muted">池名额</span>
              </td>
              <td>
                <details>
                  <summary>{{ r.events.length }} 条</summary>
                  <ul class="timeline">
                    <li v-for="e in r.events" :key="e.id">
                      <strong>{{ e.event_type }}</strong>
                      <pre class="mono" style="white-space:pre-wrap">{{ pretty(e.detail) }}</pre>
                    </li>
                  </ul>
                </details>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h2>④ 客服处置（指向当时订单与设备状态）</h2>
        <div class="row">
          <label class="field">
            <span>预约</span>
            <select v-model="svc.reservation_id">
              <option v-for="r in reservations" :key="r.id" :value="r.id">
                {{ r.ref }} — {{ STATUS_LABELS[r.status] }}
              </option>
            </select>
          </label>
          <label class="field">
            <span>动作类型</span>
            <select v-model="svc.kind">
              <option value="ALTERNATIVE_CONNECTOR">替代枪口</option>
              <option value="FEE_ADJUSTMENT">费用调整（负=补偿）</option>
              <option value="NOTE">备注</option>
            </select>
          </label>
        </div>
        <div class="row">
          <label class="field" v-if="svc.kind === 'ALTERNATIVE_CONNECTOR'">
            <span>目标枪口</span>
            <select v-model="svc.target_connector_id">
              <template v-for="s in sites" :key="s.id">
                <option v-for="c in s.connectors" :key="c.id" :value="c.id">
                  {{ c.code }}（{{ c.connector_type }} / {{ c.rated_power_kw }}kW{{ c.accessible ? ' / 无障碍' : '' }}）
                </option>
              </template>
            </select>
          </label>
          <label class="field" v-if="svc.kind === 'FEE_ADJUSTMENT'">
            <span>金额</span>
            <input type="number" step="0.01" v-model.number="svc.amount" />
          </label>
        </div>
        <label class="field">
          <span>说明</span>
          <input v-model="svc.note" />
        </label>
        <button @click="submitService">提交客服动作</button>
        <p class="muted">
          动作会固化当时的订单状态与枪口维护快照；充电中的会话不能迁移，恢复设备也不会回滚历史。
        </p>
      </div>

      <div class="panel">
        <h2>⑤ 维护窗口台账</h2>
        <table>
          <thead><tr><th>#</th><th>枪口</th><th>类型</th><th>计划窗口</th><th>实际恢复</th><th>原因</th></tr></thead>
          <tbody>
            <tr v-for="w in windows" :key="w.id">
              <td>{{ w.id }}</td>
              <td>{{ gunCode(w.connector_id) }}</td>
              <td>{{ w.kind === 'DERATE' ? `降额 ${w.effective_power_kw}kW` : '停用' }}</td>
              <td class="mono">{{ fmt(w.start_at) }}–{{ fmt(w.planned_end_at) }}</td>
              <td class="mono">{{ w.ended_at ? fmt(w.ended_at) : '—' }}</td>
              <td class="muted">{{ w.reason }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, computed } from 'vue'
import {
  api, IMPACT_LABELS, MODE_LABELS, newIdempotencyKey, STATUS_LABELS,
  POLICY_LABELS, CASE_KIND_LABELS, CASE_STATUS_LABELS,
} from '../api.js'
import PowerBoard from '../components/PowerBoard.vue'

const sites = ref([])
const reservations = ref([])
const windows = ref([])
const lastImpact = ref(null)
const notice = ref(null)
const recoverAt = reactive({})

// 站点功率预算 / 紧急处置
const powerSiteId = ref(2)
const powerAt = ref('2026-11-27T18:10')
const powerBoard = ref(null)
const opsCases = ref([])
const opsNotes = reactive({})

function policyLabel(p) { return POLICY_LABELS[p] || p }
function caseKindLabel(k) { return CASE_KIND_LABELS[k] || k }
function caseStatusLabel(s) { return CASE_STATUS_LABELS[s] || s }

async function loadOpsCases() {
  opsCases.value = await api.get('/api/operations/ops-cases')
}

async function resolveOps(c) {
  const note = (opsNotes[c.id] || '').trim()
  if (!note) {
    notice.value = { kind: 'warn', text: '请先填写处置说明' }
    return
  }
  try {
    await api.post(`/api/operations/ops-cases/${c.id}/resolve`, {
      resolution_note: note,
      idempotency_key: newIdempotencyKey(),
    })
    notice.value = { kind: 'ok', text: `处置工单 #${c.id} 已关闭` }
    await loadOpsCases()
    powerBoard.value?.load()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

const mw = reactive({
  connector_id: 2,
  kind: 'DERATE',
  start_at: '2026-11-27T18:10',
  planned_end_at: '2026-11-27T19:00',
  effective_power_kw: 60,
  reason: '模块高温降额',
})
const svc = reactive({
  reservation_id: null,
  kind: 'ALTERNATIVE_CONNECTOR',
  target_connector_id: 3,
  amount: -20,
  note: '',
})

const activeWindows = computed(() => windows.value.filter((w) => !w.ended_at))

onMounted(loadAll)

async function loadAll() {
  sites.value = await api.get('/api/sites')
  reservations.value = await api.get('/api/reservations')
  windows.value = await api.get('/api/maintenance')
  await loadOpsCases()
  for (const w of windows.value) {
    if (!recoverAt[w.id]) recoverAt[w.id] = w.planned_end_at.slice(0, 16)
  }
  if (!svc.reservation_id && reservations.value.length) {
    svc.reservation_id = reservations.value[0].id
  }
}

async function openMaintenance() {
  notice.value = null
  try {
    const body = {
      connector_id: mw.connector_id,
      kind: mw.kind,
      start_at: mw.start_at + ':00',
      planned_end_at: mw.planned_end_at + ':00',
      effective_power_kw: mw.kind === 'DERATE' ? mw.effective_power_kw : null,
      reason: mw.reason,
      idempotency_key: newIdempotencyKey(),
    }
    lastImpact.value = await api.post('/api/operations/maintenance', body)
    notice.value = { kind: 'ok', text: `维护窗口 #${lastImpact.value.maintenance.id} 已生效，影响判定见下表` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function recover(w) {
  notice.value = null
  try {
    await api.post(`/api/operations/maintenance/${w.id}/recover`, {
      ended_at: (recoverAt[w.id] || w.planned_end_at.slice(0, 16)) + ':00',
      idempotency_key: newIdempotencyKey(),
    })
    notice.value = { kind: 'info', text: `窗口 #${w.id} 已恢复；历史确认快照与订单状态不会被自动改写` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function submitService() {
  notice.value = null
  try {
    const body = {
      reservation_id: svc.reservation_id,
      kind: svc.kind,
      target_connector_id: svc.kind === 'ALTERNATIVE_CONNECTOR' ? svc.target_connector_id : null,
      amount: svc.kind === 'FEE_ADJUSTMENT' ? svc.amount : null,
      note: svc.note,
      idempotency_key: newIdempotencyKey(),
    }
    const a = await api.post('/api/operations/service-actions', body)
    notice.value = { kind: 'ok', text: `客服动作 #${a.id} 已记录并固化当时设备状态` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

function gunCode(id) {
  for (const s of sites.value) {
    const c = s.connectors.find((x) => x.id === id)
    if (c) return c.code
  }
  return '#' + id
}
function fmt(t) { return t ? t.replace('T', ' ').slice(5, 16) : '' }
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
function impactBadge(d) {
  return {
    UNCHANGED: 'ok', NEEDS_RESELECTION: 'bad',
    MANUAL_CONTACT: 'warn', PROTECTED_CHARGING: 'warn',
  }[d] || 'muted'
}
</script>
