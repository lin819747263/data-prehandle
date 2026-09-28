<script setup>
import { ref, reactive, computed, onMounted, onActivated, onBeforeUnmount, nextTick, watch } from 'vue'
import * as echarts from 'echarts'
import { ElMessageBox } from 'element-plus'
import {
  state, ds, switchStep, toast, requireBackend, sourceSig,
  loadQuality, qualityData, columnMissingStats, segmentsOf, segmentsTruncated,
  segAlgo, setSegAlgo, applySegmentImpute, applyAllSegmentsImpute, imputeAllAndDedupe,
  detectAnomalies, repairAnomalies, loadSeries,
  ANOMALY_ALGOS, ANOMALY_REPAIRS, ANOMALY_NAMES,
  generateMask, deleteMask, deleteAllMasks
} from '../store'
import { buildExprEvaluator } from '../utils'
import { checkBackend } from '../api'

const IMPUTE_ALGOS = [
  { value: 'linear', label: '时序线性插值' },
  { value: 'ffill', label: '前向观测值填充' },
  { value: 'spline', label: '三次样条平滑' },
  { value: 'zero', label: '常数0置换' }
]

// 换新引用才能失效下游 computed（见 Step2Config 同款注释）
const d = computed(() => { void state.dataVersion; return { ...ds() } })
// 本步的每个数字都来自后端：诊断走 GET /quality，曲线走 GET /series，
// 浏览器不再持有整表，所以 40 万格的表也能开这一页。
const stats = computed(() => { void state.dataVersion; return columnMissingStats() })
const diagError = computed(() => {
  void state.dataVersion
  if (!state.backend.online) return '后端未连接：本步的缺失扫描、填补、异常检测与掩码全部在后端执行，浏览器不再算第二套，页面转为只读。'
  if (!d.value.wsId) return '还没有载入数据集：请回第一步加载数据后再做质量诊断。'
  if (state.quality.error) return `诊断读取失败：${state.quality.error}`
  return ''
})
const tab = ref('impute')
const TAB_COLORS = {
  impute: 'border-amber-500 text-amber-700 bg-white',
  anomaly: 'border-rose-500 text-rose-700 bg-white',
  mask: 'border-teal-500 text-teal-700 bg-white'
}

// ============ 图表（服务端整表回传的全量曲线，多列共享一条时间轴）============
const COLORS = ['#4f46e5', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16', '#f97316', '#6366f1', '#14b8a6', '#e11d48']
const colorOf = i => COLORS[i % COLORS.length]
// 颜色按「这一列在所有数值列里的位置」定，勾掉中间一列时其余列的颜色不会串位
const colorFor = key => {
  const i = floatCols.value.findIndex(c => c.key === key)
  return colorOf(i < 0 ? 0 : i)
}

const chartEl = ref(null)
let chart = null
const brushActive = ref(false)
const brushRange = ref(null)
const sd = ref(null)          // 当前勾选列的 /series 响应：{ x, idx, series:[{col,y,missingMarks,anomalies…}] }
// 曲线取数失败（例如全量档撞上后端格子数上限）：图会空，原因得写在页面上
const seriesError = computed(() => {
  void state.dataVersion
  return state.series.error || ''
})

const floatCols = computed(() => d.value.columns.filter(c => c.type === 'float'))
const selected = reactive(new Set())
function ensureSelection() {
  if (selected.size === 0) floatCols.value.slice(0, 2).forEach(c => selected.add(c.key))
  Array.from(selected).forEach(k => { if (!floatCols.value.some(c => c.key === k)) selected.delete(k) })
}
ensureSelection()
watch(floatCols, ensureSelection)

function toggleCol(key) {
  if (selected.has(key)) selected.delete(key)
  else selected.add(key)
}
function toggleAll(on) {
  selected.clear()
  if (on) floatCols.value.forEach(c => selected.add(c.key))
}
const selectedCols = computed(() => floatCols.value.filter(c => selected.has(c.key)))
// 只有列集合真的变了才重新取数（勾选顺序不同不该多打一次后端）
const selectionKey = computed(() => selectedCols.value.map(c => c.key).join('|'))

// 覆盖层/虚线画不全时逐列汇总：超出每列预算（以及后端真抽了点时的抽稀）都算「图上没有、数据里有」。
// 叠 39 列时逐列报会把这一行撑成十几屏，所以点名前 NOTE_COLS 列，但总数一律给全。
const NOTE_COLS = 5
// 折线默认全量（后端 points=0，一行不抽）；只有后端不认全量档、退回抽稀时 decimated 才为真，
// 界面那句「画不全的原因」必须跟着它写，不许在整表都画出来的图上还提降采样。
const decimated = computed(() => !!(sd.value && sd.value.decimated))
function capNote(field, fallback) {
  const caps = sd.value && sd.value.overlayCaps
  return caps && typeof caps[field] === 'number' ? caps[field] : fallback
}
function summarizeNote(rows) {
  const bad = rows.filter(r => r.truncated)
  if (!bad.length) return null
  return {
    shown: bad.slice(0, NOTE_COLS),
    hidden: Math.max(0, bad.length - NOTE_COLS),
    allCols: bad.length,
    // 分子分母都按勾选的全部列汇总，不然「261/1103」会被读成只统计了点名这几列
    drawn: rows.reduce((n, r) => n + r.drawn, 0),
    total: rows.reduce((n, r) => n + r.total, 0)
  }
}
const overlayNote = computed(() => {
  const s = sd.value
  if (!s || !s.series) return null
  const rows = s.series.map(m => ({
    label: m.label || m.col, total: m.anomalyCount, drawn: m.anomalies.length, truncated: m.anomaliesTruncated
  }))
  return summarizeNote(rows)
})
const marksNote = computed(() => {
  const s = sd.value
  if (!s || !s.series) return null
  const rows = s.series.map(m => ({
    label: m.label || m.col, total: m.missingCount, drawn: m.missingMarks.length, truncated: m.marksTruncated
  }))
  return summarizeNote(rows)
})
const staleChart = computed(() => !!(sd.value && sd.value.anomaly && sd.value.anomaly.stale))
// 页头那句点数说明必须与后端回的一句话对上：全量就说「N 行 = N 点」，抽了才报抽稀。
const seriesChip = computed(() => {
  const s = sd.value
  if (!s) return ''
  const cols = selectedCols.value.length
  if (s.decimated) return `服务端降采样 ${s.x.length.toLocaleString()} 点 / ${s.rowCount.toLocaleString()} 行 · ${cols} 列`
  return `全量 ${s.x.length.toLocaleString()} 点 = ${s.rowCount.toLocaleString()} 行 · ${cols} 列（服务端整表回传，一个点都不抽）`
})

async function renderChart() {
  if (!chart) return
  const s = sd.value
  if (!s || !s.x?.length || !s.series?.length) { chart.clear(); return }
  const det = detection.value
  const names = s.series.map(m => m.label || m.col)
  const lines = s.series.map((m) => {
    const color = colorFor(m.col)
    return {
      name: m.label || m.col,
      type: 'line',
      data: m.y,
      showSymbol: false,
      lineStyle: { color, width: 1.5 },
      itemStyle: { color },
      // 缺失标记按列着色：折线在空洞处断开，虚线告诉你洞在哪一段时间上
      markLine: {
        symbol: 'none', silent: true,
        data: (m.missingMarks || []).map(t => ({ xAxis: t })),
        lineStyle: { color, type: 'dotted', width: 1.5 },
        label: { show: false }
      }
    }
  })
  const dots = s.series.filter(m => (m.anomalies || []).length).map((m) => ({
    name: `${m.label || m.col} · 异常`,
    type: 'scatter',
    data: m.anomalies,
    symbolSize: 9,
    itemStyle: { color: colorFor(m.col), borderColor: '#fff', borderWidth: 1.5 },
    z: 5
  }))
  chart.setOption({
    title: {
      text: `数据质量图示与异常探针 (${names.join('、')})` +
        `${det ? ` · ${ANOMALY_NAMES[det.algo] || det.algo}` : ' · 尚未执行异常检测'}`,
      left: 10, top: 5, textStyle: { fontSize: 12, color: '#334155' }
    },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross' },
      formatter(params) {
        if (!params || params.length === 0) return ''
        let tip = `<div style="font-size:11px"><b>${params[0].axisValue}</b><br/>`
        params.forEach(p => {
          const val = p.value !== null && p.value !== undefined ? Number(p.value).toFixed(2) : '缺失'
          tip += `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${p.color};margin-right:4px"></span>${p.seriesName}: <b>${val}</b><br/>`
        })
        return tip + '</div>'
      }
    },
    toolbox: { feature: { brush: { type: ['lineX', 'clear'] } }, right: 20, top: 5 },
    brush: { toolbox: ['lineX', 'clear'], xAxisIndex: 0 },
    legend: { top: 24, left: 10, textStyle: { fontSize: 10 }, itemWidth: 14, itemHeight: 8, type: 'scroll' },
    grid: { top: 58, right: 25, bottom: 50, left: 55 },
    dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 5, height: 20 }],
    xAxis: { type: 'category', data: s.x },
    yAxis: { type: 'value', splitLine: { lineStyle: { type: 'dashed' } } },
    series: [...lines, ...dots]
  }, true)
  applyBrushCursor()
}

function applyBrushCursor() {
  if (!chart) return
  chart.dispatchAction({
    type: 'takeGlobalCursor', key: 'brush',
    brushOption: brushActive.value ? { brushType: 'lineX', brushMode: 'single' } : { brushType: false }
  })
}
function toggleBrush() {
  brushActive.value = !brushActive.value
  applyBrushCursor()
}
function clearBrush() {
  chart && chart.dispatchAction({ type: 'brush', areas: [] })
  brushRange.value = null
}

// 刷选给出的是抽稀序列里的下标，必须换算回真实行号才能喂给服务端掩码
function onBrushSelected(params) {
  const batch = params.batch && params.batch[0]
  if (!batch || !batch.areas || batch.areas.length === 0) return
  const s = sd.value
  if (!s || !s.idx || s.idx.length === 0) return
  const range = batch.areas[0].coordRange
  const first = Math.max(0, Math.min(s.idx.length - 1, Math.floor(Math.min(range[0], range[1]))))
  const last = Math.max(0, Math.min(s.idx.length - 1, Math.ceil(Math.max(range[0], range[1]))))
  brushRange.value = {
    startIdx: s.idx[first], endIdx: s.idx[last],
    start: s.x[first], end: s.x[last]
  }
}

// ============ 诊断数据刷新 ============
let refreshing = null
async function refreshAll() {
  if (refreshing) return refreshing
  refreshing = (async () => {
    await loadQuality()
    await ensureSeries()
    await nextTick()
    renderChart()
  })().finally(() => { refreshing = null })
  return refreshing
}

async function ensureSeries() {
  const keys = selectedCols.value.map(c => c.key)
  // 0 = 全量档：后端把整表每一行都送回折线，一个点都不抽
  sd.value = keys.length ? await loadSeries(keys, 0) : null
}

// ============ Tab1 缺失与重复 ============
const cards = computed(() => {
  const m = stats.value
  if (!m) return []
  const overallRate = m.missingRate
  return [
    { icon: 'fa-table', bg: 'bg-slate-100', color: 'text-slate-600', label: '总行数', value: m.totalRows.toLocaleString() },
    { icon: 'fa-cells', bg: 'bg-slate-100', color: 'text-slate-600', label: '总单元格', value: m.totalCells.toLocaleString() },
    { icon: 'fa-circle-question', bg: 'bg-rose-50', color: 'text-rose-600', label: '缺失单元格', value: m.totalMissing.toLocaleString() },
    { icon: 'fa-percent', bg: overallRate > 5 ? 'bg-rose-50' : 'bg-emerald-50', color: overallRate > 5 ? 'text-rose-600' : 'text-emerald-600', label: '整体缺失率', value: overallRate.toFixed(2) + '%' },
    { icon: 'fa-copy', bg: 'bg-amber-50', color: 'text-amber-600', label: '重复时间戳', value: m.duplicateCount.toLocaleString() + ' 条' }
  ]
})
const maxMissing = computed(() => Math.max(...(stats.value?.stats || []).map(s => s.missing), 1))

const segCol = ref(null)
const segList = computed(() => {
  void state.dataVersion
  return segCol.value ? segmentsOf(segCol.value) : []
})
const segTruncated = computed(() => {
  void state.dataVersion
  return segCol.value ? segmentsTruncated(segCol.value) : false
})
function algoFor(idx) { return segAlgo(segCol.value, idx) }
function setAlgoAt(idx, ev) { setSegAlgo(segCol.value, idx, ev.target.value) }
function segTotalRows() { return segList.value.reduce((s, g) => s + g.count, 0) }
function colLabel(key) { return d.value.columns.find(c => c.key === key)?.label || key }

async function doSegmentImpute(idx) {
  const r = await applySegmentImpute(segCol.value, idx)
  if (r) toast('success', r.summary)
}
async function doAllSegments() {
  const r = await applyAllSegmentsImpute(segCol.value)
  if (r) toast('success', r.summary)
}

const dupStrategy = ref('mean')
const defaultAlgo = ref('linear')
async function doImputeAll() {
  // store 内的 imputeAllAndDedupe 已经把服务端 summary 作为 toast 发出来了
  await imputeAllAndDedupe(dupStrategy.value, defaultAlgo.value)
}

// ============ Tab2 异常检测 ============
const algo = ref('3sigma')
const repair = ref('clip')
const exprInput = ref('')
const exprFeedback = ref(null)
const detection = computed(() => {
  void state.dataVersion
  const la = state.lastAnomaly
  return la && la.wsId === d.value.wsId ? la : null
})
const anomalyResults = computed(() => detection.value?.results || null)
const anomalySummary = computed(() => detection.value?.summary || null)

const summaryPill = computed(() => {
  const m = stats.value
  if (!m) return '诊断数据待从后端读取'
  const q = qualityData()
  const runs = Object.values(q.segments || {})
  const segCols = runs.length
  const segRuns = runs.reduce((s, r) => s + r.length, 0)          // 段数
  const segCells = runs.reduce((s, r) => s + r.reduce((n, x) => n + x.count, 0), 0)  // 段内行数
  // segmentsTruncated 对每个有缺失的列都会建一个键（值可能是 false），只数 true 才是真的被截断
  const trunc = Object.values(q.segmentsTruncated || {}).filter(Boolean).length
  const capNote = trunc ? `（${trunc} 列超过 ${m.segmentCap} 段已截断）` : ''
  const missingPart = `缺失 ${m.totalMissing} 单元格 · ${segCols} 列共 ${segRuns} 段(${segCells} 行)${capNote}`
  const s = anomalySummary.value
  if (s) {
    return `检测到 ${s.totalAnomalies} 处异常 · 整体异常率 ${s.overallRate.toFixed(2)}% · ${missingPart}`
  }
  return `异常检测待执行 · ${missingPart}`
})

function validateExpr() {
  const expr = exprInput.value.trim()
  if (!expr) { exprFeedback.value = { ok: false, text: '⚠ 请输入表达式' }; return false }
  try {
    const fn = buildExprFn(expr)
    const test = fn(100, { mean: 50, std: 10, median: 48, q1: 30, q3: 70, min: 0, max: 200 })
    if (typeof test !== 'boolean') {
      exprFeedback.value = { ok: true, warn: true, text: `⚠ 表达式结果为 ${test}，建议使用比较运算符返回布尔值（truthy/falsy 亦可运行）` }
    } else {
      exprFeedback.value = { ok: true, text: `✓ 表达式合法 · 测试: v=100 → ${test ? '异常' : '正常'}` }
    }
    return true
  } catch (e) {
    exprFeedback.value = { ok: false, text: `⚠ 表达式不合法: ${e.message}` }
    return false
  }
}
// 本地白名单编译只用于「验证」按钮；真正生效的判定在后端按同一份表达式向量化求值
function buildExprFn(expr) { return buildExprEvaluator(expr) }

const detecting = ref(false)
const iforestParams = reactive({ nEstimators: 200, contamination: 'auto' })

async function doDetect() {
  if (!requireBackend('异常检测')) return
  if (algo.value === 'expr' && !validateExpr()) return
  detecting.value = true
  try {
    const params = algo.value === 'iforest_sklearn'
      ? {
        nEstimators: Number(iforestParams.nEstimators) || 200,
        contamination: iforestParams.contamination === 'auto' ? 'auto' : Number(iforestParams.contamination),
        randomState: 42
      }
      : {}
    const r = await detectAnomalies(algo.value, exprInput.value.trim(), params)
    if (r) {
      toast('success', `检测完成：${r.summary.totalAnomalies} 个异常点，涉及 ${r.summary.colsAffected}/${r.summary.numCols} 列`)
      await ensureSeries()
      renderChart()
    }
  } finally {
    detecting.value = false
  }
}

async function doRepair() {
  const la = detection.value
  if (!la) { toast('warning', '请先执行检测：修复按服务端留存的行索引执行'); return }
  if (la.stale) { toast('warning', '检测之后数据又被改过（行位置已变），请重新检测后再修复'); return }
  try {
    await ElMessageBox.confirm(
      `将按 ${ANOMALY_REPAIRS[repair.value].split('：')[0]} 方案处理 ${la.summary.totalAnomalies} 个异常点，数据会被修改。确认继续？`,
      '执行修复', { type: 'warning' }
    )
  } catch (e) { return }
  const r = await repairAnomalies(repair.value)
  if (r) toast('success', `修复完成：处理 ${r.touched} 个数据点`)
}

const anomalyCards = computed(() => {
  const s = anomalySummary.value
  if (!s) return null
  return [
    { icon: 'fa-microscope', bg: 'bg-slate-100', fg: 'text-slate-600', label: '检测算法', value: ANOMALY_NAMES[detection.value.algo] || detection.value.algo },
    { icon: 'fa-database', bg: 'bg-slate-100', fg: 'text-slate-600', label: '检测行数', value: (detection.value.rowCount || 0).toLocaleString() },
    { icon: 'fa-triangle-exclamation', bg: 'bg-rose-50', fg: 'text-rose-600', label: '异常点总数', value: s.totalAnomalies.toLocaleString() },
    { icon: 'fa-percent', bg: s.overallRate > 5 ? 'bg-rose-50' : 'bg-emerald-50', fg: s.overallRate > 5 ? 'text-rose-600' : 'text-emerald-600', label: '整体异常率', value: s.overallRate.toFixed(2) + '%' },
    { icon: 'fa-table-columns', bg: 'bg-amber-50', fg: 'text-amber-600', label: '受影响列数', value: `${s.colsAffected} / ${s.numCols}` },
    { icon: 'fa-shield-halved', bg: 'bg-emerald-50', fg: 'text-emerald-600', label: '正常数据量', value: (s.totalCells - s.totalAnomalies).toLocaleString() }
  ]
})
const maxAnomaly = computed(() => Math.max(...(anomalyResults.value || []).map(r => r.anomalies), 1))
function fmtBound(v) { return v === null || v === undefined ? '—' : Number(v).toFixed(1) }

// ============ Tab3 掩码 ============
const maskName = ref('mask_curtailment')
async function doGenerateMask() {
  if (!requireBackend('生成掩码列')) return
  if (!brushRange.value) { toast('warning', '请先在图表上拖拽框选要标记的时段'); return }
  const name = maskName.value.trim() || 'mask_1'
  const ones = await generateMask(name, brushRange.value)
  if (ones === null) return
  toast('success', `已生成布尔掩码列 [${name}]，区间内 ${ones} 行置 1`)
  const num = state.masks.length + 1
  maskName.value = `mask_segment_${num}`
  clearBrush()
}
const maskStats = computed(() => ({
  total: state.masks.length,
  rows: state.masks.reduce((s, m) => s + m.onesCount, 0),
  cols: d.value.columns.length
}))
async function doDeleteMask(idx) {
  if (!requireBackend('删除掩码列')) return
  if (await deleteMask(idx)) toast('success', '掩码列已从服务端工作区删除')
}
async function doDeleteAllMasks() {
  if (!requireBackend('清空掩码列')) return
  if (await deleteAllMasks()) toast('success', '掩码列已全部从服务端工作区删除')
}

// ============ 生命周期 ============
function init() {
  if (chartEl.value && !chart) {
    chart = echarts.init(chartEl.value)
    chart.on('brushSelected', onBrushSelected)
  }
  renderChart()
}
function resize() { chart && chart.resize() }
onMounted(async () => { await nextTick(); init(); await refreshAll(); window.addEventListener('resize', resize) })
onActivated(() => { nextTick(() => resize()); refreshAll() })
onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chart && chart.dispose()
  chart = null
})
// 整页重算的开关不能是 state.dataVersion：翻一页、读一次整表统计都会碰它，
// 那样每次只读访问都会把 /quality 与 /series 这两趟整表扫描重打一遍。
// 只有「这一页读的那批数值真的变了」才重算——签名见 store.sourceSig。
watch(() => sourceSig(), () => { if (state.currentStep === 4) refreshAll() })
watch(tab, t => { if (t === 'mask') nextTick(() => chart && applyBrushCursor()) })
watch(selectionKey, () => { ensureSeries().then(renderChart) })
</script>

<template>
  <section class="step-panel h-full p-4 flex flex-col gap-3 overflow-y-auto">
    <!-- 本步不再需要浏览器整表视图：诊断与曲线各取一次后端聚合结果 -->
    <div v-if="diagError || state.quality.loading" class="rounded-xl border px-3 py-2 text-[11px] flex items-start gap-2 shrink-0"
         :class="diagError ? 'bg-rose-50 border-rose-200 text-rose-700' : 'bg-indigo-50 border-indigo-200 text-indigo-700'">
      <i class="fa-solid mt-0.5" :class="diagError ? 'fa-triangle-exclamation' : 'fa-spinner fa-spin'"></i>
      <div class="min-w-0">
        <div class="font-semibold">{{ diagError ? '本步为只读：下方数字尚未从后端取到' : '正在从后端读取质量诊断…' }}</div>
        <div class="mt-0.5 leading-snug opacity-80">{{ diagError || `${d.wsId} · 缺失扫描与全量曲线（每行一个点）由服务端计算` }}</div>
      </div>
      <button v-if="diagError" @click="state.backend.online ? refreshAll() : checkBackend().then(refreshAll)"
              class="ml-auto shrink-0 px-2 py-0.5 rounded border border-current opacity-70 hover:opacity-100">重试</button>
    </div>

    <!-- 诊断画布 -->
    <div class="h-[48%] min-h-[340px] shrink-0 bg-white rounded-xl border border-slate-200 shadow-sm p-2 flex flex-col relative">
      <div class="flex justify-between items-center px-3 pt-1 gap-3">
        <div class="flex items-center space-x-3 min-w-0">
          <span class="text-xs font-bold text-slate-800 shrink-0">时序异常诊断与区间标注画布</span>
          <span class="text-[11px] bg-rose-50 text-rose-600 border border-rose-200 px-2 py-0.5 rounded-full font-medium truncate">{{ summaryPill }}</span>
        </div>
        <div class="flex items-center gap-2 shrink-0">
          <span v-if="sd" class="text-[10px] text-slate-400">{{ seriesChip }}</span>
          <button @click="toggleBrush"
                  class="px-2.5 py-1 text-xs rounded border font-medium flex items-center transition-colors"
                  :class="brushActive ? 'border-rose-400 bg-rose-50 text-rose-700' : 'border-indigo-300 text-indigo-700 hover:bg-indigo-50'">
            <i class="fa-solid fa-highlighter mr-1"></i><span>{{ brushActive ? '已激活刷选 (点击图表拖动)' : '开启时段刷选标记' }}</span>
          </button>
          <button @click="clearBrush" class="px-2.5 py-1 text-xs rounded border border-slate-200 text-slate-600 hover:bg-slate-100">清空框选</button>
        </div>
      </div>

      <!-- 绘图列多选（与第三步同款）：改勾选就是换一次后端取数，浏览器不持有整表 -->
      <div class="flex items-center gap-1.5 px-3 pt-1.5 flex-wrap shrink-0">
        <span class="text-[11px] font-bold text-slate-600 shrink-0">绘图列:</span>
        <button @click="toggleAll(true)" class="text-[10px] text-indigo-600 hover:underline shrink-0">全选</button>
        <span class="text-slate-300 shrink-0">|</span>
        <button @click="toggleAll(false)" class="text-[10px] text-slate-500 hover:underline shrink-0">清空</button>
        <span class="text-[10px] text-slate-400 font-mono shrink-0">已选 {{ selected.size }} 条</span>
        <label v-for="(c, i) in floatCols" :key="c.key"
               class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border cursor-pointer transition-all text-[10px] font-medium"
               :class="selected.has(c.key) ? 'bg-indigo-50 border-indigo-300 text-indigo-700' : 'bg-white border-slate-200 text-slate-600 hover:border-indigo-200 hover:bg-indigo-50/50'">
          <input type="checkbox" class="accent-indigo-500" :checked="selected.has(c.key)" @change="toggleCol(c.key)" />
          <span class="w-2 h-2 rounded-full shrink-0" :style="{ background: colorOf(i) }"></span>
          <span>{{ c.label || c.key }}</span>
        </label>
        <span v-if="!floatCols.length" class="text-[10px] text-slate-400">没有数值列可画</span>
      </div>

      <div v-if="seriesError" class="px-3 pt-0.5 text-[10px] text-rose-600 shrink-0">
        <i class="fa-solid fa-triangle-exclamation mr-1"></i>曲线没取到，图上现在没有任何点：{{ seriesError }}
      </div>
      <div v-if="overlayNote" class="px-3 pt-0.5 text-[10px] text-amber-600 shrink-0">
        <i class="fa-solid fa-circle-exclamation mr-1"></i>折线画的是整表每行，但有列的异常散点没画全
        （{{ decimated ? '被降采样抽掉、或' : '' }}超出每列 {{ capNote('anomalyPointsPerCol', 200) }} 点的覆盖层预算）：
        图上共 {{ overlayNote.drawn }} 个 / 检出共 {{ overlayNote.total }} 个。画不全的列（图上/真实数）：
        <span v-for="(r, i) in overlayNote.shown" :key="r.label">{{ r.label }} {{ r.drawn }}/{{ r.total }}<span v-if="i < overlayNote.shown.length - 1">、</span></span><span v-if="overlayNote.hidden"> 等</span>，共 {{ overlayNote.allCols }} 列（完整数量见下方表格）
      </div>
      <div v-if="marksNote" class="px-3 pt-0.5 text-[10px] text-amber-600 shrink-0">
        <i class="fa-solid fa-circle-exclamation mr-1"></i>折线画的是整表每行，但有列的缺失虚线没画全
        （{{ decimated ? '被降采样抽掉、或' : '' }}超出每列 {{ capNote('missingMarksPerCol', 80) }} 条的虚线上限）：
        图上共 {{ marksNote.drawn }} 条 / 缺失格共 {{ marksNote.total }} 个。画不全的列（虚线/缺失格）：
        <span v-for="(r, i) in marksNote.shown" :key="r.label">{{ r.label }} {{ r.drawn }}/{{ r.total }}<span v-if="i < marksNote.shown.length - 1">、</span></span><span v-if="marksNote.hidden"> 等</span>，共 {{ marksNote.allCols }} 列（完整数量见下方表格）
      </div>
      <div v-if="staleChart" class="px-3 pt-0.5 text-[10px] text-rose-600 shrink-0">
        <i class="fa-solid fa-arrows-rotate mr-1"></i>检测之后数据又被改过，图上的彩色散点已不保证落在当前行：请重新执行检测
      </div>
      <div ref="chartEl" class="w-full flex-1"></div>
    </div>

    <!-- 工作台 -->
    <div class="flex-1 bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden flex flex-col min-h-[420px]">
      <div class="flex items-center border-b border-slate-200 bg-slate-50/80 shrink-0">
        <button @click="tab = 'impute'"
                class="flex items-center gap-1.5 px-4 py-2.5 text-xs font-semibold border-b-2 transition-colors"
                :class="tab === 'impute' ? TAB_COLORS.impute : 'border-transparent text-slate-500 hover:text-slate-700'">
          <i class="fa-solid fa-wrench"></i>缺失与重复项清洗
        </button>
        <button @click="tab = 'anomaly'"
                class="flex items-center gap-1.5 px-4 py-2.5 text-xs font-semibold border-b-2 transition-colors"
                :class="tab === 'anomaly' ? TAB_COLORS.anomaly : 'border-transparent text-slate-500 hover:text-slate-700'">
          <i class="fa-solid fa-triangle-exclamation"></i>异常检测与过滤
        </button>
        <button @click="tab = 'mask'"
                class="flex items-center gap-1.5 px-4 py-2.5 text-xs font-semibold border-b-2 transition-colors"
                :class="tab === 'mask' ? TAB_COLORS.mask : 'border-transparent text-slate-500 hover:text-slate-700'">
          <i class="fa-solid fa-vector-square"></i>手动标注与掩码生成
        </button>
      </div>

      <div class="flex-1 overflow-hidden">
        <!-- Tab 1 -->
        <div v-show="tab === 'impute'" class="h-full p-5 flex gap-6 items-start overflow-auto">
          <div class="flex-1 min-w-0">
            <h3 class="text-sm font-bold text-slate-800 flex items-center mb-3">
              <i class="fa-solid fa-wrench text-amber-500 mr-2"></i>缺失值填补与重复时间戳合并
            </h3>

            <div class="grid grid-cols-5 gap-3 mb-4">
              <div v-for="c in cards" :key="c.label" class="flex items-center gap-2.5 px-3 py-2 rounded-lg border border-slate-200" :class="c.bg">
                <div class="w-7 h-7 rounded-md bg-white/70 flex items-center justify-center shrink-0">
                  <i class="fa-solid text-[11px]" :class="[c.icon, c.color]"></i>
                </div>
                <div class="min-w-0">
                  <span class="text-[10px] text-slate-400 block leading-tight">{{ c.label }}</span>
                  <span class="text-sm font-bold font-mono leading-tight" :class="c.color">{{ c.value }}</span>
                </div>
              </div>
              <div v-if="!stats" class="col-span-5 text-center py-4 text-[11px] text-slate-400 border border-dashed border-slate-200 rounded-lg">
                没有诊断数据可读：{{ diagError || '正在从后端读取…' }}
              </div>
            </div>

            <div v-if="stats" class="border border-slate-200 rounded-lg overflow-hidden mb-4">
              <div class="bg-slate-50 px-3 py-1.5 border-b border-slate-200 flex items-center justify-between">
                <span class="text-[11px] font-semibold text-slate-600">各列缺失详情 <span class="text-[10px] text-slate-400 font-normal ml-1">点击数值列行查看缺失时间段</span></span>
                <span class="text-[10px] text-slate-400">共 {{ stats.totalRows.toLocaleString() }} 行 × {{ stats.stats.length }} 列 · 服务端扫描</span>
              </div>
              <div class="max-h-[140px] overflow-auto">
                <table class="w-full text-xs">
                  <thead class="sticky top-0 bg-slate-50 text-slate-500 border-b border-slate-200">
                    <tr>
                      <th class="text-left px-3 py-1.5 font-semibold">列名</th>
                      <th class="text-left px-3 py-1.5 font-semibold">类型</th>
                      <th class="text-right px-3 py-1.5 font-semibold">总数</th>
                      <th class="text-right px-3 py-1.5 font-semibold">缺失数</th>
                      <th class="text-right px-3 py-1.5 font-semibold">缺失率</th>
                      <th class="text-right px-3 py-1.5 font-semibold">有效数</th>
                      <th class="px-3 py-1.5 font-semibold w-32">缺失分布条</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-slate-100 font-mono">
                    <tr v-for="s in stats.stats" :key="s.key"
                        class="hover:bg-amber-50/30" :class="s.imputable && s.missing > 0 ? 'cursor-pointer' : ''"
                        :style="segCol === s.key ? 'background:#fef3c7;outline:2px solid #f59e0b;outline-offset:-2px' : ''"
                        @click="s.imputable && s.missing > 0 && (segCol = s.key)">
                      <td class="px-3 py-1.5 text-slate-700 font-sans font-medium truncate max-w-[140px]">
                        {{ s.label }}
                        <i v-if="s.imputable && s.missing > 0" class="fa-solid fa-chevron-right text-[8px] text-amber-400 ml-1"></i>
                      </td>
                      <td class="px-3 py-1.5"><span class="px-1.5 py-0.5 rounded text-[10px] bg-slate-100 text-slate-500">{{ s.type }}</span></td>
                      <td class="px-3 py-1.5 text-right text-slate-600">{{ s.total.toLocaleString() }}</td>
                      <td class="px-3 py-1.5 text-right font-semibold" :class="s.missing > 0 ? 'text-rose-600' : 'text-slate-400'">{{ s.missing.toLocaleString() }}</td>
                      <td class="px-3 py-1.5 text-right font-semibold" :class="s.rate === 0 ? 'text-emerald-600' : s.rate < 5 ? 'text-amber-600' : 'text-rose-600'">{{ s.rate.toFixed(2) }}%</td>
                      <td class="px-3 py-1.5 text-right text-emerald-600">{{ s.valid.toLocaleString() }}</td>
                      <td class="px-3 py-1.5">
                        <div class="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
                          <div class="h-full rounded-full transition-all"
                               :class="s.rate === 0 ? 'bg-emerald-400' : s.rate < 5 ? 'bg-amber-400' : 'bg-rose-400'"
                               :style="{ width: Math.max(2, s.missing / maxMissing * 100) + '%' }"></div>
                        </div>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <div v-if="segCol" class="border border-amber-200 rounded-lg overflow-hidden mb-4 bg-amber-50/30">
              <div class="bg-amber-50 px-3 py-2 border-b border-amber-200 flex items-center justify-between">
                <div class="flex items-center gap-2 min-w-0">
                  <i class="fa-solid fa-clock-rotate-left text-amber-600 text-[11px]"></i>
                  <span class="text-[11px] font-bold text-amber-800 shrink-0">缺失时间段详情</span>
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-200 text-amber-800 font-mono">{{ colLabel(segCol) }}</span>
                  <span class="text-[10px] text-amber-600">共 {{ segList.length }} 个缺失段，{{ segTotalRows() }} 行</span>
                </div>
                <button @click="segCol = null" class="w-5 h-5 rounded flex items-center justify-center text-amber-400 hover:text-amber-700 hover:bg-amber-100">
                  <i class="fa-solid fa-xmark text-[11px]"></i>
                </button>
              </div>
              <div v-if="segTruncated" class="px-3 py-2 bg-rose-50 border-b border-rose-200 text-[10px] text-rose-700 leading-snug">
                <i class="fa-solid fa-circle-exclamation mr-1"></i>
                该列缺失段超过 {{ stats?.segmentCap }} 段的返回上限，以下是前 {{ segList.length }} 段：逐段填补只能覆盖这部分，
                整列请用右侧「执行填补与去重」（服务端扫描全表，不受上限限制）
              </div>
              <div class="max-h-[220px] overflow-auto divide-y divide-amber-100">
                <div v-if="segList.length === 0" class="text-center py-6 text-amber-400 text-xs">
                  <i class="fa-solid fa-check-circle text-lg block mb-1"></i>该列当前无缺失数据
                </div>
                <div v-for="(seg, idx) in segList" :key="`${seg.startIdx}-${seg.endIdx}`" class="px-3 py-2.5 flex items-center gap-3 hover:bg-amber-50 transition-colors">
                  <span class="w-5 h-5 rounded-full bg-amber-200 text-amber-800 text-[10px] font-bold flex items-center justify-center shrink-0">{{ idx + 1 }}</span>
                  <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 text-xs">
                      <span class="font-mono text-amber-900 font-medium">{{ (seg.startTime || '').replace(/^\d{4}-/, '') }}</span>
                      <i class="fa-solid fa-arrow-right text-amber-400 text-[9px]"></i>
                      <span class="font-mono text-amber-900 font-medium">{{ (seg.endTime || '').replace(/^\d{4}-/, '') }}</span>
                    </div>
                    <div class="text-[10px] text-amber-600 mt-0.5">
                      缺失 <strong>{{ seg.count }}</strong> 行
                      <span class="text-amber-400 mx-1">·</span>行号 {{ seg.startIdx }}–{{ seg.endIdx }}
                    </div>
                  </div>
                  <select :value="algoFor(idx)" @change="setAlgoAt(idx, $event)"
                          class="text-[11px] border border-amber-300 rounded px-2 py-1 bg-white focus:border-amber-500 outline-none">
                    <option v-for="o in IMPUTE_ALGOS" :key="o.value" :value="o.value">{{ o.label }}</option>
                  </select>
                  <button @click="doSegmentImpute(idx)" class="px-2.5 py-1 bg-amber-500 hover:bg-amber-600 text-white rounded text-[11px] font-semibold shadow-sm shrink-0">应用</button>
                </div>
                <div v-if="segList.length > 0" class="px-3 py-2 bg-amber-100/50 border-t border-amber-200 flex items-center justify-between">
                  <span class="text-[10px] text-amber-600">对所有缺失段执行各自选定的算法</span>
                  <button @click="doAllSegments" :disabled="segTruncated" :class="segTruncated ? 'opacity-50 cursor-not-allowed' : ''"
                          class="px-3 py-1 bg-amber-600 hover:bg-amber-700 text-white rounded text-[11px] font-bold shadow-sm">
                    <i class="fa-solid fa-wand-magic-sparkles mr-1 text-[9px]"></i>一键全部填补
                  </button>
                </div>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-5">
              <div>
                <label class="text-[11px] text-slate-500 block mb-1.5">全表填补默认算法</label>
                <select v-model="defaultAlgo" class="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white focus:border-amber-400 outline-none">
                  <option v-for="o in IMPUTE_ALGOS" :key="o.value" :value="o.value">{{ o.label }}</option>
                </select>
                <p class="text-[10px] text-slate-400 mt-1.5">服务端扫描全表时对所有缺失段使用该算法</p>
              </div>
              <div>
                <label class="text-[11px] text-slate-500 block mb-1.5">重复时间戳合并策略</label>
                <select v-model="dupStrategy" class="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white focus:border-amber-400 outline-none">
                  <option value="mean">聚合取平均值 (Aggregate Mean)</option>
                  <option value="first">保留首个记录 (Keep First)</option>
                  <option value="last">保留最后记录 (Keep Last)</option>
                </select>
                <p class="text-[10px] text-slate-400 mt-1.5">
                  <i class="fa-solid fa-circle-info mr-1 text-amber-500"></i>当前重复时间戳
                  {{ stats ? stats.duplicateCount.toLocaleString() + ' 条 / ' + stats.duplicateGroups.toLocaleString() + ' 组' : '待后端读取' }}
                </p>
              </div>
            </div>
          </div>

          <div class="shrink-0 flex flex-col items-center justify-center h-full pl-5 border-l border-slate-100">
            <button @click="doImputeAll" class="w-40 py-3.5 bg-amber-500 hover:bg-amber-600 text-white rounded-xl text-xs font-bold shadow-md hover:shadow-lg transition-all flex flex-col items-center gap-1.5">
              <i class="fa-solid fa-check text-sm"></i>
              <span>执行填补与去重</span>
            </button>
            <p class="text-[10px] text-slate-400 mt-2 text-center w-40">服务端扫描全表缺失段并填充<br/>同时按解析后的时间戳合并重复</p>
          </div>
        </div>

        <!-- Tab 2 -->
        <div v-show="tab === 'anomaly'" class="h-full p-5 flex gap-6 items-start overflow-auto">
          <div class="flex-1 min-w-0">
            <h3 class="text-sm font-bold text-slate-800 flex items-center mb-3">
              <i class="fa-solid fa-triangle-exclamation text-rose-500 mr-2"></i>统计学异常检测与智能修复
            </h3>

            <div class="grid grid-cols-3 gap-4 mb-4">
              <div>
                <label class="text-[11px] text-slate-500 block mb-1.5">判定算法及灵敏度</label>
                <select v-model="algo" class="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white focus:border-rose-400 outline-none">
                  <option value="3sigma">3-Sigma (Z-Score &gt; 3.0)</option>
                  <option value="iqr">四分位距箱线法 (IQR 1.5倍)</option>
                  <option value="iforest">孤立森林 (近似版 · 后端 numpy 窗口 MAD)</option>
                  <option value="iforest_sklearn">孤立森林 (sklearn 完整版 · {{ state.backend.online ? '后端已连接' : '后端未连接' }})</option>
                  <option value="expr">自定义表达式 (Expression)</option>
                </select>
              </div>
              <div>
                <label class="text-[11px] text-slate-500 block mb-1.5">异常点处置方案</label>
                <select v-model="repair" class="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white focus:border-rose-400 outline-none">
                  <option value="clip">上下阈值截断限制 (Clip Extremes)</option>
                  <option value="nan_impute">置为缺失值并插值重算</option>
                  <option value="mask_only">仅生成异常布尔掩码列</option>
                </select>
              </div>
              <div class="flex items-end gap-2">
                <button @click="doDetect" :disabled="detecting"
                        class="flex-1 py-2 bg-rose-500 hover:bg-rose-600 disabled:opacity-60 text-white rounded-lg text-xs font-bold shadow-sm flex items-center justify-center gap-1.5">
                  <i class="fa-solid text-[10px]" :class="detecting ? 'fa-spinner fa-spin' : 'fa-magnifying-glass'"></i>{{ detecting ? '后端计算中…' : '执行检测' }}
                </button>
                <button @click="doRepair" :disabled="!detection || detection.stale"
                        class="flex-1 py-2 bg-slate-700 hover:bg-slate-800 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg text-xs font-bold shadow-sm flex items-center justify-center gap-1.5">
                  <i class="fa-solid fa-wand-magic-sparkles text-[10px]"></i>执行修复
                </button>
              </div>
            </div>

            <div v-if="algo === 'iforest_sklearn'" class="mb-4 p-3 rounded-lg border border-indigo-200 bg-indigo-50/40 flex items-center gap-5 flex-wrap">
              <span class="text-[11px] font-bold text-indigo-700 flex items-center gap-1.5">
                <i class="fa-solid fa-server text-[10px]"></i>后端 sklearn IsolationForest 超参
              </span>
              <label class="text-[11px] text-slate-600 flex items-center gap-1.5">
                树数量 n_estimators
                <input v-model.number="iforestParams.nEstimators" type="number" min="10" max="2000" step="10"
                       class="w-20 border border-indigo-200 rounded px-2 py-1 text-[11px] bg-white outline-none focus:border-indigo-400" />
              </label>
              <label class="text-[11px] text-slate-600 flex items-center gap-1.5">
                污染率 contamination
                <select v-model="iforestParams.contamination" class="border border-indigo-200 rounded px-2 py-1 text-[11px] bg-white outline-none focus:border-indigo-400">
                  <option value="auto">auto（自适应）</option>
                  <option value="0.01">0.01</option>
                  <option value="0.02">0.02</option>
                  <option value="0.05">0.05</option>
                  <option value="0.1">0.10</option>
                </select>
              </label>
              <span class="text-[10px] text-slate-400">random_state=42 · 缺失点自动剔除 · 边界取正常点 0.5%/99.5% 分位</span>
            </div>

            <div v-if="algo === 'expr'" class="mb-4 p-3 rounded-lg border border-rose-200 bg-rose-50/40">
              <div class="flex items-center justify-between mb-2">
                <label class="text-[11px] font-bold text-rose-700 flex items-center gap-1">
                  <i class="fa-solid fa-code text-[10px]"></i>自定义异常判定表达式
                </label>
                <div class="flex items-center gap-2 text-[10px] text-rose-500">
                  <span>可用变量: <code class="bg-rose-100 px-1 rounded">v</code> (当前值)</span>
                  <span>|</span>
                  <span>统计量: <code class="bg-rose-100 px-1 rounded">mean</code> <code class="bg-rose-100 px-1 rounded">std</code> <code class="bg-rose-100 px-1 rounded">median</code> <code class="bg-rose-100 px-1 rounded">q1</code> <code class="bg-rose-100 px-1 rounded">q3</code> <code class="bg-rose-100 px-1 rounded">min</code> <code class="bg-rose-100 px-1 rounded">max</code></span>
                </div>
              </div>
              <div class="flex items-center gap-2">
                <input v-model="exprInput" placeholder="如: v > 1000 或 v < mean + 2*std"
                       class="flex-1 text-xs border border-rose-300 rounded-lg px-3 py-2 font-mono bg-white focus:border-rose-500 outline-none" />
                <button @click="validateExpr" class="px-3 py-2 border border-rose-300 hover:bg-rose-100 text-rose-600 rounded-lg text-[11px] font-medium">
                  <i class="fa-solid fa-check-double mr-1"></i>验证
                </button>
              </div>
              <div v-if="exprFeedback" class="mt-1.5 text-[10px]" :class="exprFeedback.ok ? 'text-emerald-600' : 'text-rose-600'">{{ exprFeedback.text }}</div>
              <div class="mt-1 text-[10px] text-slate-400">「验证」只检查表达式语法与取值类型，不逐行判定异常。</div>
              <div class="flex items-center gap-1.5 mt-2 flex-wrap">
                <span class="text-[10px] text-rose-400 mr-1">快捷模板:</span>
                <button v-for="tpl in ['v > 1000', 'v < 0', 'v > mean + 2*std', 'v < mean - 2*std', 'v > q3 + 1.5*(q3-q1) || v < q1 - 1.5*(q3-q1)']" :key="tpl"
                        @click="exprInput = tpl"
                        class="px-2 py-0.5 rounded text-[10px] bg-white border border-rose-200 text-rose-600 hover:bg-rose-100">{{ tpl }}</button>
              </div>
            </div>

            <div v-if="detection && detection.stale" class="mb-4 px-3 py-2 rounded-lg border border-rose-200 bg-rose-50 text-[11px] text-rose-700 flex items-start gap-2">
              <i class="fa-solid fa-arrows-rotate mt-0.5"></i>
              <span>检测之后工作区又被改过（版本 {{ detection.version }} → 当前 {{ d.meta?.version ?? '—' }}），行号已不对应当时的异常行：修复按钮已禁用，请重新执行检测。</span>
            </div>

            <div class="grid grid-cols-6 gap-3 mb-4">
              <div v-if="!anomalyCards" class="col-span-6 text-center py-6 text-slate-300 text-xs">
                <i class="fa-solid fa-magnifying-glass-chart text-2xl block mb-1.5 opacity-40"></i>
                点击「执行检测」按钮开始扫描异常数据
              </div>
              <template v-else>
                <div v-for="c in anomalyCards" :key="c.label" class="flex items-center gap-2.5 px-3 py-2.5 rounded-lg border border-slate-200" :class="c.bg">
                  <div class="w-7 h-7 rounded-md bg-white/70 flex items-center justify-center shrink-0">
                    <i class="fa-solid text-[11px]" :class="[c.icon, c.fg]"></i>
                  </div>
                  <div class="min-w-0">
                    <span class="text-[10px] text-slate-400 block leading-tight">{{ c.label }}</span>
                    <span class="text-sm font-bold font-mono leading-tight" :class="c.fg">{{ c.value }}</span>
                  </div>
                </div>
              </template>
            </div>

            <div class="border border-slate-200 rounded-lg overflow-hidden">
              <div class="bg-slate-50 px-3 py-1.5 border-b border-slate-200 flex items-center justify-between">
                <span class="text-[11px] font-semibold text-slate-600">各列异常详情</span>
                <span class="text-[10px] text-slate-400">{{ detection ? `${ANOMALY_NAMES[detection.algo] || detection.algo} · ${detection.time}` : '等待检测' }}</span>
              </div>
              <div class="max-h-[160px] overflow-auto">
                <table class="w-full text-xs">
                  <thead class="sticky top-0 bg-slate-50 text-slate-500 border-b border-slate-200">
                    <tr>
                      <th class="text-left px-3 py-1.5 font-semibold">列名</th>
                      <th class="text-right px-3 py-1.5 font-semibold">总行数</th>
                      <th class="text-right px-3 py-1.5 font-semibold">异常数</th>
                      <th class="text-right px-3 py-1.5 font-semibold">异常率</th>
                      <th class="text-right px-3 py-1.5 font-semibold">正常数</th>
                      <th class="text-right px-3 py-1.5 font-semibold">下界</th>
                      <th class="text-right px-3 py-1.5 font-semibold">上界</th>
                      <th class="px-3 py-1.5 font-semibold w-28">异常分布条</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-slate-100 font-mono">
                    <tr v-if="!anomalyResults"><td colspan="8" class="text-center py-6 text-slate-300">—</td></tr>
                    <tr v-for="r in anomalyResults" v-else :key="r.key" class="hover:bg-rose-50/30">
                      <td class="px-3 py-1.5 text-slate-700 font-sans font-medium truncate max-w-[120px]">{{ r.label }}</td>
                      <td class="px-3 py-1.5 text-right text-slate-600">{{ r.total.toLocaleString() }}</td>
                      <td class="px-3 py-1.5 text-right font-semibold" :class="r.anomalies > 0 ? 'text-rose-600' : 'text-slate-400'">{{ r.anomalies.toLocaleString() }}</td>
                      <td class="px-3 py-1.5 text-right font-semibold" :class="r.rate === 0 ? 'text-emerald-600' : r.rate < 5 ? 'text-amber-600' : 'text-rose-600'">{{ r.rate.toFixed(2) }}%</td>
                      <td class="px-3 py-1.5 text-right text-emerald-600">{{ r.normal.toLocaleString() }}</td>
                      <td class="px-3 py-1.5 text-right text-slate-500" :title="r.lower === null ? '该列全部为异常或无正常点，无法给出边界' : ''">{{ fmtBound(r.lower) }}</td>
                      <td class="px-3 py-1.5 text-right text-slate-500" :title="r.upper === null ? '该列全部为异常或无正常点，无法给出边界' : ''">{{ fmtBound(r.upper) }}</td>
                      <td class="px-3 py-1.5">
                        <div class="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
                          <div class="h-full rounded-full transition-all"
                               :class="r.rate === 0 ? 'bg-emerald-400' : r.rate < 5 ? 'bg-amber-400' : 'bg-rose-400'"
                               :style="{ width: Math.max(2, r.anomalies / maxAnomaly * 100) + '%' }"></div>
                        </div>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="shrink-0 w-44 pl-5 border-l border-slate-100 flex flex-col gap-3">
            <div class="p-3 rounded-lg bg-rose-50 border border-rose-200">
              <h4 class="text-[11px] font-bold text-rose-700 mb-1">算法说明</h4>
              <p class="text-[10px] text-rose-600 leading-relaxed">{{ ANOMALY_ALGOS[algo] }}</p>
            </div>
            <div class="p-3 rounded-lg bg-slate-50 border border-slate-200">
              <h4 class="text-[11px] font-bold text-slate-600 mb-1">处置方案说明</h4>
              <p class="text-[10px] text-slate-500 leading-relaxed">{{ ANOMALY_REPAIRS[repair] }}</p>
            </div>
          </div>
        </div>

        <!-- Tab 3 -->
        <div v-show="tab === 'mask'" class="h-full p-5 flex gap-6 items-start overflow-auto">
          <div class="flex-1 min-w-0">
            <h3 class="text-sm font-bold text-slate-800 flex items-center mb-3">
              <i class="fa-solid fa-vector-square text-teal-500 mr-2"></i>手动时段框选与布尔掩码生成
            </h3>

            <div class="grid grid-cols-3 gap-4 mb-4">
              <div>
                <label class="text-[11px] text-slate-500 block mb-1.5">当前框选时段</label>
                <div class="font-mono text-[11px] p-2.5 bg-slate-50 border border-slate-200 rounded-lg text-slate-600 truncate">
                  <template v-if="brushRange">{{ brushRange.start }} ~ {{ brushRange.end }} (索引 {{ brushRange.startIdx }}–{{ brushRange.endIdx }})</template>
                  <template v-else>未框选 — 请先在上方图表上拖拽选取时段</template>
                </div>
                <p class="text-[10px] text-slate-400 mt-1.5">图表画的是整表每一行（服务端全量回传，不抽点），框选边界吸附到的就是真实行号（行号即工作区当前帧的行位置）</p>
              </div>
              <div>
                <label class="text-[11px] text-slate-500 block mb-1.5">自定义掩码特征列名</label>
                <input v-model="maskName" class="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs font-mono bg-white focus:border-teal-400 outline-none" />
                <p class="text-[10px] text-slate-400 mt-1.5">生成的 0/1 列将追加到数据集中，1 表示框选区间内</p>
              </div>
              <div class="flex items-end">
                <button @click="doGenerateMask" class="w-full py-2.5 bg-teal-500 hover:bg-teal-600 text-white rounded-lg text-xs font-bold shadow-sm flex items-center justify-center gap-1.5">
                  <i class="fa-solid fa-plus text-[10px]"></i>生成布尔掩码
                </button>
              </div>
            </div>

            <div class="border border-slate-200 rounded-lg overflow-hidden mt-4">
              <div class="bg-slate-50 px-3 py-1.5 border-b border-slate-200 flex items-center justify-between">
                <span class="text-[11px] font-semibold text-slate-600 flex items-center gap-1.5">
                  <i class="fa-solid fa-layer-group text-teal-500 text-[10px]"></i>已生成的标注掩码
                  <span class="px-1.5 py-0.5 rounded-full text-[10px] font-bold bg-teal-100 text-teal-700">{{ state.masks.length }}</span>
                </span>
                <button v-if="state.masks.length > 0" @click="doDeleteAllMasks" class="text-[10px] text-slate-400 hover:text-rose-600">
                  <i class="fa-solid fa-trash-can mr-0.5"></i>清空全部
                </button>
              </div>
              <div class="max-h-[200px] overflow-auto">
                <div v-if="state.masks.length === 0" class="text-center py-8 text-slate-300 text-xs">
                  <i class="fa-regular fa-object-ungroup text-xl block mb-1.5 opacity-40"></i>
                  尚未生成任何标注掩码
                </div>
                <div v-else class="divide-y divide-slate-100">
                  <div v-for="(m, idx) in state.masks" :key="m.key"
                       class="flex items-center gap-3 px-3 py-2.5 hover:bg-teal-50/40 transition-colors group">
                    <div class="w-8 h-8 rounded-lg bg-teal-100 flex items-center justify-center shrink-0">
                      <i class="fa-solid fa-tag text-teal-600 text-[11px]"></i>
                    </div>
                    <div class="flex-1 min-w-0">
                      <div class="flex items-center gap-2">
                        <span class="text-xs font-bold text-slate-700 font-mono">{{ m.key }}</span>
                        <span class="px-1.5 py-0.5 rounded text-[9px] font-bold bg-teal-100 text-teal-700">BINARY</span>
                      </div>
                      <div class="text-[10px] text-slate-400 mt-0.5 flex items-center gap-2">
                        <span><i class="fa-regular fa-clock mr-0.5"></i>{{ (m.startTime || '?').slice(5, 16) }} ~ {{ (m.endTime || '?').slice(5, 16) }}</span>
                        <span class="w-0.5 h-0.5 rounded-full bg-slate-300"></span>
                        <span>标记 <strong class="text-teal-600">{{ m.onesCount.toLocaleString() }}</strong> 行</span>
                        <span class="w-0.5 h-0.5 rounded-full bg-slate-300"></span>
                        <span>索引 {{ m.startIdx }}–{{ m.endIdx }}</span>
                      </div>
                    </div>
                    <button @click="doDeleteMask(idx)" title="删除此掩码"
                            class="shrink-0 w-7 h-7 rounded-md flex items-center justify-center text-slate-300 hover:text-rose-600 hover:bg-rose-50 opacity-0 group-hover:opacity-100 transition-all">
                      <i class="fa-solid fa-trash-can text-[11px]"></i>
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="shrink-0 w-44 pl-5 border-l border-slate-100 flex flex-col gap-3">
            <div class="p-3 rounded-lg bg-teal-50 border border-teal-200">
              <h4 class="text-[11px] font-bold text-teal-700 mb-1">使用说明</h4>
              <p class="text-[10px] text-teal-600 leading-relaxed">
                1. 点击上方图表「开启时段刷选标记」<br/>
                2. 在图表上拖拽选取目标时段<br/>
                3. 填写掩码列名并点击「生成」<br/>
                4. 生成后可在下方列表中查看或删除
              </p>
            </div>
            <div class="p-3 rounded-lg bg-slate-50 border border-slate-200">
              <h4 class="text-[11px] font-bold text-slate-600 mb-1">掩码用途</h4>
              <p class="text-[10px] text-slate-500 leading-relaxed">
                掩码列 (0/1) 可用于：<br/>
                • 标记限电/检修时段<br/>
                • 特征工程中过滤特定区间<br/>
                • 作为模型训练的辅助特征
              </p>
            </div>
            <div v-if="state.masks.length > 0" class="p-3 rounded-lg bg-white border border-slate-200">
              <h4 class="text-[11px] font-bold text-slate-600 mb-1.5">掩码统计</h4>
              <div class="space-y-1 text-[10px] text-slate-500 font-mono">
                <div class="flex justify-between"><span>掩码总数</span><span class="text-teal-600 font-bold">{{ maskStats.total }}</span></div>
                <div class="flex justify-between"><span>覆盖总行数</span><span class="text-teal-600 font-bold">{{ maskStats.rows.toLocaleString() }}</span></div>
                <div class="flex justify-between"><span>数据集列数</span><span class="text-slate-600 font-bold">{{ maskStats.cols }}</span></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="flex justify-between items-center shrink-0">
      <button @click="switchStep(3)" class="px-4 py-1.5 bg-slate-200 hover:bg-slate-300 text-slate-700 rounded-lg text-xs font-medium">
        <i class="fa-solid fa-arrow-left mr-1"></i>返回数据探索
      </button>
      <button @click="switchStep(5)" class="px-5 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-xs font-semibold shadow">
        下一步：时序特征工程 <i class="fa-solid fa-arrow-right ml-1"></i>
      </button>
    </div>
  </section>
</template>
