import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// 桌面端（electron/）以 http://127.0.0.1:<port> 加载这里的服务：
// 后端 CORS 只放行 ^http://(localhost|127\.0\.0\.1)(:\d+)?$，file:// 的 Origin 是 null 会被拒。
export default defineConfig({
  plugins: [vue(), tailwindcss()],
  server: { host: '127.0.0.1', port: 5321, strictPort: true },
  preview: { host: '127.0.0.1', port: 5322, strictPort: true }
})
