<script setup>
import { ref, computed, onActivated, watch } from 'vue'
import { state, loadPresetData, loadFilesAsWorkspace, openDatasetFile, switchStep, toast } from '../store'
import { formatFileSize, getFileIconMeta } from '../utils'
import { listDatasets, checkBackend, API_BASE } from '../api'

// 内置合成示例（由后端 preset 接口生成，非 dataset 目录文件）
const BUILTIN = [
  { key: 'pv', label: 'PV 光伏 15min', icon: 'fa-solar-panel' },
  { key: 'load', label: 'Load 负荷 60min', icon: 'fa-bolt' }
]

const dragOver = ref(false)
const fileInput = ref(null)
const loading = ref(false)
const opening = ref('')

const acceptAttr = '.csv,.xlsx,.xls,.txt,.tsv,.parquet,.feather,.ft'
// 与后端 detect_format / dataset_store.ALLOWED_EXTS 逐项对应的真实白名单
const SUPPORTED = ['.csv', '.tsv', '.txt', '.xlsx', '.xls', '.parquet', '.feather', '.ft']

// 「按行合并 + 按时间排序」是后端能力，不是前端承诺：旧后端没这条时话要说得准
const canMerge = computed(() => state.backend.online
  && (state.backend.capabilities || []).includes('workspace:merge'))
const maxMerge = computed(() => state.backend.limits?.mergeFiles ?? 12)

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
const multiSelect = computed(() => state.pendingFiles.length > 1)

function addFiles(list) {
  if (!list || list.length === 0) return
  for (const f of list) state.pendingFiles.push({ name: f.name, size: f.size, file: f })
}

function removeFile(idx) { state.pendingFiles.splice(idx, 1) }
function clearFiles() { state.pendingFiles.splice(0, state.pendingFiles.length) }

async function confirmLoad() {
  if (state.pendingFiles.length === 0) { toast('warning', '请先选择或上传数据文件！'); return }
  if (!state.backend.online) await checkBackend()
  if (!state.backend.online) {
    toast('error', `解析与加工已全部改由后端执行，${API_BASE} 不可达时无法载入数据`
      + `：请在 timeseries-studio-server 目录执行 uvicorn app.main:app --port 8000`)
    return
  }
  const files = state.pendingFiles.map(p => p.file)
  loading.value = true
  try {
    const d = await loadFilesAsWorkspace(files)
    const r = d.meta.merge
    clearFiles()
    toast('success', r
      ? `后端已合并 ${r.fileCount} 份：${r.totalRows.toLocaleString()} 行 × ${r.colCount} 列，`
        + `按「${r.timeCol}」升序（${r.rowsMoved.toLocaleString()} 行挪了位置，工作区 ${d.wsId}）`
      : `已由后端解析 ${files[0].name}：${d.meta.rowCount.toLocaleString()} 行 × ${d.meta.colCount} 列`
        + `（工作区 ${d.wsId}，浏览器只缓存前 ${d.page.rows.length} 行）`)
    refreshRecent()
    switchStep(2)
  } catch (e) {
    toast('error', files.length > 1 ? `合并导入失败：${e.message}` : `导入失败：${e.message}`)
  } finally {
    loading.value = false
  }
}

async function loadSample(key) {
  if (await loadPresetData(key)) switchStep(2)
}

async function openRecent(item) {
  if (opening.value) return
  opening.value = item.filename
  try {
    const d = await openDatasetFile(item.filename)
    toast('success', `已打开 ${item.filename}：${d.meta.rowCount.toLocaleString()} 行 × ${d.meta.colCount} 列（工作区 ${d.wsId}）`)
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
    <div class="flex-1 flex flex-col gap-4 min-h-0">
      <div class="flex flex-col gap-3 shrink-0">
        <div
          class="bg-white rounded-2xl border-2 border-dashed shadow-sm flex flex-col items-center justify-center cursor-pointer transition-all duration-200 py-10"
          :class="dragOver ? 'border-indigo-500 bg-indigo-50/60' : 'border-slate-300 hover:border-indigo-400 hover:bg-indigo-50/30'"
          @click="fileInput.click()"
          @dragover.prevent="dragOver = true"
          @dragleave.prevent="dragOver = false"
          @drop.prevent="dragOver = false; addFiles($event.dataTransfer.files)"
        >
          <input ref="fileInput" type="file" multiple :accept="acceptAttr" class="hidden" @change="addFiles($event.target.files); $event.target.value = ''" />
          <div class="text-center px-8 max-w-[760px]">
            <div class="w-14 h-14 mx-auto rounded-full bg-indigo-50 flex items-center justify-center mb-3">
              <i class="fa-solid fa-cloud-arrow-up text-2xl text-indigo-500"></i>
            </div>
            <h3 class="text-base font-semibold text-slate-700 mb-1.5">拖拽文件到此处上传</h3>
            <p class="text-xs text-slate-500 mb-5">
              支持一次拖入或多选<span v-if="canMerge">多份文件</span>
              <template v-if="canMerge">，两份以上由后端按行合并并按时间列升序排序（单次最多 {{ maxMerge }} 份）</template>
              <template v-else-if="!state.backend.online">；后端离线时无法解析或合并</template>
              <template v-else>；当前后端未提供合并能力，多份文件请逐个导入</template>
            </p>
            <div class="flex items-center justify-center flex-wrap gap-1.5 mb-6">
              <span v-for="ext in SUPPORTED" :key="ext"
                    class="px-2.5 py-1 rounded-md bg-slate-100 border border-slate-200 text-[11px] font-mono text-slate-600">{{ ext }}</span>
            </div>
            <button @click.stop="fileInput.click()" class="px-5 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-xs font-semibold shadow-sm transition-colors">
              <i class="fa-solid fa-folder-open mr-1.5"></i>选择文件
            </button>
          </div>
        </div>

        <div v-if="state.pendingFiles.length > 0" class="bg-white rounded-xl border border-slate-200 shadow-sm">
          <div class="px-4 py-2.5 border-b border-slate-200 flex justify-between items-center bg-slate-50/60">
            <div class="flex items-center gap-2">
              <span class="text-xs font-bold text-slate-700">已选文件</span>
              <span class="px-2 py-0.5 rounded-full text-[11px] font-mono bg-indigo-100 text-indigo-800 font-semibold">
                {{ state.pendingFiles.length }} 个文件 · {{ formatFileSize(totalSize) }}
              </span>
              <span v-if="state.pendingFiles.length > 1 && canMerge" class="text-[10px] text-indigo-600 bg-indigo-50 border border-indigo-200 rounded px-1.5 py-0.5">
                <i class="fa-solid fa-code-merge mr-0.5"></i>后端按行合并 · 按时间升序
              </span>
              <span v-else-if="!state.backend.online" class="text-[10px] text-rose-600 bg-rose-50 border border-rose-200 rounded px-1.5 py-0.5">
                <i class="fa-solid fa-lock mr-0.5"></i>后端离线：无法导入
              </span>
            </div>
            <div class="flex items-center gap-2">
              <button @click="clearFiles()" class="text-[11px] text-slate-500 hover:text-rose-500 transition-colors">
                <i class="fa-regular fa-trash-can mr-1"></i>清空
              </button>
              <button @click="confirmLoad()" :disabled="loading"
                      class="px-3 py-1 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded text-[11px] font-semibold shadow-sm flex items-center gap-1">
                {{ loading ? (multiSelect ? '合并中…' : '导入中…') : (multiSelect ? `合并导入 ${state.pendingFiles.length} 份` : '确认导入') }}
                <i class="fa-solid text-[10px]" :class="multiSelect ? 'fa-code-merge' : 'fa-arrow-right'"></i>
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

        <div class="flex items-center gap-2 px-4 py-2 border-b border-slate-100 bg-white shrink-0">
          <span class="text-[10px] font-semibold text-slate-400 tracking-wide shrink-0">内置示例</span>
          <button v-for="b in BUILTIN" :key="b.key" @click="loadSample(b.key)"
                  class="px-2.5 py-1 rounded-md border border-slate-200 hover:border-indigo-300 hover:text-indigo-600 bg-white text-[11px] font-medium text-slate-600 transition-colors">
            <i class="fa-solid mr-1" :class="b.icon"></i>{{ b.label }}
          </button>
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
