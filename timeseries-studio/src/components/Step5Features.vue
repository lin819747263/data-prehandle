<script setup>
import { computed, reactive, ref, nextTick, onActivated, onMounted, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import {
  state, ds, toast, switchStep, rowCount, refreshPage, wsActionLog,
  initFeatureList, buildTimeFeatures, timePlanLabel, buildLagFeatures, buildDiffFeatures,
  buildCatFeatures, catColumnDistribution, renameFeature, dropFeature, firstCompleteRow,
  ROLL_STATS, lagFeaturePlan, normEwmSpan, diffFeaturePlan, TIME_OPTS_ALL,
  loadHolidays, saveHolidays
} from '../store'

// 换新引用才能失效下游 computed（见 Step2Config 同款注释）
const d = computed(() => { void state.dataVersion; return { ...ds() } })
// 四类特征全部在后端的整帧上算（期③）：浏览器只持有元数据与当前页窗口，不再常驻整表数据
const limits = computed(() => state.backend.limits || {})
const running = ref(false)
// 一次只跑一个构建：连点两次会把同一批特征建第二遍（第二次只是「新增 0 列」，白跑一趟后端）
async function runBuild(fn, onOk) {
  if (running.value) return
  running.value = true
  try {
    const r = await fn()
    if (r) onOk(r)
  } finally {
    running.value = false
  }
}

const activeTab = ref('time')
// 「本步已完成」必须来自审计链：撤销和回放都会改写操作记录，本地一次性置 true 的标志会长期说谎。
// 取 wsActionLog()：一份会话里连着开两个数据集时，上一个的构建不该给这一个打勾。
// lag_roll / diff_freq 是拆分前那一条命令的特征族，老记录同样算数。
const pipe = computed(() => {
  void state.dataVersion
  const built = new Set(wsActionLog().filter(e => e.params?.type === 'feature_build').map(e => e.params.featureType))
  return {
    time: built.has('time'),
    lag: built.has('lag') || built.has('lag_roll'),
    window: built.has('window'),
    diff: built.has('diff') || built.has('diff_freq'),
    fft: built.has('fft'),
    cat: built.has('cat')
  }
})
const PIPE_CHIPS = [
  ['time', 'bg-indigo-400', '时间'],
  ['lag', 'bg-sky-400', '滞后'],
  ['window', 'bg-cyan-400', '窗口'],
  ['diff', 'bg-emerald-400', '差分'],
  ['fft', 'bg-teal-400', '频域'],
  ['cat', 'bg-violet-400', '编码']
]

// 每张卡片自己那一族在表里已有几列：数的是后端列注册表上的 feature 标记，
// 不是「上一次点按钮成功没有」——撤销、回放、换数据集都会让这个数变，卡片得跟着变。
// lag_roll / diff_freq 是拆分前那一条命令留下的族名，老列仍算进对应的卡片里。
const familyCount = computed(() => {
  void state.dataVersion
  const m = {}
  d.value.columns.forEach(c => { if (c.feature) m[c.feature] = (m[c.feature] || 0) + 1 })
  return {
    lag: (m.lag || 0) + (m.lag_roll || 0),
    window: m.window || 0,
    diff: (m.diff || 0) + (m.diff_freq || 0),
    fft: m.fft || 0
  }
})

// ---- Tab 1: 时间与日历特征 ----
// 勾选集一律用 { key: bool } 映射：模板的 v-model="x[k]" 写的是对象属性，
// 若状态是 Set，属性写入不会进 Set，勾选框既显示不出初始值也不生效。
const TIME_DIM_CN = { hour: '小时 (Hour)', day: '日期 (Day)', month: '月份 (Month)', weekday: '星期 (DayOfWeek, 周一=0)', is_weekend: '周末判定 (0/1)', holiday: '节假日编码' }
const TIME_OPTS_DIMS = TIME_OPTS_ALL.filter(o => !o.startsWith('sincos_') && o !== 'keep_cyc_original')
const TIME_OPTS_SINCOS = TIME_OPTS_ALL.filter(o => o.startsWith('sincos_'))
const KEEP_ORIGINAL_OPT = 'keep_cyc_original'
const SINCOS_CN = { sincos_hour: '小时 (Hour, 24)', sincos_weekday: '星期 (DayOfWeek, 7)', sincos_month: '月份 (Month, 12)' }
// [勾选项, 对应日历维度]：正余弦要依附在维度上，维度没勾就灰掉
const SINCOS_PAIRS = TIME_OPTS_SINCOS.map(o => [o, o.slice('sincos_'.length)])
const timeOpts = reactive(Object.fromEntries(TIME_OPTS_ALL.map(k => [k, true])))

function pickedKeys(map) { return Object.keys(map).filter(k => map[k]) }

// 展示用的「本次会生成哪些列」，与 store 里的构建逻辑同一函数，数字可追溯
const timePlan = computed(() => timePlanLabel(TIME_OPTS_ALL.filter(o => timeOpts[o])))
const timeSinCosEnabled = computed(() => timePlan.value.cycDims.length > 0)

function runTimeFeatures() {
  const chosen = TIME_OPTS_ALL.filter(o => timeOpts[o])
  if (chosen.filter(o => TIME_OPTS_DIMS.includes(o)).length === 0) { toast('warning', '请至少勾选一项时间特征'); return }
  const plan = timePlanLabel(chosen)
  if (plan.total === 0) { toast('warning', '当前勾选组合生成不出任何列'); return }
  runBuild(() => buildTimeFeatures(chosen), r => {
    toast('success', `已写入 ${r.cols} 个时间特征列（其中正余弦 ${r.sinCos} 列，本次新增 ${r.created} 列）` +
      (r.dropped ? `；${r.dropped} 个维度只留 sin/cos、未保留数值原列` : '') + r.removedNote)
  })
}

// ---- 节假日表（可配置 + 可显示）----
// 生效的那份存在后端工作区 meta 上，这里只留一份草稿：编辑不碰数据，点「保存」才提交一条可撤销的命令
const hol = computed(() => {
  void state.dataVersion
  const h = state.holidays
  return h.wsId === d.value.wsId ? h : { wsId: '', version: -1, data: null, loading: false, error: '' }
})
const holidayDraft = ref([])        // ['2024-01-01', ...]，升序去重
const holidayInput = ref('')
const holidaySynced = ref(null)     // 上次从服务端镜像过来的那份，用来判断草稿是不是用户改过的
const holOpen = ref(false)

function sameList(a, b) {
  return !!a && !!b && a.length === b.length && a.every((v, i) => v === b[i])
}
// 服务端那份换了（撤销/重做/换数据集）就跟过来；用户正在编辑的草稿不覆盖
function syncHolidayDraft() {
  const data = hol.value.data
  if (!data) return
  if (holidaySynced.value === null || sameList(holidayDraft.value, holidaySynced.value)) {
    holidayDraft.value = [...data.days]
  }
  holidaySynced.value = [...data.days]
}
watch(() => `${hol.value.wsId}|${hol.value.version}`, syncHolidayDraft)

const holidayDirty = computed(() => !sameList(holidayDraft.value, hol.value.data?.days || []))
const holidayByYear = computed(() => {
  const g = {}
  holidayDraft.value.forEach(day => { (g[day.slice(0, 4)] ||= []).push(day) })
  return Object.entries(g).map(([year, days]) => ({ year, days }))
})

function addHolidayDay() {
  const v = holidayInput.value.trim()
  if (!/^\d{4}-\d{2}-\d{2}$/.test(v)) { toast('warning', `日期格式需为 YYYY-MM-DD，收到「${v || '空'}」`); return }
  if (holidayDraft.value.includes(v)) { toast('info', `${v} 已在表里`); return }
  const max = hol.value.data?.maxDays || 200
  if (holidayDraft.value.length >= max) { toast('warning', `一次最多配置 ${max} 天（后端同一份上限）`); return }
  holidayDraft.value = [...holidayDraft.value, v].sort()
  holidayInput.value = ''
}
function removeHolidayDay(day) {
  holidayDraft.value = holidayDraft.value.filter(x => x !== day)
}
function applyHolidayPreset(year) {
  const p = (hol.value.data?.presets || []).find(x => x.year === year)
  if (!p) { toast('warning', `后端没有 ${year} 年的节假日预设`); return }
  holidayDraft.value = [...p.days]
}
async function commitHolidays(source) {
  const days = [...new Set(holidayDraft.value)].sort()
  const r = await runBuild(() => saveHolidays(days, source),
    x => toast('success', `节假日表已更新为 ${x.count} 天（${x.source}）` + (x.days.length ? ` · ${x.days[0]} ~ ${x.days[x.days.length - 1]}` : ' · 不认任何节假日')))
  if (r) syncHolidayDraft()
}

// ---- 共享数值列勾选（滞后 / 差分两个 Tab 共用同一选择集，修正原型跨容器重复计数 bug）----
const featCols = reactive({})
// 勾选框只列原始数值列：特征列已是工程产物，混进「操作字段」会让上一次构建的产物自动变成下一次的输入
function numericCols() { return d.value.columns.filter(c => c.type === 'float' && !c.feature) }
function ensureFeatCols() {
  const cols = numericCols()
  const valid = new Set(cols.map(c => c.key))
  Object.keys(featCols).forEach(k => { if (!valid.has(k)) delete featCols[k] })
  if (pickedKeys(featCols).length === 0) cols.slice(0, 2).forEach(c => { featCols[c.key] = true })
}
function toggleAllFeatCols(on) {
  numericCols().forEach(c => { featCols[c.key] = on })
}
const featPicked = computed(() => pickedKeys(featCols))
function targetColsOrDefault() {
  const sel = pickedKeys(featCols)
  if (sel.length > 0) return sel
  const main = numericCols()[0]
  return main ? [main.key] : []
}

// ---- Tab 2: 滞后与滑动窗口 ----
const lagSteps = ref('1, 2, 4, 96')
const rollWindows = ref('4, 16, 96')
const rollExpanding = ref(false)
const rollEwm = ref(false)
const ewmSpan = ref(12)
const rollStats = reactive(Object.fromEntries(Object.keys(ROLL_STATS).map(k => [k, k === 'mean' || k === 'std'])))
const ROLL_STAT_CN = { mean: '均值 (Mean)', std: '标准差 (Std)', max: '最大值 (Max)', min: '最小值 (Min)', median: '中位数 (Median)' }
const ROLL_STAT_OPTS = Object.keys(ROLL_STATS).map(k => [k, ROLL_STAT_CN[k]])
const lagPlanOpen = ref(false)
const windowPlanOpen = ref(false)

function parseIntList(s) {
  return s.split(',').map(x => parseInt(x.trim())).filter(v => !isNaN(v) && v > 0)
}

function lagParams() {
  return {
    lags: parseIntList(lagSteps.value),
    windows: parseIntList(rollWindows.value),
    stats: Object.keys(ROLL_STATS).filter(k => rollStats[k]),
    expanding: rollExpanding.value,
    ewm: rollEwm.value,
    ewmSpan: normEwmSpan(ewmSpan.value)
  }
}

// 预览与 store 构建共用同一计划函数：面板上写的列数必然等于产物列数
// 两组各算各的：滞后按钮只看阶数输入框，滑动窗口按钮只看窗口/统计量/高级项
const lagOnlyPlan = computed(() => lagFeaturePlan(targetColsOrDefault(), lagParams(), 'lag'))
const windowPlan = computed(() => lagFeaturePlan(targetColsOrDefault(), lagParams(), 'window'))

function runLagGroup(group) {
  const targetCols = targetColsOrDefault()
  if (targetCols.length === 0) { toast('warning', '请先在左侧勾选至少一个操作字段'); return }
  const params = lagParams()
  if (group === 'lag') {
    if (params.lags.length === 0) { toast('warning', '请先填写滞后阶数（逗号分隔，如 1, 2, 4, 96）'); return }
  } else {
    if (params.windows.length > 0 && params.stats.length === 0) {
      toast('warning', '已填写滚动窗口但未勾选任何滚动统计量')
      return
    }
    if (params.windows.length === 0 && !params.expanding && !params.ewm) {
      toast('warning', '请至少填写一个滚动窗口，或勾选 Expanding / EWM')
      return
    }
  }
  const head = group === 'lag' ? '滞后' : '滑动窗口'
  runBuild(() => buildLagFeatures(targetCols, params, group), r => {
    toast('success', `已写入 ${r.cols} 个${head}特征列（本次新增 ${r.created} 列）· 目标列: ${targetCols.join(', ')}${r.removedNote}`)
  })
}

// ---- Tab 3: 差分平稳化与频域 ----
const diff1st = ref(true)
const diff2nd = ref(false)
const diffSeasonal = ref(true)
const diffPeriod = ref(96)
const fftDominant = ref(true)
const fftEntropy = ref(false)
const fftPowerRatio = ref(false)

function diffParams() {
  return {
    d1: diff1st.value, d2: diff2nd.value,
    seasonal: diffSeasonal.value, period: Math.max(1, parseInt(diffPeriod.value) || 96),
    fftDominant: fftDominant.value, fftEntropy: fftEntropy.value, fftPowerRatio: fftPowerRatio.value
  }
}
const diffOnlyPlan = computed(() => diffFeaturePlan(targetColsOrDefault(), diffParams(), 'diff'))
const fftOnlyPlan = computed(() => diffFeaturePlan(targetColsOrDefault(), diffParams(), 'fft'))
const diffPlanOpen = ref(false)
const fftPlanOpen = ref(false)

function runDiffGroup(group) {
  const targetCols = targetColsOrDefault()
  if (targetCols.length === 0) { toast('warning', '请先在左侧勾选至少一个操作字段'); return }
  const p = diffParams()
  if (group === 'diff') {
    if (!p.d1 && !p.d2 && !p.seasonal) { toast('warning', '请至少勾选一项差分（一阶 / 二阶 / 季节性）'); return }
  } else {
    if (!p.fftDominant && !p.fftEntropy && !p.fftPowerRatio) { toast('warning', '请至少勾选一项频域特征'); return }
  }
  const head = group === 'diff' ? '差分平稳化' : '频域'
  runBuild(() => buildDiffFeatures(targetCols, p, group), r => {
    toast('success', `已写入 ${r.cols} 个${head}特征列（本次新增 ${r.created} 列）· 目标列: ${targetCols.join(', ')}${r.removedNote}`)
  })
}

// ---- Tab 4: 类别特征编码 ----
const catCols = reactive({})
const catMethod = ref('onehot')
const CAT_METHOD_NAMES = { onehot: '独热编码', ordinal: '序数编码', target: '目标均值编码' }
const catPicked = computed(() => pickedKeys(catCols))

// 排除特征列：第二步生成的 dataset_split 也是 category 类型，但它已是工程产物，
// 再拿去独热就是把「train/val/test」当特征编一遍（训练标签泄漏进模型）。
function catColsList() { return d.value.columns.filter(c => c.type === 'category' && !c.feature) }
function ensureCatCols() {
  const cols = catColsList()
  const valid = new Set(cols.map(c => c.key))
  Object.keys(catCols).forEach(k => { if (!valid.has(k)) delete catCols[k] })
  if (pickedKeys(catCols).length === 0 && cols.length > 0) catCols[cols[0].key] = true
}

// 面板上的取值分布来自后端 /value-counts（整表计数）：勾选变化 / 换编码方式 / 版本号变化都要重取
const catDist = ref({ rows: [], totalNewCols: 0, loading: false, error: '' })
let catDistSeq = 0

async function loadCatDist() {
  // 换数据集时这个 watch 先于 enterStep 里的 ensureCatCols 触发，勾选集还挂着上一份数据的列名，
  // 直接送去 /value-counts 就是一句「列不存在」的 400。先按当前列注册表把失效项剔掉。
  const valid = new Set(catColsList().map(c => c.key))
  Object.keys(catCols).forEach(k => { if (!valid.has(k)) delete catCols[k] })
  const keys = catPicked.value
  const method = catMethod.value
  if (keys.length === 0) { catDist.value = { rows: [], totalNewCols: 0, loading: false, error: '' }; return }
  const seq = ++catDistSeq
  catDist.value = { ...catDist.value, loading: true }
  const r = await catColumnDistribution(keys, method)
  if (seq !== catDistSeq) return   // 只认最后一次请求的结果
  catDist.value = r
    ? { ...r, loading: false, error: '' }
    : { rows: [], totalNewCols: 0, loading: false, error: '后端未返回分布数据（原因见提示）' }
}
watch(() => `${catPicked.value.join(',')}|${catMethod.value}|${d.value.meta?.version ?? -1}`, () => { loadCatDist() })

function distBar(row) {
  return row.uniqueVals.slice(0, 4).map((v, i) => {
    const colors = ['bg-violet-400', 'bg-sky-400', 'bg-amber-400', 'bg-emerald-400']
    const pct = Math.max(2, row.counts[v] / row.total * 100)
    const last = i === Math.min(3, row.uniqueVals.length - 1)
    return { cls: `${colors[i % colors.length]} ${i === 0 ? 'rounded-l-full' : ''} ${last ? 'rounded-r-full' : ''}`, style: `width:${pct}%` }
  })
}

function runCatFeatures() {
  const keys = catPicked.value
  if (keys.length === 0) { toast('warning', '请先在左侧勾选至少一个类别列'); return }
  runBuild(() => buildCatFeatures(keys, catMethod.value), r => {
    toast('success', `已执行${CAT_METHOD_NAMES[catMethod.value]}，写入 ${r.cols} 列（本次新增 ${r.created} 列）· 列: ${keys.join(', ')}`)
  })
}

// ---- 特征矩阵预览：读后端分页窗口，翻页即向 /rows 要下一段，浏览器不留整表 ----
const newFeatCount = computed(() => { void state.dataVersion; return state.features.filter(f => f.isNew).length })
const PREVIEW_SIZE = 10
const previewStart = ref(0)
function isNull(v) { return v === null || v === undefined || (typeof v === 'number' && Number.isNaN(v)) }
const page = computed(() => d.value.page || { offset: 0, limit: 50, columns: [], rows: [], total: 0 })
const pageRows = computed(() => {
  const keys = page.value.columns
  return page.value.rows.map(r => {
    const o = {}
    for (let i = 0; i < keys.length; i++) o[keys[i]] = r[i]
    return o
  })
})
const totalRows = computed(() => page.value.total)
const previewRows = computed(() => {
  void state.dataVersion
  return pageRows.value.slice(previewStart.value, previewStart.value + PREVIEW_SIZE)
})
const previewFrom = computed(() => (previewRows.value.length ? page.value.offset + previewStart.value + 1 : 0))
const previewTo = computed(() => page.value.offset + previewStart.value + previewRows.value.length)
const previewAtHead = computed(() => page.value.offset === 0 && previewStart.value === 0)
async function shiftPreview(delta) {
  const step = delta * PREVIEW_SIZE
  const next = previewStart.value + step
  const maxInPage = Math.max(0, pageRows.value.length - Math.min(PREVIEW_SIZE, pageRows.value.length))
  if (next >= 0 && next <= maxInPage) { previewStart.value = next; return }
  // 页内放不下就换页：offset 必须落在后端那一页里，所以按整页取回再从头显示
  const offset = Math.max(0, Math.min(Math.max(0, totalRows.value - PREVIEW_SIZE), page.value.offset + step))
  if (offset === page.value.offset) { previewStart.value = 0; return }
  previewStart.value = 0
  await refreshPage(offset)
}
// 长窗口特征在前 N 行必然为空（窗口未覆盖），定位到首个新特征全部有值的行
async function jumpToComplete() {
  const newKeys = state.features.filter(f => f.isNew).map(f => f.key)
  if (newKeys.length === 0) { toast('info', '当前没有新增特征列'); return }
  const r = await firstCompleteRow(newKeys)
  if (!r) return
  if (r.index === null) {
    toast('warning', `前 ${r.scanned.toLocaleString()} 行内没有所有新增特征都有值的行`)
    return
  }
  previewStart.value = 0
  await refreshPage(r.index)
  toast('success', `第 ${r.index + 1} 行起全部新增特征均有值`)
}

const renameIdx = ref(-1)
const renameVal = ref('')
const renameInput = ref(null)

function startRename(idx) {
  renameIdx.value = idx
  renameVal.value = state.features[idx].label
  nextTick(() => { renameInput.value?.[0]?.focus(); renameInput.value?.[0]?.select?.() })
}
function commitRename() {
  if (renameIdx.value < 0) return
  const idx = renameIdx.value
  const v = renameVal.value.trim()
  renameIdx.value = -1
  if (!v || v === state.features[idx].label) return
  runBuild(() => renameFeature(idx, v), () => toast('success', `特征列已重命名为 [${v}]`))
}
function cancelRename() { renameIdx.value = -1 }

// 登记表按下标定位，删列会让后续下标整体前移，所以先退出编辑态
async function confirmDrop(idx) {
  const feat = state.features[idx]
  try {
    await ElMessageBox.confirm(
      `确认撤销特征列「${feat.label}」？后端工作区的 ${rowCount().toLocaleString()} 行会一并去掉该字段，此操作会写入操作记录（可回放、可导出 Python）。`,
      '撤销特征列', { type: 'warning' })
  } catch (e) { return }
  renameIdx.value = -1
  const label = feat.label
  await runBuild(() => dropFeature(idx), () => toast('success', `特征列 [${label}] 已撤销`))
}

function enterStep() {
  initFeatureList()
  ensureFeatCols()
  ensureCatCols()
  loadCatDist()
  loadHolidayTable()
}
onMounted(enterStep)
onActivated(enterStep)

// 节假日表跟着工作区走：撤销/重做、换数据集都可能换掉生效的那一份，所以按版本号重取
function loadHolidayTable() {
  if (!state.backend.online || !d.value.wsId) return
  loadHolidays()
}
watch(() => `${d.value.wsId}|${d.value.meta?.version ?? -1}`, loadHolidayTable)
</script>

<template>
  <section class="h-full p-4 flex flex-col gap-3 overflow-y-auto">
    <div v-if="!state.backend.online || running" class="rounded-xl border px-3 py-2 text-[11px] flex items-start gap-2 shrink-0"
         :class="state.backend.online ? 'bg-indigo-50 border-indigo-200 text-indigo-700' : 'bg-rose-50 border-rose-200 text-rose-700'">
      <i class="fa-solid mt-0.5" :class="running ? 'fa-spinner fa-spin' : 'fa-triangle-exclamation'"></i>
      <div class="min-w-0">
        <span v-if="running">构建中…</span>
        <span v-else>后端不在线：本页的构建、重命名与撤销均为只读，浏览器不再算第二套</span>
      </div>
    </div>
    <!-- 特征配置区 -->
    <div class="shrink-0 bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden flex flex-col">
      <!-- Tab 栏 + 流水线状态 -->
      <div class="flex items-center flex-wrap gap-y-1.5 border-b border-slate-200 bg-slate-50/80">
        <div class="flex items-center">
          <button @click="activeTab = 'time'"
                  class="flex items-center gap-1.5 px-4 py-2.5 text-xs font-semibold border-b-2 transition-colors"
                  :class="activeTab === 'time' ? 'border-indigo-500 text-indigo-600 bg-white' : 'border-transparent text-slate-500 hover:text-slate-700'">
            <i class="fa-regular fa-calendar-check"></i>时间与日历特征
          </button>
          <button @click="activeTab = 'lag'"
                  class="flex items-center gap-1.5 px-4 py-2.5 text-xs font-semibold border-b-2 transition-colors"
                  :class="activeTab === 'lag' ? 'border-indigo-500 text-indigo-600 bg-white' : 'border-transparent text-slate-500 hover:text-slate-700'">
            <i class="fa-solid fa-clock-rotate-left"></i>滞后与滑动窗口
          </button>
          <button @click="activeTab = 'diff'"
                  class="flex items-center gap-1.5 px-4 py-2.5 text-xs font-semibold border-b-2 transition-colors"
                  :class="activeTab === 'diff' ? 'border-indigo-500 text-indigo-600 bg-white' : 'border-transparent text-slate-500 hover:text-slate-700'">
            <i class="fa-solid fa-wave-square"></i>差分平稳化与频域
          </button>
          <button @click="activeTab = 'cat'"
                  class="flex items-center gap-1.5 px-4 py-2.5 text-xs font-semibold border-b-2 transition-colors"
                  :class="activeTab === 'cat' ? 'border-indigo-500 text-indigo-600 bg-white' : 'border-transparent text-slate-500 hover:text-slate-700'">
            <i class="fa-solid fa-tags"></i>类别特征编码
          </button>
        </div>

        <div class="flex-1"></div>

        <div class="flex items-center gap-2 px-3 h-full text-[10px]">
          <div v-for="chip in PIPE_CHIPS" :key="chip[0]"
               class="flex items-center gap-1 px-1.5 py-0.5 rounded bg-white border border-slate-200">
            <span class="w-1.5 h-1.5 rounded-full" :class="chip[1]"></span>
            <span class="text-slate-500">{{ chip[2] }}</span>
            <span class="font-bold" :class="pipe[chip[0]] ? 'font-mono text-emerald-600' : 'text-slate-400'">{{ pipe[chip[0]] ? '✓ 完成' : '待执行' }}</span>
          </div>
          <span class="text-slate-400 ml-1">新增 <strong class="text-indigo-600">{{ newFeatCount }}</strong> 列</span>
        </div>
      </div>

      <!-- Tab 内容区 -->
      <div class="flex-1 overflow-hidden">

        <!-- Tab 1: 时间与日历特征 -->
        <div v-show="activeTab === 'time'" class="h-full p-4 flex gap-6 items-start">
          <div class="flex-1 min-w-0">
            <h3 class="text-sm font-bold text-slate-800 flex items-center mb-3">
              <i class="fa-regular fa-calendar-check text-indigo-600 mr-2"></i>时间与日历特征提取
            </h3>
            <div class="grid grid-cols-3 gap-x-4 gap-y-2 text-xs text-slate-600">
              <label v-for="o in TIME_OPTS_DIMS" :key="o"
                     class="flex items-center gap-1.5 px-2 py-1.5 rounded-lg hover:bg-indigo-50 border border-transparent hover:border-indigo-200 cursor-pointer transition-colors">
                <input type="checkbox" v-model="timeOpts[o]" class="accent-indigo-500" />
                <span>{{ TIME_DIM_CN[o] }}</span>
              </label>
            </div>

            <div class="mt-3 border border-slate-200 rounded-lg p-3 bg-slate-50/50">
              <div class="flex items-center justify-between gap-2">
                <span class="text-xs font-semibold text-indigo-600">正余弦编码 (Sin / Cos) · 按维度勾选</span>
                <label class="flex items-center gap-1.5 text-[11px] font-medium text-slate-600 cursor-pointer"
                       :class="timeSinCosEnabled ? '' : 'opacity-40'">
                  <input type="checkbox" v-model="timeOpts.keep_cyc_original" :disabled="!timeSinCosEnabled" class="accent-indigo-500" />
                  <span>编码后保留数值原列</span>
                </label>
              </div>
              <div class="flex flex-wrap gap-x-4 gap-y-1.5 mt-2 text-[11px] text-slate-600">
                <label v-for="pair in SINCOS_PAIRS" :key="pair[0]"
                       class="flex items-center gap-1.5" :class="timeOpts[pair[1]] ? 'cursor-pointer' : 'opacity-40'">
                  <input type="checkbox" v-model="timeOpts[pair[0]]" :disabled="!timeOpts[pair[1]]" class="accent-indigo-500" />
                  <span>{{ SINCOS_CN[pair[0]] }}</span>
                </label>
              </div>
              <p class="text-[10px] text-slate-400 mt-1.5 leading-relaxed">
                <template v-if="!timeSinCosEnabled">
                  正余弦不是第 7 个日历维度，而是把上面某个周期维度<b class="text-slate-500">换一种编码</b>：映射到单位圆，避免「23 点与 0 点相差很远」的断裂。当前没有维度被编码，生成的是普通数值日历列。
                </template>
                <template v-else>
                  将对 <b class="text-slate-600">{{ timePlan.cyc.join('、') }}</b> 各生成 sin 与 cos 两列（共
                  <b class="text-indigo-600">{{ timePlan.sinCosCols }}</b> 列）；
                  <template v-if="timeOpts.keep_cyc_original">
                    同时保留它们的数值列 <span class="font-mono text-slate-500">{{ timePlan.cycDims.map(o => 'feat_' + o).join('、') }}</span>（一个维度出三列）。
                  </template>
                  <template v-else>
                    <b class="text-rose-500">不保留数值原列</b>：这些维度只出 sin/cos，不再生成 <span class="font-mono text-slate-500">{{ timePlan.dropped.map(o => 'feat_' + o).join('、') }}</span>。
                  </template>
                  <template v-if="timePlan.daySkipped">日期 (Day) 因每月天数不固定，不参与周期编码。</template>
                </template>
              </p>
              <div class="mt-2 pt-2 border-t border-slate-200 text-[10px] text-slate-400">
                <div>本次将生成 <strong class="text-indigo-600 text-xs">{{ timePlan.total }}</strong> 列</div>
                <div class="font-mono text-slate-400 break-all leading-snug mt-0.5">{{ timePlan.keys.join('  ') || '（未勾选任何维度）' }}</div>
              </div>
            </div>

            <!-- 节假日表：生效的一份留在后端，这里编辑的是草稿，点保存才落成一条可撤销的命令 -->
            <div class="mt-3 border border-slate-200 rounded-lg p-3 bg-white">
              <div class="flex items-center justify-between gap-2 flex-wrap">
                <button @click="holOpen = !holOpen" class="text-[11px] font-semibold text-slate-700 flex items-center gap-1.5 hover:text-indigo-600">
                  <i class="fa-regular fa-calendar-days text-rose-500"></i>节假日表
                  <span class="font-mono text-[10px] text-slate-400">
                    {{ hol.loading ? '读取中…' : hol.error ? '读取失败' : `${holidayDraft.length} 天` }}
                  </span>
                  <span v-if="hol.data" class="font-mono text-[10px] text-slate-400">· {{ hol.data.source }}</span>
                  <span v-if="holidayDirty" class="px-1 rounded bg-amber-100 text-amber-700 text-[9px] font-bold">未保存</span>
                  <i class="fa-solid text-[9px]" :class="holOpen ? 'fa-chevron-up' : 'fa-chevron-down'"></i>
                </button>
                <div v-if="holOpen" class="flex items-center gap-1.5">
                  <button v-for="p in (hol.data?.presets || [])" :key="p.year" @click="applyHolidayPreset(p.year)"
                          class="px-2 py-0.5 text-[10px] rounded border border-slate-200 hover:border-indigo-300 hover:text-indigo-600">
                    {{ p.year }} 年预设（{{ p.count }} 天）
                  </button>
                  <button @click="holidayDraft = []" class="px-2 py-0.5 text-[10px] rounded border border-slate-200 hover:border-rose-300 hover:text-rose-600">清空</button>
                  <button @click="syncHolidayDraft()" :disabled="!holidayDirty"
                          class="px-2 py-0.5 text-[10px] rounded border border-slate-200 hover:border-slate-400 disabled:opacity-40">还原生效表</button>
                </div>
              </div>

              <div v-if="holOpen" class="mt-2 space-y-2">
                <p class="text-[10px] text-slate-400 leading-relaxed">
                  勾选「节假日编码」时，<span class="font-mono">feat_holiday</span> 就是「该行日期是否落在这份表里」。
                  表配在工作区上（撤销/重做都会跟着走），改完点保存即写入一条命令；不保存就只是草稿，生成时用的仍是后端已生效的那份。
                </p>
                <div v-if="hol.error" class="text-[10px] text-rose-600">
                  {{ hol.error }}
                  <button @click="loadHolidayTable()" class="underline ml-1">重试</button>
                </div>
                <div class="flex items-center gap-1.5">
                  <input type="date" v-model="holidayInput" class="border border-slate-200 rounded px-2 py-1 text-[11px] font-mono bg-white" />
                  <button @click="addHolidayDay" class="px-2.5 py-1 text-[11px] rounded-lg bg-indigo-50 border border-indigo-200 text-indigo-700 hover:bg-indigo-100">
                    <i class="fa-solid fa-plus mr-0.5 text-[9px]"></i>加入
                  </button>
                  <span class="text-[10px] text-slate-400">上限 {{ hol.data?.maxDays || '—' }} 天</span>
                </div>
                <div class="max-h-28 overflow-auto border border-slate-100 rounded-lg p-2 bg-slate-50/60">
                  <div v-if="holidayByYear.length === 0" class="text-[10px] text-slate-400">表是空的：生成出的 feat_holiday 会整列为 0。</div>
                  <div v-for="g in holidayByYear" :key="g.year" class="mb-1.5 last:mb-0">
                    <span class="text-[10px] font-bold text-slate-500 mr-1">{{ g.year }} · {{ g.days.length }} 天</span>
                    <span v-for="day in g.days" :key="day"
                          class="inline-flex items-center gap-0.5 mr-1 mb-1 px-1.5 py-0.5 rounded bg-white border border-slate-200 text-[10px] font-mono text-slate-600">
                      {{ day.slice(5) }}
                      <button @click="removeHolidayDay(day)" class="text-slate-300 hover:text-rose-500"><i class="fa-solid fa-xmark text-[8px]"></i></button>
                    </span>
                  </div>
                </div>
                <div class="flex items-center justify-between gap-2">
                  <span class="text-[10px] text-slate-400">
                    草稿 {{ holidayDraft.length }} 天
                    <template v-if="hol.data"> · 生效 {{ hol.data.count }} 天</template>
                    <span v-if="holidayDirty" class="text-amber-600"> · 尚未写入工作区</span>
                  </span>
                  <button @click="commitHolidays('custom')" :disabled="!state.backend.online || !hol.data || !holidayDirty || running"
                          class="px-3 py-1 text-[11px] font-semibold rounded-lg bg-indigo-600 hover:bg-indigo-700 disabled:bg-slate-300 text-white">
                    保存节假日表
                  </button>
                </div>
              </div>
            </div>
          </div>
          <div class="shrink-0 flex flex-col items-center justify-center h-full pl-4 border-l border-slate-100">
            <button @click="runTimeFeatures"
                    class="w-36 py-3 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-xs font-bold shadow-md hover:shadow-lg transition-all flex flex-col items-center gap-1">
              <i class="fa-solid fa-play text-sm"></i>
              <span>生成日历特征</span>
            </button>
            <p class="text-[10px] text-slate-400 mt-2 text-center w-36">自动提取时间戳中的<br/>日历维度与周期信息</p>
          </div>
        </div>

        <!-- Tab 2: 滞后与滑动窗口 -->
        <div v-show="activeTab === 'lag'" class="h-full">
          <div class="h-full flex">
            <div class="w-52 border-r border-slate-100 p-3 flex flex-col bg-slate-50/50 shrink-0">
              <div class="flex items-center justify-between mb-2">
                <h4 class="text-xs font-bold text-slate-700 flex items-center gap-1">
                  <i class="fa-solid fa-table-columns text-amber-500"></i>操作字段
                </h4>
                <div class="flex gap-1">
                  <button @click="toggleAllFeatCols(true)" class="text-[10px] text-indigo-600 hover:underline">全选</button>
                  <span class="text-slate-300">|</span>
                  <button @click="toggleAllFeatCols(false)" class="text-[10px] text-slate-500 hover:underline">清空</button>
                </div>
              </div>
              <div class="flex-1 overflow-auto space-y-0.5 pr-1">
                <label v-for="c in numericCols()" :key="c.key"
                       class="flex items-center gap-1.5 px-1.5 py-1 rounded hover:bg-white cursor-pointer transition-colors text-[11px] text-slate-700"
                       :title="c.key">
                  <input type="checkbox" v-model="featCols[c.key]" class="accent-amber-500" />
                  <span class="truncate">{{ c.label }}</span>
                </label>
              </div>
              <div class="mt-2 pt-1.5 border-t border-slate-200 text-[10px] text-slate-400">
                已选 <strong class="text-indigo-600">{{ featPicked.length }}</strong> 列
              </div>
            </div>
            <div class="flex-1 p-4 overflow-auto">
              <h3 class="text-sm font-bold text-slate-800 flex items-center mb-1">
                <i class="fa-solid fa-clock-rotate-left text-sky-600 mr-2"></i>滞后与滑动窗口特征
              </h3>
              <p class="text-[10px] text-slate-400 mb-3 leading-relaxed">
                左右两张卡片是两条互不相干的命令：各自的参数、各自的列名预览、各自的生成按钮。
                点其中一张只提交它那一族，另一张已经生成的列原样留着（同一张卡片重新生成时，上一批里没再勾选的列会退场）。
              </p>
              <div class="grid grid-cols-2 gap-4 items-stretch">

                <!-- 卡片 1：滞后特征 —— 只吃「滞后阶数」这一个输入 -->
                <section class="flex flex-col rounded-xl border-2 border-sky-200 bg-white shadow-sm overflow-hidden">
                  <header class="flex items-center gap-2 px-3 py-2 bg-sky-50/80 border-b border-sky-100">
                    <span class="w-6 h-6 shrink-0 grid place-items-center rounded-md bg-sky-600 text-white">
                      <i class="fa-solid fa-arrow-left-long text-[11px]"></i>
                    </span>
                    <div class="min-w-0">
                      <h4 class="text-xs font-bold text-slate-800 leading-tight">滞后特征 (Lag)</h4>
                      <p class="text-[10px] text-slate-400 leading-tight">把前 N 步的值搬到当前行</p>
                    </div>
                    <span class="ml-auto shrink-0 px-1.5 py-0.5 rounded font-mono text-[10px]"
                          :class="familyCount.lag ? 'bg-sky-100 text-sky-700' : 'bg-slate-100 text-slate-400'">
                      表内 {{ familyCount.lag }} 列
                    </span>
                  </header>
                  <div class="p-3 flex-1 flex flex-col gap-3">
                    <div>
                      <label class="text-[11px] text-slate-500 block mb-1">滞后阶数 (逗号分隔)</label>
                      <input type="text" v-model="lagSteps"
                             class="w-full border border-slate-200 rounded-lg px-2.5 py-1.5 text-xs font-mono bg-white outline-none focus:border-sky-400" />
                      <span class="text-[10px] text-slate-400">96 步 = 15min 采样下的前 1 天</span>
                    </div>
                    <div class="mt-auto border border-slate-200 rounded-lg p-2.5 bg-slate-50/60">
                      <div class="flex items-center justify-between gap-2">
                        <span class="text-[10px] text-slate-500 shrink-0">
                          本卡片将写入 <strong class="text-sky-600 text-xs">{{ lagOnlyPlan.length }}</strong> 列
                          <span v-if="lagOnlyPlan.length === 0" class="text-rose-500">（未填写滞后阶数）</span>
                        </span>
                        <button v-if="lagOnlyPlan.length > 0" @click="lagPlanOpen = !lagPlanOpen"
                                class="text-[10px] text-sky-600 hover:underline shrink-0">
                          {{ lagPlanOpen ? '收起列名' : '查看列名' }}
                        </button>
                      </div>
                      <div v-if="lagPlanOpen && lagOnlyPlan.length > 0"
                           class="mt-1.5 pt-1.5 border-t border-slate-200 font-mono text-[10px] text-slate-400 break-all leading-snug max-h-24 overflow-auto">
                        {{ lagOnlyPlan.join('  ') }}
                      </div>
                    </div>
                  </div>
                  <footer class="px-3 py-2.5 border-t border-sky-100 bg-white flex items-center justify-between gap-2">
                    <span class="text-[10px] text-slate-400">只提交滞后这一族</span>
                    <button @click="runLagGroup('lag')" :disabled="running"
                            class="px-4 py-2 bg-sky-600 hover:bg-sky-700 disabled:bg-slate-300 text-white rounded-lg text-[11px] font-bold shadow-sm transition-all flex items-center gap-1.5">
                      <i class="fa-solid fa-play text-[9px]"></i>生成滞后特征
                    </button>
                  </footer>
                </section>

                <!-- 卡片 2：滑动窗口特征 —— 窗口大小 / 统计量 / 高级窗口都归它 -->
                <section class="flex flex-col rounded-xl border-2 border-cyan-200 bg-white shadow-sm overflow-hidden">
                  <header class="flex items-center gap-2 px-3 py-2 bg-cyan-50/80 border-b border-cyan-100">
                    <span class="w-6 h-6 shrink-0 grid place-items-center rounded-md bg-cyan-600 text-white">
                      <i class="fa-solid fa-bars-staggered text-[11px]"></i>
                    </span>
                    <div class="min-w-0">
                      <h4 class="text-xs font-bold text-slate-800 leading-tight">滑动窗口特征 (Rolling)</h4>
                      <p class="text-[10px] text-slate-400 leading-tight">在紧邻的历史窗口上聚合</p>
                    </div>
                    <span class="ml-auto shrink-0 px-1.5 py-0.5 rounded font-mono text-[10px]"
                          :class="familyCount.window ? 'bg-cyan-100 text-cyan-700' : 'bg-slate-100 text-slate-400'">
                      表内 {{ familyCount.window }} 列
                    </span>
                  </header>
                  <div class="p-3 flex-1 flex flex-col gap-3">
                    <div>
                      <label class="text-[11px] text-slate-500 block mb-1">滚动窗口大小 (逗号分隔)</label>
                      <input type="text" v-model="rollWindows"
                             class="w-full border border-slate-200 rounded-lg px-2.5 py-1.5 text-xs font-mono bg-white outline-none focus:border-cyan-400" />
                      <span class="text-[10px] text-slate-400">4 = 1h · 16 = 4h · 96 = 1 天</span>
                    </div>
                    <div>
                      <span class="text-[11px] text-slate-500 font-semibold block mb-1">滚动统计量</span>
                      <div class="flex flex-wrap gap-1.5 text-[11px] text-slate-600">
                        <label v-for="s in ROLL_STAT_OPTS" :key="s[0]"
                               class="flex items-center gap-1.5 px-2 py-1 rounded-md bg-white border border-slate-200 hover:border-cyan-300 cursor-pointer transition-colors">
                          <input type="checkbox" v-model="rollStats[s[0]]" class="accent-cyan-500" /> {{ s[1] }}
                        </label>
                      </div>
                    </div>
                    <div class="border border-slate-200 rounded-lg p-2.5 bg-slate-50/60 text-[11px] text-slate-600">
                      <span class="text-[11px] text-slate-500 font-semibold block mb-1">高级窗口 (Advanced)</span>
                      <label class="flex items-center gap-1.5">
                        <input type="checkbox" v-model="rollExpanding" class="accent-cyan-500" /> 扩展窗口均值 (Expanding)
                      </label>
                      <label class="flex items-center gap-1.5 mt-1">
                        <input type="checkbox" v-model="rollEwm" class="accent-cyan-500" /> 指数加权移动平均 (EWM)
                      </label>
                      <div class="flex items-center gap-1.5 pl-6 mt-1" :class="rollEwm ? '' : 'opacity-40'">
                        <span class="text-[10px] text-slate-400 shrink-0">span</span>
                        <input type="number" v-model.number="ewmSpan" :disabled="!rollEwm" min="2" max="500" step="1"
                               class="w-16 border border-slate-200 rounded px-1.5 py-0.5 text-[11px] font-mono bg-white disabled:bg-slate-50" />
                        <span class="text-[10px] text-slate-400">α = 2/(span+1) = {{ (2 / (normEwmSpan(ewmSpan) + 1)).toFixed(3) }}</span>
                      </div>
                    </div>
                    <div class="mt-auto border border-slate-200 rounded-lg p-2.5 bg-slate-50/60">
                      <div class="flex items-center justify-between gap-2">
                        <span class="text-[10px] text-slate-500 shrink-0">
                          本卡片将写入 <strong class="text-cyan-600 text-xs">{{ windowPlan.length }}</strong> 列
                          <span v-if="windowPlan.length === 0" class="text-rose-500">（未填窗口、未勾 Expanding / EWM）</span>
                        </span>
                        <button v-if="windowPlan.length > 0" @click="windowPlanOpen = !windowPlanOpen"
                                class="text-[10px] text-cyan-600 hover:underline shrink-0">
                          {{ windowPlanOpen ? '收起列名' : '查看列名' }}
                        </button>
                      </div>
                      <div v-if="windowPlanOpen && windowPlan.length > 0"
                           class="mt-1.5 pt-1.5 border-t border-slate-200 font-mono text-[10px] text-slate-400 break-all leading-snug max-h-24 overflow-auto">
                        {{ windowPlan.join('  ') }}
                      </div>
                    </div>
                  </div>
                  <footer class="px-3 py-2.5 border-t border-cyan-100 bg-white flex items-center justify-between gap-2">
                    <span class="text-[10px] text-slate-400">只提交滑动窗口这一族</span>
                    <button @click="runLagGroup('window')" :disabled="running"
                            class="px-4 py-2 bg-cyan-600 hover:bg-cyan-700 disabled:bg-slate-300 text-white rounded-lg text-[11px] font-bold shadow-sm transition-all flex items-center gap-1.5">
                      <i class="fa-solid fa-play text-[9px]"></i>生成滑动窗口特征
                    </button>
                  </footer>
                </section>
              </div>
              <p class="text-[10px] text-slate-400 mt-3 leading-relaxed">
                窗口类特征一律不含当前行（等价 pandas <span class="font-mono">.shift(1)</span>），防止用 t 时刻的值泄漏预测 t 时刻的标签；
                Expanding 为历史累计均值，EWM 半衰期由 span 控制。两张卡片用的是左侧同一份「操作字段」勾选集。
              </p>
            </div>
          </div>
        </div>

        <!-- Tab 3: 差分平稳化与频域 -->
        <div v-show="activeTab === 'diff'" class="h-full">
          <div class="h-full flex">
            <div class="w-52 border-r border-slate-100 p-3 flex flex-col bg-slate-50/50 shrink-0">
              <div class="flex items-center justify-between mb-2">
                <h4 class="text-xs font-bold text-slate-700 flex items-center gap-1">
                  <i class="fa-solid fa-table-columns text-amber-500"></i>操作字段
                </h4>
                <div class="flex gap-1">
                  <button @click="toggleAllFeatCols(true)" class="text-[10px] text-indigo-600 hover:underline">全选</button>
                  <span class="text-slate-300">|</span>
                  <button @click="toggleAllFeatCols(false)" class="text-[10px] text-slate-500 hover:underline">清空</button>
                </div>
              </div>
              <div class="flex-1 overflow-auto space-y-0.5 pr-1">
                <label v-for="c in numericCols()" :key="c.key"
                       class="flex items-center gap-1.5 px-1.5 py-1 rounded hover:bg-white cursor-pointer transition-colors text-[11px] text-slate-700"
                       :title="c.key">
                  <input type="checkbox" v-model="featCols[c.key]" class="accent-amber-500" />
                  <span class="truncate">{{ c.label }}</span>
                </label>
              </div>
              <div class="mt-2 pt-1.5 border-t border-slate-200 text-[10px] text-slate-400">
                已选 <strong class="text-indigo-600">{{ featPicked.length }}</strong> 列
              </div>
            </div>
            <div class="flex-1 p-4 overflow-auto">
              <h3 class="text-sm font-bold text-slate-800 flex items-center mb-1">
                <i class="fa-solid fa-wave-square text-emerald-600 mr-2"></i>差分平稳化与频域特征
              </h3>
              <p class="text-[10px] text-slate-400 mb-3 leading-relaxed">
                左右两张卡片是两条互不相干的命令：差分只做时域差商，频域只在滑动 DFT 窗口上取谱特征，
                各自的生成按钮只提交自己那一族。
              </p>
              <div class="grid grid-cols-2 gap-4 items-stretch">

                <!-- 卡片 1：差分平稳化 -->
                <section class="flex flex-col rounded-xl border-2 border-emerald-200 bg-white shadow-sm overflow-hidden">
                  <header class="flex items-center gap-2 px-3 py-2 bg-emerald-50/80 border-b border-emerald-100">
                    <span class="w-6 h-6 shrink-0 grid place-items-center rounded-md bg-emerald-600 text-white">
                      <i class="fa-solid fa-minus text-[11px]"></i>
                    </span>
                    <div class="min-w-0">
                      <h4 class="text-xs font-bold text-slate-800 leading-tight">差分平稳化 (Differencing)</h4>
                      <p class="text-[10px] text-slate-400 leading-tight">时域上做差，消趋势与季节项</p>
                    </div>
                    <span class="ml-auto shrink-0 px-1.5 py-0.5 rounded font-mono text-[10px]"
                          :class="familyCount.diff ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-400'">
                      表内 {{ familyCount.diff }} 列
                    </span>
                  </header>
                  <div class="p-3 flex-1 flex flex-col gap-3">
                    <div class="space-y-2 text-[11px] text-slate-600">
                      <label class="flex items-center gap-2 px-2 py-1.5 rounded-md bg-white border border-slate-200 hover:border-emerald-300 cursor-pointer transition-colors">
                        <input type="checkbox" v-model="diff1st" class="accent-emerald-500" />
                        <span>一阶差分 (Δ¹y)</span>
                        <span class="text-[10px] text-slate-400 ml-auto">消除线性趋势</span>
                      </label>
                      <label class="flex items-center gap-2 px-2 py-1.5 rounded-md bg-white border border-slate-200 hover:border-emerald-300 cursor-pointer transition-colors">
                        <input type="checkbox" v-model="diff2nd" class="accent-emerald-500" />
                        <span>二阶差分 (Δ²y)</span>
                        <span class="text-[10px] text-slate-400 ml-auto">消除二次趋势</span>
                      </label>
                      <label class="flex items-center gap-2 px-2 py-1.5 rounded-md bg-white border border-slate-200 hover:border-emerald-300 cursor-pointer transition-colors">
                        <input type="checkbox" v-model="diffSeasonal" class="accent-emerald-500" />
                        <span>季节性差分</span>
                        <span class="text-[10px] text-slate-400 ml-auto flex items-center gap-1">
                          周期
                          <input type="number" v-model="diffPeriod" min="1" class="w-12 border border-slate-200 rounded px-1 py-0.5 text-[10px] font-mono text-center bg-white" />
                          步
                        </span>
                      </label>
                    </div>
                    <div class="mt-auto border border-slate-200 rounded-lg p-2.5 bg-slate-50/60">
                      <div class="flex items-center justify-between gap-2">
                        <span class="text-[10px] text-slate-500 shrink-0">
                          本卡片将写入 <strong class="text-emerald-600 text-xs">{{ diffOnlyPlan.length }}</strong> 列
                          <span v-if="diffOnlyPlan.length === 0" class="text-rose-500">（未勾选任何差分）</span>
                        </span>
                        <button v-if="diffOnlyPlan.length > 0" @click="diffPlanOpen = !diffPlanOpen"
                                class="text-[10px] text-emerald-600 hover:underline shrink-0">
                          {{ diffPlanOpen ? '收起列名' : '查看列名' }}
                        </button>
                      </div>
                      <div v-if="diffPlanOpen && diffOnlyPlan.length > 0"
                           class="mt-1.5 pt-1.5 border-t border-slate-200 font-mono text-[10px] text-slate-400 break-all leading-snug max-h-24 overflow-auto">
                        {{ diffOnlyPlan.join('  ') }}
                      </div>
                    </div>
                  </div>
                  <footer class="px-3 py-2.5 border-t border-emerald-100 bg-white flex items-center justify-between gap-2">
                    <span class="text-[10px] text-slate-400">只提交差分这一族</span>
                    <button @click="runDiffGroup('diff')" :disabled="running"
                            class="px-4 py-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white rounded-lg text-[11px] font-bold shadow-sm transition-all flex items-center gap-1.5">
                      <i class="fa-solid fa-play text-[9px]"></i>生成差分特征
                    </button>
                  </footer>
                </section>

                <!-- 卡片 2：频域特征 -->
                <section class="flex flex-col rounded-xl border-2 border-teal-200 bg-white shadow-sm overflow-hidden">
                  <header class="flex items-center gap-2 px-3 py-2 bg-teal-50/80 border-b border-teal-100">
                    <span class="w-6 h-6 shrink-0 grid place-items-center rounded-md bg-teal-600 text-white">
                      <i class="fa-solid fa-chart-line text-[11px]"></i>
                    </span>
                    <div class="min-w-0">
                      <h4 class="text-xs font-bold text-slate-800 leading-tight">频域特征 (FFT 频谱分析)</h4>
                      <p class="text-[10px] text-slate-400 leading-tight">在 64 点滑动窗口上取谱</p>
                    </div>
                    <span class="ml-auto shrink-0 px-1.5 py-0.5 rounded font-mono text-[10px]"
                          :class="familyCount.fft ? 'bg-teal-100 text-teal-700' : 'bg-slate-100 text-slate-400'">
                      表内 {{ familyCount.fft }} 列
                    </span>
                  </header>
                  <div class="p-3 flex-1 flex flex-col gap-3">
                    <div class="space-y-2 text-[11px] text-slate-600">
                      <label class="flex items-center gap-2 px-2 py-1.5 rounded-md bg-white border border-slate-200 hover:border-teal-300 cursor-pointer transition-colors">
                        <input type="checkbox" v-model="fftDominant" class="accent-teal-500" />
                        <span>主导频率能量 (Top-3)</span>
                        <span class="text-[10px] text-slate-400 ml-auto">DFT 峰值</span>
                      </label>
                      <label class="flex items-center gap-2 px-2 py-1.5 rounded-md bg-white border border-slate-200 hover:border-teal-300 cursor-pointer transition-colors">
                        <input type="checkbox" v-model="fftEntropy" class="accent-teal-500" />
                        <span>频域谱熵 (Spectral Entropy)</span>
                        <span class="text-[10px] text-slate-400 ml-auto">复杂度指标</span>
                      </label>
                      <label class="flex items-center gap-2 px-2 py-1.5 rounded-md bg-white border border-slate-200 hover:border-teal-300 cursor-pointer transition-colors">
                        <input type="checkbox" v-model="fftPowerRatio" class="accent-teal-500" />
                        <span>频带能量比 (Power Ratio)</span>
                        <span class="text-[10px] text-slate-400 ml-auto">低频/高频</span>
                      </label>
                    </div>
                    <p class="text-[10px] text-slate-400 leading-relaxed">
                      频域特征基于 DFT 在 64 点滑动窗口上逐点计算，仅对首个勾选列执行。
                    </p>
                    <div class="mt-auto border border-slate-200 rounded-lg p-2.5 bg-slate-50/60">
                      <div class="flex items-center justify-between gap-2">
                        <span class="text-[10px] text-slate-500 shrink-0">
                          本卡片将写入 <strong class="text-teal-600 text-xs">{{ fftOnlyPlan.length }}</strong> 列
                          <span v-if="fftOnlyPlan.length === 0" class="text-rose-500">（未勾选任何频域项）</span>
                        </span>
                        <button v-if="fftOnlyPlan.length > 0" @click="fftPlanOpen = !fftPlanOpen"
                                class="text-[10px] text-teal-600 hover:underline shrink-0">
                          {{ fftPlanOpen ? '收起列名' : '查看列名' }}
                        </button>
                      </div>
                      <div v-if="fftPlanOpen && fftOnlyPlan.length > 0"
                           class="mt-1.5 pt-1.5 border-t border-slate-200 font-mono text-[10px] text-slate-400 break-all leading-snug max-h-24 overflow-auto">
                        {{ fftOnlyPlan.join('  ') }}
                      </div>
                    </div>
                  </div>
                  <footer class="px-3 py-2.5 border-t border-teal-100 bg-white flex items-center justify-between gap-2">
                    <span class="text-[10px] text-slate-400">只提交频域这一族</span>
                    <button @click="runDiffGroup('fft')" :disabled="running"
                            class="px-4 py-2 bg-teal-600 hover:bg-teal-700 disabled:bg-slate-300 text-white rounded-lg text-[11px] font-bold shadow-sm transition-all flex items-center gap-1.5">
                      <i class="fa-solid fa-play text-[9px]"></i>生成频域特征
                    </button>
                  </footer>
                </section>
              </div>
              <p class="text-[10px] text-slate-400 mt-3 leading-relaxed">
                两张卡片用的是左侧同一份「操作字段」勾选集；差分与频域互不覆盖，撤销其中一族也不会动另一族的列。
              </p>
            </div>
          </div>
        </div>

        <!-- Tab 4: 类别特征编码 -->
        <div v-show="activeTab === 'cat'" class="h-full">
          <div class="h-full flex">
            <div class="w-56 border-r border-slate-100 p-3 flex flex-col bg-slate-50/50 shrink-0">
              <h4 class="text-xs font-bold text-slate-700 flex items-center gap-1.5 mb-2">
                <i class="fa-solid fa-table-columns text-violet-500"></i>选择类别列
              </h4>
              <div class="flex-1 overflow-auto space-y-0.5 pr-1">
                <div v-if="catColsList().length === 0" class="text-[11px] text-slate-400 py-4 text-center">当前数据集无类别列</div>
                <label v-for="c in catColsList()" :key="c.key"
                       class="flex items-center gap-1.5 px-1.5 py-1 rounded hover:bg-white cursor-pointer transition-colors text-[11px] text-slate-700"
                       :title="c.key">
                  <input type="checkbox" v-model="catCols[c.key]" class="accent-violet-500" />
                  <span class="truncate">{{ c.label }}</span>
                </label>
              </div>
              <div class="mt-2 pt-1.5 border-t border-slate-200 text-[10px] text-slate-400">
                已选 <strong class="text-violet-600">{{ catPicked.length }}</strong> 列
              </div>
            </div>
            <div class="flex-1 p-4 flex flex-col justify-between overflow-auto">
              <div>
                <h3 class="text-sm font-bold text-slate-800 flex items-center mb-3">
                  <i class="fa-solid fa-tags text-violet-600 mr-2"></i>类别特征编码
                </h3>
                <div class="grid grid-cols-2 gap-5 mb-4">
                  <div>
                    <label class="text-[11px] text-slate-500 block mb-1.5">编码方式</label>
                    <select v-model="catMethod" class="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs bg-white focus:border-violet-400 outline-none transition-colors">
                      <option value="onehot">独热编码 (One-Hot Encoding)</option>
                      <option value="ordinal">序数编码 (Ordinal / Label)</option>
                      <option value="target">目标均值编码 (Target Encoding)</option>
                    </select>
                    <p class="text-[10px] text-slate-400 mt-1.5">独热编码为每个类别生成独立 0/1 列；序数编码按出现顺序映射为整数；目标均值编码以主数值列为目标 (存在数据泄露风险，仅用于探索)</p>
                  </div>
                  <div>
                    <label class="text-[11px] text-slate-500 block mb-1.5">编码参数预览</label>
                    <div class="text-[11px] p-2.5 bg-slate-50 border border-slate-200 rounded-lg text-slate-600 font-mono min-h-[42px]">
                      <template v-if="catPicked.length === 0">选择类别列后自动预览</template>
                      <template v-else-if="catDist.loading">后端正在整表计数…</template>
                      <template v-else-if="catDist.error">{{ catDist.error }}</template>
                      <template v-else>
                        <span class="text-violet-600 font-bold">{{ catPicked.length }}</span> 列 ·
                        <span class="text-violet-600 font-bold">{{ CAT_METHOD_NAMES[catMethod] }}</span> · 预计新增
                        <span class="text-violet-600 font-bold">{{ catDist.totalNewCols }}</span> 列
                      </template>
                    </div>
                  </div>
                </div>
                <div class="border border-slate-200 rounded-lg overflow-hidden">
                  <div class="bg-slate-50 px-3 py-1.5 border-b border-slate-200 flex items-center justify-between">
                    <span class="text-[11px] font-semibold text-slate-600">所选列类别值分布</span>
                    <span class="text-[10px] text-slate-400">{{ catPicked.length === 0 ? '请选择类别列' : `${CAT_METHOD_NAMES[catMethod]} · 预计新增 ${catDist.totalNewCols} 列` }}</span>
                  </div>
                  <div class="max-h-[150px] overflow-auto">
                    <table class="w-full text-xs">
                      <thead class="sticky top-0 bg-slate-50 text-slate-500 border-b border-slate-200">
                        <tr>
                          <th class="text-left px-3 py-1.5 font-semibold">列名</th>
                          <th class="text-left px-3 py-1.5 font-semibold">类别值</th>
                          <th class="text-right px-3 py-1.5 font-semibold">类别数</th>
                          <th class="text-right px-3 py-1.5 font-semibold">总行数</th>
                          <th class="px-3 py-1.5 font-semibold w-32">分布条</th>
                        </tr>
                      </thead>
                      <tbody class="divide-y divide-slate-100 font-mono">
                        <tr v-if="catDist.rows.length === 0"><td colspan="5" class="text-center py-6 text-slate-300">—</td></tr>
                        <tr v-for="r in catDist.rows" :key="r.key" class="hover:bg-violet-50/30">
                          <td class="px-3 py-1.5 text-slate-700 font-sans font-medium truncate max-w-[100px]">{{ r.label }}</td>
                          <td class="px-3 py-1.5 text-slate-500 text-[10px] truncate max-w-[200px]" :title="r.truncated ? `仅显示前 ${r.uniqueVals.length} 个取值，另有 ${r.restCount.toLocaleString()} 行落在其余取值` : r.topVals">{{ r.topVals || '—' }}</td>
                          <td class="px-3 py-1.5 text-right font-semibold text-violet-600" :title="r.truncated ? `后端只回传前 ${limits.uniqueValuesReported || 200} 个取值，此数为真实取值数` : '整表去重取值数'">
                            {{ r.uniqueTotal.toLocaleString() }}<span v-if="r.truncated" class="text-slate-400 font-normal">+</span>
                          </td>
                          <td class="px-3 py-1.5 text-right text-slate-600" :title="`非缺失 ${r.nonMissingRows.toLocaleString()} 行 / 共 ${r.total.toLocaleString()} 行`">{{ r.total.toLocaleString() }}</td>
                          <td class="px-3 py-1.5">
                            <div class="w-full h-2 bg-slate-100 rounded-full overflow-hidden flex">
                              <div v-for="(seg, si) in distBar(r)" :key="si" class="h-full" :class="seg.cls" :style="seg.style"></div>
                            </div>
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
              <div class="flex justify-end mt-3">
                <button @click="runCatFeatures"
                        class="px-8 py-2.5 bg-violet-600 hover:bg-violet-700 text-white rounded-xl text-xs font-bold shadow-md hover:shadow-lg transition-all flex items-center gap-2">
                  <i class="fa-solid fa-play text-[10px]"></i>执行分类特征编码
                </button>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>

    <!-- 特征矩阵实时预览 -->
    <div class="flex-1 bg-white rounded-xl border border-slate-200 shadow-sm flex flex-col min-h-[300px] overflow-hidden">
      <div class="px-4 py-2 border-b border-slate-200 flex justify-between items-center bg-slate-50">
        <div class="flex items-center space-x-3">
          <span class="text-xs font-bold text-slate-700">特征工程多维矩阵实时预览 <span class="text-[10px] text-slate-400 font-normal">(双击列名可重命名)</span></span>
          <span class="px-2 py-0.5 rounded-full text-[11px] font-mono bg-indigo-100 text-indigo-800 font-semibold">
            当前共计: {{ state.features.length }} 个特征列 (新增 {{ newFeatCount }} 列)
          </span>
        </div>
        <div class="flex items-center space-x-2 text-[11px]">
          <div class="flex items-center gap-1 mr-1">
            <button @click="jumpToComplete" title="定位到首个新增特征全部有值的行"
                    class="px-2 py-0.5 rounded border border-slate-200 text-slate-600 hover:border-indigo-300 hover:text-indigo-600">首个完整行</button>
            <button @click="shiftPreview(-1)" :disabled="previewAtHead"
                    class="w-6 h-6 flex items-center justify-center rounded border border-slate-200 text-slate-600 hover:border-indigo-300 disabled:opacity-30 disabled:cursor-not-allowed">
              <i class="fa-solid fa-chevron-left text-[10px]"></i>
            </button>
            <span class="font-mono text-slate-500" :title="`预览读的是后端 /rows 分页窗口（每页 ${page.limit} 行）· 工作区共 ${rowCount().toLocaleString()} 行`">第 {{ previewFrom }}–{{ previewTo }} 行 / 工作区共 {{ totalRows.toLocaleString() }} 行</span>
            <button @click="shiftPreview(1)" :disabled="previewTo >= totalRows"
                    class="w-6 h-6 flex items-center justify-center rounded border border-slate-200 text-slate-600 hover:border-indigo-300 disabled:opacity-30 disabled:cursor-not-allowed">
              <i class="fa-solid fa-chevron-right text-[10px]"></i>
            </button>
          </div>
          <span class="flex items-center"><span class="w-2 h-2 rounded bg-indigo-500 mr-1"></span>原始特征</span>
          <span class="flex items-center"><span class="w-2 h-2 rounded bg-emerald-500 mr-1"></span>新增工程特征</span>
        </div>
      </div>
      <div class="flex-1 overflow-auto">
        <table class="w-full text-left text-xs border-collapse">
          <thead class="sticky top-0 bg-slate-100 text-slate-700 font-semibold border-b border-slate-200 z-10">
            <tr>
              <th v-for="(f, idx) in state.features" :key="f.key"
                  class="group px-3 py-2 border-r border-slate-200 last:border-r-0 whitespace-nowrap select-none">
                <div v-if="renameIdx === idx" class="flex items-center gap-1">
                  <input ref="renameInput" type="text" v-model="renameVal" @keydown.enter="commitRename" @keydown.esc="cancelRename"
                         class="flex-1 text-xs font-semibold px-1.5 py-0.5 border border-indigo-400 rounded bg-white outline-none ring-2 ring-indigo-200 min-w-0 max-w-[160px]" />
                  <button @click="commitRename" title="确定"
                          class="w-5 h-5 flex items-center justify-center bg-indigo-600 text-white rounded hover:bg-indigo-700 shrink-0 text-[10px]">
                    <i class="fa-solid fa-check"></i>
                  </button>
                  <button @click="cancelRename" title="取消"
                          class="w-5 h-5 flex items-center justify-center bg-slate-200 text-slate-600 rounded hover:bg-slate-300 shrink-0 text-[10px]">
                    <i class="fa-solid fa-xmark"></i>
                  </button>
                </div>
                <div v-else class="flex items-center gap-1.5">
                  <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="f.isNew ? 'bg-emerald-500' : 'bg-indigo-500'"></span>
                  <span class="cursor-pointer hover:text-indigo-600 truncate max-w-[140px]" @dblclick="startRename(idx)" title="双击重命名">{{ f.label }}</span>
                  <button @click="startRename(idx)" title="重命名"
                          class="w-4 h-4 flex items-center justify-center text-slate-300 hover:text-indigo-600 hover:bg-indigo-100 rounded opacity-0 group-hover:opacity-100 transition-opacity shrink-0 text-[10px]">
                    <i class="fa-solid fa-pen"></i>
                  </button>
                  <button v-if="f.isNew" @click="confirmDrop(idx)" title="撤销该特征列（从数据与导出宽表中删除）"
                          class="w-4 h-4 flex items-center justify-center text-slate-300 hover:text-rose-600 hover:bg-rose-100 rounded opacity-0 group-hover:opacity-100 transition-opacity shrink-0 text-[10px]">
                    <i class="fa-solid fa-xmark"></i>
                  </button>
                </div>
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100 font-mono text-slate-600">
            <tr v-for="(row, ri) in previewRows" :key="previewStart + ri" class="hover:bg-indigo-50/30">
              <td v-for="f in state.features" :key="f.key"
                  class="px-3 py-1.5 border-r border-slate-100 last:border-r-0 whitespace-nowrap"
                  :class="f.isNew ? 'bg-emerald-50/20 text-emerald-900 font-semibold' : ''">
                <span v-if="isNull(row[f.key])" class="text-slate-300" title="该行历史长度不足以覆盖窗口，或源值缺失">—</span>
                <template v-else>{{ row[f.key] }}</template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 底部只留返回：Python Pipeline 与宽表导出统一走顶栏「导出处理结果」 -->
    <div class="flex justify-between items-center shrink-0">
      <button @click="switchStep(4)" class="px-4 py-1.5 bg-slate-200 hover:bg-slate-300 text-slate-700 rounded-lg text-xs font-medium">
        <i class="fa-solid fa-arrow-left mr-1"></i>返回质量清洗
      </button>
    </div>
  </section>
</template>
