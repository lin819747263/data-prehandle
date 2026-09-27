<script setup>
import { computed, reactive, ref, nextTick, onActivated, onMounted, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import {
  state, ds, toast, switchStep, rowCount, refreshPage, wsActionLog,
  initFeatureList, buildTimeFeatures, timePlanLabel, buildLagFeatures, buildDiffFeatures,
  buildCatFeatures, catColumnDistribution, renameFeature, dropFeature, firstCompleteRow,
  ROLL_STATS, lagFeaturePlan, normEwmSpan, diffFeaturePlan
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
const pipe = computed(() => {
  void state.dataVersion
  const built = new Set(wsActionLog().filter(e => e.params?.type === 'feature_build').map(e => e.params.featureType))
  return { time: built.has('time'), lag: built.has('lag_roll'), diff: built.has('diff_freq'), cat: built.has('cat') }
})

// ---- Tab 1: 时间与日历特征 ----
const TIME_OPTS_ALL = ['hour', 'day', 'month', 'weekday', 'is_weekend', 'holiday', 'cyclical_sincos']
// 勾选集一律用 { key: bool } 映射：模板的 v-model="x[k]" 写的是对象属性，
// 若状态是 Set，属性写入不会进 Set，勾选框既显示不出初始值也不生效。
const timeOpts = reactive(Object.fromEntries(TIME_OPTS_ALL.map(k => [k, true])))

function pickedKeys(map) { return Object.keys(map).filter(k => map[k]) }

// 展示用的「本次会生成哪些列」，与 store 里的构建逻辑同一函数，数字可追溯
const timePlan = computed(() => timePlanLabel(TIME_OPTS_ALL.filter(o => timeOpts[o])))

function runTimeFeatures() {
  const chosen = TIME_OPTS_ALL.filter(o => timeOpts[o])
  if (chosen.length === 0) { toast('warning', '请至少勾选一项时间特征'); return }
  const plan = timePlanLabel(chosen)
  if (plan.total === 0) {
    toast('warning', '正余弦是对已勾选的周期维度（小时 / 星期 / 月份）换一种编码，需先勾选至少一个周期维度')
    return
  }
  runBuild(() => buildTimeFeatures(chosen), r => {
    toast('success', `已写入 ${r.cols} 个时间特征列（其中正余弦 ${r.sinCos} 列，本次新增 ${r.created} 列）`)
  })
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
const lagPlan = computed(() => lagFeaturePlan(targetColsOrDefault(), lagParams()))

function runLagFeatures() {
  const targetCols = targetColsOrDefault()
  if (targetCols.length === 0) { toast('warning', '请先在左侧勾选至少一个操作字段'); return }
  const params = lagParams()
  if (params.lags.length === 0 && params.windows.length === 0 && !params.expanding && !params.ewm) {
    toast('warning', '滞后阶数、滚动窗口与高级选项均为空，无特征可生成')
    return
  }
  if (params.stats.length === 0 && params.windows.length > 0) {
    toast('warning', '已填写滚动窗口但未勾选任何滚动统计量')
    return
  }
  runBuild(() => buildLagFeatures(targetCols, params), r => {
    toast('success', `已写入 ${r.cols} 个滞后与窗口特征列（本次新增 ${r.created} 列）· 目标列: ${targetCols.join(', ')}`)
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
const diffPlan = computed(() => diffFeaturePlan(targetColsOrDefault(), diffParams()))
const diffPlanOpen = ref(false)

function runDiffFeatures() {
  const targetCols = targetColsOrDefault()
  if (targetCols.length === 0) { toast('warning', '请先在左侧勾选至少一个操作字段'); return }
  if (!diff1st.value && !diff2nd.value && !diffSeasonal.value && !fftDominant.value && !fftEntropy.value && !fftPowerRatio.value) {
    toast('warning', '请至少勾选一项差分或频域特征')
    return
  }
  runBuild(() => buildDiffFeatures(targetCols, diffParams()), r => {
    toast('success', `已写入 ${r.cols} 个差分与频域特征列（本次新增 ${r.created} 列）· 目标列: ${targetCols.join(', ')}`)
  })
}

// ---- Tab 4: 类别特征编码 ----
const catCols = reactive({})
const catMethod = ref('onehot')
const CAT_METHOD_NAMES = { onehot: '独热编码', ordinal: '序数编码', target: '目标均值编码' }
const catPicked = computed(() => pickedKeys(catCols))

function catColsList() { return d.value.columns.filter(c => c.type === 'category') }
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
}
onMounted(enterStep)
onActivated(enterStep)
</script>

<template>
  <section class="h-full p-4 flex flex-col gap-3 overflow-y-auto">
    <div class="rounded-xl border px-3 py-2 text-[11px] flex items-start gap-2 shrink-0"
         :class="state.backend.online ? 'bg-indigo-50 border-indigo-200 text-indigo-700' : 'bg-rose-50 border-rose-200 text-rose-700'">
      <i class="fa-solid mt-0.5" :class="state.backend.online ? 'fa-server' : 'fa-triangle-exclamation'"></i>
      <div class="min-w-0">
        <div class="font-semibold">
          <template v-if="state.backend.online">
            四类特征均在后端整帧上计算 · 工作区 {{ rowCount().toLocaleString() }} 行 × {{ d.columns.length }} 列 · v{{ d.meta?.version ?? 0 }}
            <span v-if="running" class="ml-1 opacity-70"><i class="fa-solid fa-spinner fa-spin mr-1"></i>构建中…</span>
          </template>
          <template v-else>后端不在线：本页的构建、重命名与撤销均为只读，浏览器不再算第二套</template>
        </div>
        <div class="mt-0.5 leading-snug opacity-80" v-if="state.backend.online">
          浏览器只持有列注册表与下方预览的当前页窗口；单次构建上限
          {{ limits.featureColsPerOp || '—' }} 列 · 独热单列取值上限 {{ limits.onehotLevels || '—' }} 个 ·
          滚动窗口上限 {{ limits.featureWindow || '—' }} 步
        </div>
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
          <div class="flex items-center gap-1 px-1.5 py-0.5 rounded bg-white border border-slate-200">
            <span class="w-1.5 h-1.5 rounded-full bg-indigo-400"></span>
            <span class="text-slate-500">时间</span>
            <span class="font-bold" :class="pipe.time ? 'font-mono text-emerald-600' : 'text-slate-400'">{{ pipe.time ? '✓ 完成' : '待执行' }}</span>
          </div>
          <div class="flex items-center gap-1 px-1.5 py-0.5 rounded bg-white border border-slate-200">
            <span class="w-1.5 h-1.5 rounded-full bg-sky-400"></span>
            <span class="text-slate-500">滞后</span>
            <span class="font-bold" :class="pipe.lag ? 'font-mono text-emerald-600' : 'text-slate-400'">{{ pipe.lag ? '✓ 完成' : '待执行' }}</span>
          </div>
          <div class="flex items-center gap-1 px-1.5 py-0.5 rounded bg-white border border-slate-200">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
            <span class="text-slate-500">差分</span>
            <span class="font-bold" :class="pipe.diff ? 'font-mono text-emerald-600' : 'text-slate-400'">{{ pipe.diff ? '✓ 完成' : '待执行' }}</span>
          </div>
          <div class="flex items-center gap-1 px-1.5 py-0.5 rounded bg-white border border-slate-200">
            <span class="w-1.5 h-1.5 rounded-full bg-violet-400"></span>
            <span class="text-slate-500">编码</span>
            <span class="font-bold" :class="pipe.cat ? 'font-mono text-emerald-600' : 'text-slate-400'">{{ pipe.cat ? '✓ 完成' : '待执行' }}</span>
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
            <div class="grid grid-cols-4 gap-x-4 gap-y-2 text-xs text-slate-600">
              <label v-for="opt in [['hour','小时 (Hour)'],['day','日期 (Day)'],['month','月份 (Month)'],['weekday','星期 (DayOfWeek, 周一=0)'],['is_weekend','周末判定 (0/1)'],['holiday','节假日编码']]"
                     :key="opt[0]"
                     class="flex items-center gap-1.5 px-2 py-1.5 rounded-lg hover:bg-indigo-50 border border-transparent hover:border-indigo-200 cursor-pointer transition-colors">
                <input type="checkbox" v-model="timeOpts[opt[0]]" class="accent-indigo-500" />
                <span>{{ opt[1] }}</span>
              </label>
            </div>
            <div class="mt-3 border border-slate-200 rounded-lg p-3 bg-slate-50/50">
              <label class="flex items-center gap-1.5 text-xs font-medium text-indigo-600 cursor-pointer">
                <input type="checkbox" v-model="timeOpts.cyclical_sincos" class="accent-indigo-500" />
                <span>对已勾选的周期维度做正余弦编码 (Sin / Cos)</span>
              </label>
              <p class="text-[10px] text-slate-400 mt-1.5 leading-relaxed">
                <template v-if="!timeOpts.cyclical_sincos">
                  这是上面维度的<b class="text-slate-500">编码方式</b>，不是新的日历维度：勾选后按各自周期长度把数值映射到单位圆，避免「23 点与 0 点相差很远」的断裂。
                </template>
                <template v-else-if="timePlan.cyc.length === 0">
                  小时 / 星期 / 月份才有固定周期，当前未勾选任何可编码的维度，生成时只会得到普通日历列。
                </template>
                <template v-else>
                  将对 <b class="text-slate-600">{{ timePlan.cyc.join('、') }}</b> 各生成 sin 与 cos 两列（共
                  <b class="text-indigo-600">{{ timePlan.sinCosCols }}</b> 列）<template v-if="timePlan.daySkipped">；日期 (Day) 因每月天数不固定不做周期编码</template>。
                </template>
              </p>
              <div class="mt-2 pt-2 border-t border-slate-200 text-[10px] text-slate-400">
                <div>本次将生成 <strong class="text-indigo-600 text-xs">{{ timePlan.total }}</strong> 列</div>
                <div class="font-mono text-slate-400 break-all leading-snug mt-0.5">{{ timePlan.keys.join('  ') || '（未勾选任何维度）' }}</div>
              </div>
            </div>
            <p class="text-[10px] text-slate-400 mt-2">节假日编码基于 2024 年中国法定节假日表（{{ limits.holidayDays || '—' }} 天，天数取自后端同一份表）。</p>
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
            <div class="flex-1 p-4 flex flex-col justify-between overflow-auto">
              <div>
                <h3 class="text-sm font-bold text-slate-800 flex items-center mb-3">
                  <i class="fa-solid fa-clock-rotate-left text-sky-600 mr-2"></i>滞后与滑动窗口特征
                </h3>
                <div class="grid grid-cols-3 gap-4 mb-4">
                  <div>
                    <label class="text-[11px] text-slate-500 block mb-1">滞后阶数 (逗号分隔)</label>
                    <input type="text" v-model="lagSteps" class="w-full border border-slate-200 rounded-lg px-2.5 py-1.5 text-xs font-mono bg-white" />
                    <span class="text-[10px] text-slate-400">96步 = 15min采样下前1天</span>
                  </div>
                  <div>
                    <label class="text-[11px] text-slate-500 block mb-1">滚动窗口大小 (逗号分隔)</label>
                    <input type="text" v-model="rollWindows" class="w-full border border-slate-200 rounded-lg px-2.5 py-1.5 text-xs font-mono bg-white" />
                    <span class="text-[10px] text-slate-400">4=1h · 16=4h · 96=1天</span>
                  </div>
                  <div>
                    <span class="text-[11px] text-slate-500 block mb-1">高级窗口 (Advanced)</span>
                    <div class="space-y-1 text-[11px] text-slate-600 mt-1">
                      <label class="flex items-center gap-1.5">
                        <input type="checkbox" v-model="rollExpanding" class="accent-sky-500" /> 扩展窗口均值 (Expanding)
                      </label>
                      <label class="flex items-center gap-1.5">
                        <input type="checkbox" v-model="rollEwm" class="accent-sky-500" /> 指数加权移动平均 (EWM)
                      </label>
                      <div class="flex items-center gap-1.5 pl-6" :class="rollEwm ? '' : 'opacity-40'">
                        <span class="text-[10px] text-slate-400 shrink-0">span</span>
                        <input type="number" v-model.number="ewmSpan" :disabled="!rollEwm" min="2" max="500" step="1"
                               class="w-16 border border-slate-200 rounded px-1.5 py-0.5 text-[11px] font-mono bg-white disabled:bg-slate-50" />
                        <span class="text-[10px] text-slate-400">α = 2/(span+1) = {{ (2 / (normEwmSpan(ewmSpan) + 1)).toFixed(3) }}</span>
                      </div>
                    </div>
                  </div>
                </div>
                <div class="border border-slate-200 rounded-lg p-3 bg-slate-50/50">
                  <span class="text-[11px] text-slate-500 font-semibold block mb-2">滚动统计量 (Rolling Aggregations)</span>
                  <div class="flex flex-wrap gap-3 text-[11px] text-slate-600">
                    <label v-for="s in ROLL_STAT_OPTS"
                           :key="s[0]"
                           class="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-white border border-slate-200 hover:border-sky-300 cursor-pointer transition-colors">
                      <input type="checkbox" v-model="rollStats[s[0]]" class="accent-sky-500" /> {{ s[1] }}
                    </label>
                  </div>
                </div>
                <div class="mt-3 border border-slate-200 rounded-lg p-3 bg-white">
                  <div class="text-[10px] text-slate-400 leading-relaxed mb-2">
                    窗口类特征一律不含当前行（等价 pandas <span class="font-mono">.shift(1)</span>），防止用 t 时刻的值泄漏预测 t 时刻的标签。
                    Expanding 为历史累计均值，EWM 半衰期由 span 控制。
                  </div>
                  <div class="flex items-center justify-between gap-2">
                    <span class="text-[10px] text-slate-500 shrink-0">
                      本次将写入 <strong class="text-sky-600 text-xs">{{ lagPlan.length }}</strong> 列
                      <span v-if="lagPlan.length === 0" class="text-slate-400">（未勾选任何有效项）</span>
                    </span>
                    <button v-if="lagPlan.length > 0" @click="lagPlanOpen = !lagPlanOpen"
                            class="text-[10px] text-sky-600 hover:underline shrink-0">
                      {{ lagPlanOpen ? '收起列名' : '查看列名' }}
                    </button>
                  </div>
                  <div v-if="lagPlanOpen && lagPlan.length > 0"
                       class="mt-2 pt-2 border-t border-slate-100 font-mono text-[10px] text-slate-400 break-all leading-snug max-h-24 overflow-auto">
                    {{ lagPlan.join('  ') }}
                  </div>
                </div>
              </div>
              <div class="flex justify-end mt-3">
                <button @click="runLagFeatures"
                        class="px-8 py-2.5 bg-sky-600 hover:bg-sky-700 text-white rounded-xl text-xs font-bold shadow-md hover:shadow-lg transition-all flex items-center gap-2">
                  <i class="fa-solid fa-play text-[10px]"></i>生成滞后与窗口特征
                </button>
              </div>
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
            <div class="flex-1 p-4 flex flex-col justify-between overflow-auto">
              <div>
                <h3 class="text-sm font-bold text-slate-800 flex items-center mb-3">
                  <i class="fa-solid fa-wave-square text-emerald-600 mr-2"></i>差分平稳化与频域特征
                </h3>
                <div class="grid grid-cols-2 gap-5">
                  <div class="border border-slate-200 rounded-lg p-3 bg-slate-50/50">
                    <span class="text-[11px] text-slate-500 font-semibold block mb-2 flex items-center gap-1">
                      <i class="fa-solid fa-minus text-[9px] text-emerald-500"></i>差分平稳化 (Differencing)
                    </span>
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
                  </div>
                  <div class="border border-slate-200 rounded-lg p-3 bg-slate-50/50">
                    <span class="text-[11px] text-slate-500 font-semibold block mb-2 flex items-center gap-1">
                      <i class="fa-solid fa-wave-square text-[9px] text-emerald-500"></i>频域特征 (FFT 频谱分析)
                    </span>
                    <div class="space-y-2 text-[11px] text-slate-600">
                      <label class="flex items-center gap-2 px-2 py-1.5 rounded-md bg-white border border-slate-200 hover:border-emerald-300 cursor-pointer transition-colors">
                        <input type="checkbox" v-model="fftDominant" class="accent-emerald-500" />
                        <span>主导频率能量 (Top-3)</span>
                        <span class="text-[10px] text-slate-400 ml-auto">DFT峰值</span>
                      </label>
                      <label class="flex items-center gap-2 px-2 py-1.5 rounded-md bg-white border border-slate-200 hover:border-emerald-300 cursor-pointer transition-colors">
                        <input type="checkbox" v-model="fftEntropy" class="accent-emerald-500" />
                        <span>频域谱熵 (Spectral Entropy)</span>
                        <span class="text-[10px] text-slate-400 ml-auto">复杂度指标</span>
                      </label>
                      <label class="flex items-center gap-2 px-2 py-1.5 rounded-md bg-white border border-slate-200 hover:border-emerald-300 cursor-pointer transition-colors">
                        <input type="checkbox" v-model="fftPowerRatio" class="accent-emerald-500" />
                        <span>频带能量比 (Power Ratio)</span>
                        <span class="text-[10px] text-slate-400 ml-auto">低频/高频</span>
                      </label>
                    </div>
                    <p class="text-[10px] text-slate-400 mt-2">频域特征基于 DFT 在 64 点滑动窗口上逐点计算，仅对首个勾选列执行。</p>
                  </div>
                </div>
                <div class="mt-3 border border-slate-200 rounded-lg p-3 bg-white">
                  <div class="flex items-center justify-between gap-2">
                    <span class="text-[10px] text-slate-500 shrink-0">
                      本次将写入 <strong class="text-emerald-600 text-xs">{{ diffPlan.length }}</strong> 列
                      <span v-if="diffPlan.length === 0" class="text-slate-400">（未勾选任何有效项）</span>
                    </span>
                    <button v-if="diffPlan.length > 0" @click="diffPlanOpen = !diffPlanOpen"
                            class="text-[10px] text-emerald-600 hover:underline shrink-0">
                      {{ diffPlanOpen ? '收起列名' : '查看列名' }}
                    </button>
                  </div>
                  <div v-if="diffPlanOpen && diffPlan.length > 0"
                       class="mt-2 pt-2 border-t border-slate-100 font-mono text-[10px] text-slate-400 break-all leading-snug max-h-24 overflow-auto">
                    {{ diffPlan.join('  ') }}
                  </div>
                </div>
              </div>
              <div class="flex justify-end mt-3">
                <button @click="runDiffFeatures"
                        class="px-8 py-2.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl text-xs font-bold shadow-md hover:shadow-lg transition-all flex items-center gap-2">
                  <i class="fa-solid fa-play text-[10px]"></i>生成差分与频域特征
                </button>
              </div>
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
                    <p class="text-[10px] text-slate-400 mt-1.5">显示所选列的类别数、编码后预计新增列数（均由后端整表数出，与「执行编码」的产物列数同源）</p>
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

    <!-- 底部步骤切换 -->
    <div class="flex justify-between items-center shrink-0">
      <button @click="switchStep(4)" class="px-4 py-1.5 bg-slate-200 hover:bg-slate-300 text-slate-700 rounded-lg text-xs font-medium">
        <i class="fa-solid fa-arrow-left mr-1"></i>返回质量清洗
      </button>
      <div class="flex gap-2">
        <button @click="state.showCodeModal = true"
                class="px-4 py-2 bg-slate-100 hover:bg-slate-200 text-slate-700 border border-slate-300 rounded-lg text-xs font-semibold flex items-center">
          <i class="fa-brands fa-python text-indigo-600 mr-1.5 text-sm"></i>查看生成特征的 Python Pipeline
        </button>
        <button @click="state.showExportModal = true"
                class="px-5 py-2 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg text-xs font-semibold shadow flex items-center">
          <i class="fa-solid fa-file-export mr-1.5"></i>完成并打包导出数据集
        </button>
      </div>
    </div>
  </section>
</template>
