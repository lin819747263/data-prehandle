// 后端子进程：拉起 / 复用 / 回收。两种运行时，命令行参数保持一致：
//   打包版  release\timeseries-backend.exe --host --port        （build-backend.bat 的产物）
//   源码版  python -m uvicorn app.main:app --host --port        （没打包时回落，开发期照旧改代码即生效）
// 只回收自己 spawn 的进程——端口上已有健康后端时走复用分支，退出时绝不能把它误杀。
import { spawn, spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'

const IS_WIN = process.platform === 'win32'
const HEALTH_TIMEOUT_MS = 2500
const PROBE_INTERVAL_MS = 400
const EXE_NAME = IS_WIN ? 'timeseries-backend.exe' : 'timeseries-backend'

function candidatePythons(serverDir) {
  const override = process.env.TSS_PYTHON
  if (override) return [override]
  const venv = IS_WIN
    ? path.join(serverDir, '.venv', 'Scripts', 'python.exe')
    : path.join(serverDir, '.venv', 'bin', 'python')
  return [venv, ...(IS_WIN ? ['python', 'python3'] : ['python3', 'python'])]
}

/** 打包版后端的查找顺序：显式指定 → Electron 资源目录（安装态）→ 后端项目 release/（开发态）。 */
export function backendExeCandidates(serverDir) {
  const out = []
  if (process.env.TSS_BACKEND_EXE) out.push(path.resolve(process.env.TSS_BACKEND_EXE))
  // 安装态必须优先 resources：exe 随应用走，不能被邻目录里别人的 release/ 抢走
  if (process.resourcesPath) out.push(path.join(process.resourcesPath, EXE_NAME))
  if (serverDir) out.push(path.join(serverDir, 'release', EXE_NAME))
  return out
}

function listenerPidOnPort(port) {
  if (!IS_WIN) return null
  const out = spawnSync('netstat', ['-ano', '-p', 'TCP'], { encoding: 'utf8', windowsHide: true }).stdout || ''
  for (const line of out.split(/\r?\n/)) {
    const cols = line.trim().split(/\s+/)
    if (cols.length >= 5 && cols[0] === 'TCP' && cols[3] === 'LISTENING' && cols[1].endsWith(`:${port}`)) {
      return Number(cols[4]) || null
    }
  }
  return null
}

function imageOfPid(pid) {
  if (!IS_WIN) return ''
  const out = spawnSync('tasklist', ['/fi', `PID eq ${pid}`, '/nh', '/fo', 'csv'], { encoding: 'utf8', windowsHide: true }).stdout || ''
  const m = out.match(/^\s*"([^"]+)"/)
  return m ? m[1] : ''
}

function killTree(pid) {
  if (!pid) return
  if (IS_WIN) spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true })
  else { try { process.kill(pid, 'SIGKILL') } catch (e) { /* 已退出 */ } }
}

/**
 * 补一刀：one-file exe 的引导进程会先响应终止，把真正的解释器留成孤儿继续占端口。
 * 只回收「镜像名与本次拉起时一致」的监听进程，绝不碰别人起的后端。
 */
function reclaimListener({ port, image, ownedPid, onLog }) {
  const listener = listenerPidOnPort(port)
  if (!listener || listener === ownedPid) return false
  if (image && imageOfPid(listener).toLowerCase() !== image.toLowerCase()) {
    onLog(`端口 ${port} 上的监听进程 pid=${listener} 不是本次拉起的 ${image}，不动它`)
    return false
  }
  killTree(listener)
  onLog(`按端口回收孤儿后端进程 pid=${listener}（${image}）`)
  return true
}

export async function fetchHealth(baseUrl, timeoutMs = HEALTH_TIMEOUT_MS) {
  try {
    const res = await fetch(`${baseUrl}/api/health`, { signal: AbortSignal.timeout(timeoutMs) })
    if (!res.ok) return { ok: false, reason: `HTTP ${res.status}` }
    return { ok: true, body: await res.json() }
  } catch (e) {
    return { ok: false, reason: e.name === 'TimeoutError' ? '探测超时' : (e.cause?.code || e.message || '不可达') }
  }
}

export class BackendManager {
  constructor({ serverDir, dataRoot, host = '127.0.0.1', port = 8000, onLog = () => {}, onExit = null }) {
    this.serverDir = serverDir
    // 数据目录与代码目录分开：安装态没有 ../timeseries-studio-server，dataset 要落在 userData 下
    this.dataRoot = dataRoot || serverDir
    this.host = host
    this.port = port
    this.onLog = onLog
    this.onExit = onExit
    this.baseUrl = `http://${host}:${port}`
    this.child = null
    this.lastPid = null
    this.childImage = ''
    // owned = 这个后端是本次启动拉起来的，只有 owned 才允许回收
    this.owned = false
    this.ready = false
    this.stopping = false
    this.exitInfo = null
    this.runtime = null
    this.tail = []
  }

  get spawned() { return !!this.child }

  resolvePython() {
    const candidates = candidatePythons(this.serverDir)
    for (const c of candidates) {
      // 绝对路径必须存在；裸命令（python / python3）交给 PATH 判定
      if (path.isAbsolute(c)) { if (existsSync(c)) return c; continue }
      return c
    }
    return candidates[0]
  }

  /** 进程工作目录：安装态没有后端项目目录，退到数据目录（必须真实存在，否则 spawn 直接 ENOENT）。 */
  spawnCwd() {
    if (existsSync(this.serverDir)) return this.serverDir
    return this.dataRoot
  }

  /**
   * 挑运行时：TSS_BACKEND=python 强制走源码，否则有打包版 exe 就用它。
   * 两种运行时的命令行保持一致，数据目录统一由 dataRoot 决定
   * （开发态 = 后端项目目录，安装态 = userData），保证 dataset 只有一份。
   */
  resolveRuntime() {
    if (process.env.TSS_BACKEND !== 'python') {
      const exe = backendExeCandidates(this.serverDir).find((p) => existsSync(p))
      if (exe) {
        return {
          kind: 'exe',
          label: '打包版 exe',
          command: exe,
          args: ['--host', this.host, '--port', String(this.port),
            '--dataset-dir', path.join(this.dataRoot, 'dataset'),
            '--state-dir', path.join(this.dataRoot, '.tss-state')],
          note: exe
        }
      }
    }
    const python = this.resolvePython()
    if (path.isAbsolute(python) && !existsSync(python)) {
      throw new Error(`找不到 Python 解释器：${python}\n可用 TSS_PYTHON 指定解释器，或先跑 build-backend.bat 出打包版`)
    }
    if (!existsSync(path.join(this.serverDir, 'app', 'main.py'))) {
      throw new Error(`后端运行时缺失：既没有打包版 ${EXE_NAME}，${this.serverDir} 下也没有 app/main.py\n开发态可用 TSS_SERVER_DIR 指定后端目录，安装态请确认安装包内的 resources/${EXE_NAME} 完好`)
    }
    return {
      kind: 'python',
      label: '源码 uvicorn',
      command: python,
      args: ['-m', 'uvicorn', 'app.main:app', '--host', this.host, '--port', String(this.port)],
      note: python
    }
  }

  /**
   * @param {(s: {phase: string, text?: string, detail?: string, attempt?: number}) => void} onStatus
   */
  async start(onStatus = () => {}, { timeoutMs = 120000 } = {}) {
    const existing = await fetchHealth(this.baseUrl, 1200)
    if (existing.ok) {
      this.onLog(`复用已在运行的后端：${this.baseUrl} (v${existing.body.version || '?'})`)
      onStatus({ phase: 'ready', text: '后端已在运行，直接复用', attempt: 0 })
      return { owned: false, health: existing.body, adopted: true }
    }

    if (process.env.TSS_BACKEND_NO_SPAWN === '1') {
      throw new Error(`后端未在线（${this.baseUrl}），且 TSS_BACKEND_NO_SPAWN=1 已禁用自动拉起`)
    }
    if (existing.reason !== '不可达' && !/ECONNREFUSED|ENOTFOUND|fetch failed|探测超时/i.test(String(existing.reason))) {
      this.onLog(`端口 ${this.port} 上有响应但不是本项目后端：${existing.reason}`)
    }

    const rt = this.resolveRuntime()
    this.runtime = rt
    const cwd = this.spawnCwd()
    this.onLog(`启动后端（${rt.label}）：${rt.command} ${rt.args.join(' ')}  (cwd=${cwd})`)
    onStatus({
      phase: 'spawning',
      text: rt.kind === 'exe' ? '正在拉起打包版后端' : '正在拉起 Python 后端',
      runtime: rt.kind,
      detail: `${path.basename(rt.command)} --port ${this.port}`
    })

    this.child = spawn(rt.command, rt.args, {
      cwd,
      env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      detached: false
    })
    this.owned = true
    // one-file exe 的父子两代镜像名相同，按这个名字认孤儿进程
    this.childImage = path.basename(rt.command)

    const collect = (chunk) => {
      String(chunk).split(/\r?\n/).filter(Boolean).forEach((line) => {
        this.tail.push(line)
        if (this.tail.length > 60) this.tail.shift()
        this.onLog(`[后端] ${line}`)
      })
    }
    this.child.stdout.on('data', collect)
    this.child.stderr.on('data', collect)

    this.ready = false
    this.stopping = false
    this.exitInfo = null
    this.child.on('exit', (code, signal) => {
      this.exitInfo = { code, signal }
      // 就绪之后才崩的算运行期事故，交给 onExit 弹窗；启动期崩了由下面的轮询抛出
      if (this.ready && !this.stopping) this.onExit?.({ code, signal, tail: this.tail.slice(-10) })
    })

    const deadline = Date.now() + timeoutMs
    let attempt = 0
    // 首次 import pandas / sklearn 可能要十几秒，这里只做节流轮询，不猜它好了没
    while (Date.now() < deadline) {
      attempt += 1
      const probe = await Promise.race([
        fetchHealth(this.baseUrl, 1200).then((r) => (r.ok ? r : null)),
        new Promise((res) => setTimeout(() => res(null), 1400))
      ])
      if (probe) {
        this.ready = true
        this.onLog(`后端就绪：${this.baseUrl} (v${probe.body.version || '?'} · 能力 ${(probe.body.capabilities || []).length} 项)`)
        onStatus({ phase: 'ready', text: '后端已就绪', attempt })
        return { owned: true, health: probe.body, pid: this.child.pid, attempts: attempt, runtime: rt.kind }
      }
      if (this.exitInfo) {
        throw new Error(`后端进程已退出（code=${this.exitInfo.code} signal=${this.exitInfo.signal || '-'}）\n${this.tail.slice(-10).join('\n')}`)
      }
      onStatus({
        phase: 'waiting',
        text: attempt <= 3 ? '等待 uvicorn 监听端口' : '后端正在载入依赖',
        detail: this.tail.length ? this.tail[this.tail.length - 1] : undefined,
        attempt
      })
      await new Promise((res) => setTimeout(res, PROBE_INTERVAL_MS))
    }
    await this.stop()
    throw new Error(`后端在 ${timeoutMs / 1000} 秒内没有就绪\n${this.tail.slice(-10).join('\n')}`)
  }

  /** 只杀自己拉起的进程；8000 上的既有后端原样留着。 */
  async stop({ timeoutMs = 5000 } = {}) {
    if (!this.owned || !this.child) return { stopped: false, reason: '未托管后端进程，无需回收' }
    this.stopping = true
    const pid = this.child.pid
    this.lastPid = pid
    const child = this.child
    this.child = null
    const alreadyGone = () => child.exitCode !== null || child.signalCode !== null

    child.kill('SIGTERM')
    const born = Date.now()
    while (!alreadyGone() && Date.now() - born < timeoutMs) {
      await new Promise((res) => setTimeout(res, 100))
    }
    if (IS_WIN) {
      // Windows 下 SIGTERM 只作用于直接子进程，端口仍可能被占：按进程树强杀兜底
      killTree(pid)
      this.onLog(`已回收后端进程树 pid=${pid}`)
    }
    // 打包版引导进程会先响应终止、把真正的解释器留成孤儿继续占端口，按端口再认一次
    const orphan = reclaimListener({ port: this.port, image: this.childImage, ownedPid: pid, onLog: this.onLog })
    const health = await fetchHealth(this.baseUrl, 1000)
    if (health.ok) this.onLog(`警告：回收后 ${this.baseUrl} 仍有健康响应，可能有第二个后端实例`)
    return { stopped: true, pid, orphan }
  }

  /** 主进程被强杀前的同步兜底：宁可留一个孤儿后端，也不能留一个杀不掉的应用。 */
  stopSync() {
    if (!this.owned) return
    const pid = this.child?.pid ?? this.lastPid
    if (pid) killTree(pid)
    reclaimListener({ port: this.port, image: this.childImage, ownedPid: pid, onLog: () => {} })
  }
}

export function defaultServerDir(appRoot) {
  const override = process.env.TSS_SERVER_DIR
  if (override) return path.resolve(override)
  return path.resolve(appRoot, '..', 'timeseries-studio-server')
}
