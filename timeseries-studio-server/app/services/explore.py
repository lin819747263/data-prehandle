"""第三步「统计概览与图表」的服务端实现：统计矩阵、分布直方图、多列叠加曲线。

这里的数字必须与迁移前浏览器里那份 statsMatrix 逐格对得上，所以口径全部照搬旧实现，
而不是"用 pandas 重新定义一遍"：

- 分位数取排序后第 floor(n·0.25) / floor(n·0.75) 个**观测值**，不是 np.percentile 的线性插值；
- 偶数个观测值时中位数取中间两个的平均；
- 标准差是总体标准差（÷n），与第②期 3σ 判据用的 quality.col_stats 同一份口径
  （pandas .std() 默认 ÷(n-1)，所以这里一律不走 .std()）；
- 直方图固定 25 桶、桶宽 (max-min)/25，全列同值时桶宽退化为 1，末值并入最后一桶；
- 窗口均值降采样沿用的是旧浏览器实现：步长 4、只对区间内的有效值求平均、结果 parseFloat(x.toFixed(2))。

曲线为什么必须抽稀：叠加曲线原先把整表的每一行都送进浏览器（11000 行 × 12 列就是 13 万个数），
数据量一大光 JSON 就几 MB。现在由后端按点数上限抽稀，并如实回说抽了多少。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import features, quality

DEFAULT_BINS = 25
DEFAULT_POINTS = 3000
MAX_POINTS = 6000                 # 与第④期曲线同一上限（quality.MAX_ENVELOPE_POINTS）
MEAN_WINDOW_TRIGGER = 500         # 旧实现：行数 > 500 才做窗口均值
MEAN_STEP = 4
MIN_COLUMN_BUDGET = 200           # 抽稀时单列至少保留这么多点，否则叠十几列就只剩噪声
SERIES_MODES = ("raw", "extremes", "mean")


def _float_arrays(frame: pd.DataFrame, keys: list[str]) -> dict[str, np.ndarray]:
    columns = [str(c) for c in frame.columns]
    for key in keys:
        if key not in columns:
            raise ValueError(f"列不存在：{key}")
    return {key: quality.numeric_array(frame, key) for key in keys}


# ---------------------------------------------------------------- 统计矩阵

def stats_matrix(frame: pd.DataFrame, keys: list[str], labels: dict[str, str] | None = None) -> dict:
    """每列 Count/Mean/Std/Min/Q1/Median/Q3/Max/缺失率 + 画「分布条」用的 globalMax。

    globalMax 的起点是 1、只增不减（旧实现如此）：整表都在 0~1 之间时分布条仍按 0~1 画，
    不会因为除以一个很小的最大值而溢出画布。
    """
    arrays = _float_arrays(frame, keys)
    total_rows = int(frame.shape[0])
    global_max = 1.0
    rows = []
    for key in keys:
        stats = quality.col_stats(arrays[key])
        if stats["n"] and stats["max"] is not None and stats["max"] > global_max:
            global_max = float(stats["max"])
        rows.append({
            "key": key,
            "label": (labels or {}).get(key, key),
            "missingRate": (stats["missing"] / total_rows * 100) if total_rows else 0.0,
            **stats,
        })
    return {"rowCount": total_rows, "colCount": len(keys), "globalMax": global_max, "rows": rows}


# ---------------------------------------------------------------- 分布直方图

def histogram(frame: pd.DataFrame, key: str, bins: int = DEFAULT_BINS) -> dict:
    """单列频次分布：桶边界、计数，以及均值/中位数各落在哪一根桶上（界面要高亮）。"""
    arr = _float_arrays(frame, [key])[key]
    valid = np.sort(arr[~np.isnan(arr)])
    n = int(valid.size)
    bins = max(2, min(int(bins), 200))
    out = {"col": key, "bins": bins, "n": n, "missing": int(arr.size) - n,
           "totalRows": int(arr.size), "min": None, "max": None, "mean": None, "median": None,
           "binWidth": None, "edges": [], "counts": [], "meanBin": None, "medianBin": None}
    if n == 0:
        return out
    lo, hi = float(valid[0]), float(valid[-1])
    width = (hi - lo) / bins or 1.0            # 全列同值时桶宽退化为 1（同旧实现）
    idx = np.floor((valid - lo) / width).astype("int64")
    np.clip(idx, 0, bins - 1, out=idx)         # 最大值本就落在第 bins 桶外，并入末桶
    counts = np.bincount(idx, minlength=bins)[:bins]
    mean = float(valid.mean())
    median = float(np.median(valid))
    span = (hi - lo) or 1.0
    out.update({
        "min": lo, "max": hi, "mean": mean, "median": median, "binWidth": float(width),
        "edges": [lo + i * width for i in range(bins)],
        "counts": [int(c) for c in counts.tolist()],
        "meanBin": _clamp_bin(math.floor((mean - lo) / span * bins), bins),
        "medianBin": _clamp_bin(math.floor((median - lo) / span * bins), bins),
    })
    return out


def _clamp_bin(idx: int, bins: int) -> int:
    return min(bins - 1, max(0, int(idx)))


# ---------------------------------------------------------------- 叠加曲线

def _stride(positions: list[int], cap: int) -> list[int]:
    """点数超上限时等距抽稀，首尾必留（旧实现把整表交给 ECharts 的 LTTB，现在换成这里说清楚的等距）。"""
    if len(positions) <= cap:
        return positions
    step = math.ceil(len(positions) / cap)
    kept = positions[::step]
    if kept[-1] != positions[-1]:
        kept.append(positions[-1])
    return kept


def _mean_chunks(arr: np.ndarray, positions: list[int], step: int) -> list:
    """每个窗口取区间内**有效值**的算术平均，空窗口给 None；平均后按 parseFloat(x.toFixed(2)) 舍入。"""
    out = []
    for start in positions:
        chunk = arr[start:start + step]
        valid = chunk[~np.isnan(chunk)]
        if valid.size == 0:
            out.append(None)
            continue
        total = 0.0
        for v in valid.tolist():          # 逐项累加：与浏览器 reduce((a,b)=>a+b,0) 同一个加法顺序
            total += float(v)
        out.append(features.scalar_round(total / int(valid.size), 2))
    return out


def series_multi(frame: pd.DataFrame, keys: list[str], labels: list[str],
                 mode: str = "extremes", points: int = DEFAULT_POINTS,
                 labels_by_key: dict[str, str] | None = None) -> dict:
    """多列叠加曲线：共享一份 x（时间标签），每列一条 y，抽稀策略由 mode 决定。

    - raw      全量点（行数超过 points 时等距抽稀，decimated 会说明）
    - extremes 每列各取桶内极值/缺失端点，再取并集（尖峰不会被抽掉，同 quality.envelope）
    - mean     每 MEAN_STEP 行取窗口均值（旧界面「窗口均值降采样」那一档）
    """
    if mode not in SERIES_MODES:
        raise ValueError(f"不支持的降采样方式：{mode}（可选 {'/'.join(SERIES_MODES)}）")
    if not keys:
        raise ValueError("没有要绘制的列")
    arrays = _float_arrays(frame, keys)
    n = int(frame.shape[0])
    cap = max(20, min(int(points), MAX_POINTS))
    step = 1
    if n == 0:
        positions: list[int] = []
    elif mode == "mean":
        step = MEAN_STEP if n > MEAN_WINDOW_TRIGGER else 1
        positions = list(range(0, n, step))
    elif mode == "raw":
        positions = _stride(list(range(n)), cap)
    else:
        budget = max(MIN_COLUMN_BUDGET, cap // len(keys))
        keep: set[int] = set()
        for key in keys:
            keep.update(quality.envelope(arrays[key], budget))
        positions = _stride(sorted(keep), cap)

    series = []
    for key in keys:
        arr = arrays[key]
        if step > 1:
            values = _mean_chunks(arr, positions, step)
        else:
            ys = [arr[i] for i in positions]
            values = [None if math.isnan(v) else float(v) for v in ys]
        series.append({"col": key, "label": (labels_by_key or {}).get(key, key), "y": values,
                       "missing": sum(1 for v in values if v is None)})
    return {
        "mode": mode, "rowCount": n, "points": len(positions),
        "windowStep": step, "maxPoints": cap,
        "decimated": len(positions) < n,
        "x": [labels[i] if i < len(labels) else str(i) for i in positions],
        "series": series,
    }


# ---------------------------------------------------------------- 首个完整行

def first_complete_row(frame: pd.DataFrame, keys: list[str], scan_rows: int = 5000) -> dict:
    """新增特征列里第一行全部有值的行号：长窗口特征前 N 行必然为空，界面要跳过去看真数。

    旧实现是浏览器拉整列（最多 5000 行 × 列数）回来自己扫，那一趟 JSON 比结果本身大得多。
    """
    if not keys:
        raise ValueError("没有要检查的列")
    columns = [str(c) for c in frame.columns]
    for key in keys:
        if key not in columns:
            raise ValueError(f"列不存在：{key}")
    mask = np.ones(int(frame.shape[0]), dtype=bool)
    for key in keys:
        series = frame[key]
        if pd.api.types.is_numeric_dtype(series):
            na = np.isnan(quality.numeric_array(frame, key))
        else:
            na = series.isna().to_numpy() | (series.astype("string").str.strip() == "").to_numpy()
        mask &= ~na
    limit = min(int(frame.shape[0]), max(1, int(scan_rows)))
    found = np.flatnonzero(mask[:limit])
    return {"scanned": limit, "rowCount": int(frame.shape[0]),
            "index": int(found[0]) if found.size else None,
            "completeWithinScan": int(mask[:limit].sum())}
