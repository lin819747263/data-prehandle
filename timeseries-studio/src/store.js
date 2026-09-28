// ============================================================
// 全局状态与业务动作（所有数字均来自真实计算）
//
// 第①期起：明细表留在后端工作区（timeseries-studio-server /api/ws）。
// 第④期起：这里每个数据集只保存两样东西——
//   meta     后端 meta_view()：列/类型/时间列/时间格式/采样频率/版本号/命令序列
//   page     当前页窗口（默认 50 行，array-of-arrays + columns）
// 浏览器不再常驻整表视图：整表级的统计、曲线抽稀与导出都在服务端算。
// 后端不在线时整个工作台转为只读：没有任何浏览器端算法兜底。
// ============================================================
import { reactive, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  isMissing,
  RESAMPLE_RATE_MAP, RESAMPLE_RATE_NAMES, RESAMPLE_METHOD_NAMES,
  downloadBlob
} from './utils'
import * as ws from './api'

export const PAGE_SIZE = 50

function blankDataset(key, name) {
  return {
    key,
    name: name || '',
    wsId: '',
    meta: null,
    columns: [],
    timeCol: '',
    timeFormat: 'YYYY-MM-DD HH:mm:ss',
    timeDetect: null,
    freq: '',
    freqMinutes: null,
    format: '',
    unit: '',
    page: { offset: 0, limit: PAGE_SIZE, columns: [], rows: [], total: 0 },
    overview: null
  }
}

// ---- 数据集仓库（非响应式，配合 dataVersion 计数器驱动视图） ----
export const datasets = { pv: blankDataset('pv'), load: blankDataset('load') }
export const PRESET_LABELS = {
  pv: '光伏电站实测出力数据 (PV-15min)',
  load: '区域工商业电力负荷数据 (Load-60min)'
}

// ---- 工作区桥接：把后端 meta / page 镜像成界面一直在读的形状 ----
function applyMeta(d, meta) {
  if (!meta) return
  d.wsId = meta.wsId || d.wsId
  d.meta = meta
  d.columns = meta.columns || d.columns
  // timeCol 要照实镜像：后端说"这一份工作区没有时间列"就是没有。
  // 写成 `meta.timeCol || d.timeCol` 会让上一份数据的列名残留下来，界面据此把
  // 一列中文文本当时间列去转换，正好复现"没有时间却认错列"那个 bug。
  d.timeCol = meta.timeCol ?? ''
  d.timeFormat = meta.timeFormat || d.timeFormat
  d.timeDetect = meta.timeDetect ?? null
  d.timeRejected = meta.timeRejected || []
  d.freqMinutes = meta.freqMinutes ?? null
  d.freq = meta.freqLabel || ''
  d.name = meta.name || d.name
  if (meta.unit) d.unit = meta.unit
  if (meta.format) d.format = meta.format
  syncHistory(meta)
  syncFeatureList(d)
}

// 撤销/重做的全部依据就是服务端游标与命令日志长度：可撤 steps = cursor，
// 可重做 = opsTotal - cursor，标题取日志里游标两侧的那两条。浏览器不再自己数历史。
function syncHistory(meta) {
  const h = state.history
  h.version = meta.version ?? 0
  h.opsTotal = meta.opsTotal ?? 0
  h.undo = Math.max(0, h.version)
  h.redo = Math.max(0, h.opsTotal - h.version)
  h.canUndo = !!meta.canUndo
  h.canRedo = !!meta.canRedo
  h.undoLabel = meta.undoLabel || ''
  h.redoLabel = meta.redoLabel || ''
}

// 第五步的特征清单由后端列注册表派生：带 feature=<族> 标记的列即工程产物。
// 旧实现里特征只进 state.features、不进 d.columns，两边各记一份列数；
// 现在改为一处真相，"共计 N 列 / 新增 M 列"和导出宽表、后端帧必然同数。
function syncFeatureList(d) {
  state.features = (d.columns || []).map(c => ({
    key: c.key, label: c.label, isNew: !!c.feature, feature: c.feature || null
  }))
}

function applyPage(d, page) {
  if (!page) return
  d.page = {
    offset: page.offset, limit: page.limit, total: page.total,
    columns: page.columns, rows: page.rows
  }
}

export function rowCount() {
  const d = ds()
  return d?.meta?.rowCount ?? 0
}

export async function refreshPage(offset, limit) {
  const d = ds()
  if (!d?.wsId) return null
  const page = await ws.wsRows(d.wsId, offset ?? d.page.offset, limit ?? d.page.limit)
  applyPage(d, page.page)
  applyMeta(d, page.meta)
  touch()
  return d.page
}

// 后端不在线 = 只读。所有会改数据的入口都先过这道闸门，绝不静默降级成本地计算。
export function requireBackend(action) {
  if (state.backend.online) return true
  toast('warning', `${action}需后端在线：在 timeseries-studio-server 目录执行 uvicorn app.main:app --port 8000`)
  state.busy = ''
  return false
}

async function runOp(kind, params, note, { long = false } = {}) {
  const d = ds()
  if (!d?.wsId) { toast('warning', '还没有载入数据集'); return null }
  if (!requireBackend(note?.title || '该操作')) return null
  state.busy = `${note?.title || '处理中'}…`
  try {
    // 与服务端 apply() 同一步：回退后又执行新命令，那段「等待重做」的记录从此不再成立
    pruneUndoneLog(d)
    const r = await ws.wsOp(d.wsId, kind, params, null, { long })
    applyMeta(d, r.meta)
    applyPage(d, r.page)
    touch()
    // params 是审计链的可复现部分（回放/导出 Python 都读它），少了这条记录就只剩一句人话
    if (note) {
      logAction(note.step ?? 2, note.icon ?? 'column', note.title, note.detail(r),
        note.params ? (typeof note.params === 'function' ? note.params(r) : note.params) : null)
      // 盖上服务端版本号：这条记录从此挂在游标上，撤销它就该从"当前数据的历史"里消失
      const last = state.actionLog[state.actionLog.length - 1]
      if (last) last.v = r.meta?.version ?? null
    }
    return r
  } catch (e) {
    toast('error', `${note?.title || '操作'}失败：${e.message}`)
    return null
  } finally {
    state.busy = ''
  }
}

// ---- 数据集加载（全部在后端建工作区）----
async function adoptWorkspace(key, resp, { name, format } = {}) {
  const d = datasets[key] || (datasets[key] = blankDataset(key))
  Object.assign(d, blankDataset(key), { key })
  applyMeta(d, resp.meta)
  applyPage(d, resp.page)
  if (name) d.name = name
  if (format) d.format = format
  state.currentKey = key
  resetWorkspace()
  touch()
  return d
}

export async function loadPresetData(key) {
  if (!requireBackend('加载内置示例')) return false
  state.busy = '正在生成示例数据集…'
  try {
    const resp = await ws.wsCreatePreset(key)
    const d = await adoptWorkspace(key, resp, { name: resp.meta.name, format: 'preset' })
    logAction(1, 'dataset', '加载数据集',
      `${d.name} · ${d.meta.rowCount.toLocaleString()}行×${d.meta.colCount}列 · 后端工作区 ${d.wsId}`)
    // 载入结果就写在页头与第二步快照条上，不再弹一条 3 秒即消失的复述
    return true
  } catch (e) {
    toast('error', `加载示例失败：${e.message}`)
    return false
  } finally {
    state.busy = ''
  }
}

// 导入：后端落盘到 dataset 目录 + 建工作区。单份走这条，两份以上走下面的合并通道。
export async function loadFileAsWorkspace(file) {
  const resp = await ws.wsCreateFile(file, { persist: true, limit: PAGE_SIZE })
  const key = 'custom'
  const d = await adoptWorkspace(key, resp, {
    name: (resp.meta.name || file.name), format: resp.meta.format
  })
  const saved = persistedName(resp)
  logAction(1, 'dataset', '导入并加载数据文件',
    `${file.name} · ${d.meta.rowCount.toLocaleString()}行×${d.meta.colCount}列 · 后端解析${saved ? ` · 已落盘 ${saved}` : ''}`)
  return d
}

// 后端把落盘后的真实文件名放在 persisted（字符串）里；重名时它带时间戳后缀，
// 所以最近列表里看到的可能是另一个名字，回执必须照后端给的写。
function persistedName(resp) {
  const p = resp?.persisted
  if (typeof p === 'string') return p
  return p?.filename || ''
}

// 多文件导入：两份以上一律交给后端 /api/ws/merge 拼表并按时间排序，浏览器不自己接表。
// 合并回执（每份几行、并集多出哪些列、排序前后各是多少）全部来自后端那一次计算。
export async function loadFilesAsWorkspace(files) {
  const list = Array.from(files || [])
  if (list.length === 0) throw new Error('没有待导入的文件')
  if (list.length === 1) return loadFileAsWorkspace(list[0])
  if (!state.backend.capabilities.includes('workspace:merge')) {
    throw new Error(`当前后端未提供合并导入（能力清单里没有 workspace:merge）：`
      + `请更新 timeseries-studio-server 并重启后再合并 ${list.length} 份文件`)
  }
  const resp = await ws.wsCreateFiles(list, { persist: true, limit: PAGE_SIZE })
  const d = await adoptWorkspace('custom', resp, {
    name: resp.meta.name || `${list[0].name} 等 ${list.length} 份合并`,
    format: resp.meta.format
  })
  const r = resp.merge || {}
  const saved = (resp.persisted || []).filter(Boolean)
  logAction(1, 'dataset', '多文件合并导入',
    `${list.map(f => f.name).join(' + ')} · 合并 ${r.totalRows ?? d.meta.rowCount} 行 × `
    + `${r.colCount ?? d.meta.colCount} 列（按 ${r.timeCol || '—'} 升序，位移 ${r.rowsMoved ?? 0} 行）`
    + (saved.length ? ` · 原始 ${saved.length} 份已落盘` : ''))
  return d
}

export async function openDatasetFile(filename) {
  const resp = await ws.wsOpenDataset(filename, PAGE_SIZE)
  const d = await adoptWorkspace('custom', resp, { name: resp.meta.name, format: resp.meta.format })
  logAction(1, 'dataset', '打开数据集目录文件',
    `${filename} · ${d.meta.rowCount.toLocaleString()}行×${d.meta.colCount}列 · 后端工作区 ${d.wsId}`)
  return d
}

// ---- 响应式状态 ----
export const state = reactive({
  currentStep: 1,
  currentKey: 'pv',
  dataVersion: 0,
  drawerOpen: false,
  logFilter: 'all',
  actionLog: [],
  importedLog: null,
  replayStatus: null,   // null | { text }
  showCodeModal: false,
  codeContent: '',
  showExportModal: false,
  pendingFiles: [],     // { name, size, file }
  derivedCols: [],      // { key,label,formula }
  masks: [],            // { key,label,startIdx,endIdx,startTime,endTime,onesCount }
  features: [],         // 第五步特征清单 { key,label,isNew }
  imputeSegAlgos: {},   // colKey -> { segIdx -> algo }
  // 第四步的诊断结果：GET /quality 的整份回显（缺失统计 + 缺失段 + 重复计数）。
  // sig 是这份快照的失效签名（见 sourceSig），不是页码也不是时间戳——它由响应自己算出。
  quality: { sig: '', version: -1, data: null, loading: false, error: '' },
  // 第五步节假日表：GET /holidays 的整份回显（生效日期 + 可选预设），配置存在后端 meta 上
  holidays: { wsId: '', version: -1, data: null, loading: false, error: '' },
  // 图表与刷选用的质量曲线：GET /series（默认全量，含每点的行位置 idx，掩码区间靠它换算）
  series: { key: '', data: null, error: '' },
  lastAnomaly: null,    // 后端 detection_view()：{ algo, expr, results, summary, stale, anomalyIndices }
  splitRatio: 70,
  // 撤销/重做：整份投影自服务端 meta_view()（游标 + 命令日志），浏览器不存历史
  history: { version: 0, opsTotal: 0, undo: 0, redo: 0, canUndo: false, canRedo: false, undoLabel: '', redoLabel: '' },
  // 会话：PUT /api/session 存 UI 状态，GET 时服务端按命令日志把引用的工作区当场重建一遍
  session: {
    enabled: true, found: null, savedAt: '', stateDir: '', workspaces: [],
    saving: false, error: ''
  },
  busy: '',           // 后端在算 thing，界面据此禁用按钮
  backend: { online: false, checking: true, version: '', capabilities: [], exportCodecs: {}, limits: null, error: '', checkedAt: '', datasetDir: '' }
})

export function ds() { return datasets[state.currentKey] }

export function touch() {
  state.dataVersion++
}

// ---- 失效粒度（C10）----
// state.dataVersion 每与后端同步一次就 +1：翻一页、读一次整表统计都算，它回答的是
// 「界面该重算了」，不回答「后端那份整表结果还成立吗」。拿它当失效依据，一次只读分页
// 就能把第四步的整表缺失扫描连同抽稀曲线整摞重打一遍。
// 后端给出的答案叫 valueEpoch：只有改动既有数值的命令推进它（新增列、改配置、掩码都不动），
// 而 /quality 的口径正是「非特征列」（workspace.quality()）——所以整表诊断的签名是
// 工作区 + 数值代 + 行数 + 非特征列集合，四者全等时这份快照必然还是当前帧的。
// 这里读 dataVersion 只为让监听者醒过来：datasets 是普通对象，meta 换了新对象也通知不到 Vue。
// 醒来后比的还是下面这个签名，翻一页那种「同步过但数值没变」照旧不会重打整表。
export function sourceSig(d = ds()) {
  void state.dataVersion
  const m = d?.meta
  if (!d?.wsId || !m) return ''
  // 服务端没给 valueEpoch（老日志/老后端）时退回版本号：宁可多刷一次，不读旧数字
  const epoch = m.valueEpoch ?? `v${m.version ?? -1}`
  const keys = (m.columns || []).filter(c => !c.feature).map(c => c.key).sort().join(',')
  return `${d.wsId}|${epoch}|${m.rowCount ?? 0}|${keys}`
}

// 整表诊断的缓存键由 /quality 的响应自己算出：它带着自己读的是哪一代数值、哪几列，
// 请求在途时帧又被改了 → 两边签名对不上 → 这份结果不会被当成当前这一版的。
function snapshotSig(snap) {
  if (!snap?.wsId) return ''
  const epoch = snap.valueEpoch ?? `v${snap.version ?? -1}`
  const keys = (snap.columns || []).map(c => c.key).sort().join(',')
  return `${snap.wsId}|${epoch}|${snap.rowCount ?? 0}|${keys}`
}

// 抽稀曲线的签名只看被画到的那几列：全是原始列时用数值代（第五步生成特征不动它们，
// 曲线的数字必然还是那一份）；一旦选了特征列就退回整表版本号——重生成特征不推进数值代，
// 只有 version 盯得住它，画出来的线不会停留在上一批特征上。
function seriesSig(cols) {
  const d = ds()
  const m = d?.meta
  if (!d?.wsId || !m) return ''
  const byKey = new Map((m.columns || []).map(c => [c.key, c]))
  const epoch = cols.some(k => byKey.get(k)?.feature)
    ? `v${m.version ?? -1}` : (m.valueEpoch ?? `v${m.version ?? -1}`)
  return `${d.wsId}|${epoch}|${m.rowCount ?? 0}|${[...cols].sort().join(',')}`
}

export function toast(type, msg) {
  ElMessage({ type, message: msg, duration: 3200, showClose: true, grouping: true })
}

// ---- 操作日志 ----
export const LOG_STEP_META = {
  1: { short: '① 加载', full: '第一步 · 数据加载', color: 'bg-sky-500', text: 'text-sky-600' },
  2: { short: '② 接入', full: '第二步 · 数据接入与配置', color: 'bg-indigo-500', text: 'text-indigo-600' },
  3: { short: '③ 探索', full: '第三步 · 数据探索分析', color: 'bg-violet-500', text: 'text-violet-600' },
  4: { short: '④ 清洗', full: '第四步 · 质量诊断与清洗', color: 'bg-amber-500', text: 'text-amber-600' },
  5: { short: '⑤ 特征', full: '第五步 · 特征构建工程', color: 'bg-emerald-500', text: 'text-emerald-600' }
}
export const LOG_ICONS = {
  dataset: 'fa-database', column: 'fa-columns', rename: 'fa-pen', delete: 'fa-trash-can',
  unit: 'fa-gear', resample: 'fa-wave-square', split: 'fa-scissors', impute: 'fa-wrench',
  anomaly: 'fa-triangle-exclamation', mask: 'fa-vector-square', feature: 'fa-flask',
  switch: 'fa-arrow-right', explore: 'fa-chart-line', broom: 'fa-broom', wand: 'fa-wand-magic-sparkles'
}

export function logAction(step, icon, title, detail, params) {
  const now = new Date()
  state.actionLog.push({
    id: Date.now() + Math.random().toString(36).slice(2, 6),
    step, icon, title, detail, params: params || null,
    // 记录挂在哪个后端工作区上：同一份会话里换开第二个数据集时，「本步已完成」这类
    // 由审计链推出的状态必须只认当前工作区的记录，否则上一个数据集的构建会让新数据集显示 ✓
    wsId: ds().wsId || '',
    time: `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`,
    ts: now.getTime()
  })
}

// ---- 步骤切换 ----
export function switchStep(target) {
  state.currentStep = target
}

// 审计链是整场会话的（一份日志里可以连着开了好几个数据集），但界面上的「本步已完成」、
// 导出的流程 JSON 与 Python 脚本都只该描述当前这份数据，所以统一从这里取子集。
// 导入的流程记录没有 wsId（它就是冲着当前工作区导进来的），缺字段按匹配处理，避免整段消失。
//
// 条目上的 v 是「这条命令在服务端落成的那个版本号」，只有经 runOp 执行的数据变更才盖章。
// 游标（meta.version）之前的条目才是当前这份数据的历史，之后的属于「已撤销、等待重做」，
// 于是撤销后从界面上消失、重做又回来 —— 与服务端日志的截断规则同一套口径。
function logVisible(e) {
  if (!Number.isInteger(e.v)) return true
  const owner = Object.values(datasets).find(x => x.wsId && x.wsId === e.wsId)
  if (!owner) return true
  return e.v <= (owner.meta?.version ?? 0)
}

export function visibleActionLog() {
  // datasets 是普通对象（不是 reactive），游标本身追踪不到，靠 dataVersion 这个计数器驱动重算
  void state.dataVersion
  return state.actionLog.filter(logVisible)
}

// 回退后又执行新命令：服务端 apply() 会把那段「等待重做」的日志尾巴截掉，
// 界面这份记录必须同步截掉，否则它会在那之后被当成"当前数据的历史"重新冒出来。
function pruneUndoneLog(d) {
  const cursor = d?.meta?.version
  if (!Number.isInteger(cursor)) return
  state.actionLog = state.actionLog.filter(
    e => e.wsId !== d.wsId || !Number.isInteger(e.v) || e.v <= cursor)
}

export function wsActionLog() {
  const wsId = ds().wsId || ''
  return visibleActionLog().filter(e => (e.wsId ?? wsId) === wsId)
}

// ---- 数据集加载 ----
export function resetWorkspace() {
  state.derivedCols = []
  state.masks = []
  state.features = []
  state.imputeSegAlgos = {}
  state.lastAnomaly = null
  state.quality = { sig: '', version: -1, data: null, loading: false, error: '' }
  invalidateSeries()
}

// ---- 列管理（全部打在服务端工作区）----
export async function renameColumn(idx, newName) {
  const d = ds()
  const col = d.columns[idx]
  if (!col) return false
  const r = await runOp('rename-column', { key: col.key, label: newName }, {
    step: 1, icon: 'rename', title: '重命名列', detail: () => `"${col.key}" → "${newName}"`,
    params: (res) => ({ type: 'rename_column', oldKey: col.key, newKey: res.newKey, newLabel: newName })
  })
  if (!r) return false
  // 特征清单已在 applyMeta 里按新的列注册表重建，这里只同步第二步的衍生列登记
  state.derivedCols.forEach(dc => {
    if (dc.key === r.oldKey) { dc.key = r.newKey; dc.label = newName }
  })
  return true
}

export async function deleteColumn(idx) {
  const d = ds()
  const col = d.columns[idx]
  if (!col) return false
  if (col.isTime || col.key === d.timeCol) {
    toast('warning', '时间列不可删除')
    return false
  }
  const r = await runOp('delete-column', { key: col.key }, {
    step: 1, icon: 'delete', title: '删除列', detail: () => `"${col.label}"`,
    params: { type: 'delete_column', key: col.key, label: col.label }
  })
  if (!r) return false
  const fi = state.features.findIndex(f => f.key === col.key)
  if (fi >= 0) state.features.splice(fi, 1)
  state.derivedCols = state.derivedCols.filter(dc => dc.key !== col.key)
  return true
}

export async function convertColumnUnit(idx, factor, offset, newUnit) {
  const d = ds()
  const col = d.columns[idx]
  if (!col) return false
  const r = await runOp('convert-unit', { key: col.key, factor, offset, newUnit }, {
    step: 1, icon: 'unit', title: '单位转换',
    detail: (res) => `${res.label} y=${factor}x+${offset} · 改动 ${res.changed} 个值`,
    params: { type: 'unit_convert', key: col.key, factor, offset, newUnit, oldUnit: col.unit || '', label: col.label }
  })
  return !!r
}

// ---- 时间列与时间格式：判定与渲染都在后端按整列真解析，浏览器只转述回执 ----
// 这里没有"按当前页猜格式"的兜底了：一页 500 行推不出 11000 行的结论，
// 猜错的代价是整列被洗成 NaT 或 1970 年的假时间，界面上一个错字都不留。
export async function timeFormatDetect(colKey, srcFmt) {
  const d = ds()
  if (!d.wsId || !requireBackend('时间格式识别')) return null
  try {
    return await ws.wsTimeDetect(d.wsId, colKey || d.timeCol, srcFmt || '')
  } catch (e) {
    toast('error', `时间格式识别失败：${e.message}`)
    return null
  }
}

// 把某一列指定成时间列：后端整列解析过闸门才改 dtype。srcFmt 是自动识别认不出时手填的源格式。
export async function setTimeColumn(colKey, srcFmt) {
  const r = await runOp('time-col', { key: colKey, format: srcFmt || null, customFormat: null }, {
    step: 2, icon: 'clock', title: '指定时间列',
    detail: res => `${res.timeCol}（按 ${res.format} 解析出 ${res.parsedCount.toLocaleString()}/${res.rowCount.toLocaleString()} 个值，命中率 ${res.matchRate.toFixed(1)}%）`,
    params: { type: 'time_col', key: colKey, srcFmt: srcFmt || '' }
  })
  return r
}

// 元数据里那份识别回执（后端在载入/指定时算好带回来的，不是浏览器算的）
export function savedTimeDetect(colKey) {
  const d = ds()
  if (!d.timeDetect) return null
  if (!colKey || colKey === d.timeCol) return { format: d.timeDetect.displayFormat || d.timeDetect.format, ...d.timeDetect }
  return null
}

// 页窗口是 array-of-arrays，取某一列的显示值统一走这里
export function pageColumnValues(key) {
  const d = ds()
  const idx = d.page.columns.indexOf(key)
  if (idx < 0) return []
  return d.page.rows.map(r => r[idx])
}

export async function convertTimeColumn(timeColKey, targetFmt, customFmt) {
  const d = ds()
  const r = await runOp('time-format', { format: targetFmt, customFormat: customFmt || null }, {
    step: 2, icon: 'column', title: '时间格式转换',
    detail: (res) => `${rowCount().toLocaleString()}行 → ${targetFmt === 'custom' ? customFmt : targetFmt} · ${res.changed} 个值变化`,
    params: { type: 'time_convert', timeCol: d.timeCol, targetFmt: targetFmt === 'custom' ? customFmt : targetFmt }
  })
  return r ? r.changed : null
}

// ---- 采样频率（后端按真实时间戳算，与显示格式无关）----
export function detectedFreqMinutes() {
  const d = ds()
  return d.freqMinutes ?? null
}

export async function resamplePreview(targetMinutes) {
  const d = ds()
  if (!d.wsId || !requireBackend('重采样预演')) return null
  try {
    return await ws.wsResamplePreview(d.wsId, targetMinutes)
  } catch (e) {
    toast('error', `重采样预演失败：${e.message}`)
    return null
  }
}

export async function resampleDataset(targetRate, method) {
  const targetMinutes = RESAMPLE_RATE_MAP[targetRate]
  const r = await runOp('resample', { targetMinutes, method }, {
    step: 2, icon: 'resample', title: '重采样确认', detail: (res) => res.summary,
    params: { type: 'resample', targetRate, method, detail: `${RESAMPLE_RATE_NAMES[targetRate]} / ${RESAMPLE_METHOD_NAMES[method]}` }
  })
  if (!r) return null
  return {
    oldCount: r.oldCount, newCount: r.newCount, summary: r.summary,
    direction: r.direction, directionLabel: r.directionLabel, fillNote: r.fillNote
  }
}

// 实时质量指标：来自后端 overview 的真实整表统计
export async function refreshOverview() {
  const d = ds()
  if (!d.wsId) return null
  try {
    d.overview = await ws.wsOverview(d.wsId)
    touch()   // 读的是服务端统计，明细帧没变
    return d.overview
  } catch (e) {
    toast('error', `整表统计失败：${e.message}`)
    return null
  }
}

// ---- 外生变量：三条来源全部打在服务端 ----
// 下拉选项读 GET /api/exo/presets（后端清单与生成器同一份表，前端不再自己抄）；
// 生成/对齐在后端函数里完成，浏览器既不碰整列，也没有"先在界面攒一列、再合并进主表"这一步。
let exoCatalogCache = null

export async function loadExoCatalog(force) {
  if (exoCatalogCache && !force) return exoCatalogCache
  if (!state.backend.online) await ws.checkBackend()
  if (!state.backend.online) { toast('warning', '读取外生变量清单需后端在线'); return null }
  try {
    exoCatalogCache = await ws.exoCatalog()
    return exoCatalogCache
  } catch (e) {
    toast('error', `读取外生变量清单失败：${e.message}`)
    return null
  }
}

// 已挂上的外生变量列：后端列注册表里的 exo 标记是唯一真相（撤销后列没了，这里就少了）
export function exoColumns() {
  return (ds().columns || []).map((c, idx) => ({ ...c, idx })).filter(c => c.exo)
}

// 移除一列外生变量：走的是通用删列入口，因此同样进审计链、同样可撤销
export function removeExoColumn(col) { return deleteColumn(col.idx) }

export async function addExoPreset(presetKey, alias, opts = {}) {
  const name = (alias || '').trim()
  const params = { presetKey }
  if (name) { params.key = name; params.label = name }
  if (opts.seed !== undefined && opts.seed !== null) params.seed = opts.seed
  // seed 由服务端补齐并钉进命令日志：撤销再重做拿到的是同一串模拟值
  return runOp('exo-preset', params, {
    step: 2, icon: 'column', title: `预设模板外生变量: ${name || presetKey}`,
    detail: (res) => res.summary,
    params: (res) => ({ type: 'exo-preset', presetKey, key: res.key, label: res.label, seed: res.seed })
  })
}

export async function addExoFormula(key, expr) {
  return runOp('exo-formula', { key, expr }, {
    step: 2, icon: 'column', title: `公式生成外生变量: ${key}`,
    detail: (res) => res.summary,
    params: { type: 'exo-formula', key, expr }
  })
}

// 侧表先看后挂：inspect 把文件落进 dataset/_exo/ 并回表头、可用变量名、时间列候选；
// 列名转不出合法变量名时界面在这里要求改名，而不是静默转写成 ___ 互相覆盖
export async function inspectSideTable(file) {
  if (!requireBackend('解析外生变量侧表')) return null
  state.busy = `正在解析侧表 ${file.name}…`
  try {
    return await ws.exoInspect(file)
  } catch (e) {
    toast('error', `侧表解析失败：${e.message}`)
    return null
  } finally {
    state.busy = ''
  }
}

export async function attachSideTable(spec) {
  const r = await runOp('exo-file', {
    filename: spec.filename, sha: spec.sha, sideTimeCol: spec.sideTimeCol,
    mode: spec.mode, toleranceMinutes: spec.toleranceMinutes || null,
    targets: spec.targets || []
  }, {
    step: 2, icon: 'column', title: `侧表挂列: ${spec.filename}`,
    detail: (res) => res.summary,
    // 未指定目标列时服务端按表头推导并把结果钉进日志，这里取回显的那一份，回放才逐列同名
    params: (res) => ({
      type: 'exo-file', filename: spec.filename, sha: spec.sha, sideTimeCol: spec.sideTimeCol,
      mode: spec.mode, toleranceMinutes: spec.toleranceMinutes || null,
      targets: res.targets || spec.targets || []
    })
  }, { long: true })
  return r
}

// ---- 列运算 ----
export function computeTermChain(terms, row) {
  let result = Number(row[terms[0].col])
  if (!isFinite(result)) return null
  for (let i = 1; i < terms.length; i++) {
    const val = Number(row[terms[i].col])
    if (!isFinite(val)) return null
    switch (terms[i].op) {
      case '+': result += val; break
      case '-': result -= val; break
      case '*': result *= val; break
      case '/': if (val === 0) return null; result /= val; break
      default: return null
    }
  }
  return result
}

export async function applyDerivedCol(name, terms) {
  const d = ds()
  if (d.columns.some(c => c.key === name) || state.derivedCols.some(c => c.key === name)) {
    toast('warning', `列名 [${name}] 已存在，请更换名称`)
    return false
  }
  const r = await runOp('derived-column', { name, terms }, {
    step: 2, icon: 'column', title: `列运算生成: ${name}`, detail: (res) => res.formula,
    params: { type: 'multi_calc', terms: terms.map(t => ({ ...t })), name }
  })
  if (!r) return false
  state.derivedCols.push({ key: r.key, label: name, formula: r.formula })
  return r
}

export async function deleteDerivedCol(idx) {
  const dc = state.derivedCols[idx]
  if (!dc) return false
  const r = await runOp('delete-column', { key: dc.key }, {
    step: 2, icon: 'delete', title: `删除派生列: ${dc.key}`, detail: () => dc.formula || dc.key,
    params: { type: 'delete_column', key: dc.key, label: dc.label || dc.key }
  })
  if (!r) return false
  state.derivedCols.splice(idx, 1)
  return true
}

export async function clearAllDerivedCols() {
  const keys = state.derivedCols.map(dc => dc.key)
  for (const key of keys) {
    const okDel = await runOp('delete-column', { key }, {
      step: 2, icon: 'delete', title: '清空派生列', detail: () => key,
      params: { type: 'delete_column', key, label: key }
    })
    if (!okDel) return false
    state.derivedCols = state.derivedCols.filter(dc => dc.key !== key)
  }
  state.derivedCols = []
  return true
}

// ============ 第四步：质量诊断与清洗（第②期起全部在后端执行）============
// 浏览器不再持有任何缺失/异常算法：这一页看到的每个数字都来自 GET /quality 与
// POST /anomaly-detect，要改数据就发一条加工命令，帧仍留在服务端。
const IMPUTE_LABELS = { linear: '线性插值', ffill: '前向观测值填充', spline: '三次样条平滑', zero: '常数0置换' }
const REPAIR_LABELS = { clip: '上下阈值截断', nan_impute: '置缺失并重插值', mask_only: '仅生成布尔掩码' }
export const ANOMALY_NAMES = {
  '3sigma': '3-Sigma', iqr: 'IQR 箱线法', iforest: '孤立森林(近似)',
  iforest_sklearn: '孤立森林(sklearn)', expr: '自定义表达式'
}

// 诊断快照按 sourceSig 认人（工作区 + 数值代 + 行数 + 非特征列集合）：
// 版本号每执行一条命令都会前进，可「缺失段还指向哪颗帧」只由数值代决定。
// 生成特征、加掩码、改时间格式这些命令不动既有数值，旧快照的行号依然对得上；
// 反过来，任何一次真正改值的清洗都会让签名变化，旧的缺失段行号从此作废而不是继续拿去填补。
export function qualityData() {
  const q = state.quality
  const sig = sourceSig()
  if (!sig || !q.data || q.sig !== sig) return null
  return q.data
}

export async function loadQuality({ force = false } = {}) {
  const d = ds()
  if (!d?.wsId) return null
  const version = d.meta?.version ?? -1
  if (!force && qualityData()) return state.quality.data
  if (!requireBackend('质量诊断')) return null
  state.quality = { sig: '', version, data: null, loading: true, error: '' }
  try {
    const snap = await ws.wsQuality(d.wsId)
    // 缓存键取服务端此刻这一份快照自己的签名（含它读到的 valueEpoch 与列集合）：
    // 请求在途时帧又被改过，这个签名就不会等于界面上的 sourceSig()，结果自动不被认账。
    state.quality = { sig: snapshotSig(snap), version: snap.version ?? version,
                      data: snap, loading: false, error: '' }
    if ((snap.version ?? version) !== version) applyMeta(d, await ws.wsMeta(d.wsId))
    return snap
  } catch (e) {
    state.quality = { sig: '', version, data: null, loading: false, error: e.message }
    toast('error', `质量诊断失败：${e.message}`)
    return null
  }
}

export function columnMissingStats() {
  const q = qualityData()
  if (!q) return null
  return {
    stats: q.columns, totalRows: q.rowCount, totalCells: q.rowCount * q.colCount,
    totalMissing: q.totalMissingCells, missingRate: q.missingRate,
    duplicateCount: q.duplicateRows, duplicateGroups: q.duplicateGroups, segmentCap: q.segmentCap
  }
}

export function segmentsOf(colKey) {
  return qualityData()?.segments?.[colKey] || []
}

// 超过 /quality 上限的列，浏览器根本拿不到全部缺失段，逐段选算法就是假的
export function segmentsTruncated(colKey) {
  return !!qualityData()?.segmentsTruncated?.[colKey]
}

export function segAlgo(colKey, idx) {
  return state.imputeSegAlgos[colKey]?.[idx] || 'linear'
}

export function setSegAlgo(colKey, idx, algo) {
  if (!state.imputeSegAlgos[colKey]) state.imputeSegAlgos[colKey] = {}
  state.imputeSegAlgos[colKey][idx] = algo
}

function segTargets(colKey, segs, algoOf) {
  return segs.map((s, i) => ({ key: colKey, startIdx: s.startIdx, endIdx: s.endIdx, algo: algoOf(i) }))
}

function colLabel(key) {
  return ds()?.columns?.find(c => c.key === key)?.label || key
}

async function runImpute(params, note) {
  return runOp('impute', params, note)
}

export async function applySegmentImpute(colKey, segIdx) {
  const seg = segmentsOf(colKey)[segIdx]
  if (!seg) { toast('warning', '缺失段列表已刷新，请重新选择要填补的段'); return null }
  const algo = segAlgo(colKey, segIdx)
  return runImpute({ targets: [{ key: colKey, startIdx: seg.startIdx, endIdx: seg.endIdx, algo }] }, {
    step: 4, icon: 'impute', title: `填补 ${colLabel(colKey)} 第${segIdx + 1}段`,
    detail: r => r.summary,
    params: { type: 'impute_segment', colKey, segments: [{ startIdx: seg.startIdx, endIdx: seg.endIdx, algo }] }
  })
}

export async function applyAllSegmentsImpute(colKey) {
  const segs = segmentsOf(colKey)
  // 段列表为空时这块卡片整块不渲染、被截断时按钮 disabled，这两道拦截在界面里都走不到，只做兜底
  if (!segs.length || segmentsTruncated(colKey)) return null
  const targets = segTargets(colKey, segs, i => segAlgo(colKey, i))
  const used = [...new Set(targets.map(t => t.algo))].map(a => IMPUTE_LABELS[a] || a).join('、')
  return runImpute({ targets }, {
    step: 4, icon: 'impute', title: `一键填补 ${colLabel(colKey)}（${used}）`,
    detail: r => r.summary,
    params: { type: 'impute_segment', colKey, segments: targets.map(t => ({ startIdx: t.startIdx, endIdx: t.endIdx, algo: t.algo })) }
  })
}

// 全表扫描填补 + 重复时间戳合并：段号与分组都在服务端算，所以大表也能一次做完
export async function imputeAllAndDedupe(dupStrategy, defaultAlgo = 'linear') {
  const q = qualityData() || await loadQuality()
  const hasMissing = q ? q.columns.some(c => c.imputable && c.missing > 0) : true
  const hasDup = q ? q.duplicateRows > 0 : true
  if (q && !hasMissing && !hasDup) {
    toast('info', '没有可填补的缺失值，也没有重复时间戳：未做任何修改')
    return null
  }
  const params = { all: hasMissing, defaultAlgo, dedupe: hasDup ? (dupStrategy || 'mean') : null }
  const floatCols = q ? q.columns.filter(c => c.imputable).map(c => c.key) : []
  const r = await runImpute(params, {
    step: 4, icon: 'impute', title: '一键执行缺失值填补与去重',
    detail: x => x.summary,
    // floatCols 进审计参数：导出 Python 时不能靠"事后判断 dtype"猜当时填了哪些列
    params: { type: 'impute_all', all: hasMissing, defaultAlgo, dedupe: params.dedupe, floatCols }
  })
  // 填了几格/合并了几组：缺失计数表当场归零，同一句 summary 也已永久写进操作记录，不再弹
  return r
}

// ---- 异常检测与修复（检测只读、修复是加工命令；判定索引全部留在服务端）----
// ---- 异常检测与修复（检测只读、修复是加工命令；判定索引全部留在服务端）----
// 侧栏只留判定口径与「在后端算」这件事，实现来历不写在这里
export const ANOMALY_ALGOS = {
  '3sigma': '3σ：偏离均值超过 3 倍标准差判为异常（总体标准差 ÷n）。后端 numpy 向量化。',
  'iqr': 'IQR：落在 Q1−1.5×IQR 与 Q3+1.5×IQR 之外判为异常。分位数取 sorted[int(n·q)]，与后端同一口径。',
  'iforest': '孤立森林（近似版 · 后端 numpy）：回看 33 个观测的窗口 MAD×4 判异常，不假设分布。与 sklearn 版是两套算法，结果本就不同。',
  'iforest_sklearn': '孤立森林（sklearn 完整版 · 后端）：逐列真实拟合打分，缺失点自动剔除，边界取正常点的 0.5%/99.5% 分位。',
  'expr': '自定义表达式：变量 v 为当前值，可用统计量 mean/std/median/q1/q3/min/max，返回 true 即异常。后端按 AST 白名单逐列向量化求值。'
}
export const ANOMALY_REPAIRS = {
  'clip': '截断限制：超界点钳到上下界，行数与时间戳不变。',
  'nan_impute': '缺失值重算：异常点置为 NaN 后按时序线性插值重算。',
  'mask_only': '掩码标记：只生成布尔掩码列，原始数据不改。'
}

export async function detectAnomalies(algo, exprStr, params = {}) {
  const d = ds()
  if (!d?.wsId) { toast('warning', '还没有载入数据集'); return null }
  if (!requireBackend('异常检测')) return null
  const expr = (exprStr || '').trim()
  if (algo === 'expr' && !expr) { toast('warning', '请输入异常判定表达式'); return null }
  state.busy = '后端正在扫描异常…'
  try {
    const resp = await ws.wsAnomalyDetect(d.wsId, { algo, expr: expr || null, ...params })
    applyMeta(d, resp.meta)
    state.lastAnomaly = { ...resp.detection, time: new Date().toLocaleTimeString(), wsId: d.wsId }
    invalidateSeries()      // 覆盖层换了：曲线必须重取，否则画的是上一次检测的点
    return resp.detection
  } catch (e) {
    toast('error', `检测失败：${e.message}`)
    return null
  } finally {
    state.busy = ''
  }
}

// 撤销/重做/会话恢复之后与后端对齐：检测缓存留在服务端，界面只是它的一份投影
export async function refreshAnomaly() {
  const d = ds()
  if (!d?.wsId || !state.backend.online) { state.lastAnomaly = null; return null }
  try {
    const resp = await ws.wsAnomaly(d.wsId)
    applyMeta(d, resp.meta)
    state.lastAnomaly = resp.detection ? { ...resp.detection, time: '', wsId: d.wsId } : null
    return state.lastAnomaly
  } catch (e) {
    state.lastAnomaly = null
    return null
  }
}

export async function repairAnomalies(mode) {
  const la = state.lastAnomaly
  // 第四步在未检测/已失效时把按钮禁掉并常驻说明原因，这里只是兜底：不再另弹一条重复的提示
  if (!la || la.stale) return null
  const r = await runOp('anomaly-repair', { repair: mode }, {
    step: 4, icon: 'broom', title: '异常修复执行',
    detail: x => x.summary,
    // 审计参数必须自足：回放/导出 Python 时没有当时的内存状态可借，只能按这里记的算法重测一遍
    params: {
      type: 'anomaly_repair', repair: mode, algo: la.algo, expr: la.expr || '',
      iforest: la.params || null,
      columns: (la.results || []).filter(x => x.anomalies > 0)
        .map(x => ({ key: x.key, lower: x.lower, upper: x.upper, count: x.anomalies }))
    }
  })
  if (r) await refreshAnomaly()
  return r
}

// ---- 曲线取数（画图不再需要整表）----
// 第四步默认要全量（maxPoints = 0 → 后端 points=0，整表一行不抽）；只有对照场景才传正数走抽稀档。
const seriesCache = new Map()

export function invalidateSeries() {
  seriesCache.clear()
  state.series = { key: '', data: null, error: '' }
}

export async function loadSeries(keys, maxPoints = 0) {
  const d = ds()
  const cols = (Array.isArray(keys) ? keys : [keys]).filter(Boolean)
  if (!d?.wsId || !cols.length) return null
  if (!state.backend.online) return null
  const la = state.lastAnomaly
  const detTag = la && la.wsId === d.wsId ? `${la.version}:${la.stale ? 'stale' : 'fresh'}` : 'none'
  // 后端不认 points=0（能力清单没有 workspace:series-full）时退回抽稀档：
  // 是否真的抽了点由响应里的 decimated 说了算，界面照它写，不许自称全量。
  // 点数就在键上，所以退回档与全量档各存各的，不会互相顶掉。
  const pts = maxPoints <= 0 && !state.backend.capabilities.includes('workspace:series-full') ? 1200 : maxPoints
  const key = `${seriesSig(cols)}|${pts}|${detTag}`
  if (!key) return null
  const hit = seriesCache.get(key)
  if (hit) {
    if (state.series.key !== key) state.series = { key, data: hit, error: '' }
    return hit
  }
  try {
    const resp = await ws.wsSeries(d.wsId, cols, pts)
    if (seriesCache.size > 24) seriesCache.clear()
    seriesCache.set(key, resp)
    state.series = { key, data: resp, error: '' }
    return resp
  } catch (e) {
    // 全量档撞上限（行 × 列 的格子数超过后端守卫）就是这条路：图会空，
    // 原因留在 state.series.error 上由图上那条红字常驻说明——3 秒即消失的 toast 不是它该去的地方。
    state.series = { key: '', data: null, error: e.message }
    return null
  }
}

export function currentSeries() {
  return state.series.data
}

// ---- 手动掩码列（服务端建列，界面只登记区间）----
export async function generateMask(maskName, range) {
  const r = await runOp('mask-generate',
    { maskName, startIdx: range.startIdx, endIdx: range.endIdx }, {
    step: 4, icon: 'mask', title: `生成布尔掩码: ${maskName}`,
    detail: x => x.summary,
    // onesCount 只有服务端知道（它数了区间内的 1）：记下来导出 Python 时才能自检
    params: (res) => ({ type: 'mask_generate', maskName, startIdx: range.startIdx, endIdx: range.endIdx, onesCount: res.onesCount })
  })
  if (!r) return null
  const entry = {
    key: r.key, label: r.label, startIdx: r.startIdx, endIdx: r.endIdx,
    startTime: r.startTime, endTime: r.endTime, onesCount: r.onesCount
  }
  const i = state.masks.findIndex(m => m.key === r.key)
  if (i >= 0) state.masks.splice(i, 1, entry)
  else state.masks.push(entry)
  return r.onesCount
}

export async function deleteMask(idx) {
  const m = state.masks[idx]
  if (!m) return null
  const r = await runOp('mask-delete', { keys: [m.key] }, {
    step: 4, icon: 'mask', title: `删除掩码列: ${m.key}`,
    detail: x => x.summary, params: { type: 'mask_delete', maskKey: m.key }
  })
  if (!r) return null
  const i = state.masks.findIndex(x => x.key === m.key)
  if (i >= 0) state.masks.splice(i, 1)
  return r
}

export async function deleteAllMasks() {
  const keys = state.masks.map(m => m.key)
  // 没有掩码列时「清空」按钮本就不渲染
  if (!keys.length) return null
  const r = await runOp('mask-delete', { keys }, {
    step: 4, icon: 'mask', title: '清空全部掩码',
    detail: x => x.summary, params: { type: 'mask_delete_all', keys }
  })
  if (!r) return null
  state.masks = []
  return r
}

// ---- 数据集切分 ----
// 与后端 features.split_counts 同一份公式（整数除法，先训练后测试再验证），
// 滑杆上显示的三段行数即划分列里真正写入的行数。
export function splitCounts(trainPct) {
  const total = rowCount()
  const train = Math.floor(total * trainPct / 100)
  const rest = total - train
  const test = Math.floor(rest / 2)
  const val = rest - test
  return { train, val, test, total }
}

// 拖动滑杆本身不改数据，但比例是复现切分的必要参数，必须进审计链（松手才算一次设置）
let lastLoggedSplit = null
export function setSplitRatio(ratio) {
  const r = Math.min(85, Math.max(50, Math.round(Number(ratio)) || 70))
  state.splitRatio = r
  if (lastLoggedSplit === r) return r
  lastLoggedSplit = r
  const s = splitCounts(r)
  logAction(2, 'split', '设置切分比例', `训练 ${r}% → ${s.train.toLocaleString()} / ${s.val.toLocaleString()} / ${s.test.toLocaleString()} 条`,
    { type: 'split', ratio: r })
  // 切分比例是可撤销的工作区状态，touch 让它进入撤销快照
  touch()
  return r
}

// 把比例落成真实的一列（dataset_split：train/val/test）。
// 切分动作从此跟数据走，不再只是滑杆上的三个数字——下游脚本导出宽表时这一列就在表里。
export async function applySplitColumn(ratio) {
  const r = Math.min(85, Math.max(50, Math.round(Number(ratio ?? state.splitRatio)) || 70))
  const s = splitCounts(r)
  const res = await runOp('split', { ratio: r }, {
    step: 2, icon: 'split', title: '生成数据集划分列',
    detail: x => `训练 ${x.ratio}% · train ${x.train.toLocaleString()} / val ${x.val.toLocaleString()} / test ${x.test.toLocaleString()}（共 ${x.rowCount.toLocaleString()} 行，时序不打乱）`,
    // 三段行数记的是服务端那一次的真实数字（含当时的行数）：导出脚本照它复现，
    // 之后若再删行/撤销，脚本里的 assert 会直接失败，而不是悄悄切成另一份数据
    params: x => ({ type: 'split_apply', ratio: x.ratio, key: x.key, label: x.label,
      rows: x.rowCount, train: x.train, val: x.val, test: x.test })
  })
  if (!res) return null
  // 后端才是行数的裁判：界面先按它给的数校正一次，公式若哪天两边不一致会立刻显形
  if (res.train !== s.train || res.val !== s.val || res.test !== s.test) {
    toast('warning', `后端划分结果为 train ${res.train} / val ${res.val} / test ${res.test}，与本地预估不同，以后端为准`)
  }
  return res
}

// ---- 节假日表（第五步日历特征的配置项，整份存在后端工作区 meta 上）----
export async function loadHolidays() {
  const d0 = ds()
  if (!d0?.wsId) return null
  if (!requireBackend('读取节假日表')) return null
  try {
    const r = await ws.wsHolidays(d0.wsId)
    state.holidays = { wsId: d0.wsId, version: r.version, data: r, loading: false, error: '' }
    return r
  } catch (e) {
    state.holidays = { wsId: d0.wsId, version: -1, data: null, loading: false, error: e.message }
    // 原因由节假日卡自己常驻显示（带重试按钮），不再弹一条 3 秒即消失的复述
    return null
  }
}

// days 是界面编辑后的整份表（新增/删除/换年份都提交全量）：后端 check_holiday_days 逐条校验格式
export async function saveHolidays(days, source) {
  const r = await runOp('holidays', { days, source }, {
    step: 5, icon: 'wand', title: '配置节假日表',
    detail: x => `${x.count} 天（${x.source}）` + (x.days.length ? ` · ${x.days[0]} ~ ${x.days[x.days.length - 1]}` : ' · 不认任何节假日'),
    params: x => ({ type: 'holidays', count: x.count, source: x.source, days: x.days })
  })
  if (!r) return null
  await loadHolidays()
  return r
}

// ---- 第五步：特征工程（构建全部在后端，界面只发命令、读列注册表）----
export function initFeatureList() {
  syncFeatureList(ds())
}

// 正余弦不是第 7 个日历维度，而是对「已勾选的周期维度」换一种编码方式，所以按维度各给一个勾：
// sincos_hour 只在 hour 也勾了时才生效。只有周期长度固定的维度能编码；
// day 每月天数不同（28~31），无固定周期，故不参与。
const TIME_DIMS = ['hour', 'day', 'month', 'weekday', 'is_weekend', 'holiday']
const CYCLE_DIMS = ['hour', 'weekday', 'month']
const CYCLE_PERIOD = { hour: 24, weekday: 7, month: 12 }
const CYCLE_CN = { hour: '小时', weekday: '星期', month: '月份' }
// 勾选集里的特殊项：sincos_<维度> = 该维度改用正余弦；keep_cyc_original = 编码后仍保留数值原列
const SINCOS_OPTS = CYCLE_DIMS.map(o => `sincos_${o}`)
const KEEP_ORIGINAL_OPT = 'keep_cyc_original'
// 界面照这份列表渲染勾选框：勾选项与计划函数共用一份键表，两边不会各写各的
export const TIME_OPTS_ALL = [...TIME_DIMS, ...SINCOS_OPTS, KEEP_ORIGINAL_OPT]

// 计划函数是「哪些列会被生成」的唯一答案：面板预览、构建、审计明细、回放、Python 导出全读它
export function timeFeaturePlan(opts) {
  const chosen = new Set(opts)
  const dims = TIME_DIMS.filter(o => chosen.has(o))
  const cycDims = CYCLE_DIMS.filter(o => chosen.has(o) && chosen.has(`sincos_${o}`))
  const keep = chosen.has(KEEP_ORIGINAL_OPT)
  const skipped = keep ? [] : cycDims
  const plainCols = dims.filter(o => !skipped.includes(o)).map(o => `feat_${o}`)
  const cycCols = cycDims.flatMap(o => [`feat_${o}_sin`, `feat_${o}_cos`])
  return { dims, cycDims, keep, skipped, keys: [...plainCols, ...cycCols] }
}

// 审计链里的那串 token：dims + cyclical_<维度> +（不保留原列时）replace_original。
// 三者合起来无损还原 (dims, cycDims, keep)，回放与导出脚本都靠它复原同一批列。
export function timePlanDetail(opts) {
  const plan = timeFeaturePlan(opts)
  return [...plan.dims, ...plan.cycDims.map(o => `cyclical_${o}`),
    ...(plan.keep || plan.cycDims.length === 0 ? [] : ['replace_original'])].join(',')
}

export function parseTimeDetail(detail) {
  const toks = String(detail || '').split(',').filter(Boolean)
  const replace = toks.includes('replace_original')
  const cyc = toks.filter(t => t.startsWith('cyclical_')).map(t => t.slice('cyclical_'.length))
  const dims = toks.filter(t => !t.startsWith('cyclical_') && t !== 'replace_original')
  return [...dims, ...cyc.map(o => `sincos_${o}`), ...(replace ? [] : [KEEP_ORIGINAL_OPT])]
}

// 供 UI 展示「将生成哪些列」，与实际构建共用 timeFeaturePlan，数字与列名同源于一次计算
export function timePlanLabel(opts) {
  const plan = timeFeaturePlan(opts)
  return {
    dims: plan.dims,
    cyc: plan.cycDims.map(o => `${CYCLE_CN[o]}(${CYCLE_PERIOD[o]})`),
    cycDims: plan.cycDims,
    sinCosCols: plan.cycDims.length * 2,
    dropped: plan.keep ? [] : plan.cycDims,
    keys: plan.keys,
    total: plan.keys.length,
    daySkipped: plan.dims.includes('day') && plan.cycDims.length > 0
  }
}

// 四个 Tab 的构建命令统一走 runOp：明细留在后端帧上算，响应回带最新列注册表，
// state.features 由 applyMeta → syncFeatureList 从注册表派生（见上面的注释）。

// 同一族重新生成时，后端会把上一批里没再勾选的列退场（否则取消「保留数值原列」之后
// feat_hour 还留在表里）。退了几颗必须写进审计，不然界面上就是凭空少列。
export function featureRemovedNote(x) {
  const n = (x?.removed || []).length
  if (!n) return ''
  return ` · 换出 ${n} 列：${(x.removed || []).slice(0, 4).join('、')}${n > 4 ? ' 等' : ''}`
}

export async function buildTimeFeatures(opts) {
  const plan = timeFeaturePlan(opts)
  if (plan.keys.length === 0) return null
  const detail = timePlanDetail(opts)
  const r = await runOp('feature-time', {
    dims: plan.dims, cyclical: false, cycDims: plan.cycDims, keepCycOriginal: plan.keep
  }, {
    step: 5, icon: 'wand', title: '时间日历特征',
    detail: x => `${x.cols} 列（新增 ${x.created}）: ${x.keys.join(', ')}${featureRemovedNote(x)}`,
    params: x => ({ type: 'feature_build', featureType: 'time', detail,
      // 用的就是工作区当时生效的那份表：整份记下来，脱离本项目也能复现 feat_holiday
      holidayDays: x.holidays?.used ? x.holidays.days : null })
  }, { long: true })
  return r && { cols: r.cols, created: r.created, removed: (r.removed || []).length,
    removedNote: featureRemovedNote(r),
    sinCos: plan.cycDims.length * 2, dropped: plan.keep ? 0 : plan.cycDims.length }
}

// 滚动统计量：键是勾选框/日志里的 fn，值是进入列名的简写（median → med 沿用既有命名）
export const ROLL_STATS = { mean: 'mean', std: 'std', max: 'max', min: 'min', median: 'med' }

export function normEwmSpan(v) {
  const n = Math.round(Number(v))
  return Number.isFinite(n) && n >= 2 ? Math.min(n, 500) : 12
}

// 滞后与滑动窗口拆成两次独立生成（group='lag' / 'window'）：同一份表单里两组参数各按自己的按钮提交，
// 想要哪半边就点哪半边的按钮，不必为了只生成滞后而把窗口清空。
// 所有窗口类特征（lag / rolling / expanding / ewm）统一不含当前行，
// 等价于 pandas 的 .shift(1)，避免用 t 时刻的值预测 t 时刻的标签。
export function lagFeaturePlan(targetCols, params, group) {
  const span = normEwmSpan(params.ewmSpan)
  const keys = []
  targetCols.forEach(col => {
    if (group !== 'window') (params.lags || []).forEach(s => keys.push(`lag_${col}_t${s}`))
    if (group !== 'lag') {
      ;(params.windows || []).forEach(w => (params.stats || []).forEach(fn => {
        if (ROLL_STATS[fn]) keys.push(`roll_${ROLL_STATS[fn]}_${col}_w${w}`)
      }))
      if (params.expanding) keys.push(`expanding_mean_${col}`)
      if (params.ewm) keys.push(`ewm_${col}_s${span}`)
    }
  })
  return [...new Set(keys)]
}

const LAG_DEFAULTS = { cols: [], lags: [], windows: [], stats: [], expanding: false, ewm: false, ewmSpan: 12, group: null }

export function lagPlanDetail(targetCols, params, group) {
  return [
    `cols:${targetCols.join(',')}`,
    group === 'window' ? '' : `lag:${(params.lags || []).join(',')}`,
    group === 'lag' ? '' : `roll:${(params.windows || []).join(',')}`,
    group === 'lag' ? '' : `stats:${(params.stats || []).join(',')}`,
    group === 'lag' ? '' : `exp:${params.expanding ? 1 : 0}`,
    group === 'lag' ? '' : `ewm:${params.ewm ? normEwmSpan(params.ewmSpan) : 0}`,
    group ? `group:${group}` : ''
  ].filter(Boolean).join('|')
}

export function parseLagDetail(detail) {
  const f = {}
  String(detail || '').split('|').forEach(t => {
    const i = t.indexOf(':')
    if (i > 0) f[t.slice(0, i)] = t.slice(i + 1)
  })
  const list = s => (s ? s.split(',').filter(Boolean) : [])
  const nums = s => list(s).map(Number).filter(n => Number.isFinite(n) && n > 0)
  const group = ['lag', 'window'].includes(f.group) ? f.group : null
  return {
    ...LAG_DEFAULTS,
    cols: list(f.cols),
    lags: nums(f.lag),
    windows: nums(f.roll),
    stats: list(f.stats).filter(s => ROLL_STATS[s]),
    expanding: f.exp === '1',
    ewm: !!f.ewm && f.ewm !== '0',
    ewmSpan: f.ewm && f.ewm !== '0' ? normEwmSpan(f.ewm) : 12,
    group
  }
}

export async function buildLagFeatures(targetCols, params, group) {
  const keys = lagFeaturePlan(targetCols, params, group)
  if (keys.length === 0) return null
  const detail = lagPlanDetail(targetCols, params, group)
  const head = { lag: '滞后特征', window: '滑动窗口特征' }[group] || '滞后与滑动窗口特征'
  // 后端按组校验「不许夹带另一组的参数」：所以发出去的参数集只留这一组真正会生成的部分，
  // 界面上另一组的输入框此时归滑动窗口那个按钮管，不跟这次提交走。
  const send = {
    cols: targetCols,
    lags: group === 'window' ? [] : (params.lags || []),
    windows: group === 'lag' ? [] : (params.windows || []),
    stats: group === 'lag' ? [] : (params.stats || []),
    expanding: group === 'lag' ? false : !!params.expanding,
    ewm: group === 'lag' ? false : !!params.ewm,
    ewmSpan: normEwmSpan(params.ewmSpan),
    group
  }
  const r = await runOp('feature-lag', send, {
    step: 5, icon: 'wand', title: head,
    detail: x => `${x.cols} 列（新增 ${x.created}）· ${detail}${featureRemovedNote(x)}`,
    // 组名同时决定后端的特征族（lag / window）与这份日志的归族；缺 group 的老记录重放仍是 lag_roll
    params: { type: 'feature_build', featureType: group || 'lag_roll', detail }
  }, { long: true })
  return r && { cols: r.cols, created: r.created, removed: (r.removed || []).length, removedNote: featureRemovedNote(r) }
}

// 差分与频域同理：各自一次生成，featureType 记 'diff' / 'fft'，未分组的老记录仍是 'diff_freq'
export function diffFeaturePlan(targetCols, params, group) {
  const keys = []
  if (group !== 'fft') {
    targetCols.forEach(col => {
      if (params.d1) keys.push(`diff1_${col}`)
      if (params.d2) keys.push(`diff2_${col}`)
      if (params.seasonal) keys.push(`diff_season${params.period}_${col}`)
    })
  }
  const fftCol = targetCols[0]
  if (fftCol && group !== 'diff') {
    if (params.fftDominant) keys.push(`fft_top1_${fftCol}`, `fft_top2_${fftCol}`, `fft_top3_${fftCol}`)
    if (params.fftEntropy) keys.push(`fft_entropy_${fftCol}`)
    if (params.fftPowerRatio) keys.push(`fft_power_ratio_${fftCol}`)
  }
  return [...new Set(keys)]
}

export function diffPlanDetail(targetCols, params, group) {
  const fft = []
  if (params.fftDominant) fft.push('dom')
  if (params.fftEntropy) fft.push('ent')
  if (params.fftPowerRatio) fft.push('pow')
  return [
    `cols:${targetCols.join(',')}`,
    group === 'fft' ? '' : `d1:${params.d1 ? 1 : 0}`,
    group === 'fft' ? '' : `d2:${params.d2 ? 1 : 0}`,
    group === 'fft' ? '' : `seas:${params.seasonal ? Math.max(1, Math.round(params.period) || 1) : 0}`,
    group === 'diff' ? '' : `fft:${fft.join(',')}`,
    group ? `group:${group}` : ''
  ].filter(Boolean).join('|')
}

export function parseDiffDetail(detail) {
  const f = {}
  String(detail || '').split('|').forEach(t => {
    const i = t.indexOf(':')
    if (i > 0) f[t.slice(0, i)] = t.slice(i + 1)
  })
  const cols = (f.cols || '').split(',').filter(Boolean)
  const period = Math.max(1, parseInt(f.seas) || 0)
  const fft = (f.fft || '').split(',').filter(Boolean)
  return {
    cols,
    d1: f.d1 === '1',
    d2: f.d2 === '1',
    seasonal: period > 0,
    period,
    fftDominant: fft.includes('dom'),
    fftEntropy: fft.includes('ent'),
    fftPowerRatio: fft.includes('pow'),
    group: ['diff', 'fft'].includes(f.group) ? f.group : null
  }
}

export async function buildDiffFeatures(targetCols, params, group) {
  const keys = diffFeaturePlan(targetCols, params, group)
  if (keys.length === 0) return null
  const detail = diffPlanDetail(targetCols, params, group)
  const head = { diff: '差分平稳化特征', fft: '频域特征' }[group] || '差分与频域特征'
  const r = await runOp('feature-diff', {
    cols: targetCols,
    d1: group === 'fft' ? false : !!params.d1,
    d2: group === 'fft' ? false : !!params.d2,
    seasonal: group === 'fft' ? false : !!params.seasonal,
    period: Math.max(1, Math.round(params.period) || 1),
    fftDominant: group === 'diff' ? false : !!params.fftDominant,
    fftEntropy: group === 'diff' ? false : !!params.fftEntropy,
    fftPowerRatio: group === 'diff' ? false : !!params.fftPowerRatio,
    group
  }, {
    step: 5, icon: 'wand', title: head,
    detail: x => `${x.cols} 列（新增 ${x.created}）· ${detail}${featureRemovedNote(x)}`,
    params: { type: 'feature_build', featureType: group || 'diff_freq', detail }
  }, { long: true })
  return r && { cols: r.cols, created: r.created, removed: (r.removed || []).length, removedNote: featureRemovedNote(r) }
}

// 类别列的取值分布由后端整表数出（/value-counts）：浏览器不留整表，
// 「共计多少个取值 / 各占多少行」这类数字必须由持有全量帧的一侧给出。
const catDistCache = new Map()

export async function catColumnDistribution(selectedKeys, method) {
  const d = ds()
  if (!d?.wsId || selectedKeys.length === 0) return null
  // 类别列的取值分布只随数值代作废：第五步生成特征（不改既有数值）时这份分布照样成立，
  // 用整表版本号作键会让每点一次「生成分组统计」都重打一次整表 value_counts。
  const cacheKey = `${d.wsId}|${selectedKeys.sort().join(',')}|${method}|` +
    `${d.meta?.valueEpoch ?? `v${d.meta?.version ?? -1}`}#${d.meta?.rowCount ?? 0}`
  const hit = catDistCache.get(cacheKey)
  if (hit) return hit
  try {
    const r = await ws.wsValueCounts(d.wsId, selectedKeys, method)
    const rows = r.columns.map(c => ({
      label: c.label, key: c.key,
      uniqueVals: c.uniqueVals,
      topVals: c.uniqueVals.slice(0, 5).map(v => `${v}(${c.counts[v]})`).join(', '),
      counts: c.counts,
      total: c.total,
      uniqueTotal: c.uniqueTotal,
      nonMissingRows: c.nonMissingRows,
      restCount: c.restCount,
      truncated: c.truncated
    }))
    const out = { rows, totalNewCols: r.totalNewCols, rowCount: r.rowCount }
    if (catDistCache.size > 24) catDistCache.clear()   // 键里带数值代，每改一次值都会添一条
    catDistCache.set(cacheKey, out)
    return out
  } catch (e) {
    // 失败原因随结果带回，由编码卡常驻显示（这里再弹一条 toast 就是把同一句话讲两遍）
    return { rows: [], totalNewCols: 0, rowCount: 0, error: e.message }
  }
}

export function catPlanDetail(selectedKeys, method) {
  return `cols:${selectedKeys.join(',')}|method:${method}`
}

export function parseCatDetail(detail) {
  const f = {}
  String(detail || '').split('|').forEach(t => {
    const i = t.indexOf(':')
    if (i > 0) f[t.slice(0, i)] = t.slice(i + 1)
  })
  const method = ['onehot', 'ordinal', 'target'].includes(f.method) ? f.method : 'onehot'
  return { cols: (f.cols || '').split(',').filter(Boolean), method }
}

export async function buildCatFeatures(selectedKeys, method) {
  const detail = catPlanDetail(selectedKeys, method)
  const r = await runOp('feature-cat', { cols: selectedKeys, method }, {
    step: 5, icon: 'wand', title: '类别特征编码',
    detail: x => `${x.cols} 列（新增 ${x.created}）· ${detail}${featureRemovedNote(x)}`,
    // 独热列名来自数据取值，构建当场把服务端算出的键序记进审计参数：
    // 整表不再常驻浏览器，导出 Python 时只有这一份记录能复现同一批列名与列序。
    // targetColumn 同理——目标均值编码的参照列由服务端挑，界面看不到挑中的是哪一个。
    params: x => ({ type: 'feature_build', featureType: 'cat', detail,
      onehotKeys: method === 'onehot' ? x.keys : null,
      targetColumn: x.targetColumn || null })
  }, { long: true })
  return r && { cols: r.cols, created: r.created, removed: (r.removed || []).length, removedNote: featureRemovedNote(r) }
}

// 「首个完整行」：长窗口特征在前 N 行必然为空（窗口还没盖到）。整列扫描在后端 /first-complete 做，
// 浏览器只拿回一个行号——旧写法是 wsColumns 拉 5000 行 × 列数回来自己找。
export async function firstCompleteRow(newKeys, scanRows = 5000) {
  const d = ds()
  if (!d?.wsId || newKeys.length === 0) return null
  if (!requireBackend('定位首个完整行')) return null
  state.busy = '正在扫描新增特征的取值…'
  try {
    const r = await ws.wsFirstComplete(d.wsId, newKeys, scanRows)
    return { index: r.index, scanned: r.scanned }
  } catch (e) {
    toast('error', `定位首个完整行失败：${e.message}`)
    return null
  } finally {
    state.busy = ''
  }
}

export async function renameFeature(idx, newLabel) {
  const feat = state.features[idx]
  const oldKey = feat.key
  // 原始列的键不能改（只换显示名）；特征列的键由标签推导，冲突时加时间戳后缀，
  // 键由前端算好显式下发，服务端才不会按自己的 safe_key 规则另起一个名
  let newKey = oldKey
  if (feat.isNew) {
    const safeKey = newLabel.toLowerCase().replace(/[^a-z0-9_]/g, '_').replace(/_+/g, '_')
    const conflict = state.features.some((f, i) => i !== idx && f.key === safeKey)
    newKey = conflict ? safeKey + '_' + Date.now().toString(36) : safeKey
  }
  // 改名会连带换掉导出宽表的列键，不进审计链就复现不出同一张表
  const r = await runOp('rename-column', { key: oldKey, label: newLabel, newKey }, {
    step: 5, icon: 'rename', title: '重命名特征列',
    detail: x => `"${oldKey}" → "${x.newKey}"`,
    params: { type: 'feature_rename', oldKey, newKey, newLabel }
  })
  return r ? newKey : null
}

// 特征登记表由后端列注册表派生，撤销 = 让服务端删掉那一列（applyMeta 会把清单同步回来）
export async function dropFeature(idx) {
  const feat = state.features[idx]
  // 撤销特征只针对生成的列；原始列的删除入口在第二步「数据快照预览」的列头，
  // 原话写的「第一步 · 列管理」那个地方并不存在
  if (!feat.isNew) { toast('warning', '这是原始数据列，撤销只管生成的特征列'); return false }
  const r = await runOp('delete-column', { key: feat.key }, {
    step: 5, icon: 'delete', title: '撤销特征列',
    detail: () => `"${feat.label}" (${feat.key})`,
    params: { type: 'feature_drop', key: feat.key, label: feat.label }
  })
  return !!r
}

// ============================================================
// 操作日志：导出 / 导入 / 回放
// ============================================================
export function exportActionLog() {
  const ops = wsActionLog()
  if (ops.length === 0) { toast('warning', '没有可导出的操作记录'); return false }
  const d = ds()
  const exportData = {
    version: '1.0',
    exportedAt: new Date().toISOString(),
    datasetKey: state.currentKey,
    datasetName: d?.name || '',
    datasetRows: d?.meta?.rowCount || 0,
    datasetCols: d?.columns?.length || 0,
    datasetWsId: d?.wsId || null,
    datasetVersion: d?.meta?.version ?? null,
    operations: ops.map(e => ({
      step: e.step, icon: e.icon, title: e.title, detail: e.detail, params: e.params, time: e.time
    }))
  }
  downloadBlob(JSON.stringify(exportData, null, 2), `timeseries_pipeline_${new Date().toISOString().slice(0, 10)}.json`, 'application/json')
  toast('success', `已导出 ${exportData.operations.length} 条操作记录`)
  return true
}

export function importActionLog(text) {
  let data
  try {
    data = JSON.parse(text)
  } catch (e) {
    toast('error', `文件解析失败: ${e.message}`)
    return false
  }
  if (!data.operations || !Array.isArray(data.operations)) {
    toast('error', '文件格式错误：缺少 operations 数组')
    return false
  }
  state.importedLog = data
  state.actionLog = data.operations.map((op, idx) => ({
    id: 'imported_' + idx,
    step: op.step, icon: op.icon, title: op.title, detail: op.detail,
    params: op.params, time: op.time || '', ts: 0, imported: true
  }))
  toast('success', `已导入 ${data.operations.length} 条操作记录（${data.datasetName || data.datasetKey || '未知数据集'}）`)
  return true
}

// 回放单条操作；返回 true=已执行，false=跳过
// 异步：第一步/第二步的操作现在都打在后端工作区上，回放必须走同一个入口才能留下同样的审计记录
async function replaySingleOp(op) {
  const d = ds()
  if (!d || !op.params) return false
  const p = op.params
  const relabel = t => { const last = state.actionLog[state.actionLog.length - 1]; if (last) last.title = t }
  switch (p.type) {
    case 'time_col': {
      if (!p.key) return false
      if (d.timeCol === p.key) return true   // 已经是那一列：重放不需要再做一次
      const r = await setTimeColumn(p.key, p.srcFmt)
      if (!r) return false
      relabel('[回放] 指定时间列')
      return true
    }
    case 'time_convert': {
      const fmt = p.targetFmt || 'YYYY-MM-DD HH:mm:ss'
      const changed = await convertTimeColumn(p.timeCol || d.timeCol, fmt, p.customFmt)
      if (changed === null) return false
      relabel('[回放] 时间格式转换')
      return true
    }
    case 'resample': {
      if (!p.targetRate || !p.method) return false
      const r = await resampleDataset(p.targetRate, p.method)
      if (!r) return false
      relabel('[回放] 重采样')
      return true
    }
    case 'multi_calc': {
      if (!p.terms || !p.name || p.terms.length < 2) return false
      if (d.columns.some(c => c.key === p.name)) return true // 已存在，视为成功
      const r = await applyDerivedCol(p.name, p.terms)
      if (!r) return false
      relabel(`[回放] 多列运算: ${p.name}`)
      return true
    }
    case 'impute_segment': {
      if (!p.colKey || !Array.isArray(p.segments) || p.segments.length === 0) return false
      // 段号原样打回服务端：填补只动区间内的 NaN，回放顺序与记录顺序一致时结果同一份
      const r = await runImpute({
        targets: p.segments.map(s => ({ key: p.colKey, startIdx: s.startIdx, endIdx: s.endIdx, algo: s.algo || 'linear' }))
      }, {
        step: 4, icon: 'impute', title: `[回放] 缺失值填补: ${p.colKey}`,
        detail: x => x.summary, params: p
      })
      return !!r
    }
    case 'impute_all': {
      const r = await imputeAllAndDedupe(p.dedupe || 'mean', p.defaultAlgo || 'linear')
      if (!r) return false
      relabel('[回放] 一键填补去重')
      return true
    }
    case 'anomaly_repair': {
      // 修复按检测时的行索引执行，所以回放必须先按记录的算法/表达式/超参重测一遍，
      // 并且逐列核对异常点数：数目对不上就不是同一份检测，宁可不修也不能修错点
      if (!p.repair || !p.algo) return false
      const det = await detectAnomalies(p.algo, p.expr || '', p.iforest || {})
      if (!det) return false
      const counted = {}
      ;(det.results || []).forEach(x => { counted[x.key] = x.anomalies })
      if ((p.columns || []).some(c => (counted[c.key] || 0) !== c.count)) return false
      const r = await repairAnomalies(p.repair)
      if (!r) return false
      relabel('[回放] 异常修复')
      return true
    }
    case 'mask_generate': {
      if (!p.maskName || p.startIdx === undefined || p.endIdx === undefined) return false
      const ones = await generateMask(p.maskName, { startIdx: p.startIdx, endIdx: p.endIdx })
      if (ones === null) return false
      relabel(`[回放] 掩码: ${p.maskName}`)
      return true
    }
    case 'rename_column': {
      if (!p.oldKey || !p.newLabel) return false
      const idx = ds().columns.findIndex(c => c.key === p.oldKey)
      if (idx < 0) return false
      // 与界面操作走同一函数：后端改帧 + 特征登记表一起改
      if (!await renameColumn(idx, p.newLabel)) return false
      relabel('[回放] 重命名')
      return true
    }
    case 'delete_column': {
      if (!p.key) return false
      const idx = ds().columns.findIndex(c => c.key === p.key)
      if (idx < 0) return true  // 列已不在，回放后状态与记录一致
      if (!await deleteColumn(idx)) return false
      relabel('[回放] 删除列')
      return true
    }
    case 'feature_drop': {
      if (!p.key) return false
      const fi = state.features.findIndex(f => f.key === p.key)
      if (fi < 0) return true  // 该列已不在登记表里，回放后状态与记录一致
      if (!await dropFeature(fi)) return false
      relabel('[回放] 撤销特征列')
      return true
    }
    case 'feature_rename': {
      if (!p.oldKey || !p.newLabel) return false
      let fi = state.features.findIndex(f => f.key === p.oldKey)
      if (fi < 0) {
        fi = state.features.findIndex(f => f.key === p.newKey)
        return fi >= 0  // 已按改名后的键存在，视为与记录一致
      }
      if (!await renameFeature(fi, p.newLabel)) return false
      relabel('[回放] 重命名特征列')
      return true
    }
    case 'unit_convert': {
      if (!p.key || typeof p.factor !== 'number') return false
      const idx = ds().columns.findIndex(c => c.key === p.key)
      if (idx < 0) return false
      if (!await convertColumnUnit(idx, p.factor, typeof p.offset === 'number' ? p.offset : 0, p.newUnit || '')) return false
      relabel('[回放] 单位转换')
      return true
    }
    case 'exo-preset': {
      // 记录里存的是 presetKey + 服务端补的 seed：重放把同一个 seed 打回去，得到同一串模拟值
      if (!p.presetKey) return false
      const alias = p.key && p.key !== p.presetKey ? p.key : ''
      if (!await addExoPreset(p.presetKey, alias, { seed: p.seed })) return false
      relabel(`[回放] 预设模板外生变量: ${p.key || p.presetKey}`)
      return true
    }
    case 'exo-formula': {
      if (!p.key || !p.expr) return false
      if (!(await addExoFormula(p.key, p.expr)).ok) return false
      relabel(`[回放] 公式生成外生变量: ${p.key}`)
      return true
    }
    case 'exo-file': {
      // 侧表在 inspect 那一步已落进服务端数据集目录，重放靠文件名 + 内容指纹重新挂一遍；
      // 文件被覆盖过会直接报错，不会静默换一列数据
      if (!p.filename || !p.sha || !p.sideTimeCol) return false
      if (!await attachSideTable({
        filename: p.filename, sha: p.sha, sideTimeCol: p.sideTimeCol,
        mode: p.mode || 'left', toleranceMinutes: p.toleranceMinutes, targets: p.targets || []
      })) return false
      relabel(`[回放] 侧表挂列: ${p.filename}`)
      return true
    }
    case 'mask_delete': {
      if (!p.maskKey) return false
      const idx = state.masks.findIndex(m => m.key === p.maskKey)
      if (idx < 0) return true
      if (!await deleteMask(idx)) return false
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = `[回放] 删除掩码列: ${p.maskKey}`
      return true
    }
    case 'mask_delete_all': {
      if (!Array.isArray(p.keys)) return false
      if (state.masks.length === 0) return true
      if (!await deleteAllMasks()) return false
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = `[回放] 清空全部掩码`
      return true
    }
    case 'feature_build': {
      // 四个 Tab 都打在后端同一批 op 上：回放与界面点构建走的是同一条路
      if (['lag_roll', 'lag', 'window'].includes(p.featureType) && p.detail) {
        // detail 里带完整计划（目标列/阶数/窗口/统计量/expanding/ewm span/组别），复原后走同一个构建函数
        const plan = parseLagDetail(p.detail)
        if (plan.cols.length === 0) return false
        if (!await buildLagFeatures(plan.cols, {
          lags: plan.lags, windows: plan.windows, stats: plan.stats,
          expanding: plan.expanding, ewm: plan.ewm, ewmSpan: plan.ewmSpan
        }, plan.group)) return false
      } else if (p.featureType === 'time' && p.detail) {
        // 日志记的是展开后的列计划（cyclical_hour / replace_original 这类），还原成勾选态再复用同一构建函数
        if (!await buildTimeFeatures(parseTimeDetail(p.detail))) return false
      } else if (['diff_freq', 'diff', 'fft'].includes(p.featureType) && p.detail) {
        const plan = parseDiffDetail(p.detail)
        if (plan.cols.length === 0) return false
        if (!await buildDiffFeatures(plan.cols, plan, plan.group)) return false
      } else if (p.featureType === 'cat' && p.detail) {
        const plan = parseCatDetail(p.detail)
        if (plan.cols.length === 0) return false
        if (!await buildCatFeatures(plan.cols, plan.method)) return false
      } else {
        // 未登记的构建类型一律按「无法回放」处理，绝不写一条假成功日志蒙过去
        return false
      }
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = `[回放] 特征工程 · ${p.featureType || ''}`
      touch()
      return true
    }
    case 'split': {
      if (typeof p.ratio !== 'number') return false
      lastLoggedSplit = null  // 回放必须留下条目，不能被「同一比例只记一次」合并掉
      setSplitRatio(p.ratio)  // 走同一入口：界面比例真的被还原
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = '[回放] 设置切分比例'
      return true
    }
    case 'split_apply': {
      if (typeof p.ratio !== 'number') return false
      if (!await applySplitColumn(p.ratio)) return false
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = '[回放] 生成数据集划分列'
      return true
    }
    case 'holidays': {
      if (!Array.isArray(p.days)) return false
      if (!await saveHolidays(p.days, p.source || 'replay')) return false
      const last = state.actionLog[state.actionLog.length - 1]
      last.title = '[回放] 配置节假日表'
      return true
    }
    default:
      return false
  }
}

let replayTimer = null

export function replayActionLog() {
  // 没有导入记录时「回放执行」按钮 disabled
  if (!state.importedLog || !state.importedLog.operations) return
  const ops = state.importedLog.operations
  ElMessageBox.confirm(
    `将执行 ${ops.length} 条记录，当前数据会被改写。确认？`,
    '回放确认',
    { type: 'warning', confirmButtonText: '开始回放', cancelButtonText: '取消' }
  ).then(async () => {
    state.actionLog = []
    let idx = 0, succeeded = 0, failed = 0
    state.replayStatus = { text: `回放中 (1/${ops.length})...` }
    const runNext = async () => {
      if (idx >= ops.length) {
        state.replayStatus = { text: `回放完成：${succeeded} 成功，${failed} 跳过` }
        touch()
        setTimeout(() => { state.replayStatus = null }, 3000)
        state.importedLog = null
        replayTimer = null
        return
      }
      const op = ops[idx]
      state.replayStatus = { text: `回放中 (${idx + 1}/${ops.length}): ${op.title}` }
      const before = state.actionLog.length
      try {
        if (await replaySingleOp(op)) {
          succeeded++
          // 回放补写的记录要带上原参数，否则这条流程再导出 Python 时会整段消失
          if (op.params) {
            for (let k = before; k < state.actionLog.length; k++) {
              if (!state.actionLog[k].params) state.actionLog[k].params = op.params
            }
          }
        } else { failed++; logAction(op.step || 2, 'switch', `[跳过] ${op.title}`, '无法在当前数据状态下重放该操作') }
      } catch (e) {
        failed++
        logAction(op.step || 2, 'switch', `[失败] ${op.title}`, String(e.message || e))
      }
      idx++
      replayTimer = setTimeout(runNext, 80)
    }
    runNext()
  }).catch(() => {})
}

export function clearImportedLog() {
  state.importedLog = null
  state.replayStatus = null
  if (replayTimer) { clearTimeout(replayTimer); replayTimer = null }
}

// ============================================================
// Python Pipeline 代码生成（仅包含本次会话真实执行过的操作）
// ============================================================
// 外生变量公式（buildVarEvaluator 的白名单语言）→ pandas 向量表达式。
// 单遍替换标识符，避免 np.exp 里的 exp 被二次映射。
const PY_FORMULA_TOKENS = {
  PI: 'np.pi', E: 'np.e',
  sin: 'np.sin', cos: 'np.cos', tan: 'np.tan', abs: 'np.abs', sqrt: 'np.sqrt',
  log: 'np.log', log2: 'np.log2', log10: 'np.log10', exp: 'np.exp',
  pow: 'np.power', floor: 'np.floor', ceil: 'np.ceil', round: 'np.round',
  min: 'np.minimum', max: 'np.maximum', asin: 'np.arcsin', acos: 'np.arccos',
  atan: 'np.arctan', hypot: 'np.hypot',
  hour: '@@HOUR@@', day: '@@DAY@@', month: '@@MONTH@@', weekday: '@@WEEKDAY@@', idx: '@@IDX@@'
}

function pyVarFormula(expr, tc) {
  if (!expr || /&&|\|\||!|\?/.test(expr) || /Math\./.test(expr)) return null
  const mapped = String(expr).replace(/[A-Za-z_]\w*/g, t => (PY_FORMULA_TOKENS[t] !== undefined ? PY_FORMULA_TOKENS[t] : t))
  return mapped
    .replace(/@@HOUR@@/g, `(df['${tc}'].dt.hour + df['${tc}'].dt.minute / 60)`)
    .replace(/@@DAY@@/g, `df['${tc}'].dt.day`)
    .replace(/@@MONTH@@/g, `df['${tc}'].dt.month`)
    // JS getDay() 0=周日，pandas dayofweek 0=周一
    .replace(/@@WEEKDAY@@/g, `((df['${tc}'].dt.dayofweek + 1) % 7)`)
    .replace(/@@IDX@@/g, 'np.arange(len(df))')
}

export function generatePythonCode() {
  const d = ds()
  const timeCol = d.timeCol || 'timestamp'
  // 时间列可能被改过名：加载段要用文件里的原始列名，后续段落跟着改名时点换名
  let loadTimeCol = timeCol
  wsActionLog().slice().reverse().forEach(e => {
    const rp = e.params
    if (!rp || rp.type !== 'rename_column') return
    const nk = rp.newKey || String(rp.newLabel || '').toLowerCase().replace(/[^a-z0-9_]/g, '_')
    if (nk === loadTimeCol) loadTimeCol = rp.oldKey
  })
  let tc = loadTimeCol
  const lines = []
  lines.push('# TimeSeries Feature Engineering Pipeline')
  lines.push('# 由 TimeSeries Studio 根据本次会话真实执行的操作生成')
  lines.push('import pandas as pd')
  lines.push('import numpy as np')
  lines.push('')
  lines.push('# 0. 加载数据')
  const base = (d.name || 'dataset').replace(/[^\w.-]/g, '_')
  const fmt = d.format || 'csv'
  if (fmt === 'parquet') lines.push(`df = pd.read_parquet("${base}.parquet", engine="pyarrow")`)
  else if (fmt === 'feather') lines.push(`df = pd.read_feather("${base}.feather")`)
  else if (fmt === 'xlsx' || fmt === 'xls') lines.push(`df = pd.read_excel("${base}.xlsx", engine="openpyxl")`)
  else lines.push(`df = pd.read_csv("${base}.csv")`)
  // 读入时按后端真正用的那个源格式解析（GET /time-detect 的 format），不写 format 就等于
  // 让 pandas 自己猜 —— 它会做出与界面不同的决定，导出脚本就复现不了界面上的数字了。
  const srcFmt = d.timeDetect && !String(d.timeDetect.format).startsWith('epoch_')
    && d.timeDetect.format ? pyStrftime(d.timeDetect.format) : ''
  const srcUnit = String(d.timeDetect?.format || '').startsWith('epoch_')
    ? (d.timeDetect.format === 'epoch_ms' ? 'ms' : 's') : ''
  lines.push(`df['${loadTimeCol}'] = pd.to_datetime(df['${loadTimeCol}']` +
    `${srcUnit ? `, unit='${srcUnit}'` : srcFmt ? `, format='${srcFmt}'` : ''})`)
  lines.push(`df = df.sort_values('${loadTimeCol}').reset_index(drop=True)`)

  let section = 1
  const nextHeader = (title) => {
    lines.push('')
    lines.push(`# ${section}. ${title}`)
    section++
  }

  const ops = wsActionLog().filter(e => e.params && e.params.type)
  const done = new Set()
  // 特征列可以撤销后，同一份构建计划可能出现「建 → 撤 → 再建」。若不去掉重复构建的抑制，
  // pandas 侧就少一次建列、与界面当前列集合不符。dropsSeen 统计自上次建列块以来的撤销次数，
  // rebuildRound 用来区分不同轮次的同名 drop（同一列可以反复被建又被撤）。
  let dropsSeen = 0
  let rebuildRound = 0
  let dftHelpers = false
  // 第四步的产物（逐段填补、按解析时间戳去重、异常修复）在 pandas 里要有同一套语义，
  // 否则脚本跑出来的数字与界面显示的不会是同一份数据。这段辅助函数只在出现第四步操作时发出。
  let qualityHelpers = false
  const ensureQualityHelpers = () => {
    if (qualityHelpers) return
    qualityHelpers = true
    lines.push('')
    lines.push('# ---- 质量清洗辅助函数：与后端 app/services/quality.py 逐条对齐 ----')
    lines.push('from decimal import Decimal, ROUND_HALF_UP')
    lines.push('')
    lines.push('def _r4(x):')
    lines.push('    """等价于后端 round4 / JS parseFloat(v.toFixed(4))：按二进制精确值远离 0 进位。"""')
    lines.push("    return float(Decimal(float(x)).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))")
    lines.push('')
    lines.push('def _col(series):')
    lines.push('    """列 → list（缺失为 None），非数值单元格按缺失处理，同后端 numeric_array。"""')
    lines.push("    return [None if pd.isna(v) else float(v) for v in pd.to_numeric(series, errors='coerce').tolist()]")
    lines.push('')
    lines.push('def _fill_run(vals, start, end, algo):')
    lines.push('    """就地填补 vals[start..end] 内的空位，返回填补个数。')
    lines.push('')
    lines.push('    与后端 quality.fill_run 同语义：左右观测值从区间外侧查找，所以区间内的观测值不动；')
    lines.push('    左侧没有观测值时以 0 起算（不外推也不留空），spline 用 smoothstep -2t^3+3t^2。')
    lines.push('    """')
    lines.push('    n = len(vals)')
    lines.push('    start, end = max(0, start), min(n - 1, end)')
    lines.push('    targets = [i for i in range(start, end + 1) if vals[i] is None]')
    lines.push('    if not targets:')
    lines.push('        return 0')
    lines.push('    left = [i for i in range(0, start) if vals[i] is not None]')
    lines.push('    right = [i for i in range(end + 1, n) if vals[i] is not None]')
    lines.push('    before = left[-1] if left else None')
    lines.push('    after = right[0] if right else None')
    lines.push('    v_before = 0.0 if before is None else float(vals[before])')
    lines.push('    v_after = v_before if after is None else float(vals[after])')
    lines.push(`    if algo == 'zero':`)
    lines.push('        for i in targets:')
    lines.push('            vals[i] = 0.0')
    lines.push('        return len(targets)')
    lines.push(`    if algo == 'ffill':`)
    lines.push('        for i in targets:')
    lines.push('            vals[i] = v_before if before is not None else 0.0')
    lines.push('        return len(targets)')
    lines.push(`    if algo == 'spline':`)
    lines.push('        length = end - start + 1')
    lines.push('        for i in targets:')
    lines.push('            t = (i - start + 1) / (length + 1)')
    lines.push('            vals[i] = _r4(v_before + (v_after - v_before) * (-2 * t ** 3 + 3 * t * t))')
    lines.push('        return len(targets)')
    lines.push('    left_edge = before if before is not None else start - 1')
    lines.push('    right_edge = after if after is not None else end + 1')
    lines.push('    span = right_edge - left_edge')
    lines.push('    for i in targets:')
    lines.push('        pos = (i - before) if before is not None else (i - start + 1)')
    lines.push('        ratio = pos / span if span > 1 else 0.5')
    lines.push('        vals[i] = _r4(v_before + (v_after - v_before) * ratio)')
    lines.push('    return len(targets)')
    lines.push('')
    lines.push('def _fill_all(vals, algo):')
    lines.push('    """整列扫描缺失段并逐段填补（对应服务端 all=true 与修复时的重插值）。"""')
    lines.push('    filled, i = 0, 0')
    lines.push('    while i < len(vals):')
    lines.push('        if vals[i] is None:')
    lines.push('            j = i')
    lines.push('            while j < len(vals) and vals[j] is None:')
    lines.push('                j += 1')
    lines.push('            filled += _fill_run(vals, i, j - 1, algo)')
    lines.push('            i = j')
    lines.push('        else:')
    lines.push('            i += 1')
    lines.push('    return filled')
    lines.push('')
    lines.push('def _dedupe(frame, time_col, keys, strategy):')
    lines.push(`    \"\"\"重复时间戳合并：按【解析后的时间戳】分组（与后端 merge_duplicates 一致，`)
    lines.push('    不是按显示字符串），均值写回该组首行后只保留首行；NaT 之间互相算重复。\"\"\"')
    lines.push(`    ts = pd.to_datetime(frame[time_col], errors='coerce')`)
    lines.push('    if not ts.duplicated().any():')
    lines.push('        return frame, 0')
    lines.push('    first = ~ts.duplicated()')
    lines.push('    removed = int(frame.shape[0] - first.sum())')
    lines.push(`    if strategy == 'mean' and keys:`)
    lines.push('        num = pd.DataFrame({k: pd.to_numeric(frame[k], errors=\"coerce\").astype(\"float64\") for k in keys})')
    lines.push('        means = num.groupby(ts.to_numpy(), sort=False, dropna=False).mean()')
    lines.push('        out = frame.loc[first].copy()')
    lines.push('        positions = list(np.flatnonzero(first.to_numpy()))')
    lines.push('        for row, pos in enumerate(positions):')
    lines.push('            vals = means.iloc[row]')
    lines.push('            for key in keys:')
    lines.push('                v = vals[key]')
    lines.push('                if pd.isna(v):')
    lines.push('                    continue   # 整组皆缺失：保留原值（后端同样不写）')
    lines.push('                out.iat[pos, out.columns.get_loc(key)] = _r4(v)')
    lines.push('        return out.reset_index(drop=True), removed')
    lines.push(`    keep = 'first' if strategy == 'first' else 'last'`)
    lines.push(`    return frame.loc[~ts.duplicated(keep=keep)].reset_index(drop=True), removed`)
    lines.push('')
    lines.push('def _threshold_mask(series, lower, upper):')
    lines.push('    """缺失点不参与判定（与后端一致）。"""')
    lines.push(`    arr = pd.to_numeric(series, errors='coerce').to_numpy(dtype='float64')`)
    lines.push('    return ~np.isnan(arr) & ((arr < lower) | (arr > upper))')
    lines.push('')
    lines.push('def _mad_mask(series, window=33, min_periods=5, k=4.0):')
    lines.push('    """孤立森林（近似版）= 回看窗口 MAD 判据，与后端 _mad_mask 同一段 numpy 代码。"""')
    lines.push(`    arr = pd.to_numeric(series, errors='coerce').to_numpy(dtype='float64')`)
    lines.push('    n = arr.size')
    lines.push('    pad = np.concatenate((np.full(window - 1, np.nan), arr))')
    lines.push('    windows = np.lib.stride_tricks.sliding_window_view(pad, window)[:n]')
    lines.push('    counts = np.sum(~np.isnan(windows), axis=1)')
    lines.push('    med = np.nanmedian(windows, axis=1)')
    lines.push('    mad = np.nanmean(np.abs(windows - med[:, None]), axis=1)')
    lines.push('    anchors = (counts >= min_periods) & ~np.isnan(arr)')
    lines.push('    return anchors & ((arr < med - k * mad) | (arr > med + k * mad))')
  }
  // 窗口聚合与类别编码不能直接套 pandas 现成算子：rolling(min_periods) 会因窗口内有缺失而传播 NaN
  // （后端与迁移前的浏览器实现都是「剔除缺失后按有效值聚合」）；内置 round 是银行家舍入；
  // groupby.transform('mean') 的求和顺序也不是后端那套逐行累加。三者都会在 .005 边界上翻一个百分位，
  // 导出的脚本就会和界面各说一遍数字，所以这里发一份与 app/services/features.py 同口径的辅助函数。
  let featureHelpers = false
  const ensureFeatureHelpers = () => {
    if (featureHelpers) return
    featureHelpers = true
    lines.push('')
    lines.push('# ---- 特征辅助函数：与后端 app/services/features.py 的 rolling_agg / expanding_mean / ewm_prev / build_cat 逐条对齐 ----')
    lines.push('import math')
    lines.push('from decimal import Decimal, ROUND_HALF_UP')
    lines.push('')
    lines.push('def _rh(x, nd):')
    lines.push('    """JS 的 parseFloat(x.toFixed(nd))：按二进制精确值远离 0 进位（内置 round 是银行家舍入，不可用）。"""')
    lines.push("    return float(Decimal(float(x)).quantize(Decimal(1).scaleb(-nd), rounding=ROUND_HALF_UP))")
    lines.push('')
    lines.push('def _vals(series):')
    lines.push("    \"\"\"列 → list（缺失为 None）：非数值单元格按缺失处理，同后端 numeric_column。\"\"\"")
    lines.push("    return [None if pd.isna(v) else float(v) for v in pd.to_numeric(series, errors='coerce').to_numpy(dtype='float64')]")
    lines.push('')
    lines.push('def _roll(series, w, fn):')
    lines.push('    """窗口 = 当前行之前的 w 行（不含当前行），先剔除缺失再聚合；有效值 0 个 → NaN。"""')
    lines.push('    vals = _vals(series)')
    lines.push('    out = [None] * len(vals)')
    lines.push('    for i in range(w, len(vals)):')
    lines.push('        win, total = [], 0.0')
    lines.push('        for v in vals[i - w:i]:')
    lines.push('            if v is not None:')
    lines.push('                win.append(v)')
    lines.push('                total += v   # 逐项朴素累加：Python 3.12 的 sum() 走补偿求和，与后端 seq_sum 不等价')
    lines.push('        cnt = len(win)')
    lines.push('        if cnt == 0:')
    lines.push('            continue')
    lines.push("        if fn == 'mean':")
    lines.push('            out[i] = _rh(total / cnt, 2)')
    lines.push("        elif fn == 'std':")
    lines.push('            m = total / cnt')
    lines.push('            dev = 0.0')
    lines.push('            for x in win:')
    lines.push('                dev += (x - m) ** 2')
    lines.push('            out[i] = _rh(math.sqrt(dev / cnt), 2)   # 总体标准差 ÷n')
    lines.push("        elif fn == 'med':")
    lines.push('            s = sorted(win)')
    lines.push('            out[i] = _rh(s[cnt // 2] if cnt % 2 else (s[cnt // 2 - 1] + s[cnt // 2]) / 2.0, 2)')
    lines.push("        elif fn == 'max':")
    lines.push('            out[i] = max(win)')
    lines.push('        else:')
    lines.push('            out[i] = min(win)')
    lines.push('    return out')
    lines.push('')
    lines.push('def _expanding_mean(series):')
    lines.push('    """到上一行为止的全部有效值均值（不含当前行），累加顺序同后端 cumsum / JS 顺序求和。"""')
    lines.push('    out, acc, cnt = [], 0.0, 0')
    lines.push('    for v in _vals(series):')
    lines.push('        out.append(_rh(acc / cnt, 2) if cnt else None)')
    lines.push('        if v is not None:')
    lines.push('            acc += v')
    lines.push('            cnt += 1')
    lines.push('    return out')
    lines.push('')
    lines.push('def _ewm_prev(series, span):')
    lines.push('    """α=2/(span+1) 的递推指数加权，输出上一行的状态；缺失不衰减已有状态（等价 ignore_na=True）。"""')
    lines.push('    alpha = 2.0 / (span + 1)')
    lines.push('    out, prev = [], None')
    lines.push('    for v in _vals(series):')
    lines.push('        out.append(_rh(prev, 2) if prev is not None else None)')
    lines.push('        if v is None:')
    lines.push('            continue')
    lines.push('        prev = v if prev is None else alpha * v + (1.0 - alpha) * prev')
    lines.push('    return out')
    lines.push('')
    lines.push('def _cat_key(v):')
    lines.push('    """类别取值 → 分组键：空白/缺失 → ""（不参与统计），整数值不保留 .0（同 JS String(v)）。"""')
    lines.push("    if v is None or v is pd.NaT or v == '':")
    lines.push("        return ''")
    lines.push('    if isinstance(v, float) and math.isnan(v):')
    lines.push("        return ''")
    lines.push('    if isinstance(v, float) and v.is_integer():')
    lines.push('        return str(int(v))')
    lines.push('    return str(v)')
    lines.push('')
    lines.push('def _target_mean(frame, cat_col, target_col):')
    lines.push('    """目标均值编码：按行序累加求均值后 _rh(...,2)，空白类别与缺失目标不参与（同后端 build_cat）。"""')
    lines.push('    keys = [_cat_key(v) for v in frame[cat_col].to_numpy(dtype=object, copy=False)]')
    lines.push("    tvals = [None if pd.isna(v) else float(v) for v in pd.to_numeric(frame[target_col], errors='coerce').to_numpy(dtype='float64')]")
    lines.push('    sums, cnts = {}, {}')
    lines.push('    for g, v in zip(keys, tvals):')
    lines.push("        if g == '' or v is None:")
    lines.push('            continue')
    lines.push('        sums[g] = sums.get(g, 0.0) + v')
    lines.push('        cnts[g] = cnts.get(g, 0) + 1')
    lines.push('    means = {g: _rh(sums[g] / cnts[g], 2) for g in cnts}')
    lines.push('    return [means.get(g) for g in keys]')
  }
  ops.forEach((entry, opNum) => {
    const p = entry.params
    if (p.type === 'rename_column' || p.type === 'feature_rename') {
      const newKey = p.newKey || String(p.newLabel || '').toLowerCase().replace(/[^a-z0-9_]/g, '_')
      const key = `rename:${p.oldKey}->${newKey}`
      // 特征列若只改显示名（未换键），pandas 侧无事可做
      if (!done.has(key) && newKey && newKey !== p.oldKey) {
        done.add(key)
        if (!done.has('rename_header')) {
          done.add('rename_header')
          nextHeader('列重命名')
        } else {
          lines.push('')
        }
        lines.push(`df = df.rename(columns={'${p.oldKey}': '${newKey}'})  # 界面显示名为 ${p.newLabel}`)
        if (p.oldKey === tc) tc = newKey
      }
      return
    }
    if (p.type === 'delete_column' || p.type === 'feature_drop') {
      if (!p.key) return
      // 同一列可能被反复建了又撤，pandas 侧只发一次 drop，否则第二次 KeyError
      const dkey = `drop:${rebuildRound}:${p.key}`
      if (done.has(dkey)) return
      done.add(dkey)
      if (p.type === 'feature_drop') dropsSeen++
      if (!done.has('delcol_header')) {
        done.add('delcol_header')
        nextHeader('列删除')
      } else {
        lines.push('')
      }
      lines.push(`df = df.drop(columns=['${p.key}'])  # ${p.type === 'feature_drop' ? '界面撤销的特征列' : '界面删除列'}「${p.label || p.key}」`)
    }
    if (p.type === 'unit_convert') {
      if (!p.key || typeof p.factor !== 'number') return
      if (!done.has('unit_header')) {
        done.add('unit_header')
        nextHeader('单位转换')
      } else {
        lines.push('')
      }
      const off = typeof p.offset === 'number' && p.offset !== 0 ? ` + ${p.offset}` : ''
      lines.push(`# ${p.oldUnit || '无单位'} → ${p.newUnit || '新单位'}：y = ${p.factor}x${off}`)
      lines.push(`df['${p.key}'] = np.round(df['${p.key}'] * ${p.factor}${off}, 4)  # 前端用 toFixed(4)（远离 0 进位），np.round 为银行家舍入，半数值末位可能差 0.0001`)
    }
    if (p.type === 'exo-preset' || p.type === 'exo-formula' || p.type === 'exo-file') {
      if (!done.has('exo_header')) {
        done.add('exo_header')
        nextHeader('外生变量（由服务端生成/对齐，明细不经过浏览器）')
      } else {
        lines.push('')
      }
      if (p.type === 'exo-preset') {
        lines.push(`# 预设模板 ${p.presetKey} → 列 ${p.key}：服务端按主表时间列生成，随机源 numpy.random.default_rng(${p.seed})`)
        lines.push(`# 生成器在 timeseries-studio-server/app/services/exo.py 的 generate_preset（模拟序列，界面上已标注）。`)
        lines.push(`# 本脚本不复制那份生成器；需要同一列请取「导出处理结果」的宽表，从里面读 ${p.key} 列。`)
      }
      if (p.type === 'exo-formula') {
        const vec = pyVarFormula(p.expr, tc)
        lines.push(`# 公式列 ${p.key} = ${p.expr}（服务端向量化求值；hour 为小数小时，weekday 按 JS 语义 0=周日）`)
        if (vec) {
          lines.push(`df['${p.key}'] = np.round(${vec}, 4)  # 不可得值置空，pandas 表现为 NaN`)
        } else {
          lines.push(`# 该公式含 &&、||、! 或三元运算，JS 的短路语义无法安全向量化，pandas 端未复现，数值请以界面为准`)
        }
      }
      if (p.type === 'exo-file') {
        const targets = (p.targets || []).filter(t => t && t.from && t.key)
        const mode = p.mode === 'nearest' ? 'nearest' : 'left'
        lines.push(`# 侧表 ${p.filename}（内容指纹 sha=${p.sha}，落在后端 dataset/_exo/ 下），时间列 ${p.sideTimeCol}，对齐方式 ${mode}`)
        if (!targets.length) {
          lines.push(`# 日志没有记录侧表列名与主表列名的对应关系，这一列无法在脚本里复现，请从导出宽表取`)
        } else {
          lines.push(`side = pd.read_csv('dataset/_exo/${p.filename}')`)
          lines.push(`side['_t'] = pd.to_datetime(side['${p.sideTimeCol}'], errors='coerce').dt.floor('s')`)
          lines.push(`side = side.dropna(subset=['_t']).sort_values('_t').drop_duplicates('_t', keep='last')  # 同一时刻后写的行胜出`)
          targets.forEach(t => lines.push(`side['${t.key}'] = pd.to_numeric(side['${t.from}'], errors='coerce')`))
          lines.push(`df['_t'] = pd.to_datetime(df['${tc}'], errors='coerce').dt.floor('s')`)
          if (mode === 'left') {
            lines.push(`df = df.merge(side[['_t', ${targets.map(t => `'${t.key}'`).join(', ')}]], on='_t', how='left')  # 未命中的主表行留空`)
          } else {
            const tol = p.toleranceMinutes ? `, tolerance=pd.Timedelta('${p.toleranceMinutes}min')` : ''
            lines.push(`df = pd.merge_asof(df.sort_values('_t'), side[['_t', ${targets.map(t => `'${t.key}'`).join(', ')}]].sort_values('_t'),`)
            lines.push(`              left_on='_t', right_on='_t', direction='nearest'${tol})`)
            lines.push(`# 服务端是「左右各取一个候选、距离相同取行号更小的主表行」，merge_asof 的平手规则不保证相同，边界行可能差一行`)
          }
          lines.push(`df = df.drop(columns=['_t'])`)
        }
      }
    }
    if (p.type === 'mask_delete' || p.type === 'mask_delete_all') {
      const keys = p.type === 'mask_delete' ? [p.maskKey] : (p.keys || [])
      const alive = keys.filter(k => k)
      if (alive.length === 0) return
      if (!done.has('maskdel_header')) {
        done.add('maskdel_header')
        nextHeader('布尔掩码列删除')
      } else {
        lines.push('')
      }
      lines.push(`df = df.drop(columns=[${alive.map(k => `'${k}'`).join(', ')}])`)
    }
    if (p.type === 'time_convert' && !done.has('time_convert')) {
      done.add('time_convert')
      nextHeader('时间格式标准化')
      const col = p.timeCol || tc
      const tf = String(p.targetFmt || '')
      lines.push(`# 目标格式: ${tf}`)
      if (tf === 'epoch_ms' || tf === 'epoch_s') {
        // 与后端同一条规则：naive 时间按"本机时区"读，换算要减掉这段偏移
        lines.push('from datetime import datetime')
        lines.push('_tz_off = int(datetime.now().astimezone().utcoffset().total_seconds())')
        lines.push(`df['${col}'] = pd.to_datetime(df['${col}']).astype('int64') // ` +
          (tf === 'epoch_ms' ? `1_000_000 - _tz_off * 1_000` : `1_000_000_000 - _tz_off`))
      } else if (tf.includes('SSS')) {
        lines.push(`df['${col}'] = pd.to_datetime(df['${col}']).dt.strftime('${pyStrftime(tf)}')` +
          `.str.replace(r'\\.(\\d{3})\\d{3}', r'.\\1', regex=True)`)
        lines.push(`df['${col}'] = pd.to_datetime(df['${col}'])`)
      } else {
        lines.push(`df['${col}'] = pd.to_datetime(df['${col}']).dt.strftime('${pyStrftime(tf)}')`)
        lines.push(`df['${col}'] = pd.to_datetime(df['${col}'])`)
      }
    }
    if (p.type === 'resample' && !done.has('resample')) {
      done.add('resample')
      nextHeader(`重采样至 ${p.detail}`)
      const agg = p.method === 'interpolate' ? 'mean' : p.method
      lines.push(`num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]`)
      lines.push(`df = df.set_index('${tc}').resample('${p.targetRate}').${agg}()`)
      lines.push(`df = df.reset_index().dropna(subset=['${tc}'])`)
    }
    if (p.type === 'multi_calc') {
      const key = 'multi_calc:' + p.name
      if (done.has(key)) return
      done.add(key)
      if (section === 1) nextHeader('列间运算衍生')
      else lines.push('')
      const chain = p.terms.map((t, i) => i === 0 ? `df['${t.col}']` : ` ${t.op} df['${t.col}']`).join('')
      lines.push(`df['${p.name}'] = ${chain}`)
    }
    if (p.type === 'impute_segment' || p.type === 'impute_all') {
      if (p.type === 'impute_all') {
        if (done.has('impute')) return
        done.add('impute')
        ensureQualityHelpers()
        nextHeader('缺失值填补与去重（服务端扫描全表）')
        // 列清单取操作当时的数值列：事后 dtype 判断可能与界面上「可填补的列」不一致
        const cols = (p.floatCols || []).filter(Boolean)
        if (cols.length) lines.push(`impute_cols = [${cols.map(c => `'${c}'`).join(', ')}]`)
        else lines.push(`impute_cols = [c for c in df.columns if c != '${tc}' and pd.api.types.is_numeric_dtype(df[c])]`)
        lines.push(`for _c in impute_cols:`)
        lines.push(`    _v = _col(df[_c])`)
        lines.push(`    _fill_all(_v, '${p.defaultAlgo || 'linear'}')`)
        lines.push(`    df[_c] = _v`)
        if (p.dedupe) {
          lines.push(`df, _removed = _dedupe(df, '${tc}', impute_cols, '${p.dedupe}')`)
          lines.push(`print(f'重复时间戳合并（${p.dedupe}）：删除 {_removed} 行')`)
        } else {
          lines.push(`# 本次没有执行重复时间戳合并：界面勾选时服务端扫到 0 行重复`)
        }
      } else {
        const key = 'impute:' + p.colKey
        if (done.has(key)) return
        done.add(key)
        ensureQualityHelpers()
        if (section === 1) nextHeader('缺失值时序填补')
        lines.push(`_v = _col(df['${p.colKey}'])`)
        ;(p.segments || []).forEach(s => lines.push(`_fill_run(_v, ${s.startIdx}, ${s.endIdx}, '${s.algo || 'linear'}')`))
        lines.push(`df['${p.colKey}'] = _v`)
      }
    }
    // 一条修复记录 = pandas 侧一个独立步骤。不能用"整场只发一次异常块"的去重：
    // 界面允许「检测→截断→再检测→再生成掩码」连着做，只发第一条会让导出脚本少算一步，
    // 跑出来的列集合与界面当前帧不符。
    if (p.type === 'anomaly_repair' && !done.has('anomaly:' + opNum)) {
      done.add('anomaly:' + opNum)
      const an = p.anomaly || {
        algo: p.algo, params: p.iforest || null,
        columns: Array.isArray(p.columns) ? p.columns : []
      }
      const algo = an.algo || p.algo || '3sigma'
      const cols = (an.columns || []).filter(c => c && c.key)
      ensureQualityHelpers()
      nextHeader(`异常检测（${algo}）与修复（${p.repair}）`)
      if (cols.length === 0) {
        lines.push('# 这条记录没有留存逐列判定结果（旧版本的操作日志），无法在 pandas 端复现判定条件')
      } else {
        if (algo === 'iforest_sklearn') {
          const ip = an.params || {}
          const contam = typeof ip.contamination === 'number' ? ip.contamination : "'auto'"
          lines.push('# 检测模型与后端同款：scikit-learn IsolationForest 逐列一维拟合')
          lines.push('from sklearn.ensemble import IsolationForest')
          lines.push('')
          lines.push(`def _iforest_mask(series, n_estimators=${Number(ip.nEstimators) || 200}, contamination=${contam}, random_state=${Number(ip.randomState) || 42}):`)
          lines.push(`    \"\"\"返回 (异常布尔数组, 下界, 上界)；边界取正常点的分位数，同后端 _iforest_column_mask。\"\"\"`)
          lines.push(`    arr = pd.to_numeric(series, errors='coerce').to_numpy(dtype='float64')`)
          lines.push('    valid = ~np.isnan(arr)')
          lines.push('    values = arr[valid]')
          lines.push('    model = IsolationForest(n_estimators=n_estimators, contamination=contamination,')
          lines.push('                            random_state=random_state, bootstrap=False)')
          lines.push('    pred = model.fit_predict(values.reshape(-1, 1))')
          lines.push('    mask = np.zeros(arr.size, dtype=bool)')
          lines.push('    mask[valid] = pred == -1')
          lines.push('    normal = values[pred == 1]')
          lines.push('    if normal.size:')
          lines.push(`        lower, upper = float(np.quantile(normal, ${Number(ip.normalLowerQ) || 0.005})), float(np.quantile(normal, ${Number(ip.normalUpperQ) || 0.995}))`)
          lines.push('    else:')
          lines.push('        lower, upper = float(values.min()), float(values.max())')
          lines.push('    return mask, _r4(lower), _r4(upper)')
          lines.push('')
        }
        if (algo === 'expr') {
          lines.push(`# 自定义表达式：${p.expr || an.expr || '(未留存)'} —— 后端按表达式逐点判定，`)
          lines.push('# 这里只能按检测记录到的「正常值区间」近似；表达式不是单一阈值时两者可能不同，')
          lines.push(`# 每列注释里给出当时真实检出的点数，跑完请与之核对。`)
        } else if (algo === 'iforest') {
          lines.push('# 孤立森林（近似版）= 回看 33 个观测的窗口 MAD ×4，与后端 _mad_mask 同一段 numpy 代码')
        } else {
          lines.push(`# 判定阈值由检测时的列统计量得出（后端 ${algo}），下方为记录值`)
        }
        let totalRecorded = 0
        let emitted = 0
        cols.forEach((c, i) => {
          totalRecorded += Number(c.count) || 0
          const hasBounds = Number.isFinite(Number(c.lower)) && Number.isFinite(Number(c.upper)) && c.lower !== null && c.upper !== null
          if (!hasBounds) {
            lines.push(`# 列 ${c.key}：检测未给出可用边界（当时检出 ${c.count} 点），无法在 pandas 端复现，已跳过`)
            return
          }
          const lo = Number(c.lower).toFixed(4)
          const up = Number(c.upper).toFixed(4)
          emitted++
          if (algo === 'iforest_sklearn') lines.push(`mask_${i}, lo_${i}, up_${i} = _iforest_mask(df['${c.key}'])   # 检出 ${c.count} 点`)
          else if (algo === 'iforest') lines.push(`mask_${i} = _mad_mask(df['${c.key}'])   # 检出 ${c.count} 点 · 记录边界 [${lo}, ${up}]`)
          else lines.push(`mask_${i} = _threshold_mask(df['${c.key}'], ${lo}, ${up})   # 检出 ${c.count} 点`)
          if (p.repair === 'clip') {
            lines.push(`_a = pd.to_numeric(df['${c.key}'], errors='coerce').to_numpy(dtype='float64')`)
            lines.push(`_a[mask_${i}] = np.where(_a[mask_${i}] < ${algo === 'iforest_sklearn' ? `lo_${i}` : lo}, ${algo === 'iforest_sklearn' ? `lo_${i}` : lo}, ${algo === 'iforest_sklearn' ? `up_${i}` : up})`)
            lines.push(`df['${c.key}'] = _a`)
          } else if (p.repair === 'nan_impute') {
            lines.push(`_a = _col(df['${c.key}'])`)
            lines.push(`for _i in np.flatnonzero(mask_${i}): _a[int(_i)] = None`)
            lines.push(`_fill_all(_a, 'linear')   # 置缺失后整列重插值：该列原有的缺失段也会一起被填`)
            lines.push(`df['${c.key}'] = _a`)
          } else {
            lines.push(`df['anomaly_mask_${c.key}'] = mask_${i}.astype('int64')`)
          }
        })
        if (p.repair === 'mask_only') lines.push(`# mask_only：只加 anomaly_mask_* 列，原始数值未被修改`)
        lines.push(`print('异常修复（${p.repair}）：本次涉及 ${totalRecorded} 个点，pandas 侧复现了 ${emitted} 列')`)
      }
    }
    if (p.type === 'mask_generate') {
      const key = 'mask:' + p.maskName
      if (done.has(key)) return
      done.add(key)
      nextHeader(`布尔掩码 ${p.maskName}`)
      lines.push(`df['${p.maskName}'] = 0`)
      lines.push(`df.iloc[${p.startIdx}:${Number(p.endIdx) + 1}, df.columns.get_loc('${p.maskName}')] = 1`)
      if (typeof p.onesCount === 'number') lines.push(`assert int(df['${p.maskName}'].sum()) === ${p.onesCount}   # 界面上报的置 1 行数`)
    }
    if (p.type === 'feature_build') {
      const key = 'feat:' + p.featureType + ':' + (p.detail || '')
      const isDup = done.has(key)
      if (isDup && dropsSeen === 0) return
      if (isDup) {
        dropsSeen = 0
        rebuildRound++
        lines.push('')
        lines.push('# 以下块为重建：这组特征列在流程中被撤销过，需再次生成才与界面当前列集合一致')
      }
      done.add(key)
      if (p.featureType === 'time') {
        nextHeader('日历时间特征')
        const toks = (p.detail || '').split(',').filter(Boolean)
        const replace = toks.includes('replace_original')
        const cycSet = toks.filter(t => t.startsWith('cyclical_')).map(t => t.slice('cyclical_'.length))
        const dims = toks.filter(t => !t.startsWith('cyclical_') && t !== 'replace_original')
        // 与浏览器端 weekdayIndex/cyclical 同一套语义：pandas dayofweek 同样是周一=0
        const DIM_EXPR = {
          hour: `df['${tc}'].dt.hour`,
          day: `df['${tc}'].dt.day`,
          month: `df['${tc}'].dt.month`,
          weekday: `df['${tc}'].dt.dayofweek`,
          is_weekend: `(df['${tc}'].dt.dayofweek >= 5).astype(int)`
        }
        const CYCLE_EXPR = {
          hour: `df['${tc}'].dt.hour / 24.0`,
          weekday: `df['${tc}'].dt.dayofweek / 7.0`,
          month: `(df['${tc}'].dt.month - 1) / 12.0`
        }
        dims.forEach(opt => {
          // 勾选了「编码后不保留原列」的维度只出 sin/cos：这里跳过它的数值列（同后端 time_plan 的 skip）
          if (replace && cycSet.includes(opt)) return
          if (opt === 'holiday') {
            // 节假日表以构建那一刻工作区生效的那份为准（记在这条审计参数里）：
            // 后端默认表可能已经换过年份，只有这份快照能复现界面上 feat_holiday 的取值
            const days = Array.isArray(p.holidayDays) ? p.holidayDays : []
            if (days.length === 0) {
              lines.push(`# feat_holiday 依赖工作区生效的节假日表，这条记录未留存日期清单（旧版本的操作日志），无法复现该列`)
              lines.push(`# 请在第五步重新配置一次节假日表并重建日历特征，再导出本脚本`)
              return
            }
            lines.push(`# 构建时工作区生效的节假日表（${days.length} 天，第五步「节假日表」面板配置）`)
            lines.push(`HOLIDAYS = {${days.map(h => `'${h}'`).join(', ')}}`)
            lines.push(`df['feat_holiday'] = df['${tc}'].dt.strftime('%Y-%m-%d').isin(HOLIDAYS).astype(int)`)
            return
          }
          if (DIM_EXPR[opt]) lines.push(`df['feat_${opt}'] = ${DIM_EXPR[opt]}`)
        })
        cycSet.forEach(dim => {
          const x = CYCLE_EXPR[dim]
          if (!x) return
          lines.push(`df['feat_${dim}_sin'] = np.sin(2 * np.pi * ${x})`)
          lines.push(`df['feat_${dim}_cos'] = np.cos(2 * np.pi * ${x})`)
        })
        if (cycSet.length && dims.includes('day')) {
          lines.push(`# 注：day 每月天数不固定（28~31），无周期可言，故未做正余弦编码`)
        }
        if (replace && cycSet.length) {
          lines.push(`# 这些维度勾选了「编码后不保留数值原列」，所以只有 feat_${cycSet[0]}_sin/cos，没有 feat_${cycSet.join('/')} 普通列`)
        }
      }
      if (['lag_roll', 'lag', 'window'].includes(p.featureType)) {
        ensureFeatureHelpers()
        const groupName = { lag: '滞后特征', window: '滑动窗口特征' }[p.featureType] || '滞后与滑动窗口特征'
        nextHeader(`${groupName}（${p.detail}）`)
        const plan = parseLagDetail(p.detail)
        lines.push(`# 窗口类特征只看当前行之前的数据（等价 .shift(1)）：防止标签泄漏`)
        lines.push(`# 窗口内有缺失时按有效值个数聚合（同后端 rolling_agg），不是 pandas rolling 的 NaN 传播`)
        plan.cols.forEach(col => {
          plan.lags.forEach(s => lines.push(`df['lag_${col}_t${s}'] = df['${col}'].shift(${s})`))
          plan.windows.forEach(w => plan.stats.forEach(fn => {
            lines.push(`df['roll_${ROLL_STATS[fn]}_${col}_w${w}'] = _roll(df['${col}'], ${w}, '${ROLL_STATS[fn]}')`)
          }))
          if (plan.expanding) lines.push(`df['expanding_mean_${col}'] = _expanding_mean(df['${col}'])`)
          if (plan.ewm) lines.push(`df['ewm_${col}_s${plan.ewmSpan}'] = _ewm_prev(df['${col}'], ${plan.ewmSpan})`)
        })
      }
      if (['diff_freq', 'diff', 'fft'].includes(p.featureType)) {
        const plan = parseDiffDetail(p.detail)
        const groupName = { diff: '差分平稳化特征', fft: '频域特征' }[p.featureType] || '差分与频域特征'
        nextHeader(`${groupName}（${p.detail}）`)
        if (plan.cols.length === 0) {
          lines.push('# 该次构建未登记任何目标列，无可导出语句')
          return
        }
        lines.push('# 前端对 null 与 pandas 对 NaN 等价：差分段前若干行为空属正常')
        lines.push('# 小数位：前端 toFixed（四舍五入）、numpy round（半偶舍入），仅末位边界值可能有 1 个最小单位的差异')
        plan.cols.forEach(col => {
          if (plan.d1) lines.push(`df['diff1_${col}'] = df['${col}'].diff(1).round(4)`)
          if (plan.d2) lines.push(`df['diff2_${col}'] = df['${col}'].diff(1).diff(1).round(4)`)
          if (plan.seasonal) lines.push(`df['diff_season${plan.period}_${col}'] = df['${col}'].diff(${plan.period}).round(4)`)
        })
        const fftOn = plan.fftDominant || plan.fftEntropy || plan.fftPowerRatio
        if (fftOn) {
          const fcol = plan.cols[0]
          if (!dftHelpers) {
            dftHelpers = true
            lines.push('')
            lines.push('# ---- DFT 工具：与前端 computeDFT 同款 |Σ xₙ·e^(-i2πkn/N)| / N，缺失按 0 参与计算 ----')
            lines.push('def _dft_mag(x, k):')
            lines.push('    x = np.asarray(x, dtype=float)')
            lines.push('    m = len(x)')
            lines.push('    idx = np.arange(m)')
            lines.push('    ang = -2 * np.pi * k * idx / m')
            lines.push('    return float(np.hypot((x * np.cos(ang)).sum(), (x * np.sin(ang)).sum())) / m')
            lines.push('')
            lines.push('def _spectral_entropy(x):')
            lines.push('    e = np.array([_dft_mag(x, k) for k in range(1, 17)])')
            lines.push('    p = e / (e.sum() or 1.0)')
            lines.push('    p = p[p > 0]')
            lines.push('    return round(float(-(p * np.log2(p)).sum()), 4)')
            lines.push('')
            lines.push('def _power_ratio(x):')
            lines.push('    low = sum(_dft_mag(x, k) for k in range(1, 5))')
            lines.push('    high = sum(_dft_mag(x, k) for k in (8, 12, 16))')
            lines.push('    return round(low / (high + 1e-10), 2)')
          }
          lines.push('')
          lines.push(`# ---- 频域特征（在 ${fcol} 上逐点滑窗 DFT，窗口 64 点）----`)
          lines.push(`FFT_COL = '${fcol}'`)
          lines.push(`vals = pd.to_numeric(df[FFT_COL], errors='coerce').fillna(0).to_numpy(dtype=float)`)
          lines.push('n = len(vals)')
          lines.push('W = 64')
          if (plan.fftDominant) {
            lines.push('# 主导频率在整个序列上评选（k = 1..min(50, n//2) 取幅值最大的 3 个），再逐点用截至当前行的窗口取值')
            lines.push('spectrum = [{"k": k, "energy": _dft_mag(vals, k)} for k in range(1, min(50, n // 2) + 1)]')
            lines.push('top3 = sorted(spectrum, key=lambda s: -s["energy"])[:3]')
            lines.push('for rank, item in enumerate(top3, start=1):')
            lines.push('    df[f"fft_top{rank}_{FFT_COL}"] = [round(_dft_mag(vals[max(0, i - W):i + 1], item["k"]), 2) for i in range(n)]')
          }
          if (plan.fftEntropy) {
            lines.push(`df['fft_entropy_${fcol}'] = [np.nan if i < W else _spectral_entropy(vals[i - W:i]) for i in range(n)]`)
          }
          if (plan.fftPowerRatio) {
            lines.push(`df['fft_power_ratio_${fcol}'] = [np.nan if i < W else _power_ratio(vals[i - W:i]) for i in range(n)]`)
          }
        }
      }
      if (p.featureType === 'cat') {
        const plan = parseCatDetail(p.detail)
        nextHeader(`类别编码（${p.detail}）`)
        const cols = plan.cols.filter(k => d.columns.some(c => c.key === k))
        if (cols.length === 0) {
          lines.push('# 该次构建登记的类别列在当前数据中已不存在，无可导出语句')
          return
        }
        if (plan.method === 'onehot') {
          if (!Array.isArray(p.onehotKeys) || p.onehotKeys.length === 0) {
            // 独热列名来自数据取值，浏览器不再留整表就算不出来了；构建那一次记下的键序是唯一真相
            lines.push('# 这条记录没有留存独热列名清单（旧版本的操作日志），无法复现本次编码的列集合')
            return
          }
          lines.push(`# 独热列名 = <列名>_<取值>；前端保留原类别列并追加 0/1 列，所以只对副本做 get_dummies 再拼回`)
          lines.push(`# astype('object') 保证只对选中的列编码（数值型类别列也会被当作离散取值处理），缺失行全为 0，与前端一致`)
          lines.push(`_dummies = pd.get_dummies(df[[${cols.map(c => `'${c}'`).join(', ')}]].astype('object'), prefix_sep='_').astype(int)`)
          lines.push(`df = pd.concat([df, _dummies[[${p.onehotKeys.map(x => `'${x}'`).join(', ')}]]], axis=1)  # 按取值首次出现顺序排列，与前端宽表同序`)
        } else if (plan.method === 'ordinal') {
          // categories 直接由 pandas 按取值首次出现顺序现算，与后端 unique_in_order 同一条规则，
          // 不必把整列取值抄进脚本（高基数列会让导出的代码变成几千个字符串的字面量）
          lines.push(`# 序数编号按取值首次出现顺序（非字典序）；缺失与未登记取值 → -1，与 Categorical.codes 一致`)
          cols.forEach(catCol => {
            lines.push(`_cats_${catCol.replace(/[^0-9a-zA-Z]/g, '_')} = df['${catCol}'].dropna().drop_duplicates().tolist()`)
            lines.push(`df['${catCol}_ordinal'] = df['${catCol}'].astype(pd.CategoricalDtype(_cats_${catCol.replace(/[^0-9a-zA-Z]/g, '_')})).cat.codes`)
          })
        } else {
          // 参照列取构建当场服务端用的那一列（isMain 优先），旧记录没有这个字段时退回同一份挑选规则
          const mainFloat = p.targetColumn || (d.columns.find(c => c.type === 'float' && c.isMain && !c.feature)
            || d.columns.find(c => c.type === 'float' && !c.feature && !c.key.endsWith('_ordinal') && !c.key.endsWith('_target')))?.key
          if (!mainFloat) {
            lines.push('# 目标编码需要数值目标列，当前数据无可用浮点列，前端该次构建未产出有效值')
          } else {
            ensureFeatureHelpers()
            lines.push(`# 目标编码 = 该类别下 ${mainFloat} 的均值（有标签泄漏风险，仅供探索）`)
            lines.push(`# 组均值按行序累加后再取 2 位（同后端 build_cat）；pandas 的 groupby.transform('mean') 求和顺序不同，边界上会差一个百分位`)
            cols.forEach(catCol => {
              lines.push(`df['${catCol}_target'] = _target_mean(df, '${catCol}', '${mainFloat}')`)
            })
          }
        }
      }
    }
    // 切分不在这里发射：见下方按「最后一次设置」统一处理
  })

  // 切分是收尾动作：按最后一次真实设置的比例，在最终矩阵上发射
  const splitApplied = ops.filter(e => e.params.type === 'split_apply')
  if (splitApplied.length > 0) {
    // 划分列由后端逐行写入，脚本复现同一条规则：整表行序前 train% → 中间 val% → 末段 test%
    const last = splitApplied[splitApplied.length - 1]
    const { ratio, train, val, test, rows } = last.params
    const key = last.params.key || 'dataset_split'
    nextHeader(`生成数据集划分列 ${key}（训练 ${ratio}%，时序不打乱）`)
    lines.push(`# 生成时整表 ${rows} 行：train ${train} / val ${val} / test ${test}（界面上那三个数与后端同一次计算）`)
    lines.push(`_n = len(df)`)
    lines.push(`assert _n == ${rows}, f"生成划分时 ${rows} 行，现在 {_n} 行：中间增删过行，请按新行数重新生成划分列"`)
    lines.push(`_train = _n * ${ratio} // 100`)
    lines.push(`_test = (_n - _train) // 2`)
    lines.push(`df["${key}"] = ["train"] * _train + ["val"] * (_n - _train - _test) + ["test"] * _test`)
    lines.push(`print("split:", df["${key}"].value_counts().to_dict())`)
  } else {
    const splits = ops.filter(e => e.params.type === 'split')
    if (splits.length > 0) {
      const ratio = splits[splits.length - 1].params.ratio
      nextHeader(`训练/验证/测试切分（训练 ${ratio}%，时序不打乱）`)
      const s = splitCounts(ratio)
      lines.push(`# 这份日志只设过比例、没生成划分列（旧流程），按行序切片`)
      lines.push(`train = df.iloc[:${s.train}]`)
      lines.push(`val   = df.iloc[${s.train}:${s.train + s.val}]`)
      lines.push(`test  = df.iloc[${s.train + s.val}:]`)
    }
  }

  const exported = ops.filter(e => e.params.type === 'export')
  if (exported.length > 0) {
    nextHeader('导出清洗结果')
    exported.forEach(e => {
      const f = e.params.format
      lines.push(`# ${e.title} · ${e.detail}`)
      if (f === 'parquet') lines.push(`df.to_parquet("${base}_export.parquet", engine="pyarrow", index=False)`)
      else if (f === 'feather') lines.push(`df.to_feather("${base}_export.feather")`)
      else if (f === 'csv') lines.push(`df.to_csv("${base}_export.csv", index=False, encoding="utf-8-sig")`)
      else if (f === 'xlsx') lines.push(`df.to_excel("${base}_export.xlsx", index=False, sheet_name="data")`)
    })
  }

  lines.push('')
  lines.push(`print("Pipeline executed. Matrix shape:", df.shape)`)
  lines.push(`df.to_csv("dataset_features.csv", index=False)`)
  return lines.join('\n')
}

// 占位符模板 → strftime：与后端 `_iter_template` 同一套从左到右的切分规则。
// 不能写成连续 str.replace：引入单字母占位符（M/D/H/m）之后，"MM"→"%m" 里那个 m
// 会被下一条 "m"→"%M" 再吃一遍，格式串当场作废。
const PY_FORMAT_TOKENS = [['YYYY', '%Y'], ['SSS', '%f'], ['MM', '%m'], ['DD', '%d'],
  ['HH', '%H'], ['mm', '%M'], ['ss', '%S'], ['M', '%m'], ['D', '%d'], ['H', '%H'], ['m', '%M']]

function pyStrftime(fmt) {
  const tpl = String(fmt)
  let out = ''
  let i = 0
  while (i < tpl.length) {
    const hit = PY_FORMAT_TOKENS.find(([tok]) => tpl.startsWith(tok, i))
    if (hit) { out += hit[1]; i += hit[0].length; continue }
    out += tpl[i] === '%' ? '%%' : tpl[i]
    i += 1
  }
  return out
}

// ============================================================
// 撤销 / 重做：服务端命令日志的游标就是历史
// ============================================================
// 旧做法是在浏览器压快照栈（每步一份元数据 + 页窗口 + 审计记录）。搬到服务端后它既没必要、
// 也留不住：明细帧与命令日志都在后端工作区里，游标本身就是历史。于是
//   · 撤销 = POST /restore {version: cursor-1}，重做 = cursor+1，一次网络请求换一步；
//   · 撤销跨页面刷新、甚至跨后端重启都成立（日志落盘 + 按日志重放，重启后第一次 GET 就把帧重建出来）；
//   · 大表上的快照内存与 localStorage 配额问题（审计项 7）连根消失。
async function restoreTo(version, verb) {
  const d = ds()
  if (!d?.wsId) { toast('warning', '还没有载入数据集'); return false }
  if (!requireBackend(verb)) return false
  const target = Number(version)
  if (!Number.isInteger(target) || target < 0) return false
  const label = verb === '撤销' ? state.history.undoLabel : state.history.redoLabel
  // 服务端按日志把帧重放到目标版本，浏览器只换一份 meta + 页窗口，绝不保留另一套历史
  state.busy = `${verb}：正在把后端工作区重放到第 ${target} 版…`
  try {
    const resp = await ws.wsRestore(d.wsId, target, d.page?.limit ?? PAGE_SIZE)
    applyMeta(d, resp.meta)
    applyPage(d, resp.page)
    // restore 会清掉服务端留存的检测结果：界面这份投影必须跟着作废，否则画的是上一版帧的异常点。
    // 诊断快照与抽稀曲线不用整摞清空——它们按 sourceSig 认人，数值代没变（撤销的是一条只加列的
    // 命令）时旧快照仍然成立，变了则下一次读取自动落空并重取，两种情况都不会读到错的数据。
    state.lastAnomaly = null
    touch()
    // 后端这次的真实代价：从哪一版起步、重放了几条命令（命中帧缓存时是 0 条，只换个指针）
    const tr = resp.meta?.restoreTrace
    const cost = tr ? (tr.cacheHit ? ` · 帧缓存命中，重放 0 条命令` :
      ` · ${tr.replayedOps ? `从第 ${tr.fromVersion} 版起步` : '从载入帧起步'}，重放 ${tr.replayedOps} 条命令`) : ''
    // 工作区 id 与行×列就在页头那张数据集卡上，这里只说这次撤销做了什么、代价是几条命令
    toast('success', `${verb}${label ? `：${label}` : ''} · 回到第 ${resp.meta.version} 版${cost}`)
    return true
  } catch (e) {
    toast('error', `${verb}失败：${e.message}`)
    return false
  } finally {
    state.busy = ''
  }
}

export function undo() {
  if (!state.history.canUndo) { toast('info', '没有可撤销的变更'); return Promise.resolve(false) }
  return restoreTo(state.history.version - 1, '撤销')
}

export function redo() {
  if (!state.history.canRedo) { toast('info', '没有可重做的变更'); return Promise.resolve(false) }
  return restoreTo(state.history.version + 1, '重做')
}

// ============================================================
// 会话：上次打开了什么、停在哪一步，由服务端记住
// ============================================================
// 浏览器只交出 UI 层状态（步骤、切分比例、审计记录、掩码/填补配置）和它引用了哪些 wsId；
// "这些工作区此刻还活着吗、在第几版"由 GET /api/session 当场按命令日志重建出来。
// 于是横幅上那句「上次会话」是一句有依据的陈述，而不是本地一份可能早就过期的承诺。
const SESSION_SAVE_DEBOUNCE = 700

function sessionPayload() {
  const d = ds()
  const loaded = Object.entries(datasets).filter(([, x]) => x?.wsId)
  return {
    step: state.currentStep,
    currentKey: state.currentKey,
    // 服务端按 0..1 校验切分比例，界面这一份是百分数
    splitRatio: (state.splitRatio || 70) / 100,
    workspaces: loaded.map(([key, x]) => ({ key, wsId: x.wsId, version: x.meta?.version ?? null })),
    actionLog: state.actionLog,
    meta: {
      name: d?.name || '', rows: d?.meta?.rowCount || 0, cols: d?.columns?.length || 0,
      wsId: d?.wsId || '', version: d?.meta?.version ?? null,
      ops: state.actionLog.length, step: state.currentStep
    },
    derivedCols: state.derivedCols,
    masks: state.masks,
    imputeSegAlgos: state.imputeSegAlgos,
    // 纯界面状态：服务端不理解，只在 GET 时原样回吐
    ui: {
      names: Object.fromEntries(loaded.map(([k, x]) => [k, x.name])),
      formats: Object.fromEntries(loaded.map(([k, x]) => [k, x.format])),
      page: d?.page ? { key: state.currentKey, offset: d.page.offset, limit: d.page.limit } : null
    }
  }
}

let sessionTimer = null

export function scheduleSessionSave() {
  if (!state.session.enabled || !state.backend.online) return
  clearTimeout(sessionTimer)
  sessionTimer = setTimeout(() => { saveSession() }, SESSION_SAVE_DEBOUNCE)
}

export async function saveSession() {
  if (!state.session.enabled || !state.backend.online) return false
  const payload = sessionPayload()
  if (!payload.workspaces.length && !payload.actionLog.length) return false
  state.session.saving = true
  try {
    const r = await ws.sessionSet(payload)
    state.session.savedAt = r.savedAt || ''
    state.session.error = ''
    return true
  } catch (e) {
    // 服务端的三条守卫（条数 / 体积 / 不许携带整列）拒绝时必须说清楚，
    // 不能像旧版配额写满那样静默关掉持久化——那样用户会以为自己已经存过了
    state.session.error = e.message || '写入失败'
    toast('warning', `会话未保存：${state.session.error}`)
    return false
  } finally {
    state.session.saving = false
  }
}

let pendingSession = null

// 页面启动时调用：只问不擅自替换。GET 会顺带把会话引用的工作区按日志重建/核对一遍。
export async function initSession() {
  if (!state.backend.online) await ws.checkBackend()
  if (!state.backend.online) {
    state.session.enabled = false
    state.session.error = `后端未连接（${ws.API_BASE}），会话存在服务端，此时无从查询`
    return
  }
  let doc
  try {
    doc = await ws.sessionGet()
  } catch (e) {
    state.session.error = e.message || '读取失败'
    return
  }
  state.session.enabled = true
  state.session.stateDir = doc.stateDir || ''
  state.session.workspaces = doc.workspaces || []
  if (!doc.exists) { pendingSession = null; state.session.found = null; return }
  const list = doc.workspaces || []
  // GET 的响应分两层：外层是服务端当场核对出来的（exists/savedAt/meta/workspaces/stateDir），
  // 界面状态整体躺在 snap 里。恢复要读的是 snap 那一份，混着取会拿到 undefined。
  const snap = doc.snap || {}
  pendingSession = { ...snap, savedAt: doc.savedAt, meta: doc.meta || {},
                     workspaces: list, stateDir: doc.stateDir || '' }
  const main = list.find(w => w.wsId === (doc.meta?.wsId)) || list[0] || {}
  state.session.savedAt = doc.savedAt || ''
  state.session.found = {
    name: main.name || doc.meta?.name || '未知数据集',
    rows: main.rowCount ?? doc.meta?.rows ?? 0,
    cols: doc.meta?.cols ?? 0,
    ops: doc.meta?.ops ?? (snap.actionLog || []).length,
    step: snap.step || 1,
    savedAt: doc.savedAt || '',
    wsId: main.wsId || '',
    version: main.version ?? null,
    alive: !!main.alive,
    reason: main.reason || '',
    drift: main.versionDrift || null,
    workspaces: list,
    stateDir: doc.stateDir || ''
  }
}

function applySessionUi(doc) {
  const ui = doc.ui || {}
  Object.entries(ui.names || {}).forEach(([k, name]) => {
    if (datasets[k]) datasets[k].name = name
  })
  state.actionLog = (doc.actionLog || []).map(e => ({ ...e }))
  state.derivedCols = doc.derivedCols || []
  state.masks = doc.masks || []
  state.imputeSegAlgos = doc.imputeSegAlgos || {}
  // 直接赋值而不是走 setSplitRatio：恢复不是用户拖动滑杆，不该凭空多一条审计记录
  state.splitRatio = Math.round((doc.splitRatio ?? 0.7) * 100)
  lastLoggedSplit = state.splitRatio
  state.currentStep = doc.step || 1
}

// 工作区在后端日志里没了（被淘汰、或那份来源文件已删）：UI 状态照旧恢复，
// 但绝不能留着旧行号旧统计冒充"还有数据"——那是把不存在的工作区画在屏幕上。
function detachDeadWorkspace(key) {
  const d = datasets[key]
  if (!d) return
  d.wsId = ''
  d.overview = null
  d.meta = null
  d.page = { offset: 0, limit: PAGE_SIZE, total: 0, columns: d.page?.columns || [], rows: [] }
}

export async function restoreSession() {
  if (!pendingSession) {
    await initSession()
    if (!pendingSession) { toast('info', '没有可恢复的服务端会话'); return false }
  }
  const doc = pendingSession
  const list = doc.workspaces || []
  const alive = list.filter(w => w.alive)
  applySessionUi(doc)
  state.busy = '正在按服务端命令日志恢复工作区…'
  const restored = []
  const failed = []
  try {
    for (const w of list) {
      if (!w.alive) { detachDeadWorkspace(w.key); failed.push(w); continue }
      const d = datasets[w.key] || (datasets[w.key] = blankDataset(w.key))
      const pg = doc.ui?.page
      const offset = pg && pg.key === w.key ? pg.offset : 0
      const limit = pg && pg.key === w.key ? pg.limit : PAGE_SIZE
      try {
        // /rows 一次拿回 meta + 页窗口：meta 里的游标就是服务端此刻的真实版本
        const resp = await ws.wsRows(w.wsId, offset, limit)
        d.name = w.name || d.name
        applyMeta(d, resp.meta)
        applyPage(d, resp.page)
        restored.push({ w, meta: resp.meta })
      } catch (e) {
        detachDeadWorkspace(w.key)
        failed.push({ ...w, reason: e.message })
      }
    }
  } finally {
    state.busy = ''
  }
  state.lastAnomaly = null
  state.quality = { sig: '', version: -1, data: null, loading: false, error: '' }
  invalidateSeries()
  // 数据集是上面那个循环才注册进来的，applySessionUi 跑在它之前，所以游标只能在循环之后指过去。
  // 早先那句 datasets[doc.currentKey] 守卫对目录/上传来的那一份（key 不在预置的 pv/load 里）
  // 永远不成立，结果恢复明明成功、界面却停在空白数据集上报「尚未载入数据集」。
  const focus = restored.find(r => r.w.key === doc.currentKey) || restored[0]
  if (focus) state.currentKey = focus.w.key
  touch()
  const drift = restored.find(r => r.w.versionDrift)
  const m = doc.meta || {}
  if (restored.length) {
    const main = restored.find(r => r.w.key === state.currentKey) || restored[0]
    // 行×列、状态目录这些页头数据集卡与 tooltip 上常驻，这里只讲恢复到了哪一版、代价是什么
    toast('success', `已恢复会话：${main.w.name} · 第 ${main.meta.version} 版 · ${m.ops ?? 0} 条操作` +
      (drift ? `（会话记的是第 ${drift.w.versionDrift.sessionSays} 版，服务端日志已走到第 ${drift.w.versionDrift.serverSays} 版，以服务端为准）` : '') +
      (failed.length ? `；${failed.length} 个工作区已不在服务端` : ''))
  } else {
    toast('warning', `只恢复了界面配置（${m.ops ?? 0} 条操作 · ${m.name || '未知数据集'}）：` +
      `服务端已没有这些工作区的命令日志（${failed.map(f => `${f.wsId}：${f.reason || '已不存在'}`).join('；') || '无'}），请回第一步重新载入`)
  }
  pendingSession = null
  state.session.found = null
  return true
}

export async function clearSession() {
  pendingSession = null
  state.session.found = null
  state.session.workspaces = []
  if (!state.backend.online) { state.session.error = '后端未连接，无法清除服务端会话'; return false }
  try {
    await ws.sessionClear()
    state.session.error = ''
    return true
  } catch (e) {
    state.session.error = e.message || '清除失败'
    toast('error', `清除服务端会话失败：${state.session.error}`)
    return false
  }
}

// 界面状态一变（执行命令、翻页、换数据集）就 Debounce 存一次会话。
// 撤销/重做同样走这里：服务端游标变了，会话记的 version 就该跟着变。
watch(() => state.dataVersion, () => scheduleSessionSave())
