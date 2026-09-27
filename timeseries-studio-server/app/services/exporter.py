"""数据集导出：把 DataFrame 编码为 Parquet / Feather / CSV / Excel 字节流。

Parquet / Feather 是列式二进制压缩格式，浏览器单文件无法可靠产出，
由后端用 pyarrow 真实编码；CSV / Excel 也一并支持，作为可复现归档。

两条入口：
- encode(df, ...)            —— 工作区直出（GET /api/ws/{id}/export），明细不过网络；
- build_export(cols, rows..) —— 调用方自带行数据（POST /api/export），给脚本与外部工具用。
"""
from __future__ import annotations
import io
from typing import Any

import pandas as pd

_MEDIA_TYPES = {
    "parquet": "application/vnd.apache.parquet",
    "feather": "application/vnd.apache.arrow.file",
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_EXTENSIONS = {"parquet": "parquet", "feather": "feather", "csv": "csv", "xlsx": "xlsx"}


def encode(df: pd.DataFrame, fmt: str, filename: str | None) -> tuple[bytes, str, str]:
    """返回 (字节内容, media_type, 下载文件名)。"""
    if fmt == "parquet":
        buf = io.BytesIO()
        df.to_parquet(buf, engine="pyarrow", index=False)
        data = buf.getvalue()
    elif fmt == "feather":
        buf = io.BytesIO()
        df.to_feather(buf)
        data = buf.getvalue()
    elif fmt == "csv":
        data = df.to_csv(index=False).encode("utf-8-sig")
    elif fmt == "xlsx":
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="data")
        data = buf.getvalue()
    else:
        raise ValueError(f"不支持的导出格式: {fmt}")

    media = _MEDIA_TYPES[fmt]
    base = (filename or "timeseries_export").rsplit(".", 1)[0]
    download_name = f"{base}.{_EXTENSIONS[fmt]}"
    return data, media, download_name


def build_export(fmt: str, columns: list[str], rows: list[dict[str, Any]], filename: str | None) -> tuple[bytes, str, str]:
    return encode(pd.DataFrame(rows, columns=columns), fmt, filename)
