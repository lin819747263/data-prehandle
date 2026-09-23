// ============================================================
// 全局状态与业务动作（所有数字均来自真实计算）
// ============================================================
import { reactive, markRaw, watch, toRaw } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  formatDate, parseTimeValue, convertSingleTime, convertWithCustomFormat,
  detectTimeFormatOfSamples, columnStats, isMissing, medianOf,
  calculateMissingRate, calculateDuplicateRate, detectSamplingMinutes,
  detectMissingSegments, imputeSegment, performResample,
  RESAMPLE_RATE_MAP, RESAMPLE_RATE_NAMES, RESAMPLE_METHOD_NAMES,
  buildVarEvaluator, buildExprEvaluator, columnQuantileStats,
  downloadBlob
} from './utils'

// ---- 数据集仓库（非响应式，配合 dataVersion 计数器驱动视图） ----
export const datasets = {
  pv: {
    name: '光伏电站实测出力数据 (PV-15min)',
    timeCol: 'timestamp',
    freq: '15 min (96点/天)',
    unit: 'kW',
    columns: [
      { key: 'timestamp', label: '时间戳', type: 'datetime', isTime: true },
      { key: 'active_power', label: '实际有功出力(kW)', type: 'float', isMain: true, unit: 'kW' },
      { key: 'irradiance', label: '斜面总辐照度(W/m²)', type: 'float', unit: 'W/m²' },
      { key: 'temperature', label: '环境温度(°C)', type: 'float', unit: '°C' },
      { key: 'wind_speed', label: '风速(m/s)', type: 'float', unit: 'm/s' },
      { key: 'weather_type', label: '天气类型', type: 'category' }
    ],
    data: []
  },
  load: {
    name: '区域工商业电力负荷数据 (Load-60min)',
    timeCol: 'timestamp',
    freq: '60 min (24点/天)',
    unit: 'MW',
    columns: [
      { key: 'timestamp', label: '时间戳', type: 'datetime', isTime: true },
      { key: 'load_demand', label: '总负荷需求(MW)', type: 'float', isMain: true, unit: 'MW' },
      { key: 'temperature', label: '室外气温(°C)', type: 'float', unit: '°C' },
      { key: 'humidity', label: '相对湿度(%)', type: 'float', unit: '%' },
      { key: 'price_tier', label: '分时电价区间', type: 'category' }
    ],
    data: []
  }
}

// 生成逼真的时序模拟数据（示例数据集）
export function generateSyntheticData() {
  if (datasets.pv.data.length > 0) return
  const pvData = []
  const startDate = new Date(2024, 5, 1, 0, 0, 0)
  const weatherList = ['晴朗', '多云', '少云', '阴天']
  for (let i = 0; i < 2880; i++) {
    const t = new Date(startDate.getTime() + i * 15 * 60 * 1000)
    const hour = t.getHours() + t.getMinutes() / 60
    let baseIrr = 0
    if (hour >= 6 && hour <= 19) {
      baseIrr = Math.sin(((hour - 6) / 13) * Math.PI) * 920
      baseIrr += (Math.random() - 0.5) * 60
      if (baseIrr < 0) baseIrr = 0
    }
    let power = baseIrr > 0 ? baseIrr * 1.85 + (Math.random() - 0.5) * 30 : 0
    const temp = 20 + Math.sin(((hour - 4) / 24) * 2 * Math.PI) * 10 + (Math.random() - 0.5) * 2
    const wind = 2.5 + Math.random() * 3.5
    const weather = weatherList[Math.floor((i / 96) % weatherList.length)]
    if (i >= 380 && i <= 388) power = null
    if (i === 550) power = 3100.0
    if (i === 820) power = -80.0
    pvData.push({
      timestamp: formatDate(t),
      active_power: power !== null ? parseFloat(power.toFixed(2)) : null,
      irradiance: parseFloat(baseIrr.toFixed(1)),
      temperature: parseFloat(temp.toFixed(1)),
      wind_speed: parseFloat(wind.toFixed(1)),
      weather_type: weather
    })
  }
  datasets.pv.data = pvData

  const loadData = []
  const startLoad = new Date(2024, 5, 1, 0, 0, 0)
  for (let i = 0; i < 720; i++) {
    const t = new Date(startLoad.getTime() + i * 3600 * 1000)
    const hour = t.getHours()
    let baseLoad = 350 + Math.sin(((hour - 3) / 24) * 2 * Math.PI) * 80
    if ((hour >= 9 && hour <= 11) || (hour >= 19 && hour <= 21)) baseLoad += 110
    baseLoad += (Math.random() - 0.5) * 25
    const temp = 22 + Math.sin(((hour - 5) / 24) * 2 * Math.PI) * 8
    const humidity = 60 + (Math.random() - 0.5) * 20
    const tier = (hour >= 8 && hour <= 21) ? '高峰电价' : '低谷电价'
    if (i === 120) baseLoad = 890
    loadData.push({
      timestamp: formatDate(t),
      load_demand: parseFloat(baseLoad.toFixed(2)),
      temperature: parseFloat(temp.toFixed(1)),
      humidity: parseFloat(humidity.toFixed(1)),
      price_tier: tier
    })
  }
  datasets.load.data = loadData
}

// ---- 响应式状态 ----
export const state = reactive({
  currentStep: 1,
  currentKey: 'pv',
  dataVersion: 0,
  drawerOpen: false,
  logFilter: 'all',
  actionLog: [],
  importedLog: null,
  replayStatus: null,   // null | { text }
  showCodeModal: false,
  codeContent: '',
  showExportModal: false,
  pendingFiles: [],     // { name, size, file }
  exoVars: [],          // { key,label,source,data[] }
  derivedCols: [],      // { key,label,formula }
  masks: [],            // { key,label,startIdx,endIdx,startTime,endTime,onesCount }
  features: [],         // 第五步特征清单 { key,label,isNew }
  imputeSegAlgos: {},   // colKey -> { segIdx -> algo }
  lastAnomaly: null,    // { algo, expr, time, perColumn: {col:{set,lower,upper}}, summary }
  splitRatio: 70,
  qualityDirtyVersion: 0,
  history: { undo: 0, redo: 0, undoLabel: '', redoLabel: '', enabled: true },
  session: { enabled: true, found: null, savedAt: '' },
  backend: { online: false, checking: true, version: '', capabilities: [], error: '', checkedAt: '', datasetDir: '' }
})

export function ds() { return datasets[state.currentKey] }
export function touch() { state.dataVersion++ }

export function toast(type, msg) {
  ElMessage({ type, message: msg, duration: 3200, showClose: true, grouping: true })
}

// ---- 操作日志 ----
export const LOG_STEP_META = {
  1: { short: '① 加载', full: '第一步 · 数据加载', color: 'bg-sky-500', text: 'text-sky-600' },
  2: { short: '② 接入', full: '第二步 · 数据接入与配置', color: 'bg-indigo-500', text: 'text-indigo-600' },
  3: { short: '③ 探索', full: '第三步 · 数据探索分析', color: 'bg-violet-500', text: 'text-violet-600' },
  4: { short: '④ 清洗', full: '第四步 · 质量诊断与清洗', color: 'bg-amber-500', text: 'text-amber-600' },
  5: { short: '⑤ 特征', full: '第五步 · 特征构建工程', color: 'bg-emerald-500', text: 'text-emerald-600' }
}
export const LOG_ICONS = {
  dataset: 'fa-database', column: 'fa-columns', rename: 'fa-pen', delete: 'fa-trash-can',
  unit: 'fa-gear', resample: 'fa-wave-square', split: 'fa-scissors', impute: 'fa-wrench',
  anomaly: 'fa-triangle-exclamation', mask: 'fa-vector-square', feature: 'fa-flask',
  switch: 'fa-arrow-right', explore: 'fa-chart-line', broom: 'fa-broom', wand: 'fa-wand-magic-sparkles'
}

export function logAction(step, icon, title, detail, params) {
  const now = new Date()
  state.actionLog.push({
    id: Date.now() + Math.random().toString(36).slice(2, 6),
    step, icon, title, detail, params: params || null,
    time: `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`,
    ts: now.getTime()
  })
}

// ---- 步骤切换 ----
export function switchStep(target) {
  state.currentStep = target
}

// ---- 数据集加载 ----
export function resetWorkspace() {
  state.exoVars = []
  state.derivedCols = []
  state.masks = []
  state.features = []
  state.imputeSegAlgos = {}
  state.lastAnomaly = null
}

export function loadPresetData(key) {
  state.currentKey = key
  resetWorkspace()
  touch()
  logAction(1, 'dataset', '加载数据集', `${ds().name} · ${ds().data.length.toLocaleString()}行×${ds().columns.length}列`)
}

export function loadCustomDataset(parsedList, fileNames, origin = 'upload') {
  const key = 'custom'
  if (!datasets.custom) datasets.custom = { name: '', timeCol: 'timestamp', freq: '', unit: '', columns: [], data: [] }
  const d = datasets.custom
  d.name = parsedList[0].name + (parsedList.length > 1 ? ` 等 ${parsedList.length} 个文件` : '')
  d.columns = parsedList[0].columns
  d.timeCol = parsedList[0].timeCol
  if (parsedList.length > 1) {
    // 必须列名与顺序完全一致才能按行拼接：只比列数会把键不同的行塞进来，
    // 那些行按 d.columns 取值全部落空，预览就变成整片空值。
    const base = parsedList[0]
    const baseKeys = base.columns.map(c => c.key).join('\u0000')
    parsedList.slice(1).forEach(p => {
      if (p.columns.map(c => c.key).join('\u0000') === baseKeys && p.timeCol === base.timeCol) {
        base.data.push(...p.data)
      } else {
        toast('warning', `文件「${p.name}」列结构与首个文件不一致，已跳过`)
      }
    })
    d.columns = base.columns
    d.data = base.data
  } else {
    d.data = parsedList[0].data
  }
  if (!d.timeCol) {
    d.timeCol = d.columns[0].key
    d.columns[0].type = 'datetime'
    d.columns[0].isTime = true
    toast('warning', '未识别到时间列，已按第一列作为时间列处理')
  }
  // 表头与数据行键不一致时不静默展示空表，直接指出是哪一列
  const orphan = d.data.length ? d.columns.find(c => !(c.key in d.data[0])) : null
  if (orphan) {
    toast('error', `列「${orphan.key}」在实际数据行中不存在，表头与数据不匹配。`
      + `数据行实际字段：${Object.keys(d.data[0]).slice(0, 8).join('、')}`)
  }
  const minutes = detectSamplingMinutes(d.data, d.timeCol)
  d.freq = minutes ? `${minutes} min (${Math.floor(1440 / minutes)}点/天)` : '未知'
  d.format = parsedList[0].format || 'csv'
  state.currentKey = key
  resetWorkspace()
  touch()
  const viaBackend = parsedList.filter(p => p.source === 'backend').length
  const saved = parsedList.filter(p => p.saved).map(p => p.saved)
  logAction(1, 'dataset', origin === 'dataset' ? '打开数据集目录文件' : '导入并加载数据文件',
    `${fileNames.join(', ')} · ${d.data.length.toLocaleString()}行×${d.columns.length}列` +
    (viaBackend ? ` · ${viaBackend} 个文件由后端解析` : ' · 浏览器本地解析') +
    (saved.length ? ` · 已落盘 ${saved.join(', ')}` : ''))
}

// ---- 列管理 ----
export function renameColumn(idx, newName) {
  const d = ds()
  const col = d.columns[idx]
  const safeKey = newName.toLowerCase().replace(/[^a-z0-9_]/g, '_')
  const oldKey = col.key
  col.label = newName
  col.key = safeKey
  d.data.forEach(row => { row[safeKey] = row[oldKey]; delete row[oldKey] })
  if (d.timeCol === oldKey) d.timeCol = safeKey
  // 特征登记表要跟着真实列走，否则第五步预览与导出宽表会留下旧键名的空壳幽灵列
  const feat = state.features.find(f => f.key === oldKey)
  if (feat) { feat.key = safeKey; feat.label = newName }
  touch()
  logAction(1, 'rename', '重命名列', `"${oldKey}" → "${newName}"`, { type: 'rename_column', oldKey, newKey: safeKey, newLabel: newName })
}

export function deleteColumn(idx) {
  const d = ds()
  const col = d.columns[idx]
  if (col.isTime || col.key === d.timeCol) {
    toast('warning', '时间列不可删除')
    return false
  }
  d.columns.splice(idx, 1)
  d.data.forEach(row => delete row[col.key])
  const fi = state.features.findIndex(f => f.key === col.key)
  if (fi >= 0) state.features.splice(fi, 1)
  touch()
  logAction(1, 'delete', '删除列', `"${col.label}"`, { type: 'delete_column', key: col.key, label: col.label })
  return true
}

export function convertColumnUnit(idx, factor, offset, newUnit) {
  const d = ds()
  const col = d.columns[idx]
  d.data.forEach(row => {
    const v = row[col.key]
    if (v !== null && v !== undefined && !isNaN(Number(v))) {
      row[col.key] = Number((Number(v) * factor + offset).toFixed(4))
    }
  })
  const baseName = col.label.replace(/\s*\([^)]*\)\s*$/, '').trim()
  const oldUnit = col.unit || ''
  col.label = `${baseName}(${newUnit})`
  col.unit = newUnit
  touch()
  logAction(1, 'unit', '单位转换', `${col.label} y=${factor}x+${offset}`,
    { type: 'unit_convert', key: col.key, factor, offset, newUnit, oldUnit })
}

// ---- 时间格式识别与转换 ----
export function detectTimeFormat(timeCol) {
  const d = ds()
  const samples = d.data.map(r => r[timeCol]).filter(v => !isMissing(v)).slice(0, 20)
  if (samples.length === 0) return null
  return detectTimeFormatOfSamples(samples)
}

export function convertTimeColumn(timeCol, targetFmt, customFmt) {
  const d = ds()
  let changed = 0
  d.data.forEach(row => {
    const v = row[timeCol]
    if (isMissing(v)) return
    const converted = targetFmt === 'custom' ? convertWithCustomFormat(v, customFmt) : convertSingleTime(v, targetFmt)
    if (converted !== String(v).trim()) changed++
    row[timeCol] = converted
  })
  touch()
  logAction(2, 'column', '时间格式转换', `${d.data.length}行 → ${targetFmt === 'custom' ? customFmt : targetFmt}`, { type: 'time_convert', timeCol, targetFmt: targetFmt === 'custom' ? customFmt : targetFmt })
  return changed
}

// ---- 采样频率推断（真实计算） ----
export function detectedFreqMinutes() {
  const d = ds()
  return detectSamplingMinutes(d.data, d.timeCol)
}

export function resampleDataset(targetRate, method) {
  const d = ds()
  const targetMinutes = RESAMPLE_RATE_MAP[targetRate]
  const numericCols = d.columns.filter(c => c.type === 'float').map(c => c.key)
  const categoryCols = d.columns.filter(c => c.type === 'category').map(c => c.key)
  const oldCount = d.data.length
  const newData = performResample(d.data, d.timeCol, numericCols, categoryCols, targetMinutes, method)
  d.data = newData
  d.freq = `${RESAMPLE_RATE_NAMES[targetRate]} (${Math.floor(1440 / targetMinutes)}点/天)`
  touch()
  logAction(2, 'resample', '重采样确认',
    `频率→${RESAMPLE_RATE_NAMES[targetRate]} | 方法→${RESAMPLE_METHOD_NAMES[method]} | ${oldCount.toLocaleString()}→${newData.length.toLocaleString()}`,
    { type: 'resample', targetRate, method, detail: `${RESAMPLE_RATE_NAMES[targetRate]} / ${RESAMPLE_METHOD_NAMES[method]}` })
  return { oldCount, newCount: newData.length }
}

// 实时质量指标（真实计算）
export function currentMissingRate() {
  const d = ds()
  return calculateMissingRate(d.data, d.columns.filter(c => c.type === 'float').map(c => c.key))
}
export function currentDuplicateRate() {
  const d = ds()
  return calculateDuplicateRate(d.data, d.timeCol)
}

// ---- 外生变量 ----
export const EXO_PRESETS = [
  { value: 'humidity', label: '相对湿度 (%)' },
  { value: 'pressure', label: '大气压强 (hPa)' },
  { value: 'cloud_cover', label: '云量 (0-10)' },
  { value: 'dew_point', label: '露点温度 (°C)' },
  { value: 'radiation_ghi', label: '水平面总辐照 GHI (W/m²)' },
  { value: 'radiation_dni', label: '法向直射辐照 DNI (W/m²)' },
  { value: 'electricity_price', label: '实时电价 (元/kWh)' },
  { value: 'grid_frequency', label: '电网频率 (Hz)' }
]

// 预设模板：按时间规律模拟生成（界面已标注为模拟数据）
const EXO_PRESET_GENERATORS = {
  humidity: (d) => d.data.map((r) => {
    const hour = parseTimeValue(r[d.timeCol]).getHours()
    return parseFloat((60 + 20 * Math.sin((hour - 6) / 24 * 2 * Math.PI) + (Math.random() - 0.5) * 15).toFixed(1))
  }),
  pressure: (d) => d.data.map(() => parseFloat((1013.25 + (Math.random() - 0.5) * 8).toFixed(1))),
  cloud_cover: (d) => d.data.map((_, i) => {
    const base = [2, 5, 7, 3][Math.floor(i / 96) % 4]
    return Math.max(0, Math.min(10, Math.round(base + (Math.random() - 0.5) * 4)))
  }),
  dew_point: (d) => d.data.map((r) => parseFloat(((Number(r.temperature) || 20) - 5 - Math.random() * 3).toFixed(1))),
  radiation_ghi: (d) => d.data.map((r) => {
    const hour = parseTimeValue(r[d.timeCol]).getHours()
    if (hour < 6 || hour > 19) return 0
    return parseFloat((Math.sin((hour - 6) / 13 * Math.PI) * 800 + (Math.random() - 0.5) * 50).toFixed(1))
  }),
  radiation_dni: (d) => d.data.map((r) => {
    const hour = parseTimeValue(r[d.timeCol]).getHours()
    if (hour < 6 || hour > 19) return 0
    return parseFloat((Math.sin((hour - 6) / 13 * Math.PI) * 650 + (Math.random() - 0.5) * 40).toFixed(1))
  }),
  electricity_price: (d) => d.data.map((r) => {
    const hour = parseTimeValue(r[d.timeCol]).getHours()
    const peak = hour >= 8 && hour <= 21
    return parseFloat((peak ? 0.85 + Math.random() * 0.15 : 0.35 + Math.random() * 0.1).toFixed(3))
  }),
  grid_frequency: (d) => d.data.map(() => parseFloat((50 + (Math.random() - 0.5) * 0.1).toFixed(3)))
}

export function addExoPreset(presetKey, alias) {
  const d = ds()
  if (state.exoVars.some(v => v.key === (alias || presetKey))) {
    toast('warning', `变量 [${alias || presetKey}] 已添加`)
    return false
  }
  const preset = EXO_PRESET_GENERATORS[presetKey]
  const meta = EXO_PRESETS.find(p => p.value === presetKey)
  const data = preset(d)
  state.exoVars.push({ key: alias || presetKey, label: alias || meta.label.replace(/\s/g, ''), source: 'preset', data })
  logAction(2, 'column', `导入外生变量: ${alias || presetKey}`, `预设模板(模拟) · ${data.length} 行`,
    { type: 'exo_var_add', kind: 'preset', key: alias || presetKey, label: alias || meta.label.replace(/\s/g, ''), presetKey, data })
  return true
}

export function addExoFormula(name, expr) {
  const d = ds()
  if (state.exoVars.some(v => v.key === name)) {
    toast('warning', `变量 [${name}] 已存在`)
    return { ok: false }
  }
  let evaluator
  try { evaluator = buildVarEvaluator(expr) } catch (e) { return { ok: false, error: e.message } }
  const data = d.data.map((row, idx) => {
    try {
      const dt = parseTimeValue(row[d.timeCol])
      const val = evaluator(dt.getHours() + dt.getMinutes() / 60, dt.getDate(), dt.getMonth() + 1, dt.getDay(), idx)
      return typeof val === 'number' && !isNaN(val) ? parseFloat(val.toFixed(4)) : null
    } catch (e) { return null }
  })
  state.exoVars.push({ key: name, label: name, source: 'formula', formula: expr, data })
  logAction(2, 'column', `公式生成外生变量: ${name}`, expr,
    { type: 'exo_var_add', kind: 'formula', key: name, label: name, expr })
  return { ok: true }
}

export function addExoFileVars(fileName, vars) {
  vars.forEach(v => {
    if (!state.exoVars.some(x => x.key === v.key)) {
      state.exoVars.push({ ...v, source: 'file' })
    }
  })
  logAction(2, 'column', `文件导入外生变量: ${fileName}`, `${vars.length} 列 · ${vars[0]?.data.length || 0} 行(对齐后)`,
    { type: 'exo_var_add', kind: 'file', fileName, vars: vars.map(v => ({ key: v.key, label: v.label, data: v.data.slice() })) })
}

export function mergeExoVars() {
  const d = ds()
  let newCols = 0
  const names = []
  const snapshot = state.exoVars.map(v => ({ key: v.key, label: v.label, data: v.data.slice() }))
  state.exoVars.forEach(exo => {
    if (!d.columns.some(c => c.key === exo.key)) {
      d.columns.push({ key: exo.key, label: exo.label, type: 'float' })
      newCols++
    }
    d.data.forEach((row, i) => { row[exo.key] = exo.data[i] !== undefined ? exo.data[i] : null })
    names.push(exo.key)
  })
  const count = state.exoVars.length
  state.exoVars = []
  touch()
  logAction(2, 'column', `合并 ${count} 个外生变量`, names.join(', '), { type: 'exo_merge', vars: snapshot })
  return { count, newCols }
}

// ---- 列运算 ----
export function computeTermChain(terms, row) {
  let result = Number(row[terms[0].col])
  if (!isFinite(result)) return null
  for (let i = 1; i < terms.length; i++) {
    const val = Number(row[terms[i].col])
    if (!isFinite(val)) return null
    switch (terms[i].op) {
      case '+': result += val; break
      case '-': result -= val; break
      case '*': result *= val; break
      case '/': if (val === 0) return null; result /= val; break
      default: return null
    }
  }
  return result
}

export function applyDerivedCol(name, terms) {
  const d = ds()
  if (d.columns.some(c => c.key === name) || state.derivedCols.some(c => c.key === name)) {
    toast('warning', `列名 [${name}] 已存在，请更换名称`)
    return false
  }
  const opLabel = { '+': '+', '-': '-', '*': '×', '/': '÷' }
  d.data.forEach(row => {
    const v = computeTermChain(terms, row)
    row[name] = v !== null ? parseFloat(v.toFixed(4)) : null
  })
  d.columns.push({ key: name, label: name, type: 'float' })
  const formula = terms.map((t, i) => {
    const label = d.columns.find(c => c.key === t.col)?.label || t.col
    return i === 0 ? label : `${opLabel[t.op] || t.op} ${label}`
  }).join(' ')
  state.derivedCols.push({ key: name, label: name, formula })
  touch()
  logAction(2, 'column', `列运算生成: ${name}`, formula, { type: 'multi_calc', terms: terms.map(t => ({ ...t })), name })
  return true
}

export function deleteDerivedCol(idx) {
  const dc = state.derivedCols[idx]
  const d = ds()
  d.data.forEach(row => delete row[dc.key])
  d.columns = d.columns.filter(c => c.key !== dc.key)
  state.derivedCols.splice(idx, 1)
  touch()
}

export function clearAllDerivedCols() {
  const d = ds()
  state.derivedCols.forEach(dc => {
    d.data.forEach(row => delete row[dc.key])
    d.columns = d.columns.filter(c => c.key !== dc.key)
  })
  state.derivedCols = []
  touch()
}

// ---- 缺失与重复清洗 ----
export function columnMissingStats() {
  const d = ds()
  const totalRows = d.data.length
  const stats = d.columns.map(col => {
    let missing = 0
    d.data.forEach(row => { if (isMissing(row[col.key])) missing++ })
    return {
      key: col.key, label: col.label, type: col.type,
      total: totalRows, missing, valid: totalRows - missing,
      rate: totalRows ? (missing / totalRows * 100) : 0
    }
  })
  const timeSet = new Set()
  let duplicateCount = 0
  d.data.forEach(row => {
    const t = row[d.timeCol]
    if (timeSet.has(t)) duplicateCount++
    else timeSet.add(t)
  })
  return { stats, totalRows, duplicateCount }
}

export function segmentsOf(colKey) {
  const d = ds()
  return detectMissingSegments(d.data, colKey, d.timeCol)
}

export function applySegmentImpute(colKey, segIdx) {
  const d = ds()
  const segments = segmentsOf(colKey)
  if (segIdx >= segments.length) return 0
  const seg = segments[segIdx]
  const algo = state.imputeSegAlgos[colKey]?.[segIdx] || 'linear'
  imputeSegment(d.data, colKey, seg, algo)
  touch()
  const col = d.columns.find(c => c.key === colKey)
  logAction(4, 'impute', `填补 ${col?.label || colKey} 第${segIdx + 1}段`, `行${seg.startIdx}–${seg.endIdx} · ${seg.count}行`, { type: 'impute_segment', colKey, segments: [{ startIdx: seg.startIdx, endIdx: seg.endIdx, algo }] })
  return seg.count
}

export function applyAllSegmentsImpute(colKey) {
  const d = ds()
  const segments = segmentsOf(colKey)
  let total = 0
  for (let i = segments.length - 1; i >= 0; i--) {
    const algo = state.imputeSegAlgos[colKey]?.[i] || 'linear'
    imputeSegment(d.data, colKey, segments[i], algo)
    total += segments[i].count
  }
  touch()
  const col = d.columns.find(c => c.key === colKey)
  logAction(4, 'impute', `一键填补 ${col?.label || colKey}`, `共 ${segments.length} 段，${total} 行`, { type: 'impute_segment', colKey, segments: segments.map((s, i) => ({ startIdx: s.startIdx, endIdx: s.endIdx, algo: state.imputeSegAlgos[colKey]?.[i] || 'linear' })) })
  return { count: segments.length, total }
}

export function imputeAllAndDedupe(dupStrategy) {
  const d = ds()
  const numericCols = d.columns.filter(c => c.type === 'float')
  let totalFilled = 0, colsFixed = 0
  numericCols.forEach(col => {
    const segments = segmentsOf(col.key)
    if (segments.length === 0) return
    for (let i = segments.length - 1; i >= 0; i--) {
      const algo = state.imputeSegAlgos[col.key]?.[i] || 'linear'
      imputeSegment(d.data, col.key, segments[i], algo)
      totalFilled += segments[i].count
    }
    colsFixed++
  })

  // 重复时间戳合并
  const groups = new Map()
  d.data.forEach((row, idx) => {
    const t = row[d.timeCol]
    if (!groups.has(t)) groups.set(t, [])
    groups.get(t).push(idx)
  })
  const dupKeys = [...groups.entries()].filter(([, idxs]) => idxs.length > 1)
  const toDelete = new Set()
  dupKeys.forEach(([, idxs]) => {
    if (dupStrategy === 'first') idxs.slice(1).forEach(i => toDelete.add(i))
    else if (dupStrategy === 'last') idxs.slice(0, -1).forEach(i => toDelete.add(i))
    else {
      // mean: 数值列取均值写回首行
      const base = d.data[idxs[0]]
      numericCols.forEach(col => {
        const vals = idxs.map(i => d.data[i][col.key]).filter(v => !isMissing(v)).map(Number)
        if (vals.length > 0) base[col.key] = parseFloat((vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(4))
      })
      idxs.slice(1).forEach(i => toDelete.add(i))
    }
  })
  const dupCount = toDelete.size
  if (dupCount > 0) d.data = d.data.filter((_, i) => !toDelete.has(i))

  touch()
  logAction(4, 'impute', '一键执行缺失值填补与去重', `${colsFixed} 列 · ${totalFilled} 值 · 合并重复 ${dupCount} 条`, { type: 'impute_all' })
  return { colsFixed, totalFilled, dupCount }
}

// ---- 异常检测与修复 ----
export const ANOMALY_ALGOS = {
  '3sigma': '3-Sigma：假设数据服从正态分布，Z-Score 超过 3 倍标准差的点视为异常。适合检测远离均值的极端尖峰和深谷。',
  'iqr': '四分位距箱线法：以 Q1-1.5×IQR 和 Q3+1.5×IQR 为边界，超出范围的点视为异常。对偏态分布更加鲁棒。',
  'iforest': '孤立森林（浏览器近似版）：以滑动窗口 MAD（中位数绝对偏差）近似隔离异常，无需假设分布。完整 Isolation Forest 模型训练需后端支持。',
  'iforest_sklearn': '孤立森林（sklearn 完整版 · 后端）：FastAPI + scikit-learn IsolationForest 对每列真实拟合并打分，缺失点自动剔除，边界取正常点的 0.5%/99.5% 分位数供截断修复。',
  'expr': '自定义表达式：使用数学表达式定义异常条件。变量 v 代表当前值，可用统计量 mean/std/median/q1/q3/min/max。表达式返回 true 即为异常。'
}
export const ANOMALY_REPAIRS = {
  'clip': '截断限制：将超出上下界的数据强制钳位到边界值，保留时间序列连续性，适合传感器饱和场景。',
  'nan_impute': '缺失值重算：将异常点置为 NaN，再用时序插值方法重算，消除异常影响的同时保持趋势平滑。',
  'mask_only': '掩码标记：仅生成布尔掩码列，不修改原始数据。适合需要保留原始观测的分析场景。'
}

export function detectAnomalies(algo, exprStr) {
  const d = ds()
  const totalRows = d.data.length
  const numCols = d.columns.filter(c => c.type === 'float')
  if (algo === 'expr' && !exprStr) return { error: '请输入异常判定表达式' }

  const perColumn = {}
  const results = numCols.map(col => {
    const vals = d.data.map(r => r[col.key])
    const st = columnQuantileStats(vals)
    const anomalySet = new Set()
    let lower, upper

    if (algo === '3sigma') {
      lower = st.mean - 3 * st.std; upper = st.mean + 3 * st.std
      vals.forEach((v, i) => { if (!isMissing(v) && (v < lower || v > upper)) anomalySet.add(i) })
    } else if (algo === 'iqr') {
      const iqr = st.q3 - st.q1
      lower = st.q1 - 1.5 * iqr; upper = st.q3 + 1.5 * iqr
      vals.forEach((v, i) => { if (!isMissing(v) && (v < lower || v > upper)) anomalySet.add(i) })
    } else if (algo === 'expr') {
      let evaluator
      try { evaluator = buildExprEvaluator(exprStr) } catch (e) { return { error: `表达式错误: ${e.message}` } }
      vals.forEach((v, i) => {
        if (isMissing(v)) return
        try { if (evaluator(v, st)) anomalySet.add(i) } catch (e) { /* 单点失败不标记 */ }
      })
      const normalVals = vals.filter((v, i) => !anomalySet.has(i) && !isMissing(v))
      lower = normalVals.length ? Math.min(...normalVals) : st.min
      upper = normalVals.length ? Math.max(...normalVals) : st.max
    } else { // iforest 近似（滑窗 MAD）
      const winSize = 32
      lower = Infinity; upper = -Infinity
      for (let i = 0; i < vals.length; i++) {
        if (isMissing(vals[i])) continue
        const win = vals.slice(Math.max(0, i - winSize), i + 1).filter(v => !isMissing(v)).map(Number)
        if (win.length < 5) continue
        const med = medianOf(win)
        const mad = win.reduce((s, v) => s + Math.abs(v - med), 0) / win.length
        const lo = med - 4 * mad, hi = med + 4 * mad
        if (vals[i] < lo || vals[i] > hi) anomalySet.add(i)
        if (lo < lower) lower = lo
        if (hi > upper) upper = hi
      }
      if (lower === Infinity) { lower = st.min; upper = st.max }
    }

    perColumn[col.key] = { set: anomalySet, lower, upper }
    const rate = totalRows ? anomalySet.size / totalRows * 100 : 0
    return {
      key: col.key, label: col.label, total: totalRows,
      anomalies: anomalySet.size, rate, normal: totalRows - anomalySet.size, lower, upper
    }
  })
  const firstErr = results.find(r => r.error)
  if (firstErr) return { error: firstErr.error }

  const totalAnomalies = results.reduce((s, r) => s + r.anomalies, 0)
  const overallRate = totalRows && numCols.length ? totalAnomalies / (totalRows * numCols.length) * 100 : 0
  const colsAffected = results.filter(r => r.anomalies > 0).length
  const summary = { totalAnomalies, overallRate, colsAffected, numCols: numCols.length }

  state.lastAnomaly = { algo, expr: exprStr, time: new Date().toLocaleTimeString(), perColumn, results, summary }
  touch()
  return { results, summary }
}

// 把后端 sklearn 孤立森林响应映射为前端 lastAnomaly 结构（散点标注与 clip 修复直接复用）
export function applyBackendAnomaly(resp, algoKey = 'iforest_sklearn') {
  const d = ds()
  const totalRows = d.data.length
  const numCols = d.columns.filter(c => c.type === 'float')
  const perColumn = {}
  const results = numCols.map(col => {
    const pc = resp.perColumn[col.key] || {}
    const set = new Set(pc.anomalyIndices || [])
    let lower = pc.lower
    let upper = pc.upper
    if (lower === null || lower === undefined || upper === null || upper === undefined) {
      const st = columnQuantileStats(d.data.map(r => r[col.key]))
      lower = st.min; upper = st.max
    }
    perColumn[col.key] = { set, lower, upper }
    return {
      key: col.key, label: col.label, total: totalRows,
      anomalies: set.size, rate: totalRows ? set.size / totalRows * 100 : 0,
      normal: totalRows - set.size, lower, upper
    }
  })
  const totalAnomalies = results.reduce((s, r) => s + r.anomalies, 0)
  const overallRate = totalRows && numCols.length ? totalAnomalies / (totalRows * numCols.length) * 100 : 0
  const colsAffected = results.filter(r => r.anomalies > 0).length
  const summary = { totalAnomalies, overallRate, colsAffected, numCols: numCols.length }
  state.lastAnomaly = {
    algo: algoKey, expr: '', time: new Date().toLocaleTimeString(),
    perColumn, results, summary, engine: resp.engine, params: resp.params
  }
  touch()
  return { results, summary }
}

export function repairAnomalies(repair) {
  const d = ds()
  if (!state.lastAnomaly) return { error: '请先执行检测' }
  const la = state.lastAnomaly
  const { perColumn } = la
  let touched = 0
  Object.entries(perColumn).forEach(([colKey, info]) => {
    if (info.set.size === 0) return
    if (repair === 'clip') {
      info.set.forEach(i => {
        const v = d.data[i][colKey]
        d.data[i][colKey] = v < info.lower ? parseFloat(info.lower.toFixed(4)) : parseFloat(info.upper.toFixed(4))
        touched++
      })
    } else if (repair === 'nan_impute') {
      info.set.forEach(i => { d.data[i][colKey] = null; touched++ })
      segmentsOf(colKey).forEach(seg => imputeSegment(d.data, colKey, seg, 'linear'))
    } else { // mask_only
      const maskKey = `anomaly_mask_${colKey}`
      if (!d.columns.some(c => c.key === maskKey)) {
        d.columns.push({ key: maskKey, label: `异常掩码:${colKey}`, type: 'binary' })
      }
      d.data.forEach((row, i) => { row[maskKey] = info.set.has(i) ? 1 : 0 })
      touched += info.set.size
    }
  })
  if (repair !== 'mask_only') state.lastAnomaly = null
  touch()
  const snap = {
    algo: la.algo, engine: la.engine || null, params: la.params || null,
    columns: Object.entries(perColumn).filter(([, info]) => info.set.size > 0)
      .map(([key, info]) => ({ key, lower: Number(info.lower), upper: Number(info.upper), count: info.set.size }))
  }
  logAction(4, 'broom', '异常修复执行',
    `${repair === 'clip' ? '阈值截断' : repair === 'nan_impute' ? '置缺失并重插值' : '生成布尔掩码'} · 处理 ${touched} 点`,
    { type: 'anomaly_repair', repair, anomaly: snap })
  return { touched }
}

// ---- 手动掩码 ----
export function generateMask(maskName, range) {
  const d = ds()
  const isNew = !d.columns.some(c => c.key === maskName)
  if (isNew) d.columns.push({ key: maskName, label: `掩码:${maskName}`, type: 'binary' })
  let ones = 0
  d.data.forEach((row, i) => {
    const inRange = i >= range.startIdx && i <= range.endIdx ? 1 : 0
    row[maskName] = inRange
    if (inRange) ones++
  })
  const existIdx = state.masks.findIndex(m => m.key === maskName)
  const entry = { key: maskName, label: `掩码:${maskName}`, startIdx: range.startIdx, endIdx: range.endIdx, startTime: range.start, endTime: range.end, onesCount: ones }
  if (existIdx >= 0) state.masks[existIdx] = entry
  else state.masks.push(entry)
  touch()
  logAction(4, 'mask', `生成布尔掩码: ${maskName}`, `行${range.startIdx}–${range.endIdx} · ${ones}个1`, { type: 'mask_generate', maskName, startIdx: range.startIdx, endIdx: range.endIdx, startTime: range.start, endTime: range.end })
  return ones
}

export function deleteMask(idx) {
  const m = state.masks[idx]
  const d = ds()
  d.data.forEach(row => delete row[m.key])
  d.columns = d.columns.filter(c => c.key !== m.key)
  state.masks.splice(idx, 1)
  touch()
  logAction(4, 'mask', `删除掩码列: ${m.key}`, `行${m.startIdx}–${m.endIdx} · ${m.onesCount}个1`, { type: 'mask_delete', maskKey: m.key })
}

export function deleteAllMasks() {
  const d = ds()
  state.masks.forEach(m => {
    d.data.forEach(row => delete row[m.key])
    d.columns = d.columns.filter(c => c.key !== m.key)
  })
  const count = state.masks.length
  const keys = state.masks.map(m => m.key)
  state.masks = []
  touch()
  logAction(4, 'mask', '清空全部掩码', `删除 ${count} 个掩码列`, { type: 'mask_delete_all', keys })
}

// ---- 数据集切分 ----
export function splitCounts(trainPct) {
  const total = ds().data.length
  const train = Math.floor(total * trainPct / 100)
  const rest = total - train
  const test = Math.floor(rest / 2)
  const val = rest - test
  return { train, val, test, total }
}

// 拖动滑杆本身不改数据，但比例是复现切分的必要参数，必须进审计链（松手才算一次设置）
let lastLoggedSplit = null
export function setSplitRatio(ratio) {
  const r = Math.min(85, Math.max(50, Math.round(Number(ratio)) || 70))
  state.splitRatio = r
  if (lastLoggedSplit === r) return r
  lastLoggedSplit = r
  const s = splitCounts(r)
  logAction(2, 'split', '设置切分比例', `训练 ${r}% → ${s.train.toLocaleString()} / ${s.val.toLocaleString()} / ${s.test.toLocaleString()} 条`,
    { type: 'split', ratio: r })
  // 切分比例是可撤销的工作区状态，touch 让它进入撤销快照
  touch()
  return r
}

// ---- 第五步：特征工程 ----
export function initFeatureList() {
  const d = ds()
  if (state.features.length === 0) {
    state.features = d.columns.map(c => ({ key: c.key, label: c.label, isNew: false }))
    return
  }
  // 重开数据集会清空登记表，而回放是先补特征再进第五步：只种一次会让原始列从预览里消失，
  // 于是「当前共计 N 列」和导出宽表的列数对不上。按 d.columns 补漏，列序与主表一致。
  const missing = d.columns.filter(c => !state.features.some(f => f.key === c.key))
  if (missing.length) state.features.unshift(...missing.map(c => ({ key: c.key, label: c.label, isNew: false })))
}

// 返回是否真的新增，调用方的「新增 N 列」必须以此计数，重复执行同一构建不能虚报。
// 注意：第五步产物只登记进 state.features，导出宽表由 App.vue 的 exportDataset
// 按 features 补齐列；不要写回 d.columns，否则会污染时间列/列运算等选择器。
function pushFeature(key, label) {
  if (state.features.some(f => f.key === key)) return false
  state.features.push({ key, label, isNew: true })
  return true
}

// 中国 2024 年部分法定节假日（示例数据集覆盖 2024-06）
const HOLIDAYS_2024 = new Set(['2024-01-01', '2024-02-10', '2024-02-11', '2024-02-12', '2024-02-13', '2024-02-14', '2024-04-04', '2024-04-05', '2024-05-01', '2024-05-02', '2024-05-03', '2024-06-10', '2024-09-15', '2024-09-16', '2024-10-01', '2024-10-02', '2024-10-03', '2024-10-04', '2024-10-05'])

// 正余弦不是第 7 个日历维度，而是对「已勾选的周期维度」换一种编码方式。
// 只有周期长度固定的维度能编码；day 每月天数不同（28~31），无固定周期，故不参与。
const TIME_DIMS = ['hour', 'day', 'month', 'weekday', 'is_weekend', 'holiday']
const CYCLE_DIMS = ['hour', 'weekday', 'month']
const CYCLE_PERIOD = { hour: 24, weekday: 7, month: 12 }
const CYCLE_CN = { hour: '小时', weekday: '星期', month: '月份' }

export function timeFeaturePlan(opts) {
  const chosen = new Set(opts)
  const dims = TIME_DIMS.filter(o => chosen.has(o))
  const wantSinCos = chosen.has('cyclical_sincos')
  const cycDims = wantSinCos ? CYCLE_DIMS.filter(o => chosen.has(o)) : []
  const cycCols = cycDims.flatMap(o => [`feat_${o}_sin`, `feat_${o}_cos`])
  return { dims, cycDims, cycCols, keys: [...dims.map(o => `feat_${o}`), ...cycCols] }
}

// 供 UI 展示「将生成哪些列」，与实际构建共用 timeFeaturePlan，数字与列名同源于一次计算
export function timePlanLabel(opts) {
  const plan = timeFeaturePlan(opts)
  return {
    dims: plan.dims,
    cyc: plan.cycDims.map(o => `${CYCLE_CN[o]}(${CYCLE_PERIOD[o]})`),
    sinCosCols: plan.cycCols.length,
    keys: plan.keys,
    total: plan.keys.length,
    daySkipped: opts.includes('day') && opts.includes('cyclical_sincos')
  }
}

export const HOLIDAY_COUNT = HOLIDAYS_2024.size

// 与 pandas dt.dayofweek 对齐：周一 = 0
function weekdayIndex(dt) {
  return (dt.getDay() + 6) % 7
}

// 把维度值归一化到 [0,1) 后映射到单位圆，保证周期首尾相连
function cyclical(dt, opt) {
  const raw = opt === 'hour' ? dt.getHours()
    : opt === 'weekday' ? weekdayIndex(dt)
    : dt.getMonth()
  const angle = (raw / CYCLE_PERIOD[opt]) * 2 * Math.PI
  return {
    sin: parseFloat(Math.sin(angle).toFixed(3)),
    cos: parseFloat(Math.cos(angle).toFixed(3))
  }
}

export function buildTimeFeatures(opts) {
  const d = ds()
  const plan = timeFeaturePlan(opts)
  if (plan.keys.length === 0) return { cols: 0, created: 0, sinCos: 0 }
  let created = 0
  const add = key => {
    if (pushFeature(key, `时间:${key.slice('feat_'.length)}`)) created++
  }
  plan.dims.forEach(opt => add(`feat_${opt}`))
  plan.cycDims.forEach(opt => { add(`feat_${opt}_sin`); add(`feat_${opt}_cos`) })
  d.data.forEach(row => {
    const dt = parseTimeValue(row[d.timeCol])
    plan.dims.forEach(opt => {
      const key = `feat_${opt}`
      if (opt === 'hour') row[key] = dt.getHours()
      if (opt === 'day') row[key] = dt.getDate()
      if (opt === 'month') row[key] = dt.getMonth() + 1
      if (opt === 'weekday') row[key] = weekdayIndex(dt)
      if (opt === 'is_weekend') row[key] = (dt.getDay() === 0 || dt.getDay() === 6) ? 1 : 0
      if (opt === 'holiday') {
        const ymd = `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`
        row[key] = HOLIDAYS_2024.has(ymd) ? 1 : 0
      }
    })
    plan.cycDims.forEach(opt => {
      const { sin, cos } = cyclical(dt, opt)
      row[`feat_${opt}_sin`] = sin
      row[`feat_${opt}_cos`] = cos
    })
  })
  touch()
  logAction(5, 'wand', '时间日历特征',
    `${plan.keys.length} 列（新增 ${created}）: ${plan.keys.join(', ')}`,
    { type: 'feature_build', featureType: 'time', detail: [...plan.dims, ...(plan.cycDims.length ? plan.cycDims.map(o => `cyclical_${o}`) : [])].join(',') })
  return { cols: plan.keys.length, created, sinCos: plan.cycCols.length }
}

// 滚动统计量：键是勾选框/日志里的 fn，值是进入列名的简写（median → med 沿用既有命名）
export const ROLL_STATS = { mean: 'mean', std: 'std', max: 'max', min: 'min', median: 'med' }

export function normEwmSpan(v) {
  const n = Math.round(Number(v))
  return Number.isFinite(n) && n >= 2 ? Math.min(n, 500) : 12
}

// 所有窗口类特征（lag / rolling / expanding / ewm）统一不含当前行，
// 等价于 pandas 的 .shift(1)，避免用 t 时刻的值预测 t 时刻的标签。
export function lagFeaturePlan(targetCols, params) {
  const span = normEwmSpan(params.ewmSpan)
  const keys = []
  targetCols.forEach(col => {
    (params.lags || []).forEach(s => keys.push(`lag_${col}_t${s}`))
    ;(params.windows || []).forEach(w => (params.stats || []).forEach(fn => {
      if (ROLL_STATS[fn]) keys.push(`roll_${ROLL_STATS[fn]}_${col}_w${w}`)
    }))
    if (params.expanding) keys.push(`expanding_mean_${col}`)
    if (params.ewm) keys.push(`ewm_${col}_s${span}`)
  })
  return [...new Set(keys)]
}

const LAG_DEFAULTS = { cols: [], lags: [], windows: [], stats: [], expanding: false, ewm: false, ewmSpan: 12 }

export function lagPlanDetail(targetCols, params) {
  return [
    `cols:${targetCols.join(',')}`,
    `lag:${(params.lags || []).join(',')}`,
    `roll:${(params.windows || []).join(',')}`,
    `stats:${(params.stats || []).join(',')}`,
    `exp:${params.expanding ? 1 : 0}`,
    `ewm:${params.ewm ? normEwmSpan(params.ewmSpan) : 0}`
  ].join('|')
}

export function parseLagDetail(detail) {
  const f = {}
  String(detail || '').split('|').forEach(t => {
    const i = t.indexOf(':')
    if (i > 0) f[t.slice(0, i)] = t.slice(i + 1)
  })
  const list = s => (s ? s.split(',').filter(Boolean) : [])
  const nums = s => list(s).map(Number).filter(n => Number.isFinite(n) && n > 0)
  return {
    ...LAG_DEFAULTS,
    cols: list(f.cols),
    lags: nums(f.lag),
    windows: nums(f.roll),
    stats: list(f.stats).filter(s => ROLL_STATS[s]),
    expanding: f.exp === '1',
    ewm: !!f.ewm && f.ewm !== '0',
    ewmSpan: f.ewm && f.ewm !== '0' ? normEwmSpan(f.ewm) : 12
  }
}

export function buildLagFeatures(targetCols, params) {
  const d = ds()
  const data = d.data
  const span = normEwmSpan(params.ewmSpan)
  const keys = lagFeaturePlan(targetCols, params)
  let created = 0
  keys.forEach(k => { if (pushFeature(k, k)) created++ })
  targetCols.forEach(col => {
    params.lags.forEach(step => {
      const lagKey = `lag_${col}_t${step}`
      data.forEach((row, i) => { row[lagKey] = i >= step ? data[i - step][col] : null })
    })
    params.windows.forEach(w => {
      params.stats.forEach(fn => {
        const rKey = `roll_${ROLL_STATS[fn]}_${col}_w${w}`
        data.forEach((row, i) => {
          if (i >= w) {
            const vals = data.slice(i - w, i).map(x => x[col]).filter(v => !isMissing(v)).map(Number)
            if (vals.length === 0) { row[rKey] = null; return }
            if (fn === 'mean') row[rKey] = parseFloat((vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(2))
            else if (fn === 'std') {
              // 总体标准差（ddof=0），与导出的 pandas 代码保持一致
              const m = vals.reduce((a, b) => a + b, 0) / vals.length
              row[rKey] = parseFloat(Math.sqrt(vals.reduce((a, v) => a + (v - m) ** 2, 0) / vals.length).toFixed(2))
            }
            else if (fn === 'max') row[rKey] = Math.max(...vals)
            else if (fn === 'min') row[rKey] = Math.min(...vals)
            else row[rKey] = parseFloat(medianOf(vals).toFixed(2))
          } else row[rKey] = null
        })
      })
    })
    if (params.expanding) {
      const exKey = `expanding_mean_${col}`
      let cumSum = 0, cumCnt = 0
      data.forEach(row => {
        row[exKey] = cumCnt > 0 ? parseFloat((cumSum / cumCnt).toFixed(2)) : null
        const v = row[col]
        if (!isMissing(v)) { cumSum += Number(v); cumCnt++ }
      })
    }
    if (params.ewm) {
      const ewmKey = `ewm_${col}_s${span}`
      const alpha = 2 / (span + 1)
      let prev = null
      data.forEach(row => {
        row[ewmKey] = prev === null ? null : parseFloat(prev.toFixed(2))
        const v = row[col]
        if (isMissing(v)) return
        prev = prev === null ? Number(v) : alpha * Number(v) + (1 - alpha) * prev
      })
    }
  })
  touch()
  const detail = lagPlanDetail(targetCols, params)
  logAction(5, 'wand', '滞后与滑动窗口特征',
    `${keys.length} 列（新增 ${created}）· ${detail}`,
    { type: 'feature_build', featureType: 'lag_roll', detail })
  return { cols: keys.length, created }
}

function computeDFT(x, k) {
  let re = 0, im = 0
  for (let n = 0; n < x.length; n++) {
    const angle = -2 * Math.PI * k * n / x.length
    re += x[n] * Math.cos(angle)
    im += x[n] * Math.sin(angle)
  }
  return Math.sqrt(re * re + im * im) / x.length
}

// 差分与频域：同样是「一份计划四处复用」（面板预览 / 构建 / 日志 / 回放 / 代码导出）
export function diffFeaturePlan(targetCols, params) {
  const keys = []
  targetCols.forEach(col => {
    if (params.d1) keys.push(`diff1_${col}`)
    if (params.d2) keys.push(`diff2_${col}`)
    if (params.seasonal) keys.push(`diff_season${params.period}_${col}`)
  })
  const fftCol = targetCols[0]
  if (fftCol) {
    if (params.fftDominant) keys.push(`fft_top1_${fftCol}`, `fft_top2_${fftCol}`, `fft_top3_${fftCol}`)
    if (params.fftEntropy) keys.push(`fft_entropy_${fftCol}`)
    if (params.fftPowerRatio) keys.push(`fft_power_ratio_${fftCol}`)
  }
  return [...new Set(keys)]
}

export function diffPlanDetail(targetCols, params) {
  const fft = []
  if (params.fftDominant) fft.push('dom')
  if (params.fftEntropy) fft.push('ent')
  if (params.fftPowerRatio) fft.push('pow')
  return [
    `cols:${targetCols.join(',')}`,
    `d1:${params.d1 ? 1 : 0}`,
    `d2:${params.d2 ? 1 : 0}`,
    `seas:${params.seasonal ? Math.max(1, Math.round(params.period) || 1) : 0}`,
    `fft:${fft.join(',')}`
  ].join('|')
}

export function parseDiffDetail(detail) {
  const f = {}
  String(detail || '').split('|').forEach(t => {
    const i = t.indexOf(':')
    if (i > 0) f[t.slice(0, i)] = t.slice(i + 1)
  })
  const cols = (f.cols || '').split(',').filter(Boolean)
  const period = Math.max(1, parseInt(f.seas) || 0)
  const fft = (f.fft || '').split(',').filter(Boolean)
  return {
    cols,
    d1: f.d1 === '1',
    d2: f.d2 === '1',
    seasonal: period > 0,
    period,
    fftDominant: fft.includes('dom'),
    fftEntropy: fft.includes('ent'),
    fftPowerRatio: fft.includes('pow')
  }
}

export function diffFeatureLabel(key) {
  let m = key.match(/^diff_season(\d+)_(.+)$/)
  if (m) return `ΔS${m[1]}_${m[2]}`
  m = key.match(/^diff(\d)_(.+)$/)
  if (m) return `Δ${m[1] === '1' ? '¹' : '²'}_${m[2]}`
  m = key.match(/^fft_entropy_(.+)$/)
  if (m) return `spectral_entropy_${m[1]}`
  m = key.match(/^fft_power_ratio_(.+)$/)
  if (m) return `power_ratio_${m[1]}`
  m = key.match(/^fft_top(\d)_(.+)$/)
  if (m) return `FFT_Top${m[1]}_${m[2]}`
  return key
}

export function buildDiffFeatures(targetCols, params) {
  const d = ds()
  const data = d.data
  const keys = diffFeaturePlan(targetCols, params)
  let created = 0
  keys.forEach(k => { if (pushFeature(k, diffFeatureLabel(k))) created++ })
  targetCols.forEach(col => {
    if (params.d1) {
      const k = `diff1_${col}`
      data.forEach((row, i) => {
        const cur = Number(row[col]), prev = Number(data[i - 1]?.[col])
        row[k] = i >= 1 && !isMissing(row[col]) && !isMissing(data[i - 1][col]) ? parseFloat((cur - prev).toFixed(4)) : null
      })
    }
    if (params.d2) {
      const k = `diff2_${col}`
      data.forEach((row, i) => {
        if (i < 2 || isMissing(row[col]) || isMissing(data[i - 1][col]) || isMissing(data[i - 2][col])) { row[k] = null; return }
        row[k] = parseFloat(((row[col] - data[i - 1][col]) - (data[i - 1][col] - data[i - 2][col])).toFixed(4))
      })
    }
    if (params.seasonal) {
      const p = params.period
      const k = `diff_season${p}_${col}`
      data.forEach((row, i) => {
        row[k] = i >= p && !isMissing(row[col]) && !isMissing(data[i - p][col]) ? parseFloat((row[col] - data[i - p][col]).toFixed(4)) : null
      })
    }
  })

  const fftCol = targetCols[0]
  if (fftCol && (params.fftDominant || params.fftEntropy || params.fftPowerRatio)) {
    const vals = data.map(r => (isMissing(r[fftCol]) ? 0 : Number(r[fftCol])))
    if (params.fftDominant) {
      const spectrum = []
      for (let k = 1; k <= Math.min(50, Math.floor(vals.length / 2)); k++) spectrum.push({ k, energy: computeDFT(vals, k) })
      spectrum.sort((a, b) => b.energy - a.energy)
      spectrum.slice(0, 3).forEach((item, rank) => {
        const key = `fft_top${rank + 1}_${fftCol}`
        const feat = state.features.find(f => f.key === key)
        if (feat) feat.label = `FFT_Top${rank + 1}_E${item.energy.toFixed(1)}`
        const k = item.k
        data.forEach((row, i) => {
          row[key] = parseFloat(computeDFT(vals.slice(Math.max(0, i - 64), i + 1), k).toFixed(2))
        })
      })
    }
    if (params.fftEntropy) {
      const key = `fft_entropy_${fftCol}`
      data.forEach((row, i) => {
        if (i < 64) { row[key] = null; return }
        const seg = vals.slice(i - 64, i)
        const energies = []
        for (let k = 1; k <= 16; k++) energies.push(computeDFT(seg, k))
        const totalE = energies.reduce((a, b) => a + b, 0) || 1
        const entropy = energies.map(e => e / totalE).reduce((acc, p) => acc + (p > 0 ? -p * Math.log2(p) : 0), 0)
        row[key] = parseFloat(entropy.toFixed(4))
      })
    }
    if (params.fftPowerRatio) {
      const key = `fft_power_ratio_${fftCol}`
      data.forEach((row, i) => {
        if (i < 64) { row[key] = null; return }
        const seg = vals.slice(i - 64, i)
        const lowE = [1, 2, 3, 4].reduce((a, k) => a + computeDFT(seg, k), 0)
        const highE = [8, 12, 16].reduce((a, k) => a + computeDFT(seg, k), 0)
        row[key] = parseFloat((lowE / (highE + 1e-10)).toFixed(2))
      })
    }
  }
  touch()
  const detail = diffPlanDetail(targetCols, params)
  logAction(5, 'wand', '差分与频域特征',
    `${keys.length} 列（新增 ${created}）· ${detail}`,
    { type: 'feature_build', featureType: 'diff_freq', detail })
  return { cols: keys.length, created }
}

export function catColumnDistribution(selectedKeys, method) {
  const d = ds()
  const rows = []
  selectedKeys.forEach(key => {
    const col = d.columns.find(c => c.key === key)
    if (!col) return
    const valueMap = {}
    d.data.forEach(row => {
      const v = row[key]
      if (!isMissing(v)) valueMap[v] = (valueMap[v] || 0) + 1
    })
    const uniqueVals = Object.keys(valueMap)
    rows.push({
      label: col.label, key, uniqueVals,
      topVals: uniqueVals.slice(0, 5).map(v => `${v}(${valueMap[v]})`).join(', '),
      counts: valueMap, total: d.data.length
    })
  })
  // 预计新增列数与构建函数共用同一份计划，面板上的数字即产物列数
  return { rows, totalNewCols: catFeaturePlan(selectedKeys, method).length }
}

// 独热列名来自数据取值，日志只记「选哪些列 + 哪种方法」，回放时按当前数据重算同一份计划
export function catFeaturePlan(selectedKeys, method) {
  const d = ds()
  const items = []
  selectedKeys.forEach(catCol => {
    if (!d.columns.some(c => c.key === catCol)) return
    const uniqueVals = [...new Set(d.data.map(r => r[catCol]).filter(v => !isMissing(v)))]
    if (method === 'onehot') {
      uniqueVals.forEach((val, i) => items.push({ key: `${catCol}_${val}`, label: `${catCol}=${val}`, col: catCol, val, idx: i }))
    } else if (method === 'ordinal') {
      items.push({ key: `${catCol}_ordinal`, label: `${catCol}_序数`, col: catCol })
    } else {
      items.push({ key: `${catCol}_target`, label: `${catCol}_目标编码`, col: catCol })
    }
  })
  return [...new Map(items.map(it => [it.key, it])).values()]
}

export function catPlanDetail(selectedKeys, method) {
  return `cols:${selectedKeys.join(',')}|method:${method}`
}

export function parseCatDetail(detail) {
  const f = {}
  String(detail || '').split('|').forEach(t => {
    const i = t.indexOf(':')
    if (i > 0) f[t.slice(0, i)] = t.slice(i + 1)
  })
  const method = ['onehot', 'ordinal', 'target'].includes(f.method) ? f.method : 'onehot'
  return { cols: (f.cols || '').split(',').filter(Boolean), method }
}

export function buildCatFeatures(selectedKeys, method) {
  const d = ds()
  const items = catFeaturePlan(selectedKeys, method)
  let created = 0
  items.forEach(it => { if (pushFeature(it.key, it.label)) created++ })
  const uniqueOf = {}
  selectedKeys.forEach(catCol => {
    uniqueOf[catCol] = [...new Set(d.data.map(r => r[catCol]).filter(v => !isMissing(v)))]
  })
  if (method === 'onehot') {
    items.forEach(it => { d.data.forEach(row => { row[it.key] = row[it.col] === it.val ? 1 : 0 }) })
  } else if (method === 'ordinal') {
    selectedKeys.forEach(catCol => {
      const outKey = `${catCol}_ordinal`
      const valIdx = {}
      uniqueOf[catCol].forEach((v, i) => { valIdx[v] = i })
      d.data.forEach(row => { row[outKey] = isMissing(row[catCol]) ? -1 : (valIdx[row[catCol]] ?? -1) })
    })
  } else {
    const mainFloat = (d.columns.find(c => c.type === 'float' && c.isMain)
      || d.columns.find(c => c.type === 'float' && !c.key.endsWith('_ordinal') && !c.key.endsWith('_target')))?.key
    selectedKeys.forEach(catCol => {
      const outKey = `${catCol}_target`
      const sums = {}, counts = {}
      d.data.forEach(row => {
        const g = row[catCol], v = row[mainFloat]
        if (!isMissing(g) && !isMissing(v)) { sums[g] = (sums[g] || 0) + Number(v); counts[g] = (counts[g] || 0) + 1 }
      })
      d.data.forEach(row => {
        const g = row[catCol]
        row[outKey] = !isMissing(g) && counts[g] !== undefined ? parseFloat((sums[g] / counts[g]).toFixed(2)) : null
      })
    })
  }
  touch()
  const detail = catPlanDetail(selectedKeys, method)
  logAction(5, 'wand', '类别特征编码',
    `${items.length} 列（新增 ${created}）· ${detail}`,
    { type: 'feature_build', featureType: 'cat', detail })
  return { cols: items.length, created }
}

function pad(n) { return String(n).padStart(2, '0') }

export function renameFeature(idx, newLabel) {
  const feat = state.features[idx]
  const d = ds()
  const oldKey = feat.key
  let newKey = oldKey
  if (feat.isNew) {
    const safeKey = newLabel.toLowerCase().replace(/[^a-z0-9_]/g, '_').replace(/_+/g, '_')
    const conflict = state.features.some((f, i) => i !== idx && f.key === safeKey)
    newKey = conflict ? safeKey + '_' + Date.now().toString(36) : safeKey
    d.data.forEach(row => {
      if (row[oldKey] !== undefined) { row[newKey] = row[oldKey]; delete row[oldKey] }
    })
    const col = d.columns.find(c => c.key === oldKey)
    if (col) { col.key = newKey; col.label = newLabel }
    feat.key = newKey
  }
  feat.label = newLabel
  touch()
  // 改名会连带换掉导出宽表的列键，不进审计链就复现不出同一张表
  logAction(5, 'rename', '重命名特征列', `"${oldKey}" → "${newKey}"`,
    { type: 'feature_rename', oldKey, newKey, newLabel })
}

// 特征登记表原本是只增不减的：误建的列会一路带进导出宽表、回放与 Python 脚本。
// 第五步产物只存在于 data 行键与 state.features（不写回 d.columns，见 pushFeature 注释），
// 所以这里自己清行键，不走 deleteColumn。
export function dropFeature(idx) {
  const feat = state.features[idx]
  if (!feat.isNew) { toast('warning', '原始数据列请在第一步「列管理」中删除'); return false }
  const d = ds()
  d.data.forEach(row => delete row[feat.key])
  state.features.splice(idx, 1)
  touch()
  logAction(5, 'delete', '撤销特征列', `"${feat.label}" (${feat.key})`,
    { type: 'feature_drop', key: feat.key, label: feat.label })
  return true
}

// ============================================================
// 操作日志：导出 / 导入 / 回放
// ============================================================
export function exportActionLog() {
  if (state.actionLog.length === 0) { toast('warning', '当前没有操作记录可导出'); return false }
  const d = ds()
  const exportData = {
    version: '1.0',
    exportedAt: new Date().toISOString(),
    datasetKey: state.currentKey,
    datasetName: d?.name || '',
    datasetRows: d?.data?.length || 0,
    datasetCols: d?.columns?.length || 0,
    operations: state.actionLog.map(e => ({
      step: e.step, icon: e.icon, title: e.title, detail: e.detail, params: e.params, time: e.time
    }))
  }
  downloadBlob(JSON.stringify(exportData, null, 2), `timeseries_pipeline_${new Date().toISOString().slice(0, 10)}.json`, 'application/json')
  toast('success', `已导出 ${exportData.operations.length} 条操作记录`)
  return true
}

export function importActionLog(text) {
  let data
  try {
    data = JSON.parse(text)
  } catch (e) {
    toast('error', `文件解析失败: ${e.message}`)
    return false
  }
  if (!data.operations || !Array.isArray(data.operations)) {
    toast('error', '文件格式错误：缺少 operations 数组')
    return false
  }
  state.importedLog = data
  state.actionLog = data.operations.map((op, idx) => ({
    id: 'imported_' + idx,
    step: op.step, icon: op.icon, title: op.title, detail: op.detail,
    params: op.params, time: op.time || '', ts: 0, imported: true
  }))
  toast('success', `已导入 ${data.operations.length} 条操作记录（${data.datasetName || data.datasetKey || '未知数据集'}），点击「回放执行」可复用此流程`)
  return true
}

// 回放单条操作；返回 true=已执行，false=跳过
function replaySingleOp(op) {
  const d = ds()
  if (!d || !op.params) return false
  const p = op.params
  switch (p.type) {
    case 'time_convert': {
      const timeCol = p.timeCol || d.timeCol
      const fmt = p.targetFmt || 'YYYY-MM-DD HH:mm:ss'
      d.data.forEach(row => {
        if (!isMissing(row[timeCol])) row[timeCol] = convertSingleTime(row[timeCol], fmt)
      })
      touch()
      logAction(2, 'column', '[回放] 时间格式转换', `${d.data.length}行 → ${fmt}`)
      return true
    }
    case 'resample': {
      if (!p.targetRate || !p.method) return false
      resampleDataset(p.targetRate, p.method)
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = '[回放] 重采样'
      return true
    }
    case 'multi_calc': {
      if (!p.terms || !p.name || p.terms.length < 2) return false
      if (d.columns.some(c => c.key === p.name)) return true // 已存在，视为成功
      applyDerivedCol(p.name, p.terms)
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = `[回放] 多列运算: ${p.name}`
      return true
    }
    case 'exo_merge': {
      if (!Array.isArray(p.vars)) return false
      p.vars.forEach(v => {
        if (!Array.isArray(v.data)) return
        if (!d.columns.some(c => c.key === v.key)) {
          d.columns.push({ key: v.key, label: v.label || v.key, type: 'float' })
        }
        d.data.forEach((row, i) => { row[v.key] = v.data[i] !== undefined ? v.data[i] : null })
      })
      touch()
      logAction(2, 'column', '[回放] 合并外生变量', p.vars.map(v => v.key).join(', '))
      return true
    }
    case 'impute_segment': {
      if (!p.colKey || !Array.isArray(p.segments)) return false
      p.segments.forEach(seg => {
        const fresh = segmentsOf(p.colKey).find(s => s.startIdx === seg.startIdx)
        if (fresh) imputeSegment(d.data, p.colKey, fresh, seg.algo || 'linear')
      })
      touch()
      logAction(4, 'impute', `[回放] 缺失值填补: ${p.colKey}`, `${p.segments.length} 段`)
      return true
    }
    case 'impute_all': {
      imputeAllAndDedupe('mean')
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = '[回放] 一键填补去重'
      return true
    }
    case 'anomaly_repair': {
      // 依据检测时缓存的真实阈值重新检测后修复（无法还原历史视图时跳过）
      if (!p.repair) return false
      const res = detectAnomalies(state.lastAnomaly?.algo || '3sigma', state.lastAnomaly?.expr)
      if (res.error) return false
      repairAnomalies(p.repair)
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = '[回放] 异常修复'
      return true
    }
    case 'mask_generate': {
      if (!p.maskName || p.startIdx === undefined || p.endIdx === undefined) return false
      generateMask(p.maskName, { startIdx: p.startIdx, endIdx: p.endIdx, start: p.startTime, end: p.endTime })
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = `[回放] 掩码: ${p.maskName}`
      return true
    }
    case 'rename_column': {
      if (!p.oldKey || !p.newLabel) return false
      const idx = d.columns.findIndex(c => c.key === p.oldKey)
      if (idx < 0) return false
      renameColumn(idx, p.newLabel)  // 与界面操作走同一函数：键名/时间列/行内数据一起改
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = '[回放] 重命名'
      return true
    }
    case 'delete_column': {
      if (!p.key) return false
      const idx = d.columns.findIndex(c => c.key === p.key)
      if (idx < 0) return true  // 列已不在，回放后状态与记录一致
      if (!deleteColumn(idx)) return false
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = '[回放] 删除列'
      return true
    }
    case 'feature_drop': {
      if (!p.key) return false
      const fi = state.features.findIndex(f => f.key === p.key)
      if (fi < 0) return true  // 该列已不在登记表里，回放后状态与记录一致
      if (!dropFeature(fi)) return false
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = '[回放] 撤销特征列'
      return true
    }
    case 'feature_rename': {
      if (!p.oldKey || !p.newLabel) return false
      let fi = state.features.findIndex(f => f.key === p.oldKey)
      if (fi < 0) {
        fi = state.features.findIndex(f => f.key === p.newKey)
        return fi >= 0  // 已按改名后的键存在，视为与记录一致
      }
      renameFeature(fi, p.newLabel)
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = '[回放] 重命名特征列'
      return true
    }
    case 'unit_convert': {
      if (!p.key || typeof p.factor !== 'number') return false
      const idx = d.columns.findIndex(c => c.key === p.key)
      if (idx < 0) return false
      convertColumnUnit(idx, p.factor, typeof p.offset === 'number' ? p.offset : 0, p.newUnit || '')
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = '[回放] 单位转换'
      return true
    }
    case 'exo_var_add': {
      if (p.kind === 'formula') {
        if (!p.key || !p.expr) return false
        const r = addExoFormula(p.key, p.expr)   // 公式对同一时间轴是确定性的，重算即等价
        if (!r || !r.ok) return false
        const last = state.actionLog[state.actionLog.length - 1]
        last.title = `[回放] 公式生成外生变量: ${p.key}`
        return true
      }
      const items = p.kind === 'preset'
        ? [{ key: p.key, label: p.label, source: 'preset', data: p.data }]
        : p.kind === 'file' ? (p.vars || []).map(v => ({ ...v, source: 'file' })) : null
      if (!items || items.length === 0) return false
      let added = 0
      // 预设模板含随机扰动、文件变量来自用户本地文件，两者都只能按导出时记录的数值逐值复原
      items.forEach(v => {
        if (!v.key || !Array.isArray(v.data) || state.exoVars.some(x => x.key === v.key)) return
        // 逐值复制：活数据不能与操作记录共用同一个数组，否则记录会被后续操作改写
        state.exoVars.push({ ...v, data: v.data.slice() })
        added++
      })
      touch()
      logAction(2, 'column', `[回放] 登记外生变量: ${items.map(v => v.key).join(', ')}`,
        `${added} 个 · 按导出记录的数值逐值复原（${p.kind === 'preset' ? '预设模板为模拟数据' : '文件来源'}）`)
      return true
    }
    case 'mask_delete': {
      if (!p.maskKey) return false
      const idx = state.masks.findIndex(m => m.key === p.maskKey)
      if (idx < 0) return true
      deleteMask(idx)
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = `[回放] 删除掩码列: ${p.maskKey}`
      return true
    }
    case 'mask_delete_all': {
      if (!Array.isArray(p.keys)) return false
      if (state.masks.length === 0) return true
      deleteAllMasks()
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = `[回放] 清空全部掩码`
      return true
    }
    case 'feature_build': {
      if (p.featureType === 'lag_roll' && p.detail) {
        // detail 里带完整计划（目标列/阶数/窗口/统计量/expanding/ewm span），复原后走同一个构建函数
        const plan = parseLagDetail(p.detail)
        if (plan.cols.length === 0) return false
        buildLagFeatures(plan.cols, {
          lags: plan.lags, windows: plan.windows, stats: plan.stats,
          expanding: plan.expanding, ewm: plan.ewm, ewmSpan: plan.ewmSpan
        })
      } else if (p.featureType === 'time' && p.detail) {
        // 日志记的是展开后的列计划（cyclical_hour 这类），还原成勾选态再复用同一构建函数
        const toks = p.detail.split(',').filter(Boolean)
        const cyc = toks.filter(t => t.startsWith('cyclical_'))
        buildTimeFeatures([
          ...toks.filter(t => !t.startsWith('cyclical_')),
          ...(cyc.length ? ['cyclical_sincos'] : [])
        ])
      } else if (p.featureType === 'diff_freq' && p.detail) {
        const plan = parseDiffDetail(p.detail)
        if (plan.cols.length === 0) return false
        buildDiffFeatures(plan.cols, plan)
      } else if (p.featureType === 'cat' && p.detail) {
        const plan = parseCatDetail(p.detail)
        if (plan.cols.length === 0) return false
        buildCatFeatures(plan.cols, plan.method)
      } else {
        // 未登记的构建类型一律按「无法回放」处理，绝不写一条假成功日志蒙过去
        return false
      }
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = `[回放] 特征工程 · ${p.featureType || ''}`
      touch()
      return true
    }
    case 'split': {
      if (typeof p.ratio !== 'number') return false
      lastLoggedSplit = null  // 回放必须留下条目，不能被「同一比例只记一次」合并掉
      setSplitRatio(p.ratio)  // 走同一入口：界面比例真的被还原
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = '[回放] 设置切分比例'
      return true
    }
    default:
      return false
  }
}

let replayTimer = null

export function replayActionLog() {
  if (!state.importedLog || !state.importedLog.operations) {
    toast('warning', '请先导入操作流程文件')
    return
  }
  const ops = state.importedLog.operations
  ElMessageBox.confirm(
    `即将回放 ${ops.length} 条操作记录，当前数据将被修改。确认继续？`,
    '回放确认',
    { type: 'warning', confirmButtonText: '开始回放', cancelButtonText: '取消' }
  ).then(() => {
    state.actionLog = []
    let idx = 0, succeeded = 0, failed = 0
    state.replayStatus = { text: `回放中 (1/${ops.length})...` }
    const runNext = () => {
      if (idx >= ops.length) {
        state.replayStatus = { text: `回放完成：${succeeded} 成功，${failed} 跳过` }
        touch()
        setTimeout(() => { state.replayStatus = null }, 3000)
        state.importedLog = null
        replayTimer = null
        return
      }
      const op = ops[idx]
      state.replayStatus = { text: `回放中 (${idx + 1}/${ops.length}): ${op.title}` }
      const before = state.actionLog.length
      try {
        if (replaySingleOp(op)) {
          succeeded++
          // 回放补写的记录要带上原参数，否则这条流程再导出 Python 时会整段消失
          if (op.params) {
            for (let k = before; k < state.actionLog.length; k++) {
              if (!state.actionLog[k].params) state.actionLog[k].params = op.params
            }
          }
        } else { failed++; logAction(op.step || 2, 'switch', `[跳过] ${op.title}`, '无法在当前数据状态下重放该操作') }
      } catch (e) {
        failed++
        logAction(op.step || 2, 'switch', `[失败] ${op.title}`, String(e.message || e))
      }
      idx++
      replayTimer = setTimeout(runNext, 80)
    }
    runNext()
  }).catch(() => {})
}

export function clearImportedLog() {
  state.importedLog = null
  state.replayStatus = null
  if (replayTimer) { clearTimeout(replayTimer); replayTimer = null }
}

// ============================================================
// Python Pipeline 代码生成（仅包含本次会话真实执行过的操作）
// ============================================================
// 外生变量公式（buildVarEvaluator 的白名单语言）→ pandas 向量表达式。
// 单遍替换标识符，避免 np.exp 里的 exp 被二次映射。
const PY_FORMULA_TOKENS = {
  PI: 'np.pi', E: 'np.e',
  sin: 'np.sin', cos: 'np.cos', tan: 'np.tan', abs: 'np.abs', sqrt: 'np.sqrt',
  log: 'np.log', log2: 'np.log2', log10: 'np.log10', exp: 'np.exp',
  pow: 'np.power', floor: 'np.floor', ceil: 'np.ceil', round: 'np.round',
  min: 'np.minimum', max: 'np.maximum', asin: 'np.arcsin', acos: 'np.arccos',
  atan: 'np.arctan', hypot: 'np.hypot',
  hour: '@@HOUR@@', day: '@@DAY@@', month: '@@MONTH@@', weekday: '@@WEEKDAY@@', idx: '@@IDX@@'
}

function pyVarFormula(expr, tc) {
  if (!expr || /&&|\|\||!|\?/.test(expr) || /Math\./.test(expr)) return null
  const mapped = String(expr).replace(/[A-Za-z_]\w*/g, t => (PY_FORMULA_TOKENS[t] !== undefined ? PY_FORMULA_TOKENS[t] : t))
  return mapped
    .replace(/@@HOUR@@/g, `(df['${tc}'].dt.hour + df['${tc}'].dt.minute / 60)`)
    .replace(/@@DAY@@/g, `df['${tc}'].dt.day`)
    .replace(/@@MONTH@@/g, `df['${tc}'].dt.month`)
    // JS getDay() 0=周日，pandas dayofweek 0=周一
    .replace(/@@WEEKDAY@@/g, `((df['${tc}'].dt.dayofweek + 1) % 7)`)
    .replace(/@@IDX@@/g, 'np.arange(len(df))')
}

export function generatePythonCode() {
  const d = ds()
  const timeCol = d.timeCol || 'timestamp'
  // 时间列可能被改过名：加载段要用文件里的原始列名，后续段落跟着改名时点换名
  let loadTimeCol = timeCol
  state.actionLog.slice().reverse().forEach(e => {
    const rp = e.params
    if (!rp || rp.type !== 'rename_column') return
    const nk = rp.newKey || String(rp.newLabel || '').toLowerCase().replace(/[^a-z0-9_]/g, '_')
    if (nk === loadTimeCol) loadTimeCol = rp.oldKey
  })
  let tc = loadTimeCol
  const lines = []
  lines.push('# TimeSeries Feature Engineering Pipeline')
  lines.push('# 由 TimeSeries Studio 根据本次会话真实执行的操作生成')
  lines.push('import pandas as pd')
  lines.push('import numpy as np')
  lines.push('')
  lines.push('# 0. 加载数据')
  const base = (d.name || 'dataset').replace(/[^\w.-]/g, '_')
  const fmt = d.format || 'csv'
  if (fmt === 'parquet') lines.push(`df = pd.read_parquet("${base}.parquet", engine="pyarrow")`)
  else if (fmt === 'feather') lines.push(`df = pd.read_feather("${base}.feather")`)
  else if (fmt === 'xlsx' || fmt === 'xls') lines.push(`df = pd.read_excel("${base}.xlsx", engine="openpyxl")`)
  else lines.push(`df = pd.read_csv("${base}.csv")`)
  lines.push(`df['${loadTimeCol}'] = pd.to_datetime(df['${loadTimeCol}'])`)
  lines.push(`df = df.sort_values('${loadTimeCol}').reset_index(drop=True)`)

  let section = 1
  const nextHeader = (title) => {
    lines.push('')
    lines.push(`# ${section}. ${title}`)
    section++
  }

  const ops = state.actionLog.filter(e => e.params && e.params.type)
  const done = new Set()
  // 特征列可以撤销后，同一份构建计划可能出现「建 → 撤 → 再建」。若不去掉重复构建的抑制，
  // pandas 侧就少一次建列、与界面当前列集合不符。dropsSeen 统计自上次建列块以来的撤销次数，
  // rebuildRound 用来区分不同轮次的同名 drop（同一列可以反复被建又被撤）。
  let dropsSeen = 0
  let rebuildRound = 0
  let dftHelpers = false
  ops.forEach(entry => {
    const p = entry.params
    if (p.type === 'rename_column' || p.type === 'feature_rename') {
      const newKey = p.newKey || String(p.newLabel || '').toLowerCase().replace(/[^a-z0-9_]/g, '_')
      const key = `rename:${p.oldKey}->${newKey}`
      // 特征列若只改显示名（未换键），pandas 侧无事可做
      if (!done.has(key) && newKey && newKey !== p.oldKey) {
        done.add(key)
        if (!done.has('rename_header')) {
          done.add('rename_header')
          nextHeader('列重命名')
        } else {
          lines.push('')
        }
        lines.push(`df = df.rename(columns={'${p.oldKey}': '${newKey}'})  # 界面显示名为 ${p.newLabel}`)
        if (p.oldKey === tc) tc = newKey
      }
      return
    }
    if (p.type === 'delete_column' || p.type === 'feature_drop') {
      if (!p.key) return
      // 同一列可能被反复建了又撤，pandas 侧只发一次 drop，否则第二次 KeyError
      const dkey = `drop:${rebuildRound}:${p.key}`
      if (done.has(dkey)) return
      done.add(dkey)
      if (p.type === 'feature_drop') dropsSeen++
      if (!done.has('delcol_header')) {
        done.add('delcol_header')
        nextHeader('列删除')
      } else {
        lines.push('')
      }
      lines.push(`df = df.drop(columns=['${p.key}'])  # ${p.type === 'feature_drop' ? '界面撤销的特征列' : '界面删除列'}「${p.label || p.key}」`)
    }
    if (p.type === 'unit_convert') {
      if (!p.key || typeof p.factor !== 'number') return
      if (!done.has('unit_header')) {
        done.add('unit_header')
        nextHeader('单位转换')
      } else {
        lines.push('')
      }
      const off = typeof p.offset === 'number' && p.offset !== 0 ? ` + ${p.offset}` : ''
      lines.push(`# ${p.oldUnit || '无单位'} → ${p.newUnit || '新单位'}：y = ${p.factor}x${off}`)
      lines.push(`df['${p.key}'] = np.round(df['${p.key}'] * ${p.factor}${off}, 4)  # 前端用 toFixed(4)（远离 0 进位），np.round 为银行家舍入，半数值末位可能差 0.0001`)
    }
    if (p.type === 'exo_var_add') {
      if (!done.has('exoadd_header')) {
        done.add('exoadd_header')
        nextHeader('外生变量登记')
      } else {
        lines.push('')
      }
      if (p.kind === 'formula') {
        const vec = pyVarFormula(p.expr, tc)
        if (vec) {
          lines.push(`# 公式 ${p.expr}（hour 为小数小时，weekday 按 JS 语义 0=周日）`)
          lines.push(`df['${p.key}'] = np.round(${vec}, 4)  # 前端对不可得值置空，pandas 表现为 NaN`)
        } else {
          lines.push(`# 公式列 ${p.key} = ${p.expr}`)
          lines.push(`# 该公式含 &&、||、! 或三元运算，JS 的短路语义无法安全向量化，pandas 端未复现，数值请按界面为准`)
        }
      } else if (p.kind === 'preset') {
        lines.push(`# 预设模板列 ${p.key}（模板 ${p.presetKey}）· ${(p.data || []).length} 行`)
        lines.push(`# 该列由前端按时间规律 + 随机扰动模拟生成（界面上已标注「模拟」），随机序列不随工程导出，`)
        lines.push(`# pandas 端无法复现同一数值；如需保留请先在界面导出结果宽表 CSV，再从该 CSV 读取此列。`)
      } else if (p.kind === 'file') {
        const vars = p.vars || []
        lines.push(`# 文件导入列：${p.fileName || '未命名文件'} → ${vars.map(v => v.key).join(', ')}（每列 ${(vars[0]?.data || []).length} 行）`)
        lines.push(`# 数值来自用户本地文件、并按时间戳与主表对齐后的结果，原始文件不在本脚本旁边时 pandas 无法复现。`)
        lines.push(`# 若要复现请补：other = pd.read_csv("<原文件>.csv"); other['${tc}'] = pd.to_datetime(other['${tc}'])`)
        lines.push(`#            df = df.merge(other, on='${tc}', how='left')`)
      }
    }
    if (p.type === 'mask_delete' || p.type === 'mask_delete_all') {
      const keys = p.type === 'mask_delete' ? [p.maskKey] : (p.keys || [])
      const alive = keys.filter(k => k)
      if (alive.length === 0) return
      if (!done.has('maskdel_header')) {
        done.add('maskdel_header')
        nextHeader('布尔掩码列删除')
      } else {
        lines.push('')
      }
      lines.push(`df = df.drop(columns=[${alive.map(k => `'${k}'`).join(', ')}])`)
    }
    if (p.type === 'time_convert' && !done.has('time_convert')) {
      done.add('time_convert')
      nextHeader('时间格式标准化')
      lines.push(`# 目标格式: ${p.targetFmt}`)
      lines.push(`df['${p.timeCol || tc}'] = pd.to_datetime(df['${p.timeCol || tc}']).dt.strftime('${pyStrftime(p.targetFmt)}')`)
      lines.push(`df['${p.timeCol || tc}'] = pd.to_datetime(df['${p.timeCol || tc}'])`)
    }
    if (p.type === 'resample' && !done.has('resample')) {
      done.add('resample')
      nextHeader(`重采样至 ${p.detail}`)
      const agg = p.method === 'interpolate' ? 'mean' : p.method
      lines.push(`num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]`)
      lines.push(`df = df.set_index('${tc}').resample('${p.targetRate}').${agg}()`)
      lines.push(`df = df.reset_index().dropna(subset=['${tc}'])`)
    }
    if (p.type === 'exo_merge' && !done.has('exo_merge')) {
      done.add('exo_merge')
      nextHeader('合并外生变量')
      lines.push(`# 已合并列: ${p.vars.map(v => v.key).join(', ')}`)
      lines.push(`# （外生变量数据已随主表合并，此处保留列）`)
      lines.push(`exo_cols = [${p.vars.map(v => `'${v.key}'`).join(', ')}]`)
      lines.push(`df = df[[c for c in df.columns if c in exo_cols or c == '${tc}'] + [c for c in df.columns if c not in exo_cols and c != '${tc}']]`)
    }
    if (p.type === 'multi_calc') {
      const key = 'multi_calc:' + p.name
      if (done.has(key)) return
      done.add(key)
      if (section === 1) nextHeader('列间运算衍生')
      else lines.push('')
      const chain = p.terms.map((t, i) => i === 0 ? `df['${t.col}']` : ` ${t.op} df['${t.col}']`).join('')
      lines.push(`df['${p.name}'] = ${chain}`)
    }
    if (p.type === 'impute_segment' || p.type === 'impute_all') {
      if (p.type === 'impute_all') {
        if (done.has('impute')) return
        done.add('impute')
        nextHeader('缺失值填补与去重')
        lines.push(`num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]`)
        lines.push(`df[num_cols] = df[num_cols].interpolate(method='linear').bfill().ffill()`)
        lines.push(`df = df.drop_duplicates(subset=['${tc}'], keep='first')`)
      } else {
        const key = 'impute:' + p.colKey
        if (done.has(key)) return
        done.add(key)
        if (section === 1) nextHeader('缺失值时序填补')
        const algo = p.segments[0]?.algo || 'linear'
        const pdMethod = algo === 'ffill' ? 'ffill' : algo === 'zero' ? 'zero' : 'linear'
        if (pdMethod === 'zero') lines.push(`df['${p.colKey}'] = df['${p.colKey}'].fillna(0)`)
        else if (pdMethod === 'ffill') lines.push(`df['${p.colKey}'] = df['${p.colKey}'].ffill()`)
        else lines.push(`df['${p.colKey}'] = df['${p.colKey}'].interpolate(method='linear')`)
      }
    }
    if (p.type === 'anomaly_repair' && !done.has('anomaly')) {
      done.add('anomaly')
      nextHeader(`异常检测与修复（${p.repair}）`)
      const an = p.anomaly || (state.lastAnomaly ? {
        algo: state.lastAnomaly.algo,
        engine: state.lastAnomaly.engine || null,
        params: state.lastAnomaly.params || null,
        columns: Object.entries(state.lastAnomaly.perColumn).filter(([, info]) => info.set.size > 0)
          .map(([key, info]) => ({ key, lower: Number(info.lower), upper: Number(info.upper), count: info.set.size }))
      } : null)
      if (!an || an.columns.length === 0) {
        lines.push('# 本次修复未留存检测边界数据，无可复现的判定条件')
      } else {
        const sklearn = an.engine === 'sklearn.IsolationForest'
        if (sklearn) {
          const contam = typeof an.params.contamination === 'number' ? an.params.contamination : "'auto'"
          lines.push('# 检测模型与后端 /api/anomaly/iforest 完全同款（scikit-learn IsolationForest）')
          lines.push('from sklearn.ensemble import IsolationForest')
          lines.push('')
          lines.push(`def _iforest_mask(series, n_estimators=${an.params.n_estimators}, contamination=${contam}, random_state=${an.params.random_state}):`)
          lines.push('    valid = series.dropna().to_frame()')
          lines.push('    model = IsolationForest(n_estimators=n_estimators, contamination=contamination,')
          lines.push('                            random_state=random_state, bootstrap=False).fit(valid)')
          lines.push('    mask = pd.Series(False, index=series.index)')
          lines.push('    mask.loc[valid.index] = model.predict(valid) == -1')
          lines.push('    return mask')
        } else {
          lines.push(`# 检测算法：${an.algo}（以下界/上界阈值复现）`)
        }
        an.columns.forEach((c, i) => {
          const lo = Number(c.lower).toFixed(4)
          const up = Number(c.upper).toFixed(4)
          if (sklearn) lines.push(`mask_${i} = _iforest_mask(df['${c.key}'])   # 检出 ${c.count} 点`)
          else lines.push(`mask_${i} = (df['${c.key}'] < ${lo}) | (df['${c.key}'] > ${up})   # 检出 ${c.count} 点`)
          if (p.repair === 'clip') lines.push(`df.loc[mask_${i}, '${c.key}'] = df['${c.key}'].clip(lower=${lo}, upper=${up})`)
          else if (p.repair === 'nan_impute') lines.push(`df.loc[mask_${i}, '${c.key}'] = np.nan`)
          else lines.push(`df['anomaly_mask_${c.key}'] = mask_${i}.astype(int)`)
        })
        if (p.repair === 'nan_impute') lines.push(`df = df.interpolate(method='linear')`)
        if (p.repair === 'mask_only') lines.push(`# mask_only：仅生成布尔掩码列，原始数据未被修改`)
      }
    }
    if (p.type === 'mask_generate') {
      const key = 'mask:' + p.maskName
      if (done.has(key)) return
      done.add(key)
      nextHeader(`布尔掩码 ${p.maskName}`)
      lines.push(`df['${p.maskName}'] = 0`)
      lines.push(`df.loc[${p.startIdx}:${p.endIdx}, '${p.maskName}'] = 1`)
    }
    if (p.type === 'feature_build') {
      const key = 'feat:' + p.featureType + ':' + (p.detail || '')
      const isDup = done.has(key)
      if (isDup && dropsSeen === 0) return
      if (isDup) {
        dropsSeen = 0
        rebuildRound++
        lines.push('')
        lines.push('# 以下块为重建：这组特征列在流程中被撤销过，需再次生成才与界面当前列集合一致')
      }
      done.add(key)
      if (p.featureType === 'time') {
        nextHeader('日历时间特征')
        const dims = (p.detail || '').split(',').filter(Boolean)
        // 与浏览器端 weekdayIndex/cyclical 同一套语义：pandas dayofweek 同样是周一=0
        const DIM_EXPR = {
          hour: `df['${tc}'].dt.hour`,
          day: `df['${tc}'].dt.day`,
          month: `df['${tc}'].dt.month`,
          weekday: `df['${tc}'].dt.dayofweek`,
          is_weekend: `(df['${tc}'].dt.dayofweek >= 5).astype(int)`
        }
        const CYCLE_EXPR = {
          hour: `df['${tc}'].dt.hour / 24.0`,
          weekday: `df['${tc}'].dt.dayofweek / 7.0`,
          month: `(df['${tc}'].dt.month - 1) / 12.0`
        }
        dims.forEach(opt => {
          if (opt === 'holiday') {
            lines.push(`HOLIDAYS = {${[...HOLIDAYS_2024].map(h => `'${h}'`).join(', ')}}`)
            lines.push(`df['feat_holiday'] = df['${tc}'].dt.strftime('%Y-%m-%d').isin(HOLIDAYS).astype(int)`)
            return
          }
          if (opt.startsWith('cyclical_')) {
            const dim = opt.slice('cyclical_'.length)
            const x = CYCLE_EXPR[dim]
            if (!x) return
            lines.push(`df['feat_${dim}_sin'] = np.sin(2 * np.pi * ${x})`)
            lines.push(`df['feat_${dim}_cos'] = np.cos(2 * np.pi * ${x})`)
            return
          }
          if (DIM_EXPR[opt]) lines.push(`df['feat_${opt}'] = ${DIM_EXPR[opt]}`)
        })
        if (dims.some(o => o.startsWith('cyclical_')) && dims.includes('day')) {
          lines.push(`# 注：day 每月天数不固定（28~31），无周期可言，故未做正余弦编码`)
        }
      }
      if (p.featureType === 'lag_roll') {
        nextHeader(`滞后与滑动窗口特征（${p.detail}）`)
        const plan = parseLagDetail(p.detail)
        lines.push(`# 窗口类特征统一 .shift(1)：只使用当前时刻之前的数据，防止标签泄漏`)
        lines.push(`# 注：滚动窗口内含缺失时 pandas 会传播 NaN，请先完成缺失值填补再执行本段`)
        plan.cols.forEach(col => {
          plan.lags.forEach(s => lines.push(`df['lag_${col}_t${s}'] = df['${col}'].shift(${s})`))
          plan.windows.forEach(w => plan.stats.forEach(fn => {
            const agg = fn === 'std' ? '.std(ddof=0)' : `.${fn}()`
            lines.push(`df['roll_${ROLL_STATS[fn]}_${col}_w${w}'] = df['${col}'].rolling(${w}, min_periods=${w})${agg}.shift(1)`)
          }))
          if (plan.expanding) lines.push(`df['expanding_mean_${col}'] = df['${col}'].expanding().mean().shift(1)`)
          if (plan.ewm) lines.push(`df['ewm_${col}_s${plan.ewmSpan}'] = df['${col}'].ewm(span=${plan.ewmSpan}, adjust=False).mean().shift(1)`)
        })
      }
      if (p.featureType === 'diff_freq') {
        const plan = parseDiffDetail(p.detail)
        nextHeader(`差分与频域特征（${p.detail}）`)
        if (plan.cols.length === 0) {
          lines.push('# 该次构建未登记任何目标列，无可导出语句')
          return
        }
        lines.push('# 前端对 null 与 pandas 对 NaN 等价：差分段前若干行为空属正常')
        lines.push('# 小数位：前端 toFixed（四舍五入）、numpy round（半偶舍入），仅末位边界值可能有 1 个最小单位的差异')
        plan.cols.forEach(col => {
          if (plan.d1) lines.push(`df['diff1_${col}'] = df['${col}'].diff(1).round(4)`)
          if (plan.d2) lines.push(`df['diff2_${col}'] = df['${col}'].diff(1).diff(1).round(4)`)
          if (plan.seasonal) lines.push(`df['diff_season${plan.period}_${col}'] = df['${col}'].diff(${plan.period}).round(4)`)
        })
        const fftOn = plan.fftDominant || plan.fftEntropy || plan.fftPowerRatio
        if (fftOn) {
          const fcol = plan.cols[0]
          if (!dftHelpers) {
            dftHelpers = true
            lines.push('')
            lines.push('# ---- DFT 工具：与前端 computeDFT 同款 |Σ xₙ·e^(-i2πkn/N)| / N，缺失按 0 参与计算 ----')
            lines.push('def _dft_mag(x, k):')
            lines.push('    x = np.asarray(x, dtype=float)')
            lines.push('    m = len(x)')
            lines.push('    idx = np.arange(m)')
            lines.push('    ang = -2 * np.pi * k * idx / m')
            lines.push('    return float(np.hypot((x * np.cos(ang)).sum(), (x * np.sin(ang)).sum())) / m')
            lines.push('')
            lines.push('def _spectral_entropy(x):')
            lines.push('    e = np.array([_dft_mag(x, k) for k in range(1, 17)])')
            lines.push('    p = e / (e.sum() or 1.0)')
            lines.push('    p = p[p > 0]')
            lines.push('    return round(float(-(p * np.log2(p)).sum()), 4)')
            lines.push('')
            lines.push('def _power_ratio(x):')
            lines.push('    low = sum(_dft_mag(x, k) for k in range(1, 5))')
            lines.push('    high = sum(_dft_mag(x, k) for k in (8, 12, 16))')
            lines.push('    return round(low / (high + 1e-10), 2)')
          }
          lines.push('')
          lines.push(`# ---- 频域特征（在 ${fcol} 上逐点滑窗 DFT，窗口 64 点）----`)
          lines.push(`FFT_COL = '${fcol}'`)
          lines.push(`vals = pd.to_numeric(df[FFT_COL], errors='coerce').fillna(0).to_numpy(dtype=float)`)
          lines.push('n = len(vals)')
          lines.push('W = 64')
          if (plan.fftDominant) {
            lines.push('# 主导频率在整个序列上评选（k = 1..min(50, n//2) 取幅值最大的 3 个），再逐点用截至当前行的窗口取值')
            lines.push('spectrum = [{"k": k, "energy": _dft_mag(vals, k)} for k in range(1, min(50, n // 2) + 1)]')
            lines.push('top3 = sorted(spectrum, key=lambda s: -s["energy"])[:3]')
            lines.push('for rank, item in enumerate(top3, start=1):')
            lines.push('    df[f"fft_top{rank}_{FFT_COL}"] = [round(_dft_mag(vals[max(0, i - W):i + 1], item["k"]), 2) for i in range(n)]')
          }
          if (plan.fftEntropy) {
            lines.push(`df['fft_entropy_${fcol}'] = [np.nan if i < W else _spectral_entropy(vals[i - W:i]) for i in range(n)]`)
          }
          if (plan.fftPowerRatio) {
            lines.push(`df['fft_power_ratio_${fcol}'] = [np.nan if i < W else _power_ratio(vals[i - W:i]) for i in range(n)]`)
          }
        }
      }
      if (p.featureType === 'cat') {
        const plan = parseCatDetail(p.detail)
        nextHeader(`类别编码（${p.detail}）`)
        const cols = plan.cols.filter(k => d.columns.some(c => c.key === k))
        if (cols.length === 0) {
          lines.push('# 该次构建登记的类别列在当前数据中已不存在，无可导出语句')
          return
        }
        if (plan.method === 'onehot') {
          const names = catFeaturePlan(cols, 'onehot').map(it => it.key)
          lines.push(`# 独热列名 = <列名>_<取值>；前端保留原类别列并追加 0/1 列，所以只对副本做 get_dummies 再拼回`)
          lines.push(`# astype('object') 保证只对选中的列编码（数值型类别列也会被当作离散取值处理），缺失行全为 0，与前端一致`)
          lines.push(`_dummies = pd.get_dummies(df[[${cols.map(c => `'${c}'`).join(', ')}]].astype('object'), prefix_sep='_').astype(int)`)
          lines.push(`df = pd.concat([df, _dummies[[${names.map(x => `'${x}'`).join(', ')}]]], axis=1)  # 按取值首次出现顺序排列，与前端宽表同序`)
        } else if (plan.method === 'ordinal') {
          lines.push(`# categories 按取值首次出现顺序编号（与前端一致，非字典序）；未登记取值 → -1`)
          cols.forEach(catCol => {
            const uniq = [...new Set(d.data.map(r => r[catCol]).filter(v => !isMissing(v)))]
            lines.push(`df['${catCol}_ordinal'] = df['${catCol}'].astype(pd.CategoricalDtype([${uniq.map(v => `'${v}'`).join(', ')}])).cat.codes`)
          })
        } else {
          const mainFloat = (d.columns.find(c => c.type === 'float' && c.isMain)
            || d.columns.find(c => c.type === 'float' && !c.key.endsWith('_ordinal') && !c.key.endsWith('_target')))?.key
          if (!mainFloat) {
            lines.push('# 目标编码需要数值目标列，当前数据无可用浮点列，前端该次构建未产出有效值')
          } else {
            lines.push(`# 目标编码 = 该类别下 ${mainFloat} 的均值（有标签泄漏风险，仅供探索）`)
            cols.forEach(catCol => {
              lines.push(`df['${catCol}_target'] = df.groupby('${catCol}')['${mainFloat}'].transform('mean').round(2)`)
            })
          }
        }
      }
    }
    // 切分不在这里发射：见下方按「最后一次设置」统一处理
  })

  // 切分是收尾动作：按最后一次真实设置的比例，在最终矩阵上发射
  const splits = state.actionLog.filter(e => e.params && e.params.type === 'split')
  if (splits.length > 0) {
    const ratio = splits[splits.length - 1].params.ratio
    nextHeader(`训练/验证/测试切分（训练 ${ratio}%，时序不打乱）`)
    const s = splitCounts(ratio)
    lines.push(`train = df.iloc[:${s.train}]`)
    lines.push(`val   = df.iloc[${s.train}:${s.train + s.val}]`)
    lines.push(`test  = df.iloc[${s.train + s.val}:]`)
  }

  const exported = state.actionLog.filter(e => e.params && e.params.type === 'export')
  if (exported.length > 0) {
    nextHeader('导出清洗结果')
    exported.forEach(e => {
      const f = e.params.format
      lines.push(`# ${e.title} · ${e.detail}`)
      if (f === 'parquet') lines.push(`df.to_parquet("${base}_export.parquet", engine="pyarrow", index=False)`)
      else if (f === 'feather') lines.push(`df.to_feather("${base}_export.feather")`)
      else if (f === 'csv') lines.push(`df.to_csv("${base}_export.csv", index=False, encoding="utf-8-sig")`)
      else if (f === 'xlsx') lines.push(`df.to_excel("${base}_export.xlsx", index=False, sheet_name="data")`)
    })
  }

  lines.push('')
  lines.push(`print("Pipeline executed. Matrix shape:", df.shape)`)
  lines.push(`df.to_csv("dataset_features.csv", index=False)`)
  return lines.join('\n')
}

function pyStrftime(fmt) {
  return String(fmt)
    .replace(/YYYY/g, '%Y').replace(/MM/g, '%m').replace(/DD/g, '%d')
    .replace(/HH/g, '%H').replace(/mm/g, '%M').replace(/ss/g, '%S')
}

// ============================================================
// 撤销 / 重做 / 会话持久化（快照式）
// ============================================================
// 为什么是快照而不是逐条逆操作：本工作区过半动作不可逆（重采样、批量填补、撤销特征列、
// 掩码抹除），给每个操作补 inverse 一定会漏，漏掉的那条就是"撤销完数据悄悄错掉"。
// 代价是内存，所以设了单元格护栏：超限的数据集不建撤销点，并把这个决定当面告诉用户。
const HISTORY_LIMIT = 25
const SNAPSHOT_CELL_LIMIT = 400000
// 撤销栈双重限长：只按条数限的话，25 份 40 万格快照就是千万格，内存会失控，
// 所以再加一条总单元格预算，超预算从最旧的撤销点开始丢。
const HISTORY_CELL_BUDGET = 1500000
const SESSION_KEY = 'tss.session.v1'

let undoStack = []
let redoStack = []
let baseline = null      // 永远等于"当前状态"的快照；下一次变更时它就是撤销目标
let baselineCells = 0
let applying = false     // 还原快照自身会 bump dataVersion，必须让 watcher 跳过这一次
let lastLogLen = 0

function trimHistory(stack) {
  while (stack.length > HISTORY_LIMIT) stack.shift()
  let total = stack.reduce((n, e) => n + (e.cells || 0), 0)
  while (stack.length > 1 && total > HISTORY_CELL_BUDGET) total -= (stack.shift().cells || 0)
}

function cellCount() {
  const d = ds()
  return d && d.data ? d.data.length * d.columns.length : 0
}

// 不能用 structuredClone：state 是 reactive 的，取出来的 features/masks/... 是 Proxy，
// 浏览器会以 DataCloneError 拒绝克隆（实测会让 mounted 与 watcher 双双抛错、撤销点全丢）。
// 这里自己按层 toRaw 解开，顺带保住 Set / Date 这两类 JSON 存不下的值。
function deepClone(v) {
  const raw = toRaw(v)
  if (raw === null || typeof raw !== 'object') return raw
  if (raw instanceof Date) return new Date(raw.getTime())
  if (raw instanceof Set) return new Set(Array.from(raw, deepClone))
  if (raw instanceof Map) return new Map(Array.from(raw, ([k, x]) => [deepClone(k), deepClone(x)]))
  if (Array.isArray(raw)) return raw.map(deepClone)
  const o = {}
  for (const k of Object.keys(raw)) o[k] = deepClone(raw[k])
  return o
}

function takeSnapshot() {
  return deepClone({
    currentKey: state.currentKey,
    splitRatio: state.splitRatio,
    datasets,
    exoVars: state.exoVars,
    derivedCols: state.derivedCols,
    masks: state.masks,
    features: state.features,
    imputeSegAlgos: state.imputeSegAlgos,
    lastAnomaly: state.lastAnomaly,
    actionLog: state.actionLog
  })
}

// 还原时一律再克隆一份：活数据不能与快照共用同一个对象，否则下一次修改会改写历史
function restoreSnapshot(s) {
  const restored = deepClone(s)
  Object.keys(datasets).forEach(k => { delete datasets[k] })
  Object.assign(datasets, restored.datasets)
  state.currentKey = restored.currentKey
  state.splitRatio = restored.splitRatio
  lastLoggedSplit = restored.splitRatio
  state.exoVars = restored.exoVars
  state.derivedCols = restored.derivedCols
  state.masks = restored.masks
  state.features = restored.features
  state.imputeSegAlgos = restored.imputeSegAlgos
  state.lastAnomaly = restored.lastAnomaly
  state.actionLog = restored.actionLog
}

function syncHistoryCounters() {
  state.history.undo = undoStack.length
  state.history.redo = redoStack.length
  state.history.undoLabel = undoStack.length ? undoStack[undoStack.length - 1].label : ''
  state.history.redoLabel = redoStack.length ? redoStack[redoStack.length - 1].label : ''
}

// flush: 'sync' 是必需的——applying 标志只在 touch() 的那一帧有效，
// 默认的 pre 调度会在标志复位之后才回调，撤销自身就会被记成一个新的撤销点
let coalescing = false

// 一次用户操作的收尾（微任务）：store 里 touch() 与 logAction() 的先后顺序不统一（重命名就是
// 先改数据再记录），所以撤销点的标题要等记录写完再取，baseline 也要等到这一刻才是"操作结束态"。
// 若沿用同步重取 baseline，下一个撤销点会少掉上一条操作，撤销时会连带丢审计记录。
function finalizeHistoryPoint() {
  coalescing = false
  const top = undoStack[undoStack.length - 1]
  if (top && top.fromLogLen <= state.actionLog.length) {
    const written = state.actionLog.slice(top.fromLogLen)
    if (written.length) top.label = written[written.length - 1].title
  }
  baseline = takeSnapshot()
  baselineCells = cellCount()
  lastLogLen = state.actionLog.length
  syncHistoryCounters()
  scheduleSessionSave()
}

watch(() => state.dataVersion, () => {
  if (applying) return
  const cells = cellCount()
  if (cells > SNAPSHOT_CELL_LIMIT) {
    if (state.history.enabled) {
      state.history.enabled = false
      toast('warning', `当前 ${cells.toLocaleString()} 单元格超过撤销快照上限 ${SNAPSHOT_CELL_LIMIT.toLocaleString()}，` +
        `本数据集不再记录撤销点（大数组的增量快照需另做，见性能项）`)
    }
    undoStack = []; redoStack = []; baseline = null; baselineCells = 0
    syncHistoryCounters()
    return
  }
  state.history.enabled = true
  // 一次用户操作里可能连着多次 touch（多列批量处理），撤销粒度必须是"操作"而不是"touch 次数"，
  // 否则会出现按一次撤销什么都没变。微任务结束前都算同一次操作。
  if (coalescing) return
  if (!baseline) { finalizeHistoryPoint(); return }
  undoStack.push({ label: '数据变更', fromLogLen: lastLogLen, snap: baseline, cells })
  trimHistory(undoStack)
  redoStack = []
  coalescing = true
  queueMicrotask(finalizeHistoryPoint)
}, { flush: 'sync' })

export function undo() {
  const entry = undoStack.pop()
  if (!entry) { toast('info', '没有可撤销的变更'); return false }
  // 用当前活状态开一份"重做用"快照，不能拿 baseline 顶替：baseline 是上一次操作结束时的状态，
  // 对先改数据后写记录的操作来说，它还缺这条操作的审计记录。
  const current = takeSnapshot(), currentCells = cellCount()
  applying = true
  restoreSnapshot(entry.snap)
  // 快照可能来自"第五步还没打开过"的时刻，登记表要按真实列补齐，否则特征预览会显示 0 列
  initFeatureList()
  touch()
  applying = false
  redoStack.push({ label: entry.label, snap: current, cells: currentCells })
  trimHistory(redoStack)
  baseline = entry.snap
  baselineCells = entry.cells
  lastLogLen = state.actionLog.length
  syncHistoryCounters()
  scheduleSessionSave()
  toast('success', `已撤销：${entry.label}`)
  return true
}

export function redo() {
  const entry = redoStack.pop()
  if (!entry) { toast('info', '没有可重做的变更'); return false }
  const current = takeSnapshot(), currentCells = cellCount()
  applying = true
  restoreSnapshot(entry.snap)
  initFeatureList()
  touch()
  applying = false
  undoStack.push({ label: entry.label, snap: current, cells: currentCells })
  trimHistory(undoStack)
  baseline = entry.snap
  baselineCells = entry.cells
  lastLogLen = state.actionLog.length
  syncHistoryCounters()
  scheduleSessionSave()
  toast('success', `已重做：${entry.label}`)
  return true
}

// ---- 会话持久化：浏览器本地存储，重启页面后询问是否恢复 ----
// 只存"当前状态"，不存撤销栈（体积翻倍且跨会话无意义）；
// lastAnomaly 里有 Set 与检测结果，JSON 存不下，因此不随会话恢复，重跑一次检测即可。
let saveTimer = null
function scheduleSessionSave() {
  if (!state.session.enabled) return
  clearTimeout(saveTimer)
  saveTimer = setTimeout(saveSession, 800)
}

export function saveSession() {
  if (!state.session.enabled || !baseline) return false
  // 存"当前活状态"而不是 baseline：baseline 可能是上一次操作结束时的快照，
  // 对先改数据后写记录的操作会少一条审计记录。
  const snap = takeSnapshot()
  snap.lastAnomaly = null
  const d = ds()
  const payload = JSON.stringify({
    version: 1,
    savedAt: new Date().toISOString(),
    meta: {
      name: d?.name || '', rows: d?.data?.length || 0, cols: d?.columns?.length || 0,
      ops: snap.actionLog.length, step: state.currentStep
    },
    snap
  })
  try {
    localStorage.setItem(SESSION_KEY, payload)
    state.session.savedAt = new Date().toLocaleTimeString()
    return true
  } catch (e) {
    state.session.enabled = false
    toast('warning', e.name === 'QuotaExceededError'
      ? '浏览器本地存储配额已满，会话持久化已关闭；请改用「导出流程」JSON + 重新导入数据集复现'
      : `会话持久化写入失败：${e.message}`)
    return false
  }
}

let pendingSession = null

// 页面启动时调用：只读出元信息挂成一条询问，不擅自把工作区替换掉
export function initHistoryAndSession() {
  baseline = cellCount() <= SNAPSHOT_CELL_LIMIT ? takeSnapshot() : null
  baselineCells = baseline ? cellCount() : 0
  lastLogLen = state.actionLog.length
  syncHistoryCounters()
  let raw = null
  try { raw = localStorage.getItem(SESSION_KEY) } catch (e) { state.session.enabled = false; return }
  if (!raw) return
  let parsed
  try { parsed = JSON.parse(raw) } catch (e) { clearSession(); return }
  if (!parsed || !parsed.snap || parsed.version !== 1) { clearSession(); return }
  pendingSession = parsed
  state.session.found = { ...parsed.meta, savedAt: parsed.savedAt }
}

export function restoreSession() {
  if (!pendingSession) return false
  const s = pendingSession
  applying = true
  restoreSnapshot({ ...s.snap, lastAnomaly: null })
  initFeatureList()
  touch()
  applying = false
  baseline = takeSnapshot()
  baselineCells = cellCount()
  undoStack = []; redoStack = []
  lastLogLen = state.actionLog.length
  syncHistoryCounters()
  switchStep(s.meta?.step || 1)
  const t = new Date(s.savedAt)
  toast('success', `已恢复上次会话：${s.meta.name} · ${s.meta.rows.toLocaleString()} 行 × ${s.meta.cols} 列 · ${s.meta.ops} 条操作` +
    `（保存于 ${isNaN(t.getTime()) ? '' : t.toLocaleString()}，异常检测结果与撤销栈不跨会话）`)
  pendingSession = null
  state.session.found = null
  return true
}

export function clearSession() {
  pendingSession = null
  state.session.found = null
  try { localStorage.removeItem(SESSION_KEY) } catch (e) { /* 隐私模式下写入本就不可用 */ }
}
