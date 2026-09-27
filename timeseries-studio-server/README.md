# TimeSeries Studio Server

时序数据清洗工作台（`../timeseries-studio`）的 FastAPI 后端。

**数据加工在这里真实发生。** 明细不再整张塞进浏览器：`POST /api/ws` 之后 DataFrame 留在服务端进程内，
前端只持有元数据（列、类型、频率、时间格式、版本号、命令序列）与"当前页附近"的行窗口（每页最多 500 行）。
每个加工动作是一条打在服务端的命令，撤销 = 让服务端回到某个版本号。

为什么必须这样：一张 11000 行 × 40 列的表有 440000 个单元格，迁移前的浏览器既要存整表、又要存 25 份深拷贝快照
做撤销，数据量上来就撑不住 —— 那两套东西现在都不在前端了（快照栈与 `localStorage` 里的整表会话已删除）。

| 能力 | 归属 |
| --- | --- |
| 工作区：载入 / 分页取行 / 整列取数 / 总览统计 | 后端（`pyarrow` + `pandas`） |
| 时间格式识别与渲染、单位换算、列运算、重命名/删列、重采样 | 后端，且可版本号重放 |
| 统计矩阵、25 桶直方图、多列叠加曲线、首个完整行 | 后端，整表数完后只回笼统数字与抽稀后的点 |
| 缺失段诊断与填补、重复时间戳合并 | 后端（pandas + numpy，`services/quality.py`） |
| 异常检测 3σ / IQR / 滑动窗口 MAD / 表达式 / 孤立森林 | 后端，`iforest_sklearn` 用 `scikit-learn.IsolationForest` |
| 异常修复（截断 / 置空 / 生成掩码）与布尔掩码增删 | 后端，检测结果留在服务端，前端不回传行索引 |
| CSV / Excel / Parquet / Feather 导出 | 后端从自己的工作区直接编码字节流，明细不过网络；浏览器单文件伪造不出合法的 thrift / Arrow IPC 编码 |
| 数据集目录读写 | 后端，浏览器无法枚举或持久化本地目录 |
| 撤销 / 重做（版本号游标 + 按日志重放） | 后端，日志落盘，重启后第一次访问即重建那一版 |
| 会话（第几步、审计记录、UI 状态） | 后端 `<cwd>/.tss-state/session.json`，`GET` 时逐个核对引用的工作区是否还在 |
| 外生变量三条来源（预设 / 公式 / 侧表对齐） | 后端生成与对齐，浏览器不持有任何外生列的整列数据 |
| 图表交互、UI 状态、当前页附近的行窗口缓存、审计链的游标投影 | 前端 |

唯一的落盘位置有两处：数据集目录 `<cwd>/dataset`（`TSS_DATASET_DIR` 可覆盖，不存在时自动创建）
与状态目录 `<cwd>/.tss-state`（`TSS_STATE_DIR`，存命令日志与会话）。进程内的帧注册表不落数据库，
但**日志在磁盘上**：后端重启后第一次访问某个工作区，就按它那份日志把帧重放出来（同一 wsId、同一版本号）。

## 运行

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

接口文档：http://127.0.0.1:8000/docs

前端地址默认 `http://127.0.0.1:8000`，改址在浏览器控制台执行
`localStorage.setItem('tss.apiBase', 'http://127.0.0.1:9000')` 后刷新。

**后端不在线时前端只读**：不再保留任何浏览器端的算法兜底实现，避免同一份数据出现两套结果。

**迁移进度**：五期全部落地。第①~④步走服务端 —— 清洗与异常（②）、特征构建（③）、统计矩阵 / 直方图 /
叠加曲线 / 导出（④）都在服务端的整帧上算，浏览器只拿到元数据与当前页窗口。
`POST /api/ws/{id}/replace`（整表回传通道）**已删除**，浏览器再也无法用一份本地表覆盖服务端；
`GET /columns` 保留但只服务"按时间戳对齐生成外生变量"这类必须要整列的操作，一次最多两三列。
第⑤期把**撤销与会话**也搬到服务端：撤销/重做就是挪服务端游标（浏览器不再存 25 份深拷贝快照，
`localStorage` 里那份整表会话已删），命令日志落盘、重启可重放，外生变量三条来源全部在服务端生成。
至此浏览器侧不再保留任何一份"整表 + 快照"，也就没有第二套算法能算出不同的数。

## 工作区模型

```
载入（preset / dataset / 上传）→ base_df + base_meta
每条加工命令 append 到 ops[]
当前帧 = 从 base_df 重放 ops[:cursor]
```

- 命令日志是**游标**而不是栈：`version == cursor`，`POST /api/ws/{id}/restore {version}` 只挪游标，
  往回是撤销、往前是重做，日志本身不截断（截断就等于把"重做"这个按钮做成报错）
- 游标之后留着的尾巴是"被撤销、等待重做"的那段；只有在回退之后又执行新命令时，`apply()` 才把尾巴截掉（分支语义）
- `restore` 是原子的：中途某个 handler 抛错就整帧回滚到调用前的状态，不会留下"版本号涨了、帧却坏了"
- 每条命令都必须**自可重放**。依赖服务端上下文的命令（异常修复要读检测出的逐列行索引）把那份上下文
  钉进记录的私有 `replay` 字段：它不进 HTTP 响应、也不进 `meta.ops`，只在重放时回流给 handler。
  因此撤销掉"生成掩码"再重做，不需要重新检测也能得到逐格一致的结果
- 检测索引是按某一颗帧算的，`restore` 之后一律作废（`anomaly=null`），界面据此把旧结果标成失效
- 上限：同时 8 个工作区（LRU 淘汰）、单个工作区 500 万单元格、单次上传 64 MiB、单页 500 行
- `GET /api/health` 的 `capabilities` / `limits` 就是这套契约，前端据此决定按钮是否可点

### 与前端口径的一致性

- 列 key 规则、时间格式投票、采样频率（排序后正间隔的中位数）、派生列 4 位小数，均与
  `../timeseries-studio/src/utils.js`、`store.js` 逐条对拍；中文列名会折叠成 `____`，
  因为前端的 `safeKey` 就是这个规则（保留一致性而非"修正"它）
- 时间列内部一律存 `datetime64`，`timeFormat` 只管渲染。因此重复时间戳、频率识别不再依赖显示格式
  —— 这是相对旧浏览器实现的**行为修正**（旧代码比较的是渲染后的字符串）
- `interpolate` 重采样为真正的线性插值。旧浏览器代码把它静默当成均值聚合 —— **行为修正**
- 第三步的统计与抽稀**保持迁移前浏览器实现的口径**，而不是套用 pandas 的默认值：`q1 = sorted[int(n·0.25)]`、
  `q3 = sorted[int(n·0.75)]`（pandas 的 `quantile` 会线性插值），偶数个有效值的中位数取中间两个的平均，
  标准差是**总体**（÷n，pandas 默认 ÷n-1），缺失率分母是总行数而非有效行数，`globalMax` 从 1 起只增不减；
  直方图固定 25 桶、桶宽 `(hi-lo)/25`（全列同值时为 1）、末值夹进最后一桶；
  `mean` 抽稀的窗口均值先剔除缺失再按有效值平均，逐值用 float 顺序累加后取 2 位小数
  （`features.scalar_round(·, 2)`，与前端 `parseFloat(v.toFixed(2))` 同值）。
  这套规则由 `scripts/ref_phase4.mjs` 用**纯 JS 独立重实现**、直接读 CSV、不经 pandas 也不经服务端代码，
  再与 HTTP 返回逐格对拍（见下方"验证"）
- 第四步（清洗与异常）的每个数字都在服务端算：缺失段由 `missing_runs` 扫描、4 位小数一律走
  `round4`（`Decimal` + `ROUND_HALF_UP`，与前端 `parseFloat(v.toFixed(4))` 同值）；3σ 用**总体**标准差（÷n）、
  IQR 的分位数取 `sorted[int(n·q)]`、滑动 MAD 固定窗口 33 / min_periods 5 / k=4.0，三者与 `utils.js`
  和独立 numpy 重算三方对拍（见下方"验证"）
- 生成/删除掩码列只加列、不动既有数值（`_valueChange: False`）：它不该作废上一次的检测索引，
  也不该把曲线图的数值抖一下
- 修复模式 `mask_only` 不改原值，`clip`/`nan_impute` 才改；`clip` 之后可能残留 ≤5e-5 的贴边差，
  那是 4 位小数进位的固有结果，不是缺陷
- 第五步的窗口口径**不是** pandas 现成算子的口径：`rolling_agg` 先剔除缺失、按有效值个数聚合
  （`pandas.rolling(min_periods=w)` 是遇 NaN 传播），标准差取**总体**（÷n），均值/标准差/中位数/EWM
  一律 `round_half_up(·, 2)`，类别取值与序数编号按**首次出现**顺序，目标编码的组均值按行序累加。
  这套规则与迁移前的浏览器实现逐条一致，是刻意的**行为保持**（不是"修正"成 pandas 的样子）
- 前端"查看生成特征的 Python Pipeline"导出的脚本必须能脱离本项目复现界面数字，所以脚本里自带
  `_roll / _expanding_mean / _ewm_prev / _target_mean` 四个同口径辅助函数，而不是套用 pandas 的
  `rolling().mean()`、`groupby().transform('mean')`、内置 `round()`。两条踩过的坑记在这里：
  Python 内置 `round` 是银行家舍入（用 `Decimal` + `ROUND_HALF_UP`），**Python 3.12 起的 `sum()`
  对 float 走 Neumaier 补偿求和**，与后端 `seq_sum` 的朴素顺序累加不等价，落在 `.005` 边界上会差一个百分位
- 前端的操作记录（审计链）是**整场会话**的，一次会话里可以连着开好几份数据；但"本步已完成"的徽标、
  导出的流程 JSON 和上面这份 Python 脚本都只该描述**当前这份工作区**。所以每条记录都带 `wsId`，
  这些出口统一走 `store.js` 的 `wsActionLog()` 取子集——否则上一份数据的构建会给新打开的那份打勾，
  导出的脚本也会把两份数据的操作混进同一段（`replay` 回放进新工作区时会把 `wsId` 改写成当前值）

## 接口

- `GET /api/health` → `{ status, version, capabilities[], limits{workspaces,cellsPerWorkspace,maxPageRows,maxUploadBytes,featureColsPerOp,onehotLevels,featureWindow,uniqueValuesReported,holidayDays,holidayDates2024,seriesMaxPoints,histogramBins,sessionActionLog,sessionInlineArray,sessionBytes,sessionWorkspaces,workspaceLogBytes,exoColsPerOp,exoSideRows}, datasetDir, stateDir }`
- `GET /api/datasets` → `{ dir, count, items[{filename,name,ext,format,size,sizeText,modifiedAt,mtime}] }`，按修改时间倒序

工作区：

- `POST /api/ws`（multipart `file`，`?persist=true` 同时落盘）→ `{ meta, page, persisted }`
- `POST /api/ws/preset` `{ key: "pv"|"load", seed? }` → 种子化内置示例，同 seed 必然同一张表
- `POST /api/ws/dataset` `{ filename }` → 打开数据集目录里已有的文件
- `GET /api/ws` → 活跃工作区列表；`DELETE /api/ws/{id}` → 关闭
- `GET /api/ws/{id}` → 元数据；`GET /api/ws/{id}/rows?offset&limit` → 行窗口（`array-of-arrays` + `columns`）
- `GET /api/ws/{id}/columns?keys=a,b&max_rows` → 整列取数。第⑤期之后前端已经不用它了（外生变量改由服务端
  生成，见下一节），留着它只为一件事：验收脚本要拿整列与独立实现逐位对拍，那才是"界面上每个数字都能追到
  一次真实计算"的证据
- `GET /api/ws/{id}/overview` → 缺失率 / 重复率 / 未解析时间 / 时间范围 / 内存占用
- `GET /api/ws/{id}/resample-preview?targetMinutes` → 真实分桶计数得到的投影行数
- `POST /api/ws/{id}/op/{time-format,rename-column,delete-column,convert-unit,derived-column,resample}`
  → `{ ...命令结果, meta, page }`
- `POST /api/ws/{id}/restore {version}` → 把游标挪到某版本：往回是撤销、往前是重做，日志不截断

曾经有的 `POST /api/ws/{id}/replace`（前端改完整表再回传）**已删除**，`/api/health` 的 `capabilities`
里也没有 `replace` 这一项；`verify_phase4.py` 会断言这条通道返回 404，防止它被悄悄加回来。

### 第三步（统计图表与导出）的端点

界面上的每一个统计数字都是下面这些调用的一次返回，浏览器不再本地数表：

- `GET /api/ws/{id}/stats?cols=a,b` → 统计矩阵（Count/Mean/Std/Min/Q1/Median/Q3/Max/缺失率 + `globalMax`）。
  `cols` 留空则统计全部数值列。四分位取 `sorted[int(n·q)]`、标准差是**总体**（÷n）、缺失率分母是**总行数**，
  与迁移前的浏览器实现逐格一致
- `GET /api/ws/{id}/hist?col&bins` → 单列频次直方图，`bins` 缺省且上限都是 **25**（`limits.histogramBins`），
  返回桶边界、计数与均值/中位数所在桶
- `GET /api/ws/{id}/series-multi?cols=a,b&mode=raw|extremes|mean&points` → 多列叠加曲线，共享一条时间轴。三种抽稀：
  `raw`（等间隔跨步，首尾必留）、`extremes`（每列取极值包络点位，预算 `max(200, cap//列数)` 共享，
  因此**不会抽掉任何一列的最大/最小值**）、`mean`（窗口均值，窗口 4 行，值保留 2 位小数）。
  `points` 上限由 `limits.seriesMaxPoints = 6000` 声明，超出会被 clamp 而不是报错
- `GET /api/ws/{id}/first-complete?cols&scan_rows` → 新增特征列行行有值的首个行号（长窗口特征开头必然为空，
  前端"首个完整行"按钮只拿一个行号，不再拉 5000 行回来自己扫）
- `GET /api/ws/{id}/export?format=csv|xlsx|parquet|feather` → 宽表直出。时间列按 `timeFormat` 渲染后写出，
  CSV 带 `utf-8-sig` BOM（Excel 双击不乱码），xlsx 走 openpyxl 的 `data` 工作表，
  parquet/feather 由 pyarrow 真实编码。`Content-Length` 与 `Content-Disposition` 显式列进
  `Access-Control-Expose-Headers`，界面才能把**真实字节数**记进操作记录

统计与曲线类端点**只吃数值列**：类别列或时间列混进来直接 400（`该接口只接受数值列：xxx 是 category 列`）。
理由是这两种输入都能"数出一个看起来像数的东西"——类别列数出满屏 `n=0`，时间列数出一串 epoch 大数，
把前端选错列的 bug 洗成结果。让它在第一次调用就炸，比在图上摆一个假数字好。

### 第四步（清洗与异常）的端点

浏览器这一侧只剩界面与当前页窗口，第五步之前的所有诊断数字都出自下面这些调用：

- `GET /api/ws/{id}/quality` → 各列缺失统计、缺失段区间（`startIdx/endIdx`，上限 300 段并如实标 `truncated`）、
  重复时间戳计数、每列建议算法；界面"共 1404 个缺失 / 1394 段"就是这里的原始计数
- `GET /api/ws/{id}/series?col&max_points` → 画一条曲线所需的抽稀点 + **原始行号** + 异常覆盖层；
  有了行号，鼠标点上去仍能对回真实行，不必把整表拉回浏览器
- `GET /api/ws/{id}/anomaly` → 服务端留存的检测结果（没有则 `null`），`meta.anomaly.stale` 表示数据已改动、需重测
- `POST /api/ws/{id}/anomaly-detect { algo: "3sigma"|"iqr"|"iforest"|"iforest_sklearn"|"expr", expr?, nEstimators?, contamination?, randomState?, normalLowerQ?, normalUpperQ? }`
  → `{ detection, meta }`；结果只存在服务端，后续修复按这份索引走，前端不回传点
- `POST /api/ws/{id}/op/impute { targets[{key,startIdx,endIdx,algo}] | all, keys?, defaultAlgo?, algos?, dedupe?: "mean"|"first"|"last" }`
  → 逐段填补；`all=true` 让服务端自己扫全表（缺失段超上限的大表只能走这条），`dedupe` 同时按解析后的时间戳合并重复行
- `POST /api/ws/{id}/op/anomaly-repair { repair: "clip"|"nan_impute"|"mask_only" }`
  → 用服务端留存的检测改帧；检测之后数据又被动过就直接 400 拒绝，不拿旧索引乱动行
- `POST /api/ws/{id}/op/mask-generate { maskName, startIdx, endIdx }` → 按框选行区间新增一列 0/1 掩码
- `POST /api/ws/{id}/op/mask-delete { keys[] }` → 删除掩码列

孤立森林不再单独开接口：它是 `anomaly-detect` 的一种 `algo`。`iforest` 是浏览器旧实现的等价复刻
（numpy 回看窗口 MAD 近似版，窗口 33 / min_periods 5 / k=4.0），`iforest_sklearn` 才走
`scikit-learn.IsolationForest`（逐列单变量拟合，`nEstimators/contamination/randomState` 只对这一支生效）。

### 第五步（特征构建）的端点

四个 Tab 全部在服务端的整帧上算，浏览器只拿到 `meta`（列注册表）和当前页窗口——第五步不再需要
"把整表物化到浏览器"那张过渡通道：

- `GET /api/ws/{id}/value-counts?keys=a,b&method=onehot|ordinal|target` → 类别列的取值分布
  （按首次出现排序、上限 `uniqueValuesReported=200` 个并如实标 `truncated`），界面勾选时的分布条即来自这里
- `POST /api/ws/{id}/op/feature-time { dims[], cyclical }` → 日历维度 + 正余弦；`holiday` 用的就是
  `features.HOLIDAYS_2024` 这一份表（`/api/health` 的 `limits.holidayDays` / `limits.holidayDates2024`
  把它同时告诉前端，界面上的"共 N 天"和导出的脚本都只能引用它，前端不留副本）
- `POST /api/ws/{id}/op/feature-lag { cols[], lags[], windows[], stats[], expanding, ewm, span }`
  → 滞后 + 滚动/扩张/EWM；一次最多 `featureColsPerOp=400` 列
- `POST /api/ws/{id}/op/feature-diff { cols[], d1, d2, seasonal, period, dominant, entropy, powerRatio }`
  → 差分与频域；FFT 只对 `cols[0]` 执行（界面同一处已标注），逐行窗口 64 点
- `POST /api/ws/{id}/op/feature-cat { cols[], method }` → 独热（单列取值上限 `onehotLevels=200`）/
  序数 / 目标均值编码；响应里的 `created` 是本次真正新增的列数，重复构建同一组列不会重复加列
- 矩阵上的两个改表动作复用 `op/rename-column`、`op/delete-column`（带 `feature` 标记，撤销后仍能重建）

其他：

- `POST /api/export`（JSON `{ format, columns[], rows[], filename }`）→ 附件字节流

### 服务端历史与会话（第⑤期）

明细留在工作区，**"怎么算出来的"留在磁盘**：`<cwd>/.tss-state`（`TSS_STATE_DIR` 可覆盖）下的
`workspaces/<wsId>.json` 存来源 + 命令日志 + 游标，`session.json` 存会话。

- `GET /api/ws` → `{ count, activeCount, stateDir, items[] }`：内存里的与只在磁盘上的合并返回，条目用
  `loaded` 区分，界面因此能在后端重启后 still 列出"上次那些工作区"
- `GET /api/ws/{id}`（以及任何一条 `/op/*`）都是"要么在内存、要么从日志重放出来"：`get()` 会透明地
  `reopen()`，同一 wsId、同一版本号、撤销/重做两边历史都在。`capabilities` 里的 `workspace:reopen` 就是这条
- 失败的重建不留半成品：日志里的来源文件被删了、侧表内容被改过，`reopen()` 会把这颗半成品从注册表摘掉
  再抛 404，绝不让下一次读到一个缺列、版本号也不对的帧
- `DELETE /api/ws/{id}` → 关闭 = 内存与日志一起删；被淘汰（只留最近 `workspaces=8` 颗帧）≠ 关闭，日志留着
- `GET /api/session` → 服务端**逐个重建/核对**会话引用的工作区后返回 `{ exists, savedAt, meta, snap,
  workspaces[], stateDir }`；`PUT /api/session` 保存；`DELETE /api/session` 清除
- 会话的三条守卫（数字同时出现在 `/api/health` 的 `limits` 里）：条数 `sessionActionLog=2000`、
  总字节 `sessionBytes=2MB`、以及任何长度 > `sessionInlineArray=200` 的数组一律 400——第③期之前那种
  把整列塞进 localStorage 的做法，现在在服务端就被明说拒绝
- 版本号以服务端为准：会话里记的 `version` 与服务端重开后的版本不一致时，`/api/session` 回
  `versionDrift { sessionSays, serverSays }`，界面据此提示"历史已回到第 N 版"

已知限制：上传时 `persist=false` 的那条来源**不落盘**，因此后端重启后它无法重开（`/api/ws/{id}` 会 404 并
说明来源文件不在数据集目录里）。前端第⑤期起把上传一律按 `persist=true` 落盘，就是为了避开这条路。

### 外生变量（第⑤期）：三条来源都在服务端生成

浏览器不再持有任何外生变量的整列数据，也不再有"前端算一列再回传"的通道
（`POST /api/ws/{id}/op/add-columns` 已删除，`/api/health` 的 `capabilities` 里同样没有它，验证脚本会断言
这条通道返回 404）：

- `GET /api/exo/presets` → `{ items[8 个预设模板 + needs], formulaHelp, funcs, vars, alignModes }`。界面下拉
  选项只渲染这一份表，前端不再抄第二份
- `POST /api/ws/{id}/op/exo-preset { presetKey, key?, label?, seed? }` → 服务端按主表时间列生成一列模拟值；
  没给 seed 就现场挑一个并**改写进命令日志**，所以撤销→重做、重启→重开拿到的都是同一列（实测逐位一致）
- `POST /api/ws/{id}/op/exo-formula { key, expr, label? }` → `hour`（含分钟小数）/`day`/`month`/
  `weekday`（周日为 0）/`idx` 五个变量在服务端向量化求值；表达式走 AST 白名单，只允许算术、比较、
  `and/or/not` 与固定函数表，`open('/etc/passwd')` 这类一律 400
- `POST /api/exo/inspect`（multipart）→ 侧表**先看后挂**：文件按内容指纹落进 `dataset/_exo/`
  （`<stem>__<sha12><ext>`，同名不同内容各留一份），返回 `{ filename, sha, rows, sideTimeCol,
  columns[{from,key,label,numeric,time,needsName,reason}] }`。这条不产生任何命令日志
- `POST /api/ws/{id}/op/exo-file { filename, sha, sideTimeCol, mode, toleranceMinutes?, targets? }` →
  按时间戳对齐挂列。几十 MB 的侧表只上传一次；日志记文件名 + sha + 规格，重放时重新读那份文件，
  内容被覆盖过就明确报错（`重放到版本 N 失败…内容已变化（命令里记录 sha …）`），不会静默换一列数据
- 对齐只有两种真实语义：`left` 精确匹配（未命中留空）、`nearest` 就近匹配（可选 `toleranceMinutes`）。
  界面上曾有第三项"内连接"，而浏览器旧实现里它与 `left` 一模一样（外生变量是往主表挂列，主表行数不由
  侧表决定），因此不提供这个假选项
- 表头 → 变量名的规则：**纯 ASCII** 且转写后不重名的列可以自动推导；含中文等非标点字符的表头（如
  `气温(°C)`）必须由用户显式命名后再提交，否则 400 并列出这些表头。旧前端会把两个中文表头双双转写成
  `___` 互相覆盖，界面上却是两个不同变量——这种静默撞车在这里不被允许
- 挂进来的列在 `meta.columns[].exo` 上带来源标记（`{kind: preset|formula|file, ...}`），界面据此显示
  "外生变量"角标；删除走通用的 `op/delete-column`，不再有单独的清空通道
- 上限：`exoColsPerOp=100` 列、`exoSideRows=200000` 行（超了报"请先聚合再导入"），都在 `/api/health` 的
  `limits` 里

## 验证

```bash
# 直接调服务层（不起 HTTP）：载入 fixtures 里的样例并打印对拍数字
.venv/Scripts/python.exe scripts/smoke_workspace.py
# 生成大表样本 dataset/big40.csv（11000 × 40，注入 1404 个缺失）
.venv/Scripts/python.exe scripts/make_big_csv.py
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000   # 另开一个终端，注意没有 --reload

# 期①验收：与 pd.read_csv 对拍数字 + 证明整表从未出现在响应里
.venv/Scripts/python.exe scripts/verify_http.py
# 期②验收：140 项，覆盖缺失段/填补/去重/五种检测/三种修复/掩码增删，以及游标式撤销与重做
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_phase2.py
# 期③验收：四个特征 Tab 的列集合/标签/取值顺序/上限拒绝/重复构建幂等 + 大表 big40 全程不吃整表
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_phase3.py
# 期④验收：169 项。统计矩阵/直方图/三种抽稀曲线与「浏览器旧算法的 JS 重实现」逐格跨语言对拍，
# 再加自洽不变量、与其他通道的交叉验证、首个完整行、四格式导出与二次载入、非数值列被 400 打回
node --version >/dev/null 2>&1 || echo "期④需要 node 跑参考实现 scripts/ref_phase4.mjs"
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_phase4.py
# 只跑某一节（parity|invariants|cross|first-complete|export|errors）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_phase4.py --only=parity
# 期⑤验收：50 项。自己起一个临时状态目录的后端，逐版回退比整表 CSV 指纹、
# 审计链与服务端 ops[:游标] 跨语言三方对拍、会话存取与三条守卫、杀掉进程换端口重开再比指纹
node --version >/dev/null 2>&1 || echo "期⑤需要 node 跑参考实现 scripts/ref_phase5.mjs"
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_phase5.py
```

`verify_phase2.py` 里刻意包含"服务端数字 vs 独立 numpy/pandas 重算"的对拍（大表 3σ 点数、缺失格数），
以及一条**跨语言自证**：前端"导出 Python"产出的脚本独立跑 `big40.csv`，结果与该工作区的服务端当前帧
逐格比较（11000 × 79，最大偏差 0、39 个掩码列全等）。界面、后端、脚本三条路必须给出同一份数据。

期③ 的同一条自证跑在**带缺失值**的 `na3.csv` 上（这才是窗口口径真正被考验的地方）：浏览器里走完
四类特征 + 改名 + 撤销，导出脚本单独跑 → 与界面当前表 240 × 81 逐列比较，`SAME_ORDER: True`、
`MISMATCHED_COLS: NONE`。修好它之前是 34 列不一致：33 列窗口特征（`rolling` 的 NaN 传播，以及
`expanding`/`ewm` 与后端不同的缺失处理）+ 1 列目标编码（`groupby.transform('mean')` 的求和顺序让
`grade` 的组均值在 `.005` 上翻了一个百分位，6.53 ↔ 6.54）。补上同口径辅助函数后归零；改回 pandas
现成算子会立刻重现这 34 列，这条比较就是防回归的哨兵。

期④ 的跨语言自证换了一种形式：`scripts/ref_phase4.mjs` 是把被删掉的浏览器算法**原样用原生 JS 重写一遍**
（自己 `readFileSync` 解析 CSV，不用 pandas、不 import 服务端任何代码、也不走 HTTP），
`scripts/verify_phase4.py` 再把它 printed 的 JSON 与真实接口的返回逐格比较 —— 覆盖 4 份数据
（密布缺失的 `na3`、清洗后的 PV、中文带单位列名的 `machine`、11000×40 的 `big40`）× 2 个点数上限，
统计矩阵、25 桶直方图与 raw/extremes/mean 三条抽稀曲线全部对拍。
比较规则与第五步同源：**浮点用相对容差 `1e-9`，计数/行号/桶边界精确相等** ——
numpy 的成对求和与 JS 的顺序累加在 float 上必然有最后一位差别（实测最大相对偏差 3.7e-15），
而"哪个点是极值""某一桶有几个值"不允许差一个。三条曲线的坐标值实测**完全相等**（偏差 0.0）。

导出这一侧看的是字节而不是数字：`Content-Length` 必须等于真实收到的字节数，CSV 前 5 行必须与
`GET /rows` 的同一页一致，xlsx 抽样 7 行逐格等于接口返回的列值，parquet/feather 再上传回后端后
形状/时间列/整张统计矩阵不变（big40 往返 351 格、偏差 0）。大表 11000×40 的实测一次：
CSV 3106 KiB / 564 ms，xlsx 2785 KiB / **9.3 s**，parquet 2476 KiB / 176 ms，feather 2039 KiB / 132 ms。
xlsx 慢在 openpyxl 逐格写 XML，不是网络 —— 明细从头到尾没出过后端。

期⑤ 的跨语言对拍比的是**同一份服务端事实的两种解读**：`scripts/ref_phase5.mjs` 把界面上那三条审计链规则
（条目上的 `v` 是版本号、无 `v` 恒可见、`v > 游标` 即"已撤销等待重做"）用原生 JS 独立实现一遍，
`verify_phase5.py` 里再用 Python 独立实现一遍，两边与 `ops[:cursor]` 三方比 —— 谁都不是自己的参照物。
比 B 节更硬的是**逐版回退比整表指纹**：六条命令（含预设外生变量、60min 重采样、侧表就近挂列）各推进一版，
每一版把全表导成 CSV 取 sha256，然后 v6→v0 逐版回退重算，7 个指纹必须一个不差；E 节再杀掉进程、
换一个端口用同一份状态目录重启，重启后的指纹必须与重启前逐字节同源。

浏览器端实测（`http://localhost:5321`，工作区 `5c4ed7dced43`）：预设外生变量 `humidity · 2880 行 · seed=357876160`；
公式 `sin(hour/24*2*PI) * 100` 在 06:00 那行给出 `100`、00:15 那行 `6.5403`；侧表 `browser_side__6f1e9cf45d73.csv`
（49 行，时间戳整体偏移 4 分钟）按就近匹配上 `匹配 49/2880 行`。撤销一步：9 列回到 8 列、审计链从 4 条收成 3 条、
重做按钮写着"侧表对齐合并"；刷新页面后横幅报"服务端记着上次会话…此刻在第 2 版"，点恢复回到第②步、游标仍是 2；
**杀掉后端进程再重启**（`GET /api/ws` 显示 13 份工作区、`activeCount: 0`），恢复会话仍拿到 v3 · 9 列，
`rain_mm` 由重放重新读盘挂上。离线那一刻点撤销不会假装成功，而是明说
"撤销需后端在线执行（数据加工全部在后端，浏览器不再算第二套）"。

## 目录

```
app/
  main.py                 FastAPI 实例、CORS、health / datasets / export
  routers/workspace.py    工作区端点：命令式加工 + 分页窗口 + 版本重放
  routers/session.py      GET/PUT/DELETE /api/session：逐个重建/核对会话引用的工作区 + 三条守卫
  schemas.py              Pydantic 请求模型与取值校验
  services/workspace.py   DataFrame 注册表、ops 重放（含 replay 私有载荷）、时间格式、重采样
  services/state_store.py 状态目录：命令日志落盘与重放重建（`workspaces/<wsId>.json`）+ 会话 JSON
  services/quality.py     缺失段扫描与填补、重复时间戳合并、五种异常检测、三种修复、掩码
  services/features.py    第五步四类特征：日历/滞后滚动/差分频域(逐行 DFT)/类别编码 + 2024 节假日表
  services/exo.py         外生变量三条来源：8 个预设模板生成器、表达式 AST 白名单求值、侧表按时间戳对齐
  services/explore.py     第三步：整表统计矩阵、25 桶直方图、三种抽稀叠加曲线、首个完整行
  services/exporter.py    DataFrame / 行数据 → 四种格式字节流
  services/dataset_store.py 数据集目录：建目录/列举/读取/保存 + 文件名防护（侧表落 `dataset/_exo/`）
scripts/                  smoke_workspace / make_big_csv / frontend_flow_node.mjs
                          verify_http(期①) / verify_phase2(期②) / verify_phase3(期③) / verify_phase4(期④)
                          / verify_phase5(期⑤)
                          + ref_phase4.mjs（期④的浏览器旧算法 JS 参考实现，供跨语言对拍）
                          + ref_phase5.mjs（期⑤的审计链游标投影 JS 参考实现，供三方对拍）
dataset/                  数据集目录（首次运行自动创建，已 gitignore）
fixtures/                 手工验证用样例数据（parquet/feather/csv 各一份，含注入尖峰与缺失）
```
