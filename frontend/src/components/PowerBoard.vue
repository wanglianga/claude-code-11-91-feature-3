<template>
  <div class="power-board">
    <div v-if="board" class="stats">
      <div class="stat">
        <div class="stat-k">变压器上限</div>
        <div class="stat-v">{{ board.transformer_limit_kw }} kW</div>
      </div>
      <div class="stat">
        <div class="stat-k">已确认最低保证合计</div>
        <div class="stat-v">{{ board.guaranteed_committed_kw }} kW</div>
      </div>
      <div class="stat">
        <div class="stat-k">保证预算余量</div>
        <div :class="['stat-v', board.budget_headroom_kw < 0 ? 'bad' : 'ok']">
          {{ board.budget_headroom_kw }} kW
        </div>
      </div>
      <div class="stat">
        <div class="stat-k">当前已分配</div>
        <div class="stat-v">{{ board.currently_allocated_kw }} kW</div>
      </div>
      <div class="stat">
        <div class="stat-k">实时剩余功率</div>
        <div :class="['stat-v', board.power_remaining_now_kw <= 0.01 ? 'warn' : 'ok']">
          {{ board.power_remaining_now_kw }} kW
        </div>
      </div>
      <div class="stat">
        <div class="stat-k">电网缺口</div>
        <div :class="['stat-v', board.grid_shortfall_total_kw > 0 ? 'bad' : 'ok']">
          {{ board.grid_shortfall_total_kw }} kW
        </div>
      </div>
    </div>

    <div v-if="board && board.emergency" class="notice bad" style="margin:10px 0">
      ⚠ 电网紧急限额下最低保证无法全部满足：下表红色车辆已生成运营处置工单，系统不显示为正常。
    </div>

    <table v-if="board && board.sessions.length" style="margin-top:10px">
      <thead>
        <tr>
          <th>订单/车辆</th><th>枪（单枪上限）</th><th>状态</th>
          <th>最低保证</th><th>期望</th><th>车报当前可接受</th>
          <th style="min-width:170px">当前分配 / 站点上限</th><th>处置</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="s in board.sessions" :key="s.reservation_id"
            :style="s.grid_shortfall_kw > 0 ? 'background:var(--bad-soft)' : ''">
          <td class="mono">
            <strong>{{ s.ref }}</strong><br />
            <span class="muted">车 #{{ s.vehicle_id }}</span>
          </td>
          <td>
            <span v-if="s.connector_code">{{ s.connector_code }}</span>
            <span v-else class="muted">未分配</span>
            <br />
            <span class="muted" style="font-size:12px">
              单枪上限 {{ s.connector_limit_kw ?? '—' }}kW
            </span>
          </td>
          <td>
            <span class="badge info">{{ statusLabel(s.status) }}</span>
            <span v-if="s.battery_derated" class="badge warn" style="margin-top:3px">电池限功率</span>
          </td>
          <td><strong>{{ s.guaranteed_min_kw }}</strong> kW</td>
          <td>{{ s.expected_kw }} kW</td>
          <td>
            <span v-if="s.reported_available_kw != null">{{ s.reported_available_kw }} kW</span>
            <span v-else class="muted">未回报</span>
          </td>
          <td>
            <div class="bar">
              <div class="bar-fill"
                   :class="s.grid_shortfall_kw > 0 ? 'bad' : ''"
                   :style="{ width: barWidth(s) + '%' }"></div>
              <div class="bar-guarantee"
                   :style="{ left: guaranteeMark(s) + '%' }"
                   :title="'最低保证 ' + s.guaranteed_min_kw + 'kW'"></div>
            </div>
            <div style="font-size:12px;margin-top:2px">
              分配 <strong>{{ s.allocated_kw ?? '—' }}</strong> / {{ board.transformer_limit_kw }}kW
              <span v-if="s.grid_shortfall_kw > 0" class="bad">
                （缺口 {{ s.grid_shortfall_kw }}）
              </span>
              <span v-else-if="s.is_charging" class="ok">（≥保证值）</span>
            </div>
          </td>
          <td>
            <span v-if="s.open_ops_case_id" class="badge bad">
              工单 #{{ s.open_ops_case_id }} {{ caseStatus(s.ops_case_status) }}
            </span>
            <span v-else-if="s.grid_shortfall_kw > 0" class="badge bad">缺额</span>
            <span v-else class="badge ok">正常</span>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else-if="board" class="muted" style="margin-top:10px">该时刻没有重叠的在途会话。</p>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import { api, CASE_STATUS_LABELS, STATUS_LABELS } from '../api.js'

const props = defineProps({
  siteId: { type: Number, default: 2 },
  at: { type: String, required: true },
})

const board = ref(null)

function normAt(t) {
  return t.length === 16 ? t + ':00' : t
}

async function load() {
  if (!props.at) return
  board.value = await api.get(
    `/api/sites/${props.siteId}/power-board?at=${encodeURIComponent(normAt(props.at))}`
  )
}

defineExpose({ load })
watch(() => [props.siteId, props.at], load, { immediate: true })

function statusLabel(s) { return STATUS_LABELS[s] || s }
function caseStatus(s) { return CASE_STATUS_LABELS[s] || s || '' }
function barWidth(s) {
  const limit = board.value?.transformer_limit_kw || 1
  const v = s.allocated_kw ?? 0
  return Math.max(0, Math.min(100, (v / limit) * 100))
}
function guaranteeMark(s) {
  const limit = board.value?.transformer_limit_kw || 1
  return Math.max(0, Math.min(100, (s.guaranteed_min_kw / limit) * 100))
}
</script>

<style scoped>
.stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
@media (max-width: 720px) { .stats { grid-template-columns: repeat(2, 1fr); } }
.stat {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 12px;
  background: #fafbfc;
}
.stat-k { font-size: 12px; color: var(--muted); }
.stat-v { font-size: 19px; font-weight: 800; margin-top: 2px; }
.stat-v.ok { color: var(--ok); }
.stat-v.warn { color: var(--warn); }
.stat-v.bad { color: var(--bad); }
.bar {
  position: relative;
  height: 14px;
  background: #eef1f4;
  border-radius: 7px;
  overflow: visible;
}
.bar-fill {
  height: 14px;
  border-radius: 7px;
  background: linear-gradient(90deg, #2f8fd0, #1769aa);
  transition: width .3s;
}
.bar-fill.bad { background: var(--bad); }
.bar-guarantee {
  position: absolute;
  top: -3px;
  width: 2px;
  height: 20px;
  background: var(--ink);
  opacity: .55;
}
</style>
