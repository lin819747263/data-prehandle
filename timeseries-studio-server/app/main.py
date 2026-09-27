"""TimeSeries Studio 后端：数据加工全部在这里真实发生。

工作台不再把整张表放进浏览器：POST /api/ws 之后明细留在服务端的 DataFrame 里，
前端只持有元数据与"当前页附近"的行窗口，每个加工动作是一条打在服务端的命令。
唯一的落盘位置是数据集目录 <cwd>/dataset（可用 TSS_DATASET_DIR 覆盖），不存在时自动创建。

  GET    /api/health                 探活 + 能力声明，前端据此决定"在线可编辑 / 离线只读"
  GET    /api/datasets               列举数据集目录中的真实文件（"最近打开的数据集"）

  POST   /api/ws                     上传文件建工作区（persist=true 时同时落盘到数据集目录）
  POST   /api/ws/preset              用种子化 numpy 生成内置示例数据集
  POST   /api/ws/dataset             打开数据集目录里的已有文件
  GET    /api/ws/{id}                元数据（列、行数、采样频率、时间格式、版本号、命令序列）
  GET    /api/ws/{id}/rows           分页行窗口
  GET    /api/ws/{id}/columns        整列取数（外生变量按时间戳对齐这类必须要整列的操作用）
  GET    /api/ws/{id}/overview       缺失率 / 重复率 / 时间范围等整表统计
  GET    /api/ws/{id}/quality        第四步诊断：每列缺失统计 + 缺失段区间 + 重复时间戳
  GET    /api/ws/{id}/series         第四步质量曲线：多列共享抽稀时间轴（含原始行号、逐列缺失标记与异常覆盖层）
  GET    /api/ws/{id}/stats          第三步统计矩阵：Count/Mean/Std/四分位/Min/Max/缺失率
  GET    /api/ws/{id}/hist           第三步直方图：25 桶计数 + 均值/中位数所在桶
  GET    /api/ws/{id}/series-multi   第三步叠加曲线：多列共享时间轴，服务端按点数上限抽稀（span 取年/月/周/日窗口）
  GET    /api/ws/{id}/first-complete 新特征列的首个完整行号（长窗口特征开头必为空）
  GET    /api/ws/{id}/anomaly        服务端留存的最近一次检测结果（索引不外泄）
  POST   /api/ws/{id}/anomaly-detect 3σ / IQR / 滑窗 MAD / sklearn 孤立森林 / 表达式
  POST   /api/ws/{id}/op/...         加工命令：时间格式、重命名、删列、单位换算、
                                     列运算、重采样、缺失段填补、重复时间戳合并、
                                     异常修复、掩码生成/删除、四类特征编码
  POST   /api/ws/{id}/op/split       数据集切分落成真实的一列（train/val/test，时序不打乱）
  GET    /api/ws/{id}/holidays       当前工作区生效的节假日表与内置预设
  POST   /api/ws/{id}/op/holidays    配置节假日表（改配置不改帧，同样可撤销、可重放）
  POST   /api/ws/{id}/op/exo-preset  预设模板生成外生变量列（服务端 seeded numpy）
  POST   /api/ws/{id}/op/exo-formula 时间公式生成外生变量列
  POST   /api/ws/{id}/op/exo-file    侧表按时间戳对齐挂列（只引用文件名 + sha，不重传文件）
  GET    /api/exo/presets            预设清单与公式变量表（界面下拉选项读它，不再自己抄）
  POST   /api/exo/inspect            侧表先看后挂：落盘并回表头 / 可用变量名 / 时间列候选
  POST   /api/ws/{id}/restore        回到某个版本号（撤销的服务端实现，从最近帧缓存就近重放）
  GET    /api/ws/{id}/export         宽表直出 csv/xlsx/parquet/feather（明细不过网络）
  POST   /api/ws/{id}/save-as        把当前帧写进数据集目录，下次从第一步「最近打开」点开
  DELETE /api/ws/{id}                关闭工作区（同时删掉它的命令日志）

  GET    /api/session                读回上次会话：服务端逐个重建/核对其引用的工作区再返回
  PUT    /api/session                保存会话（只收 UI 状态，携带整列数据的请求直接 400）
  DELETE /api/session                清除会话

  POST   /api/export                 行数据 → Parquet/Feather/CSV/Excel 字节流（自带行的调用方用）

工作区的命令日志与会话落在 <cwd>/.tss-state（可用 TSS_STATE_DIR 覆盖）。落的是「怎么算出来的」，
不是明细本身：后端重启后同一个 wsId 仍能按日志重放复原，刷新页面也就不再丢撤销历史。
"""
from __future__ import annotations

import urllib.parse

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from . import __version__, schemas
from .routers import session as session_router
from .routers import workspace as workspace_router
from .schemas import ExportRequest
from .services import dataset_store, exo, explore, features, state_store
from .services.exporter import build_export, codec_status

app = FastAPI(
    title="TimeSeries Studio Server",
    description="时序数据清洗工作台后端（FastAPI · 服务端工作区 · 无数据库）",
    version=__version__,
)

app.add_middleware(
    CORSMiddleware,
    # 前端 dev server 端口由 vite 动态分配，放行本机任意端口
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(workspace_router.router)
app.include_router(session_router.router)


@app.get("/api/health")
def health() -> dict:
    # 导出格式逐个真编一次再声明：装了 pandas 不代表装了 pyarrow / openpyxl，
    # 界面「后端可用」徽章读这份能力清单，不能凭分支写了就报可用。
    codecs = codec_status()
    return {
        "status": "ok",
        "version": __version__,
        "capabilities": [
            "workspace:create", "workspace:preset", "workspace:open", "workspace:rows",
            "workspace:columns", "workspace:overview", "workspace:restore",
            "workspace:quality", "workspace:series", "workspace:anomaly",
            "workspace:stats", "workspace:hist", "workspace:series-multi",
            "workspace:first-complete", "workspace:export", "workspace:save-as",
            "op:time-format", "op:rename-column", "op:delete-column", "op:convert-unit",
            "op:derived-column", "op:resample", "op:split",
            "op:impute", "op:anomaly-repair", "op:mask-generate", "op:mask-delete",
            "anomaly:detect",
            "workspace:value-counts", "workspace:holidays", "op:holidays",
            "op:feature-time", "op:feature-lag", "op:feature-diff", "op:feature-cat",
            "feature-group:lag-window", "feature-group:diff-fft", "feature-sincos-replace",
            "series-window:year-month-week-day", "series-period-paging",
            # 期⑤：外生变量三条来源全部在服务端生成，浏览器不再回传整列
            "op:exo-preset", "op:exo-formula", "op:exo-file", "exo:presets", "exo:inspect",
            # 撤销不再整段重放：服务端留住最近几版帧，回退一步就是换个指针
            "undo:frame-cache",
            # 只有探测真编得出来的格式才进能力清单
            *[f"export:{fmt}" for fmt, why in codecs.items() if why is None],
            "datasets:list",
            # 期⑤：历史与会话搬到服务端。reopen 是「进程里没有、按日志重放出来」，
            # 前端据此判断刷新/重启之后还能不能接着撤销
            "workspace:reopen", "workspace:list-durable",
            "session:get", "session:set", "session:clear",
        ],
        # 每种格式探测时真实编码一次的结果：None = 编得出来，否则是失败原因。
        # 界面据此把「后端可用」换成「缺依赖 · 原因」，而不是把用不了的按钮照原样点亮。
        "exportCodecs": codecs,
        "limits": {
            "workspaces": workspace_router.ws_store.MAX_WORKSPACES,
            "cellsPerWorkspace": workspace_router.ws_store.MAX_CELLS,
            "maxPageRows": workspace_router.MAX_PAGE,
            "maxUploadBytes": schemas.MAX_UPLOAD_BYTES,
            # 撤销重放的帧缓存上限：超过字节上限的大帧不留档，退回从载入帧整段重放
            "snapshotVersions": workspace_router.ws_store.SNAP_MAX_VERSIONS,
            "snapshotMaxBytes": workspace_router.ws_store.SNAP_MAX_BYTES,
            "featureColsPerOp": workspace_router.ws_store.MAX_FEATURE_COLS_PER_OP,
            "onehotLevels": workspace_router.ws_store.MAX_ONEHOT_LEVELS,
            "featureWindow": features.MAX_WINDOW,
            "uniqueValuesReported": features.MAX_UNIQUE_VALUES,
            # 第三步曲线一次最多回这么多点：叠加曲线的抽稀上限，界面按它摆「跨度」按钮
            "seriesMaxPoints": explore.MAX_POINTS,
            "seriesSpans": list(explore.SPANS),
            "histogramBins": explore.DEFAULT_BINS,
            # 默认节假日表（内置预设）的天数与清单：每个工作区可以在它之上改出自己的那份，
            # 改完之后界面、feature-time 与导出脚本读的都是工作区那一份（GET /api/ws/{id}/holidays）
            "holidayDays": len(features.HOLIDAY_PRESETS[features.DEFAULT_HOLIDAY_YEAR]),
            "holidayDates2024": sorted(features.HOLIDAYS_2024),
            "holidayPresetYears": sorted(features.HOLIDAY_PRESETS),
            "maxHolidayDays": features.MAX_HOLIDAY_DAYS,
            # 会话的三条守卫：条数、整列内联长度、总字节
            "sessionActionLog": schemas.MAX_SESSION_ACTION_LOG,
            "sessionInlineArray": schemas.MAX_SESSION_INLINE_ARRAY,
            "sessionBytes": schemas.MAX_SESSION_BYTES,
            "sessionWorkspaces": schemas.MAX_SESSION_WORKSPACES,
            "workspaceLogBytes": state_store.MAX_LOG_BYTES,
            # 外生变量侧表的两条上限
            "exoColsPerOp": exo.MAX_EXO_COLS,
            "exoSideRows": exo.MAX_SIDE_ROWS,
        },
        "datasetDir": dataset_store.dataset_dir(),
        # 命令日志与会话的位置：界面「状态目录」一栏显示它，用户才找得到历史存在哪
        "stateDir": str(state_store.state_dir()),
    }


@app.get("/api/datasets")
def datasets_list() -> dict:
    """列举数据集目录中的真实文件；目录不存在时由 dataset_dir() 自动创建。"""
    try:
        return dataset_store.list_datasets()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取数据集目录失败：{type(exc).__name__}: {exc}") from exc


@app.get("/api/exo/presets")
def exo_presets() -> dict:
    """预设外生变量清单：下拉选项由这一份表渲染，前端不再自己抄一遍。"""
    return {"items": exo.presets_view(),
            "formulaHelp": exo.FORMULA_HELP,
            "funcs": sorted(exo._FORMULA_FUNCS),
            "vars": list(exo._FORMULA_NAMES),
            "alignModes": [{"mode": "left", "label": "精确时间戳"},
                           {"mode": "nearest", "label": "就近匹配"}]}


@app.post("/api/exo/inspect")
async def exo_inspect(file: UploadFile = File(..., description="外生变量侧表")) -> dict:
    """侧表先看后挂：落盘（文件名带内容指纹）并回表头、可用变量名、时间列候选。

    分成两步是为了让「列名转不出合法变量名」这种情况在挂列之前就能改名，而不是像旧版
    那样静默转写成 ___ 互相覆盖；同时文件已经在这一步落进 dataset/_exo/，提交时只传
    文件名 + sha，几十 MB 的侧表不用上传第二遍。只看不挂：这条不产生任何命令日志。
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(content) > schemas.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"侧表文件超过 {schemas.MAX_UPLOAD_MB}MB 上限")
    try:
        stored = dataset_store.save_exo_bytes(file.filename or "exo.csv", content)
        return exo.inspect_side_table(content, stored)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"侧表解析失败：{type(exc).__name__}: {exc}") from exc


@app.post("/api/export")
def export(payload: ExportRequest) -> Response:
    if not payload.columns:
        raise HTTPException(status_code=400, detail="columns 不能为空")
    try:
        data, media_type, download_name = build_export(
            payload.format, payload.columns, payload.rows, payload.filename
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"导出失败：{type(exc).__name__}: {exc}") from exc
    quoted = urllib.parse.quote(download_name)
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
            "Content-Length": str(len(data)),
        },
    )
