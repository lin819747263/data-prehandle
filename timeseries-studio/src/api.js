// 后端 API 客户端：数据加工的服务端入口。
// 明细表留在后端（/api/ws 工作区），浏览器只持有元数据与"当前页附近"的行窗口；
// Parquet/Feather 编解码、sklearn 完整版孤立森林、dataset 目录读写同样在后端。
// 后端不在线时工作台转为只读：不再保留浏览器端的算法兜底实现。
import { state } from './store'
import { downloadBlob } from './utils'

export const API_BASE = localStorage.getItem('tss.apiBase') || 'http://127.0.0.1:8000'

const TIMEOUT_MS = 20000
// 整列取数 / 大表建区可能要几十秒，不能用 20 秒的统一超时一刀切
const LONG_TIMEOUT_MS = 180000

async function withTimeout(path, init = {}, timeoutMs = TIMEOUT_MS) {
  if (init.signal) return fetch(API_BASE + path, init)
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), timeoutMs)
  try {
    return await fetch(API_BASE + path, { ...init, signal: ctl.signal })
  } finally {
    clearTimeout(timer)
  }
}

async function jsonError(res) {
  let detail = ''
  try {
    const body = await res.json()
    detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
  } catch (e) { /* 非 JSON 响应 */ }
  return new Error(`HTTP ${res.status}${detail ? ' · ' + detail.slice(0, 160) : ''}`)
}

// ---- 探活 ----
export async function checkBackend() {
  state.backend.checking = true
  try {
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), 4000)
    const res = await withTimeout('/api/health', { signal: ctl.signal })
    clearTimeout(timer)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const body = await res.json()
    state.backend.online = true
    state.backend.version = body.version || ''
    state.backend.capabilities = body.capabilities || []
    state.backend.limits = body.limits || null
    state.backend.datasetDir = body.datasetDir || ''
    state.backend.error = ''
  } catch (e) {
    state.backend.online = false
    state.backend.version = ''
    state.backend.capabilities = []
    state.backend.limits = null
    state.backend.datasetDir = ''
    state.backend.error = e.name === 'AbortError' ? '请求超时' : (e.message || '不可达')
  }
  state.backend.checkedAt = new Date().toLocaleTimeString()
  state.backend.checking = false
  return state.backend
}

// ---- 数据集目录（后端 <cwd>/dataset，前端据此渲染"最近打开的数据集"）----
export async function listDatasets() {
  const res = await withTimeout('/api/datasets')
  if (!res.ok) throw await jsonError(res)
  return res.json()
}

// ============================================================
// 服务端工作区：明细留在后端，前端只拿元数据 + 一页窗口
// ============================================================
async function wsJson(path, init) {
  const res = await withTimeout(path, init)
  if (!res.ok) throw await jsonError(res)
  return res.json()
}

const enc = encodeURIComponent

// 建区成功的响应统一是 { meta, page, ... }
export function wsCreatePreset(key, seed) {
  return wsJson('/api/ws/preset', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(seed === null || seed === undefined ? { key } : { key, seed })
  })
}

// 导入：先落盘到数据集目录（persist），再建工作区，因此下次打开即出现在最近列表
export async function wsCreateFile(file, { persist = true, limit } = {}) {
  const fd = new FormData()
  fd.append('file', file, file.name)
  const q = `?persist=${persist ? 'true' : 'false'}${limit ? `&limit=${limit}` : ''}`
  const res = await withTimeout(`/api/ws${q}`, { method: 'POST', body: fd }, LONG_TIMEOUT_MS)
  if (!res.ok) throw await jsonError(res)
  return res.json()
}

export function wsOpenDataset(filename, limit) {
  return wsJson(`/api/ws/dataset${limit ? `?limit=${limit}` : ''}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename })
  })
}

export function wsRows(wsId, offset = 0, limit = 50) {
  return wsJson(`/api/ws/${enc(wsId)}/rows?offset=${offset}&limit=${limit}`)
}

export function wsMeta(wsId) {
  return wsJson(`/api/ws/${enc(wsId)}`)
}

export function wsOverview(wsId) {
  return wsJson(`/api/ws/${enc(wsId)}/overview`)
}

// ---- 第四步：诊断与曲线（明细不出后端）----
export function wsQuality(wsId) {
  return withTimeout(`/api/ws/${enc(wsId)}/quality`, {}, LONG_TIMEOUT_MS)
    .then(async (res) => { if (!res.ok) throw await jsonError(res); return res.json() })
}

export function wsSeries(wsId, key, maxPoints = 1200) {
  return wsJson(`/api/ws/${enc(wsId)}/series?col=${enc(key)}&max_points=${maxPoints}`)
}

export function wsAnomaly(wsId) {
  return wsJson(`/api/ws/${enc(wsId)}/anomaly`)
}

// 检测结果留在后端（修复按它记的行索引执行），响应只有统计与抽样
export function wsAnomalyDetect(wsId, payload) {
  return withTimeout(`/api/ws/${enc(wsId)}/anomaly-detect`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
  }, LONG_TIMEOUT_MS).then(async (res) => { if (!res.ok) throw await jsonError(res); return res.json() })
}

export function wsResamplePreview(wsId, targetMinutes) {
  return wsJson(`/api/ws/${enc(wsId)}/resample-preview?targetMinutes=${targetMinutes}`)
}

// long：整表级算法（第五步的特征构建）在几十万格上要几十秒，不能被 20 秒的统一超时砍掉
export function wsOp(wsId, kind, params, limit, { long = false } = {}) {
  const q = limit ? `?limit=${limit}` : ''
  const path = `/api/ws/${enc(wsId)}/op/${kind}${q}`
  const init = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(params) }
  if (!long) return wsJson(path, init)
  return withTimeout(path, init, LONG_TIMEOUT_MS)
    .then(async (res) => { if (!res.ok) throw await jsonError(res); return res.json() })
}

// ---- 第三步：统计矩阵 / 直方图 / 叠加曲线（整表留在后端，浏览器只拿抽稀后的序列）----
export function wsStats(wsId, keys) {
  const q = keys && keys.length ? `?cols=${keys.map(enc).join(',')}` : ''
  return withTimeout(`/api/ws/${enc(wsId)}/stats${q}`, {}, LONG_TIMEOUT_MS)
    .then(async (res) => { if (!res.ok) throw await jsonError(res); return res.json() })
}

export function wsHist(wsId, key, bins = 25) {
  return wsJson(`/api/ws/${enc(wsId)}/hist?col=${enc(key)}&bins=${bins}`)
}

export function wsSeriesMulti(wsId, keys, mode, points) {
  const q = `cols=${keys.map(enc).join(',')}&mode=${enc(mode)}&points=${points}`
  return withTimeout(`/api/ws/${enc(wsId)}/series-multi?${q}`, {}, LONG_TIMEOUT_MS)
    .then(async (res) => { if (!res.ok) throw await jsonError(res); return res.json() })
}

// 新特征列的「首个完整行」：整列扫描在后端做，不再把 5000 行拉回浏览器找行号
export function wsFirstComplete(wsId, keys, scanRows = 5000) {
  return wsJson(`/api/ws/${enc(wsId)}/first-complete?cols=${keys.map(enc).join(',')}&scan_rows=${scanRows}`)
}

// 宽表直出：后端拿着自己的工作区编码，浏览器只负责触发下载
export async function wsExport(wsId, format) {
  const res = await withTimeout(`/api/ws/${enc(wsId)}/export?format=${enc(format)}`, {}, LONG_TIMEOUT_MS)
  if (!res.ok) throw await jsonError(res)
  const blob = await res.blob()
  const disposition = res.headers.get('Content-Disposition') || ''
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
  const plain = /filename="?([^";]+)"?/i.exec(disposition)
  const name = utf8 ? decodeURIComponent(utf8[1]) : (plain ? plain[1] : `timeseries.${format}`)
  downloadBlob(blob, name, blob.type || 'application/octet-stream')
  return { bytes: blob.size, name }
}

// 类别列的取值分布：整表计数在后端数，面板上的「预计新增 N 列」与 /op/feature-cat 同一份口径
export function wsValueCounts(wsId, keys, method) {
  const q = `keys=${keys.map(enc).join(',')}&method=${enc(method)}`
  return withTimeout(`/api/ws/${enc(wsId)}/value-counts?${q}`, {}, LONG_TIMEOUT_MS)
    .then(async (res) => { if (!res.ok) throw await jsonError(res); return res.json() })
}

export function wsRestore(wsId, version, limit) {
  return wsJson(`/api/ws/${enc(wsId)}/restore${limit ? `?limit=${limit}` : ''}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ version })
  })
}

export function wsClose(wsId) {
  return wsJson(`/api/ws/${enc(wsId)}`, { method: 'DELETE' })
}

export function wsList() {
  return wsJson('/api/ws')
}

// ---- 外生变量：选项清单由服务端给，侧表「先看后挂」 ----
export function exoCatalog() {
  return wsJson('/api/exo/presets')
}

// 上传侧表只做解析与落盘，返回表头/可用变量名/时间列候选；挂哪几列由界面确认后走 /op/exo-file
export async function exoInspect(file) {
  const fd = new FormData()
  fd.append('file', file, file.name)
  const res = await withTimeout('/api/exo/inspect', { method: 'POST', body: fd }, LONG_TIMEOUT_MS)
  if (!res.ok) throw await jsonError(res)
  return res.json()
}

// ---- 会话：上次打开了什么、停在哪一步，由服务端记住（GET 时会按命令日志重建工作区）----
export function sessionGet() {
  return withTimeout('/api/session', {}, LONG_TIMEOUT_MS)
    .then(async (res) => { if (!res.ok) throw await jsonError(res); return res.json() })
}

export async function sessionSet(payload) {
  const res = await withTimeout('/api/session', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
  })
  if (!res.ok) throw await jsonError(res)
  return res.json()
}

export function sessionClear() {
  return wsJson('/api/session', { method: 'DELETE' })
}
