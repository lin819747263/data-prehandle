<script setup>
import { ref, computed, reactive, onMounted, onActivated, onBeforeUnmount, nextTick, watch } from 'vue'
import * as echarts from 'echarts'
import { state, ds, switchStep, rowCount } from '../store'
import { wsStats, wsHist, wsSeriesMulti } from '../api'

const COLORS = ['#4f46e5', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16', '#f97316', '#6366f1', '#14b8a6', '#e11d48']
const colorOf = i => COLORS[i % COLORS.length]

// 换新引用才能失效下游 computed（见 Step2Config 同款注释）
const d = computed(() => { void state.dataVersion; return { ...ds() } })
const floatCols = computed(() => d.value.columns.filter(c => c.type === 'float'))

// 本页三块数字各自一次后端整表计算：统计矩阵、叠加曲线、直方图
const gate = ref({ status: 'idle', note: '' })
const stats = ref(null)
const chart = ref(null)
const hist = ref(null)
const fetching = reactive({ stats: false, series: false, hist: false })
let loadedVersion = null

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
  refreshSeries()
}
function toggleAll(on) {
  selected.clear()
  if (on) floatCols.value.forEach(c => selected.add(c.key))
  refreshSeries()
}
function selectedList() {
  return floatCols.value.filter(c => selected.has(c.key)).map(c => c.key)
}

// 抽稀在后端执行（旧的那档 LTTB 是渲染期算法，随浏览器整表视图一起退场）
const MODE_LABELS = { raw: '原始全量', extremes: '极值抽稀', mean: '窗口均值' }
const downsample = ref('extremes')
const pointsCap = computed(() => state.backend.limits?.seriesMaxPoints || 3000)
const binsCount = computed(() => state.backend.limits?.histogramBins || 25)

// 时间窗口档位：由后端按自然周期筛行（explore.resolve_window），不是旧版那种「按采样间隔估百分比」
// 的假 3 天/7 天 —— 界面上写的窗口必须与真实行同源。offset 是在该档位的周期清单里翻到第几期。
const SPANS = [
  { key: 'all', label: '全量' },
  { key: 'year', label: '年' },
  { key: 'month', label: '月' },
  { key: 'week', label: '周' },
  { key: 'day', label: '日' }
]
const span = ref('all')
const periodOffset = ref(0)
const spanUnit = computed(() => SPANS.find(s => s.key === span.value)?.label || '')

// 翻页用的当前位置一律读后端回的那一份（window.offset）：越界由服务端贴边，
// 界面跟着改口径，才会出现「请求第 9 期、实际停在第 8 期」这种说得清的状态。
const win = computed(() => chart.value?.window || null)
const periodTotal = computed(() => (span.value === 'all' ? 1 : (win.value?.periodTotal || 0)))
const periodAt = computed(() => (win.value?.periodIndex || 0) - 1)
const canPrev = computed(() => span.value !== 'all' && periodAt.value > 0)
const canNext = computed(() => span.value !== 'all' && periodTotal.value > 0 && periodAt.value < periodTotal.value - 1)

function stepPeriod(delta) {
  if (!canPrev.value && !canNext.value) return
  if (delta < 0 && !canPrev.value) return
  if (delta > 0 && !canNext.value) return
  periodOffset.value = periodAt.value + delta
  zoom.start = 0
  zoom.end = 100
  refreshSeries()
}

const distCol = ref('')
watch(floatCols, () => {
  if (!floatCols.value.some(c => c.key === distCol.value)) distCol.value = floatCols.value[0]?.key || ''
}, { immediate: true })

const mainEl = ref(null)
const distEl = ref(null)
let chartMain = null
let chartDist = null

const displayRows = computed(() => (stats.value ? stats.value.rowCount : rowCount()))
const displayCols = computed(() => (stats.value ? stats.value.colCount : floatCols.value.length))
function fmt(v) { return v === null || v === undefined ? '—' : Number(v).toFixed(1) }

async function loadStats() {
  const keys = floatCols.value.map(c => c.key)
  if (!keys.length) { stats.value = null; return }
  fetching.stats = true
  try {
    stats.value = await wsStats(d.value.wsId, keys)
  } catch (e) {
    gate.value = { status: 'error', note: `统计矩阵获取失败：${e.message || e}` }
    return
  } finally {
    fetching.stats = false
  }
}

async function refreshSeries() {
  const keys = selectedList()
  if (!keys.length) { chart.value = null; drawMain(); return }
  fetching.series = true
  try {
    chart.value = await wsSeriesMulti(d.value.wsId, keys, downsample.value, pointsCap.value,
      span.value, periodOffset.value)
    periodOffset.value = chart.value?.window?.offset ?? 0
    if (gate.value.status === 'loading') gate.value = { status: 'ready', note: '' }
  } catch (e) {
    gate.value = { status: 'error', note: `曲线数据获取失败：${e.message || e}` }
    return
  } finally {
    fetching.series = false
  }
  drawMain()
}

async function refreshHist() {
  if (!distCol.value) { hist.value = null; drawDist(); return }
  fetching.hist = true
  try {
    hist.value = await wsHist(d.value.wsId, distCol.value, binsCount.value)
  } catch (e) {
    gate.value = { status: 'error', note: `直方图获取失败：${e.message || e}` }
    return
  } finally {
    fetching.hist = false
  }
  drawDist()
}

async function prepare(force = false) {
  if (!state.backend.online) { gate.value = { status: 'offline', note: '后端不在线：第三步的整表统计无从计算' }; return }
  if (!d.value.wsId) { gate.value = { status: 'noData', note: '尚未接入数据' }; return }
  const version = d.value.meta?.version
  if (!force && loadedVersion === version && gate.value.status === 'ready') return
  gate.value = { status: 'loading', note: '正在由后端整表计算统计矩阵、叠加曲线与直方图…' }
  await loadStats()
  await refreshSeries()
  await refreshHist()
  loadedVersion = version
  if (gate.value.status === 'loading') gate.value = { status: 'ready', note: '' }
}

// 用户拖出来的缩放窗口（百分比）：换列、换抽稀方式都不该被重置，
// 只有显式换时间窗口（换的是另一段行）才回到整段。旧实现每次 setOption 都带 start:0,end:30，
// 叠一份 notMerge，于是勾掉一列就等于把图缩回开头。
const zoom = reactive({ start: 0, end: 100 })
let zoomBound = false

function drawMain() {
  if (!chartMain) return
  const data = chart.value
  if (!data || !data.series || data.series.length === 0) { chartMain.clear(); return }

  const series = data.series.map((fd, i) => {
    const isFirst = i === 0
    return {
      name: fd.label || fd.col,
      type: 'line',
      data: fd.y,
      smooth: true,
      showSymbol: false,
      lineStyle: { width: isFirst ? 2 : 1.5, color: colorOf(i) },
      itemStyle: { color: colorOf(i) },
      areaStyle: isFirst ? {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: colorOf(i) + '30' },
          { offset: 1, color: colorOf(i) + '00' }
        ])
      } : undefined
    }
  })

  chartMain.setOption({
    title: {
      text: series.length === 1
        ? `时序曲线 — ${data.series[0].label || data.series[0].col}`
        : `多特征叠加曲线 (${series.length}条)`,
      left: 10, top: 5, textStyle: { fontSize: 13, color: '#334155' }
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
    legend: { top: 5, right: 10, textStyle: { fontSize: 10 }, itemWidth: 14, itemHeight: 8 },
    grid: { top: 40, right: 25, bottom: 65, left: 55 },
    dataZoom: [
      { type: 'inside', start: zoom.start, end: zoom.end },
      { type: 'slider', bottom: 10, height: 22, borderColor: '#cbd5e1', start: zoom.start, end: zoom.end }
    ],
    xAxis: { type: 'category', data: data.x, axisLine: { lineStyle: { color: '#94a3b8' } } },
    yAxis: { type: 'value', splitLine: { lineStyle: { type: 'dashed', color: '#e2e8f0' } } },
    series
  }, { replaceMerge: ['series', 'legend'] })
}

function drawDist() {
  if (!chartDist) return
  const h = hist.value
  if (!h || !h.edges || h.edges.length === 0) { chartDist.clear(); return }
  const binLabels = h.edges.map(e => Number(e).toFixed(1))

  chartDist.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { top: 25, right: 15, bottom: 28, left: 40 },
    xAxis: { type: 'category', data: binLabels, axisLabel: { fontSize: 9, rotate: 30 } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: '#f1f5f9' } } },
    series: [{
      name: '频次',
      type: 'bar',
      data: h.counts.map((v, i) => ({
        value: v,
        itemStyle: {
          color: (i === h.meanBin || i === h.medianBin)
            ? '#6366f1'
            : new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: '#a5b4fc' },
              { offset: 1, color: '#e0e7ff' }
            ]),
          borderRadius: [2, 2, 0, 0]
        }
      })),
      barWidth: '90%',
      markLine: {
        symbol: 'none',
        data: [
          { xAxis: binLabels[h.meanBin], name: `Mean(${h.mean.toFixed(1)})`, lineStyle: { color: '#ef4444', width: 1.5, type: 'dashed' }, label: { show: true, formatter: `μ${h.mean.toFixed(0)}`, fontSize: 9, color: '#ef4444' } },
          { xAxis: binLabels[h.medianBin], name: `Median(${h.median.toFixed(1)})`, lineStyle: { color: '#4f46e5', width: 2 }, label: { show: true, formatter: `M${h.median.toFixed(0)}`, fontSize: 9, color: '#4f46e5' } }
        ]
      }
    }]
  }, true)
}

function render() { drawMain(); drawDist() }

// 换档 = 换一段行（后端按自然周期重新筛），所以缩放窗口回到整段；
// 勾/取消列只是往同一窗口里多画或少画一条线，窗口保持用户当前看的范围。
function setSpan(next) {
  if (span.value === next) return
  span.value = next
  periodOffset.value = 0
  zoom.start = 0
  zoom.end = 100
  refreshSeries()
}

function bindZoom() {
  if (zoomBound || !chartMain) return
  zoomBound = true
  chartMain.on('dataZoom', () => {
    const dz = chartMain.getOption()?.dataZoom?.[0]
    if (!dz) return
    zoom.start = typeof dz.start === 'number' ? dz.start : 0
    zoom.end = typeof dz.end === 'number' ? dz.end : 100
  })
}

function initCharts() {
  if (mainEl.value && !chartMain) {
    chartMain = echarts.init(mainEl.value)
    bindZoom()
  }
  if (distEl.value && !chartDist) chartDist = echarts.init(distEl.value)
}
function resize() {
  chartMain && chartMain.resize()
  chartDist && chartDist.resize()
}

onMounted(async () => {
  await nextTick()
  initCharts()
  window.addEventListener('resize', resize)
  await prepare(true)
  render()
})
onActivated(async () => {
  nextTick(() => { resize(); render() })
  await prepare()
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chartMain && chartMain.dispose()
  chartDist && chartDist.dispose()
  chartMain = null
  chartDist = null
})

watch(downsample, refreshSeries)
watch(distCol, refreshHist)
// 第二步改过数据（重采样/删列/换算）后版本号变了，此前那份整表统计即作废
watch(() => d.value.meta?.version, () => { prepare(true) })
watch(() => state.backend.online, on => { if (on) prepare(true) })
</script>

<template>
  <section class="step-panel h-full p-4 flex flex-col gap-3 overflow-y-auto">
    <div v-if="gate.status !== 'ready'" class="rounded-xl border px-3 py-2 text-[11px] flex items-start gap-2 shrink-0"
         :class="gate.status === 'loading' ? 'bg-indigo-50 border-indigo-200 text-indigo-700' : 'bg-rose-50 border-rose-200 text-rose-700'">
      <i class="fa-solid mt-0.5" :class="gate.status === 'loading' ? 'fa-spinner fa-spin' : 'fa-triangle-exclamation'"></i>
      <div class="min-w-0">
        <div class="font-semibold">{{ gate.note || '正在计算…' }}</div>
      </div>
      <button v-if="gate.status !== 'loading'" @click="prepare(true)"
              class="ml-auto shrink-0 px-2 py-0.5 rounded border border-current opacity-70 hover:opacity-100">重试</button>
    </div>

    <!-- 图上的数字仍是离线前那一次真实计算的结果，说清楚免得被当成实时值 -->
    <div v-if="gate.status === 'ready' && !state.backend.online"
         class="rounded-lg bg-amber-50 border border-amber-200 text-amber-700 px-3 py-1.5 text-[11px] shrink-0">
      后端已离线：下方统计矩阵、曲线与直方图是离线前最后一次真实计算的结果，本页此时只读；重连后会自动重新计算。
    </div>

    <div class="bg-white p-3 rounded-xl border border-slate-200 shadow-sm flex flex-col gap-2.5 shrink-0">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-4">
          <div class="flex items-center gap-2">
            <label class="text-xs font-bold text-slate-600">多特征叠加曲线:</label>
            <div class="flex items-center gap-1.5">
              <button @click="toggleAll(true)" class="text-[10px] text-indigo-600 hover:underline">全选</button>
              <span class="text-slate-300">|</span>
              <button @click="toggleAll(false)" class="text-[10px] text-slate-500 hover:underline">清空</button>
            </div>
            <span class="text-[10px] text-slate-400 font-mono">已选 {{ selected.size }} 条</span>
          </div>
          <div class="flex items-center gap-2">
            <label class="text-xs font-bold text-slate-600">抽稀方式:</label>
            <select v-model="downsample" class="text-xs border border-slate-300 rounded px-2 py-1 bg-white">
              <option value="raw">原始全量（超后端点数上限时等距抽稀）</option>
              <option value="extremes">极值抽稀（每桶留最小/最大，尖峰不丢）</option>
              <option value="mean">窗口均值降采样（4 行一桶取均值）</option>
            </select>
          </div>
          <div class="flex items-center gap-1.5 text-xs">
            <span class="text-slate-400">时间窗口:</span>
            <button v-for="s in SPANS" :key="s.key" @click="setSpan(s.key)"
                    class="px-2 py-0.5 border rounded transition-colors"
                    :class="span === s.key ? 'border-indigo-400 bg-indigo-50 text-indigo-700 font-semibold' : 'border-slate-200 hover:bg-slate-100 text-slate-600'"
                    :title="s.key === 'all' ? '整表所有行' : `按${s.label}筛行（后端按时间列分周期），再用右侧箭头翻到上/下一个${s.label}`">
              {{ s.label }}
            </button>
            <!-- 翻页器：位置与总期数都来自后端那一份周期清单，两端按到底就灰掉，不做「点了再说」的假按钮 -->
            <div v-if="span !== 'all'" class="flex items-center gap-0.5 pl-1.5 ml-0.5 border-l border-slate-200">
              <button @click="stepPeriod(-1)" :disabled="!canPrev || fetching.series"
                      class="px-1.5 py-0.5 border border-slate-200 rounded text-slate-500 hover:bg-slate-100 disabled:opacity-30 disabled:cursor-not-allowed"
                      :title="canPrev ? `上一个${spanUnit}` : '已经是第一个周期'">
                <i class="fa-solid fa-chevron-left text-[9px]"></i>
              </button>
              <span class="text-[10px] font-mono text-slate-500 whitespace-nowrap min-w-[104px] text-center">
                <template v-if="fetching.series">后端筛行中…</template>
                <template v-else-if="periodTotal">第 {{ periodAt + 1 }}/{{ periodTotal }} 个{{ spanUnit }}</template>
                <template v-else>无可翻周期</template>
              </span>
              <button @click="stepPeriod(1)" :disabled="!canNext || fetching.series"
                      class="px-1.5 py-0.5 border border-slate-200 rounded text-slate-500 hover:bg-slate-100 disabled:opacity-30 disabled:cursor-not-allowed"
                      :title="canNext ? `下一个${spanUnit}` : '已经是最后一个周期'">
                <i class="fa-solid fa-chevron-right text-[9px]"></i>
              </button>
            </div>
            <span v-if="span !== 'all' && chart" class="text-[10px] text-slate-400 font-mono whitespace-nowrap">
              {{ chart.window?.from || '' }} ~ {{ chart.window?.to || '' }} · 窗口内 {{ (chart.windowRows ?? 0).toLocaleString() }} 行
            </span>
            <span v-else-if="span === 'all' && chart" class="text-[10px] text-slate-400 font-mono whitespace-nowrap">
              不分期 · 全表 {{ (chart.windowRows ?? 0).toLocaleString() }} 行
            </span>
          </div>
        </div>
      </div>
      <div class="flex flex-wrap gap-1.5">
        <label v-for="(c, i) in floatCols" :key="c.key"
               class="inline-flex items-center gap-1 px-2 py-1 rounded-md border cursor-pointer transition-all text-[11px] font-medium"
               :class="selected.has(c.key) ? 'bg-indigo-50 border-indigo-300 text-indigo-700' : 'bg-white border-slate-200 text-slate-600 hover:border-indigo-200 hover:bg-indigo-50/50'">
          <input type="checkbox" class="accent-indigo-500" :checked="selected.has(c.key)" @change="toggleCol(c.key)" />
          <span class="w-2 h-2 rounded-full shrink-0" :style="{ background: colorOf(i) }"></span>
          <span>{{ c.label }}</span>
        </label>
      </div>
    </div>

    <div class="h-[52%] min-h-[360px] shrink-0 bg-white rounded-xl border border-slate-200 shadow-sm p-2 flex flex-col">
      <div ref="mainEl" class="w-full flex-1"></div>
      <div class="px-2 pb-1 text-[10px] text-slate-400 font-mono flex items-center gap-2 shrink-0">
        <span v-if="fetching.series"><i class="fa-solid fa-spinner fa-spin mr-1"></i>后端抽稀中…</span>
        <template v-else-if="chart">
          <span>整表 {{ chart.rowCount.toLocaleString() }} 行</span>
          <span v-if="chart.window && chart.window.span !== 'all'" class="text-indigo-600">
            → 第 {{ chart.window.periodIndex }}/{{ chart.window.periodTotal }} 个{{ chart.window.label }}
            {{ chart.window.from }} ~ {{ chart.window.to }}（含起不含止）{{ chart.windowRows.toLocaleString() }} 行
            <span v-if="chart.window.clamped" class="text-amber-600">（该档只有 {{ chart.window.periodTotal }} 期，已贴到端点）</span>
          </span>
          <span>→ 图上 {{ chart.points.toLocaleString() }} 点 · {{ MODE_LABELS[chart.mode] }}</span>
          <span v-if="chart.decimated" class="text-amber-600">已抽稀（放大不会补回被抽掉的行）</span>
          <span v-else class="text-emerald-600">逐行未抽稀</span>
          <span v-if="chart.windowStep > 1">窗口 {{ chart.windowStep }} 行</span>
        </template>
      </div>
    </div>

    <div class="flex-1 grid grid-cols-12 gap-3 min-h-[300px]">
      <div class="col-span-7 bg-white rounded-xl border border-slate-200 shadow-sm p-3 flex flex-col">
        <div class="flex justify-between items-center mb-2">
          <span class="text-xs font-bold text-slate-700 flex items-center gap-1.5">
            <i class="fa-solid fa-table-columns text-indigo-500 text-[10px]"></i>多列统计特征矩阵
          </span>
          <span class="text-[10px] text-slate-400 font-mono">
            <span v-if="fetching.stats"><i class="fa-solid fa-spinner fa-spin mr-1"></i>后端整表统计中…</span>
            <template v-else>
              {{ displayCols }} 列 × {{ displayRows.toLocaleString() }} 行
              <span :class="gate.status === 'ready' ? 'text-emerald-600' : 'text-rose-500'">
                · {{ gate.status === 'ready' ? '后端整表统计（Std 为总体标准差 ÷n）' : '统计未就绪' }}
              </span>
            </template>
          </span>
        </div>
        <div class="flex-1 overflow-auto">
          <table class="w-full text-[11px] text-left border-collapse">
            <thead class="sticky top-0 bg-slate-100 text-slate-600 border-b border-slate-200 z-10">
              <tr>
                <th class="px-2 py-1.5 font-semibold whitespace-nowrap">列名</th>
                <th class="px-2 py-1.5 font-semibold text-right whitespace-nowrap">Count</th>
                <th class="px-2 py-1.5 font-semibold text-right whitespace-nowrap">Mean</th>
                <th class="px-2 py-1.5 font-semibold text-right whitespace-nowrap">Std</th>
                <th class="px-2 py-1.5 font-semibold text-right whitespace-nowrap">Min</th>
                <th class="px-2 py-1.5 font-semibold text-right whitespace-nowrap">Q1</th>
                <th class="px-2 py-1.5 font-semibold text-right whitespace-nowrap">Median</th>
                <th class="px-2 py-1.5 font-semibold text-right whitespace-nowrap">Q3</th>
                <th class="px-2 py-1.5 font-semibold text-right whitespace-nowrap">Max</th>
                <th class="px-2 py-1.5 font-semibold text-right whitespace-nowrap">缺失率</th>
                <th class="px-2 py-1.5 font-semibold whitespace-nowrap w-24">分布条</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 font-mono">
              <tr v-for="s in (stats && stats.rows) || []" :key="s.key"
                  class="transition-colors" :class="s.key === distCol ? 'bg-indigo-50/50' : 'hover:bg-slate-50/50'">
                <td class="px-2 py-1.5 font-sans font-medium text-slate-700 whitespace-nowrap max-w-[100px] truncate" :title="s.label">
                  <span class="inline-flex items-center gap-1">
                    <span class="w-1.5 h-1.5 rounded-full" :class="s.key === distCol ? 'bg-indigo-500' : 'bg-slate-300'"></span>
                    {{ s.label }}
                  </span>
                </td>
                <td class="px-2 py-1.5 text-right text-slate-600">{{ s.n.toLocaleString() }}</td>
                <td class="px-2 py-1.5 text-right font-semibold text-indigo-600">{{ fmt(s.mean) }}</td>
                <td class="px-2 py-1.5 text-right text-slate-600">{{ fmt(s.std) }}</td>
                <td class="px-2 py-1.5 text-right text-slate-500">{{ fmt(s.min) }}</td>
                <td class="px-2 py-1.5 text-right text-emerald-600">{{ fmt(s.q1) }}</td>
                <td class="px-2 py-1.5 text-right font-semibold text-indigo-600">{{ fmt(s.median) }}</td>
                <td class="px-2 py-1.5 text-right text-emerald-600">{{ fmt(s.q3) }}</td>
                <td class="px-2 py-1.5 text-right text-slate-500">{{ fmt(s.max) }}</td>
                <td class="px-2 py-1.5 text-right font-semibold"
                    :class="s.missingRate === 0 ? 'text-emerald-600' : s.missingRate < 5 ? 'text-amber-600' : 'text-rose-600'">{{ s.missingRate.toFixed(1) }}%</td>
                <td class="px-2 py-1.5">
                  <div v-if="s.n > 0" class="w-full h-2 bg-slate-100 rounded-full overflow-hidden relative">
                    <div class="absolute h-full bg-indigo-400/60 rounded-full"
                         :style="{ left: Math.max(0, s.min / stats.globalMax * 100) + '%', width: Math.max(2, Math.min(100, s.max / stats.globalMax * 100) - Math.max(0, s.min / stats.globalMax * 100)) + '%' }"></div>
                    <div class="absolute h-full w-0.5 bg-indigo-600" :style="{ left: (s.median / stats.globalMax * 100) + '%' }"></div>
                  </div>
                  <span v-else class="text-[9px] text-slate-400 font-sans">全列缺失</span>
                </td>
              </tr>
              <tr v-if="!((stats && stats.rows) || []).length && !fetching.stats">
                <td colspan="11" class="px-2 py-6 text-center text-slate-400 font-sans">
                  {{ gate.status === 'ready' ? '没有可统计的数值列' : '等待后端统计结果' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="col-span-5 bg-white rounded-xl border border-slate-200 shadow-sm p-2 flex flex-col">
        <div class="flex items-center justify-between px-2 pt-1 gap-2">
          <span class="text-xs font-bold text-slate-700 shrink-0">数据分布直方图</span>
          <select v-model="distCol" class="text-[11px] border border-slate-200 rounded px-1.5 py-0.5 bg-white text-indigo-700 font-medium max-w-[180px] truncate outline-none focus:border-indigo-400">
            <option v-for="c in floatCols" :key="c.key" :value="c.key">{{ c.label }}</option>
          </select>
        </div>
        <div ref="distEl" class="w-full flex-1"></div>
        <div class="px-2 pb-1 text-[10px] text-slate-400 font-mono shrink-0">
          <span v-if="fetching.hist"><i class="fa-solid fa-spinner fa-spin mr-1"></i>后端整表计数中…</span>
          <span v-else-if="hist && hist.edges.length">
            {{ hist.bins }} 桶 · {{ hist.n.toLocaleString() }} 个有效值 · 桶宽 {{ hist.binWidth.toFixed(3) }}
            <span v-if="hist.missing" class="text-amber-600">· {{ hist.missing.toLocaleString() }} 缺失（不计桶）</span>
          </span>
          <span v-else>该列没有有效数值</span>
        </div>
      </div>
    </div>

    <div class="flex justify-between items-center shrink-0">
      <button @click="switchStep(2)" class="px-4 py-1.5 bg-slate-200 hover:bg-slate-300 text-slate-700 rounded-lg text-xs font-medium">
        <i class="fa-solid fa-arrow-left mr-1"></i>返回数据接入
      </button>
      <button @click="switchStep(4)" class="px-5 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-xs font-semibold shadow">
        下一步：数据质量清洗 <i class="fa-solid fa-arrow-right ml-1"></i>
      </button>
    </div>
  </section>
</template>
