"""TimeSeries Studio 后端：为浏览器端无法胜任的能力提供真实计算。

无状态设计：前端随每个请求携带数据，服务端不使用任何数据库。
唯一的落盘位置是数据集目录 <cwd>/dataset（可用 TSS_DATASET_DIR 覆盖），不存在时自动创建。

  GET    /api/health                   探活，前端据此决定"在线/需后端"呈现
  POST   /api/parse                    解析 CSV/Excel/Parquet/Feather（pyarrow 真实解码列式压缩）
  POST   /api/export                   编码为 Parquet/Feather/CSV/Excel 字节流
  POST   /api/anomaly/iforest          scikit-learn 完整版孤立森林逐列检测
  GET    /api/datasets                 列举数据集目录中的真实文件（"最近打开的数据集"）
  POST   /api/datasets                 导入上传文件：落盘到数据集目录并解析返回
  GET    /api/datasets/{name}/parse    打开数据集目录中的已有文件并解析
"""
from __future__ import annotations

import urllib.parse

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from . import __version__
from .schemas import ExportRequest, IForestRequest
from .services import dataset_store
from .services.anomaly import detect_iforest
from .services.exporter import build_export
from .services.parser import parse_table

MAX_UPLOAD_BYTES = 64 * 1024 * 1024

app = FastAPI(
    title="TimeSeries Studio Server",
    description="时序数据清洗工作台后端（FastAPI · 无数据库）",
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


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "capabilities": [
            "parse:parquet", "parse:feather",
            "export:parquet", "export:feather",
            "anomaly:iforest",
            "datasets:list", "datasets:import", "datasets:open",
        ],
        "datasetDir": dataset_store.dataset_dir(),
    }


@app.post("/api/parse")
async def parse(file: UploadFile = File(...)) -> dict:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="上传文件超过 64MB 上限")
    try:
        return parse_table(content, file.filename or "data.csv")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"解析失败：{type(exc).__name__}: {exc}") from exc


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


@app.post("/api/anomaly/iforest")
def iforest(payload: IForestRequest) -> dict:
    if not payload.columns:
        raise HTTPException(status_code=400, detail="columns 不能为空")
    try:
        return detect_iforest(payload.model_dump())
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"检测失败：{type(exc).__name__}: {exc}") from exc


@app.get("/api/datasets")
def datasets_list() -> dict:
    """列举数据集目录中的真实文件；目录不存在时由 dataset_dir() 自动创建。"""
    try:
        return dataset_store.list_datasets()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取数据集目录失败：{type(exc).__name__}: {exc}") from exc


@app.post("/api/datasets")
async def datasets_import(file: UploadFile = File(...)) -> dict:
    """导入：把上传文件落盘到数据集目录，再解析返回（因此下次打开即出现在最近列表）。"""
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="上传文件超过 64MB 上限")
    try:
        saved = dataset_store.save_bytes(file.filename or "data.csv", content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"写入数据集目录失败：{type(exc).__name__}: {exc}") from exc
    try:
        parsed = parse_table(content, saved)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"解析失败：{type(exc).__name__}: {exc}") from exc
    return {**parsed, "saved": saved, "dir": str(dataset_store.dataset_dir())}


@app.get("/api/datasets/{filename}/parse")
def datasets_open(filename: str) -> dict:
    """打开数据集目录中的已有文件并解析。"""
    try:
        content, real_name = dataset_store.read_bytes(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return parse_table(content, real_name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"解析失败：{type(exc).__name__}: {exc}") from exc
