"""第四步（质量诊断与清洗）的唯一实现：缺失段、填补、重复时间戳合并、异常检测与修复、布尔掩码。

这些算法原先跑在浏览器里（第①期靠 /columns 把整表拉进内存再算），本模块把它搬成
服务端一份实现，界面读到的每个数字都从这里出。

数值语义逐条对齐旧的浏览器版本，其中以下几处是历史约定、不是笔误：
- 线性填补在缺失段左侧没有观测值时以 0 起算（不外推、也不保持 NaN）；
- 分位数取 sorted[int(n·q)]（最近秩下取整），不是 pandas 默认的线性插值，n=8 时与
  `Series.quantile(0.25)` 差一个位次；
- 标准差用总体标准差（÷n），而 pandas 默认样本标准差（÷(n−1)）；
- 「孤立森林（近似）」实为回看 33 个观测的均值绝对偏差（MAD）判据，与 sklearn 版不同算法。
改动这些约定会让同一份数据在迁移前后给出两套数字，所以原样保留并在此标注。

唯一一处刻意不一致：重复时间戳按解析后的时间戳分组（旧浏览器版按显示字符串分组，
详见 merge_duplicates 的说明），否则本页的重复行数和 /overview 会给出两个数字。
"""
from __future__ import annotations

import ast
import math
import re
import warnings
from decimal import Decimal, ROUND_HALF_UP

import numpy as np
import pandas as pd

IMPUTE_ALGOS = ("linear", "ffill", "spline", "zero")
DUP_STRATEGIES = ("mean", "first", "last")
ANOMALY_ALGOS = ("3sigma", "iqr", "iforest", "iforest_sklearn", "expr")
REPAIR_MODES = ("clip", "nan_impute", "mask_only")

# 缺失段一次最多回给界面这么多条：整列都是稀疏缺失时段的数量会到上万，
# 列表放不下也没法逐段选算法，超出部分在响应里标 truncated。
MAX_SEGMENTS_PER_COLUMN = 300
# 一次填补执行后回给界面的逐段明细条数上限（全量段数在 segmentCount 里）
MAX_IMPUTE_DETAIL = 200
# 「孤立森林（近似）」的回看窗口：含当前点共 33 个观测，至少要 5 个才判定
MAD_WINDOW = 33
MAD_MIN_PERIODS = 5
MAD_K = 4.0
MAX_ENVELOPE_POINTS = 6000
# 散点覆盖层一次最多回这么多点：整列都是异常时（比如表达式写错）不该把几 MB 索引推给浏览器
MAX_ANOMALY_POINTS = 4000
MAX_MISSING_MARKS = 80


# ---------------------------------------------------------------- 数值工具

def round4(x):
    """等价于 JS 的 parseFloat(v.toFixed(4))：按二进制精确值远离 0 进位。

    Python 内置 round 是银行家舍入，且必须先转成 Decimal 才看得到真实二进制值：
    Decimal(2.6749999...) → 2.675 的下一位是 4，与 JS 同一结果。
    """
    if x is None:
        return None
    v = float(x)
    if math.isnan(v) or math.isinf(v):
        return v
    return float(Decimal(v).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def numeric_array(frame: pd.DataFrame, key: str) -> np.ndarray:
    """列 → float64 数组（非数值单元格转成 NaN）。调用方须先确认该列在 meta 里是 float。"""
    return pd.to_numeric(frame[key], errors="coerce").to_numpy(dtype="float64", copy=True)


def missing_runs(arr: np.ndarray) -> list[tuple[int, int]]:
    """连续缺失的闭区间列表 [(start, end)]，按行位置（与浏览器版 detectMissingSegments 同）。"""
    na = np.isnan(arr)
    if not na.any():
        return []
    changes = np.flatnonzero(na[1:] != na[:-1]) + 1
    bounds = np.concatenate(([0], changes, [arr.size]))
    return [(int(a), int(b - 1)) for a, b in zip(bounds[:-1], bounds[1:]) if na[a]]


def col_stats(arr: np.ndarray) -> dict:
    """一个数值列的统计量；n=0 时各项给 None，由界面显示成「—」而不是伪造 0。"""
    total = int(arr.size)
    valid = arr[~np.isnan(arr)]
    n = int(valid.size)
    if n == 0:
        return {"n": 0, "missing": total, "mean": None, "std": None, "median": None,
                "q1": None, "q3": None, "min": None, "max": None}
    s = np.sort(valid)
    mean = float(s.mean())
    std = float(math.sqrt(float(np.sum((s - mean) ** 2)) / n))
    return {
        "n": n, "missing": total - n, "mean": mean, "std": std,
        "median": float(np.median(s)),
        "q1": float(s[int(n * 0.25)]), "q3": float(s[int(n * 0.75)]),
        "min": float(s[0]), "max": float(s[-1]),
    }


# ---------------------------------------------------------------- 缺失填补

def fill_run(arr: np.ndarray, start: int, end: int, algo: str) -> int:
    """在 arr 上就地填补 [start, end] 区间内的缺失点，返回填补个数。

    段边界上的"最近观测值"从区间外侧查找，因此给定区间内只填 NaN、不动观测值；
    区间内没有缺失时直接返回 0（调用方据此判断"数据已变化"）。
    """
    if algo not in IMPUTE_ALGOS:
        raise ValueError(f"不支持的填补算法：{algo}（可选 {'/'.join(IMPUTE_ALGOS)}）")
    n = arr.size
    start = max(0, int(start))
    end = min(n - 1, int(end))
    if start > end:
        raise ValueError(f"缺失段区间越界：{start}–{end}（共 {n} 行）")
    targets = np.flatnonzero(np.isnan(arr[start:end + 1])) + start
    if targets.size == 0:
        return 0
    valid = ~np.isnan(arr)
    left = np.flatnonzero(valid[:start])
    right = np.flatnonzero(valid[end + 1:])
    before = int(left[-1]) if left.size else None
    after = int(right[0]) + end + 1 if right.size else None
    v_before = float(arr[before]) if before is not None else 0.0
    v_after = float(arr[after]) if after is not None else v_before

    if algo == "zero":
        arr[targets] = 0.0
        return int(targets.size)
    if algo == "ffill":
        arr[targets] = v_before if before is not None else 0.0
        return int(targets.size)
    if algo == "spline":
        length = end - start + 1
        for i in targets:
            t = (int(i) - start + 1) / (length + 1)
            h = -2 * t ** 3 + 3 * t * t
            arr[int(i)] = round4(v_before + (v_after - v_before) * h)
        return int(targets.size)

    left_edge = before if before is not None else start - 1
    right_edge = after if after is not None else end + 1
    span = right_edge - left_edge
    for i in targets:
        pos = int(i) - before if before is not None else int(i) - start + 1
        ratio = pos / span if span > 1 else 0.5
        arr[int(i)] = round4(v_before + (v_after - v_before) * ratio)
    return int(targets.size)


def apply_impute(arr: np.ndarray, targets: list[dict]) -> tuple[np.ndarray, int, list[dict]]:
    """按 [{startIdx,endIdx,algo}] 逐段填补，返回 (新数组, 填补总数, 每段实际填了几个)。"""
    out = np.asarray(arr, dtype="float64").copy()
    filled = 0
    detail = []
    for t in targets:
        n = fill_run(out, int(t["startIdx"]), int(t["endIdx"]), t.get("algo") or "linear")
        filled += n
        detail.append({"startIdx": int(t["startIdx"]), "endIdx": int(t["endIdx"]),
                       "algo": t.get("algo") or "linear", "filled": n})
    return out, filled, detail


def merge_duplicates(frame: pd.DataFrame, time_col: str, keys: list[str],
                     strategy: str) -> tuple[pd.DataFrame, int, int]:
    """重复时间戳合并，返回 (新表, 删除行数, 涉及重复的组数)。

    分组按"解析后的时间戳"，与 /overview 的重复行口径保持一致：旧浏览器版按渲染出来的
    时间字符串分组，显示格式藏掉秒时会把 10:00:01 与 10:00:07 并进同一组。
    无法解析的时间（NaT）之间互相算重复，行为与浏览器版一致。
    行序保持原样（浏览器版就是按出现顺序分组、均值写回首行）。
    """
    if strategy not in DUP_STRATEGIES:
        raise ValueError(f"不支持的去重策略：{strategy}")
    if time_col not in frame.columns:
        raise ValueError(f"没有可用的时间列：{time_col}")
    ts = pd.to_datetime(frame[time_col], errors="coerce")
    dup = ts.duplicated()
    if not dup.any():
        return frame, 0, 0
    groups = int((ts.value_counts(dropna=False) > 1).sum())
    first = ~dup
    if strategy == "mean" and keys:
        out = frame.copy()
        num = pd.DataFrame({k: pd.to_numeric(out[k], errors="coerce").astype("float64") for k in keys})
        means = np.asarray(num.groupby(ts.to_numpy(), sort=False, dropna=False).mean(), dtype="float64")
        positions = np.flatnonzero(first.to_numpy())
        for row, pos in enumerate(positions):
            for col, key in enumerate(keys):
                v = means[row][col]
                if math.isnan(v):
                    continue        # 整组皆缺失：保留原值（浏览器版同样不写）
                out.iat[int(pos), out.columns.get_loc(key)] = round4(v)
        out = out.loc[first]
    else:
        keep = "first" if strategy == "first" else "last"
        out = frame.loc[~ts.duplicated(keep=keep)]
    removed = int(frame.shape[0] - out.shape[0])
    return out.reset_index(drop=True), removed, groups


# ---------------------------------------------------------------- 表达式求值

_EXPR_CHARS = re.compile(r"^[0-9a-zA-Z_+\-*/().,<>=!&|\s%]+$")
_EXPR_NAMES = ("v", "mean", "std", "median", "q1", "q3", "min", "max")
_EXPR_FUNCS = {
    "abs": np.abs, "sqrt": np.sqrt, "log": np.log, "log2": np.log2, "log10": np.log10,
    "exp": np.exp, "sin": np.sin, "cos": np.cos, "tan": np.tan,
    "floor": np.floor, "ceil": np.ceil, "pow": np.power, "hypot": np.hypot,
}
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare, ast.Name, ast.Load,
    ast.Constant, ast.Call, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Invert,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
)


class _BoolToBit(ast.NodeTransformer):
    """把 and/or/not 换成逐元素运算：numpy 数组上 `a and b` 会抛歧义真值错误。

    先按 JS 语义解析出正确的结合性，再改节点，因此不需要在文本里补括号。
    """
    def visit_BoolOp(self, node):
        self.generic_visit(node)
        op = ast.BitAnd() if isinstance(node.op, ast.And) else ast.BitOr()
        value = node.values[0]
        for rhs in node.values[1:]:
            value = ast.BinOp(left=value, op=op, right=rhs)
        return value

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        # 布尔数组上 ~ 就是逐元素取非（not 会先要求歧义真值）
        if isinstance(node.op, ast.Not):
            return ast.UnaryOp(op=ast.Invert(), operand=node.operand)
        return node


def compile_expr(src: str):
    """把界面的异常判定表达式编成可对 numpy 数组求值的函数；不合法一律抛 ValueError。

    变量 v 是当前值，mean/std/median/q1/q3/min/max 是该列统计量。
    """
    text = (src or "").strip()
    if not text:
        raise ValueError("请输入异常判定表达式")
    if not _EXPR_CHARS.match(text):
        raise ValueError("表达式包含不支持的字符")
    py = text.replace("||", " or ").replace("&&", " and ")
    py = re.sub(r"!(?!=)", " not ", py)
    try:
        tree = ast.parse(py, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"表达式无法解析：{exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in _EXPR_NAMES and node.id not in _EXPR_FUNCS:
            raise ValueError(f"表达式含未知变量：{node.id}（可用 {'/'.join(_EXPR_NAMES)}）")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in _EXPR_FUNCS):
            raise ValueError("表达式只允许调用 abs/sqrt/log/log2/log10/exp/sin/cos/tan/floor/ceil/pow/hypot")
        if isinstance(node, ast.Compare) and len(node.ops) > 1:
            raise ValueError("不支持连续比较（如 1 < v < 3），请改用 and 连接")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            raise ValueError("表达式不支持字符串")
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"表达式含不支持的语法：{type(node).__name__}")
    tree = _BoolToBit().visit(tree)
    ast.fix_missing_locations(tree)
    code = compile(tree, "<anomaly-expr>", "eval")

    def evaluate(v, stats):
        env = {**stats, "v": v, **_EXPR_FUNCS}
        out = eval(code, {"__builtins__": {}}, env)  # noqa: S307 - AST 已按白名单校验
        arr = np.asarray(out)
        if arr.dtype != bool:
            arr = arr.astype(bool)
        return np.broadcast_to(arr, np.shape(v) if np.ndim(v) else arr.shape)

    return evaluate


# ---------------------------------------------------------------- 异常检测

def _threshold_mask(arr, lower, upper):
    finite = ~np.isnan(arr)
    mask = finite & ((arr < lower) | (arr > upper))
    return mask, float(lower), float(upper)


def _mad_mask(arr: np.ndarray):
    """回看窗口的 MAD 判据（浏览器近似版的向量化）。"""
    n = arr.size
    pad = np.concatenate((np.full(MAD_WINDOW - 1, np.nan), arr))
    windows = np.lib.stride_tricks.sliding_window_view(pad, MAD_WINDOW)[:n]
    counts = np.sum(~np.isnan(windows), axis=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # 整窗皆缺失时 nanmedian 会提醒
        med = np.nanmedian(windows, axis=1)
        dev = np.abs(windows - med[:, None])
        mad = np.nanmean(dev, axis=1)
    anchors = (counts >= MAD_MIN_PERIODS) & ~np.isnan(arr)
    lo = med - MAD_K * mad
    hi = med + MAD_K * mad
    mask = anchors & ((arr < lo) | (arr > hi))
    if not anchors.any():
        return mask, None, None
    return mask, float(np.min(lo[anchors])), float(np.max(hi[anchors]))


def _iforest_column_mask(arr: np.ndarray, params: dict):
    """sklearn 完整版孤立森林：逐列一维拟合，边界取正常点的分位数。"""
    from sklearn.ensemble import IsolationForest

    valid = ~np.isnan(arr)
    values = arr[valid]
    if values.size < 3:
        return np.zeros(arr.size, dtype=bool), None, None
    contamination = params.get("contamination", "auto")
    if isinstance(contamination, str):
        contamination = "auto" if contamination.lower() == "auto" else float(contamination)
    else:
        contamination = float(contamination)
    model = IsolationForest(
        n_estimators=int(params.get("nEstimators", 200)),
        contamination=contamination,
        random_state=int(params.get("randomState", 42)),
        bootstrap=False,
    )
    X = values.reshape(-1, 1)
    pred = model.fit_predict(X)
    mask = np.zeros(arr.size, dtype=bool)
    mask[valid] = pred == -1
    normal = values[pred == 1]
    if normal.size:
        lower = float(np.quantile(normal, float(params.get("normalLowerQ", 0.005))))
        upper = float(np.quantile(normal, float(params.get("normalUpperQ", 0.995))))
    else:
        lower, upper = float(values.min()), float(values.max())
    return mask, round4(lower), round4(upper)


def detect_column(arr: np.ndarray, algo: str, key: str, expr=None,
                  iforest_params: dict | None = None) -> dict:
    stats = col_stats(arr)
    if algo == "3sigma":
        if stats["n"] == 0:
            mask, lower, upper = np.zeros(arr.size, dtype=bool), None, None
        else:
            mask, lower, upper = _threshold_mask(arr, stats["mean"] - 3 * stats["std"],
                                                 stats["mean"] + 3 * stats["std"])
    elif algo == "iqr":
        if stats["n"] == 0:
            mask, lower, upper = np.zeros(arr.size, dtype=bool), None, None
        else:
            iqr = stats["q3"] - stats["q1"]
            mask, lower, upper = _threshold_mask(arr, stats["q1"] - 1.5 * iqr, stats["q3"] + 1.5 * iqr)
    elif algo == "iforest":
        mask, lower, upper = _mad_mask(arr)
        if lower is None and stats["n"]:
            lower, upper = stats["min"], stats["max"]
    elif algo == "iforest_sklearn":
        mask, lower, upper = _iforest_column_mask(arr, iforest_params or {})
        if lower is None and stats["n"]:
            lower, upper = stats["min"], stats["max"]
    elif algo == "expr":
        if stats["n"] == 0:
            mask = np.zeros(arr.size, dtype=bool)
        else:
            evaluate = expr if callable(expr) else compile_expr(expr or "")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                raw = evaluate(arr, {k: (0.0 if v is None else v) for k, v in stats.items()
                                     if k in ("mean", "std", "median", "q1", "q3", "min", "max")})
            mask = np.broadcast_to(np.asarray(raw, dtype=bool), arr.shape) & ~np.isnan(arr)
        normal = arr[~mask & ~np.isnan(arr)]
        lower = float(normal.min()) if normal.size else stats["min"]
        upper = float(normal.max()) if normal.size else stats["max"]
    else:
        raise ValueError(f"不支持的检测算法：{algo}")

    indices = np.flatnonzero(mask)
    return {
        "key": key, "mask": mask, "indices": indices, "count": int(indices.size),
        "lower": lower, "upper": upper, "stats": stats, "total": int(arr.size),
    }


def detect(frame: pd.DataFrame, columns: list[dict], algo: str, expr=None,
           iforest_params: dict | None = None) -> dict:
    """对全部数值列做检测。缺失单元格一律不参与判定（与浏览器版一致）。"""
    if algo not in ANOMALY_ALGOS:
        raise ValueError(f"不支持的检测算法：{algo}")
    evaluator = compile_expr(expr) if algo == "expr" else None
    per_column: dict[str, dict] = {}
    results = []
    total_rows = int(frame.shape[0])
    for col in columns:
        key = col["key"]
        if key not in frame.columns:
            raise ValueError(f"列不存在：{key}")
        found = detect_column(numeric_array(frame, key), algo, key, evaluator, iforest_params)
        per_column[key] = found
        n = found["count"]
        rate = (n / total_rows * 100) if total_rows else 0.0
        results.append({
            "key": key, "label": col.get("label") or key, "type": col.get("type"),
            "total": total_rows, "anomalies": n, "rate": rate, "normal": total_rows - n,
            "lower": found["lower"], "upper": found["upper"],
            "mean": found["stats"]["mean"], "std": found["stats"]["std"],
            "missing": found["stats"]["missing"],
        })
    num_cols = len(columns)
    total_anomalies = int(sum(r["anomalies"] for r in results))
    denom = total_rows * num_cols
    return {
        "algo": algo,
        "expr": (expr or "") if algo == "expr" else None,
        "params": dict(iforest_params or {}) if algo == "iforest_sklearn" else None,
        # 掩码留在服务端：修复要按检测时的行索引执行，界面只拿到统计与抽样
        "perColumn": per_column,
        "engine": {
            "3sigma": "numpy 3σ（总体标准差 ÷n）",
            "iqr": "numpy 四分位距（sorted[int(n·q)]）",
            "iforest": "numpy 回看窗口 MAD（近似版）",
            "iforest_sklearn": "sklearn.IsolationForest",
            "expr": f"AST 白名单表达式：{expr}" if expr else "表达式",
        }[algo],
        "rowCount": total_rows,
        "results": results,
        "summary": {
            "totalAnomalies": total_anomalies,
            "overallRate": (total_anomalies / denom * 100) if denom else 0.0,
            "colsAffected": int(sum(1 for r in results if r["anomalies"] > 0)),
            "numCols": num_cols,
            "totalCells": denom,
        },
    }


def detection_view(detection: dict | None, stale: bool) -> dict | None:
    """给界面的投影：检测索引留在服务端（修复要用），响应里只带回统计。"""
    if not detection:
        return None
    return {k: v for k, v in detection.items() if k != "perColumn"} | {
        "stale": stale, "anomalyIndices": {k: int(v["count"]) for k, v in (detection.get("perColumn") or {}).items()},
    }


def apply_repair(frame: pd.DataFrame, detection: dict, mode: str,
                 labels: dict[str, str]) -> tuple[pd.DataFrame, int, list[str]]:
    """按检测缓存执行修复。clip/nan_impute 改数值，mask_only 只加 0/1 列。"""
    if mode not in REPAIR_MODES:
        raise ValueError(f"不支持的修复方案：{mode}")
    per_column = detection.get("perColumn") or {}
    out = frame.copy()
    touched = 0
    mask_keys: list[str] = []
    for key, found in per_column.items():
        # np.asarray 而不是直接用：进程内重放时它是 numpy 数组，但从落盘的命令日志
        # 重建回来时它是 JSON list（跨重启的撤销/重做走的就是这条路），两种都得能修。
        indices = np.asarray(found["indices"], dtype="int64")
        if indices.size == 0:
            continue
        if key not in out.columns:
            raise ValueError(f"检测缓存里的列 [{key}] 已不在数据表中，请重新检测")
        arr = numeric_array(out, key)
        if mode == "clip":
            lower, upper = found["lower"], found["upper"]
            if lower is None or upper is None:
                raise ValueError(f"列 [{key}] 没有可用边界，无法截断")
            lo, up = round4(lower), round4(upper)
            vals = arr[indices]
            arr[indices] = np.where(vals < lower, lo, up)
            out[key] = arr
        elif mode == "nan_impute":
            arr[indices] = np.nan
            for start, end in missing_runs(arr):
                fill_run(arr, start, end, "linear")
            out[key] = arr
        else:
            mask_key = f"anomaly_mask_{key}"
            column = np.zeros(out.shape[0], dtype="int64")
            column[indices] = 1
            out[mask_key] = column
            mask_keys.append(mask_key)
            labels.setdefault(mask_key, f"异常掩码:{labels.get(key, key)}")
        touched += int(indices.size)
    return out, touched, mask_keys


# ---------------------------------------------------------------- 缺失段快照 / 曲线降采样

def quality_snapshot(frame: pd.DataFrame, columns: list[dict], time_col: str | None,
                     time_labels: list) -> dict:
    """各列缺失统计 + 数值列的缺失时间段明细 + 重复时间戳计数（全部按当前帧算）。"""
    n = int(frame.shape[0])
    col_stats_out = []
    segments: dict[str, list] = {}
    truncated: dict[str, bool] = {}
    total_missing = 0
    for col in columns:
        key = col["key"]
        series = frame[key]
        if str(key) == str(time_col) and time_col:
            missing = int(pd.to_datetime(series, errors="coerce").isna().sum())
        elif col.get("type") == "float":
            missing = int(np.isnan(numeric_array(frame, key)).sum())
        else:
            missing = int(series.isna().sum() + (series.astype("string") == "").sum())
        total_missing += missing
        col_stats_out.append({
            "key": key, "label": col.get("label") or key, "type": col.get("type"),
            "total": n, "missing": missing, "valid": n - missing,
            "rate": (missing / n * 100) if n else 0.0,
            "imputable": col.get("type") == "float" and str(key) != str(time_col),
        })
        if col.get("type") == "float" and str(key) != str(time_col) and missing:
            runs = missing_runs(numeric_array(frame, key))
            segments[key] = [{"startIdx": a, "endIdx": b, "count": b - a + 1,
                              "startTime": _label(time_labels, a), "endTime": _label(time_labels, b)}
                             for a, b in runs[:MAX_SEGMENTS_PER_COLUMN]]
            truncated[key] = len(runs) > MAX_SEGMENTS_PER_COLUMN
    dup_rows = dup_groups = 0
    if time_col and time_col in frame.columns:
        ts = pd.to_datetime(frame[time_col], errors="coerce")
        dup_rows = int(ts.duplicated().sum())
        # 与 merge_duplicates 的组数同一口径：NaT 之间也算一组，否则诊断页与执行结果会给出两个数字
        dup_groups = int((ts.value_counts(dropna=False) > 1).sum())
    cells = n * len(columns)
    return {
        "rowCount": n, "colCount": len(columns), "timeCol": time_col,
        "columns": col_stats_out, "segments": segments, "segmentsTruncated": truncated,
        "segmentCap": MAX_SEGMENTS_PER_COLUMN,
        "totalMissingCells": total_missing,
        "missingRate": (total_missing / cells * 100) if cells else 0.0,
        "duplicateRows": dup_rows, "duplicateGroups": dup_groups,
        "duplicateRate": (dup_rows / n * 100) if n else 0.0,
    }


def _label(labels: list, idx: int):
    return labels[idx] if 0 <= idx < len(labels) else None


def envelope(arr: np.ndarray, max_points: int) -> list[int]:
    """折线抽稀：每桶保留极小/极大两点，另加缺失段的首尾位置。

    等距抽样会把尖峰整个抽掉，而这一步的图正是用来看尖峰的；缺失点必须留在序列里，
    否则折线会跨过本该断开的空洞。
    """
    n = int(arr.size)
    if n <= max_points:
        return list(range(n))
    buckets = max(1, min(n, int(max_points) // 4))
    keep: set[int] = set()
    for b in range(buckets):
        a = b * n // buckets
        e = max(a + 1, (b + 1) * n // buckets)
        chunk = arr[a:e]
        nan = np.isnan(chunk)
        if (~nan).any():
            keep.add(a + int(np.nanargmin(chunk)))
            keep.add(a + int(np.nanargmax(chunk)))
        if nan.any():
            positions = np.flatnonzero(nan)
            keep.add(a + int(positions[0]))
            keep.add(a + int(positions[-1]))
    return sorted(keep)
