<script setup>
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { ElMessageBox } from 'element-plus'
import {
  state, ds, touch, switchStep, toast, logAction, rowCount,
  LOG_STEP_META, LOG_ICONS, exportActionLog, importActionLog,
  replayActionLog, clearImportedLog, generatePythonCode, visibleActionLog,
  undo, redo, initSession, restoreSession, clearSession
} from './store'
import { checkBackend, wsExport, wsSaveAs, API_BASE, IS_DESKTOP } from './api'
import Step1Load from './components/Step1Load.vue'
import Step2Config from './components/Step2Config.vue'
import Step3Explore from './components/Step3Explore.vue'
import Step4Quality from './components/Step4Quality.vue'
import Step5Features from './components/Step5Features.vue'

const STEPS = [
  { n: 1, label: '数据加载' },
  { n: 2, label: '数据接入与配置' },
  { n: 3, label: '数据探索分析' },
  { n: 4, label: '质量诊断与清洗' },
  { n: 5, label: '特征构建工程' }
]

const stepComponents = { 1: Step1Load, 2: Step2Config, 3: Step3Explore, 4: Step4Quality, 5: Step5Features }
const currentComponent = computed(() => stepComponents[state.currentStep])

// 依赖 dataVersion 使大数据变化后表头指标刷新
const dsName = computed(() => { void state.dataVersion; return ds()?.name || '未加载数据' })
// 整表行数来自后端 meta，不依赖浏览器是否缓存了明细
const dsTag = computed(() => {
  void state.dataVersion
  const d = ds()
  if (!d?.wsId) return '未加载数据'
  return `${d.name} · 整表 ${rowCount().toLocaleString()} 行 × ${d.columns.length} 列 · 工作区 ${d.wsId} v${d.meta?.version ?? 0}`
})
const logCount = computed(() => visibleActionLog().length)
const hasWorkspace = computed(() => { void state.dataVersion; return !!ds()?.wsId })

// ---- 操作记录抽屉 ----
const filters = [
  { key: 'all', label: '全部' },
  { key: '1', label: '① 加载' },
  { key: '2', label: '② 配置' },
  { key: '3', label: '③ 探索' },
  { key: '4', label: '④ 清洗' },
  { key: '5', label: '⑤ 特征' }
]
// 审计链是服务端命令日志在界面上的投影：撤销掉的条目（游标之后）不该继续显示成"当前数据的历史"
const groupedLog = computed(() => {
  void state.dataVersion
  const all = visibleActionLog()
  const filtered = state.logFilter === 'all'
    ? all
    : all.filter(a => String(a.step) === state.logFilter)
  const groups = {}
  filtered.forEach(e => { (groups[e.step] = groups[e.step] || []).push(e) })
  return Object.keys(groups).map(Number).sort((a, b) => a - b).map(n => ({ n, meta: LOG_STEP_META[n], items: groups[n] }))
})
function iconOf(name) { return LOG_ICONS[name] || 'fa-circle-dot' }

function setFilter(k) { state.logFilter = k }

async function clearLog() {
  const shown = visibleActionLog().length
  if (shown === 0) return
  try {
    await ElMessageBox.confirm(`确定清空界面上的 ${shown} 条操作记录？服务端工作区的命令日志与撤销栈不受影响`, '清空确认', { type: 'warning' })
    state.actionLog = []
    clearImportedLog()
    // 不 touch 的话抽屉不会刷新（列表依赖 dataVersion），会话也不会跟着瘦身
    touch()
  } catch (e) { /* 取消 */ }
}

function onImportFile(ev) {
  const file = ev.target.files && ev.target.files[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = (e) => { importActionLog(String(e.target.result)) }
  reader.onerror = () => toast('error', '文件读取失败')
  reader.readAsText(file)
  ev.target.value = ''
}

// ---- 代码模态 ----
const codeContent = computed(() => state.showCodeModal ? generatePythonCode() : '')
function copyCode() {
  navigator.clipboard?.writeText(codeContent.value)
    .then(() => toast('success', '代码已复制到剪贴板'))
    .catch(() => toast('warning', '浏览器拒绝了剪贴板访问，请手动选中复制'))
}

// ---- 后端连接状态 ----
const backendTitle = computed(() => (state.backend.online
  ? `FastAPI 后端在线：${API_BASE} · 能力 ${state.backend.capabilities.join(' / ')} · 最近探测 ${state.backend.checkedAt}（点击重新探测）`
  : `后端未连接（${API_BASE}）：${state.backend.error || '未知原因'} · 整表计算、数据加工与四种格式的宽表导出都在服务端，工作台此时只读（点击重试）`)
  + ` · 会话：${sessionLine.value}`
  + (IS_DESKTOP ? '\n桌面端托管：后端优先跑打包版 exe（build-backend.bat 的产物），没有 exe 才回落到 python -m uvicorn app.main:app；应用退出时一并回收，外部已起的后端则复用、退出时不动它。' : ''))
// 会话存在服务端（PUT /api/session），这里这一句必须说清它此刻到底在不在
const sessionLine = computed(() => {
  if (!state.session.enabled) return `未启用（${state.session.error || '后端未连接'}）`
  if (state.session.saving) return '写入中…'
  if (state.session.error) return `上次写入被服务端拒绝：${state.session.error}`
  return state.session.savedAt
    ? `已写入 ${state.session.stateDir || '服务端状态目录'} · ${state.session.savedAt}`
    : '尚无写入（做过一步操作后自动保存）'
})

// ---- 撤销 / 重做：按纽上的计数就是服务端命令日志的游标 ----
const canUndo = computed(() => state.history.canUndo)
const canRedo = computed(() => state.history.canRedo)
const undoTitle = computed(() => canUndo.value
  ? `撤销「${state.history.undoLabel}」（Ctrl+Z）：让后端工作区回到第 ${state.history.version - 1} 版（按命令日志重放），` +
    `重做栈还有 ${state.history.redo} 步`
  : '后端工作区已在第 0 版（原始数据），没有可撤销的变更')
const redoTitle = computed(() => canRedo.value
  ? `重做「${state.history.redoLabel}」（Ctrl+Y）：后端工作区回到第 ${state.history.version + 1} 版，共 ${state.history.opsTotal} 条命令日志`
  : '没有可重做的变更')

// 输入框里的 Ctrl+Z 是原生文字撤销，不能被工作区撤销抢走
// 撤销/重做要向后端回滚工作区帧，是网络操作，必须防连点
const histBusy = ref(false)
async function runUndo() {
  if (histBusy.value) return
  histBusy.value = true
  try { await undo() } finally { histBusy.value = false }
}
async function runRedo() {
  if (histBusy.value) return
  histBusy.value = true
  try { await redo() } finally { histBusy.value = false }
}
async function runRestoreSession() {
  if (histBusy.value) return
  histBusy.value = true
  try { await restoreSession() } finally { histBusy.value = false }
}
async function runClearSession() {
  if (histBusy.value) return
  histBusy.value = true
  try { await clearSession() } finally { histBusy.value = false }
}

function onKeydown(ev) {
  if (!(ev.ctrlKey || ev.metaKey) || ev.altKey) return
  const t = ev.target
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
  const k = ev.key.toLowerCase()
  if (k === 'z' && !ev.shiftKey) { ev.preventDefault(); runUndo() }
  else if (k === 'y' || (k === 'z' && ev.shiftKey)) { ev.preventDefault(); runRedo() }
}

// ---- 本地会话恢复 ----
const sessionWhen = computed(() => {
  const t = new Date(state.session.found?.savedAt || '')
  return isNaN(t.getTime()) ? '未知时间' : t.toLocaleString()
})

// ---- 导出结果模态 ----
// 宽表直出：明细留在后端工作区，浏览器只触发下载并记下真实字节数（不再有整表物化这一层）
const EXPORT_FORMATS = { csv: 'CSV', xlsx: 'EXCEL', parquet: 'PARQUET', feather: 'FEATHER' }
const exportCols = computed(() => { void state.dataVersion; return ds()?.columns?.length || 0 })
const exporting = ref('')

// 后端逐个格式真编一次探测出来的结果（null = 编得出来，字符串 = 缺依赖之类的理由）。
// 「后端可用」只能读这个，不能读「前端有这条分支」——本机没装 pyarrow 时 parquet 会 422。
const codecReason = fmt => state.backend.online ? state.backend.exportCodecs[fmt] : '后端未连接'
const canEncode = fmt => state.backend.online && state.backend.exportCodecs[fmt] === null
const codecBadge = fmt => exporting.value === fmt ? '编码中…' : (canEncode(fmt) ? '后端可用' : '后端编不出')
const codecTitle = fmt => canEncode(fmt) ? '后端已用一行的表真编过一次这个格式' : String(codecReason(fmt)).slice(0, 160)
const saveFormats = computed(() => Object.fromEntries(
  Object.entries(EXPORT_FORMATS).filter(([fmt]) => canEncode(fmt))))
const missingFormats = computed(() => {
  if (!state.backend.online) return []
  return Object.entries(EXPORT_FORMATS)
    .filter(([fmt]) => !canEncode(fmt))
    .map(([fmt]) => ({ label: EXPORT_FORMATS[fmt], reason: String(codecReason(fmt)) }))
})

async function doExport(kind) {
  if (kind === 'py') { state.showCodeModal = true; state.showExportModal = false; return }
  if (!EXPORT_FORMATS[kind]) return
  if (exporting.value) return
  // 离线时四个格式入口要么 disabled 要么换成「后端未连接」那块占位，走不到这里
  if (!state.backend.online) return
  if (!canEncode(kind)) {
    toast('warning', `后端编不出 ${EXPORT_FORMATS[kind]}：${codecReason(kind)} · 换格式或补依赖（parquet/feather 要 pyarrow，xlsx 要 openpyxl）`)
    return
  }
  const d = ds()
  if (!d?.wsId) { toast('warning', '还没有载入数据集'); return }
  exporting.value = kind
  try {
    const rows = rowCount()
    const r = await wsExport(d.wsId, kind)
    toast('success', `导出完成：${r.name} · ${(r.bytes / 1024).toFixed(1)} KB（服务端直出）`)
    logAction(5, 'dataset', `导出 ${EXPORT_FORMATS[kind]} 宽表`,
      `${rows.toLocaleString()} 行 × ${exportCols.value} 列 · ${(r.bytes / 1024).toFixed(1)} KB · 后端工作区直出`,
      { type: 'export', format: kind, bytes: r.bytes, rows, cols: exportCols.value })
    state.showExportModal = false
  } catch (e) {
    toast('error', `导出失败：${e.message}`)
  } finally {
    exporting.value = ''
  }
}

// ---- 另存为数据集：同一条编码路径，目的地换成服务端的数据集目录 ----
// 与上面的 doExport 共用 ws.export_dataframe()：存进目录的字节与浏览器下载到的字节同源，
// 区别只是明细不绕网络 —— 第七步想拿这份结果当新数据的起点，回第一步点开就行。
const saveName = ref('')
const saveFmt = ref('csv')
const saving = ref(false)
// 存成功那句话要在模态里留着：toast 三秒就走，人还在这个框里，需要就地看到落到盘上的那个文件名
const savedOk = ref('')
watch(() => state.showExportModal, v => { if (v) savedOk.value = '' })
// 只让用户选后端真编得出来的格式；默认值编不出来就换到第一个能用的
watch(saveFormats, m => {
  const keys = Object.keys(m)
  if (keys.length && !keys.includes(saveFmt.value)) saveFmt.value = keys[0]
}, { immediate: true })

async function doSaveAsDataset() {
  if (saving.value) return
  if (!state.backend.online) return
  const d = ds()
  if (!d?.wsId) { toast('warning', '还没有载入数据集'); return }
  if (!canEncode(saveFmt.value)) return
  saving.value = true
  try {
    const r = await wsSaveAs(d.wsId, saveFmt.value, saveName.value.trim())
    savedOk.value = `${r.filename} · ${r.sizeText} · ${r.rows.toLocaleString()} 行 × ${r.cols} 列` +
      (r.renamed ? `（${r.proposed} 已存在，另存为新名）` : '')
    // 不再另弹一条 toast：模态里那句 savedOk 就是同一批数字，而且它不会三秒就消失
    logAction(5, 'dataset', `另存为数据集（${EXPORT_FORMATS[saveFmt.value]}）`,
      `${r.filename} · ${r.rows.toLocaleString()} 行 × ${r.cols} 列 · ${r.sizeText}` +
      (r.renamed ? ` · 同名已存在，原名 ${r.proposed} 另存` : ''),
      { type: 'save_as_dataset', format: r.format, filename: r.filename,
        rows: r.rows, cols: r.cols, bytes: r.size, version: r.version })
  } catch (e) {
    savedOk.value = ''
    toast('error', `另存为数据集失败：${e.message}`)
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  // 先探活再查会话：会话存在服务端，离线时无从查询（initSession 内部也会自己补一次探测）
  await checkBackend()
  initSession()
})
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <header class="bg-white border-b border-slate-200 px-6 py-2.5 flex justify-between items-center gap-4 z-20 shrink-0 shadow-sm">
    <div class="flex items-center space-x-3">
      <div class="w-8 h-8 rounded-lg bg-indigo-600 flex items-center justify-center text-white font-bold text-base shadow">
        <i class="fa-solid fa-chart-line"></i>
      </div>
      <div>
        <h1 class="font-bold text-base text-slate-800 leading-tight">TimeSeries Studio</h1>
        <p class="text-xs text-slate-400">新能源与电力负荷时序数据工程平台</p>
      </div>
    </div>

    <nav class="flex items-center space-x-1.5">
      <template v-for="(s, i) in STEPS" :key="s.n">
        <button @click="switchStep(s.n)"
                class="step-btn px-3 py-1.5 rounded-full text-xs font-medium transition-all flex items-center space-x-1.5"
                :class="state.currentStep === s.n
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : (state.currentStep > s.n ? 'text-indigo-600 bg-indigo-50 hover:bg-indigo-100' : 'text-slate-500 hover:bg-slate-200')">
          <span class="w-4 h-4 rounded-full text-[11px] font-bold flex items-center justify-center"
                :class="state.currentStep === s.n ? 'bg-white text-indigo-600' : (state.currentStep > s.n ? 'bg-indigo-200 text-indigo-700' : 'bg-slate-300 text-slate-600')">{{ s.n }}</span>
          <span>{{ s.label }}</span>
        </button>
        <i v-if="i < STEPS.length - 1" class="fa-solid fa-angle-right text-slate-300 text-xs"></i>
      </template>
    </nav>

    <div class="flex items-center space-x-3 text-xs">
      <button @click="checkBackend()" :title="backendTitle"
              class="px-2.5 py-1 rounded-md border flex items-center gap-1.5 transition-colors">
        <span class="w-1.5 h-1.5 rounded-full shrink-0"
              :class="state.backend.online ? 'bg-emerald-500' : (state.backend.checking ? 'bg-amber-400 animate-pulse' : 'bg-slate-400')"></span>
        <i class="fa-solid fa-server text-[10px]" :class="state.backend.online ? 'text-emerald-600' : 'text-slate-400'"></i>
        <span :class="state.backend.online ? 'text-emerald-700' : 'text-slate-500'">{{ state.backend.online ? '后端 v' + state.backend.version : '后端离线' }}</span>
      </button>
      <span class="bg-emerald-50 text-emerald-700 border border-emerald-200 px-2.5 py-1 rounded-md flex items-center gap-1.5 max-w-[300px]" :title="dsTag">
        <span class="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse shrink-0"></span>
        <span class="truncate">{{ dsName }}</span>
        <span v-if="hasWorkspace" class="font-mono text-[10px] text-emerald-600 shrink-0">{{ rowCount().toLocaleString() }} 行·后端</span>
      </span>
      <div class="flex items-center gap-1">
        <button @click="runUndo()" :disabled="!canUndo || histBusy" :title="undoTitle"
                class="px-2 py-1 rounded-md border border-slate-200 bg-white text-slate-600 hover:text-indigo-600 hover:border-indigo-300 disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex items-center gap-1">
          <i class="fa-solid text-[10px]" :class="histBusy ? 'fa-spinner fa-spin' : 'fa-rotate-left'"></i>
          <span>撤销</span>
          <span v-if="state.history.undo > 0" class="min-w-[16px] h-4 px-1 rounded-full bg-slate-100 text-slate-500 text-[10px] font-bold flex items-center justify-center">{{ state.history.undo }}</span>
        </button>
        <button @click="runRedo()" :disabled="!canRedo || histBusy" :title="redoTitle"
                class="px-2 py-1 rounded-md border border-slate-200 bg-white text-slate-600 hover:text-indigo-600 hover:border-indigo-300 disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex items-center gap-1">
          <i class="fa-solid fa-rotate-right text-[10px]"></i>
          <span>重做</span>
          <span v-if="state.history.redo > 0" class="min-w-[16px] h-4 px-1 rounded-full bg-slate-100 text-slate-500 text-[10px] font-bold flex items-center justify-center">{{ state.history.redo }}</span>
        </button>
      </div>
      <button @click="state.showExportModal = true"
              class="px-3 py-1.5 bg-slate-800 text-white rounded-md hover:bg-slate-700 text-xs font-medium transition-colors">
        <i class="fa-solid fa-download mr-1"></i>导出处理结果
      </button>
    </div>
  </header>

  <!-- 服务端会话恢复提示：GET /api/session 会当场按命令日志把工作区重建/核对一遍，这里只询问，不擅自替换当前工作区 -->
  <div v-if="state.session.found" class="bg-amber-50 border-b border-amber-200 px-6 py-2 flex items-center gap-2.5 text-xs shrink-0 z-20">
    <i class="fa-solid fa-clock-rotate-left text-amber-600"></i>
    <span class="text-amber-800 min-w-0">
      服务端记着上次会话：<b class="font-semibold">{{ state.session.found.name }}</b>
      · {{ (state.session.found.rows || 0).toLocaleString() }} 行 × {{ state.session.found.cols }} 列
      · {{ state.session.found.ops }} 条操作 · 保存于 {{ sessionWhen }}
      <span v-if="state.session.found.wsId" class="text-amber-700">
        （工作区 {{ state.session.found.wsId }} 此刻在第 {{ state.session.found.version }} 版<template v-if="!state.session.found.alive">，但服务端已没有它的命令日志：恢复后需回第一步重新载入</template>）
      </span>
      <span v-if="state.session.found.drift" class="text-rose-600">
        （会话记的是第 {{ state.session.found.drift.sessionSays }} 版，服务端日志已走到第 {{ state.session.found.drift.serverSays }} 版 —— 以服务端为准）
      </span>
      <span v-if="state.session.found.alive" class="text-amber-600">（刷新与后端重启都还在；异常检测结果会随回退失效）</span>
    </span>
    <div class="ml-auto flex items-center gap-2 shrink-0">
      <button @click="runRestoreSession()" :disabled="histBusy"
              class="px-2.5 py-1 bg-amber-600 text-white rounded hover:bg-amber-700 font-medium transition-colors disabled:opacity-50">
        <i class="fa-solid text-[10px] mr-1" :class="histBusy ? 'fa-spinner fa-spin' : 'fa-arrow-rotate-left'"></i>恢复会话
      </button>
      <button @click="runClearSession()"
              class="px-2.5 py-1 bg-white border border-amber-300 text-amber-700 rounded hover:bg-amber-100 transition-colors">
        忽略并清除
      </button>
    </div>
  </div>

  <main class="flex-1 min-h-0 overflow-hidden relative">
    <KeepAlive>
      <component :is="currentComponent" :key="state.currentStep" />
    </KeepAlive>

    <!-- 右侧收缩把手 -->
    <button @click="state.drawerOpen = !state.drawerOpen"
            class="absolute top-1/2 -translate-y-1/2 right-0 z-30 flex flex-col items-center gap-1.5 bg-white border border-slate-200 border-r-0 rounded-l-xl shadow-lg py-3 px-1.5 hover:bg-indigo-50 transition-all group"
            :style="{ opacity: state.drawerOpen ? 0.3 : 1 }"
            title="数据操作记录">
      <i class="fa-solid fa-list-check text-slate-500 group-hover:text-indigo-600 text-sm"></i>
      <span class="text-[9px] font-bold text-slate-500 group-hover:text-indigo-600 writing-vertical tracking-wider">操 作 记 录</span>
      <span v-if="logCount > 0"
            class="min-w-[18px] h-[18px] rounded-full bg-indigo-600 text-white text-[10px] font-bold flex items-center justify-center px-1 mt-0.5">
        {{ logCount > 99 ? '99+' : logCount }}
      </span>
    </button>

    <!-- 操作记录抽屉 -->
    <aside class="absolute top-0 right-0 bottom-0 w-[380px] bg-white border-l border-slate-200 shadow-2xl z-40 flex flex-col transform transition-transform duration-300 ease-out"
           :class="state.drawerOpen ? 'translate-x-0' : 'translate-x-full'">
      <div class="px-4 py-3 border-b border-slate-200 bg-slate-50">
        <div class="flex items-center justify-between mb-2">
          <div class="flex items-center gap-2">
            <i class="fa-solid fa-list-check text-indigo-600"></i>
            <h3 class="text-sm font-bold text-slate-800">数据操作记录</h3>
          </div>
          <button @click="state.drawerOpen = false" title="收起"
                  class="w-7 h-7 flex items-center justify-center text-slate-400 hover:text-slate-700 hover:bg-slate-200 rounded">
            <i class="fa-solid fa-xmark text-xs"></i>
          </button>
        </div>
        <div class="flex items-center gap-1.5">
          <button @click="exportActionLog()"
                  class="flex-1 px-2 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-md text-[11px] font-semibold flex items-center justify-center gap-1 transition-colors">
            <i class="fa-solid fa-download text-[9px]"></i>导出流程
          </button>
          <label class="flex-1 px-2 py-1.5 bg-white border border-slate-200 hover:border-indigo-400 hover:bg-indigo-50 text-slate-600 rounded-md text-[11px] font-semibold flex items-center justify-center gap-1 transition-colors cursor-pointer">
            <i class="fa-solid fa-upload text-[9px]"></i>导入流程
            <input type="file" accept=".json" class="hidden" @change="onImportFile" />
          </label>
          <button @click="replayActionLog()" :disabled="!state.importedLog"
                  class="flex-1 px-2 py-1.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-md text-[11px] font-semibold flex items-center justify-center gap-1 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">
            <i class="fa-solid fa-play text-[9px]"></i>回放执行
          </button>
          <div class="w-px h-5 bg-slate-200 mx-0.5"></div>
          <button @click="clearLog()" title="清空"
                  class="w-7 h-7 flex items-center justify-center text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded">
            <i class="fa-solid fa-trash-can text-xs"></i>
          </button>
        </div>
      </div>

      <div class="px-3 py-2 border-b border-slate-100 flex items-center gap-1.5 bg-white overflow-x-auto">
        <button v-for="f in filters" :key="f.key" @click="setFilter(f.key)"
                class="px-2 py-0.5 text-[11px] rounded-full font-medium shrink-0 transition-colors"
                :class="state.logFilter === f.key ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'">
          {{ f.label }}
        </button>
      </div>

      <div v-if="state.replayStatus" class="px-3 py-2 bg-emerald-50 border-b border-emerald-200 flex items-center gap-2">
        <div class="w-3 h-3 border-2 border-emerald-500 border-t-transparent rounded-full animate-spin"></div>
        <span class="text-[11px] font-medium text-emerald-700">{{ state.replayStatus.text }}</span>
      </div>

      <div class="flex-1 overflow-y-auto px-3 py-3 space-y-2">
        <div v-if="logCount === 0" class="text-center text-slate-400 text-[11px] py-8">
          <i class="fa-regular fa-circle-check text-2xl block mb-2 opacity-40"></i>
          还没有任何数据操作记录
        </div>
        <template v-else>
          <div v-for="g in groupedLog" :key="g.n" class="mb-3">
            <div class="flex items-center gap-1.5 mb-1.5 sticky top-0 bg-white/95 backdrop-blur py-1 z-10">
              <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold text-white" :class="g.meta.color">
                {{ g.meta.short }}
              </span>
              <span class="text-[10px] text-slate-400">· {{ g.items.length }} 条</span>
            </div>
            <div class="relative pl-3 border-l-2 border-dashed border-slate-200 ml-[7px]">
              <div v-for="item in g.items" :key="item.id" class="relative mb-2 last:mb-0 group">
                <span class="absolute -left-[19px] top-1 w-3 h-3 rounded-full ring-2 ring-white border-2 border-white" :class="g.meta.color"></span>
                <div class="bg-slate-50 hover:bg-indigo-50/60 border border-slate-200 group-hover:border-indigo-300 rounded-lg px-2.5 py-1.5 transition-colors cursor-pointer">
                  <div class="flex items-center justify-between gap-2">
                    <div class="flex items-center gap-1.5 min-w-0">
                      <i class="fa-solid text-[10px] shrink-0" :class="[iconOf(item.icon), g.meta.text]"></i>
                      <span class="text-[11px] font-semibold text-slate-800 truncate">{{ item.title }}</span>
                    </div>
                    <span class="text-[9px] font-mono text-slate-400 shrink-0">{{ item.time }}</span>
                  </div>
                  <div v-if="item.detail" class="text-[10px] text-slate-500 mt-0.5 leading-snug font-mono break-all">{{ item.detail }}</div>
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>
    </aside>
  </main>

  <!-- Python 代码模态 -->
  <div v-if="state.showCodeModal" class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center" @click.self="state.showCodeModal = false">
    <div class="bg-white rounded-xl shadow-2xl w-[680px] max-h-[85vh] flex flex-col overflow-hidden">
      <div class="px-4 py-3 bg-slate-900 text-white flex justify-between items-center">
        <span class="text-xs font-bold font-mono flex items-center">
          <i class="fa-brands fa-python text-amber-400 mr-2"></i>timeseries_pipeline_generated.py
        </span>
        <button @click="state.showCodeModal = false" class="text-slate-400 hover:text-white text-sm">&times;</button>
      </div>
      <pre class="p-4 bg-slate-950 text-emerald-400 font-mono text-xs overflow-auto flex-1 leading-relaxed whitespace-pre">{{ codeContent }}</pre>
      <div class="p-3 bg-slate-100 border-t border-slate-200 flex justify-between">
        <span class="text-[11px] text-slate-500 self-center"
              title="这份记录来自服务端命令日志的投影：撤销掉的条目会跟着游标一起隐去，重做后再回来">仅本次会话真实执行的 {{ logCount }} 条</span>
        <div class="flex gap-2">
          <button @click="copyCode()" class="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded text-xs font-medium">复制代码</button>
          <button @click="state.showCodeModal = false" class="px-4 py-1.5 bg-slate-800 text-white rounded text-xs font-medium">关闭</button>
        </div>
      </div>
    </div>
  </div>

  <!-- 导出结果模态 -->
  <div v-if="state.showExportModal" class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center" @click.self="state.showExportModal = false">
    <div class="bg-white rounded-xl shadow-2xl w-[520px] overflow-hidden">
      <div class="px-5 py-3.5 border-b border-slate-200 flex items-center justify-between">
        <h3 class="text-sm font-bold text-slate-800"><i class="fa-solid fa-box-archive text-indigo-600 mr-2"></i>导出处理结果</h3>
        <button @click="state.showExportModal = false" class="text-slate-400 hover:text-slate-700"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="p-5 space-y-2.5">
        <div class="text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 font-mono leading-relaxed">
          {{ dsName }} · 后端工作区 {{ rowCount().toLocaleString() }} 行 × {{ exportCols }} 列
        </div>
        <button @click="doExport('csv')" :disabled="!state.backend.online || !!exporting"
                class="w-full flex items-center gap-3 p-3 rounded-lg border border-slate-200 hover:border-indigo-400 hover:bg-indigo-50/40 disabled:opacity-60 text-left transition-all">
          <span class="w-9 h-9 rounded-lg bg-emerald-50 flex items-center justify-center shrink-0"><i class="fa-solid fa-file-csv text-emerald-600"></i></span>
          <span class="flex-1 min-w-0">
            <span class="block text-xs font-bold text-slate-700">CSV 宽表 (.csv)</span>
            <span class="block text-[11px] text-slate-400">含清洗结果与特征列 · UTF-8 BOM，Excel 双击不乱码</span>
          </span>
          <i class="fa-solid fa-chevron-right text-slate-300 text-xs"></i>
        </button>
        <button @click="doExport('xlsx')" :disabled="!state.backend.online || !!exporting"
                class="w-full flex items-center gap-3 p-3 rounded-lg border border-slate-200 hover:border-indigo-400 hover:bg-indigo-50/40 disabled:opacity-60 text-left transition-all">
          <span class="w-9 h-9 rounded-lg bg-green-50 flex items-center justify-center shrink-0"><i class="fa-solid fa-file-excel text-green-600"></i></span>
          <span class="flex-1 min-w-0">
            <span class="block text-xs font-bold text-slate-700">Excel 工作簿 (.xlsx)</span>
            <span class="block text-[11px] text-slate-400">单表全量数据</span>
          </span>
          <i class="fa-solid fa-chevron-right text-slate-300 text-xs"></i>
        </button>
        <button @click="doExport('py')" class="w-full flex items-center gap-3 p-3 rounded-lg border border-slate-200 hover:border-indigo-400 hover:bg-indigo-50/40 text-left transition-all">
          <span class="w-9 h-9 rounded-lg bg-amber-50 flex items-center justify-center shrink-0"><i class="fa-brands fa-python text-amber-600"></i></span>
          <span class="flex-1 min-w-0">
            <span class="block text-xs font-bold text-slate-700">Python Pipeline (.py)</span>
            <span class="block text-[11px] text-slate-400">按真实执行记录生成 pandas 复现脚本</span>
          </span>
          <i class="fa-solid fa-chevron-right text-slate-300 text-xs"></i>
        </button>
        <template v-if="state.backend.online">
          <button @click="doExport('parquet')" :disabled="exporting === 'parquet'"
                  class="w-full flex items-center gap-3 p-3 rounded-lg border border-slate-200 hover:border-orange-400 hover:bg-orange-50/40 disabled:opacity-60 text-left transition-all">
            <span class="w-9 h-9 rounded-lg bg-orange-50 flex items-center justify-center shrink-0"><i class="fa-solid fa-file-columns text-orange-600"></i></span>
            <span class="flex-1 min-w-0">
              <span class="block text-xs font-bold text-slate-700">Parquet 列式归档 (.parquet)</span>
              <span class="block text-[11px] text-slate-400">snappy 压缩，含全部清洗结果与特征列</span>
            </span>
            <span class="text-[10px] px-1.5 py-0.5 rounded shrink-0 border"
                  :class="canEncode('parquet') ? 'bg-emerald-50 text-emerald-600 border-emerald-200' : 'bg-rose-50 text-rose-600 border-rose-200'"
                  :title="codecTitle('parquet')">
              <i class="fa-solid mr-0.5" :class="canEncode('parquet') ? 'fa-circle-check' : 'fa-triangle-exclamation'"></i>{{ codecBadge('parquet') }}
            </span>
          </button>
          <button @click="doExport('feather')" :disabled="exporting === 'feather'"
                  class="w-full flex items-center gap-3 p-3 rounded-lg border border-slate-200 hover:border-sky-400 hover:bg-sky-50/40 disabled:opacity-60 text-left transition-all">
            <span class="w-9 h-9 rounded-lg bg-sky-50 flex items-center justify-center shrink-0"><i class="fa-solid fa-feather text-sky-600"></i></span>
            <span class="flex-1 min-w-0">
              <span class="block text-xs font-bold text-slate-700">Feather 高速格式 (.feather)</span>
              <span class="block text-[11px] text-slate-400">读写最快，适合本地流水线中转</span>
            </span>
            <span class="text-[10px] px-1.5 py-0.5 rounded shrink-0 border"
                  :class="canEncode('feather') ? 'bg-emerald-50 text-emerald-600 border-emerald-200' : 'bg-rose-50 text-rose-600 border-rose-200'"
                  :title="codecTitle('feather')">
              <i class="fa-solid mr-0.5" :class="canEncode('feather') ? 'fa-circle-check' : 'fa-triangle-exclamation'"></i>{{ codecBadge('feather') }}
            </span>
          </button>
        </template>
        <div v-else class="w-full flex items-center gap-3 p-3 rounded-lg border border-dashed border-slate-300 opacity-90">
          <span class="w-9 h-9 rounded-lg bg-orange-50 flex items-center justify-center shrink-0"><i class="fa-solid fa-file-columns text-orange-500"></i></span>
          <span class="flex-1 min-w-0">
            <span class="block text-xs font-bold text-slate-600">Parquet / Feather</span>
            <span class="block text-[11px] text-rose-500">后端未连接（{{ API_BASE }}）· 四种格式都由服务端直出</span>
          </span>
          <button @click="checkBackend()" class="text-[10px] px-2 py-1 rounded border border-slate-300 text-slate-500 hover:text-indigo-600 hover:border-indigo-300 shrink-0">重试</button>
        </div>
      </div>
      <div class="px-5 pb-4">
        <div class="rounded-lg border border-slate-200 bg-slate-50/70 p-3">
          <div class="flex items-center gap-2 text-[11px] font-semibold text-slate-600">
            <i class="fa-solid fa-folder-plus text-indigo-600"></i>
            <span>不下载，直接存进数据集目录</span>
          </div>
          <div class="mt-1 text-[11px] text-slate-400 leading-snug">
            与上面的导出同一条编码路径，字节只落到服务端磁盘；同名不覆盖，后端另起带时间戳的文件名。
          </div>
          <div class="mt-2.5 flex items-center gap-2">
            <input v-model="saveName" type="text" spellcheck="false" :placeholder="`${dsName}_v${state.history.version || 0}（留空即用这个）`"
                   class="min-w-0 flex-1 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] text-slate-700 outline-none focus:border-indigo-400" />
            <select v-model="saveFmt" class="rounded-md border border-slate-200 bg-white px-2 py-1.5 text-[11px] text-slate-600 outline-none focus:border-indigo-400 shrink-0">
              <option v-for="(label, k) in saveFormats" :key="k" :value="k">{{ label }}</option>
            </select>
            <button @click="doSaveAsDataset" :disabled="saving || !state.backend.online"
                    class="shrink-0 px-3 py-1.5 rounded-md bg-indigo-600 hover:bg-indigo-700 text-white text-[11px] font-semibold disabled:opacity-60">
              <i class="fa-solid fa-arrow-down-to-bracket mr-1"></i>{{ saving ? '写入中…' : '存为数据集' }}
            </button>
          </div>
          <div v-if="savedOk" class="mt-2 flex items-start gap-1.5 text-[11px] text-emerald-700 leading-snug break-words">
            <i class="fa-solid fa-circle-check mt-0.5 shrink-0"></i>
            <span class="min-w-0"
                  title="另存时后端会按行号重编一次时间列，并把合并来源记进会话，重启后可照这份日志重放出同一张表；来源文件若已从数据集目录删除则无法重开。">
              已写进服务端数据集目录：{{ savedOk }}</span>
          </div>
          <!-- 后端编不出的格式从下拉里收起，并把探测到的整段原因写在原地说清楚，而不是让人点了才吃 422 -->
          <div v-if="missingFormats.length" class="mt-2 space-y-1">
            <div v-for="m in missingFormats" :key="m.label"
                 class="flex items-start gap-1.5 text-[11px] text-rose-600 leading-snug break-words">
              <i class="fa-solid fa-triangle-exclamation mt-0.5 shrink-0"></i>
              <span class="min-w-0">{{ m.label }} 后端编不出 · {{ m.reason }}</span>
            </div>
          </div>
        </div>
      </div>
      <div class="px-5 py-3 bg-slate-50 border-t border-slate-200 text-[11px] text-slate-400">
        导出的是当前工作区的数据，可与操作记录 JSON 一同归档复现。
      </div>
    </div>
  </div>
</template>
