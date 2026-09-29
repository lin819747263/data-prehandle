// 预加载脚本：沙箱内可用的最小桥。渲染进程拿不到 Node，这里只透出宿主注入的地址与状态事件。
const { contextBridge, ipcRenderer } = require('electron')

const arg = (prefix) => (process.argv.find((a) => a.startsWith(`${prefix}=`)) || '').slice(prefix.length + 1)

const apiBase = arg('--tss-api-base')
const mode = arg('--tss-frontend-mode')
const win = arg('--tss-window') || 'main'

contextBridge.exposeInMainWorld('tssDesktop', {
  desktop: true,
  window: win,
  mode,
  apiBase: apiBase || null,
  platform: process.platform,
  onStatus: (cb) => {
    if (win !== 'splash') return () => {}
    const handler = (_e, msg) => cb(msg)
    ipcRenderer.on('tss:status', handler)
    return () => ipcRenderer.removeListener('tss:status', handler)
  },
  // 监听器注册好之后由闪屏自己报到：主进程在此之前攒着状态消息，避免首屏后的高速推送落空
  splashReady: () => ipcRenderer.send('tss:splash-ready'),
  resize: (height) => ipcRenderer.send('tss:splash-resize', height),
  action: (type, payload) => ipcRenderer.invoke('tss:action', { type, payload }),
  backendInfo: () => ipcRenderer.invoke('tss:action', { type: 'backend-info' })
})
