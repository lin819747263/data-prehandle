<script setup>
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { ElMessageBox } from 'element-plus'
import {
  state, ds, touch, switchStep, toast, generateSyntheticData, logAction,
  LOG_STEP_META, LOG_ICONS, exportActionLog, importActionLog,
  replayActionLog, clearImportedLog, generatePythonCode,
  undo, redo, initHistoryAndSession, restoreSession, clearSession
} from './store'
import { exportDatasetCSV, exportDatasetExcel } from './utils'
import { checkBackend, exportViaBackend, API_BASE } from './api'
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
const logCount = computed(() => state.actionLog.length)

// ---- 操作记录抽屉 ----
const filters = [
  { key: 'all', label: '全部' },
  { key: '1', label: '① 加载' },
  { key: '2', label: '② 配置' },
  { key: '3', label: '③ 探索' },
  { key: '4', label: '④ 清洗' },
  { key: '5', label: '⑤ 特征' }
]
const groupedLog = computed(() => {
  void state.dataVersion
  const filtered = state.logFilter === 'all'
    ? state.actionLog
    : state.actionLog.filter(a => String(a.step) === state.logFilter)
  const groups = {}
  filtered.forEach(e => { (groups[e.step] = groups[e.step] || []).push(e) })
  return Object.keys(groups).map(Number).sort((a, b) => a - b).map(n => ({ n, meta: LOG_STEP_META[n], items: groups[n] }))
})
function iconOf(name) { return LOG_ICONS[name] || 'fa-circle-dot' }

function setFilter(k) { state.logFilter = k }

async function clearLog() {
  if (state.actionLog.length === 0) return
  try {
    await ElMessageBox.confirm('确定清空全部操作历史？', '清空确认', { type: 'warning' })
    state.actionLog = []
    clearImportedLog()
    // 不 touch 的话抽屉不会刷新（列表依赖 dataVersion），而且这次清空也进不了撤销链
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
    .catch(() => toast('warning', '浏览器拒绝了剪贴板访问，请手动选择复制'))
}

// ---- 后端连接状态 ----
const backendTitle = computed(() => state.backend.online
  ? `FastAPI 后端在线：${API_BASE} · 能力 ${state.backend.capabilities.join(' / ')} · 最近探测 ${state.backend.checkedAt}（点击重新探测）`
  : `后端未连接（${API_BASE}）：${state.backend.error || '未知原因'} · Parquet/Feather 解析导出与 sklearn 完整版孤立森林不可用（点击重试）`)

// ---- 撤销 / 重做 ----
const canUndo = computed(() => state.history.enabled && state.history.undo > 0)
const canRedo = computed(() => state.history.enabled && state.history.redo > 0)
const undoTitle = computed(() => canUndo.value
  ? `撤销「${state.history.undoLabel}」（Ctrl+Z），当前 ${state.history.undo} 步可撤销`
  : (state.history.enabled ? '没有可撤销的变更' : '撤销已关闭：当前数据量超过快照上限，见提示'))
const redoTitle = computed(() => canRedo.value
  ? `重做「${state.history.redoLabel}」（Ctrl+Y），当前 ${state.history.redo} 步可重做`
  : (state.history.enabled ? '没有可重做的变更' : '撤销已关闭：当前数据量超过快照上限，见提示'))

// 输入框里的 Ctrl+Z 是原生文字撤销，不能被工作区撤销抢走
function onKeydown(ev) {
  if (!(ev.ctrlKey || ev.metaKey) || ev.altKey) return
  const t = ev.target
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
  const k = ev.key.toLowerCase()
  if (k === 'z' && !ev.shiftKey) { ev.preventDefault(); undo() }
  else if (k === 'y' || (k === 'z' && ev.shiftKey)) { ev.preventDefault(); redo() }
}

// ---- 本地会话恢复 ----
const sessionWhen = computed(() => {
  const t = new Date(state.session.found?.savedAt || '')
  return isNaN(t.getTime()) ? '未知时间' : t.toLocaleString()
})

// ---- 导出结果模态 ----
const exportDataset = computed(() => {
  void state.dataVersion
  const d = ds()
  if (!d) return null
  const extra = state.features
    .filter(f => !d.columns.some(c => c.key === f.key))
    .map(f => ({ key: f.key, label: f.label, type: 'float' }))
  return { ...d, columns: [...d.columns, ...extra] }
})
const exportRows = computed(() => exportDataset.value?.data?.length || 0)
const exportCols = computed(() => exportDataset.value?.columns?.length || 0)
const exporting = ref('')
async function doExport(kind) {
  const d = exportDataset.value
  if (!d || d.data.length === 0) { toast('warning', '当前没有可导出的数据'); return }
  if (kind === 'csv') { exportDatasetCSV(d, 'cleaned'); return }
  if (kind === 'xlsx') { exportDatasetExcel(d, 'features'); return }
  if (kind === 'py') { state.showCodeModal = true; state.showExportModal = false; return }
  if (kind !== 'parquet' && kind !== 'feather') return
  if (!state.backend.online) {
    toast('info', 'Parquet / Feather 二进制格式需要后端支持（浏览器单文件无法可靠编码列式压缩），请使用 CSV 或 Excel')
    return
  }
  exporting.value = kind
  try {
    const r = await exportViaBackend(kind, d, `timeseries_${d.name}`)
    toast('success', `后端 pyarrow 已编码 ${(r.bytes / 1024).toFixed(1)} KB ${kind.toUpperCase()}：${r.name}`)
    logAction(5, 'dataset', `导出 ${kind.toUpperCase()} 宽表`,
      `${d.data.length.toLocaleString()} 行 × ${d.columns.length} 列 · ${(r.bytes / 1024).toFixed(1)} KB · 后端 pyarrow 编码`,
      { type: 'export', format: kind, bytes: r.bytes })
    state.showExportModal = false
  } catch (e) {
    toast('error', `后端导出失败：${e.message}`)
  } finally {
    exporting.value = ''
  }
}

onMounted(() => {
  generateSyntheticData()
  initHistoryAndSession()
  checkBackend()
  window.addEventListener('keydown', onKeydown)
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
      <span class="bg-emerald-50 text-emerald-700 border border-emerald-200 px-2.5 py-1 rounded-md flex items-center gap-1.5 max-w-[240px]">
        <span class="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse shrink-0"></span>
        <span class="truncate">{{ dsName }}</span>
      </span>
      <div class="flex items-center gap-1">
        <button @click="undo()" :disabled="!canUndo" :title="undoTitle"
                class="px-2 py-1 rounded-md border border-slate-200 bg-white text-slate-600 hover:text-indigo-600 hover:border-indigo-300 disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex items-center gap-1">
          <i class="fa-solid fa-rotate-left text-[10px]"></i>
          <span>撤销</span>
          <span v-if="state.history.undo > 0" class="min-w-[16px] h-4 px-1 rounded-full bg-slate-100 text-slate-500 text-[10px] font-bold flex items-center justify-center">{{ state.history.undo }}</span>
        </button>
        <button @click="redo()" :disabled="!canRedo" :title="redoTitle"
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

  <!-- 本地会话恢复提示：只询问，不擅自替换当前工作区 -->
  <div v-if="state.session.found" class="bg-amber-50 border-b border-amber-200 px-6 py-2 flex items-center gap-2.5 text-xs shrink-0 z-20">
    <i class="fa-solid fa-clock-rotate-left text-amber-600"></i>
    <span class="text-amber-800 min-w-0">
      检测到浏览器本地保存的上次会话：<b class="font-semibold">{{ state.session.found.name }}</b>
      · {{ state.session.found.rows.toLocaleString() }} 行 × {{ state.session.found.cols }} 列
      · {{ state.session.found.ops }} 条操作 · 保存于 {{ sessionWhen }}
      <span class="text-amber-600">（撤销栈与异常检测结果不跨会话）</span>
    </span>
    <div class="ml-auto flex items-center gap-2 shrink-0">
      <button @click="restoreSession()"
              class="px-2.5 py-1 bg-amber-600 text-white rounded hover:bg-amber-700 font-medium transition-colors">
        <i class="fa-solid fa-arrow-rotate-left mr-1 text-[10px]"></i>恢复会话
      </button>
      <button @click="clearSession()"
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
                {{ g.meta.short.replace(/^[①②③④⑤]\s*/, '') }}
              </span>
              <span class="text-[11px] font-bold text-slate-700">{{ g.meta.full }}</span>
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
        <span class="text-[11px] text-slate-500 self-center">仅包含本次会话真实执行过的 {{ state.actionLog.length }} 条操作</span>
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
        <div class="text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 font-mono">
          {{ dsName }} · {{ exportRows.toLocaleString() }} 行 × {{ exportCols }} 列
        </div>
        <button @click="doExport('csv')" class="w-full flex items-center gap-3 p-3 rounded-lg border border-slate-200 hover:border-indigo-400 hover:bg-indigo-50/40 text-left transition-all">
          <span class="w-9 h-9 rounded-lg bg-emerald-50 flex items-center justify-center shrink-0"><i class="fa-solid fa-file-csv text-emerald-600"></i></span>
          <span class="flex-1 min-w-0">
            <span class="block text-xs font-bold text-slate-700">CSV 宽表 (.csv)</span>
            <span class="block text-[11px] text-slate-400">含全部清洗结果、外生变量与衍生特征列，浏览器内真实生成</span>
          </span>
          <i class="fa-solid fa-chevron-right text-slate-300 text-xs"></i>
        </button>
        <button @click="doExport('xlsx')" class="w-full flex items-center gap-3 p-3 rounded-lg border border-slate-200 hover:border-indigo-400 hover:bg-indigo-50/40 text-left transition-all">
          <span class="w-9 h-9 rounded-lg bg-green-50 flex items-center justify-center shrink-0"><i class="fa-solid fa-file-excel text-green-600"></i></span>
          <span class="flex-1 min-w-0">
            <span class="block text-xs font-bold text-slate-700">Excel 工作簿 (.xlsx)</span>
            <span class="block text-[11px] text-slate-400">SheetJS 本地生成，单表全量数据</span>
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
              <span class="block text-[11px] text-slate-400">后端 pyarrow 真实编码（snappy 压缩），含全部清洗结果与特征列</span>
            </span>
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-600 border border-emerald-200 shrink-0">
              <i class="fa-solid fa-circle-check mr-0.5"></i>{{ exporting === 'parquet' ? '编码中…' : '后端可用' }}
            </span>
          </button>
          <button @click="doExport('feather')" :disabled="exporting === 'feather'"
                  class="w-full flex items-center gap-3 p-3 rounded-lg border border-slate-200 hover:border-sky-400 hover:bg-sky-50/40 disabled:opacity-60 text-left transition-all">
            <span class="w-9 h-9 rounded-lg bg-sky-50 flex items-center justify-center shrink-0"><i class="fa-solid fa-feather text-sky-600"></i></span>
            <span class="flex-1 min-w-0">
              <span class="block text-xs font-bold text-slate-700">Feather 高速格式 (.feather)</span>
              <span class="block text-[11px] text-slate-400">后端 Arrow IPC 编码，读写最快，适合本地流水线中转</span>
            </span>
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-600 border border-emerald-200 shrink-0">
              <i class="fa-solid fa-circle-check mr-0.5"></i>{{ exporting === 'feather' ? '编码中…' : '后端可用' }}
            </span>
          </button>
        </template>
        <div v-else class="w-full flex items-center gap-3 p-3 rounded-lg border border-dashed border-slate-300 opacity-90">
          <span class="w-9 h-9 rounded-lg bg-orange-50 flex items-center justify-center shrink-0"><i class="fa-solid fa-file-columns text-orange-500"></i></span>
          <span class="flex-1 min-w-0">
            <span class="block text-xs font-bold text-slate-600">Parquet / Feather</span>
            <span class="block text-[11px] text-rose-500">后端未连接（{{ API_BASE }}）· 浏览器端不提供列式压缩编码，避免产出损坏文件</span>
          </span>
          <button @click="checkBackend()" class="text-[10px] px-2 py-1 rounded border border-slate-300 text-slate-500 hover:text-indigo-600 hover:border-indigo-300 shrink-0">重试</button>
        </div>
      </div>
      <div class="px-5 py-3 bg-slate-50 border-t border-slate-200 text-[11px] text-slate-400">
        导出内容为当前工作区真实数据状态，可与操作记录 JSON 一同归档复现。
      </div>
    </div>
  </div>
</template>
