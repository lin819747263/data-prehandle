// TimeSeries Studio 桌面端主进程。
// 启动次序：闪屏 → 起前端 → 起（或复用）Python 后端 → 后端 /api/health 通过后开主窗。
// 退出次序：主窗关闭 → 回收自己 spawn 的后端与 vite 子进程 → app.quit()。
// 复用的既有后端（别的终端里跑着的那个）不属于本进程，退出时不动它。
import { app, BrowserWindow, ipcMain, shell, dialog } from 'electron'
import { appendFileSync, existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { BackendManager, defaultServerDir } from './backend-manager.js'
import { startViteFrontend, startStaticFrontend } from './frontend-server.js'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const APP_ROOT = path.resolve(HERE, '..')

// 参数优先级：命令行 > 环境变量 > 默认值
const flag = (name) => (process.argv.find((a) => a.startsWith(`--${name}=`)) || '').split('=').slice(1).join('=')
const config = (name, env, dflt) => flag(name) || process.env[env] || dflt

const SERVER_DIR = path.resolve(config('server-dir', 'TSS_SERVER_DIR', defaultServerDir(APP_ROOT)))
const BACKEND_HOST = config('host', 'TSS_HOST', '127.0.0.1')
const BACKEND_PORT = Number(config('backend-port', 'TSS_PORT', '8000'))
const BACKEND_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}`
// 前端必须以 http://127.0.0.1:<port> 暴露：后端 CORS 只放行本机 http 源，file:// 的 Origin 是 null
const FRONTEND_MODE = config('frontend', 'TSS_FRONTEND', app.isPackaged ? 'dist' : 'vite')
const LOG_FILE = path.join(app.getPath('userData'), 'desktop.log')

let splash = null
let mainWin = null
let frontend = null
let backend = null
let shuttingDown = false
let booted = false
let splashReady = false
const statusQueue = []

function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}`
  console.log(line)
  try { appendFileSync(LOG_FILE, line + '\n') } catch (e) { /* 日志写不进不该影响启动 */ }
}

function sendToSplash(msg) {
  if (!splash || splash.isDestroyed()) return
  // 闪屏的监听器要等它的脚本跑起来才存在，这里先攒着，等 tss:splash-ready 再按序补发
  if (!splashReady) { statusQueue.push(msg); return }
  splash.webContents.send('tss:status', msg)
}

ipcMain.on('tss:splash-ready', (e) => {
  if (!splash || e.sender !== splash.webContents) return
  splashReady = true
  const pending = statusQueue.splice(0)
  pending.forEach((m) => sendToSplash(m))
})

// 闪屏是不可缩放窗口，内容长高（出错面板）时由它自己报一个高度过来
ipcMain.on('tss:splash-resize', (e, height) => {
  if (!splash || e.sender !== splash.webContents) return
  const [w] = splash.getSize()
  const h = Math.max(380, Math.min(620, Math.round(Number(height) || 420)))
  if (Math.abs(h - splash.getSize()[1]) > 2) splash.setSize(w, h)
})

function createSplash() {
  splash = new BrowserWindow({
    width: 640,
    height: 420,
    frame: false,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    alwaysOnTop: true,
    center: true,
    show: false,
    backgroundColor: '#f1f5f9',
    title: 'TimeSeries Studio',
    webPreferences: {
      preload: path.join(HERE, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      additionalArguments: ['--tss-window=splash', `--tss-frontend-mode=${FRONTEND_MODE}`]
    }
  })
  splash.setMenuBarVisibility(false)
  splash.loadFile(path.join(HERE, 'splash.html'))
  splash.once('ready-to-show', () => splash.show())
  splash.on('closed', () => { splash = null })
}

function createMainWindow() {
  mainWin = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    show: false,
    backgroundColor: '#f1f5f9',
    title: 'TimeSeries Studio',
    webPreferences: {
      preload: path.join(HERE, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      additionalArguments: [
        '--tss-window=main',
        `--tss-frontend-mode=${FRONTEND_MODE}`,
        `--tss-api-base=${BACKEND_URL}`
      ]
    }
  })
  mainWin.setMenuBarVisibility(process.env.TSS_MENU === '1')
  mainWin.once('ready-to-show', () => {
    mainWin.show()
    if (process.env.TSS_OPEN_DEVTOOLS === '1') mainWin.webContents.openDevTools({ mode: 'detach' })
  })
  mainWin.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })
  mainWin.on('closed', () => { mainWin = null })
  mainWin.loadURL(frontend.url)
  log(`主窗加载：${frontend.url}`)
}

ipcMain.handle('tss:action', async (_e, { type }) => {
  if (type === 'quit') { app.quit(); return { ok: true } }
  if (type === 'retry-backend') {
    const ok = await bootBackendQuiet()
    if (ok && !booted) await finishBoot()
    return { ok }
  }
  if (type === 'backend-info') {
    return {
      ok: true, url: BACKEND_URL, mode: FRONTEND_MODE,
      owned: !!backend?.owned, pid: backend?.child?.pid ?? null,
      runtime: backend?.runtime?.kind || (backend?.owned ? 'unknown' : 'adopted'),
      runtimePath: backend?.runtime?.note || null
    }
  }
  return { ok: false, reason: `未知动作 ${type}` }
})

async function bootBackend() {
  if (!backend) {
    backend = new BackendManager({
      serverDir: SERVER_DIR,
      host: BACKEND_HOST,
      port: BACKEND_PORT,
      onLog: log,
      // 已经在用的后端半途崩了：问用户重启还是退出，而不是让工作台静默变只读
      onExit: async ({ code, signal }) => {
        if (shuttingDown) return
        log(`运行中后端退出 code=${code} signal=${signal}`)
        const choice = await dialog.showMessageBox(mainWin || undefined, {
          type: 'error',
          title: '后端已退出',
          message: `Python 后端进程意外退出（code=${code ?? '未知'}）。`,
          detail: '可能是解释器被结束，或后端自身异常退出。可以重新启动后端。',
          buttons: ['重新启动后端', '退出应用'],
          defaultId: 0,
          cancelId: 1
        })
        if (choice.response === 0) await bootBackendQuiet()
        else app.quit()
      }
    })
  }
  sendToSplash({ phase: 'spawning', text: '正在拉起 Python 后端' })
  const r = await backend.start((s) => sendToSplash(s), { timeoutMs: Number(process.env.TSS_BOOT_TIMEOUT) || 120000 })
  sendToSplash({
    phase: 'ready',
    text: '后端已就绪，正在进入工作台',
    attempt: r.attempts || 0,
    capabilities: (r.health?.capabilities || []).length,
    version: r.health?.version || '',
    adopted: !r.owned,
    runtime: r.runtime || (r.adopted ? 'adopted' : ''),
    apiBase: BACKEND_URL
  })
  log(`后端就绪：${BACKEND_URL}（${r.owned ? '本进程拉起' : '复用既有实例'}）`)
  return r
}

/** 失败时把原因写回闪屏（那里有重试按钮），调用方只关心成没成。 */
async function bootBackendQuiet() {
  try {
    await bootBackend()
    return true
  } catch (e) {
    log(`后端启动失败：${e.message}`)
    sendToSplash({ phase: 'error', message: String(e.message || e).slice(0, 4000) })
    return false
  }
}

async function finishBoot() {
  // 让"已就绪"这一帧真的能被看见；TSS_SPLASH_HOLD 只是给截图/调试拉长停留，默认 0.65 秒
  await new Promise((r) => setTimeout(r, Number(process.env.TSS_SPLASH_HOLD) || 650))
  createMainWindow()
  if (splash && !splash.isDestroyed()) splash.close()
  booted = true
}

async function boot() {
  createSplash()
  sendToSplash({ phase: 'boot', text: `正在启动工作台前端（${FRONTEND_MODE}）` })
  log(`桌面端启动：mode=${FRONTEND_MODE} server=${SERVER_DIR} backend=${BACKEND_URL}`)

  frontend = FRONTEND_MODE === 'dist'
    ? await startStaticFrontend({ distDir: path.join(APP_ROOT, 'dist'), onLog: log })
    : await startViteFrontend({ appRoot: APP_ROOT, onLog: log })
  sendToSplash({ phase: 'frontend-ready', text: '工作台前端已就绪', detail: frontend.url })
  log(`前端就绪：${frontend.url}（${frontend.owned ? '本进程拉起' : '复用既有实例'}）`)

  if (!await bootBackendQuiet()) return
  await finishBoot()
}

async function shutdown() {
  if (shuttingDown) return
  shuttingDown = true
  log('开始回收子进程…')
  try { if (backend) await backend.stop() } catch (e) { log(`回收后端失败：${e.message}`) }
  try { if (frontend) await frontend.stop() } catch (e) { log(`回收前端失败：${e.message}`) }
  log('子进程回收完成')
}

// 被任务管理器直接杀掉时没有 before-quit，只能同步补一刀 taskkill；宁可留日志也别留孤儿后端
process.on('exit', () => {
  try { backend?.stopSync(); frontend?.stopSync?.() } catch (e) { /* 退出路径不报错 */ }
})
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => { shutdown().then(() => app.exit(0)) })
}

const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (mainWin) { if (mainWin.isMinimized()) mainWin.restore(); mainWin.focus() }
    else if (splash) splash.focus()
  })

  app.whenReady().then(() => {
    if (!existsSync(SERVER_DIR)) log(`警告：后端目录不存在：${SERVER_DIR}（可用 TSS_SERVER_DIR 指定）`)
    boot().catch(async (e) => {
      log(`启动失败：${e.stack || e.message}`)
      sendToSplash({ phase: 'error', message: String(e.message || e).slice(0, 4000) })
      if (!booted && !splash) {
        await dialog.showMessageBox({ type: 'error', title: '启动失败', message: String(e.message || e) })
        app.quit()
      }
    })
  })

  app.on('window-all-closed', () => { app.quit() })
  app.on('before-quit', (e) => {
    if (shuttingDown) return
    e.preventDefault()
    shutdown().finally(() => app.quit())
  })
}
