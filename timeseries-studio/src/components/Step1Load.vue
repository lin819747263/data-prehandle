<script setup>
import { ref, computed, onActivated, watch } from 'vue'
import { state, datasets, loadPresetData, loadCustomDataset, switchStep, toast } from '../store'
import { parseDataFile, formatFileSize, getFileIconMeta, fileExt } from '../utils'
import { importViaBackend, openDataset, listDatasets, checkBackend, API_BASE } from '../api'

// 内置合成示例（非 dataset 目录文件，独立入口）
const BUILTIN = [
  { key: 'pv', label: '光伏电站实测出力 PV-15min', icon: 'fa-solar-panel' },
  { key: 'load', label: '区域工商业负荷 Load-60min', icon: 'fa-bolt' }
]

const dragOver = ref(false)
const fileInput = ref(null)
const loading = ref(false)
const opening = ref('')

// Parquet/Feather 是列式压缩二进制，浏览器无法解码；CSV/Excel 在线时也统一走后端以便落盘
const NEEDS_BACKEND = ['parquet', 'feather', 'ft']
const acceptAttr = computed(() =>
  state.backend.online ? '.csv,.xlsx,.xls,.txt,.tsv,.parquet,.feather,.ft' : '.csv,.xlsx,.xls,.txt,.tsv')

// ---- 最近打开的数据集：后端 dataset 目录的真实文件 ----
const recent = ref([])
const recentDir = ref('')
const recentState = ref('loading') // loading | ready | offline | error
const recentError = ref('')

async function refreshRecent() {
  if (!state.backend.online) {
    recentState.value = 'offline'
    recent.value = []
    return
  }
  recentState.value = 'loading'
  try {
    const r = await listDatasets()
    recent.value = r.items || []
    recentDir.value = r.dir || state.backend.datasetDir || ''
    recentError.value = ''
    recentState.value = 'ready'
  } catch (e) {
    recentError.value = e.message || String(e)
    recentState.value = 'error'
  }
}

onActivated(() => { refreshRecent() })
// 首屏探活是异步的：后端由离线转在线时要补刷一次
watch(() => state.backend.online, v => { if (v && recentState.value !== 'ready') refreshRecent() })

const totalSize = computed(() => state.pendingFiles.reduce((s, f) => s + f.size, 0))

function addFiles(list) {
  if (!list || list.length === 0) return
  for (const f of list) state.pendingFiles.push({ name: f.name, size: f.size, file: f })
}

function removeFile(idx) { state.pendingFiles.splice(idx, 1) }
function clearFiles() { state.pendingFiles.splice(0, state.pendingFiles.length) }

// 单文件解析入口：后端在线时统一走导入接口（落盘 + 解析），否则浏览器本地解析
async function parseOne(file) {
  const ext = fileExt(file.name)
  if (state.backend.online) {
    try {
      const r = await importViaBackend(file)
      if (!r.data || r.data.length === 0) throw new Error('后端未解析出任何数据行')
      return {
        source: 'backend', format: r.format, saved: r.saved,
        name: r.name.replace(/\.[^.]+$/, ''), columns: r.columns, timeCol: r.timeCol, data: r.data
      }
    } catch (e) {
      if (NEEDS_BACKEND.includes(ext)) throw e
      toast('warning', `后端导入失败（${e.message}），${file.name} 已改用浏览器解析，未写入 dataset 目录`)
    }
  }
  if (NEEDS_BACKEND.includes(ext)) {
    throw new Error(`.${ext} 需后端 pyarrow 解析，但 ${API_BASE} 不可达（请在 timeseries-studio-server 目录执行 uvicorn app.main:app --port 8000）`)
  }
  return { source: 'browser', format: ext, ...(await parseDataFile(file)) }
}

async function confirmLoad() {
  if (state.pendingFiles.length === 0) { toast('warning', '请先选择或上传数据文件！'); return }
  loading.value = true
  if (!state.backend.online && state.pendingFiles.some(p => NEEDS_BACKEND.includes(fileExt(p.name)))) {
    await checkBackend()
  }
  const parsed = []
  const failed = []
  let viaBackend = 0
  for (const p of state.pendingFiles) {
    try {
      const r = await parseOne(p.file)
      if (r.source === 'backend') viaBackend++
      parsed.push({ name: p.name, ...r })
    } catch (e) {
      failed.push(`${p.name}: ${e.message}`)
    }
  }
  loading.value = false
  if (parsed.length === 0) {
    toast('error', `没有文件解析成功\n${failed.join('\n')}`.slice(0, 200))
    return
  }
  loadCustomDataset(parsed, parsed.map(p => p.name))
  if (failed.length > 0) toast('warning', `${failed.length} 个文件未能解析：${failed.join('；')}`.slice(0, 220))
  clearFiles()
  const saved = parsed.filter(p => p.saved)
  const via = viaBackend > 0 ? `（其中 ${viaBackend} 个由后端解析）` : ''
  const savedMsg = saved.length ? `，已写入 dataset 目录：${saved.map(p => p.saved).join('、')}` : ''
  toast('success', `已成功解析 ${parsed.length} 个数据文件${via}${savedMsg}`)
  refreshRecent()
  switchStep(2)
}

function loadSample(key) {
  loadPresetData(key)
  toast('success', `已加载内置示例数据集：${datasets[key].name}`)
  switchStep(2)
}

async function openRecent(item) {
  if (opening.value) return
  opening.value = item.filename
  try {
    const r = await openDataset(item.filename)
    if (!r.data || r.data.length === 0) throw new Error('未解析出任何数据行')
    loadCustomDataset([{
      source: 'backend', format: r.format, name: r.name.replace(/\.[^.]+$/, ''),
      columns: r.columns, timeCol: r.timeCol, data: r.data
    }], [item.filename], 'dataset')
    toast('success', `已打开 ${item.filename}（${r.rowCount.toLocaleString()} 行 × ${r.columns.length} 列）`)
    switchStep(2)
  } catch (e) {
    toast('error', `打开失败：${e.message}`)
  } finally {
    opening.value = ''
  }
}

function relTime(mtime) {
  const diff = Math.max(0, Date.now() / 1000 - mtime)
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 604800) return `${Math.floor(diff / 86400)} 天前`
  return ''
}
</script>

<template>
  <section class="step-panel h-full p-6 flex flex-col gap-5 overflow-y-auto">
    <div class="text-center py-4">
      <h2 class="text-xl font-bold text-slate-800 flex items-center justify-center gap-2">
        <i class="fa-solid fa-database text-indigo-600"></i>
        数据加载
      </h2>
      <p class="text-xs text-slate-500 mt-1">导入的数据集会落盘到后端 dataset 目录，下次可直接从下方打开</p>
    </div>

    <div class="flex-1 flex flex-col gap-4 min-h-0">
      <div class="flex flex-col gap-3 shrink-0">
        <div
          class="bg-white rounded-2xl border-2 border-dashed shadow-sm flex flex-col items-center justify-center cursor-pointer transition-all duration-200 py-8"
          :class="dragOver ? 'border-indigo-500 bg-indigo-50/60' : 'border-slate-300 hover:border-indigo-400 hover:bg-indigo-50/30'"
          @click="fileInput.click()"
          @dragover.prevent="dragOver = true"
          @dragleave.prevent="dragOver = false"
          @drop.prevent="dragOver = false; addFiles($event.dataTransfer.files)"
        >
          <input ref="fileInput" type="file" multiple :accept="acceptAttr" class="hidden" @change="addFiles($event.target.files); $event.target.value = ''" />
          <div class="text-center px-8">
            <div class="w-14 h-14 mx-auto rounded-full bg-indigo-50 flex items-center justify-center mb-3">
              <i class="fa-solid fa-cloud-arrow-up text-2xl text-indigo-500"></i>
            </div>
            <h3 class="text-base font-semibold text-slate-700 mb-1">拖拽文件到此处上传</h3>
            <p class="text-xs text-slate-500 mb-3">或点击下方按钮选择文件</p>
            <div class="flex items-center justify-center gap-2 mb-3">
              <span class="px-2.5 py-1 bg-slate-100 rounded text-[11px] font-mono text-slate-600">.csv</span>
              <span class="px-2.5 py-1 bg-slate-100 rounded text-[11px] font-mono text-slate-600">.xlsx</span>
              <span v-for="ext in ['parquet', 'feather']" :key="ext"
                    class="px-2.5 py-1 rounded text-[11px] font-mono border"
                    :class="state.backend.online ? 'bg-emerald-50 border-emerald-200 text-emerald-700' : 'bg-orange-50 border-orange-200 text-orange-600'"
                    :title="state.backend.online ? `由后端 pyarrow 真实解码 · ${API_BASE}` : '需后端支持（当前未连接）'">
                .{{ ext }} <i v-if="state.backend.online" class="fa-solid fa-circle-check text-[9px]"></i><template v-else>(需后端)</template>
              </span>
            </div>
            <div class="flex items-center justify-center gap-2">
              <button @click.stop="fileInput.click()" class="px-5 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-xs font-semibold shadow-sm transition-colors">
                <i class="fa-solid fa-folder-open mr-1.5"></i>选择文件
              </button>
              <button v-for="b in BUILTIN" :key="b.key" @click.stop="loadSample(b.key)"
                      class="px-4 py-2 border border-slate-300 hover:border-indigo-400 hover:text-indigo-600 bg-white rounded-lg text-xs font-medium transition-colors">
                <i class="fa-solid mr-1.5" :class="b.icon"></i>{{ b.label }}
              </button>
            </div>
            <p class="text-[11px] text-slate-400 mt-3 flex items-center justify-center gap-1">
              <i class="fa-solid fa-circle-info"></i>支持多文件同时上传 · 单文件建议 ≤ 64MB ·
              {{ state.backend.online ? '导入即落盘 dataset 目录，Parquet/Feather 由后端 pyarrow 解码' : '后端未连接：仅浏览器解析 CSV/Excel，且不写入数据集目录' }}
            </p>
            <div class="mt-3 flex items-center justify-center">
              <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] border"
                    :class="state.backend.online ? 'bg-emerald-50 border-emerald-200 text-emerald-700' : 'bg-slate-50 border-slate-200 text-slate-500'">
                <span class="w-1.5 h-1.5 rounded-full" :class="state.backend.online ? 'bg-emerald-500' : (state.backend.checking ? 'bg-amber-400 animate-pulse' : 'bg-slate-400')"></span>
                <i class="fa-solid fa-server text-[10px]"></i>
                <span>{{ state.backend.checking ? '正在探测后端…' : (state.backend.online ? `后端在线 v${state.backend.version} · ${API_BASE}` : `后端离线 · ${state.backend.error || '未连接'}`) }}</span>
                <button @click="checkBackend().then(refreshRecent)" class="underline hover:no-underline ml-0.5">重试</button>
              </span>
            </div>
          </div>
        </div>

        <div v-if="state.pendingFiles.length > 0" class="bg-white rounded-xl border border-slate-200 shadow-sm">
          <div class="px-4 py-2.5 border-b border-slate-200 flex justify-between items-center bg-slate-50/60">
            <div class="flex items-center gap-2">
              <span class="text-xs font-bold text-slate-700">已选文件</span>
              <span class="px-2 py-0.5 rounded-full text-[11px] font-mono bg-indigo-100 text-indigo-800 font-semibold">
                {{ state.pendingFiles.length }} 个文件 · {{ formatFileSize(totalSize) }}
              </span>
              <span v-if="state.backend.online" class="text-[10px] text-teal-600 bg-teal-50 border border-teal-200 rounded px-1.5 py-0.5">
                <i class="fa-solid fa-floppy-disk mr-0.5"></i>确认后将写入 dataset 目录
              </span>
            </div>
            <div class="flex items-center gap-2">
              <button @click="clearFiles()" class="text-[11px] text-slate-500 hover:text-rose-500 transition-colors">
                <i class="fa-regular fa-trash-can mr-1"></i>清空
              </button>
              <button @click="confirmLoad()" :disabled="loading"
                      class="px-3 py-1 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded text-[11px] font-semibold shadow-sm flex items-center gap-1">
                {{ loading ? '导入中…' : '确认导入' }} <i class="fa-solid fa-arrow-right text-[10px]"></i>
              </button>
            </div>
          </div>
          <div class="max-h-[160px] overflow-auto p-2 space-y-1.5">
            <div v-for="(f, idx) in state.pendingFiles" :key="f.name + idx"
                 class="flex items-center gap-2 p-2 rounded-lg border border-slate-100 hover:border-indigo-200 bg-slate-50/50">
              <div class="w-8 h-8 rounded flex items-center justify-center shrink-0" :class="getFileIconMeta(f.name).bg">
                <i class="fa-solid text-sm" :class="[getFileIconMeta(f.name).icon, getFileIconMeta(f.name).color]"></i>
              </div>
              <div class="flex-1 min-w-0">
                <div class="text-xs font-medium text-slate-700 truncate">{{ f.name }}</div>
                <div class="text-[10px] text-slate-400">{{ formatFileSize(f.size) }}</div>
              </div>
              <button @click="removeFile(idx)" class="w-6 h-6 rounded hover:bg-rose-50 text-slate-400 hover:text-rose-500 flex items-center justify-center transition-colors shrink-0">
                <i class="fa-solid fa-xmark text-xs"></i>
              </button>
            </div>
          </div>
        </div>
      </div>

      <div class="bg-white rounded-xl border border-slate-200 shadow-sm flex flex-col overflow-hidden flex-1 min-h-[220px] shrink-0">
        <div class="px-4 py-2.5 border-b border-slate-200 flex items-center justify-between bg-slate-50/60 shrink-0 gap-3">
          <h3 class="text-xs font-bold text-slate-700 flex items-center gap-1.5 shrink-0">
            <i class="fa-regular fa-clock text-indigo-500"></i>最近打开的数据集
            <span v-if="recentState === 'ready'" class="text-[10px] font-normal text-slate-400">{{ recent.length }} 个文件</span>
          </h3>
          <div class="flex items-center gap-2 min-w-0">
            <span v-if="recentDir" class="text-[10px] font-mono text-slate-400 truncate max-w-[380px]" :title="`数据集目录（绝对路径）\n${recentDir}`">
              <i class="fa-solid fa-folder mr-1"></i>{{ recentDir }}
            </span>
            <button @click="refreshRecent()" :disabled="recentState === 'loading'"
                    class="text-[11px] text-slate-500 hover:text-indigo-600 border border-slate-200 hover:border-indigo-300 rounded px-2 py-0.5 transition-colors disabled:opacity-50 shrink-0">
              <i class="fa-solid fa-rotate mr-0.5" :class="recentState === 'loading' ? 'fa-spin' : ''"></i>刷新
            </button>
          </div>
        </div>

        <div class="flex-1 overflow-auto p-3">
          <div v-if="recentState === 'offline'" class="h-full flex flex-col items-center justify-center text-center py-8">
            <i class="fa-solid fa-server text-2xl text-slate-300 mb-2"></i>
            <p class="text-xs text-slate-500 font-medium">读取 dataset 目录需后端在线</p>
            <p class="text-[11px] text-slate-400 mt-1 mb-3 font-mono">{{ API_BASE }}</p>
            <button @click="checkBackend().then(refreshRecent)" class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded text-[11px] font-semibold">
              <i class="fa-solid fa-plug-circle-arrow-up mr-1"></i>重新探测
            </button>
          </div>

          <div v-else-if="recentState === 'error'" class="h-full flex flex-col items-center justify-center text-center py-8">
            <i class="fa-solid fa-triangle-exclamation text-2xl text-amber-400 mb-2"></i>
            <p class="text-xs text-slate-600 font-medium">读取数据集目录失败</p>
            <p class="text-[11px] text-slate-400 mt-1 mb-3 font-mono break-all max-w-[420px]">{{ recentError }}</p>
            <button @click="refreshRecent()" class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded text-[11px] font-semibold">
              <i class="fa-solid fa-rotate mr-1"></i>重试
            </button>
          </div>

          <div v-else-if="recentState === 'loading'" class="h-full flex items-center justify-center py-10 text-slate-400 text-xs">
            <i class="fa-solid fa-spinner fa-spin mr-2"></i>正在扫描 dataset 目录…
          </div>

          <div v-else-if="recent.length === 0" class="h-full flex flex-col items-center justify-center text-center py-8">
            <i class="fa-regular fa-folder-open text-2xl text-slate-300 mb-2"></i>
            <p class="text-xs text-slate-500 font-medium">dataset 目录里还没有数据文件</p>
            <p class="text-[11px] text-slate-400 mt-1 font-mono break-all max-w-[460px]">{{ recentDir }}</p>
            <p class="text-[11px] text-slate-400 mt-2">向上方拖拽上传即可自动写入，或直接把文件拷进该目录后点「刷新」</p>
          </div>

          <div v-else class="grid grid-cols-2 gap-2 content-start">
            <div v-for="item in recent" :key="item.filename"
                 @click="openRecent(item)"
                 class="group p-3 rounded-lg border border-slate-200 hover:border-indigo-300 hover:bg-indigo-50/50 cursor-pointer transition-all flex items-center gap-3"
                 :class="opening === item.filename ? 'border-indigo-400 bg-indigo-50/60' : ''">
              <div class="w-10 h-10 rounded-lg flex items-center justify-center shrink-0 shadow-sm" :class="getFileIconMeta(item.filename).bg">
                <i class="fa-solid text-lg" :class="[getFileIconMeta(item.filename).icon, getFileIconMeta(item.filename).color]"></i>
              </div>
              <div class="flex-1 min-w-0">
                <div class="text-xs font-semibold text-slate-700 truncate group-hover:text-indigo-700">{{ item.filename }}</div>
                <div class="text-[11px] text-slate-400 mt-0.5 flex items-center gap-2">
                  <span class="px-1 rounded font-mono text-[9px] uppercase bg-slate-100 text-slate-500">{{ item.ext }}</span>
                  <span>{{ item.sizeText }}</span>
                  <span class="w-0.5 h-0.5 rounded-full bg-slate-300"></span>
                  <span>{{ item.modifiedAt }}{{ relTime(item.mtime) ? ` · ${relTime(item.mtime)}` : '' }}</span>
                </div>
              </div>
              <i v-if="opening === item.filename" class="fa-solid fa-spinner fa-spin text-indigo-500 text-xs shrink-0"></i>
              <i v-else class="fa-solid fa-chevron-right text-slate-300 group-hover:text-indigo-500 text-xs shrink-0"></i>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
