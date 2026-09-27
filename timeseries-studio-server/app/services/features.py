"""第五步「特征构建」的服务端实现（numpy/pandas 复刻浏览器里的那套算法）。

这里的每个函数都不是"更合理的算法"，而是**旧 JS 实现的可移植副本**——界面、导出脚本、
回放三条链路必须给出同一张表，所以语义逐条对齐 utils.js / store.js：

- 取整：`parseFloat(x.toFixed(n))` → `round_half_up`（对 |x| 四舍五入，负数不往 0 靠）；
- 窗口类（rolling / expanding / ewm）一律**不含当前行**，等价 pandas 的 .shift(1) 后再开窗；
- rolling 先剔除缺失再聚合（窗口内有几个有效值就按几个算），这与 pandas `rolling(min_periods=w)`
  的 NaN 传播不同，所以这里不能用现成的 rolling；
- std 用总体标准差（÷n），与 utils.js 和 3σ 判定同一口径；
- 傅里叶幅度 |Σ xₙ·e^(-2πikn/N)|/N，主频逐行用**变长窗口**（i<64 时窗口只有 i+1 格）。
"""
from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

# 与 store.js 保持一致的常量
TIME_DIMS = ("hour", "day", "month", "weekday", "is_weekend", "holiday")
CYCLE_DIMS = ("hour", "weekday", "month")
CYCLE_PERIOD = {"hour": 24, "weekday": 7, "month": 12}
CYCLE_CN = {"hour": "小时", "weekday": "星期", "month": "月份"}
ROLL_STATS = {"mean": "mean", "std": "std", "max": "max", "min": "min", "median": "med"}
CAT_METHODS = ("onehot", "ordinal", "target")
# lag/window 与 diff/fft 是各自独立的一次生成；lag_roll、diff_freq 是拆分之前那条命令的族名，
# 老日志重放出来仍是这两个值，界面的特征登记表才认得旧列。
FEATURE_FAMILIES = ("time", "lag", "window", "lag_roll", "diff", "fft", "diff_freq", "cat", "split")

# 中国法定节假日表（示例数据集覆盖 2024-06）。界面可以改，改完的表落在工作区 meta 里，
# 这条内置表只是「从未配置过」时的默认值与预设恢复源。
HOLIDAYS_2024 = frozenset([
    "2024-01-01", "2024-02-10", "2024-02-11", "2024-02-12", "2024-02-13", "2024-02-14",
    "2024-04-04", "2024-04-05", "2024-05-01", "2024-05-02", "2024-05-03", "2024-06-10",
    "2024-09-15", "2024-09-16", "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04",
    "2024-10-05",
])

# 内置预设清单：界面上的「按年份恢复默认」只列这里有的，不做「看起来像节假日」的编造
HOLIDAY_PRESETS: dict[str, frozenset[str]] = {"2024": HOLIDAYS_2024}
DEFAULT_HOLIDAY_YEAR = "2024"   # 工作区从未配置过时的默认表

MAX_HOLIDAY_DAYS = 200         # 与 schemas.MAX_SESSION_INLINE_ARRAY 同一档：生效日期表要整份记进
                               # 审计参数（导出脚本只能从那份复现日期集合），而会话保存会拒收更长的数组


def holiday_view(days) -> dict:
    """把一份日期集合整理成界面与 /holidays 响应用的形状（天数、按年分组、命中数据集的年份）。"""
    ordered = sorted(set(days or []))
    by_year: dict[str, list[str]] = {}
    for d in ordered:
        by_year.setdefault(d[:4], []).append(d)
    return {"days": ordered, "count": len(ordered),
            "byYear": [{"year": y, "days": v} for y, v in sorted(by_year.items())]}


def check_holiday_days(values) -> list[str]:
    """校验并去重节假日日期：只收 YYYY-MM-DD 且真实存在的日期，其余一律报错而不是悄悄丢掉。"""
    if values is None:
        raise ValueError("节假日列表不能为空（要清空请提交空列表）")
    if len(values) > MAX_HOLIDAY_DAYS:
        raise ValueError(f"节假日 {len(values)} 天，超过上限 {MAX_HOLIDAY_DAYS} 天")
    out: list[str] = []
    for raw in values:
        s = str(raw or "").strip()
        if len(s) != 10 or s[4] != "-" or s[7] != "-":
            raise ValueError(f"节假日日期需为 YYYY-MM-DD，收到「{raw}」")
        try:
            date = pd.Timestamp(s)
        except Exception:
            raise ValueError(f"节假日日期无法解析：「{raw}」") from None
        iso = date.strftime("%Y-%m-%d")
        if iso not in out:
            out.append(iso)
    return sorted(out)


# 数据集划分：时序不打乱，前 train% → 中间 val% → 末尾 test%，剩余两条平分。
# 这份公式与前端 splitCounts()、导出脚本三处同源，改一处就要改三处。
SPLIT_COL_KEY = "dataset_split"
SPLIT_COL_LABEL = "数据集划分"
SPLIT_VALUES = ("train", "val", "test")


def split_counts(n: int, ratio: int) -> dict:
    train = int(n) * int(ratio) // 100
    rest = int(n) - train
    test = rest // 2
    return {"train": train, "val": rest - test, "test": test, "total": int(n)}

MAX_WINDOW = 5000
FFT_WINDOW = 64          # 逐行频谱窗口的左跨度：主频含当前行（长 65），熵/能量比不含（长 64）
FFT_MAX_K = 50           # 全谱搜索的最高阶
FFT_ENTROPY_K = 16
FFT_LOW_K = (1, 2, 3, 4)
FFT_HIGH_K = (8, 12, 16)
# 逐行窗口按块算，块高 × 窗口宽 控制在这个格数内，避免大表上一次性撑出几百 MB
_WINDOW_CHUNK_ROWS = 4096
MAX_UNIQUE_VALUES = 200


# ---------------------------------------------------------------- 数值工具

def _exact_half_up(x: float, nd: int) -> float:
    """`parseFloat(x.toFixed(nd))` 的标量精确版：floor(|x|·10^nd + 1/2) 全程走整数。

    as_integer_ratio 给的是这个 double 的**精确**值，所以不会像 `abs(x)*100` 那样
    先把 446.49999999999998578915 抬成 446.5、再四舍五入成 4.47。
    """
    if x != x:
        return float("nan")
    scale = 10 ** nd
    n, d = abs(float(x)).as_integer_ratio()
    k = (2 * scale * n + d) // (2 * d)
    v = k / scale
    return -v if x < 0 and v != 0 else v


def round_half_up(values, nd: int) -> np.ndarray:
    """`parseFloat(x.toFixed(nd))` 的向量化等价：按 |x| 四舍五入，NaN 原样保留。

    为什么不用 Python 的 round()：那是银行家舍入（round(0.125, 2) == 0.12），
    而 JS 的 toFixed 先取绝对值再补负号，.5 永远往上走（(-0.125).toFixed(2) == "-0.13"）。

    也不能直接 `floor(abs(x)*100 + 0.5)` 一把梭：浮点乘法本身会舍入，实测
    60 万个真实量级的格子里有 3.1 万个被抬过 .5 边界（4.465 那类），差的就是一个百分位。
    所以向量化算完之后，只对落在 .5 附近的那几个格子用整数精确重算。
    """
    arr = np.asarray(values, dtype="float64")
    scale = float(10 ** nd)
    ay = np.abs(arr) * scale
    base = np.sign(arr) * np.floor(ay + 0.5) / scale
    base = np.where(np.isnan(arr), np.nan, base) + 0.0     # -0.0 → 0.0，否则导出会写成 "-0"
    finite = np.isfinite(ay)
    # np.spacing(NaN) 会刷 RuntimeWarning，先替成 1.0 再取间距（NaN 格子随后被 finite 掩掉）
    step = np.spacing(np.where(finite, np.maximum(ay, 1.0), 1.0))
    near = finite & (np.abs((ay - np.floor(ay)) - 0.5) <= 8.0 * step)
    if near.any():
        flat = arr.ravel()
        idx = np.flatnonzero(near.ravel())
        base.ravel()[idx] = np.array([_exact_half_up(float(flat[i]), nd) for i in idx], dtype="float64")
    return base


def scalar_round(value, nd: int) -> float:
    return float(round_half_up(np.array([value], dtype="float64"), nd)[0])


def numeric_column(frame: pd.DataFrame, key: str) -> np.ndarray:
    """列 → float64；非数值单元格转 NaN（对应 JS 的 filter(!isMissing).map(Number)）。"""
    return pd.to_numeric(frame[key], errors="coerce").to_numpy(dtype="float64", copy=True)


def seq_sum(block: np.ndarray) -> np.ndarray:
    """沿窗口维**按行顺序**累加，等价 JS 的 `reduce((a, b) => a + b, 0)`。

    numpy 的 sum / `@` 走成对求和（BLAS 还会分块），浮点下与顺序累加不等价；
    一个 .005 附近的平均值就能让 round(2) 翻一个百分位，界面和导出会各说一遍数字。
    缺失格预先填 0.0 不改变结果（x + 0.0 精确等于 x），所以窗口宽度不用随缺失变动。
    """
    acc = np.zeros(block.shape[0], dtype="float64")
    for j in range(block.shape[1]):
        acc += block[:, j]
    return acc


def cat_value_str(v) -> str:
    """类别取值 → 参与列名/计数键的字符串，按 JS 的 String(v) 口径：整数值不保留 .0。"""
    if isinstance(v, (float, np.floating)):
        f = float(v)
        if math.isnan(f):
            return "NaN"
        if f.is_integer():
            return str(int(f))
        return str(f)
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    return str(v)


def _is_blank(v) -> bool:
    """JS 的 isMissing：null/undefined/''/NaN 都算缺失（0 与 False 不算）。"""
    if v is None or v is pd.NaT:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return isinstance(v, str) and v == ""


# ---------------------------------------------------------------- 计划（列名 + 标签）

def time_plan(dims: list[str], cyc_dims: list[str], keep_original: bool = True) -> list[tuple[str, str]]:
    """keep_original=False 时，被正余弦编码的维度不再另出一份数值列（sin/cos 就是它的替代）。"""
    skip = set() if keep_original else {d for d in cyc_dims if d in CYCLE_DIMS}
    items = [(f"feat_{d}", f"时间:{d}") for d in dims if d not in skip]
    for d in cyc_dims:
        items.append((f"feat_{d}_sin", f"时间:{d}_sin"))
        items.append((f"feat_{d}_cos", f"时间:{d}_cos"))
    return items


def normalize_span(span) -> int:
    try:
        n = int(round(float(span)))
    except (TypeError, ValueError):
        return 12
    return min(n, 500) if n >= 2 else 12


def lag_plan(cols: list[str], lags: list[int], windows: list[int], stats: list[str],
             expanding: bool, ewm: bool, span: int) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    def add(key: str):
        if key not in seen:
            seen.add(key)
            out.append((key, key))

    for col in cols:
        for s in lags:
            add(f"lag_{col}_t{s}")
        for w in windows:
            for fn in stats:
                short = ROLL_STATS.get(fn)
                if short:
                    add(f"roll_{short}_{col}_w{w}")
        if expanding:
            add(f"expanding_mean_{col}")
        if ewm:
            add(f"ewm_{col}_s{span}")
    return out


def diff_label(key: str) -> str:
    if key.startswith("diff_season"):
        p, _, col = key[len("diff_season"):].partition("_")
        return f"ΔS{p}_{col}"
    if key.startswith("diff1_"):
        return f"Δ¹_{key[len('diff1_'):]}"
    if key.startswith("diff2_"):
        return f"Δ²_{key[len('diff2_'):]}"
    if key.startswith("fft_entropy_"):
        return f"spectral_entropy_{key[len('fft_entropy_'):]}"
    if key.startswith("fft_power_ratio_"):
        return f"power_ratio_{key[len('fft_power_ratio_'):]}"
    if key.startswith("fft_top"):
        rank, _, col = key[len("fft_top"):].partition("_")
        return f"FFT_Top{rank}_{col}"
    return key


def diff_plan(cols: list[str], d1: bool, d2: bool, seasonal: bool, period: int,
              dominant: bool, entropy: bool, power_ratio: bool) -> list[tuple[str, str]]:
    keys: list[str] = []
    for col in cols:
        if d1:
            keys.append(f"diff1_{col}")
        if d2:
            keys.append(f"diff2_{col}")
        if seasonal:
            keys.append(f"diff_season{period}_{col}")
    head = cols[0] if cols else None
    if head:
        if dominant:
            keys += [f"fft_top1_{head}", f"fft_top2_{head}", f"fft_top3_{head}"]
        if entropy:
            keys.append(f"fft_entropy_{head}")
        if power_ratio:
            keys.append(f"fft_power_ratio_{head}")
    seen: set[str] = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append((k, diff_label(k)))
    return out


def cat_plan(cols: list[str], method: str, uniques: dict[str, list]) -> list[tuple[str, str]]:
    """uniques[cat] 必须是"按首次出现排序"的去重取值：独热列名与序数编号都取自这个顺序。"""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for cat in cols:
        if method == "onehot":
            items = [(f"{cat}_{cat_value_str(v)}", f"{cat}={cat_value_str(v)}") for v in uniques[cat]]
        elif method == "ordinal":
            items = [(f"{cat}_ordinal", f"{cat}_序数")]
        else:
            items = [(f"{cat}_target", f"{cat}_目标编码")]
        for k, label in items:
            if k not in seen:
                seen.add(k)
                out.append((k, label))
    return out


# ---------------------------------------------------------------- 时间与日历

def build_time(ts: pd.Series, dims: list[str], cyc_dims: list[str],
               keep_original: bool = True, holidays=frozenset(HOLIDAYS_2024)) -> dict[str, np.ndarray]:
    """日历维度 + 可选的正余弦编码。ts 必须是 datetime64（工作区载入时已转好）。

    holidays 是**当前工作区生效的那份**节假日表（GET /holidays 与 feature_time 同源），
    不再是模块里的常量——用户改过日历之后，特征列必须跟着他配的那份走。
    """
    out: dict[str, np.ndarray] = {}
    hour = pd.to_numeric(ts.dt.hour, errors="coerce").to_numpy(dtype="float64")
    day = pd.to_numeric(ts.dt.day, errors="coerce").to_numpy(dtype="float64")
    month = pd.to_numeric(ts.dt.month, errors="coerce").to_numpy(dtype="float64")
    # pandas 的 dayofweek 周一=0，与 JS 的 (getDay()+6)%7 同一套编号
    weekday = pd.to_numeric(ts.dt.dayofweek, errors="coerce").to_numpy(dtype="float64")
    skip = set() if keep_original else {d for d in cyc_dims if d in CYCLE_DIMS}
    if "hour" in dims and "hour" not in skip:
        out["feat_hour"] = hour
    if "day" in dims and "day" not in skip:
        out["feat_day"] = day
    if "month" in dims and "month" not in skip:
        out["feat_month"] = month
    if "weekday" in dims and "weekday" not in skip:
        out["feat_weekday"] = weekday
    if "is_weekend" in dims:
        wknd = np.where(np.isnan(weekday), np.nan, (weekday >= 5).astype("float64"))
        out["feat_is_weekend"] = wknd
    if "holiday" in dims:
        days = set(holidays)
        hit = ts.dt.strftime("%Y-%m-%d").isin(days).to_numpy(dtype="float64")
        out["feat_holiday"] = np.where(ts.isna().to_numpy(), np.nan, hit)

    # 正余弦只对周期长度固定的维度有意义：day 每月 28~31 天，无固定周期，故不参与。
    # month 的角度用 0 基（JS 的 getMonth()），而 feat_month 那一列用 1 基，两者本来就是两套数。
    for d in cyc_dims:
        raw = {"hour": hour, "weekday": weekday, "month": month - 1.0}[d]
        angle = (raw / CYCLE_PERIOD[d]) * 2.0 * math.pi
        with np.errstate(invalid="ignore"):
            out[f"feat_{d}_sin"] = round_half_up(np.sin(angle), 3)
            out[f"feat_{d}_cos"] = round_half_up(np.cos(angle), 3)
    return out


# ---------------------------------------------------------------- 滞后与窗口

def _row_windows(vals: np.ndarray, w: int, blocks) -> np.ndarray:
    """把「每块若干行、每行一个长度 w 的前序窗口」摊平成 (行数, w) 的矩阵。

    行 i 的窗口是 vals[i-w : i]。左侧先补 w-1 个 NaN，这样 view[i-1] 正好是那一格，
    行号偏移一位换来整条链路无需再判越界；不足 w 行的那几行本来就是全 NaN。
    """
    pad = np.concatenate([np.full(w - 1, np.nan), vals])
    view = sliding_window_view(pad, w)
    return view[np.asarray(blocks, dtype="int64") - 1]


def rolling_agg(vals: np.ndarray, w: int, fn: str) -> np.ndarray:
    """剔除缺失后再聚合；有效值个数为 0 → NaN（与浏览器端 vals.length===0 → null 同）。"""
    n = vals.size
    out = np.full(n, np.nan)
    if w < 1 or n <= w:
        return out
    for a in range(w, n, _WINDOW_CHUNK_ROWS):
        b = min(n, a + _WINDOW_CHUNK_ROWS)
        rows = np.arange(a, b)
        block = _row_windows(vals, w, rows)
        mask = ~np.isnan(block)
        cnt = mask.sum(axis=1)
        got = cnt > 0
        if not got.any():
            continue
        if fn in ("max", "min"):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                agg = np.nanmax(block, axis=1) if fn == "max" else np.nanmin(block, axis=1)
            out[rows[got]] = agg[got]
            continue
        filled = np.where(mask, block, 0.0)
        means = seq_sum(filled) / cnt
        if fn == "mean":
            agg = round_half_up(means, 2)
        elif fn == "std":
            dev = np.where(mask, block - means[:, None], 0.0)
            agg = round_half_up(np.sqrt(seq_sum(dev * dev) / cnt), 2)   # 总体标准差 ÷n
        elif fn == "median":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                agg = round_half_up(np.nanmedian(block, axis=1), 2)
        else:
            raise ValueError(f"不支持的滚动统计量：{fn}")
        out[rows[got]] = agg[got]
    return out


def expanding_mean(vals: np.ndarray) -> np.ndarray:
    """到上一行为止的全部有效值均值（不含当前行）。cumsum 与 JS 的顺序累加同序。"""
    n = vals.size
    out = np.full(n, np.nan)
    if n <= 1:
        return out
    valid = ~np.isnan(vals)
    cs = np.cumsum(np.where(valid, vals, 0.0))
    cc = np.cumsum(valid.astype("int64"))
    prev_sum, prev_cnt = cs[:-1], cc[:-1]
    got = prev_cnt > 0
    tail = np.full(n - 1, np.nan)
    tail[got] = prev_sum[got] / prev_cnt[got]
    out[1:] = round_half_up(tail, 2)
    return out


def ewm_prev(vals: np.ndarray, span: int) -> np.ndarray:
    """α=2/(span+1) 的递推指数加权，输出**上一行**的状态；缺失不衰减已有状态（等价 ignore_na=True）。

    用逐行循环而不是 .ewm()：JS 的 `alpha*v + (1-alpha)*prev` 是逐字浮点表达式，
    交给 pandas 会换成另一套求和顺序，round(2) 之前那 1e-16 级的差偶尔会翻一个百分位。
    """
    alpha = 2.0 / (span + 1)
    one_minus = 1.0 - alpha
    out = np.full(vals.size, np.nan)
    prev = None
    for i in range(vals.size):
        v = vals[i]
        if prev is not None:
            out[i] = prev
        if math.isnan(v):
            continue
        prev = float(v) if prev is None else alpha * float(v) + one_minus * prev
    return round_half_up(out, 2)


def lag_column(series: pd.Series, step: int) -> pd.Series:
    """原值平移，不做数值转换：JS 就是把 data[i-step][col] 直接搬过来（前 step 行为 null）。"""
    return series.shift(step)


# ---------------------------------------------------------------- 差分与频域

def _angles(ks, t, length):
    """e^(-2πikn/N) 的角度，按 JS 的运算次序 ((-2·π)·k)·n 再除以 N，一次只差一个 ULP 也算差。"""
    k = np.asarray(ks, dtype="float64").reshape(-1, 1)
    n = np.asarray(t, dtype="float64").reshape(1, -1)
    return ((-2.0 * math.pi) * k) * n / float(length)


def _dft_mags(vals: np.ndarray, ks) -> np.ndarray:
    """一串 k 的幅度 |Σ xₙ·e^(-2πikn/N)|/N，求和按 n 递增顺序累加（同 JS 的 for 循环）。"""
    n = int(vals.size)
    ks = list(ks)
    if n == 0 or not ks:
        return np.zeros(len(ks))
    angle = _angles(ks, range(n), n)
    c, s = np.cos(angle), np.sin(angle)
    re = np.zeros(len(ks))
    im = np.zeros(len(ks))
    for j in range(n):
        re += vals[j] * c[:, j]
        im += vals[j] * s[:, j]
    return np.sqrt(re * re + im * im) / n


def _row_dft_mags(vals: np.ndarray, ks, span: int, include_current: bool):
    """逐行窗口 DFT，返回 {k: 每行幅度数组}。

    include_current=True  → 行 i 用 vals[max(0, i-span) : i+1]（主频：i<span 时窗口随 i 增长）
    include_current=False → 行 i 用 vals[i-span : i]，且 i<span 时为 NaN（熵/能量比的 null 分支）
    """
    n = vals.size
    ks = list(ks)
    full = span + (1 if include_current else 0)
    out = {k: np.full(n, np.nan) for k in ks}
    if not ks or n == 0:
        return out
    # 左端不足定长窗口的行（只有主频会走到）：逐行算，最多 span 次
    for i in range(min(span, n)):
        if not include_current:
            break
        mags = _dft_mags(vals[:i + 1], ks)
        for k, m in zip(ks, mags):
            out[k][i] = m
    tail = sliding_window_view(vals, full)
    angle = _angles(ks, range(full), full)
    c, s = np.cos(angle), np.sin(angle)
    for a in range(span, n, _WINDOW_CHUNK_ROWS):
        b = min(n, a + _WINDOW_CHUNK_ROWS)
        rows = np.arange(a, b)
        block = tail[rows - span]                    # 每行一个定长窗口
        re = np.zeros((rows.size, len(ks)))
        im = np.zeros((rows.size, len(ks)))
        for j in range(full):
            col = block[:, j][:, None]
            re += col * c.T[j]
            im += col * s.T[j]
        mags = np.sqrt(re * re + im * im) / full
        for ki, k in enumerate(ks):
            out[k][rows] = mags[:, ki]
    return out


def dominant_columns(vals: np.ndarray, col: str) -> tuple[dict[str, np.ndarray], list[tuple[str, str]]]:
    """全谱取前 3 主频（k=1..min(50, n//2)，能量降序、同值保持 k 升序 = JS 的稳定排序），
    再逐行输出该 k 的幅度；标签里带上全谱能量，界面显示的 FFT_Top1_E12.3 就是这么来的。"""
    n = int(vals.size)
    ks = list(range(1, min(FFT_MAX_K, n // 2) + 1))
    if not ks:
        return {}, []
    energies = _dft_mags(vals, ks)
    ranked = sorted(zip(ks, energies.tolist()), key=lambda t: -t[1])[:3]
    per_k = _row_dft_mags(vals, [k for k, _ in ranked], FFT_WINDOW, True)
    out: dict[str, np.ndarray] = {}
    items: list[tuple[str, str]] = []
    for rank, (k, energy) in enumerate(ranked, start=1):
        key = f"fft_top{rank}_{col}"
        out[key] = round_half_up(per_k[k], 2)
        items.append((key, f"FFT_Top{rank}_E{scalar_round(energy, 1):.1f}"))
    return out, items


def spectral_columns(vals: np.ndarray, col: str) -> tuple[dict[str, np.ndarray], list[tuple[str, str]]]:
    """谱熵与高低频能量比：窗口 vals[i-64 : i] 共 64 格、不含当前行，i<64 时为 null。

    熵的分母是**幅度之和**而不是功率之和（JS 就这么算的，虽然不标准），照抄不改。
    """
    per_k = _row_dft_mags(vals, list(range(1, FFT_ENTROPY_K + 1)), FFT_WINDOW, False)
    mat = np.column_stack([per_k[k] for k in range(1, FFT_ENTROPY_K + 1)])
    ok = ~np.isnan(mat[:, 0])
    total = seq_sum(np.where(np.isnan(mat), 0.0, mat))
    denom = np.where(total > 0, total, 1.0)          # JS: energies.reduce(...) || 1
    p = mat / denom[:, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        log_term = np.log2(np.where(p > 0, p, 1.0))
    terms = np.where(p > 0, -p * log_term, 0.0)
    entropy = np.where(ok, seq_sum(terms), np.nan)
    low = sum(per_k[k] for k in FFT_LOW_K)
    high = sum(per_k[k] for k in FFT_HIGH_K)
    ratio = np.where(ok, low / (high + 1e-10), np.nan)
    return ({f"fft_entropy_{col}": round_half_up(entropy, 4),
             f"fft_power_ratio_{col}": round_half_up(ratio, 2)},
            [(f"fft_entropy_{col}", f"spectral_entropy_{col}"),
             (f"fft_power_ratio_{col}", f"power_ratio_{col}")])


# ---------------------------------------------------------------- 类别编码

def unique_in_order(series: pd.Series) -> list:
    """非缺失取值，按首次出现排序（JS 用 Set 保序，Object.keys 同样保序）。"""
    seen: set = set()
    out: list = []
    for v in series.to_numpy(dtype=object, copy=False):
        if _is_blank(v):
            continue
        item = v.item() if isinstance(v, np.generic) else v
        probe = int(item) if isinstance(item, float) and item.is_integer() else item
        if probe in seen:
            continue
        seen.add(probe)
        out.append(item)
    return out


def build_cat(frame: pd.DataFrame, cols: list[str], method: str,
              target_col: str | None) -> tuple[dict[str, np.ndarray], list[tuple[str, str]]]:
    out: dict[str, np.ndarray] = {}
    items: list[tuple[str, str]] = []
    for cat in cols:
        s = frame[cat]
        uniq = unique_in_order(s)
        if method == "onehot":
            for v in uniq:
                key = f"{cat}_{cat_value_str(v)}"
                if key in out:      # 两个取值撞出同一个列名（如字符串 "3" 与数字 3）时保留先出现的那个
                    continue
                out[key] = (s == v).astype("int64").to_numpy()
                items.append((key, f"{cat}={cat_value_str(v)}"))
        elif method == "ordinal":
            order = {}
            for i, v in enumerate(uniq):
                order.setdefault(cat_value_str(v), i)
            keys = [("" if _is_blank(v) else cat_value_str(v)) for v in s.to_numpy(dtype=object, copy=False)]
            codes = np.array([order.get(k, -1) for k in keys], dtype="int64")
            out[f"{cat}_ordinal"] = codes
            items.append((f"{cat}_ordinal", f"{cat}_序数"))
        else:
            if not target_col:
                raise ValueError(f"目标均值编码需要指定数值目标列（类别列 {cat} 无法自行推断）")
            tvals = pd.to_numeric(frame[target_col], errors="coerce").to_numpy(dtype="float64")
            gkeys = [("" if _is_blank(v) else cat_value_str(v)) for v in s.to_numpy(dtype=object, copy=False)]
            sums: dict[str, float] = {}
            counts: dict[str, int] = {}
            for g, v in zip(gkeys, tvals):
                if g == "" or math.isnan(v):
                    continue
                sums[g] = sums.get(g, 0.0) + v
                counts[g] = counts.get(g, 0) + 1
            means = {g: scalar_round(sums[g] / counts[g], 2) for g in counts}
            out[f"{cat}_target"] = np.array([means.get(g, np.nan) for g in gkeys], dtype="float64")
            items.append((f"{cat}_target", f"{cat}_目标编码"))
    return out, items


# ---------------------------------------------------------------- 参数校验

def check_time_params(dims, cyclical, cyc_dims=None) -> tuple[list[str], list[str]]:
    """勾选校验。

    cyc_dims 是现在的按维度选择（['hour','month']）；cyclical=True 是拆分前的旧命令，
    含义是「对全部已勾选的周期维度编码」。两者同时给时以 cyc_dims 为准。
    """
    chosen = [d for d in (dims or []) if d in TIME_DIMS]
    if not chosen:
        raise ValueError("没有勾选任何日历维度")
    if cyc_dims:
        bad = [d for d in cyc_dims if d not in CYCLE_DIMS]
        if bad:
            raise ValueError(f"只有周期固定的维度能做正余弦编码（{'/'.join(CYCLE_DIMS)}），收到 {'/'.join(bad)}")
        cyc = [d for d in CYCLE_DIMS if d in cyc_dims and d in chosen]
    elif cyclical:
        cyc = [d for d in CYCLE_DIMS if d in chosen]
    else:
        cyc = []
    return chosen, cyc


def check_int_list(name: str, values, upper: int = MAX_WINDOW) -> list[int]:
    out: list[int] = []
    for v in values or []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 含非整数：{v}") from None
        if n < 1 or n > upper:
            raise ValueError(f"{name} 需在 1~{upper} 之间，收到 {n}")
        if n not in out:
            out.append(n)
    return out


def check_period(value) -> int:
    try:
        n = max(1, int(round(float(value or 1))))
    except (TypeError, ValueError):
        raise ValueError(f"季节差分周期不合法：{value}") from None
    if n > MAX_WINDOW:
        raise ValueError(f"季节差分周期需在 1~{MAX_WINDOW} 之间，收到 {n}")
    return n


def value_counts(frame: pd.DataFrame, keys: list[str], labels: dict[str, str]) -> dict:
    """类别列的取值分布：给第五步面板上的「预计新增 N 列 / 取值占比」用，整表计数在服务端做。

    高基数列（一行一个 ID 那种）会把响应直接撑爆，所以只回前 MAX_UNIQUE_VALUES 个取值，
    剩下的合并进 restCount —— 面板顶多画 4 根分布条，但「共计多少个取值」必须仍是真数。
    """
    columns = [str(c) for c in frame.columns]
    out = []
    for key in keys:
        if key not in columns:
            raise ValueError(f"列不存在：{key}")
        uniq = unique_in_order(frame[key])
        counts: dict[str, int] = {}
        for v in frame[key].to_numpy(dtype=object, copy=False):
            if _is_blank(v):
                continue
            s = cat_value_str(v)
            counts[s] = counts.get(s, 0) + 1
        shown = [cat_value_str(v) for v in uniq[:MAX_UNIQUE_VALUES]]
        shown_rows = sum(counts.get(s, 0) for s in shown)
        out.append({
            "key": key,
            "label": labels.get(key, key),
            "uniqueVals": shown,
            "counts": {s: counts.get(s, 0) for s in shown},
            "total": int(frame.shape[0]),
            "uniqueTotal": len(uniq),
            "nonMissingRows": sum(counts.values()),
            "restCount": sum(counts.values()) - shown_rows,
            "truncated": len(uniq) > MAX_UNIQUE_VALUES,
        })
    return {"rowCount": int(frame.shape[0]), "columns": out}
