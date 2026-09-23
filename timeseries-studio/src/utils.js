// ============================================================
// 通用工具：格式化 / 时间识别与转换 / 统计 / 文件解析与导出
// ============================================================
import Papa from 'papaparse'
import * as XLSX from 'xlsx'

export function pad(n) { return String(n).padStart(2, '0') }

export function formatDate(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB'
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB'
}

// ---- 时间格式识别 ----
export const TIME_FORMAT_PATTERNS = [
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

export function looksLikeTimestamp(str) {
  const s = String(str).trim()
  return TIME_FORMAT_PATTERNS.some(p => p.regex.test(s))
}

export function parseTimeValue(value) {
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
export function isMissing(v) {
  return v === null || v === undefined || v === '' || (typeof v === 'number' && isNaN(v))
}

export function columnStats(values) {
  const valid = values.filter(v => !isMissing(v) && !isNaN(Number(v))).map(Number).sort((a, b) => a - b)
  const total = values.length
  const missing = total - valid.length
  if (valid.length === 0) {
    return { n: 0, missing, missingRate: total ? 100 : 0, mean: 0, std: 0, min: 0, q1: 0, median: 0, q3: 0, max: 0 }
  }
  const n = valid.length
  const mean = valid.reduce((s, v) => s + v, 0) / n
  const std = Math.sqrt(valid.reduce((s, v) => s + (v - mean) ** 2, 0) / n)
  const median = n % 2 === 0 ? (valid[n / 2 - 1] + valid[n / 2]) / 2 : valid[Math.floor(n / 2)]
  return {
    n, missing, missingRate: (missing / total) * 100,
    min: valid[0], max: valid[n - 1], mean, std, median,
    q1: valid[Math.floor(n * 0.25)], q3: valid[Math.floor(n * 0.75)]
  }
}

export function medianOf(arr) {
  if (arr.length === 0) return null
  const s = arr.slice().sort((a, b) => a - b)
  const mid = Math.floor(s.length / 2)
  return s.length % 2 !== 0 ? s[mid] : (s[mid - 1] + s[mid]) / 2
}

export function calculateMissingRate(data, numericCols) {
  let total = 0, missing = 0
  data.forEach(row => numericCols.forEach(col => {
    total++
    if (row[col] === null || row[col] === undefined) missing++
  }))
  return total > 0 ? (missing / total * 100).toFixed(2) + '%' : '0.00%'
}

export function calculateDuplicateRate(data, timeCol) {
  if (data.length === 0) return '0.00%'
  const timeSet = new Set()
  let duplicates = 0
  data.forEach(row => {
    const t = row[timeCol]
    if (timeSet.has(t)) duplicates++
    else timeSet.add(t)
  })
  return (duplicates / data.length * 100).toFixed(2) + '%'
}

// 从数据中真实推断采样频率（分钟）
export function detectSamplingMinutes(data, timeCol) {
  const times = data.map(r => parseTimeValue(r[timeCol]).getTime()).filter(t => !isNaN(t)).sort((a, b) => a - b)
  if (times.length < 2) return null
  const deltas = []
  for (let i = 1; i < times.length; i++) {
    const d = times[i] - times[i - 1]
    if (d > 0) deltas.push(d)
  }
  if (deltas.length === 0) return null
  const medianMs = medianOf(deltas)
  return Math.max(1, Math.round(medianMs / 60000))
}

// ---- 缺失时间段检测与填补 ----
export function detectMissingSegments(data, colKey, timeCol) {
  const segments = []
  let segStart = -1
  for (let i = 0; i < data.length; i++) {
    const isMiss = isMissing(data[i][colKey])
    if (isMiss && segStart === -1) segStart = i
    else if (!isMiss && segStart !== -1) {
      segments.push({ startIdx: segStart, endIdx: i - 1, startTime: data[segStart][timeCol], endTime: data[i - 1][timeCol], count: i - segStart })
      segStart = -1
    }
  }
  if (segStart !== -1) {
    segments.push({ startIdx: segStart, endIdx: data.length - 1, startTime: data[segStart][timeCol], endTime: data[data.length - 1][timeCol], count: data.length - segStart })
  }
  return segments
}

function findNearestValid(data, colKey, idx, dir) {
  let i = idx + dir
  while (i >= 0 && i < data.length) {
    if (!isMissing(data[i][colKey])) return i
    i += dir
  }
  return -1
}

export function imputeSegment(data, colKey, seg, algo) {
  const start = seg.startIdx, end = seg.endIdx
  switch (algo) {
    case 'linear': {
      const before = findNearestValid(data, colKey, start, -1)
      const after = findNearestValid(data, colKey, end, 1)
      const vBefore = before !== -1 ? Number(data[before][colKey]) : 0
      const vAfter = after !== -1 ? Number(data[after][colKey]) : vBefore
      const span = (after !== -1 ? after : end + 1) - (before !== -1 ? before : start - 1)
      for (let i = start; i <= end; i++) {
        const pos = before !== -1 ? i - before : i - start + 1
        const ratio = span > 1 ? pos / span : 0.5
        data[i][colKey] = parseFloat((vBefore + (vAfter - vBefore) * ratio).toFixed(4))
      }
      break
    }
    case 'ffill': {
      const bef = findNearestValid(data, colKey, start, -1)
      const fillVal = bef !== -1 ? data[bef][colKey] : 0
      for (let i = start; i <= end; i++) data[i][colKey] = fillVal
      break
    }
    case 'spline': {
      const bIdx = findNearestValid(data, colKey, start, -1)
      const aIdx = findNearestValid(data, colKey, end, 1)
      const vb = bIdx !== -1 ? Number(data[bIdx][colKey]) : 0
      const va = aIdx !== -1 ? Number(data[aIdx][colKey]) : vb
      const len = end - start + 1
      for (let i = start; i <= end; i++) {
        const t = (i - start + 1) / (len + 1)
        const h = -2 * t ** 3 + 3 * t * t // smoothstep
        data[i][colKey] = parseFloat((vb + (va - vb) * h).toFixed(4))
      }
      break
    }
    case 'zero': {
      for (let i = start; i <= end; i++) data[i][colKey] = 0
      break
    }
  }
}

// ---- 重采样 ----
export const RESAMPLE_RATE_MAP = { '15min': 15, '30min': 30, '60min': 60, '5min': 5, '1min': 1, '120min': 120, '1440min': 1440 }
export const RESAMPLE_RATE_NAMES = { '15min': '15 min', '30min': '30 min', '60min': '60 min', '5min': '5 min', '1min': '1 min' }
export const RESAMPLE_METHOD_NAMES = { mean: '均值聚合', sum: '求和聚合', first: '首值采样', interpolate: '线性插值' }

export function performResample(data, timeCol, numericCols, categoryCols, targetMinutes, method) {
  if (data.length === 0) return []
  const parsedData = data.map(row => ({ ...row, _time: parseTimeValue(row[timeCol]).getTime() }))
    .filter(r => !isNaN(r._time))
    .sort((a, b) => a._time - b._time)
  if (parsedData.length === 0) return []

  const startTime = parsedData[0]._time
  const endTime = parsedData[parsedData.length - 1]._time
  const stepMs = targetMinutes * 60 * 1000

  const groups = {}
  parsedData.forEach(row => {
    const groupTime = Math.floor(row._time / stepMs) * stepMs
    if (!groups[groupTime]) groups[groupTime] = []
    groups[groupTime].push(row)
  })

  const resampled = []
  let t = Math.floor(startTime / stepMs) * stepMs
  while (t <= endTime) {
    const group = groups[t] || []
    const newRow = { [timeCol]: formatDate(new Date(t)) }
    numericCols.forEach(col => {
      const values = group.map(r => r[col]).filter(v => !isMissing(v)).map(Number)
      if (values.length === 0) {
        newRow[col] = null
      } else if (method === 'mean' || method === 'interpolate') {
        newRow[col] = parseFloat((values.reduce((a, b) => a + b, 0) / values.length).toFixed(2))
      } else if (method === 'sum') {
        newRow[col] = parseFloat(values.reduce((a, b) => a + b, 0).toFixed(2))
      } else {
        newRow[col] = values[0]
      }
    })
    categoryCols.forEach(col => { newRow[col] = group.length > 0 ? group[0][col] : null })
    resampled.push(newRow)
    t += stepMs
  }
  return resampled
}

// ---- 表达式求值（外生变量公式 / 异常判定）----
const EXPR_ALLOWED_TOKENS = /^[0-9a-zA-Z_+\-*/().,><=!&|\s%]+$/

// 公式生成变量: 白名单函数 + hour/day/month/weekday/idx/PI/E
export function buildVarEvaluator(expr) {
  if (!EXPR_ALLOWED_TOKENS.test(expr)) throw new Error('表达式包含不支持的字符')
  const safe = expr
    .replace(/\bPI\b/g, 'Math.PI')
    .replace(/\bE\b/g, 'Math.E')
    .replace(/\b(sin|cos|tan|abs|sqrt|log|log2|log10|exp|pow|floor|ceil|round|min|max|asin|acos|atan|hypot)\b/g, 'Math.$1')
  // eslint-disable-next-line no-new-func
  return new Function('hour', 'day', 'month', 'weekday', 'idx', `"use strict"; return (${safe});`)
}

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

export function columnQuantileStats(values) {
  const valid = values.filter(v => !isMissing(v) && !isNaN(Number(v))).map(Number).sort((a, b) => a - b)
  const n = valid.length
  const mean = valid.reduce((s, v) => s + v, 0) / (n || 1)
  const std = Math.sqrt(valid.reduce((s, v) => s + (v - mean) ** 2, 0) / (n || 1))
  return {
    mean, std,
    median: medianOf(valid),
    q1: valid[Math.floor(n * 0.25)], q3: valid[Math.floor(n * 0.75)],
    min: valid[0], max: valid[n - 1]
  }
}

// ---- 文件解析：真实 CSV / Excel 解析，parquet/feather 需后端 ----
export function fileExt(name) { return (name.split('.').pop() || '').toLowerCase() }

export function getFileIconMeta(filename) {
  const ext = fileExt(filename)
  if (ext === 'csv') return { icon: 'fa-file-csv', color: 'text-emerald-600', bg: 'bg-emerald-50' }
  if (ext === 'xlsx' || ext === 'xls') return { icon: 'fa-file-excel', color: 'text-green-600', bg: 'bg-green-50' }
  if (ext === 'parquet') return { icon: 'fa-file-columns', color: 'text-orange-600', bg: 'bg-orange-50' }
  if (ext === 'feather') return { icon: 'fa-feather', color: 'text-sky-600', bg: 'bg-sky-50' }
  return { icon: 'fa-file', color: 'text-slate-600', bg: 'bg-slate-100' }
}

// 从对象行推断列元信息
export function inferColumns(rows) {
  if (!rows || rows.length === 0) return []
  const keys = Object.keys(rows[0])
  return keys.map(k => {
    const samples = rows.slice(0, 60).map(r => r[k]).filter(v => v !== null && v !== undefined && v !== '')
    let type = 'category'
    const allNumeric = samples.length > 0 && samples.every(v => typeof v === 'number' || (!isNaN(Number(v)) && !looksLikeTimestamp(v)))
    const timeLike = samples.length > 0 && samples.filter(v => looksLikeTimestamp(v)).length / samples.length >= 0.8
      && !/^\d+$|^\d+\.\d+$/.test(String(samples[0]))
    if (timeLike) type = 'datetime'
    else if (allNumeric) type = 'float'
    return { key: k, label: k, type }
  })
}

export function pickTimeCol(columns) {
  const dt = columns.find(c => c.type === 'datetime' && /time|date|时间|日期|timestamp/i.test(c.key))
    || columns.find(c => c.type === 'datetime')
  return dt ? dt.key : null
}

// 返回 { name, columns, data, timeCol } ；不支持的格式抛错
export function parseDataFile(file) {
  const ext = fileExt(file.name)
  if (ext === 'csv' || ext === 'txt' || ext === 'tsv') {
    return file.text().then(text => {
      const res = Papa.parse(text.trim(), { header: true, dynamicTyping: true, skipEmptyLines: true })
      const rows = res.data.map(row => {
        const clean = {}
        Object.entries(row).forEach(([k, v]) => { clean[String(k).trim()] = v === '' ? null : v })
        return clean
      })
      if (rows.length === 0) throw new Error('文件为空或无法解析为表格')
      return rows
    }).then(rows => finishParsed(file.name, rows))
  }
  if (ext === 'xlsx' || ext === 'xls') {
    return file.arrayBuffer().then(buf => {
      const wb = XLSX.read(buf, { type: 'array', cellDates: false })
      const ws = wb.Sheets[wb.SheetNames[0]]
      const rows = XLSX.utils.sheet_to_json(ws, { defval: null, raw: true })
      if (rows.length === 0) throw new Error('首个工作表为空')
      return rows
    }).then(rows => finishParsed(file.name, rows))
  }
  if (ext === 'parquet' || ext === 'feather') {
    return Promise.reject(new Error(`${ext.toUpperCase()} 解析需要后端支持，浏览器端暂不支持`))
  }
  return Promise.reject(new Error(`不支持的文件类型: .${ext}`))
}

function finishParsed(name, rows) {
  const columns = inferColumns(rows)
  const timeCol = pickTimeCol(columns)
  if (timeCol) {
    const tc = columns.find(c => c.key === timeCol)
    if (tc) tc.isTime = true
  }
  return { name: name.replace(/\.[^.]+$/, ''), columns, data: rows, timeCol }
}

// 把解析出的外生变量文件与主表按时间戳对齐
export function alignExoRows(exoRows, exoTimeColName, mainTimes, mergeMode) {
  const timeIdx = new Map()
  mainTimes.forEach((t, i) => { if (!timeIdx.has(t)) timeIdx.set(t, i) })
  const exoKeys = Object.keys(exoRows[0] || {}).filter(k => k !== exoTimeColName)
  const out = {}
  exoKeys.forEach(k => { out[k] = new Array(mainTimes.length).fill(null) })

  const normKey = s => String(s).replace(/\//g, '-').replace('T', ' ').slice(0, 19)
  const matched = []
  exoRows.forEach(er => {
    const rawT = er[exoTimeColName]
    if (rawT === null || rawT === undefined) return
    const t = normKey(rawT)
    if (mergeMode === 'nearest') {
      const d = parseTimeValue(t).getTime()
      if (isNaN(d)) return
      let best = -1, bestDiff = Infinity
      mainTimes.forEach((mt, i) => {
        const diff = Math.abs(parseTimeValue(mt).getTime() - d)
        if (diff < bestDiff) { bestDiff = diff; best = i }
      })
      if (best >= 0) matched.push({ idx: best, er })
    } else {
      let idx = timeIdx.get(t)
      if (idx === undefined) idx = timeIdx.get(rawT)
      if (idx !== undefined) matched.push({ idx, er })
    }
  })
  matched.forEach(({ idx, er }) => {
    exoKeys.forEach(k => {
      const v = er[k]
      out[k][idx] = typeof v === 'number' || v === null ? v : (isNaN(Number(v)) ? v : Number(v))
    })
  })
  return { keys: exoKeys, values: out, matchedCount: matched.length }
}

// ---- 导出 ----
export function toCSV(dset) {
  const head = dset.columns.map(c => csvEscape(c.key)).join(',')
  const lines = [head]
  dset.data.forEach(row => {
    lines.push(dset.columns.map(c => csvEscape(row[c.key])).join(','))
  })
  return lines.join('\r\n')
}
function csvEscape(v) {
  if (v === null || v === undefined) return ''
  const s = String(v)
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s
}

export function downloadBlob(content, filename, mime) {
  const blob = content instanceof Blob ? content : new Blob([content], { type: mime || 'application/octet-stream' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 500)
}

export function exportDatasetCSV(dset, nameSuffix) {
  downloadBlob('\ufeff' + toCSV(dset), `${nameSuffix || dset.name}.csv`, 'text/csv;charset=utf-8')
}

export function exportDatasetExcel(dset, nameSuffix) {
  const aoa = [dset.columns.map(c => c.key), ...dset.data.map(r => dset.columns.map(c => r[c.key] ?? ''))]
  const ws = XLSX.utils.aoa_to_sheet(aoa)
  const wb = XLSX.utils.book_new()
  XLSX.utils.book_append_sheet(wb, ws, 'data')
  const out = XLSX.write(wb, { bookType: 'xlsx', type: 'array' })
  downloadBlob(new Blob([out], { type: 'application/octet-stream' }), `${nameSuffix || dset.name}.xlsx`)
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
