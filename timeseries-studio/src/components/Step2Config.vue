<script setup>
import { ref, computed, reactive, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import {
  state, ds, touch, switchStep, toast,
  detectTimeFormat, convertTimeColumn, detectedFreqMinutes,
  resampleDataset, currentMissingRate, currentDuplicateRate,
  EXO_PRESETS, addExoPreset, addExoFormula, addExoFileVars, mergeExoVars,
  applyDerivedCol, clearAllDerivedCols, deleteDerivedCol, computeTermChain,
  renameColumn, deleteColumn, convertColumnUnit, splitCounts, setSplitRatio
} from '../store'
import {
  convertSingleTime, convertWithCustomFormat, isMissing,
  parseDataFile, alignExoRows, UNIT_CONVERSIONS, RESAMPLE_RATE_MAP, formatDate
} from '../utils'

// datasets 是普通对象，ds() 恒返回同一引用；下游 computed 读的是它的属性，
// 引用不变就不会失效，重采样/填补后仍会拿到缓存。所以版本号一变就换新引用。
const d = computed(() => { void state.dataVersion; return { ...ds() } })
const numericCols = computed(() => d.value.columns.filter(c => c.type === 'float'))

// ============ 时间列识别与转换 ============
const timeCol = ref(d.value.timeCol)
// 换数据集要重置本地选择；同一数据集内的增删改（touch）不能重置，否则冲掉用户手选。
// 只 watch d 会在每次 touch 触发，只 watch timeCol 又漏掉「两个数据集同名时间列」的切换。
watch([() => state.currentKey, () => d.value.timeCol], ([, v]) => { timeCol.value = v })
const detected = ref(null)
const targetFmt = ref('YYYY-MM-DD HH:mm:ss')
const customFmt = ref('DD/MM/YYYY HH:mm')
const convertPreview = ref(null)
const convertStatus = ref(null)

function runDetect(silent = false) {
  const r = detectTimeFormat(timeCol.value)
  if (!r) { if (!silent) toast('warning', '该列没有可用的时间样本，无法识别格式'); return }
  detected.value = r
  const samples = d.value.data.slice(0, 3).map(row => row[timeCol.value])
  convertPreview.value = {
    beforeFmt: r.format, afterFmt: targetFmt.value,
    before: samples.map(s => String(s)),
    after: samples.map(s => targetFmt.value === 'custom' ? convertWithCustomFormat(s, customFmt.value) : convertSingleTime(s, targetFmt.value))
  }
  if (!silent) convertStatus.value = null
}

// 进入步骤或切换时间列/数据时自动识别（静默）
watch([timeCol, () => d.value.data.length], () => runDetect(true), { immediate: true })
watch(targetFmt, () => { if (detected.value) runDetect(true) })

function runConvert() {
  const changed = convertTimeColumn(timeCol.value, targetFmt.value, customFmt.value)
  convertStatus.value = { ok: true, text: `已完成 ${d.value.data.length.toLocaleString()} 行时间格式转换，${changed} 个值发生变化。` }
  toast('success', '时间格式转换完成')
}

// ============ 采样频率与重采样 ============
const targetRate = ref('60min')
const resampleMethod = ref('mean')
const srcFreq = computed(() => { void state.dataVersion; return detectedFreqMinutes() || 15 })
const missingRate = computed(() => { void state.dataVersion; return currentMissingRate() })
const duplicateRate = computed(() => { void state.dataVersion; return currentDuplicateRate() })
const projectedCount = computed(() => {
  const rows = d.value.data
  if (rows.length < 2) return rows.length
  const t0 = Date.parse(String(rows[0][d.value.timeCol]).replace(/-/g, '/'))
  const t1 = Date.parse(String(rows[rows.length - 1][d.value.timeCol]).replace(/-/g, '/'))
  if (isNaN(t0) || isNaN(t1)) return rows.length
  return Math.floor(Math.abs(t1 - t0) / 60000 / RESAMPLE_RATE_MAP[targetRate.value]) + 1
})

async function confirmResample() {
  try {
    await ElMessageBox.confirm(
      `将把 ${d.value.data.length.toLocaleString()} 行重采样为 ${targetRate.value} 粒度（约 ${projectedCount.value.toLocaleString()} 行），当前数据会被替换。确认继续？`,
      '重采样确认', { type: 'warning' }
    )
  } catch (e) { return }
  const r = resampleDataset(targetRate.value, resampleMethod.value)
  toast('success', `重采样完成：${r.oldCount.toLocaleString()} 行 → ${r.newCount.toLocaleString()} 行`)
}

// ============ 外生变量 ============
const exoMode = ref('preset')
const presetKey = ref('')
const presetAlias = ref('')
const formulaName = ref('')
const formulaExpr = ref('')
const exoTimeCol = ref('timestamp')
const exoMerge = ref('left')
const exoFileInput = ref(null)
const exoFileBusy = ref(false)

function doAddPreset() {
  if (!presetKey.value) { toast('warning', '请先选择一个预设外生变量'); return }
  if (addExoPreset(presetKey.value, presetAlias.value.trim())) {
    toast('success', `已添加外生变量 [${presetAlias.value.trim() || presetKey.value}]（按主表时间规律生成的模拟序列）`)
    presetAlias.value = ''
  }
}

function doAddFormula() {
  const name = formulaName.value.trim()
  if (!name || !formulaExpr.value.trim()) { toast('warning', '请填写变量名称与生成公式'); return }
  const r = addExoFormula(name, formulaExpr.value.trim())
  if (r.ok) { toast('success', `已生成外生变量 [${name}]`); formulaName.value = ''; formulaExpr.value = '' }
  else toast('error', `公式无效：${r.error}`)
}

async function onExoFile(ev) {
  const file = ev.target.files && ev.target.files[0]
  ev.target.value = ''
  if (!file) return
  exoFileBusy.value = true
  try {
    const parsed = await parseDataFile(file)
    const mainTimes = d.value.data.map(r => r[d.value.timeCol])
    const aligned = alignExoRows(parsed.data, exoTimeCol.value, mainTimes, exoMerge.value)
    if (aligned.keys.length === 0) { toast('warning', '文件中除时间列外没有可导入的变量列'); return }
    const vars = aligned.keys.map(k => ({ key: k, label: k, data: aligned.values[k] }))
    addExoFileVars(file.name, vars)
    toast('success', `已导入 ${vars.length} 列，按「${exMergeLabel()}」对齐匹配 ${aligned.matchedCount.toLocaleString()} / ${mainTimes.length.toLocaleString()} 行时间戳`)
  } catch (e) {
    toast('error', `外生变量文件解析失败：${e.message}`)
  } finally {
    exoFileBusy.value = false
  }
}
function exMergeLabel() { return exoMerge.value === 'left' ? '左连接' : exoMerge.value === 'inner' ? '内连接' : '最近邻' }

function doRemoveExo(idx) { state.exoVars.splice(idx, 1) }
async function doClearExo() {
  if (state.exoVars.length === 0) return
  try { await ElMessageBox.confirm(`确认清空全部 ${state.exoVars.length} 个外生变量？`, '清空确认', { type: 'warning' }) } catch (e) { return }
  state.exoVars = []
}
function doMergeExo() {
  const r = mergeExoVars()
  toast('success', `成功合并 ${r.count} 个外生变量到主数据集${r.newCols ? `（新增 ${r.newCols} 列）` : ''}`)
}

// ============ 列运算 ============
const terms = reactive([])
function initTerms() {
  terms.splice(0, terms.length)
  const cols = numericCols.value
  if (cols.length >= 2) terms.push({ col: cols[0].key }, { op: '+', col: cols[1].key })
  else if (cols.length === 1) terms.push({ col: cols[0].key }, { op: '+', col: cols[0].key })
}
initTerms()
watch(() => d.value.columns.length, () => { if (terms.length === 0) initTerms() })

function addTerm() {
  const cols = numericCols.value
  if (cols.length === 0) { toast('warning', '没有可用于运算的数值列'); return }
  terms.push({ op: '-', col: cols[cols.length - 1].key })
}
function removeTerm(i) { terms.splice(i, 1) }

const newColName = ref('')
const calcPreview = ref(null)
const OP_LABEL = { '+': '+', '-': '-', '*': '×', '/': '÷' }
const formulaText = computed(() => terms.map((t, i) => {
  const label = d.value.columns.find(c => c.key === t.col)?.label || t.col
  return i === 0 ? label : `${OP_LABEL[t.op] || t.op} ${label}`
}).join(' '))

function previewCalc() {
  if (terms.length < 2) { toast('warning', '至少需要两个操作列'); return }
  const samples = d.value.data.slice(0, 3).map(row => {
    const v = computeTermChain(terms.map(t => ({ ...t })), row)
    return v === null ? 'NaN' : v.toFixed(2)
  })
  calcPreview.value = { formula: formulaText.value, samples }
}

function doApplyCalc() {
  const name = newColName.value.trim().replace(/\s+/g, '_')
  if (!name) { toast('warning', '请填写新列名'); return }
  if (terms.length < 2) { toast('warning', '至少需要两个操作列'); return }
  if (applyDerivedCol(name, terms.map(t => ({ ...t })))) {
    toast('success', `已生成派生列 [${name}]`)
    newColName.value = ''
  }
}

// ============ 数据快照表格 ============
const editingIdx = ref(-1)
const editingValue = ref('')
const unitIdx = ref(-1)
const unitTarget = ref('')
const unitFactor = ref(1)
const unitOffset = ref(0)
const unitLabel = ref('')

const sampleRows = computed(() => {
  const rows = d.value.data
  return [...rows.slice(0, 8), { __divider: true }, ...rows.slice(-2)]
})

function startRename(idx) {
  editingIdx.value = idx
  editingValue.value = d.value.columns[idx].label
}
function commitRename(idx) {
  const v = editingValue.value.trim()
  if (v) renameColumn(idx, v)
  editingIdx.value = -1
}

async function askDeleteColumn(idx) {
  const col = d.value.columns[idx]
  try {
    await ElMessageBox.confirm(`确认删除列 "${col.label}"？此操作不可撤销。`, '删除列', { type: 'warning' })
  } catch (e) { return }
  if (deleteColumn(idx)) toast('success', `列 [${col.label}] 已删除`)
}

function openUnitPanel(idx) {
  if (unitIdx.value === idx) { unitIdx.value = -1; return }
  unitIdx.value = idx
  const col = d.value.columns[idx]
  const list = UNIT_CONVERSIONS[col.unit] || []
  unitTarget.value = list.length ? list[0].target : 'custom'
  unitFactor.value = list.length ? list[0].factor : 1
  unitOffset.value = list.length ? list[0].offset : 0
  unitLabel.value = list.length ? list[0].target : ''
}
function onUnitTargetChange() {
  const col = d.value.columns[unitIdx.value]
  const c = (UNIT_CONVERSIONS[col.unit] || []).find(x => x.target === unitTarget.value)
  if (c) { unitFactor.value = c.factor; unitOffset.value = c.offset; unitLabel.value = c.target }
}
const unitPreview = computed(() => {
  if (unitIdx.value < 0) return ''
  const col = d.value.columns[unitIdx.value]
  const vals = d.value.data.filter(r => !isMissing(r[col.key])).slice(0, 3).map(r => Number(r[col.key]))
  if (vals.length === 0) return '无可转换样本'
  const out = vals.map(v => (v * Number(unitFactor.value) + Number(unitOffset.value)).toFixed(3))
  return `${vals.map(v => v.toFixed(2)).join(', ')} ${col.unit || ''} → ${out.join(', ')} ${unitLabel.value || '新单位'}  [y=${unitFactor.value}x${unitOffset.value >= 0 ? '+' : ''}${unitOffset.value}]`
})
function commitUnit() {
  const idx = unitIdx.value
  convertColumnUnit(idx, Number(unitFactor.value), Number(unitOffset.value), unitLabel.value || '新单位')
  unitIdx.value = -1
  toast('success', '单位换算已应用')
}

// ============ 数据集切分 ============
const split = computed(() => { void state.dataVersion; return splitCounts(state.splitRatio) })
// 只有进了审计链的比例才能被回放和 Python 复现，默认值不自动记，避免混入用户没做过的操作
const splitLogged = computed(() => { void state.actionLog.length; return state.actionLog.some(e => e.params?.type === 'split') })
function commitSplit() { setSplitRatio(state.splitRatio) }
</script>

<template>
  <section class="step-panel h-full p-4 flex flex-col gap-3 overflow-y-auto">
    <div class="grid grid-cols-12 gap-3">
      <!-- 时间列识别与转换 -->
      <div class="col-span-6 bg-white p-4 rounded-xl border border-slate-200 shadow-sm flex flex-col gap-3">
        <div class="flex items-center justify-between">
          <h2 class="text-xs font-bold text-slate-700 flex items-center">
            <i class="fa-regular fa-clock mr-1.5 text-indigo-600"></i>时间列识别与转换引擎
          </h2>
          <div class="flex items-center gap-2">
            <span class="text-[10px] text-slate-400">自动识别:</span>
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">
              <i class="fa-solid fa-check-circle text-[9px]"></i>
              <span>{{ detected ? detected.format : '未识别' }}</span>
            </span>
            <span class="text-[10px] text-slate-400 font-mono">{{ detected ? `样本命中率 ${detected.matchRate.toFixed(0)}%` : '点击识别格式' }}</span>
          </div>
        </div>

        <div class="grid grid-cols-4 gap-2">
          <div>
            <label class="text-[11px] text-slate-500 block mb-1">时间列</label>
            <select v-model="timeCol" class="w-full text-xs border border-slate-200 rounded px-2 py-1.5 bg-white focus:border-indigo-400 outline-none">
              <option v-for="c in d.columns" :key="c.key" :value="c.key">{{ c.label }}<template v-if="c.label !== c.key"> ({{ c.key }})</template></option>
            </select>
          </div>
          <div>
            <label class="text-[11px] text-slate-500 block mb-1">识别到的原始格式</label>
            <div class="text-xs font-mono py-1.5 px-2 bg-emerald-50/70 border border-emerald-200 text-emerald-800 rounded truncate">
              {{ detected ? detected.format : '— 等待识别' }}
            </div>
          </div>
          <div>
            <label class="text-[11px] text-slate-500 block mb-1">目标输出格式</label>
            <select v-model="targetFmt" class="w-full text-xs border border-slate-200 rounded px-2 py-1.5 bg-white focus:border-indigo-400 outline-none">
              <option value="YYYY-MM-DD HH:mm:ss">YYYY-MM-DD HH:mm:ss</option>
              <option value="YYYY/MM/DD HH:mm">YYYY/MM/DD HH:mm</option>
              <option value="YYYYMMDDHHmmss">YYYYMMDDHHmmss</option>
              <option value="YYYY-MM-DD">YYYY-MM-DD (仅日期)</option>
              <option value="epoch_ms">Unix 毫秒时间戳</option>
              <option value="custom">自定义格式…</option>
            </select>
          </div>
          <div class="flex items-end gap-1.5">
            <button @click="runDetect()" class="flex-1 py-1.5 border border-indigo-300 hover:bg-indigo-50 text-indigo-600 rounded text-xs font-semibold transition-colors flex items-center justify-center gap-1">
              <i class="fa-solid fa-magnifying-glass text-[10px]"></i>识别格式
            </button>
            <button @click="runConvert" class="flex-1 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded text-xs font-semibold shadow-sm transition-colors flex items-center justify-center gap-1">
              <i class="fa-solid fa-wand-magic-sparkles text-[10px]"></i>执行转换
            </button>
          </div>
        </div>

        <div v-if="targetFmt === 'custom'" class="grid grid-cols-2 gap-2">
          <div>
            <label class="text-[11px] text-slate-500 block mb-1">自定义格式字符串</label>
            <input v-model="customFmt" placeholder="如: DD/MM/YYYY HH:mm" class="w-full text-xs border border-slate-200 rounded px-2 py-1.5 font-mono bg-white focus:border-indigo-400 outline-none" />
          </div>
          <div></div>
        </div>

        <div v-if="convertPreview">
          <div class="grid grid-cols-2 gap-2">
            <div class="bg-slate-50 border border-slate-200 rounded-lg px-3 py-2">
              <div class="flex items-center justify-between mb-1">
                <span class="text-[10px] font-semibold text-slate-500">转换前 (原始)</span>
                <span class="text-[9px] font-mono px-1.5 py-0.5 rounded bg-slate-200 text-slate-500">{{ convertPreview.beforeFmt }}</span>
              </div>
              <div class="text-xs font-mono text-slate-600 space-y-0.5">
                <div v-for="(s, i) in convertPreview.before" :key="i">{{ s }}</div>
              </div>
            </div>
            <div class="bg-indigo-50/50 border border-indigo-200 rounded-lg px-3 py-2">
              <div class="flex items-center justify-between mb-1">
                <span class="text-[10px] font-semibold text-indigo-600">转换后 (目标)</span>
                <span class="text-[9px] font-mono px-1.5 py-0.5 rounded bg-indigo-200 text-indigo-700">{{ convertPreview.afterFmt }}</span>
              </div>
              <div class="text-xs font-mono text-indigo-800 space-y-0.5">
                <div v-for="(s, i) in convertPreview.after" :key="i">{{ s }}</div>
              </div>
            </div>
          </div>
        </div>

        <div v-if="convertStatus" class="flex items-center gap-2 text-[11px] px-3 py-1.5 rounded-lg border border-emerald-200 bg-emerald-50 text-emerald-700">
          <i class="fa-solid fa-circle-check"></i>
          <span>{{ convertStatus.text }}</span>
        </div>
      </div>

      <!-- 采样频率配置 -->
      <div class="col-span-6 bg-white p-4 rounded-xl border border-slate-200 shadow-sm flex flex-col justify-between">
        <div class="flex items-center justify-between mb-2">
          <h2 class="text-xs font-bold text-slate-700 flex items-center">
            <i class="fa-solid fa-wave-square mr-1.5 text-indigo-600"></i>采样频率配置
          </h2>
          <span class="text-[11px] text-slate-400">检测到 {{ srcFreq }} min 间隔</span>
        </div>
        <div class="grid grid-cols-3 gap-2">
          <div>
            <label class="text-[11px] text-slate-500 block mb-1">原始采样频率</label>
            <div class="text-xs font-mono py-1.5 px-2 bg-slate-50 border border-slate-200 rounded text-slate-700">{{ srcFreq }} min</div>
          </div>
          <div>
            <label class="text-[11px] text-slate-500 block mb-1">目标采样频率</label>
            <select v-model="targetRate" class="w-full text-xs border border-slate-200 rounded px-2 py-1.5 bg-slate-50 focus:bg-white">
              <option value="15min">15 min (96点/天)</option>
              <option value="30min">30 min (48点/天)</option>
              <option value="60min">60 min (24点/天)</option>
              <option value="5min">5 min (288点/天)</option>
              <option value="1min">1 min (1440点/天)</option>
            </select>
          </div>
          <div>
            <label class="text-[11px] text-slate-500 block mb-1">重采样方法</label>
            <select v-model="resampleMethod" class="w-full text-xs border border-slate-200 rounded px-2 py-1.5 bg-slate-50 focus:bg-white">
              <option value="mean">均值聚合 (Mean)</option>
              <option value="sum">求和聚合 (Sum)</option>
              <option value="first">首值采样 (First)</option>
              <option value="interpolate">线性插值 (Interpolate)</option>
            </select>
          </div>
        </div>
        <div class="mt-2 grid grid-cols-2 gap-2">
          <div class="flex items-center justify-between px-2.5 py-1.5 rounded-lg border bg-emerald-50/60 border-emerald-200">
            <div class="flex items-center gap-1.5">
              <i class="fa-solid fa-circle-exclamation text-emerald-600 text-[11px]"></i>
              <span class="text-[11px] text-emerald-800 font-medium">缺失率</span>
            </div>
            <span class="font-mono font-bold text-emerald-700 text-xs">{{ missingRate }}</span>
          </div>
          <div class="flex items-center justify-between px-2.5 py-1.5 rounded-lg border bg-amber-50/60 border-amber-200">
            <div class="flex items-center gap-1.5">
              <i class="fa-solid fa-copy text-amber-600 text-[11px]"></i>
              <span class="text-[11px] text-amber-800 font-medium">重复率</span>
            </div>
            <span class="font-mono font-bold text-amber-700 text-xs">{{ duplicateRate }}</span>
          </div>
        </div>
        <div class="mt-2 flex items-center justify-between text-[11px]">
          <span class="text-slate-500">调整后数据总量:</span>
          <span class="font-mono font-bold text-indigo-600">{{ d.data.length.toLocaleString() }} 条 → 约 {{ projectedCount.toLocaleString() }} 条</span>
        </div>
        <div class="mt-2 flex justify-end">
          <button @click="confirmResample" class="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded text-xs font-semibold shadow-sm flex items-center gap-1.5 transition-colors">
            <i class="fa-solid fa-check text-[11px]"></i>确认重采样
          </button>
        </div>
      </div>
    </div>

    <!-- 外生变量导入 -->
    <div class="bg-white rounded-xl border border-slate-200 shadow-sm p-4 flex flex-col gap-3 shrink-0">
      <div class="flex items-center justify-between">
        <h2 class="text-xs font-bold text-slate-700 flex items-center">
          <i class="fa-solid fa-link mr-1.5 text-indigo-600"></i>外生变量导入
          <span class="text-[10px] text-slate-400 font-normal ml-2">按时间戳对齐合并外部特征变量</span>
        </h2>
        <span v-if="state.exoVars.length > 0" class="text-[11px] bg-teal-100 text-teal-700 px-2 py-0.5 rounded-full font-mono font-semibold">已导入 {{ state.exoVars.length }} 个变量</span>
      </div>

      <div class="grid grid-cols-12 gap-3">
        <div class="col-span-7 flex flex-col gap-2.5">
          <div class="flex items-center gap-3">
            <label class="text-[11px] text-slate-500">导入方式:</label>
            <div class="flex items-center gap-1.5">
              <button @click="exoMode = 'preset'" class="px-2.5 py-1 text-[11px] rounded-md font-medium transition-colors"
                      :class="exoMode === 'preset' ? 'bg-teal-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'">
                <i class="fa-solid fa-cubes mr-1 text-[9px]"></i>预设变量模板
              </button>
              <button @click="exoMode = 'file'" class="px-2.5 py-1 text-[11px] rounded-md font-medium transition-colors"
                      :class="exoMode === 'file' ? 'bg-teal-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'">
                <i class="fa-solid fa-file-import mr-1 text-[9px]"></i>从文件导入
              </button>
              <button @click="exoMode = 'formula'" class="px-2.5 py-1 text-[11px] rounded-md font-medium transition-colors"
                      :class="exoMode === 'formula' ? 'bg-teal-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'">
                <i class="fa-solid fa-function mr-1 text-[9px]"></i>公式生成
              </button>
            </div>
          </div>

          <div v-if="exoMode === 'preset'" class="flex flex-col gap-2">
            <div class="grid grid-cols-2 gap-2">
              <div>
                <label class="text-[11px] text-slate-500 block mb-1">选择预设外生变量</label>
                <select v-model="presetKey" class="w-full text-xs border border-slate-200 rounded-lg px-2.5 py-2 bg-white focus:border-teal-400 outline-none">
                  <option value="">-- 请选择 --</option>
                  <option v-for="p in EXO_PRESETS" :key="p.value" :value="p.value">{{ p.label }}</option>
                </select>
              </div>
              <div>
                <label class="text-[11px] text-slate-500 block mb-1">自定义列名（可选）</label>
                <input v-model="presetAlias" placeholder="留空则使用默认名" class="w-full text-xs border border-slate-200 rounded-lg px-2.5 py-2 font-mono bg-white focus:border-teal-400 outline-none" />
              </div>
            </div>
            <p class="text-[10px] text-amber-600 bg-amber-50 border border-amber-200 rounded px-2 py-1">
              <i class="fa-solid fa-circle-info mr-1"></i>预设模板在本工作区内按主表时间轴生成模拟序列（真实气象/电价需接入后端数据源），操作记录中同样标注为模拟数据。
            </p>
            <button @click="doAddPreset" class="self-start px-4 py-1.5 bg-teal-600 hover:bg-teal-700 text-white rounded-lg text-xs font-semibold shadow-sm transition-colors flex items-center gap-1.5">
              <i class="fa-solid fa-plus text-[10px]"></i>添加变量
            </button>
          </div>

          <div v-else-if="exoMode === 'file'" class="flex flex-col gap-2">
            <div @click="exoFileInput.click()"
                 class="border-2 border-dashed border-slate-300 hover:border-teal-400 rounded-lg px-4 py-4 text-center transition-colors cursor-pointer">
              <input ref="exoFileInput" type="file" accept=".csv,.xlsx,.xls" class="hidden" @change="onExoFile" />
              <i class="fa-solid fa-cloud-arrow-up text-lg text-teal-500 mb-1"></i>
              <p class="text-[11px] text-slate-600 font-medium">{{ exoFileBusy ? '解析并对齐中…' : '点击上传外生变量文件' }}</p>
              <p class="text-[10px] text-slate-400">CSV / Excel · 需包含时间列以自动对齐</p>
            </div>
            <div class="grid grid-cols-2 gap-2">
              <div>
                <label class="text-[11px] text-slate-500 block mb-1">文件中的时间列名</label>
                <input v-model="exoTimeCol" class="w-full text-xs border border-slate-200 rounded-lg px-2.5 py-2 font-mono bg-white focus:border-teal-400 outline-none" />
              </div>
              <div>
                <label class="text-[11px] text-slate-500 block mb-1">对齐方式</label>
                <select v-model="exoMerge" class="w-full text-xs border border-slate-200 rounded-lg px-2.5 py-2 bg-white focus:border-teal-400 outline-none">
                  <option value="left">左连接 (保留主表全部)</option>
                  <option value="inner">内连接 (仅匹配行)</option>
                  <option value="nearest">最近邻匹配</option>
                </select>
              </div>
            </div>
          </div>

          <div v-else class="flex flex-col gap-2">
            <div class="grid grid-cols-2 gap-2">
              <div>
                <label class="text-[11px] text-slate-500 block mb-1">变量名称</label>
                <input v-model="formulaName" placeholder="如: solar_elevation" class="w-full text-xs border border-slate-200 rounded-lg px-2.5 py-2 font-mono bg-white focus:border-teal-400 outline-none" />
              </div>
              <div>
                <label class="text-[11px] text-slate-500 block mb-1">生成公式</label>
                <input v-model="formulaExpr" placeholder="如: sin(hour/24*2*PI)" class="w-full text-xs border border-slate-200 rounded-lg px-2.5 py-2 font-mono bg-white focus:border-teal-400 outline-none" />
              </div>
            </div>
            <div class="flex items-center gap-2 text-[10px] text-slate-400">
              <span>可用变量: <code class="bg-slate-100 px-1 rounded">hour</code> <code class="bg-slate-100 px-1 rounded">day</code> <code class="bg-slate-100 px-1 rounded">month</code> <code class="bg-slate-100 px-1 rounded">weekday</code> <code class="bg-slate-100 px-1 rounded">idx</code> (行号)</span>
              <span>|</span>
              <span>常量: <code class="bg-slate-100 px-1 rounded">PI</code> <code class="bg-slate-100 px-1 rounded">E</code></span>
            </div>
            <button @click="doAddFormula" class="self-start px-4 py-1.5 bg-teal-600 hover:bg-teal-700 text-white rounded-lg text-xs font-semibold shadow-sm transition-colors flex items-center gap-1.5">
              <i class="fa-solid fa-plus text-[10px]"></i>生成变量
            </button>
          </div>
        </div>

        <div class="col-span-5 flex flex-col gap-2">
          <div class="border border-slate-200 rounded-lg overflow-hidden flex-1">
            <div class="bg-slate-50 px-3 py-1.5 border-b border-slate-200 flex items-center justify-between">
              <span class="text-[11px] font-semibold text-slate-600">已导入的外生变量</span>
              <button v-if="state.exoVars.length > 0" @click="doClearExo" class="text-[10px] text-slate-400 hover:text-rose-600 transition-colors">
                <i class="fa-solid fa-trash-can mr-0.5"></i>清空
              </button>
            </div>
            <div class="max-h-[120px] overflow-auto divide-y divide-slate-100">
              <div v-if="state.exoVars.length === 0" class="text-center py-5 text-slate-300 text-[11px]">
                <i class="fa-regular fa-object-ungroup text-base block mb-1 opacity-40"></i>
                尚未导入外生变量
              </div>
              <div v-for="(v, idx) in state.exoVars" :key="v.key"
                   class="flex items-center gap-2.5 px-3 py-2 hover:bg-teal-50/40 transition-colors group">
                <div class="w-6 h-6 rounded-md bg-teal-100 flex items-center justify-center shrink-0">
                  <i class="fa-solid text-teal-600 text-[9px]"
                     :class="v.source === 'preset' ? 'fa-cubes' : v.source === 'file' ? 'fa-file-import' : 'fa-function'"></i>
                </div>
                <div class="flex-1 min-w-0">
                  <div class="flex items-center gap-1.5">
                    <span class="text-[11px] font-bold text-slate-700 font-mono truncate">{{ v.key }}</span>
                    <span class="px-1 py-0.5 rounded text-[8px] font-bold bg-teal-100 text-teal-700">{{ v.source === 'preset' ? '预设' : v.source === 'file' ? '文件' : '公式' }}</span>
                  </div>
                  <div class="text-[9px] text-slate-400 mt-0.5">{{ v.data.length }} 行 · 样本: {{ v.data.slice(0, 3).map(n => n !== null ? Number(n).toFixed(1) : 'N/A').join(', ') }}…</div>
                </div>
                <button @click="doRemoveExo(idx)" class="shrink-0 w-5 h-5 rounded flex items-center justify-center text-slate-300 hover:text-rose-600 hover:bg-rose-50 transition-colors opacity-0 group-hover:opacity-100" title="移除">
                  <i class="fa-solid fa-xmark text-[10px]"></i>
                </button>
              </div>
            </div>
          </div>
          <button @click="doMergeExo" :disabled="state.exoVars.length === 0"
                  class="w-full py-2 bg-teal-600 hover:bg-teal-700 text-white rounded-lg text-xs font-bold shadow-sm transition-colors flex items-center justify-center gap-1.5 disabled:opacity-40 disabled:cursor-not-allowed">
            <i class="fa-solid fa-code-merge text-[10px]"></i>合并到主数据集
          </button>
        </div>
      </div>
    </div>

    <!-- 列运算生成器 -->
    <div class="bg-white rounded-xl border border-slate-200 shadow-sm p-4 flex flex-col gap-3 shrink-0">
      <div class="flex items-center justify-between">
        <h2 class="text-xs font-bold text-slate-700 flex items-center">
          <i class="fa-solid fa-calculator mr-1.5 text-indigo-600"></i>列运算生成新特征列
          <span class="text-[10px] text-slate-400 font-normal ml-2">选择多列进行链式运算，从左到右顺序计算</span>
        </h2>
        <span v-if="state.derivedCols.length > 0" class="text-[11px] bg-indigo-100 text-indigo-700 px-2 py-0.5 rounded-full font-mono font-semibold">已生成 {{ state.derivedCols.length }} 列</span>
      </div>

      <div class="space-y-1.5">
        <div v-for="(t, i) in terms" :key="i" class="flex items-center gap-2">
          <select v-if="i > 0" v-model="t.op" class="w-14 text-xs border border-slate-200 rounded px-2 py-1.5 bg-white font-mono text-center">
            <option value="+">+</option><option value="-">−</option><option value="*">×</option><option value="/">÷</option>
          </select>
          <span v-else class="w-14 text-center text-[10px] text-slate-400 font-mono">起始</span>
          <select v-model="t.col" class="flex-1 text-xs border border-slate-200 rounded px-2 py-1.5 bg-white focus:border-indigo-400 outline-none">
            <option v-for="c in numericCols" :key="c.key" :value="c.key">{{ c.label }}<template v-if="c.label !== c.key"> · {{ c.key }}</template></option>
          </select>
          <button v-if="i > 1" @click="removeTerm(i)" class="w-6 h-6 rounded text-slate-300 hover:text-rose-500 hover:bg-rose-50 flex items-center justify-center">
            <i class="fa-solid fa-xmark text-[10px]"></i>
          </button>
          <span v-else class="w-6"></span>
        </div>
      </div>

      <div class="flex items-center gap-2 flex-wrap">
        <button @click="addTerm" class="px-3 py-1.5 border border-dashed border-indigo-300 hover:border-indigo-500 hover:bg-indigo-50 text-indigo-600 rounded-lg text-[11px] font-medium transition-colors flex items-center gap-1">
          <i class="fa-solid fa-plus text-[9px]"></i>添加操作列
        </button>
        <div class="flex-1"></div>
        <span class="text-slate-400 font-bold text-sm">→</span>
        <input v-model="newColName" placeholder="新列名" class="w-44 text-xs border border-slate-200 rounded-lg px-2.5 py-2 font-mono bg-white focus:border-indigo-400 outline-none" />
        <button @click="previewCalc" class="px-3 py-2 border border-slate-200 hover:border-indigo-400 hover:bg-indigo-50 text-slate-600 hover:text-indigo-600 rounded-lg text-xs font-medium transition-colors">
          <i class="fa-solid fa-eye mr-1"></i>预览
        </button>
        <button @click="doApplyCalc" class="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-xs font-semibold shadow-sm transition-colors">
          <i class="fa-solid fa-plus mr-1"></i>生成列
        </button>
      </div>

      <div v-if="calcPreview" class="flex items-center gap-3 text-xs font-mono px-3 py-2 bg-indigo-50/70 border border-indigo-200 rounded-lg">
        <span class="text-indigo-500 font-sans font-semibold text-[11px]">预览公式:</span>
        <span class="text-indigo-800">{{ calcPreview.formula }}</span>
        <span class="text-slate-400">|</span>
        <span class="text-indigo-500 font-sans font-semibold text-[11px]">前3行结果:</span>
        <span class="text-indigo-700">{{ calcPreview.samples.join(', ') }}</span>
      </div>

      <div v-if="state.derivedCols.length > 0" class="border border-slate-200 rounded-lg overflow-hidden">
        <div class="bg-slate-50 px-3 py-1.5 border-b border-slate-200 flex items-center justify-between">
          <span class="text-[11px] font-semibold text-slate-600">已生成的派生列</span>
          <button @click="clearAllDerivedCols" class="text-[10px] text-slate-400 hover:text-rose-600 transition-colors">
            <i class="fa-solid fa-trash-can mr-0.5"></i>清空全部
          </button>
        </div>
        <div class="max-h-[120px] overflow-auto divide-y divide-slate-100">
          <div v-for="(c, idx) in state.derivedCols" :key="c.key" class="flex items-center gap-2 px-3 py-1.5 text-[11px]">
            <i class="fa-solid fa-square-root-variable text-indigo-500 text-[10px]"></i>
            <span class="font-mono font-bold text-slate-700">{{ c.key }}</span>
            <span class="text-slate-400 truncate flex-1">= {{ c.formula }}</span>
            <button @click="deleteDerivedCol(idx)" class="text-slate-300 hover:text-rose-600"><i class="fa-solid fa-xmark text-[10px]"></i></button>
          </div>
        </div>
      </div>
    </div>

    <!-- 数据快照预览 -->
    <div class="bg-white rounded-xl border border-slate-200 shadow-sm flex flex-col overflow-hidden shrink-0">
      <div class="px-4 py-2.5 border-b border-slate-200 flex justify-between items-center bg-slate-50/60">
        <div class="flex items-center space-x-2">
          <span class="text-xs font-bold text-slate-700">数据快照预览 <span class="text-[10px] text-slate-400 font-normal">(双击列名可重命名)</span></span>
          <span class="text-[11px] bg-slate-200 text-slate-600 px-2 py-0.5 rounded-full font-mono">总计: {{ d.data.length.toLocaleString() }} 行 × {{ d.columns.length }} 列</span>
        </div>
        <div class="text-[11px] text-slate-400">显示前 8 行及后 2 行抽样</div>
      </div>
      <div class="overflow-auto">
        <table class="w-full text-left text-xs border-collapse">
          <thead class="sticky top-0 bg-slate-100 text-slate-600 font-semibold border-b border-slate-200 z-10">
            <tr>
              <th v-for="(c, idx) in d.columns" :key="c.key" class="group relative px-3 py-2 border-r border-slate-200 last:border-r-0 select-none">
                <div v-if="editingIdx === idx" class="flex items-center gap-1">
                  <input v-model="editingValue" @keyup.enter="commitRename(idx)" @keyup.esc="editingIdx = -1"
                         class="flex-1 text-xs font-semibold px-1 py-0.5 border border-indigo-400 rounded bg-white outline-none ring-2 ring-indigo-200 font-sans" v-focus />
                  <button @click="commitRename(idx)" title="确定" class="w-5 h-5 flex items-center justify-center bg-indigo-600 text-white rounded hover:bg-indigo-700 text-[10px]">
                    <i class="fa-solid fa-check"></i>
                  </button>
                  <button @click="editingIdx = -1" title="取消" class="w-5 h-5 flex items-center justify-center bg-slate-200 text-slate-600 rounded hover:bg-slate-300 text-[10px]">
                    <i class="fa-solid fa-xmark"></i>
                  </button>
                </div>
                <div v-else class="flex items-center justify-between gap-1">
                  <span class="cursor-text hover:text-indigo-600 truncate max-w-[120px]" @dblclick="startRename(idx)">{{ c.label }}</span>
                  <div class="flex items-center gap-1 shrink-0">
                    <span v-if="c.unit" class="text-[9px] font-mono px-1 rounded bg-indigo-100 text-indigo-600 font-semibold" title="当前单位">{{ c.unit }}</span>
                    <span class="text-[10px] font-mono font-normal uppercase px-1 rounded bg-slate-200 text-slate-500">{{ c.type }}</span>
                    <button v-if="!c.isTime" @click="openUnitPanel(idx)" title="单位转换"
                            class="w-4 h-4 flex items-center justify-center text-slate-300 hover:text-indigo-600 hover:bg-indigo-100 rounded opacity-0 group-hover:opacity-100 transition-opacity text-[10px]">
                      <i class="fa-solid fa-gear"></i>
                    </button>
                    <button v-if="!c.isTime" @click="askDeleteColumn(idx)" title="删除列"
                            class="w-4 h-4 flex items-center justify-center text-slate-300 hover:text-rose-600 hover:bg-rose-100 rounded opacity-0 group-hover:opacity-100 transition-opacity text-[10px]">
                      <i class="fa-solid fa-xmark"></i>
                    </button>
                  </div>
                </div>
                <div v-if="unitIdx === idx" class="mt-2 pt-2 border-t border-dashed border-slate-200 bg-indigo-50/50 rounded px-2 py-1.5">
                  <div class="space-y-1.5">
                    <div class="flex items-center gap-1.5 text-[11px]">
                      <span class="text-slate-500">当前:</span>
                      <span class="font-mono font-bold text-indigo-600">{{ c.unit || '—' }}</span>
                      <i class="fa-solid fa-arrow-right text-slate-400 text-[9px]"></i>
                      <select v-model="unitTarget" @change="onUnitTargetChange" class="flex-1 text-[11px] border border-slate-200 rounded px-1.5 py-0.5 bg-white focus:border-indigo-400 outline-none">
                        <option v-for="opt in (UNIT_CONVERSIONS[c.unit] || [])" :key="opt.target" :value="opt.target">{{ opt.target }}</option>
                        <option value="custom">自定义公式…</option>
                      </select>
                    </div>
                    <div v-if="unitTarget === 'custom'" class="flex items-center gap-1 text-[11px]">
                      <span class="text-slate-500">新单位:</span>
                      <input v-model="unitLabel" placeholder="如: kWh" class="w-16 text-[11px] border border-slate-200 rounded px-1.5 py-0.5 outline-none focus:border-indigo-400" />
                      <span class="text-slate-500">y =</span>
                      <input v-model="unitFactor" class="w-12 text-[11px] font-mono border border-slate-200 rounded px-1 py-0.5 text-right outline-none focus:border-indigo-400" />
                      <span class="text-slate-500">x +</span>
                      <input v-model="unitOffset" class="w-12 text-[11px] font-mono border border-slate-200 rounded px-1 py-0.5 text-right outline-none focus:border-indigo-400" />
                    </div>
                    <div class="text-[10px] font-mono text-slate-600 bg-white/80 border border-slate-200 rounded px-1.5 py-1">{{ unitPreview }}</div>
                    <div class="flex items-center gap-1">
                      <button @click="commitUnit" class="flex-1 text-[11px] font-semibold bg-indigo-600 text-white rounded py-1 hover:bg-indigo-700">
                        <i class="fa-solid fa-check mr-0.5"></i>应用转换
                      </button>
                      <button @click="unitIdx = -1" class="text-[11px] text-slate-500 border border-slate-200 rounded px-2 py-1 hover:bg-slate-100">取消</button>
                    </div>
                  </div>
                </div>
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100 font-mono text-slate-600">
            <template v-for="(row, ri) in sampleRows" :key="ri">
              <tr v-if="row.__divider" class="bg-slate-50">
                <td :colspan="d.columns.length" class="text-center py-1 text-slate-400 font-sans tracking-widest text-[11px]">··· 忽略中间数据行 ···</td>
              </tr>
              <tr v-else class="hover:bg-indigo-50/40">
                <td v-for="c in d.columns" :key="c.key"
                    class="px-3 py-1.5 border-r border-slate-100 last:border-r-0 truncate max-w-[180px]"
                    :class="isMissing(row[c.key]) ? 'bg-rose-50 text-rose-500 font-bold' : ''">
                  <template v-if="isMissing(row[c.key])"><i class="fa-solid fa-ban mr-1"></i>NaN</template>
                  <template v-else>{{ row[c.key] }}</template>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 时序数据集切分 -->
    <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm flex flex-col justify-between">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-700 flex items-center">
          <i class="fa-solid fa-timeline mr-1.5 text-indigo-600"></i>时序数据集切分 (时序不可打乱)
        </h2>
        <span class="text-[11px] font-mono text-slate-500">训练集 {{ state.splitRatio }}% | 验证集 {{ Math.round((100 - state.splitRatio) / 2) }}% | 测试集 {{ 100 - state.splitRatio - Math.round((100 - state.splitRatio) / 2) }}%</span>
      </div>
      <div class="w-full h-3.5 rounded-full overflow-hidden flex mb-2 bg-slate-100 p-0.5 border border-slate-200">
        <div :style="{ width: state.splitRatio + '%' }" class="h-full bg-indigo-500 rounded-l-full transition-all"></div>
        <div :style="{ width: (Math.round((100 - state.splitRatio) / 2)) + '%' }" class="h-full bg-amber-400 transition-all"></div>
        <div :style="{ width: (100 - state.splitRatio - Math.round((100 - state.splitRatio) / 2)) + '%' }" class="h-full bg-emerald-400 rounded-r-full transition-all"></div>
      </div>
      <div class="flex items-center justify-between gap-4 text-xs">
        <input type="range" min="50" max="85" v-model.number="state.splitRatio" @change="commitSplit" class="w-full h-1.5 bg-slate-200 rounded-lg cursor-pointer accent-indigo-600" />
        <div class="shrink-0 flex gap-2 text-[11px]">
          <span class="flex items-center"><span class="w-2 h-2 rounded-full bg-indigo-500 inline-block mr-1"></span>训练: {{ split.train.toLocaleString() }} 条</span>
          <span class="flex items-center"><span class="w-2 h-2 rounded-full bg-amber-400 inline-block mr-1"></span>验证: {{ split.val.toLocaleString() }} 条</span>
          <span class="flex items-center"><span class="w-2 h-2 rounded-full bg-emerald-400 inline-block mr-1"></span>测试: {{ split.test.toLocaleString() }} 条</span>
        </div>
      </div>
      <p class="text-[10px] text-slate-400 mt-2 flex items-center gap-1">
        <i :class="splitLogged ? 'fa-solid fa-circle-check text-emerald-500' : 'fa-solid fa-circle-info text-slate-300'"></i>
        {{ splitLogged
          ? `切分比例已写入操作记录（${state.splitRatio}%），会随流程回放与 Python 代码一起复现`
          : `当前为默认 ${state.splitRatio}%，尚未写入操作记录；拖动滑杆并松手即记录一次，才会进入回放与导出` }}
      </p>
    </div>

    <div class="flex justify-between gap-3 pt-1">
      <button @click="switchStep(1)" class="px-4 py-2 bg-slate-200 hover:bg-slate-300 text-slate-700 rounded-lg text-xs font-medium flex items-center">
        <i class="fa-solid fa-arrow-left mr-1.5"></i>返回数据加载
      </button>
      <button @click="switchStep(3)" class="px-5 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-xs font-semibold shadow flex items-center">
        下一步：数据探索分析 <i class="fa-solid fa-arrow-right ml-1.5"></i>
      </button>
    </div>
  </section>
</template>

<script>
export default {
  directives: {
    focus: { mounted: el => { const i = el.querySelector('input') || el; if (i.focus) { i.focus(); i.select && i.select() } } }
  }
}
</script>
