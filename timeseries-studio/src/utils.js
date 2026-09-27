// ============================================================
// 通用工具：格式化 / 时间识别与转换 / 异常判定表达式 / 下载与文件图标
// 整表级的统计、抽稀、重采样、清洗都在服务端算，这里不放算法
// ============================================================

export function pad(n) { return String(n).padStart(2, '0') }

export function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB'
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB'
}

// ---- 时间格式识别 ----
const TIME_FORMAT_PATTERNS = [
  { regex: /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/, format: 'YYYY-MM-DD HH:mm:ss', confidence: 98 },
  { regex: /^\d{4}\/\d{2}\/\d{2} \d{2}:\d{2}$/,     format: 'YYYY/MM/DD HH:mm',     confidence: 95 },
  { regex: /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/,  format: 'YYYY-MM-DDTHH:mm:ss',  confidence: 96 },
  { regex: /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/,        format: 'YYYY-MM-DD HH:mm',     confidence: 92 },
  { regex: /^\d{4}-\d{2}-\d{2}$/,                     format: 'YYYY-MM-DD',           confidence: 90 },
  { regex: /^\d{4}\/\d{2}\/\d{2}$/,                   format: 'YYYY/MM/DD',           confidence: 88 },
  { regex: /^\d{13}$/,                                 format: 'epoch_ms',             confidence: 99 },
  { regex: /^\d{10}$/,                                 format: 'epoch_s',              confidence: 97 },
  { regex: /^\d{14}$/,                                 format: 'YYYYMMDDHHmmss',       confidence: 94 },
  { regex: /^\d{4}\d{2}\d{2}$/,                        format: 'YYYYMMDD',             confidence: 85 },
  { regex: /^\d{2}\/\d{2}\/\d{4}/,                     format: 'MM/DD/YYYY',           confidence: 80 },
  { regex: /^\d{2}-\d{2}-\d{4}/,                       format: 'DD-MM-YYYY',           confidence: 78 }
]

function parseTimeValue(value) {
  const str = String(value).trim()
  if (/^\d{13}$/.test(str)) return new Date(Number(str))
  if (/^\d{10}$/.test(str)) return new Date(Number(str) * 1000)
  if (/^\d{14}$/.test(str)) {
    return new Date(+str.slice(0, 4), +str.slice(4, 6) - 1, +str.slice(6, 8), +str.slice(8, 10), +str.slice(10, 12), +str.slice(12, 14))
  }
  return new Date(str.replace(/\//g, '-').replace('T', ' '))
}

export function convertSingleTime(value, targetFmt) {
  const str = String(value).trim()
  const d = parseTimeValue(str)
  if (isNaN(d.getTime())) return str

  switch (targetFmt) {
    case 'YYYY-MM-DD HH:mm:ss': return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    case 'YYYY-MM-DD HH:mm':    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
    case 'YYYY-MM-DD':          return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`
    case 'YYYY/MM/DD HH:mm':    return `${d.getFullYear()}/${pad(d.getMonth()+1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
    case 'YYYY/MM/DD':          return `${d.getFullYear()}/${pad(d.getMonth()+1)}/${pad(d.getDate())}`
    case 'YYYY-MM-DDTHH:mm:ss': return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    case 'YYYYMMDDHHmmss':      return `${d.getFullYear()}${pad(d.getMonth()+1)}${pad(d.getDate())}${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
    case 'YYYYMMDD':            return `${d.getFullYear()}${pad(d.getMonth()+1)}${pad(d.getDate())}`
    case 'epoch_ms':            return String(d.getTime())
    case 'epoch_s':             return String(Math.floor(d.getTime() / 1000))
    default: return str
  }
}

// 对样本值做格式投票识别，返回 { format, confidence, matchRate }
export function detectTimeFormatOfSamples(samples) {
  const votes = {}
  TIME_FORMAT_PATTERNS.forEach(p => { votes[p.format] = 0 })
  samples.forEach(s => {
    const str = String(s).trim()
    TIME_FORMAT_PATTERNS.forEach(p => {
      if (p.regex.test(str)) votes[p.format] = (votes[p.format] || 0) + 1
    })
  })
  let bestFormat = 'YYYY-MM-DD HH:mm:ss', bestCount = 0
  Object.entries(votes).forEach(([fmt, count]) => {
    if (count > bestCount) { bestCount = count; bestFormat = fmt }
  })
  const pattern = TIME_FORMAT_PATTERNS.find(p => p.format === bestFormat)
  const confidence = pattern ? pattern.confidence : 80
  const matchRate = samples.length ? (bestCount / samples.length) * 100 : 0
  return { format: bestFormat, confidence, matchRate, matched: bestCount }
}

// 自定义格式字符串（仅支持 YYYY MM DD HH mm ss 占位符）
export function convertWithCustomFormat(value, customFmt) {
  const d = parseTimeValue(value)
  if (isNaN(d.getTime())) return String(value)
  return customFmt
    .replace(/YYYY/g, d.getFullYear())
    .replace(/MM/g, pad(d.getMonth() + 1))
    .replace(/DD/g, pad(d.getDate()))
    .replace(/HH/g, pad(d.getHours()))
    .replace(/mm/g, pad(d.getMinutes()))
    .replace(/ss/g, pad(d.getSeconds()))
}

// ---- 统计 ----
// 整表级统计（Count/Mean/Std/分位数/缺失率）只有后端一份实现：
// 浏览器曾有一份同名的 columnStats 兜底，两套口径迟早给出两个答案，已删除。
export function isMissing(v) {
  return v === null || v === undefined || v === '' || (typeof v === 'number' && isNaN(v))
}

// 缺失率 / 重复率 / 缺失段 / 逐段填补 / 采样频率推断 / 重采样 / 统计矩阵的浏览器实现已删除：
// 这些数字现在只有 GET /quality、GET /stats、GET /hist、GET /series-multi 与服务端 op 一个来源，
// 浏览器不再持有整表（见 store.js 顶部的说明与 server README 的"迁移进度"）。

// ---- 重采样（这里的三张表只是界面选项文案；真实聚合由 POST /op/resample 在服务端做）----
export const RESAMPLE_RATE_MAP = { '15min': 15, '30min': 30, '60min': 60, '5min': 5, '1min': 1, '120min': 120, '1440min': 1440 }
export const RESAMPLE_RATE_NAMES = { '15min': '15 min', '30min': '30 min', '60min': '60 min', '5min': '5 min', '1min': '1 min' }
export const RESAMPLE_METHOD_NAMES = { mean: '均值聚合', sum: '求和聚合', first: '首值采样', interpolate: '线性插值' }

// ---- 表达式求值（异常判定；外生变量公式在服务端 compile_formula 里求值）----
const EXPR_ALLOWED_TOKENS = /^[0-9a-zA-Z_+\-*/().,><=!&|\s%]+$/

// 异常判定表达式: v + 统计量
export function buildExprEvaluator(expr) {
  if (!EXPR_ALLOWED_TOKENS.test(expr)) throw new Error('表达式包含不支持的字符')
  const safe = expr
    .replace(/\bmedian\b/g, 'stats.median')
    .replace(/\bmean\b/g, 'stats.mean')
    .replace(/\bstd\b/g, 'stats.std')
    .replace(/\bq1\b/g, 'stats.q1')
    .replace(/\bq3\b/g, 'stats.q3')
    .replace(/\bmin\b/g, 'stats.min')
    .replace(/\bmax\b/g, 'stats.max')
  // eslint-disable-next-line no-new-func
  return new Function('v', 'stats', `"use strict"; return (${safe});`)
}

// ---- 文件图标：文件本身由后端解析，这里只管界面外观 ----
export function fileExt(name) { return (name.split('.').pop() || '').toLowerCase() }

export function getFileIconMeta(filename) {
  const ext = fileExt(filename)
  if (ext === 'csv') return { icon: 'fa-file-csv', color: 'text-emerald-600', bg: 'bg-emerald-50' }
  if (ext === 'xlsx' || ext === 'xls') return { icon: 'fa-file-excel', color: 'text-green-600', bg: 'bg-green-50' }
  if (ext === 'parquet') return { icon: 'fa-file-columns', color: 'text-orange-600', bg: 'bg-orange-50' }
  if (ext === 'feather') return { icon: 'fa-feather', color: 'text-sky-600', bg: 'bg-sky-50' }
  return { icon: 'fa-file', color: 'text-slate-600', bg: 'bg-slate-100' }
}

// ---- 导出 ----
// 宽表（CSV/XLSX/Parquet/Feather）不再在浏览器编码：明细留在后端工作区，
// 导出走 GET /api/ws/{id}/export，这里只负责把回来的 Blob 触发下载。
export function downloadBlob(content, filename, mime) {
  const blob = content instanceof Blob ? content : new Blob([content], { type: mime || 'application/octet-stream' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 500)
}

// 单位换算表
export const UNIT_CONVERSIONS = {
  'kW':   [{ target: 'W', factor: 1000, offset: 0 }, { target: 'MW', factor: 0.001, offset: 0 }],
  'W':    [{ target: 'kW', factor: 0.001, offset: 0 }, { target: 'MW', factor: 1e-6, offset: 0 }],
  'MW':   [{ target: 'kW', factor: 1000, offset: 0 }, { target: 'W', factor: 1e6, offset: 0 }],
  'W/m²': [{ target: 'kW/m²', factor: 0.001, offset: 0 }],
  'kW/m²':[{ target: 'W/m²', factor: 1000, offset: 0 }],
  '°C':   [{ target: '°F', factor: 9 / 5, offset: 32 }, { target: 'K', factor: 1, offset: 273.15 }],
  '°F':   [{ target: '°C', factor: 5 / 9, offset: -32 * 5 / 9 }, { target: 'K', factor: 5 / 9, offset: -32 * 5 / 9 + 273.15 }],
  'K':    [{ target: '°C', factor: 1, offset: -273.15 }, { target: '°F', factor: 9 / 5, offset: -273.15 * 9 / 5 + 32 }],
  'm/s':  [{ target: 'km/h', factor: 3.6, offset: 0 }, { target: 'mph', factor: 2.23694, offset: 0 }],
  'km/h': [{ target: 'm/s', factor: 1 / 3.6, offset: 0 }, { target: 'mph', factor: 0.621371, offset: 0 }],
  '%':    [{ target: '‰', factor: 10, offset: 0 }]
}
