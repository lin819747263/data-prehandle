// 后端 API 客户端：承担浏览器做不到或需要真实文件系统的能力
// （Parquet/Feather 列式压缩编解码、sklearn 完整版孤立森林、dataset 目录读写）。
// 除数据集目录外后端无状态；离线时前端保持本地解析与提示。
import { state } from './store'
import { downloadBlob } from './utils'

export const API_BASE = localStorage.getItem('tss.apiBase') || 'http://127.0.0.1:8000'

const TIMEOUT_MS = 20000

async function withTimeout(path, init = {}) {
  if (init.signal) return fetch(API_BASE + path, init)
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS)
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
    state.backend.datasetDir = body.datasetDir || ''
    state.backend.error = ''
  } catch (e) {
    state.backend.online = false
    state.backend.version = ''
    state.backend.capabilities = []
    state.backend.datasetDir = ''
    state.backend.error = e.name === 'AbortError' ? '请求超时' : (e.message || '不可达')
  }
  state.backend.checkedAt = new Date().toLocaleTimeString()
  state.backend.checking = false
  return state.backend
}

// ---- 导出（Parquet/Feather 需真实列式压缩编码）----
export async function exportViaBackend(format, dset, filename) {
  const res = await withTimeout('/api/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      format,
      columns: dset.columns.map(c => c.key),
      rows: dset.data,
      filename
    })
  })
  if (!res.ok) throw await jsonError(res)
  const blob = await res.blob()
  const disposition = res.headers.get('Content-Disposition') || ''
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
  const plain = /filename="?([^";]+)"?/i.exec(disposition)
  const name = utf8 ? decodeURIComponent(utf8[1]) : (plain ? plain[1] : `${filename}.${format}`)
  downloadBlob(blob, name, blob.type || 'application/octet-stream')
  return { bytes: blob.size, name }
}

// ---- 完整版孤立森林（sklearn IsolationForest）----
export async function iforestViaBackend(columns, params = {}) {
  const res = await withTimeout('/api/anomaly/iforest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ columns, ...params })
  })
  if (!res.ok) throw await jsonError(res)
  return res.json()
}

// ---- 数据集目录（后端 <cwd>/dataset，前端据此渲染"最近打开的数据集"）----
export async function listDatasets() {
  const res = await withTimeout('/api/datasets')
  if (!res.ok) throw await jsonError(res)
  return res.json()
}

// 导入：落盘到数据集目录 + 解析返回，因此下次打开即出现在最近列表
export async function importViaBackend(file) {
  const fd = new FormData()
  fd.append('file', file, file.name)
  const res = await withTimeout('/api/datasets', { method: 'POST', body: fd })
  if (!res.ok) throw await jsonError(res)
  return res.json()
}

export async function openDataset(filename) {
  const res = await withTimeout(`/api/datasets/${encodeURIComponent(filename)}/parse`)
  if (!res.ok) throw await jsonError(res)
  return res.json()
}
