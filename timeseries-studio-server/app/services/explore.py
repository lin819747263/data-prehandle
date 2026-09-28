"""第三步「统计概览与图表」的服务端实现：统计矩阵、分布直方图、多列叠加曲线。

这里的数字必须与迁移前浏览器里那份 statsMatrix 逐格对得上，所以口径全部照搬旧实现，
而不是"用 pandas 重新定义一遍"：

- 分位数取排序后第 floor(n·0.25) / floor(n·0.75) 个**观测值**，不是 np.percentile 的线性插值；
- 偶数个观测值时中位数取中间两个的平均；
- 标准差是总体标准差（÷n），与第②期 3σ 判据用的 quality.col_stats 同一份口径
  （pandas .std() 默认 ÷(n-1)，所以这里一律不走 .std()）；
- 直方图固定 25 桶、桶宽 (max-min)/25，全列同值时桶宽退化为 1，末值并入最后一桶；
- 窗口均值降采样沿用的是旧浏览器实现：步长 4、只对区间内的有效值求平均、结果 parseFloat(x.toFixed(2))。

曲线为什么必须降采样：叠加曲线原先把整表的每一行都送进浏览器（11000 行 × 12 列就是 13 万个数），
数据量一大光 JSON 就几 MB。现在由后端按点数上限降采样（默认 LTTB），并如实回说抽了多少；
「全量」这一档例外——它把窗口内每一行都送回，一个点都不抽，格子数超过上限时直接报错而不偷偷抽点。
第四步的质量曲线折线默认就是这一档（全量），只有覆盖层仍受每列预算约束，见 series_quality。
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
MIN_COLUMN_BUDGET = 200           # 降采样时单列至少保留这么多点，否则叠十几列就只剩噪声
FULL_RAW_MAX_VALUES = 600_000     # 「全量」一次回传的格子数上限（行 × 列）：超了就明确报错，绝不偷偷抽点
SERIES_MODES = ("raw", "extremes", "mean", "lttb")
DEFAULT_MODE = "lttb"
# 第三步的时间窗口档位：按自然周期筛行，并且能在这些周期之间左右翻页（offset）。
# 不是「最近 N 天」也不是「按行数估算的百分比」——界面上写的窗口必须与后端筛的行同源。
SPANS = ("all", "year", "month", "week", "day")
SPAN_CN = {"all": "全量", "year": "自然年", "month": "自然月",
           "week": "自然周（周一起）", "day": "自然日"}
# 分页用的 period 频率：pandas 的 'W' 本就是「周一起、周日止」，
# 与旧版手算的 t0 - dayofweek 同一份口径，换成 to_period 不会把周一变成周日。
PERIOD_FREQ = {"year": "Y", "month": "M", "week": "W", "day": "D"}


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


def _fill_nan(arr: np.ndarray) -> tuple[np.ndarray, float]:
    """缺失值代入本列有效均值得到一份纯数值序列，并给出极差（全列同值或全缺失时极差取 1）。

    LTTB 靠三角形面积选点，面积一旦碰上 NaN 整段就塌成不可比较；用均值代入只影响"选哪一行"，
    回传给画图的取值仍是原始数组，缺失行照旧是 null —— 降采样不该把空洞抹平成直线。
    """
    valid = arr[~np.isnan(arr)]
    filler = float(valid.mean()) if valid.size else 0.0
    filled = np.where(np.isnan(arr), filler, arr)
    span = (float(valid.max()) - float(valid.min())) if valid.size else 0.0
    return filled, (span or 1.0)


def lttb_positions(arrays: list[np.ndarray], threshold: int) -> list[int]:
    """LTTB（最大三角形三桶）降采样：把 n 个点降到 threshold 个，尽量保住折线的视觉形态。

    桶边界与叉积面积按 Steinarsson 的原始定义实现（见 scripts/verify_step3_downsample.py 里
    那份纯 JS 参照实现，单列时逐点比对）。多列共用一条时间轴时，每个桶取的是
    **各列归一化面积之和**最大的那一行：面积除以该列极差，量纲不同的列才能相加，
    而且这样选出的点不取决于用户先勾了哪一列（按首列选点会让后勾的列的尖峰随机消失）。
    单列时归一化只是一个常数因子，与标准 LTTB 逐点相同。
    """
    n = min((int(a.size) for a in arrays), default=0)
    t = int(threshold)
    if n <= t or t < 3:
        return list(range(n))
    prepared = [_fill_nan(a) for a in arrays]
    filled = np.vstack([p[0] for p in prepared])          # k × n
    norms = np.array([p[1] for p in prepared], dtype=float)
    xs = np.arange(n, dtype=float)

    keep = [0]
    a = 0
    every = (n - 2) / (t - 2)
    for i in range(t - 2):
        cur_s = int(i * every) + 1
        cur_e = min(int((i + 1) * every) + 1, n)
        nxt_s = cur_e
        nxt_e = min(int((i + 2) * every) + 1, n)
        if nxt_e <= nxt_s:
            nxt_e = min(nxt_s + 1, n)
        if cur_e <= cur_s or nxt_e <= nxt_s or cur_s >= n:
            break
        avg_x = float(xs[nxt_s:nxt_e].mean())
        avg_y = filled[:, nxt_s:nxt_e].mean(axis=1)       # 每列下一桶的均值点
        left_x = float(a)
        left_y = filled[:, a]
        idx = xs[cur_s:cur_e]
        areas = np.abs((left_x - avg_x) * (filled[:, cur_s:cur_e] - left_y[:, None])
                       - (left_x - idx[None, :]) * (avg_y - left_y)[:, None])
        score = (areas / norms[:, None]).sum(axis=0)
        a = cur_s + int(np.argmax(score))                 # 并列取靠前的那一行（与 > 比较同效）
        keep.append(a)
    keep.append(n - 1)
    return keep


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


def _shared_positions(arrays: dict[str, np.ndarray], keys: list[str], cap: int) -> list[int]:
    """多列共用一条时间轴时的取点：每列各取桶内极值/缺失端点，并集后再按总上限等距抽稀。

    单列预算下限是 MIN_COLUMN_BUDGET：叠十几列时若不设下限，每列只分到几十个点，尖峰会被抽平。
    但下限也不能盖过 cap 本身——预算比上限还大，并集必然要走 `_stride`，而等距抽稀恰好会把
    envelope 辛苦留下的尖峰按间隔抽掉，这张图就是为了看尖峰才存在的。
    """
    budget = max(min(MIN_COLUMN_BUDGET, cap), cap // len(keys))
    keep: set[int] = set()
    for key in keys:
        keep.update(quality.envelope(arrays[key], budget))
    return _stride(sorted(keep), cap)


def time_series_of(frame: pd.DataFrame, time_col: str | None) -> pd.Series | None:
    """取工作区的时间列（转成 datetime64）；没有时间列就给 None，由调用方决定怎么报错。"""
    if not time_col or time_col not in [str(c) for c in frame.columns]:
        return None
    ts = frame[time_col]
    if not pd.api.types.is_datetime64_any_dtype(ts):
        ts = pd.to_datetime(ts, errors="coerce")
    return ts


def _calendar_periods(ts: pd.Series, span: str) -> tuple[pd.Series, list]:
    """每行所属的自然周期（Period）+ 数据里真实出现过行的周期清单（升序）。

    清单只列有行的周期：翻到一段没有数据的日期会得到一张空图，
    与其给一个「第 5/9 期 · 0 行」，不如让左右箭头停在实际有数据的那一期上。
    """
    labels = ts.dt.to_period(PERIOD_FREQ[span])
    return labels, sorted(pd.unique(labels.dropna()))


def resolve_window(ts: pd.Series | None, span: str, offset: int = 0) -> tuple[np.ndarray, dict]:
    """把 span + offset 解析成落在该自然周期内的**行下标数组** + 一份给界面照抄的边界说明。

    用「时间列在 [起,止) 内」筛选，而不是「前 N 行」：采样间隔一旦不均匀（缺行、重复时间戳），
    按行数估的窗口就和真实时间跨度差得越来越远，界面上的「1 天」就成了假数字。

    offset 是在周期清单上的位移（0 = 第一期），越界就贴到最近的一端，
    并把请求值和实际值一起回给界面——箭头按到底不能假装还能翻。
    """
    n = 0 if ts is None else int(ts.size)
    if span not in SPANS:
        raise ValueError(f"不支持的时间窗口：{span}（可选 {'/'.join(SPANS)}）")
    requested = int(offset or 0)
    if span == "all":
        return np.arange(n), {"span": span, "label": SPAN_CN[span],
                              "from": None, "to": None, "windowRows": n, "totalRows": n,
                              "periodTotal": 1, "periodIndex": 1, "offset": 0,
                              "requested": requested, "clamped": False}
    if ts is None:
        raise ValueError("按年/月/周/日查看需要时间列，当前工作区没有可用的时间列")
    labels, periods = _calendar_periods(ts, span)
    if not periods:
        raise ValueError("时间列全部解析失败，无法按年/月/周/日取窗口")
    idx = min(max(requested, 0), len(periods) - 1)
    chosen = periods[idx]
    start, end = chosen.start_time, (chosen + 1).start_time
    sel = np.flatnonzero((labels == chosen).to_numpy())
    return sel, {"span": span, "label": SPAN_CN[span],
                 "from": start.strftime("%Y-%m-%d %H:%M:%S"),
                 "to": end.strftime("%Y-%m-%d %H:%M:%S"),
                 "windowRows": int(sel.size), "totalRows": n,
                 "periodTotal": len(periods), "periodIndex": int(idx) + 1,
                 "offset": int(idx), "requested": requested, "clamped": idx != requested}


def series_multi(frame: pd.DataFrame, keys: list[str], labels: list[str],
                 mode: str = DEFAULT_MODE, points: int = DEFAULT_POINTS,
                 labels_by_key: dict[str, str] | None = None,
                 ts: pd.Series | None = None, span: str = "all", offset: int = 0) -> dict:
    """多列叠加曲线：共享一份时间轴，每列一条降采样后的序列，降采样只在 span 窗口内做。

    - raw      全量：窗口内每一行都送回，**不做任何抽取**（格子数超过 FULL_RAW_MAX_VALUES 时直接报错，
               而不是偷偷抽点——一抽点，界面上写的「全量」就成了假话）
    - extremes 每列各取桶内极值/缺失端点，再取并集（尖峰不会被抽掉，同 quality.envelope）
    - mean     每 MEAN_STEP 行取窗口均值（旧界面「窗口均值降采样」那一档）
    - lttb     LTTB 三角形面积降采样（默认）：点数按上限摊到各桶，形态起伏最保真

    span 与 offset 决定取哪段行（见 resolve_window）：窗口内的点数上限与整表同值，所以「看一天」
    拿到的是这一天自己的 3000 个点，而不是整年 3000 个点里漏下的几颗。
    """
    if mode not in SERIES_MODES:
        raise ValueError(f"不支持的降采样方式：{mode}（可选 {'/'.join(SERIES_MODES)}）")
    if not keys:
        raise ValueError("没有要绘制的列")
    arrays = _float_arrays(frame, keys)
    n = int(frame.shape[0])
    sel, window = resolve_window(ts, span, offset)
    cap = max(20, min(int(points), MAX_POINTS))
    sub = {key: arrays[key][sel] for key in keys}
    m = int(sel.size)
    step = 1
    if m == 0:
        local: list[int] = []
    elif mode == "raw":
        values = m * len(keys)
        if values > FULL_RAW_MAX_VALUES:
            raise ValueError(
                f"全量模式要送回 {m:,} 行 × {len(keys)} 列 = {values:,} 个格子，"
                f"超过单次上限 {FULL_RAW_MAX_VALUES:,}；请把时间窗口收窄到更小的周期，或改用降采样方式")
        local = list(range(m))
    elif mode == "mean":
        step = MEAN_STEP if m > MEAN_WINDOW_TRIGGER else 1
        local = list(range(0, m, step))
    elif mode == "extremes":
        local = _shared_positions(sub, keys, cap)
    else:
        local = lttb_positions([sub[key] for key in keys], cap)

    series = []
    for key in keys:
        arr = sub[key]
        if step > 1:
            values = _mean_chunks(arr, local, step)
        else:
            ys = [arr[i] for i in local]
            values = [None if math.isnan(v) else float(v) for v in ys]
        series.append({"col": key, "label": (labels_by_key or {}).get(key, key), "y": values,
                       "missing": sum(1 for v in values if v is None)})
    idx = [int(sel[i]) for i in local]
    return {
        "mode": mode, "rowCount": n, "points": len(local),
        "windowRows": m, "window": window,
        "windowStep": step, "maxPoints": None if mode == "raw" else cap,
        "decimated": len(local) < m,
        "x": [labels[i] if i < len(labels) else str(i) for i in idx],
        "idx": idx,
        "series": series,
    }


# ---------------------------------------------------------------- 质量曲线（第四步）

def _label_at(labels: list, i: int) -> str:
    return labels[i] if 0 <= i < len(labels) else str(i)


def series_quality(frame: pd.DataFrame, keys: list[str], labels: list[str],
                   points: int = 0,
                   labels_by_key: dict[str, str] | None = None,
                   detection: dict | None = None, stale: bool = False) -> dict:
    """第四步的多列质量曲线：与叠加曲线同一套取点，另外带每列的缺失标记与异常覆盖层。

    - points = 0（默认，全量）：整表每一行都送回折线，**一个点都不抽**。格子数（行 × 列）超过
      FULL_RAW_MAX_VALUES 时直接报错，而不是偷偷抽点——一抽点，界面上写的「全量」就成了假话。
    - points > 0：按该上限抽稀（与第三步 extremes 档同一套取点），供脚本与旧调用方使用。

    与 series_multi 的区别只在「如实说明画不出来的部分」：折线的 markLine 与 scatter 都按时间
    标签匹配类目轴，抽稀后不在轴上的标签根本落不回去，所以这里只回能落到轴上的标记，
    同时把整列的真实缺失数/异常数与截断标志一起给出，界面不能说「图上没有」就是「数据里没有」。
    折线走全量后仍少画的只有两类覆盖层：缺失虚线每列 MAX_MISSING_MARKS 条、
    异常散点每列 overlay_budget 个——那是渲染与 JSON 的预算，不是抽点。
    """
    if not keys:
        raise ValueError("没有要绘制的列")
    arrays = _float_arrays(frame, keys)
    n = int(frame.shape[0])
    requested = int(points or 0)
    full = requested <= 0
    cap = None if full else max(20, min(requested, MAX_POINTS))
    if full:
        values = n * len(keys)
        if values > FULL_RAW_MAX_VALUES:
            # 上限是按格子数（行 × 列）算的，所以能直接告诉界面这张表一次最多画几列——
            # 第四步没有降采样开关，用户看得懂的补救动作只有「少勾几列」。
            raise ValueError(
                f"全量曲线要送回 {n:,} 行 × {len(keys)} 列 = {values:,} 个格子，"
                f"超过单次上限 {FULL_RAW_MAX_VALUES:,}：这张表一次最多画 {FULL_RAW_MAX_VALUES // n} 列，请减少绘图列")
        positions = list(range(n))
    else:
        positions = _shared_positions(arrays, keys, cap) if n else []
    on_axis = np.zeros(n, dtype=bool)
    on_axis[positions] = True
    # 覆盖层的预算按列数分摊：叠十几列时 4000 点 × 列数 会把响应撑到几 MB
    overlay_budget = max(200, quality.MAX_ANOMALY_POINTS // len(keys))

    per_col = (detection or {}).get("perColumn") or {}
    series = []
    for key in keys:
        arr = arrays[key]
        nan = np.isnan(arr)
        marks = np.flatnonzero(nan)
        all_marks = int(marks.size)
        marks = marks[on_axis[marks]]
        res = {
            "col": key, "label": (labels_by_key or {}).get(key, key),
            "y": [None if nan[i] else float(arr[i]) for i in positions],
            "missingCount": int(nan.sum()),
            "missingMarks": [_label_at(labels, int(i)) for i in marks[:quality.MAX_MISSING_MARKS]],
            # 与异常覆盖层同一套口径：少画既可能是抽稀抽掉的，也可能是撞上每列上限的
            "marksTruncated": bool(all_marks > quality.MAX_MISSING_MARKS or marks.size < all_marks),
            "anomalyCount": 0, "anomalies": [], "anomaliesTruncated": False,
        }
        found = per_col.get(key) if (detection and not stale) else None
        if found is not None:
            indices = np.asarray(found["indices"], dtype="int64")
            drawn = indices[on_axis[indices]] if indices.size else indices
            res["anomalyCount"] = int(indices.size)
            res["anomalies"] = [[_label_at(labels, int(i)), float(arr[int(i)])]
                                for i in drawn[:overlay_budget]]
            # 少画了有两种原因（抽稀抽掉的、超出覆盖层预算的），对界面来说都是「别把图当全部」
            res["anomaliesTruncated"] = bool(drawn.size > len(res["anomalies"]) or drawn.size < indices.size)
        series.append(res)

    return {
        "rowCount": n, "points": len(positions), "maxPoints": cap,
        "decimated": len(positions) < n,
        # 覆盖层的每列预算随响应给出：界面那句「画不全」要报真实数字，不能自己写死
        "overlayCaps": {"missingMarksPerCol": quality.MAX_MISSING_MARKS,
                        "anomalyPointsPerCol": overlay_budget},
        "x": [_label_at(labels, i) for i in positions],
        "idx": list(positions),
        "anomaly": None if not detection else {
            "algo": detection.get("algo"), "at": detection.get("at"), "stale": stale,
        },
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
