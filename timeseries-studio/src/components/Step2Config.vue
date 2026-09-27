<script setup>
import { ref, computed, reactive, watch, onActivated } from 'vue'
import { ElMessageBox } from 'element-plus'
import {
  state, ds, switchStep, toast, PAGE_SIZE, wsActionLog,
  detectTimeFormat, convertTimeColumn, detectedFreqMinutes, pageColumnValues,
  refreshPage, refreshOverview, rowCount,
  resampleDataset, resamplePreview,
  loadExoCatalog, exoColumns,
  inspectSideTable, attachSideTable, removeExoColumn,
  applyDerivedCol, clearAllDerivedCols, deleteDerivedCol, computeTermChain,
  renameColumn, deleteColumn, convertColumnUnit, splitCounts, setSplitRatio, applySplitColumn
} from '../store'
import {
  convertSingleTime, convertWithCustomFormat, isMissing,
  UNIT_CONVERSIONS, RESAMPLE_RATE_MAP
} from '../utils'
import { checkBackend, API_BASE } from '../api'

// datasets 是普通对象，ds() 恒返回同一引用；下游 computed 读的是它的属性，
// 引用不变就不会失效，重采样/填补后仍会拿到缓存。所以版本号一变就换新引用。
const d = computed(() => { void state.dataVersion; return { ...ds() } })
const numericCols = computed(() => d.value.columns.filter(c => c.type === 'float'))
const hasWs = computed(() => !!d.value.wsId)
const totalRows = computed(() => { void state.dataVersion; return rowCount() })
const wsVersion = computed(() => { void state.dataVersion; return d.value.meta?.version ?? null })
// 单元格数与内存必须和 rowCount 同源于 meta：每个响应都整体带回 meta，
// 若改用异步的 overview，撤销后会出现「新行数 + 旧格数」这种自相矛盾的中间帧。
const wsCells = computed(() => { void state.dataVersion; return d.value.meta?.cellCount ?? null })
const wsMemoryBytes = computed(() => { void state.dataVersion; return d.value.meta?.memoryBytes ?? null })
const overview = computed(() => { void state.dataVersion; return d.value.overview })
const backendReady = computed(() => state.backend.online)

// ============ 页窗口：明细表留在后端，这里只有当前这一页 ============
const page = computed(() => d.value.page || { offset: 0, limit: PAGE_SIZE, columns: [], rows: [], total: 0 })
const pageRows = computed(() => {
  const keys = page.value.columns
  return page.value.rows.map(r => {
    const o = {}
    for (let i = 0; i < keys.length; i++) o[keys[i]] = r[i]
    return o
  })
})
const pageFrom = computed(() => (page.value.total ? page.value.offset + 1 : 0))
const pageTo = computed(() => page.value.offset + page.value.rows.length)
const pageNo = computed(() => Math.floor(page.value.offset / page.value.limit) + 1)
const pageCount = computed(() => Math.max(1, Math.ceil(page.value.total / page.value.limit)))
const PAGE_SIZES = [20, 50, 100, 200, 500]
const paging = ref(false)
const pageMissing = computed(() => {
  let n = 0
  for (const r of pageRows.value) for (const c of d.value.columns) if (isMissing(r[c.key])) n++
  return n
})

async function goPage(offset, limit) {
  if (!hasWs.value || paging.value) return
  const size = limit ?? page.value.limit
  const maxOffset = Math.max(0, Math.floor((page.value.total - 1) / size) * size)
  const target = Math.min(Math.max(0, offset), maxOffset)
  if (target === page.value.offset && size === page.value.limit) return
  paging.value = true
  try { await refreshPage(target, size) } finally { paging.value = false }
}
function onPageSize(ev) { goPage(0, Number(ev.target.value)) }

// 整表概览（采样间隔/未解析时间）与当前页都要向后端取，进入本步即刷新一次
const numbersBusy = ref(false)
async function reloadServerNumbers() {
  if (!hasWs.value || !backendReady.value || numbersBusy.value) return
  numbersBusy.value = true
  try {
    await Promise.all([refreshOverview(), refreshPage(page.value.offset, page.value.limit)])
  } finally { numbersBusy.value = false }
}

// ============ 时间列识别与转换 ============
const timeCol = ref(d.value.timeCol)
// 换数据集要重置本地选择；同一数据集内的增删改（touch）不能重置，否则冲掉用户手选。
watch([() => state.currentKey, () => d.value.timeCol], ([, v]) => { timeCol.value = v })
const detected = ref(null)
const targetFmt = ref('YYYY-MM-DD HH:mm:ss')
const customFmt = ref('DD/MM/YYYY HH:mm')
const convertPreview = ref(null)
const convertStatus = ref(null)
const converting = ref(false)

function sampleTimes() {
  return pageColumnValues(timeCol.value).filter(v => !isMissing(v)).slice(0, 3).map(String)
}
function localConvert(raw) {
  return targetFmt.value === 'custom'
    ? convertWithCustomFormat(raw, customFmt.value)
    : convertSingleTime(raw, targetFmt.value)
}

function runDetect(silent = false) {
  const r = detectTimeFormat(timeCol.value)
  if (!r) {
    detected.value = null
    convertPreview.value = null
    if (!silent) toast('warning', `当前页窗口（第 ${pageFrom.value}–${pageTo.value} 行）没有可识别的时间样本，请翻页或换列`)
    return
  }
  detected.value = r
  const before = sampleTimes()
  convertPreview.value = {
    // 浏览器端只是"预估"，真实渲染由后端完成；执行转换后会用后端返回值覆盖并比对
    beforeFmt: r.format, afterFmt: targetFmt.value === 'custom' ? customFmt.value : targetFmt.value,
    before, after: before.map(localConvert), estimated: true, drifted: false
  }
  if (!silent) convertStatus.value = null
}

watch([timeCol, () => d.value.wsId, () => page.value.offset], () => runDetect(true), { immediate: true })
watch(targetFmt, () => { if (detected.value) runDetect(true) })

async function runConvert() {
  if (converting.value) return
  converting.value = true
  const est = convertPreview.value?.after?.slice() || []
  const before = convertPreview.value?.before?.slice() || sampleTimes()
  const prevFmt = detected.value?.format || d.value.timeFormat
  const changed = await convertTimeColumn(timeCol.value, targetFmt.value, customFmt.value)
  converting.value = false
  if (changed === null) return
  const after = sampleTimes()
  const drifted = est.length > 0 && after.length > 0 && after.some((v, i) => est[i] !== v)
  detected.value = detectTimeFormat(timeCol.value) || detected.value
  convertPreview.value = { beforeFmt: prevFmt, afterFmt: targetFmt.value === 'custom' ? customFmt.value : targetFmt.value, before, after, estimated: false, drifted }
  convertStatus.value = {
    ok: !drifted,
    text: `后端已转换 ${totalRows.value.toLocaleString()} 行 × 时间列，${changed.toLocaleString()} 个值发生变化；` +
      `浏览器只重取当前页 ${after.length.toLocaleString()} 个样本核对渲染结果`
  }
  toast('success', '时间格式转换完成（后端执行）')
}

// ============ 采样频率与重采样 ============
const targetRate = ref('60min')
const resampleMethod = ref('mean')
const srcFreq = computed(() => { void state.dataVersion; return detectedFreqMinutes() || overview.value?.freqMinutes || 15 })
const projection = ref(null)
const projBusy = ref(false)

// 行数投影必须由后端按真实时间跨度算：本地拿一页数据推不出来，所以这里只显示后端返回值
async function loadProjection() {
  if (!hasWs.value || !backendReady.value) { projection.value = null; return }
  projBusy.value = true
  try { projection.value = await resamplePreview(RESAMPLE_RATE_MAP[targetRate.value]) }
  finally { projBusy.value = false }
}
watch([() => d.value.wsId, targetRate, wsVersion], loadProjection, { immediate: true })
onActivated(() => { reloadServerNumbers(); loadProjection() })
watch(() => d.value.wsId, () => reloadServerNumbers())
watch(wsVersion, () => refreshOverview())

async function confirmResample() {
  const p = projection.value
  const msg = p
    ? `后端按真实时间跨度预演：${p.currentRows.toLocaleString()} 行 → ${p.projectedRows.toLocaleString()} 行` +
      `（${targetRate.value} 粒度，${p.filledBuckets.toLocaleString()} 个桶有数据、${p.emptyBuckets.toLocaleString()} 个桶为空` +
      `${p.compression ? `，压缩比 ${p.compression.toFixed(2)}×` : ''}）。\n当前工作区数据会被替换（可用撤销回退）。确认继续？`
    : `将在后端按 ${targetRate.value} 粒度聚合整表（当前 ${totalRows.value.toLocaleString()} 行）。确认继续？`
  try { await ElMessageBox.confirm(msg, '重采样确认', { type: 'warning' }) } catch (e) { return }
  const r = await resampleDataset(targetRate.value, resampleMethod.value)
  if (!r) return
  toast('success', `重采样完成（后端）：${r.oldCount.toLocaleString()} 行 → ${r.newCount.toLocaleString()} 行`)
}

// ============ 外生变量：只从文件导入，生成与对齐都在服务端 ============
// 侧表先看后挂：/api/exo/inspect 解析并落盘，确认列名/时间列/对齐方式后才提交一条命令。
// 这里没有「先在浏览器攒一列、再点合并」那一步，也没有 inner 连接这种实现里不成立的选项。
const exoBusy = ref(false)

// 对齐方式读 GET /api/exo/presets 的 alignModes：与后端 attach 校验用的是同一份枚举
const catalog = ref(null)
const catalogBusy = ref(false)
async function ensureCatalog(force) {
  if (catalog.value && !force) return catalog.value
  if (catalogBusy.value) return null
  catalogBusy.value = true
  try { catalog.value = await loadExoCatalog(force) } finally { catalogBusy.value = false }
  return catalog.value
}
// 后端可能是稍后才探到的（首屏 checkBackend 还在飞），在线就必须有清单
watch(() => state.backend.online, (ok) => { if (ok) ensureCatalog() }, { immediate: true })

const alignModes = computed(() => catalog.value?.alignModes
  || [{ mode: 'left', label: '精确时间戳' }, { mode: 'nearest', label: '就近匹配' }])

// ---- 侧表：inspect 回显 → 逐列确认 → attach ----
const exoFileInput = ref(null)
const side = ref(null)
const sideTimeCol = ref('')
const sideMode = ref('left')
const sideTolerance = ref(60)
const sideRows = ref([])
const sideBusy = ref(false)

async function onExoFile(ev) {
  const file = ev.target.files && ev.target.files[0]
  ev.target.value = ''
  if (!file) return
  sideBusy.value = true
  const r = await inspectSideTable(file)
  sideBusy.value = false
  if (!r) return
  side.value = r
  sideTimeCol.value = r.sideTimeCol || ''
  sideRows.value = (r.columns || []).filter(c => !c.time).map(c => ({
    from: c.from, label: c.label, numeric: c.numeric, needsName: !!c.needsName, reason: c.reason || '',
    key: c.key || '', on: !c.needsName && c.numeric
  }))
  if (!r.importable) toast('warning', `侧表 ${r.filename} 除时间列外没有可挂的数值列（${r.rows} 行已解析，未写入工作区）`)
}

// 时间列本身不该再作为变量列挂上去：换时间列时把新的那一列取消勾选
watch(sideTimeCol, (t) => {
  sideRows.value.forEach(row => { if (row.from === t) row.on = false })
})

const sidePicked = computed(() => sideRows.value.filter(r => r.on && r.key.trim()))
const sideUnnamed = computed(() => sideRows.value.filter(r => r.on && !r.key.trim()))

async function doAttachSide() {
  if (!side.value) { toast('warning', '请先上传侧表并确认表头'); return }
  if (!sideTimeCol.value) { toast('warning', '请指定侧表中的时间列'); return }
  if (!sidePicked.value.length) { toast('warning', '请至少勾选一列要挂到主表的变量'); return }
  if (sideUnnamed.value.length) { toast('warning', `列 ${sideUnnamed.value.map(r => r.from).join('、')} 还没有合法变量名`); return }
  if (exoBusy.value) return
  exoBusy.value = true
  const r = await attachSideTable({
    filename: side.value.filename, sha: side.value.sha, sideTimeCol: sideTimeCol.value,
    mode: sideMode.value, toleranceMinutes: sideMode.value === 'nearest' ? Number(sideTolerance.value) : null,
    targets: sidePicked.value.map(t => ({ from: t.from, key: t.key.trim(), label: t.label }))
  })
  exoBusy.value = false
  if (r) {
    toast('success', `已挂上 ${r.count} 列：${r.summary}` +
      (r.nonNumeric?.length ? `（非数值列 ${r.nonNumeric.length} 个已跳过）` : ''))
    side.value = null; sideRows.value = []
  }
}

function doRemoveExo(col) { return removeExoColumn(col) }
const attachedExo = computed(() => { void state.dataVersion; return exoColumns() })
// 界面只剩「从文件导入」一条入口，但服务端命令日志里可能还留着早前用预设/公式挂上的列，
// 那是要照实标出来的历史，不能因为入口没了就把它们显示成侧表
const EXO_KIND_META = {
  preset: { icon: 'fa-cubes', label: '预设' },
  formula: { icon: 'fa-function', label: '公式' },
  file: { icon: 'fa-file-import', label: '侧表' }
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
  const samples = pageRows.value.slice(0, 3).map(row => {
    const v = computeTermChain(terms.map(t => ({ ...t })), row)
    return v === null ? '缺失' : v.toFixed(2)
  })
  calcPreview.value = { formula: formulaText.value, samples }
}

async function doApplyCalc() {
  const name = newColName.value.trim().replace(/\s+/g, '_')
  if (!name) { toast('warning', '请填写新列名'); return }
  if (terms.length < 2) { toast('warning', '至少需要两个操作列'); return }
  if (await applyDerivedCol(name, terms.map(t => ({ ...t })))) {
    toast('success', `后端已生成派生列 [${name}]`)
    newColName.value = ''
    calcPreview.value = null
  }
}

async function doDeleteDerivedCol(idx) {
  const dc = state.derivedCols[idx]
  if (await deleteDerivedCol(idx)) toast('success', `已删除派生列 [${dc?.key || ''}]`)
}
async function doClearDerivedCols() {
  if (state.derivedCols.length === 0) return
  try { await ElMessageBox.confirm(`确认删除全部 ${state.derivedCols.length} 个派生列？`, '清空确认', { type: 'warning' }) } catch (e) { return }
  await clearAllDerivedCols()
}

// ============ 表头操作：重命名 / 删除 / 单位 ============
const editingIdx = ref(-1)
const editingValue = ref('')
const unitIdx = ref(-1)
const unitTarget = ref('')
const unitFactor = ref(1)
const unitOffset = ref(0)
const unitLabel = ref('')

function startRename(idx) {
  editingIdx.value = idx
  editingValue.value = d.value.columns[idx].label
}
async function commitRename(idx) {
  const v = editingValue.value.trim()
  editingIdx.value = -1
  if (!v) return
  const old = d.value.columns[idx]?.label
  if (await renameColumn(idx, v)) toast('success', `列名已改为 [${v}]（原 ${old}）`)
}

async function askDeleteColumn(idx) {
  const col = d.value.columns[idx]
  try {
    await ElMessageBox.confirm(
      `确认在后端工作区删除整列 "${col.label}"（${totalRows.value.toLocaleString()} 行）？可用顶部「撤销」回退。`,
      '删除列', { type: 'warning' })
  } catch (e) { return }
  if (await deleteColumn(idx)) toast('success', `列 [${col.label}] 已删除`)
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
// 试算只取当前页的非缺失值：整表换算在后端执行
const unitPreview = computed(() => {
  if (unitIdx.value < 0) return ''
  const col = d.value.columns[unitIdx.value]
  const vals = pageRows.value.filter(r => !isMissing(r[col.key])).slice(0, 3).map(r => Number(r[col.key]))
  if (vals.length === 0) return '当前页无可转换样本（翻页或换列后重试）'
  const out = vals.map(v => (v * Number(unitFactor.value) + Number(unitOffset.value)).toFixed(3))
  return `${vals.map(v => v.toFixed(2)).join(', ')} ${col.unit || ''} → ${out.join(', ')} ${unitLabel.value || '新单位'}  [y=${unitFactor.value}x${unitOffset.value >= 0 ? '+' : ''}${unitOffset.value}]`
})
async function commitUnit() {
  const idx = unitIdx.value
  const ok = await convertColumnUnit(idx, Number(unitFactor.value), Number(unitOffset.value), unitLabel.value || '新单位')
  unitIdx.value = -1
  if (ok) toast('success', '单位换算已在后端整表应用')
}

// ============ 数据集切分 ============
const split = computed(() => { void state.dataVersion; return splitCounts(state.splitRatio) })
// 只有进了审计链的比例才能被回放和 Python 复现，默认值不自动记，避免混入用户没做过的操作。
// 取 wsActionLog()：换开第二份数据集后，上一份的切分记录不该把这一份的切分标成「已执行」。
const splitLogged = computed(() => {
  void state.dataVersion
  return wsActionLog().some(e => e.params?.type === 'split')
})
function commitSplit() { setSplitRatio(state.splitRatio) }

// 划分列的真实样子取自审计链：那三个数是后端生成当场回带的，不是界面按行数估的
const splitApplied = computed(() => {
  void state.dataVersion
  const recs = wsActionLog().filter(e => e.params?.type === 'split_apply')
  return recs.length ? recs[recs.length - 1].params : null
})
// 生成之后又删过行 / 改过比例，列里的标签就和现在界面上算的对不上了：把差在哪写出来，而不是悄悄一致
const splitStale = computed(() => {
  const a = splitApplied.value
  if (!a) return ''
  const why = []
  if (a.rows !== totalRows.value) why.push(`行数已从 ${a.rows.toLocaleString()} 变成 ${totalRows.value.toLocaleString()}`)
  if (a.ratio !== state.splitRatio) why.push(`比例已从 ${a.ratio}% 改成 ${state.splitRatio}%`)
  return why.join('；')
})
async function doApplySplit() {
  const r = await applySplitColumn(state.splitRatio)
  // runOp 已把最新元数据与页窗口换回来，新列就在下方快照里，不必再要一次
  if (r) toast('success', `划分列 [${r.label}] 已写入 ${r.rowCount.toLocaleString()} 行：train ${r.train.toLocaleString()} / val ${r.val.toLocaleString()} / test ${r.test.toLocaleString()}`)
}

// ============ Tab 布局：五个环节改为切换显示（与第五步同款） ============
const activeTab = ref('time')
// 「已执行」一律取自审计链：撤销与回放都会改写操作记录，本地一次性置 true 的标志会长期说谎。
// 取 wsActionLog() 而非 state.actionLog——同一份会话里连着开两个数据集时，上一份的操作不该给这一个打勾。
const pipe = computed(() => {
  void state.dataVersion
  const t = new Set(wsActionLog().map(e => e.params?.type))
  return {
    time: t.has('time_convert'),
    rate: t.has('resample'),
    exo: t.has('exo-preset') || t.has('exo-formula') || t.has('exo-file'),
    calc: t.has('multi_calc'),
    split: t.has('split') || t.has('split_apply')
  }
})
const PIPE_CHIPS = [
  ['time', 'bg-indigo-400', '转换'],
  ['rate', 'bg-sky-400', '频率'],
  ['exo', 'bg-teal-400', '外生'],
  ['calc', 'bg-amber-400', '运算'],
  ['split', 'bg-emerald-400', '切分']
]
</script>

<template>
  <section class="step-panel h-full p-4 flex flex-col gap-3 overflow-y-auto">
    <!-- 工作区状态条：界面上的每个整表数字都出自这里 -->
    <div class="bg-white border border-slate-200 rounded-xl px-4 py-2.5 shadow-sm flex items-center gap-3 flex-wrap text-[11px] shrink-0">
      <span class="font-bold text-slate-700 flex items-center gap-1.5">
        <i class="fa-solid fa-server text-indigo-600"></i>后端工作区
      </span>
      <template v-if="hasWs">
        <span class="font-mono text-slate-600">{{ d.wsId }}</span>
        <span class="px-1.5 py-0.5 rounded bg-slate-100 font-mono text-slate-600">版本 v{{ wsVersion }}</span>
        <span class="text-slate-500">整表 <b class="font-mono text-slate-700">{{ totalRows.toLocaleString() }}</b> 行 × {{ d.columns.length }} 列</span>
        <span class="text-slate-500">浏览器缓存第 <b class="font-mono text-indigo-700">{{ pageFrom.toLocaleString() }}–{{ pageTo.toLocaleString() }}</b> 行</span>
        <span v-if="wsCells !== null" class="text-slate-400 font-mono"
              :title="`与行数同源于后端 meta：${wsCells} 格 / ${wsMemoryBytes ?? 0} 字节`">
          整表 {{ wsCells.toLocaleString() }} 格 · {{ (wsMemoryBytes / 1048576).toFixed(1) }} MiB
        </span>
      </template>
      <span v-else class="text-slate-400">尚未载入数据集，请回到第一步</span>
      <div class="ml-auto flex items-center gap-2">
        <span v-if="state.busy" class="text-indigo-600"><i class="fa-solid fa-spinner fa-spin mr-1"></i>{{ state.busy }}</span>
        <span v-if="overview && overview.unparsedTimes" class="px-2 py-0.5 rounded-full bg-rose-50 border border-rose-200 text-rose-600">
          <i class="fa-solid fa-triangle-exclamation mr-1"></i>{{ overview.unparsedTimes.toLocaleString() }} 个时间未解析
        </span>
        <span v-if="!backendReady" class="px-2 py-0.5 rounded-full bg-rose-50 border border-rose-200 text-rose-600 font-semibold">
          <i class="fa-solid fa-lock mr-1"></i>后端离线：本步只读
        </span>
        <button @click="reloadServerNumbers()" :disabled="!hasWs || numbersBusy"
                class="px-2 py-0.5 rounded border border-slate-200 hover:border-indigo-300 text-slate-500 hover:text-indigo-600 disabled:opacity-40">
          <i class="fa-solid fa-rotate mr-0.5"></i>重取后端统计
        </button>
      </div>
    </div>

    <!-- 五个配置环节改为 Tab 切换：一次只看一个环节，与第五步同款布局 -->
    <div class="shrink-0 bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden flex flex-col">
      <!-- Tab 栏 + 流水线状态 -->
      <div class="flex items-center flex-wrap gap-y-1.5 border-b border-slate-200 bg-slate-50/80">
        <div class="flex items-center">
          <button @click="activeTab = 'time'"
                  class="flex items-center gap-1.5 px-4 py-2.5 text-xs font-semibold border-b-2 transition-colors"
                  :class="activeTab === 'time' ? 'border-indigo-500 text-indigo-600 bg-white' : 'border-transparent text-slate-500 hover:text-slate-700'">
            <i class="fa-regular fa-clock"></i>时间格式转换
          </button>
          <button @click="activeTab = 'rate'"
                  class="flex items-center gap-1.5 px-4 py-2.5 text-xs font-semibold border-b-2 transition-colors"
                  :class="activeTab === 'rate' ? 'border-indigo-500 text-indigo-600 bg-white' : 'border-transparent text-slate-500 hover:text-slate-700'">
            <i class="fa-solid fa-wave-square"></i>采样频率配置
          </button>
          <button @click="activeTab = 'exo'"
                  class="flex items-center gap-1.5 px-4 py-2.5 text-xs font-semibold border-b-2 transition-colors"
                  :class="activeTab === 'exo' ? 'border-indigo-500 text-indigo-600 bg-white' : 'border-transparent text-slate-500 hover:text-slate-700'">
            <i class="fa-solid fa-link"></i>外生变量导入
          </button>
          <button @click="activeTab = 'calc'"
                  class="flex items-center gap-1.5 px-4 py-2.5 text-xs font-semibold border-b-2 transition-colors"
                  :class="activeTab === 'calc' ? 'border-indigo-500 text-indigo-600 bg-white' : 'border-transparent text-slate-500 hover:text-slate-700'">
            <i class="fa-solid fa-calculator"></i>列运算生成列
          </button>
          <button @click="activeTab = 'split'"
                  class="flex items-center gap-1.5 px-4 py-2.5 text-xs font-semibold border-b-2 transition-colors"
                  :class="activeTab === 'split' ? 'border-indigo-500 text-indigo-600 bg-white' : 'border-transparent text-slate-500 hover:text-slate-700'">
            <i class="fa-solid fa-timeline"></i>数据集切分
          </button>
        </div>

        <div class="flex-1"></div>

        <div class="flex items-center gap-2 px-3 h-full text-[10px]">
          <div v-for="c in PIPE_CHIPS" :key="c[0]"
               class="flex items-center gap-1 px-1.5 py-0.5 rounded bg-white border border-slate-200">
            <span class="w-1.5 h-1.5 rounded-full" :class="c[1]"></span>
            <span class="text-slate-500">{{ c[2] }}</span>
            <span class="font-bold" :class="pipe[c[0]] ? 'font-mono text-emerald-600' : 'text-slate-400'">{{ pipe[c[0]] ? '✓ 完成' : '待执行' }}</span>
          </div>
          <span class="text-slate-400 ml-1">工作区 <strong class="text-indigo-600">v{{ wsVersion ?? 0 }}</strong></span>
        </div>
      </div>

      <!-- Tab 1: 时间列识别与转换 -->
      <div v-show="activeTab === 'time'" class="p-4 flex flex-col gap-3">
        <div class="flex items-center justify-between">
          <h2 class="text-xs font-bold text-slate-700 flex items-center">
            <i class="fa-regular fa-clock mr-1.5 text-indigo-600"></i>时间列识别与转换引擎
          </h2>
          <div class="flex items-center gap-2">
            <span class="text-[10px] text-slate-400">当前格式:</span>
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
            <button @click="runConvert" :disabled="converting || !backendReady || !hasWs"
                    class="flex-1 py-1.5 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded text-xs font-semibold shadow-sm transition-colors flex items-center justify-center gap-1">
              <i class="fa-solid fa-wand-magic-sparkles text-[10px]"></i>{{ converting ? '后端转换中…' : '执行转换' }}
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
                <span class="text-[10px] font-semibold text-slate-500">转换前（当前页真实样本）</span>
                <span class="text-[9px] font-mono px-1.5 py-0.5 rounded bg-slate-200 text-slate-500">{{ convertPreview.beforeFmt }}</span>
              </div>
              <div class="text-xs font-mono text-slate-600 space-y-0.5 break-all">
                <div v-for="(s, i) in convertPreview.before" :key="i">{{ s }}</div>
              </div>
            </div>
            <div class="bg-indigo-50/50 border border-indigo-200 rounded-lg px-3 py-2">
              <div class="flex items-center justify-between mb-1">
                <span class="text-[10px] font-semibold text-indigo-600">
                  转换后（{{ convertPreview.estimated ? '浏览器预估' : '后端渲染' }}）
                </span>
                <span class="text-[9px] font-mono px-1.5 py-0.5 rounded bg-indigo-200 text-indigo-700">{{ convertPreview.afterFmt }}</span>
              </div>
              <div class="text-xs font-mono text-indigo-800 space-y-0.5 break-all">
                <div v-for="(s, i) in convertPreview.after" :key="i">{{ s }}</div>
              </div>
            </div>
          </div>
          <p v-if="convertPreview.estimated" class="text-[10px] text-slate-400 mt-1 flex items-center gap-1">
            <i class="fa-solid fa-circle-info"></i>
            「浏览器预估」只用于预览目标格式，执行转换后这里会换成后端真实渲染值并自动比对。
          </p>
          <p v-else-if="convertPreview.drifted" class="text-[10px] text-amber-600 mt-1 flex items-center gap-1">
            <i class="fa-solid fa-triangle-exclamation"></i>
            本地格式预估与后端渲染不一致，已以后端返回值为展示基准。
          </p>
        </div>

        <div v-if="convertStatus" class="flex items-center gap-2 text-[11px] px-3 py-1.5 rounded-lg border"
             :class="convertStatus.ok ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : 'border-amber-200 bg-amber-50 text-amber-700'">
          <i class="fa-solid" :class="convertStatus.ok ? 'fa-circle-check' : 'fa-triangle-exclamation'"></i>
          <span>{{ convertStatus.text }}</span>
        </div>
      </div>

      <!-- Tab 2: 采样频率配置 -->
      <div v-show="activeTab === 'rate'" class="p-4 flex flex-col">
        <div class="flex items-center justify-between mb-2">
          <h2 class="text-xs font-bold text-slate-700 flex items-center">
            <i class="fa-solid fa-wave-square mr-1.5 text-indigo-600"></i>采样频率配置
          </h2>
          <span class="text-[11px] text-slate-400">
            后端按整表时间戳算得 {{ srcFreq }} min 间隔
            <span v-if="overview?.freqLabel" class="font-mono">（{{ overview.freqLabel }}）</span>
          </span>
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
        <div class="mt-2 flex items-center justify-between text-[11px] gap-2">
          <span class="text-slate-500 shrink-0">后端预演:</span>
          <span v-if="projBusy" class="font-mono text-slate-400"><i class="fa-solid fa-spinner fa-spin mr-1"></i>正在按真实时间跨度预演…</span>
          <span v-else-if="projection" class="font-mono font-bold text-indigo-600 text-right">
            {{ projection.currentRows.toLocaleString() }} 条 → {{ projection.projectedRows.toLocaleString() }} 条
            <span class="text-[10px] font-normal text-slate-400">
              （{{ projection.filledBuckets.toLocaleString() }} 桶有值 / {{ projection.emptyBuckets.toLocaleString() }} 桶为空{{ projection.compression ? ` · 压缩 ${projection.compression.toFixed(2)}×` : '' }}）
            </span>
          </span>
          <span v-else class="font-mono text-slate-400">{{ backendReady ? '载入数据集后由后端预演' : `需后端在线（${API_BASE}）` }}</span>
        </div>
        <div class="mt-2 flex items-center justify-between gap-2">
          <span v-if="!backendReady" class="text-[10px] text-rose-500">
            <button @click="checkBackend()" class="underline hover:no-underline font-semibold">重新探测后端</button> 后才能执行加工
          </span>
          <button @click="confirmResample" :disabled="!hasWs || !backendReady || !!state.busy"
                  class="ml-auto px-4 py-1.5 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded text-xs font-semibold shadow-sm flex items-center gap-1.5 transition-colors shrink-0">
            <i class="fa-solid fa-check text-[11px]"></i>确认重采样
          </button>
        </div>
      </div>

      <!-- Tab 3: 外生变量导入 -->
      <div v-show="activeTab === 'exo'" class="p-4 flex flex-col gap-3">
        <div class="flex items-center justify-between">
          <h2 class="text-xs font-bold text-slate-700 flex items-center">
            <i class="fa-solid fa-link mr-1.5 text-indigo-600"></i>外生变量导入
          </h2>
          <span v-if="attachedExo.length > 0" class="text-[11px] bg-teal-100 text-teal-700 px-2 py-0.5 rounded-full font-mono font-semibold">已挂 {{ attachedExo.length }} 列</span>
        </div>

        <div class="grid grid-cols-12 gap-3">
          <div class="col-span-7 flex flex-col gap-2.5">
            <div class="flex items-center gap-3">
              <label class="text-[11px] text-slate-500">导入方式:</label>
              <span class="px-2.5 py-1 text-[11px] rounded-md font-medium bg-teal-600 text-white">
                <i class="fa-solid fa-file-import mr-1 text-[9px]"></i>从文件导入
              </span>
              <span v-if="!backendReady" class="text-[10px] text-rose-500">需后端在线</span>
            </div>

            <!-- 侧表：先看后挂 -->
            <div class="flex flex-col gap-2">
              <div @click="exoFileInput.click()"
                   class="border-2 border-dashed border-slate-300 hover:border-teal-400 rounded-lg px-4 py-3 text-center transition-colors cursor-pointer">
                <input ref="exoFileInput" type="file" accept=".csv,.xlsx,.xls" class="hidden" @change="onExoFile" />
                <i class="fa-solid text-lg mb-1" :class="sideBusy ? 'fa-spinner fa-spin text-slate-400' : 'fa-cloud-arrow-up text-teal-500'"></i>
                <p class="text-[11px] text-slate-600 font-medium">{{ sideBusy ? '后端解析中…' : (side ? `已解析：${side.filename}` : '点击上传外生变量侧表') }}</p>
                <p class="text-[10px] text-slate-400">CSV / Excel · 上传即落盘到服务端数据集目录，此处只确认表头，不写入工作区</p>
              </div>

              <div v-if="side" class="flex flex-col gap-2 border border-teal-200 bg-teal-50/40 rounded-lg p-2.5">
                <div class="flex items-center justify-between text-[10px] text-slate-500">
                  <span>侧表 <code class="bg-white px-1 rounded font-mono">{{ side.rows.toLocaleString() }} 行</code> · 内容指纹 <code class="bg-white px-1 rounded font-mono">{{ (side.sha || '').slice(0, 12) }}</code></span>
                  <button @click="side = null; sideRows = []" class="text-slate-400 hover:text-rose-600"><i class="fa-solid fa-xmark mr-0.5"></i>丢弃这份</button>
                </div>
                <div class="grid grid-cols-3 gap-2">
                  <div>
                    <label class="text-[11px] text-slate-500 block mb-1">侧表时间列</label>
                    <select v-model="sideTimeCol" class="w-full text-xs border border-slate-200 rounded-lg px-2 py-1.5 bg-white focus:border-teal-400 outline-none">
                      <option value="">-- 请选择 --</option>
                      <option v-for="c in side.columns" :key="c.from" :value="c.from">{{ c.label }}</option>
                    </select>
                  </div>
                  <div>
                    <label class="text-[11px] text-slate-500 block mb-1">对齐方式</label>
                    <select v-model="sideMode" class="w-full text-xs border border-slate-200 rounded-lg px-2 py-1.5 bg-white focus:border-teal-400 outline-none">
                      <option v-for="m in alignModes" :key="m.mode" :value="m.mode">{{ m.label }}</option>
                    </select>
                  </div>
                  <div>
                    <label class="text-[11px] text-slate-500 block mb-1">容差（分钟，仅就近）</label>
                    <input v-model.number="sideTolerance" type="number" min="1" max="1440" :disabled="sideMode !== 'nearest'"
                           class="w-full text-xs border border-slate-200 rounded-lg px-2.5 py-1.5 font-mono bg-white focus:border-teal-400 outline-none disabled:bg-slate-100 disabled:text-slate-400" />
                  </div>
                </div>
                <div class="border border-slate-200 rounded-lg bg-white overflow-hidden">
                  <div class="bg-slate-50 px-2.5 py-1 border-b border-slate-200 text-[10px] font-semibold text-slate-600 flex items-center justify-between">
                    <span>要挂到主表的列（{{ sidePicked.length }} 已选）</span>
                    <span class="font-normal text-slate-400">变量名需为 ASCII 合法标识符，可手动改</span>
                  </div>
                  <div class="max-h-[168px] overflow-auto divide-y divide-slate-100">
                    <div v-for="row in sideRows" :key="row.from" class="flex items-center gap-2 px-2.5 py-1.5">
                      <input type="checkbox" v-model="row.on" :disabled="!row.numeric || row.from === sideTimeCol" class="shrink-0" />
                      <span class="text-[11px] text-slate-700 font-mono truncate w-32 shrink-0" :title="row.from">{{ row.from }}</span>
                      <span v-if="row.needsName" class="text-[9px] text-rose-500 shrink-0" :title="row.reason">需命名</span>
                      <input v-model="row.key" :placeholder="row.key || '变量名'"
                             class="w-28 text-[11px] border border-slate-200 rounded px-1.5 py-0.5 font-mono outline-none focus:border-teal-400 ml-auto" />
                      <span class="text-[9px] shrink-0" :class="row.numeric ? 'text-slate-400' : 'text-amber-600'">{{ row.numeric ? '数值' : '非数值，跳过' }}</span>
                    </div>
                  </div>
                </div>
                <button @click="doAttachSide" :disabled="exoBusy || !backendReady || !hasWs || !!state.busy || !sidePicked.length"
                        class="self-start px-4 py-1.5 bg-teal-600 hover:bg-teal-700 disabled:opacity-50 text-white rounded-lg text-xs font-semibold shadow-sm transition-colors flex items-center gap-1.5">
                  <i class="fa-solid text-[10px]" :class="exoBusy ? 'fa-spinner fa-spin' : 'fa-code-merge'"></i>{{ exoBusy ? '后端对齐挂列中…' : `按时间戳对齐并挂 ${sidePicked.length} 列` }}
                </button>
                <p class="text-[10px] text-slate-500 leading-snug">
                  <i class="fa-solid fa-circle-info mr-1"></i>「精确时间戳」按秒对齐，未命中的主表行留空；「就近匹配」取容差内最近的一条，主表时间戳重复时以第一条为准。文件名与内容指纹会写进命令日志，回放时重新读那份文件。
                </p>
              </div>
            </div>
          </div>

          <div class="col-span-5 flex flex-col gap-2">
            <div class="border border-slate-200 rounded-lg overflow-hidden flex-1">
              <div class="bg-slate-50 px-3 py-1.5 border-b border-slate-200 flex items-center justify-between">
                <span class="text-[11px] font-semibold text-slate-600">已挂上的外生变量列（工作区真实列）</span>
                <span class="text-[10px] text-slate-400">{{ attachedExo.length }} 列</span>
              </div>
              <div class="max-h-[240px] overflow-auto divide-y divide-slate-100">
                <div v-if="attachedExo.length === 0" class="text-center py-5 text-slate-300 text-[11px]">
                  <i class="fa-regular fa-object-ungroup text-base block mb-1 opacity-40"></i>
                  尚未挂任何外生变量
                </div>
                <div v-for="col in attachedExo" :key="col.key"
                     class="flex items-center gap-2.5 px-3 py-2 hover:bg-teal-50/40 transition-colors group">
                  <div class="w-6 h-6 rounded-md bg-teal-100 flex items-center justify-center shrink-0">
                    <i class="fa-solid text-teal-600 text-[9px]" :class="(EXO_KIND_META[col.exo?.kind] || EXO_KIND_META.formula).icon"></i>
                  </div>
                  <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-1.5">
                      <span class="text-[11px] font-bold text-slate-700 font-mono truncate">{{ col.key }}</span>
                      <span class="px-1 py-0.5 rounded text-[8px] font-bold bg-teal-100 text-teal-700">{{ (EXO_KIND_META[col.exo?.kind] || EXO_KIND_META.formula).label }}</span>
                      <span v-if="col.label !== col.key" class="text-[9px] text-slate-400 truncate">{{ col.label }}</span>
                    </div>
                    <div class="text-[9px] text-slate-400 mt-0.5 font-mono">{{ col.exo?.expr || col.exo?.presetKey || col.exo?.filename || '' }}</div>
                  </div>
                  <button @click="doRemoveExo(col)" :disabled="!!state.busy" class="shrink-0 w-5 h-5 rounded flex items-center justify-center text-slate-300 hover:text-rose-600 hover:bg-rose-50 transition-colors opacity-0 group-hover:opacity-100" title="删除该列（走通用删列命令，可撤销）">
                    <i class="fa-solid fa-xmark text-[10px]"></i>
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Tab 4: 列运算生成器 -->
      <div v-show="activeTab === 'calc'" class="p-4 flex flex-col gap-3">
        <div class="flex items-center justify-between">
          <h2 class="text-xs font-bold text-slate-700 flex items-center">
            <i class="fa-solid fa-calculator mr-1.5 text-indigo-600"></i>列运算生成新特征列
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
            <i class="fa-solid fa-eye mr-1"></i>本页试算
          </button>
          <button @click="doApplyCalc" :disabled="!backendReady || !hasWs || !!state.busy"
                  class="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-lg text-xs font-semibold shadow-sm transition-colors">
            <i class="fa-solid fa-plus mr-1"></i>后端生成列
          </button>
        </div>

        <div v-if="calcPreview" class="flex items-center gap-3 text-xs font-mono px-3 py-2 bg-indigo-50/70 border border-indigo-200 rounded-lg">
          <span class="text-indigo-500 font-sans font-semibold text-[11px]">预览公式:</span>
          <span class="text-indigo-800">{{ calcPreview.formula }}</span>
          <span class="text-slate-400">|</span>
          <span class="text-indigo-500 font-sans font-semibold text-[11px]">当前页前 3 行试算:</span>
          <span class="text-indigo-700">{{ calcPreview.samples.join(', ') }}</span>
        </div>

        <div v-if="state.derivedCols.length > 0" class="border border-slate-200 rounded-lg overflow-hidden">
          <div class="bg-slate-50 px-3 py-1.5 border-b border-slate-200 flex items-center justify-between">
            <span class="text-[11px] font-semibold text-slate-600">已生成的派生列</span>
            <button @click="doClearDerivedCols" class="text-[10px] text-slate-400 hover:text-rose-600 transition-colors">
              <i class="fa-solid fa-trash-can mr-0.5"></i>清空全部
            </button>
          </div>
          <div class="max-h-[120px] overflow-auto divide-y divide-slate-100">
            <div v-for="(c, idx) in state.derivedCols" :key="c.key" class="flex items-center gap-2 px-3 py-1.5 text-[11px]">
              <i class="fa-solid fa-square-root-variable text-indigo-500 text-[10px]"></i>
              <span class="font-mono font-bold text-slate-700">{{ c.key }}</span>
              <span class="text-slate-400 truncate flex-1">= {{ c.formula }}</span>
              <button @click="doDeleteDerivedCol(idx)" class="text-slate-300 hover:text-rose-600"><i class="fa-solid fa-xmark text-[10px]"></i></button>
            </div>
          </div>
        </div>
      </div>

      <!-- Tab 5: 时序数据集切分 -->
      <div v-show="activeTab === 'split'" class="p-4 flex flex-col">
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
          · 三条行数按后端整表行数 {{ totalRows.toLocaleString() }} 计算
        </p>

        <!-- 落成真实的一列：比例只是参数，划分列才是随数据走产物 -->
        <div class="mt-3 border border-slate-200 rounded-lg p-3 bg-slate-50/50 flex items-start gap-3">
          <div class="flex-1 min-w-0">
            <h3 class="text-[11px] font-bold text-slate-700 flex items-center gap-1">
              <i class="fa-solid fa-certificate text-indigo-500"></i>生成数据集划分列
            </h3>
            <p class="text-[10px] text-slate-400 mt-1 leading-relaxed">
              新增 <span class="font-mono text-slate-500">dataset_split</span> 列，逐行写入
              <span class="font-mono text-indigo-600">train</span> /
              <span class="font-mono text-amber-600">val</span> /
              <span class="font-mono text-emerald-600">test</span>：按当前行序前 {{ state.splitRatio }}% 记 train，余下对半分给 val 与 test，时序不打乱。
              列会出现在下方快照与第五步特征登记表里，宽表导出与 Python 脚本复现的都是同一批标签。
            </p>
            <div v-if="splitApplied" class="mt-1.5 text-[10px] font-mono text-emerald-700">
              已生成：train {{ splitApplied.train.toLocaleString() }} / val {{ splitApplied.val.toLocaleString() }} / test {{ splitApplied.test.toLocaleString() }}
              · 整表 {{ splitApplied.rows.toLocaleString() }} 行 · 工作区第 {{ d.meta?.version ?? 0 }} 版
              <span v-if="splitStale" class="text-amber-600 font-sans">（{{ splitStale }}，重新点一次才会与当前数据一致）</span>
            </div>
          </div>
          <button @click="doApplySplit"
                  :disabled="!backendReady || !hasWs || !!state.busy || totalRows < 3"
                  class="shrink-0 px-3.5 py-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-slate-300 disabled:cursor-not-allowed text-white rounded-lg text-[11px] font-semibold shadow-sm transition-colors">
            <i class="fa-solid fa-play mr-1 text-[9px]"></i>{{ splitApplied ? '重新生成划分列' : '生成划分列' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 数据快照：后端分页窗口 -->
    <div class="bg-white rounded-xl border border-slate-200 shadow-sm flex flex-col overflow-hidden shrink-0">
      <div class="px-4 py-2.5 border-b border-slate-200 flex justify-between items-center bg-slate-50/60 gap-3 flex-wrap">
        <div class="flex items-center space-x-2">
          <span class="text-xs font-bold text-slate-700">数据快照预览 <span class="text-[10px] text-slate-400 font-normal">(双击列名可重命名)</span></span>
          <span class="text-[11px] bg-slate-200 text-slate-600 px-2 py-0.5 rounded-full font-mono">整表: {{ totalRows.toLocaleString() }} 行 × {{ d.columns.length }} 列</span>
          <span class="text-[11px] bg-indigo-50 text-indigo-700 border border-indigo-200 px-2 py-0.5 rounded-full font-mono">
            本页第 {{ pageFrom.toLocaleString() }}–{{ pageTo.toLocaleString() }} 行<span v-if="pageMissing"> · 缺失 {{ pageMissing.toLocaleString() }} 格</span>
          </span>
        </div>
        <div class="flex items-center gap-2 text-[11px] text-slate-500">
          <select :value="page.limit" @change="onPageSize" class="text-[11px] border border-slate-200 rounded px-1.5 py-0.5 bg-white">
            <option v-for="n in PAGE_SIZES" :key="n" :value="n">{{ n }} 行/页</option>
          </select>
          <button @click="goPage(0)" :disabled="pageNo <= 1 || paging || !hasWs"
                  class="px-2 py-0.5 border border-slate-200 rounded hover:border-indigo-300 hover:text-indigo-600 disabled:opacity-40">首页</button>
          <button @click="goPage(page.offset - page.limit)" :disabled="pageNo <= 1 || paging || !hasWs"
                  class="px-2 py-0.5 border border-slate-200 rounded hover:border-indigo-300 hover:text-indigo-600 disabled:opacity-40">上一页</button>
          <span class="font-mono">{{ pageNo }} / {{ pageCount }}</span>
          <button @click="goPage(page.offset + page.limit)" :disabled="pageTo >= page.total || paging || !hasWs"
                  class="px-2 py-0.5 border border-slate-200 rounded hover:border-indigo-300 hover:text-indigo-600 disabled:opacity-40">下一页</button>
          <button @click="goPage((pageCount - 1) * page.limit)" :disabled="pageTo >= page.total || paging || !hasWs"
                  class="px-2 py-0.5 border border-slate-200 rounded hover:border-indigo-300 hover:text-indigo-600 disabled:opacity-40">末页</button>
          <span v-if="paging" class="text-indigo-500"><i class="fa-solid fa-spinner fa-spin"></i></span>
        </div>
      </div>
      <div class="overflow-auto">
        <table class="w-full text-left text-xs border-collapse">
          <thead class="sticky top-0 bg-slate-100 text-slate-600 font-semibold border-b border-slate-200 z-10">
            <tr>
              <th class="px-2 py-2 border-r border-slate-200 text-right text-[10px] font-mono text-slate-400 w-14">#</th>
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
                      <button @click="commitUnit" :disabled="!backendReady || !!state.busy"
                              class="flex-1 text-[11px] font-semibold bg-indigo-600 text-white rounded py-1 hover:bg-indigo-700 disabled:opacity-50">
                        <i class="fa-solid fa-check mr-0.5"></i>后端整表应用
                      </button>
                      <button @click="unitIdx = -1" class="text-[11px] text-slate-500 border border-slate-200 rounded px-2 py-1 hover:bg-slate-100">取消</button>
                    </div>
                  </div>
                </div>
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100 font-mono text-slate-600">
            <tr v-if="pageRows.length === 0">
              <td :colspan="d.columns.length + 1" class="px-3 py-6 text-center text-slate-400 font-sans text-[11px]">
                <i class="fa-regular fa-folder-open mr-1.5"></i>
                {{ hasWs ? '后端未返回数据行（工作区可能已被回收，请回第一步重新载入）' : '尚未载入数据集' }}
              </td>
            </tr>
            <tr v-for="(row, ri) in pageRows" :key="page.offset + ri" class="hover:bg-indigo-50/40">
              <td class="px-2 py-1.5 text-right text-[10px] text-slate-400 border-r border-slate-100 font-mono">{{ (page.offset + ri + 1).toLocaleString() }}</td>
              <td v-for="c in d.columns" :key="c.key"
                  class="px-3 py-1.5 border-r border-slate-100 last:border-r-0 truncate max-w-[180px]"
                  :class="isMissing(row[c.key]) ? 'bg-rose-50 text-rose-500 font-bold' : ''">
                <template v-if="isMissing(row[c.key])"><i class="fa-solid fa-ban mr-1"></i>缺失</template>
                <template v-else>{{ row[c.key] }}</template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
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
