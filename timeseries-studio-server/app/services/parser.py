"""表格文件解析：CSV / Excel / Parquet / Feather → 统一的前端数据集结构。

浏览器无法可靠解码 Parquet/Feather 的列式压缩，交由后端用 pyarrow 真实解析。
CSV/Excel 后端也支持，便于前端统一走同一入口（前端本地解析仍保留为离线回退）。
"""
from __future__ import annotations
import io
import math
from datetime import datetime, date
from typing import Any

import numpy as np
import pandas as pd

# 支持的时间列名启发式
_TIME_NAME_HINTS = ("timestamp", "time", "date", "datetime", "日期", "时间", "ts")


def _detect_format(filename: str) -> str:
    """返回与前端 d.format 约定一致的标识（xlsx/xls 而非笼统的 excel）。"""
    name = (filename or "").lower()
    if name.endswith(".parquet"):
        return "parquet"
    if name.endswith(".feather") or name.endswith(".ft"):
        return "feather"
    if name.endswith(".xlsx"):
        return "xlsx"
    if name.endswith(".xls"):
        return "xls"
    return "csv"


def _read_df(content: bytes, fmt: str) -> pd.DataFrame:
    buf = io.BytesIO(content)
    if fmt == "parquet":
        return pd.read_parquet(buf, engine="pyarrow")
    if fmt == "feather":
        return pd.read_feather(buf)
    if fmt in ("xlsx", "xls"):
        return pd.read_excel(buf, engine="openpyxl")
    # CSV：优先 utf-8-sig（带 BOM），失败回退 gbk（中文电力数据常见）
    buf.seek(0)
    raw = buf.getvalue()
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return pd.read_csv(io.BytesIO(raw), encoding=enc)
        except UnicodeDecodeError:
            continue
    buf.seek(0)
    return pd.read_csv(buf)


def _jsonable(v: Any) -> Any:
    """把 numpy / pandas / datetime 标量转成 JSON 可序列化的原生类型，缺失转 None。"""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (np.floating,)):
        f = float(v)
        return None if math.isnan(f) else f
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return v.isoformat(sep=" ") if isinstance(v, (pd.Timestamp, datetime)) else v.isoformat()
    if isinstance(v, np.generic):
        return _jsonable(v.item())
    if isinstance(v, float) and math.isinf(v):
        return None
    return v


def _col_type(series: pd.Series) -> str:
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    if pd.api.types.is_numeric_dtype(series):
        return "float"
    return "category"


def _detect_time_col(df: pd.DataFrame) -> str | None:
    for c in df.columns:
        if str(c).lower() in _TIME_NAME_HINTS or any(h in str(c).lower() for h in _TIME_NAME_HINTS):
            return c
    dt_cols = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    return dt_cols[0] if dt_cols else None


def _detect_freq_minutes(times: list[str]) -> int | None:
    """从解析后的时间字符串推断采样间隔（分钟），失败返回 None。"""
    ts = pd.to_datetime(pd.Series([t for t in times if t]), errors="coerce").dropna()
    if len(ts) < 2:
        return None
    diffs = ts.diff().dropna().dt.total_seconds() / 60
    diffs = diffs[diffs > 0]
    if diffs.empty:
        return None
    med = float(diffs.median())
    return int(round(med)) if med >= 1 else None


def parse_table(content: bytes, filename: str) -> dict:
    """解析上传字节，返回 {name, format, columns, timeCol, freqMinutes, rowCount, data}。"""
    fmt = _detect_format(filename)
    df = _read_df(content, fmt)

    # 尝试把看起来像时间的字符串列转成 datetime，便于前端时序处理
    for c in df.columns:
        if df[c].dtype == object:
            sample = df[c].dropna().head(20).astype(str).tolist()
            if sample and all(_looks_like_time(s) for s in sample):
                try:
                    df[c] = pd.to_datetime(df[c], errors="coerce")
                except Exception:
                    pass

    time_col = _detect_time_col(df)
    columns = [
        {
            "key": str(c),
            "label": str(c),
            "type": _col_type(df[c]),
            "isTime": (str(c) == str(time_col)),
        }
        for c in df.columns
    ]

    records: list[dict] = []
    for _, row in df.iterrows():
        records.append({str(c): _jsonable(row[c]) for c in df.columns})

    freq = _detect_freq_minutes([r.get(str(time_col)) for r in records]) if time_col else None

    return {
        "name": (filename or "uploaded").rsplit("/", 1)[-1],
        "format": fmt,
        "columns": columns,
        "timeCol": str(time_col) if time_col is not None else None,
        "freqMinutes": freq,
        "rowCount": len(records),
        "data": records,
    }


def _looks_like_time(s: str) -> bool:
    s = s.strip()
    if len(s) < 8:
        return False
    has_date = bool(pd.to_datetime(s, errors="coerce", dayfirst=True) is not pd.NaT)
    has_sep = ("/" in s or "-" in s) and any(ch.isdigit() for ch in s)
    return has_date and has_sep
