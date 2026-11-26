async function request(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    // FastAPI 的 detail 可能是字符串或结构化对象
    const detail = data.detail
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.message || JSON.stringify(detail || data)
    const err = new Error(message)
    err.status = resp.status
    err.detail = detail
    throw err
  }
  return data
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b) => request('POST', p, b),
}

export function newIdempotencyKey() {
  // 前端生成可读幂等键；重复提交时复用同一个键即可回放原结果
  return 'ui-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8)
}

export const STATUS_LABELS = {
  CONFIRMED: '已确认',
  ARRIVED: '已到站',
  CHARGING: '充电中（受保护）',
  COMPLETED: '已完成',
  CANCELLED: '已取消（已释放）',
  NO_SHOW: '迟到失约（已释放）',
  NEEDS_RESELECTION: '需改选',
  MANUAL_CONTACT: '需人工联系',
}

export const MODE_LABELS = {
  CONNECTOR: '固定枪口',
  POOL: '资源池',
}

export const IMPACT_LABELS = {
  UNCHANGED: '继续履约',
  NEEDS_RESELECTION: '进入改选',
  MANUAL_CONTACT: '人工联系',
  PROTECTED_CHARGING: '充电中受保护',
}

export const POLICY_LABELS = {
  RESERVE_EXPECTED: '按期望整段预留',
  GUARANTEED_MIN: '仅保证最低值 · 5 分钟动态分配',
}

export const CASE_STATUS_LABELS = {
  OPEN: '待处置',
  HANDLING: '处置中',
  RESOLVED: '已处置',
}

export const CASE_KIND_LABELS = {
  GRID_EMERGENCY: '电网紧急限额',
  BUDGET_REVISION: '预算修订',
  POWER_SHORTFALL: '功率缺额',
}

export function fmtTime(t) {
  return t ? t.replace('T', ' ').slice(5, 16) : ''
}
