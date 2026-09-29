// 前端宿主：桌面端必须以 http://127.0.0.1:<port> 的形态加载页面，
// 因为后端的 CORS 白名单是 ^http://(localhost|127\.0\.0\.1)(:\d+)?$，file:// 的 Origin 是 null 会被拒。
// 两种模式：vite dev（开发）/ dist 静态服务（预览与打包后）。
import { createServer } from 'node:http'
import { spawn, spawnSync } from 'node:child_process'
import { createReadStream, existsSync, statSync } from 'node:fs'
import net from 'node:net'
import path from 'node:path'

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.otf': 'font/otf',
  '.eot': 'application/vnd.ms-fontobject',
  '.map': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8'
}

async function responds(url, timeoutMs = 1200) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) })
    return res.ok
  } catch (e) {
    return false
  }
}

/** 先试约定端口；被占就让操作系统挑一个空闲端口（后端 CORS 放行本机任意端口）。 */
function pickFreePort(host, preferred) {
  const tryListen = (port) => new Promise((resolve) => {
    const probe = net.createServer()
    probe.once('error', () => resolve(null))
    probe.listen(port, host, () => probe.close(() => resolve(port)))
  })
  return tryListen(preferred).then(async (p) => p ?? await tryListen(0) ?? preferred + 1)
}

/** vite dev server：约定端口上已有人在服务就直接复用，否则挑一个空闲端口自己起。 */
export async function startViteFrontend({ appRoot, host = '127.0.0.1', port = Number(process.env.TSS_FRONTEND_PORT) || 5321, onLog = () => {} }) {
  const baseUrl = `http://${host}:${port}`
  onLog(`检查前端 dev server：${baseUrl}`)
  if (await responds(`${baseUrl}/`)) {
    onLog('复用在运行的 vite dev server')
    return { url: baseUrl, mode: 'vite', owned: false, stop: async () => ({ stopped: false, reason: '未托管前端进程' }), stopSync: () => {} }
  }
  // 后端 CORS 放行本机任意端口，所以端口被占时换个空闲端口比抢端口更省事
  const actual = await pickFreePort(host, port)
  if (actual !== port) onLog(`${port} 已被占用，改用 ${actual}`)
  const url = `http://${host}:${actual}`

  const viteBin = path.join(appRoot, 'node_modules', 'vite', 'bin', 'vite.js')
  if (!existsSync(viteBin)) throw new Error(`未找到 vite：${viteBin}\n请先执行 npm install`)
  onLog(`启动 vite dev server：vite --host ${host} --port ${actual} --strictPort`)
  const child = spawn(process.execPath, [viteBin, '--host', host, '--port', String(actual), '--strictPort'], {
    cwd: appRoot,
    // 打包后的 electron 二进制以 Node 兼容模式运行 vite（开发路径才用得到）
    env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe']
  })
  const tail = []
  const collect = (chunk) => String(chunk).split(/\r?\n/).filter(Boolean).forEach((l) => {
    tail.push(l); if (tail.length > 40) tail.shift(); onLog(`[vite] ${l}`)
  })
  child.stdout.on('data', collect)
  child.stderr.on('data', collect)

  const deadline = Date.now() + 60000
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`vite 进程已退出（code=${child.exitCode}）\n${tail.slice(-6).join('\n')}`)
    if (await responds(`${url}/`)) {
      return {
        url,
        mode: 'vite',
        owned: true,
        child,
        stop: async () => {
          if (child.exitCode !== null) return { stopped: true }
          child.kill('SIGTERM')
          await new Promise((r) => setTimeout(r, 400))
          // vite 会派生 esbuild 服务进程，Windows 上按进程树收干净
          if (process.platform === 'win32') spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true })
          else { try { process.kill(child.pid, 'SIGKILL') } catch (e) { /* 已退出 */ } }
          return { stopped: true }
        },
        stopSync: () => {
          if (child.exitCode !== null) return
          if (process.platform === 'win32') spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true })
          else { try { process.kill(child.pid, 'SIGKILL') } catch (e) { /* 已退出 */ } }
        }
      }
    }
    await new Promise((r) => setTimeout(r, 250))
  }
  child.kill('SIGTERM')
  throw new Error(`vite dev server 60 秒内没有响应\n${tail.slice(-8).join('\n')}`)
}

/** dist 静态服务：只读 dist 目录内的文件，路径穿越一律拒绝。 */
export function startStaticFrontend({ distDir, host = '127.0.0.1', port = Number(process.env.TSS_FRONTEND_PORT) || 0, onLog = () => {} }) {
  const index = path.join(distDir, 'index.html')
  if (!existsSync(index)) throw new Error(`未找到构建产物：${index}\n请先执行 npm run build`)

  const server = createServer((req, res) => {
    const send = (code, body, headers = {}) => {
      res.writeHead(code, { 'Cache-Control': 'no-cache', ...headers })
      res.end(body)
    }
    let pathname
    try {
      pathname = decodeURIComponent(new URL(req.url, `http://${host}`).pathname)
    } catch (e) {
      return send(400, 'bad request')
    }
    if (req.method !== 'GET' && req.method !== 'HEAD') return send(405, 'method not allowed')

    const target = path.resolve(distDir, '.' + pathname)
    const inside = target === path.resolve(distDir) || target.startsWith(path.resolve(distDir) + path.sep)
    if (!inside) return send(403, 'forbidden')

    let file = target
    try {
      if (statSync(file).isDirectory()) file = path.join(file, 'index.html')
      if (!existsSync(file)) throw new Error('missing')
    } catch (e) {
      // 无前缀路由回落到 index.html，带扩展名的资源缺失就直接 404（别把 HTML 当 PNG 发出去）
      if (path.extname(pathname)) return send(404, 'not found')
      file = index
    }
    const type = MIME[path.extname(file).toLowerCase()] || 'application/octet-stream'
    if (req.method === 'HEAD') return send(200, '', { 'Content-Type': type })
    res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-cache' })
    createReadStream(file).pipe(res)
  })

  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(port, host, () => {
      const actual = server.address().port
      const url = `http://${host}:${actual}`
      onLog(`静态托管 dist：${url}`)
      resolve({ url, mode: 'dist', owned: true, stop: async () => { server.close(); return { stopped: true } } })
    })
  })
}
