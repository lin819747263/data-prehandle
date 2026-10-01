// ============================================================
// echarts 按需注册（tree-shaking）
// 取代 Step3Explore.vue / Step4Quality.vue 里原来的 `import * as echarts from 'echarts'`
// 全量引入（约 1.05MB）。这里只 install 本项目两张画布真正用到的图表与组件，
// 缺任何一个都会让对应的 setOption 键/series type 在浏览器里画成空白图。
//
// 事实来源（逐处核对，非猜测）：
//   Step3Explore.vue -> drawMain setOption(第235行) / drawDist setOption(第272行)
//   Step4Quality.vue -> renderChart setOption(第177行)
// ============================================================
import * as echarts from 'echarts/core'
import { LineChart, BarChart, ScatterChart } from 'echarts/charts'
import {
  // GridComponent —— 三张图都要：Step3 drawMain / drawDist、Step4 renderChart 全都写了
  //   顶层 grid:{} 与 xAxis:{type:'category'}/yAxis:{type:'value'}。它提供直角坐标系容器与轴，
  //   缺了就没有坐标轴与绘图网格，series 无处安放。
  GridComponent,
  // TooltipComponent —— 三张图都写了 tooltip:{trigger:'axis', formatter(...)}。
  //   Step3 drawMain/drawDist 与 Step4 renderChart 的悬浮提示全靠它。
  //   注意它的 install 内部 use(installAxisPointer)，因此 tooltip 里的
  //   axisPointer:{type:'cross'}（Step3 drawMain、Step4 renderChart）与
  //   axisPointer:{type:'shadow'}（Step3 drawDist）也一并具备，不必再单独注册 AxisPointerComponent。
  TooltipComponent,
  // LegendComponent —— Step3 drawMain 有 legend:{...}，Step4 renderChart 有 legend:{type:'scroll'}。
  //   LegendComponent 的 install 同时装 plain 与 scroll 两种模式，故 scroll 型图例（Step4 列多时滚动）可用。
  //   drawMain/drawDist 用了 replaceMerge:['series','legend']，legend 组件必须在册，否则替换报错。
  LegendComponent,
  // DataZoomComponent —— Step3 drawMain 与 Step4 renderChart 的 dataZoom:[{type:'inside'},{type:'slider'}]。
  //   它的 install 内部 use(installDataZoomInside)+use(installDataZoomSlider)，滚轮缩放(inside)与底部滑条(slider)都齐。
  //   两图都 chart.on('dataZoom') 读缩放窗口，缺它就没有区间缩放。
  DataZoomComponent,
  // ToolboxComponent —— Step4 renderChart 写了 toolbox:{feature:{brush:...}}，提供工具条按钮容器。
  ToolboxComponent,
  // BrushComponent —— Step4 renderChart 的顶层 brush:{toolbox:['lineX','clear'], xAxisIndex:0}、
  //   dispatchAction({type:'takeGlobalCursor', key:'brush'}) / dispatchAction({type:'brush',areas:[]})、
  //   以及 chart.on('brushSelected')。它的 install 还会 registerFeature('brush', BrushFeature)，
  //   于是 toolbox.feature.brush 才有对应的功能项，二者缺一不可。
  BrushComponent,
  // MarkLineComponent —— Step3 drawDist 的 series.markLine（μ 均值线 / M 中位数线参考线），
  //   Step4 renderChart 每条折线 series.markLine（缺失时段竖直虚线）。没有它这两处标记线不画。
  MarkLineComponent,
  // TitleComponent —— Step3 drawMain 写了 title:{text: ...}（曲线标题）。drawDist/renderChart 无 title，
  //   但 drawMain 有，缺则标题区空白。
  TitleComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  // series type 覆盖：line（Step3 drawMain、Step4 折线）、bar（Step3 drawDist 直方图）、scatter（Step4 异常散点 dots）
  LineChart,
  BarChart,
  ScatterChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  ToolboxComponent,
  BrushComponent,
  MarkLineComponent,
  TitleComponent,
  // CanvasRenderer —— echarts.init(dom) 需要渲染器；本项目全部走 canvas（未用 SVG）。
  CanvasRenderer,
])

// 明确不注册（本项目两张图均未用到，省下来才是“瘦身”）：
//   - GraphicComponent：Step3 用的 new echarts.graphic.LinearGradient(...) 是 echarts/core 的运行时 API 导出，
//     core 自带，不需要为它注册组件；且两图都没有声明式的顶层 graphic:{} 图元。
//   - MarkPointComponent：无 series.markPoint。
//   - VisualMapComponent：无 visualMap。
//   - 其余 series（pie/gauge/radar/map…）与非笛卡尔坐标系组件（polar/radar/geo…）一概不引。

export default echarts
