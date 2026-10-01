import { createApp, computed } from 'vue'
// Element Plus 不再全量引入：本项目 0 个 <el-*> 模板标签，组件库只被 ElMessage / ElMessageBox 用到，
// 而这两者在 store.js 与各 .vue 里是「直接从 'element-plus' 具名 import」的（element-plus 的 sideEffects
// 只把样式文件列为副作用，具名 import 会被 Rollup tree-shake）。因此这里：
//   - 删掉 `import ElementPlus from 'element-plus'` 与 `.use(ElementPlus)`（全量注册，是旧 JS 臃肿主因）；
//   - 删掉 `element-plus/dist/index.css`（全量 EP 样式，约 343KB），改为只引这两个组件用到的样式入口：
//       message/style/css 会带 base+badge；message-box/style/css 会带 base+input+button+overlay，覆盖弹层全部依赖。
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { provideGlobalConfig } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
// FontAwesome 换成本地 woff2-only 版：@font-face 只声明 woff2，dist 不再产出 400KB 级 fa-*-*.ttf。
// solid/regular/brands 三个 woff2 仍被引用，99 个图标照常显示。
import './styles/fontawesome-woff2.css'
import App from './App.vue'
import './styles/app.css'

const app = createApp(App)

// 没有 .use(ElementPlus) 后，脱离 app 单独 render 的 ElMessageBox 会退回英文按钮（OK/Cancel）。
// MessageBox 组件读的是 element-plus 内部「模块级 globalConfig」，所以这里用
// provideGlobalConfig(config, app, /* global */ true) 把 zh-CN locale 一次性写进全局 ——
// 全部 6 处确认框（都在别人负责的 .vue / store.js，只传了 {type:'warning'}，未显式传 locale）
// 的按钮文案都会因此变成「确定 / 取消」，无需改动任何调用点。
provideGlobalConfig(computed(() => ({ locale: zhCn })), app, true)

app.mount('#app')
