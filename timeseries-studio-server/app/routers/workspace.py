"""工作区路由：明细数据留在服务端，浏览器只拿元数据与当前页窗口。

约定：
- 所有加工都是"命令"（POST /api/ws/{id}/op/...），响应统一回带最新 meta 与页窗口，
  前端不需要再自己遍历整张表去拼数字；
- 命令失败一律 4xx 并给出中文原因，绝不静默改数据；
- /api/ws/{id}/restore 是撤销的唯一入口：回到某个版本号 = 从载入帧重放；
- 命令日志落盘（state_store），所以「刷新还在、后端重启也还在」：进程里没有的工作区
  会由 get() 从日志重放重建，界面拿到的是同一个 wsId；
- 异常检测不是加工命令（它不改数据），结果缓存在工作区里并按 value_epoch 判新鲜度，
  所以 /anomaly-detect 之后可以直接 /op/anomaly-repair，界面不必回传上千个行索引。
"""
from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile

from ..schemas import (
    MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, AnomalyDetectRequest, AnomalyRepairRequest, ConvertUnitRequest,
    DatasetWorkspaceRequest, DerivedColumnRequest, ExoFileRequest, ExoFormulaRequest,
    ExoPresetRequest,
    FeatureCatRequest, FeatureDiffRequest,
    FeatureLagRequest, FeatureTimeRequest,
    HolidaysRequest, ImputeRequest, MaskDeleteRequest, MaskGenerateRequest,
    PresetRequest, RenameColumnRequest, ResampleRequest,
    RestoreRequest, SaveAsRequest, SetTimeColRequest, SplitApplyRequest, TimeFormatRequest, DeleteColumnRequest,
)
from ..services import dataset_store, exporter, explore, features
from ..services import workspace as ws_store
from ..services.workspace import DEFAULT_PAGE, MAX_PAGE

router = APIRouter(prefix="/api/ws", tags=["workspace"])


def _get(ws_id: str) -> ws_store.Workspace:
    try:
        return ws_store.get(ws_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"工作区 {ws_id} 既不在内存里、也没有命令日志（后端只保留最近 "
                   f"{ws_store.MAX_WORKSPACES} 颗帧的历史，被显式关闭或换过状态目录就找不回来了），"
                   f"请回第一步重新载入数据",
        ) from exc
    except ValueError as exc:
        # 日志在、进程里没有，按日志重建时才发现来源文件被删了：
        # 这类工作区界面必须提示回第一步重新载入，不能报成 500
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _guard(fn, *args):
    try:
        return fn(*args)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"{type(exc).__name__}: {exc}") from exc


def _with_page(ws: ws_store.Workspace, result: dict, offset: int, limit: int) -> dict:
    return {**result, "page": ws.rows(offset, limit)}


@router.get("")
def list_workspaces() -> dict:
    return ws_store.list_workspaces()


@router.post("")
async def create_workspace(file: UploadFile = File(..., description="CSV/Excel/Parquet/Feather"),
                           persist: bool = Query(False, description="是否同时落盘到数据集目录"),
                           offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"上传文件超过 {MAX_UPLOAD_MB}MB 上限")
    name = file.filename or "data.csv"
    saved = None
    if persist:
        try:
            saved = dataset_store.save_bytes(name, content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"写入数据集目录失败：{type(exc).__name__}: {exc}") from exc
    ws = _guard(ws_store.create_from_bytes, content, saved or name)
    meta = ws.meta_view()
    if saved:
        meta["source"] = {**meta["source"], "persisted": saved, "dir": str(dataset_store.dataset_dir())}
    return {"meta": meta, "page": ws.rows(offset, limit), "persisted": saved}


@router.post("/preset")
def create_preset(payload: PresetRequest,
                  offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _guard(ws_store.create_preset, payload.key, payload.seed)
    return {"meta": ws.meta_view(), "page": ws.rows(offset, limit)}


@router.post("/dataset")
def create_from_dataset(payload: DatasetWorkspaceRequest,
                        offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    try:
        ws, real_name = ws_store.create_from_dataset(payload.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"解析失败：{type(exc).__name__}: {exc}") from exc
    meta = ws.meta_view()
    meta["source"] = {**meta["source"], "filename": real_name}
    return {"meta": meta, "page": ws.rows(offset, limit)}


@router.post("/merge")
async def create_merged_workspace(
        files: list[UploadFile] = File(..., description="两份以上的 CSV/Excel/Parquet/Feather"),
        persist: bool = Query(True, description="每份原始文件是否落盘到数据集目录"),
        offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    """多文件按行合并 + 按时间排序：拼表在后端 pandas 里做，浏览器只拿回执和页窗口。

    `persist` 默认打开：合并帧没有"原始整表"可退回，服务端重启后只能靠数据集目录里的这
    几份文件依次重新解析再拼一遍，所以文件名必须真实落在目录里才能重建。
    """
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="合并至少需要两份文件；单份请走 POST /api/ws")
    if len(files) > ws_store.MAX_MERGE_FILES:
        raise HTTPException(status_code=400,
                            detail=f"一次最多合并 {ws_store.MAX_MERGE_FILES} 份文件，本次收到 {len(files)} 份，请分批导入")
    payloads: list[tuple[str, bytes]] = []
    total = 0
    for f in files:
        name = f.filename or "data.csv"
        content = await f.read()
        if not content:
            raise HTTPException(status_code=400, detail=f"上传文件 {name} 为空")
        total += len(content)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"合并文件累计 {total / (1024 * 1024):.1f}MB，超过 "
                                       f"{MAX_UPLOAD_MB}MB 上限（与单次导入同一口径）")
        if persist:
            # 先只做校名：第 3 份文件名非法时，不能把前 2 份已经写进目录
            try:
                dataset_store.safe_name(name)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"{name}：{exc}") from exc
        payloads.append((name, content))
    # 解析、合并、写盘都在服务里按这个顺序做：拼不成一张表的一批不会留下孤儿文件
    ws, saved = _guard(ws_store.create_from_files,
                       [(c, n) for n, c in payloads], None, persist)
    meta = ws.meta_view()
    if persist:
        meta["source"] = {**meta["source"], "dir": str(dataset_store.dataset_dir())}
    return {"meta": meta, "page": ws.rows(offset, limit), "persisted": saved, "merge": meta.get("merge")}


@router.get("/{ws_id}")
def get_meta(ws_id: str) -> dict:
    return _get(ws_id).meta_view()


@router.get("/{ws_id}/rows")
def get_rows(ws_id: str, offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    return {"meta": ws.meta_view(), "page": ws.rows(offset, limit)}


@router.get("/{ws_id}/columns")
def get_columns(ws_id: str, keys: str = Query(..., description="逗号分隔的列名"),
                max_rows: int | None = Query(None, ge=1)) -> dict:
    ws = _get(ws_id)
    wanted = [k for k in keys.split(",") if k]
    if not wanted:
        raise HTTPException(status_code=400, detail="keys 不能为空")
    return _guard(ws.column_values, wanted, max_rows)


@router.get("/{ws_id}/overview")
def get_overview(ws_id: str) -> dict:
    return _get(ws_id).overview()


@router.get("/{ws_id}/time-detect")
def get_time_detect(ws_id: str, col: str = Query(..., min_length=1, description="要试的列名"),
                    format: str = Query("", description="手填的源格式（可选）：登记名、epoch_ms/epoch_s 或占位符模板"),
                    customFormat: str = Query("", description="format=custom 时的模板")) -> dict:
    """按整列真解析一次，回答"这一列能不能当时间列"：认不出也给真实原因与解析数。

    判定不看列名像不像时间，只看有多少样本能按已知格式解析出来——所以中文文本列、
    普通读数列都会被判"不能"，而不是被洗成 NaT 或 1970 年的假时间。
    带 `format` 时按调用方指定的源格式解析（自动识别认不出时的出路），命中率照实回。
    """
    ws = _get(ws_id)
    fmt = None
    if (format or "").strip():
        try:
            fmt = ws_store.normalize_format(format, customFormat or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _guard(ws_store.time_detect, ws, None, col, fmt)


@router.get("/{ws_id}/quality")
def get_quality(ws_id: str) -> dict:
    """第四步诊断：各列缺失统计、缺失段区间、重复时间戳计数，全部在服务端算。"""
    ws = _get(ws_id)
    return _guard(ws.quality)


def _split_cols(raw: str) -> list[str]:
    return [c for c in (raw or "").split(",") if c]


def _numeric_only(ws: ws_store.Workspace, keys: list[str]) -> None:
    """曲线与统计类接口只吃数值列。

    类别列塞进来会数出一整列 n=0，时间列塞进来会数出一串 epoch 大数——两种都能把调用方的
    bug 洗成「看起来是个数」的结果，所以直接 400 打回，让前端尽早暴露选错列。
    """
    types = {c["key"]: c["type"] for c in ws.meta["columns"]}
    for key in keys:
        if types.get(key) not in (None, "float"):
            raise HTTPException(status_code=400,
                                detail=f"该接口只接受数值列：{key} 是 {types[key]} 列")


@router.get("/{ws_id}/series")
def get_series(ws_id: str, cols: str = Query(..., description="逗号分隔的数值列名"),
               points: int = Query(0, ge=0, le=explore.MAX_POINTS,
                                  description="0（默认）= 全量，整表每一行都送回折线；> 0 = 按该上限抽稀")) -> dict:
    """第四步的质量曲线：几条列共享一条时间轴，每列带原始行号、缺失标记与异常覆盖层。

    默认全量（points=0）：折线一个点都不抽，行 × 列的格子数超过
    `explore.FULL_RAW_MAX_VALUES` 时报 400 并说明怎么办，绝不偷偷抽点。
    """
    ws = _get(ws_id)
    wanted = _split_cols(cols)
    if not wanted:
        raise HTTPException(status_code=400, detail="cols 不能为空")
    _numeric_only(ws, wanted)
    labels_by_key = {c["key"]: c["label"] for c in ws.meta["columns"]}

    def compute():
        return {"wsId": ws.id, "version": ws.version, "timeCol": ws.time_col,
                **explore.series_quality(ws.df, wanted, ws.time_labels(), points,
                                         labels_by_key, ws.anomaly, ws.anomaly_stale())}

    return _guard(compute)


# ---------------- 第三步：统计概览与图表 ----------------

@router.get("/{ws_id}/stats")
def get_stats(ws_id: str, cols: str = Query("", description="逗号分隔的数值列名，留空则统计全部数值列")) -> dict:
    """统计矩阵：每列 Count/Mean/Std/Min/Q1/Median/Q3/Max/缺失率，整表在服务端数。"""
    ws = _get(ws_id)
    wanted = _split_cols(cols) or [c["key"] for c in ws.float_columns()]
    _numeric_only(ws, wanted)
    labels = {c["key"]: c["label"] for c in ws.meta["columns"]}
    return {"wsId": ws.id, "version": ws.version,
            **_guard(explore.stats_matrix, ws.df, wanted, labels)}


@router.get("/{ws_id}/hist")
def get_hist(ws_id: str, col: str = Query(..., description="列名"),
             bins: int = Query(explore.DEFAULT_BINS, ge=2, le=200)) -> dict:
    """单列频次直方图：桶边界、计数与均值/中位数所在桶，界面只管画。"""
    ws = _get(ws_id)
    _numeric_only(ws, [col])
    return {"wsId": ws.id, "version": ws.version, **_guard(explore.histogram, ws.df, col, bins)}


@router.get("/{ws_id}/series-multi")
def get_series_multi(ws_id: str, cols: str = Query(..., description="逗号分隔的列名"),
                     mode: str = Query(explore.DEFAULT_MODE,
                                       description="raw 全量（每行都回，不抽点）/ extremes 极值 / mean 窗口均值 / lttb 三角面积降采样"),
                     points: int = Query(explore.DEFAULT_POINTS, ge=20, le=explore.MAX_POINTS,
                                         description="降采样档位的点数上限；raw 全量不受它约束"),
                     span: str = Query("all", description="all/year/month/week/day：按自然周期筛行"),
                     offset: int = Query(0, ge=-100000, le=100000,
                                         description="该档位内往前/往后第几个自然周期，0=第一期，越界贴到最近一端")) -> dict:
    """多列叠加曲线：共享一条时间轴，每列一条降采样后的序列，整表不出后端。

    span 由服务端按真实时间列筛行（不是按行数估算），点数上限在窗口内重新分配，
    所以「看一天」得到的是这一天自己的点，而不是整年降采样后剩下的几颗。
    """
    ws = _get(ws_id)
    wanted = _split_cols(cols)
    if not wanted:
        raise HTTPException(status_code=400, detail="cols 不能为空")
    _numeric_only(ws, wanted)
    labels_by_key = {c["key"]: c["label"] for c in ws.meta["columns"]}

    def compute():
        data = explore.series_multi(ws.df, wanted, ws.time_labels(), mode, points, labels_by_key,
                                    ts=explore.time_series_of(ws.df, ws.time_col),
                                    span=span, offset=offset)
        return {"wsId": ws.id, "version": ws.version, "timeCol": ws.time_col, **data}

    return _guard(compute)


@router.get("/{ws_id}/holidays")
def get_holidays(ws_id: str) -> dict:
    """当前工作区生效的节假日表：界面显示、导出复现与 feature-time 计算读的都是这一份。"""
    ws = _get(ws_id)
    days = ws.holiday_days()
    return {
        "wsId": ws.id, "version": ws.version,
        "configured": ws.meta.get("holidayDays") is not None,
        "source": ws.meta.get("holidaySource") or f"preset-{features.DEFAULT_HOLIDAY_YEAR}",
        **features.holiday_view(days),
        "presets": [{"year": y, "label": f"{y} 年中国法定节假日", **features.holiday_view(sorted(v))}
                    for y, v in sorted(features.HOLIDAY_PRESETS.items())],
        "maxDays": features.MAX_HOLIDAY_DAYS,
    }


@router.get("/{ws_id}/export")
def export_workspace(ws_id: str, fmt: str = Query("csv", alias="format",
                                                  pattern="^(csv|xlsx|parquet|feather)$")) -> Response:
    """宽表直出：后端拿着自己的工作区编码字节流，浏览器不再回传整表。"""
    ws = _get(ws_id)

    def build():
        return exporter.encode(ws.export_dataframe(), fmt,
                               f"timeseries_{ws.meta.get('name') or 'workspace'}")

    data, media_type, download_name = _guard(build)
    quoted = urllib.parse.quote(download_name)
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
            "Content-Length": str(len(data)),
            # 界面要把真实字节数记进操作记录，跨域时默认只暴露 safelist 头
            "Access-Control-Expose-Headers": "Content-Disposition, Content-Length",
        },
    )


# ---------------- 另存为数据集：服务端帧 → 数据集目录 ----------------

@router.post("/{ws_id}/save-as")
def save_as_dataset(ws_id: str, payload: SaveAsRequest,
                    fmt: str = Query("csv", alias="format",
                                     pattern="^(csv|xlsx|parquet|feather)$")) -> dict:
    """把当前帧写进数据集目录：回第一步就能在「最近打开的数据集」里点开这份加工结果。

    与 GET /export 走同一份编码路径（exporter.encode(ws.export_dataframe(), ...)），
    存进目录的字节与浏览器下载到的字节同源；区别只是目的地在服务端磁盘上，明细不绕网络。
    重名由 dataset_store 追加时间戳另存，绝不覆盖目录里已有的文件。
    """
    ws = _get(ws_id)
    frame = ws.export_dataframe()
    rows, cols = int(frame.shape[0]), int(frame.shape[1])

    def build():
        base = (payload.filename or "").strip() or f"{ws.meta.get('name') or 'workspace'}_v{ws.version}"
        return exporter.encode(frame, fmt, base)

    data, _media, download_name = _guard(build)
    try:
        stored = dataset_store.save_bytes(download_name, data)
        on_disk = (dataset_store.dataset_dir() / stored).stat().st_size
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"写入数据集目录失败：{type(exc).__name__}: {exc}") from exc
    # sizeOnDisk 是写完之后回到磁盘上量的那一份，与 size（内存里编码出的字节数）对得上，
    # 才说明这份数据集真的落盘了，而不是接口返回了一个没人见过的数字。
    return {"wsId": ws.id, "version": ws.version, "format": fmt,
            "filename": stored, "proposed": download_name, "renamed": stored != download_name,
            "dir": str(dataset_store.dataset_dir()),
            "size": len(data), "sizeOnDisk": int(on_disk),
            "sizeText": dataset_store.size_text(len(data)),
            "rows": rows, "cols": cols}


# ---------------- 第五步辅助：特征预览定位 ----------------

@router.get("/{ws_id}/first-complete")
def first_complete(ws_id: str, cols: str = Query(..., description="逗号分隔的新列名"),
                   scan_rows: int = Query(5000, ge=1, le=200000)) -> dict:
    """新增特征列里第一个「行行有值」的行号（长窗口特征开头必然为空）。"""
    ws = _get(ws_id)
    wanted = _split_cols(cols)
    if not wanted:
        raise HTTPException(status_code=400, detail="cols 不能为空")
    return {"wsId": ws.id, "version": ws.version,
            **_guard(explore.first_complete_row, ws.df, wanted, scan_rows)}


@router.get("/{ws_id}/anomaly")
def get_anomaly(ws_id: str) -> dict:
    ws = _get(ws_id)
    return {"meta": ws.meta_view(), **ws.anomaly_view()}


@router.post("/{ws_id}/anomaly-detect")
def op_anomaly_detect(ws_id: str, payload: AnomalyDetectRequest) -> dict:
    """执行检测并把结果留在服务端：修复按这份缓存的行索引走，界面不再回传索引。"""
    ws = _get(ws_id)
    params = {
        "nEstimators": payload.nEstimators, "contamination": payload.contamination,
        "randomState": payload.randomState,
        "normalLowerQ": payload.normalLowerQ, "normalUpperQ": payload.normalUpperQ,
    }
    detection = _guard(ws.run_detection, payload.algo, payload.expr, params)
    return {"detection": detection, "meta": ws.meta_view()}


@router.get("/{ws_id}/resample-preview")
def resample_preview(ws_id: str, targetMinutes: int = Query(..., ge=1, le=1440)) -> dict:
    ws = _get(ws_id)
    return _guard(ws_store.resample_preview, ws, targetMinutes)


@router.post("/{ws_id}/op/time-format")
def op_time_format(ws_id: str, payload: TimeFormatRequest,
                   offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "set_time_format", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/time-col")
def op_time_col(ws_id: str, payload: SetTimeColRequest,
                offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    """指定时间列：整列解析得过才改，改完这一列就是 datetime64（一条可撤销的命令）。"""
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "set_time_col", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/rename-column")
def op_rename(ws_id: str, payload: RenameColumnRequest,
              offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "rename_column", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/delete-column")
def op_delete_column(ws_id: str, payload: DeleteColumnRequest,
                     offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "delete_column", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/convert-unit")
def op_convert_unit(ws_id: str, payload: ConvertUnitRequest,
                    offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "convert_unit", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/derived-column")
def op_derived(ws_id: str, payload: DerivedColumnRequest,
               offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    params = {"name": payload.name, "terms": [t.model_dump() for t in payload.terms]}
    result = _guard(ws.apply, {"kind": "derived_column", "params": params})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/exo-preset")
def op_exo_preset(ws_id: str, payload: ExoPresetRequest,
                  offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    """预设模板：服务端按主表时间列生成一列模拟外生变量（seed 进日志，可重放）。"""
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "exo-preset", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/exo-formula")
def op_exo_formula(ws_id: str, payload: ExoFormulaRequest,
                   offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    """时间公式：hour/day/month/weekday/idx 在服务端向量化求值，浏览器不碰整列。"""
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "exo-formula", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/exo-file")
def op_exo_file(ws_id: str, payload: ExoFileRequest,
                offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    """侧表挂列：文件在 /api/exo/inspect 那一步就落进 dataset/_exo/ 了，这里只引用文件名 + sha。

    和其他命令一样只产生**一条**日志记录：存文件名、内容指纹与规格，重放时重新读那份文件
    （内容被覆盖过会明确报错，不会静默换一列数据）。侧表几十 MB，不必上传两遍。
    """
    ws = _get(ws_id)
    params = payload.model_dump(by_alias=True)
    params["targets"] = [t.model_dump(by_alias=True) for t in payload.targets] or None
    result = _guard(ws.apply, {"kind": "exo-file", "params": params})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/resample")
def op_resample(ws_id: str, payload: ResampleRequest,
                offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "resample", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/split")
def op_split(ws_id: str, payload: SplitApplyRequest,
             offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    """把切分结果落成真实的一列：前端只发比例，三档行数由服务端按整表行数算并回带。"""
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "split_apply", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/impute")
def op_impute(ws_id: str, payload: ImputeRequest,
              offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    """按缺失段填补（可一次跨多列多段），dedupe 非空时同时合并重复时间戳。"""
    ws = _get(ws_id)
    body = payload.model_dump()
    body["targets"] = [t.model_dump() for t in payload.targets]
    result = _guard(ws.apply, {"kind": "impute", "params": body})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/anomaly-repair")
def op_anomaly_repair(ws_id: str, payload: AnomalyRepairRequest,
                      offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    """按服务端留存的检测结果修复：检测后数据又被改过就直接拒绝，不拿旧索引乱动行。"""
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "anomaly-repair", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/mask-generate")
def op_mask_generate(ws_id: str, payload: MaskGenerateRequest,
                     offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "mask-generate", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/mask-delete")
def op_mask_delete(ws_id: str, payload: MaskDeleteRequest,
                   offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "mask-delete", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


# ---------------- 第五步：特征构建 ----------------

@router.get("/{ws_id}/value-counts")
def value_counts(ws_id: str, keys: str = Query(..., description="逗号分隔的类别列名"),
                 method: str = Query("onehot", description="编码方式，只用于算出预计新增列数")) -> dict:
    """类别列的整表取值分布：面板上的「预计新增 N 列 / 取值占比」全部由服务端数出来。"""
    ws = _get(ws_id)
    wanted = [k for k in keys.split(",") if k]
    if not wanted:
        raise HTTPException(status_code=400, detail="keys 不能为空")
    if method not in features.CAT_METHODS:
        raise HTTPException(status_code=400, detail=f"不支持的编码方式：{method}")
    labels = {c["key"]: c["label"] for c in ws.meta["columns"]}

    def compute():
        data = features.value_counts(ws.df, wanted, labels)
        # 预计新增列数用 uniqueTotal 而不是截断后的 uniqueVals 长度：
        # 面板上的数字要和 /op/feature-cat 真会生成的列数一致，高基数列也不例外
        total = sum(c["uniqueTotal"] for c in data["columns"]) if method == "onehot" else len(wanted)
        return {"wsId": ws.id, "version": ws.version, "method": method,
                "totalNewCols": total, **data}

    return _guard(compute)


@router.post("/{ws_id}/op/holidays")
def op_holidays(ws_id: str, payload: HolidaysRequest,
                offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    """配置节假日表：只改工作区配置、不动帧，所以它仍是一条可撤销、可重放的命令。"""
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "holidays", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/feature-time")
def op_feature_time(ws_id: str, payload: FeatureTimeRequest,
                    offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "feature_time", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/feature-lag")
def op_feature_lag(ws_id: str, payload: FeatureLagRequest,
                   offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "feature_lag", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/feature-diff")
def op_feature_diff(ws_id: str, payload: FeatureDiffRequest,
                    offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "feature_diff", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/op/feature-cat")
def op_feature_cat(ws_id: str, payload: FeatureCatRequest,
                   offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    result = _guard(ws.apply, {"kind": "feature_cat", "params": payload.model_dump()})
    return _with_page(ws, result, offset, limit)


@router.post("/{ws_id}/restore")
def restore(ws_id: str, payload: RestoreRequest,
            offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE)) -> dict:
    ws = _get(ws_id)
    # 允许往回也允许往前：撤销与重做都是 restore，日志尾部留着的就是可重做的那段
    if payload.version > len(ws.ops):
        raise HTTPException(status_code=400, detail=f"版本号越界：{payload.version}（日志共 {len(ws.ops)} 条）")
    meta = _guard(ws.restore, payload.version)
    return {"meta": meta, "restoredTo": payload.version, "page": ws.rows(offset, limit)}


@router.delete("/{ws_id}")
def close(ws_id: str) -> dict:
    return {"closed": ws_store.close(ws_id)}
