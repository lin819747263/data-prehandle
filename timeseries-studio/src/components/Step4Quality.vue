<script setup>
import { ref, reactive, computed, onMounted, onActivated, onBeforeUnmount, nextTick, watch } from 'vue'
import * as echarts from 'echarts'
import { ElMessageBox } from 'element-plus'
import {
  state, ds, switchStep, toast,
  columnMissingStats, applySegmentImpute, applyAllSegmentsImpute,
  imputeAllAndDedupe, detectAnomalies, applyBackendAnomaly, repairAnomalies,
  ANOMALY_ALGOS, ANOMALY_REPAIRS, generateMask, deleteMask, deleteAllMasks
} from '../store'
import { isMissing, detectMissingSegments, buildExprEvaluator } from '../utils'
import { iforestViaBackend } from '../api'

const IMPUTE_ALGOS = [
  { value: 'linear', label: '时序线性插值' },
  { value: 'ffill', label: '前向观测值填充' },
  { value: 'spline', label: '三次样条平滑' },
  { value: 'zero', label: '常数0置换' }
]
const ALGO_NAMES = { '3sigma': '3-Sigma', iqr: 'IQR 箱线法', iforest: '孤立森林(近似)', iforest_sklearn: '孤立森林(sklearn·后端)', expr: '自定义表达式' }

// 换新引用才能失效下游 computed（见 Step2Config 同款注释）
const d = computed(() => { void state.dataVersion; return { ...ds() } })
const tab = ref('impute')
const TAB_COLORS = {
  impute: 'border-amber-500 text-amber-700 bg-white',
  anomaly: 'border-rose-500 text-rose-700 bg-white',
  mask: 'border-teal-500 text-teal-700 bg-white'
}

// ============ 图表 ============
const chartEl = ref(null)
let chart = null
const brushActive = ref(false)
const brushRange = ref(null)

const qualityCol = computed(() => d.value.columns.find(c => c.type === 'float')?.key)

function renderChart() {
  if (!chart) return
  const data = d.value.data
  const colKey = qualityCol.value
  if (!colKey) { chart.clear(); return }
  const timestamps = data.map(r => r[d.value.timeCol])
  const values = data.map(r => r[colKey])

  const anomalyScatter = []
  const missingMarks = []
  const la = state.lastAnomaly
  const set = la?.perColumn?.[colKey]?.set
  values.forEach((v, i) => {
    if (isMissing(v)) missingMarks.push({ xAxis: timestamps[i] })
    else if (set && set.has(i)) anomalyScatter.push([timestamps[i], v])
  })

  chart.setOption({
    title: {
      text: `数据质量图示与异常探针 (${colKey})${la ? ` · ${ALGO_NAMES[la.algo] || la.algo}` : ' · 尚未执行异常检测'}`,
      left: 10, top: 5, textStyle: { fontSize: 12, color: '#334155' }
    },
    tooltip: { trigger: 'axis' },
    toolbox: { feature: { brush: { type: ['lineX', 'clear'] } }, right: 20, top: 5 },
    brush: { toolbox: ['lineX', 'clear'], xAxisIndex: 0 },
    grid: { top: 40, right: 25, bottom: 50, left: 55 },
    dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 5, height: 20 }],
    xAxis: { type: 'category', data: timestamps },
    yAxis: { type: 'value', splitLine: { lineStyle: { type: 'dashed' } } },
    series: [
      {
        name: '原始时序', type: 'line', data: values,
        lineStyle: { color: '#6366f1', width: 1.5 },
        markLine: { symbol: 'none', data: missingMarks.slice(0, 40), lineStyle: { color: '#f43f5e', type: 'dotted', width: 1.5 } }
      },
      {
        name: '检测到的异常点', type: 'scatter', data: anomalyScatter,
        symbolSize: 10, itemStyle: { color: '#ef4444', borderColor: '#fff', borderWidth: 2 }
      }
    ]
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

function onBrushSelected(params) {
  const batch = params.batch && params.batch[0]
  if (!batch || !batch.areas || batch.areas.length === 0) return
  const range = batch.areas[0].coordRange
  const data = d.value.data
  const startIdx = Math.max(0, Math.floor(Math.min(range[0], range[1])))
  const endIdx = Math.min(data.length - 1, Math.ceil(Math.max(range[0], range[1])))
  brushRange.value = {
    startIdx, endIdx,
    start: data[startIdx][d.value.timeCol],
    end: data[endIdx][d.value.timeCol]
  }
}

// ============ Tab1 缺失与重复 ============
const missingStats = computed(() => { void state.dataVersion; return columnMissingStats() })
const cards = computed(() => {
  const { stats, totalRows, duplicateCount } = missingStats.value
  const cols = d.value.columns.length
  const totalCells = totalRows * cols
  const totalMissing = stats.reduce((s, c) => s + c.missing, 0)
  const overallRate = totalCells > 0 ? totalMissing / totalCells * 100 : 0
  return [
    { icon: 'fa-table', bg: 'bg-slate-100', color: 'text-slate-600', label: '总行数', value: totalRows.toLocaleString() },
    { icon: 'fa-cells', bg: 'bg-slate-100', color: 'text-slate-600', label: '总单元格', value: totalCells.toLocaleString() },
    { icon: 'fa-circle-question', bg: 'bg-rose-50', color: 'text-rose-600', label: '缺失单元格', value: totalMissing.toLocaleString() },
    { icon: 'fa-percent', bg: overallRate > 5 ? 'bg-rose-50' : 'bg-emerald-50', color: overallRate > 5 ? 'text-rose-600' : 'text-emerald-600', label: '整体缺失率', value: overallRate.toFixed(2) + '%' },
    { icon: 'fa-copy', bg: 'bg-amber-50', color: 'text-amber-600', label: '重复时间戳', value: duplicateCount.toLocaleString() + ' 条' }
  ]
})
const maxMissing = computed(() => Math.max(...missingStats.value.stats.map(s => s.missing), 1))

const segCol = ref(null)
const segList = computed(() => {
  if (!segCol.value) return []
  return detectMissingSegments(d.value.data, segCol.value, d.value.timeCol)
})
function algoFor(idx) {
  return state.imputeSegAlgos[segCol.value]?.[idx] || 'linear'
}
function setAlgo(idx, ev) {
  if (!state.imputeSegAlgos[segCol.value]) state.imputeSegAlgos[segCol.value] = {}
  state.imputeSegAlgos[segCol.value][idx] = ev.target.value
}
function segTotalRows() { return segList.value.reduce((s, g) => s + g.count, 0) }
function colLabel(key) { return d.value.columns.find(c => c.key === key)?.label || key }

function doSegmentImpute(idx) {
  const n = applySegmentImpute(segCol.value, idx)
  toast('success', `已填补 ${n} 行`)
}
function doAllSegments() {
  const r = applyAllSegmentsImpute(segCol.value)
  toast('success', `已填补 ${r.count} 个缺失段，共 ${r.total} 行`)
}

const dupStrategy = ref('mean')
function doImputeAll() {
  const r = imputeAllAndDedupe(dupStrategy.value)
  toast('success', `缺失值填补完成：${r.colsFixed} 列、${r.totalFilled} 个缺失值，合并重复时间戳 ${r.dupCount} 条。`)
}

// ============ Tab2 异常检测 ============
const algo = ref('3sigma')
const repair = ref('clip')
const exprInput = ref('')
const exprFeedback = ref(null)
const anomalyResults = computed(() => state.lastAnomaly?.results || null)
const anomalySummary = computed(() => state.lastAnomaly?.summary || null)

const summaryPill = computed(() => {
  const segCols = d.value.columns.filter(c => c.type === 'float')
    .reduce((s, c) => s + detectMissingSegments(d.value.data, c.key, d.value.timeCol).length, 0)
  const totalMissingCells = missingStats.value.stats.reduce((s, c) => s + c.missing, 0)
  if (anomalySummary.value) {
    return `检测到 ${anomalySummary.value.totalAnomalies} 处异常 · 整体异常率 ${anomalySummary.value.overallRate.toFixed(2)}% · ${segCols} 段缺失(${totalMissingCells} 单元格)`
  }
  return `${segCols} 处缺失段（${totalMissingCells} 个缺失单元格）· 异常检测待执行`
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
// 复用 store 的白名单编译（表达式错误会抛异常）
function buildExprFn(expr) { return buildExprEvaluator(expr) }

const detecting = ref(false)
const iforestParams = reactive({ nEstimators: 200, contamination: 'auto' })

async function detectViaBackend() {
  if (!state.backend.online) {
    toast('error', '后端未连接：sklearn 完整版孤立森林需要 FastAPI 服务（默认 http://127.0.0.1:8000）。可先使用浏览器近似版。')
    return
  }
  const numCols = d.value.columns.filter(c => c.type === 'float')
  if (numCols.length === 0) { toast('warning', '没有数值列可检测'); return }
  detecting.value = true
  try {
    const payload = numCols.map(col => ({
      key: col.key,
      values: d.value.data.map(r => (isMissing(r[col.key]) ? null : Number(r[col.key])))
    }))
    const resp = await iforestViaBackend(payload, {
      n_estimators: Number(iforestParams.nEstimators) || 200,
      contamination: iforestParams.contamination === 'auto' ? 'auto' : Number(iforestParams.contamination),
      random_state: 42
    })
    const r = applyBackendAnomaly(resp)
    toast('success', `后端 ${resp.engine} 检测完成：${r.summary.totalAnomalies} 个异常点，涉及 ${r.summary.colsAffected}/${r.summary.numCols} 列`)
  } catch (e) {
    toast('error', `后端检测失败：${e.message}`)
  } finally {
    detecting.value = false
  }
}

function doDetect() {
  if (algo.value === 'iforest_sklearn') { detectViaBackend(); return }
  const r = detectAnomalies(algo.value, exprInput.value.trim())
  if (r.error) { toast('error', r.error); return }
  toast('success', `检测完成：${r.summary.totalAnomalies} 个异常点，涉及 ${r.summary.colsAffected}/${r.summary.numCols} 列`)
}
async function doRepair() {
  if (!state.lastAnomaly) { toast('warning', '请先执行检测'); return }
  try {
    await ElMessageBox.confirm(
      `将按 ${ANOMALY_REPAIRS[repair.value].split('：')[0]} 方案处理 ${state.lastAnomaly.summary.totalAnomalies} 个异常点，数据会被修改。确认继续？`,
      '执行修复', { type: 'warning' }
    )
  } catch (e) { return }
  const r = repairAnomalies(repair.value)
  if (r.error) { toast('error', r.error); return }
  toast('success', `修复完成：处理 ${r.touched} 个数据点`)
}
const anomalyCards = computed(() => {
  const s = anomalySummary.value
  const r = anomalyResults.value
  if (!s || !r) return null
  const totalCells = d.value.data.length * s.numCols
  return [
    { icon: 'fa-microscope', bg: 'bg-slate-100', fg: 'text-slate-600', label: '检测算法', value: ALGO_NAMES[state.lastAnomaly.algo] || state.lastAnomaly.algo },
    { icon: 'fa-database', bg: 'bg-slate-100', fg: 'text-slate-600', label: '检测行数', value: d.value.data.length.toLocaleString() },
    { icon: 'fa-triangle-exclamation', bg: 'bg-rose-50', fg: 'text-rose-600', label: '异常点总数', value: s.totalAnomalies.toLocaleString() },
    { icon: 'fa-percent', bg: s.overallRate > 5 ? 'bg-rose-50' : 'bg-emerald-50', fg: s.overallRate > 5 ? 'text-rose-600' : 'text-emerald-600', label: '整体异常率', value: s.overallRate.toFixed(2) + '%' },
    { icon: 'fa-table-columns', bg: 'bg-amber-50', fg: 'text-amber-600', label: '受影响列数', value: `${s.colsAffected} / ${s.numCols}` },
    { icon: 'fa-shield-halved', bg: 'bg-emerald-50', fg: 'text-emerald-600', label: '正常数据量', value: (totalCells - s.totalAnomalies).toLocaleString() }
  ]
})
const maxAnomaly = computed(() => Math.max(...(anomalyResults.value || []).map(r => r.anomalies), 1))

// ============ Tab3 掩码 ============
const maskName = ref('mask_curtailment')
function doGenerateMask() {
  if (!brushRange.value) { toast('warning', '请先在图表上拖拽框选要标记的时段'); return }
  const name = maskName.value.trim() || 'mask_1'
  const ones = generateMask(name, brushRange.value)
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

// ============ 生命周期 ============
function init() {
  if (chartEl.value && !chart) {
    chart = echarts.init(chartEl.value)
    chart.on('brushSelected', onBrushSelected)
  }
  renderChart()
}
function resize() { chart && chart.resize() }
onMounted(async () => { await nextTick(); init(); window.addEventListener('resize', resize) })
onActivated(() => nextTick(() => { resize(); renderChart() }))
onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chart && chart.dispose()
  chart = null
})
watch(() => state.dataVersion, () => nextTick(renderChart))
watch(tab, t => { if (t === 'mask') nextTick(() => chart && applyBrushCursor()) })
</script>

<template>
  <section class="step-panel h-full p-4 flex flex-col gap-3 overflow-y-auto">
    <!-- 诊断画布 -->
    <div class="h-[48%] min-h-[340px] shrink-0 bg-white rounded-xl border border-slate-200 shadow-sm p-2 flex flex-col relative">
      <div class="flex justify-between items-center px-3 pt-1">
        <div class="flex items-center space-x-3">
          <span class="text-xs font-bold text-slate-800">时序异常诊断与区间标注画布</span>
          <span class="text-[11px] bg-rose-50 text-rose-600 border border-rose-200 px-2 py-0.5 rounded-full font-medium">{{ summaryPill }}</span>
        </div>
        <div class="flex items-center gap-2">
          <button @click="toggleBrush"
                  class="px-2.5 py-1 text-xs rounded border font-medium flex items-center transition-colors"
                  :class="brushActive ? 'border-rose-400 bg-rose-50 text-rose-700' : 'border-indigo-300 text-indigo-700 hover:bg-indigo-50'">
            <i class="fa-solid fa-highlighter mr-1"></i><span>{{ brushActive ? '已激活刷选 (点击图表拖动)' : '开启时段刷选标记' }}</span>
          </button>
          <button @click="clearBrush" class="px-2.5 py-1 text-xs rounded border border-slate-200 text-slate-600 hover:bg-slate-100">清空框选</button>
        </div>
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
            </div>

            <div class="border border-slate-200 rounded-lg overflow-hidden mb-4">
              <div class="bg-slate-50 px-3 py-1.5 border-b border-slate-200 flex items-center justify-between">
                <span class="text-[11px] font-semibold text-slate-600">各列缺失详情 <span class="text-[10px] text-slate-400 font-normal ml-1">点击行查看缺失时间段</span></span>
                <span class="text-[10px] text-slate-400">共 {{ missingStats.totalRows.toLocaleString() }} 行 × {{ d.columns.length }} 列</span>
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
                    <tr v-for="s in missingStats.stats" :key="s.key"
                        class="hover:bg-amber-50/30" :class="s.missing > 0 ? 'cursor-pointer' : ''"
                        :style="segCol === s.key ? 'background:#fef3c7;outline:2px solid #f59e0b;outline-offset:-2px' : ''"
                        @click="s.missing > 0 && (segCol = s.key)">
                      <td class="px-3 py-1.5 text-slate-700 font-sans font-medium truncate max-w-[140px]">
                        {{ s.label }}
                        <i v-if="s.missing > 0" class="fa-solid fa-chevron-right text-[8px] text-amber-400 ml-1"></i>
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
                <div class="flex items-center gap-2">
                  <i class="fa-solid fa-clock-rotate-left text-amber-600 text-[11px]"></i>
                  <span class="text-[11px] font-bold text-amber-800">缺失时间段详情</span>
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-200 text-amber-800 font-mono">{{ colLabel(segCol) }}</span>
                  <span class="text-[10px] text-amber-600">共 {{ segList.length }} 个缺失段，{{ segTotalRows() }} 行</span>
                </div>
                <button @click="segCol = null" class="w-5 h-5 rounded flex items-center justify-center text-amber-400 hover:text-amber-700 hover:bg-amber-100">
                  <i class="fa-solid fa-xmark text-[11px]"></i>
                </button>
              </div>
              <div class="max-h-[220px] overflow-auto divide-y divide-amber-100">
                <div v-if="segList.length === 0" class="text-center py-6 text-amber-400 text-xs">
                  <i class="fa-solid fa-check-circle text-lg block mb-1"></i>该列无缺失数据
                </div>
                <div v-for="(seg, idx) in segList" :key="idx" class="px-3 py-2.5 flex items-center gap-3 hover:bg-amber-50 transition-colors">
                  <span class="w-5 h-5 rounded-full bg-amber-200 text-amber-800 text-[10px] font-bold flex items-center justify-center shrink-0">{{ idx + 1 }}</span>
                  <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 text-xs">
                      <span class="font-mono text-amber-900 font-medium">{{ (seg.startTime || '').replace(/^\d{4}-/, '') }}</span>
                      <i class="fa-solid fa-arrow-right text-amber-400 text-[9px]"></i>
                      <span class="font-mono text-amber-900 font-medium">{{ (seg.endTime || '').replace(/^\d{4}-/, '') }}</span>
                    </div>
                    <div class="text-[10px] text-amber-600 mt-0.5">
                      缺失 <strong>{{ seg.count }}</strong> 行
                      <span class="text-amber-400 mx-1">·</span>索引 {{ seg.startIdx }}–{{ seg.endIdx }}
                    </div>
                  </div>
                  <select :value="algoFor(idx)" @change="setAlgo(idx, $event)"
                          class="text-[11px] border border-amber-300 rounded px-2 py-1 bg-white focus:border-amber-500 outline-none">
                    <option v-for="o in IMPUTE_ALGOS" :key="o.value" :value="o.value">{{ o.label }}</option>
                  </select>
                  <button @click="doSegmentImpute(idx)" class="px-2.5 py-1 bg-amber-500 hover:bg-amber-600 text-white rounded text-[11px] font-semibold shadow-sm shrink-0">应用</button>
                </div>
                <div v-if="segList.length > 0" class="px-3 py-2 bg-amber-100/50 border-t border-amber-200 flex items-center justify-between">
                  <span class="text-[10px] text-amber-600">对所有缺失段执行各自选定的算法</span>
                  <button @click="doAllSegments" class="px-3 py-1 bg-amber-600 hover:bg-amber-700 text-white rounded text-[11px] font-bold shadow-sm">
                    <i class="fa-solid fa-wand-magic-sparkles mr-1 text-[9px]"></i>一键全部填补
                  </button>
                </div>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-5">
              <div>
                <label class="text-[11px] text-slate-500 block mb-1.5">重复时间戳合并策略</label>
                <select v-model="dupStrategy" class="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white focus:border-amber-400 outline-none">
                  <option value="mean">聚合取平均值 (Aggregate Mean)</option>
                  <option value="first">保留首个记录 (Keep First)</option>
                  <option value="last">保留最后记录 (Keep Last)</option>
                </select>
                <p class="text-[10px] text-slate-400 mt-1.5">当同一时间戳存在多条记录时，选择合并或去重策略</p>
              </div>
              <div class="flex items-end">
                <p class="text-[10px] text-slate-400 leading-relaxed">
                  <i class="fa-solid fa-circle-info mr-1 text-amber-500"></i>缺失值填补算法已置于上方各列详情中，点击行后可按时间段独立选择算法
                </p>
              </div>
            </div>
          </div>

          <div class="shrink-0 flex flex-col items-center justify-center h-full pl-5 border-l border-slate-100">
            <button @click="doImputeAll" class="w-40 py-3.5 bg-amber-500 hover:bg-amber-600 text-white rounded-xl text-xs font-bold shadow-md hover:shadow-lg transition-all flex flex-col items-center gap-1.5">
              <i class="fa-solid fa-check text-sm"></i>
              <span>执行填补与去重</span>
            </button>
            <p class="text-[10px] text-slate-400 mt-2 text-center w-40">自动检测缺失段并填充<br/>同时合并重复时间戳</p>
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
                  <option value="iforest">孤立森林 (浏览器近似版 · 完整版需后端)</option>
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
                <button @click="doRepair" class="flex-1 py-2 bg-slate-700 hover:bg-slate-800 text-white rounded-lg text-xs font-bold shadow-sm flex items-center justify-center gap-1.5">
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
              <div class="flex items-center gap-1.5 mt-2 flex-wrap">
                <span class="text-[10px] text-rose-400 mr-1">快捷模板:</span>
                <button v-for="tpl in ['v > 1000', 'v < 0', 'v > mean + 2*std', 'v < mean - 2*std', 'v > q3 + 1.5*(q3-q1) || v < q1 - 1.5*(q3-q1)']" :key="tpl"
                        @click="exprInput = tpl"
                        class="px-2 py-0.5 rounded text-[10px] bg-white border border-rose-200 text-rose-600 hover:bg-rose-100">{{ tpl }}</button>
              </div>
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
                <span class="text-[10px] text-slate-400">{{ state.lastAnomaly ? `${ALGO_NAMES[state.lastAnomaly.algo]} · ${state.lastAnomaly.time}` : '等待检测' }}</span>
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
                      <td class="px-3 py-1.5 text-right text-slate-500">{{ r.lower.toFixed(1) }}</td>
                      <td class="px-3 py-1.5 text-right text-slate-500">{{ r.upper.toFixed(1) }}</td>
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
                <p class="text-[10px] text-slate-400 mt-1.5">使用上方图表的「开启时段刷选标记」按钮框选</p>
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
                <button v-if="state.masks.length > 0" @click="deleteAllMasks" class="text-[10px] text-slate-400 hover:text-rose-600">
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
                    <button @click="deleteMask(idx)" title="删除此掩码"
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
