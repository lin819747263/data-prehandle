<script setup>
import { ref, computed, reactive, onMounted, onActivated, onBeforeUnmount, nextTick, watch } from 'vue'
import * as echarts from 'echarts'
import { state, ds, switchStep, detectedFreqMinutes } from '../store'
import { isMissing } from '../utils'

const COLORS = ['#4f46e5', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16', '#f97316', '#6366f1', '#14b8a6', '#e11d48']
const colorOf = i => COLORS[i % COLORS.length]

// 换新引用才能失效下游 computed（见 Step2Config 同款注释）
const d = computed(() => { void state.dataVersion; return { ...ds() } })
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
  render()
}
function toggleAll(on) {
  selected.clear()
  if (on) floatCols.value.forEach(c => selected.add(c.key))
  render()
}
function selectedList() {
  return floatCols.value.filter(c => selected.has(c.key)).map(c => c.key)
}

const downsample = ref('lttb')
const distCol = ref('')
watch(floatCols, () => {
  if (!floatCols.value.some(c => c.key === distCol.value)) distCol.value = floatCols.value[0]?.key || ''
}, { immediate: true })

const mainEl = ref(null)
const distEl = ref(null)
let chartMain = null
let chartDist = null

const statsMatrix = computed(() => {
  const data = d.value.data
  const totalRows = data.length
  let globalMax = 1
  const rows = floatCols.value.map(col => {
    const vals = data.map(r => r[col.key]).filter(v => !isMissing(v) && !isNaN(Number(v))).map(Number).sort((a, b) => a - b)
    const missing = totalRows - vals.length
    const missingRate = totalRows > 0 ? missing / totalRows * 100 : 0
    if (vals.length === 0) {
      return { label: col.label, key: col.key, n: 0, mean: 0, std: 0, min: 0, q1: 0, median: 0, q3: 0, max: 0, missing, missingRate }
    }
    const n = vals.length
    const min = vals[0], max = vals[n - 1]
    const mean = vals.reduce((s, v) => s + v, 0) / n
    const median = n % 2 === 0 ? (vals[n / 2 - 1] + vals[n / 2]) / 2 : vals[Math.floor(n / 2)]
    const q1 = vals[Math.floor(n * 0.25)]
    const q3 = vals[Math.floor(n * 0.75)]
    const std = Math.sqrt(vals.reduce((s, v) => s + (v - mean) ** 2, 0) / n)
    if (max > globalMax) globalMax = max
    return { label: col.label, key: col.key, n, mean, std, min, q1, median, q3, max, missing, missingRate }
  })
  return { rows, globalMax, totalRows, cols: floatCols.value.length }
})

function downsampleValues(vals) {
  if (downsample.value !== 'mean' || vals.length <= 500) return vals
  const step = 4, result = []
  for (let i = 0; i < vals.length; i += step) {
    const chunk = vals.slice(i, i + step).filter(v => !isMissing(v)).map(Number)
    result.push(chunk.length ? parseFloat((chunk.reduce((a, b) => a + b, 0) / chunk.length).toFixed(2)) : null)
  }
  return result
}

function render() {
  if (!chartMain) return
  const data = d.value.data
  const feats = selectedList()
  if (feats.length === 0) { chartMain.clear(); return }

  const timestamps = data.map(r => r[d.value.timeCol])
  const displayTs = downsample.value === 'mean' && timestamps.length > 500
    ? timestamps.filter((_, i) => i % 4 === 0) : timestamps

  const featData = feats.map(key => {
    const raw = data.map(r => r[key])
    const dsVals = downsampleValues(raw)
    return { key, raw, dsVals }
  })

  const series = featData.map((fd, i) => {
    const isFirst = i === 0
    return {
      name: d.value.columns.find(c => c.key === fd.key)?.label || fd.key,
      type: 'line',
      data: fd.dsVals,
      smooth: true,
      showSymbol: false,
      sampling: downsample.value === 'lttb' ? 'lttb' : false,
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
      text: feats.length === 1 ? `时序曲线 — ${feats[0]}` : `多特征叠加曲线 (${feats.length}条)`,
      left: 10, top: 5, textStyle: { fontSize: 13, color: '#334155' }
    },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross' },
      formatter(params) {
        if (!params || params.length === 0) return ''
        let tip = `<div style="font-size:11px"><b>${params[0].axisValue}</b><br/>`
        params.forEach(p => {
          const val = p.value !== null && p.value !== undefined ? Number(p.value).toFixed(2) : 'NaN'
          tip += `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${p.color};margin-right:4px"></span>${p.seriesName}: <b>${val}</b><br/>`
        })
        return tip + '</div>'
      }
    },
    legend: { top: 5, right: 10, textStyle: { fontSize: 10 }, itemWidth: 14, itemHeight: 8 },
    grid: { top: 40, right: 25, bottom: 65, left: 55 },
    dataZoom: [
      { type: 'inside', start: 0, end: 30 },
      { type: 'slider', bottom: 10, height: 22, borderColor: '#cbd5e1' }
    ],
    xAxis: { type: 'category', data: displayTs, axisLine: { lineStyle: { color: '#94a3b8' } } },
    yAxis: { type: 'value', splitLine: { lineStyle: { type: 'dashed', color: '#e2e8f0' } } },
    series
  }, true)

  if (!distCol.value || !feats.includes(distCol.value)) distCol.value = feats[0]
  renderDist()
}

function renderDist() {
  if (!chartDist || !distCol.value) return
  const colKey = distCol.value
  const vals = d.value.data.map(r => r[colKey]).filter(v => !isMissing(v) && !isNaN(Number(v))).map(Number).sort((a, b) => a - b)
  if (vals.length === 0) { chartDist.clear(); return }

  const n = vals.length
  const min = vals[0], max = vals[n - 1]
  const mean = vals.reduce((s, v) => s + v, 0) / n
  const median = n % 2 === 0 ? (vals[n / 2 - 1] + vals[n / 2]) / 2 : vals[Math.floor(n / 2)]
  const binCount = 25
  const binWidth = (max - min) / binCount || 1
  const bins = new Array(binCount).fill(0)
  const binLabels = []
  for (let i = 0; i < binCount; i++) binLabels.push((min + i * binWidth).toFixed(1))
  vals.forEach(v => {
    let idx = Math.floor((v - min) / binWidth)
    if (idx >= binCount) idx = binCount - 1
    bins[idx]++
  })
  const meanBinIdx = Math.min(binCount - 1, Math.max(0, Math.floor(((mean - min) / (max - min || 1)) * binCount)))
  const medianBinIdx = Math.min(binCount - 1, Math.max(0, Math.floor(((median - min) / (max - min || 1)) * binCount)))

  chartDist.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { top: 25, right: 15, bottom: 28, left: 40 },
    xAxis: { type: 'category', data: binLabels, axisLabel: { fontSize: 9, rotate: 30 } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: '#f1f5f9' } } },
    series: [{
      name: '频次',
      type: 'bar',
      data: bins.map((v, i) => ({
        value: v,
        itemStyle: {
          color: (i === meanBinIdx || i === medianBinIdx)
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
          { xAxis: binLabels[meanBinIdx], name: `Mean(${mean.toFixed(1)})`, lineStyle: { color: '#ef4444', width: 1.5, type: 'dashed' }, label: { show: true, formatter: `μ${mean.toFixed(0)}`, fontSize: 9, color: '#ef4444' } },
          { xAxis: binLabels[medianBinIdx], name: `Median(${median.toFixed(1)})`, lineStyle: { color: '#4f46e5', width: 2 }, label: { show: true, formatter: `M${median.toFixed(0)}`, fontSize: 9, color: '#4f46e5' } }
        ]
      }
    }]
  }, true)
}

function quickZoom(days) {
  if (!chartMain) return
  const total = d.value.data.length
  const freq = Math.max(1, Math.round(1440 / (detectedFreqMinutes() || 15)))
  const percentage = Math.min(100, (days * freq / total) * 100)
  chartMain.dispatchAction({ type: 'dataZoom', start: 0, end: percentage })
}

function initCharts() {
  if (mainEl.value && !chartMain) chartMain = echarts.init(mainEl.value)
  if (distEl.value && !chartDist) chartDist = echarts.init(distEl.value)
  render()
}
function resize() {
  chartMain && chartMain.resize()
  chartDist && chartDist.resize()
}

onMounted(async () => {
  await nextTick()
  initCharts()
  window.addEventListener('resize', resize)
})
onActivated(() => {
  nextTick(() => { resize(); render() })
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chartMain && chartMain.dispose()
  chartDist && chartDist.dispose()
  chartMain = null
  chartDist = null
})
watch(downsample, render)
watch(distCol, renderDist)
</script>

<template>
  <section class="step-panel h-full p-4 flex flex-col gap-3 overflow-y-auto">
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
            <label class="text-xs font-bold text-slate-600">降采样:</label>
            <select v-model="downsample" class="text-xs border border-slate-300 rounded px-2 py-1 bg-white">
              <option value="none">原始全量</option>
              <option value="lttb">LTTB 极值采样</option>
              <option value="mean">窗口均值降采样</option>
            </select>
          </div>
          <div class="flex items-center gap-1.5 text-xs">
            <span class="text-slate-400">跨度:</span>
            <button @click="quickZoom(3)" class="px-2 py-0.5 border border-slate-200 rounded hover:bg-slate-100 text-slate-600">3天</button>
            <button @click="quickZoom(7)" class="px-2 py-0.5 border border-slate-200 rounded hover:bg-slate-100 text-slate-600">7天</button>
            <button @click="quickZoom(30)" class="px-2 py-0.5 border border-slate-200 rounded hover:bg-slate-100 text-slate-600">全量</button>
          </div>
        </div>
        <div class="text-[11px] text-slate-400 flex items-center">
          <i class="fa-solid fa-mouse-pointer mr-1"></i>拖拽缩放 · 底部滑块长距拖动
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
    </div>

    <div class="flex-1 grid grid-cols-12 gap-3 min-h-[300px]">
      <div class="col-span-7 bg-white rounded-xl border border-slate-200 shadow-sm p-3 flex flex-col">
        <div class="flex justify-between items-center mb-2">
          <span class="text-xs font-bold text-slate-700 flex items-center gap-1.5">
            <i class="fa-solid fa-table-columns text-indigo-500 text-[10px]"></i>多列统计特征矩阵
          </span>
          <span class="text-[10px] text-slate-400 font-mono">{{ statsMatrix.cols }} 列 × {{ statsMatrix.totalRows.toLocaleString() }} 行</span>
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
              <tr v-for="s in statsMatrix.rows" :key="s.key"
                  class="transition-colors" :class="s.key === distCol ? 'bg-indigo-50/50' : 'hover:bg-slate-50/50'">
                <td class="px-2 py-1.5 font-sans font-medium text-slate-700 whitespace-nowrap max-w-[100px] truncate" :title="s.label">
                  <span class="inline-flex items-center gap-1">
                    <span class="w-1.5 h-1.5 rounded-full" :class="s.key === distCol ? 'bg-indigo-500' : 'bg-slate-300'"></span>
                    {{ s.label }}
                  </span>
                </td>
                <td class="px-2 py-1.5 text-right text-slate-600">{{ s.n.toLocaleString() }}</td>
                <td class="px-2 py-1.5 text-right font-semibold text-indigo-600">{{ s.n > 0 ? s.mean.toFixed(1) : '—' }}</td>
                <td class="px-2 py-1.5 text-right text-slate-600">{{ s.n > 0 ? s.std.toFixed(1) : '—' }}</td>
                <td class="px-2 py-1.5 text-right text-slate-500">{{ s.n > 0 ? s.min.toFixed(1) : '—' }}</td>
                <td class="px-2 py-1.5 text-right text-emerald-600">{{ s.n > 0 ? s.q1.toFixed(1) : '—' }}</td>
                <td class="px-2 py-1.5 text-right font-semibold text-indigo-600">{{ s.n > 0 ? s.median.toFixed(1) : '—' }}</td>
                <td class="px-2 py-1.5 text-right text-emerald-600">{{ s.n > 0 ? s.q3.toFixed(1) : '—' }}</td>
                <td class="px-2 py-1.5 text-right text-slate-500">{{ s.n > 0 ? s.max.toFixed(1) : '—' }}</td>
                <td class="px-2 py-1.5 text-right font-semibold"
                    :class="s.missingRate === 0 ? 'text-emerald-600' : s.missingRate < 5 ? 'text-amber-600' : 'text-rose-600'">{{ s.missingRate.toFixed(1) }}%</td>
                <td class="px-2 py-1.5">
                  <div class="w-full h-2 bg-slate-100 rounded-full overflow-hidden relative">
                    <div class="absolute h-full bg-indigo-400/60 rounded-full"
                         :style="{ left: Math.max(0, s.min / statsMatrix.globalMax * 100) + '%', width: Math.max(2, Math.min(100, s.max / statsMatrix.globalMax * 100) - Math.max(0, s.min / statsMatrix.globalMax * 100)) + '%' }"></div>
                    <div class="absolute h-full w-0.5 bg-indigo-600" :style="{ left: (s.median / statsMatrix.globalMax * 100) + '%' }"></div>
                  </div>
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
