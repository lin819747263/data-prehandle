"""外生变量在服务端生成：预设模板、时间公式、侧表文件按时间戳对齐。

三条通道的共同点：**明细不经过网络，命令日志里只有规格**。
日志存 presetKey + seed / 表达式原文 / 侧表文件名 + 内容 sha + 对齐方式，
所以撤销后重做、后端重启后重放，得到的都是同一串数值——这是把生成搬到服务端最主要的好处：
浏览器那版用 Math.random()，同一个模板每点一次都不一样，撤销再重做就换了一列数据。

预设模板的随机源换成 `numpy.random.default_rng(seed)`：分布与浏览器版逐条对齐
（同一个公式、同一个小数位、同一套夹取），换的是「可复现」，不是数值口径。
"""
from __future__ import annotations

import ast
import hashlib
import re

import numpy as np
import pandas as pd

from .quality import _ALLOWED_NODES, _BoolToBit

MAX_EXO_COLS = 100                # 一次从侧表导入的列数上限
MAX_SIDE_ROWS = 200_000           # 侧表行数上限：它和被加工的主表同量级，再大就该先聚合

PRESET_LABELS = {
    "humidity": "相对湿度 (%)",
    "pressure": "大气压强 (hPa)",
    "cloud_cover": "云量 (0-10)",
    "dew_point": "露点温度 (°C)",
    "radiation_ghi": "水平面总辐照 GHI (W/m²)",
    "radiation_dni": "法向直射辐照 DNI (W/m²)",
    "electricity_price": "实时电价 (元/kWh)",
    "grid_frequency": "电网频率 (Hz)",
}
# dew_point 不是纯时间函数，它读主表气温列，因此必须显式声明依赖
PRESET_NEEDS = {"dew_point": ["temperature"]}
# 与公式/预设里那些 Math.random() 项对应：不看时刻的模板，时间戳读不出来也照样有值
PRESET_TIMELESS = ("pressure", "cloud_cover", "dew_point", "grid_frequency")


def presets_view() -> list[dict]:
    """给界面的下拉选项：与生成器同一份表，前端不再自己抄一遍。"""
    return [{"key": k, "label": v, "needs": PRESET_NEEDS.get(k, [])} for k, v in PRESET_LABELS.items()]


# ---------------------------------------------------------------- 时间分量

def _dt(values) -> pd.Series:
    return pd.to_datetime(pd.Series(values), errors="coerce")


def _hour_of(dt: pd.Series) -> np.ndarray:
    """整数小时（预设模板口径，与浏览器 getHours() 一致：分秒不进公式）。"""
    return dt.dt.hour.to_numpy("float64")


def _frac_hour(dt: pd.Series) -> np.ndarray:
    """公式里的 hour 带分钟：浏览器用的是 getHours() + getMinutes()/60，秒同样被丢掉。"""
    return dt.dt.hour.to_numpy("float64") + dt.dt.minute.to_numpy("float64") / 60.0


def _time_components(dt: pd.Series, n: int) -> dict[str, np.ndarray]:
    return {
        "hour": _frac_hour(dt),
        "day": dt.dt.day.to_numpy("float64"),
        # pandas 周一=0，JS getDay() 周日=0：公式里 weekday 的语义必须跟界面文档一致
        "weekday": (dt.dt.dayofweek.to_numpy("float64") + 1) % 7,
        "idx": np.arange(n, dtype="float64"),
    }


def _roundk(arr, k: int) -> np.ndarray:
    """half-up 保留 k 位，与浏览器 `parseFloat(x.toFixed(k))` 同一取法。

    numpy 的 round 是 half-to-even（np.round(0.5)=0、np.round(2.5)=2），
    直接用会让同一列数据在 JS/Python 两边差 0.1，对拍必然不过。
    """
    factor = 10.0 ** k
    return np.floor(np.asarray(arr, dtype="float64") * factor + 0.5) / factor


def _finite_or_null(arr) -> np.ndarray:
    """±Inf 归为缺失：浏览器把 Infinity 塞进 JSON 后落回后端就是一列 null，不如现在就统一成 null。"""
    v = np.asarray(arr, dtype="float64")
    return np.where(np.isfinite(v), v, np.nan)


# ---------------------------------------------------------------- 预设模板

def generate_preset(key: str, time_values, frame: pd.DataFrame, seed: int) -> tuple[np.ndarray, dict]:
    """按主表时间列生成一列模拟外生变量，返回 (与主表等长的数组, 规格)。

    规格里的 seed 会被调用方写进命令日志，重放时按同一 seed 再抽一遍。
    """
    if key not in PRESET_LABELS:
        raise ValueError(f"未知的预设外生变量：{key}（可选 {'/'.join(PRESET_LABELS)}）")
    dt = _dt(time_values)
    n = int(dt.shape[0])
    hour = _hour_of(dt)
    rng = np.random.default_rng(int(seed) % (2 ** 63))
    noise = lambda: rng.random(n)      # noqa: E731 - 五个模板都要「每行独立同分布」这一句

    if key == "humidity":
        out = _roundk(60 + 20 * np.sin((hour - 6) / 24 * 2 * np.pi) + (noise() - 0.5) * 15, 1)
    elif key == "pressure":
        out = _roundk(1013.25 + (noise() - 0.5) * 8, 1)
    elif key == "cloud_cover":
        base = np.array([2, 5, 7, 3], dtype="float64")[(np.arange(n) // 96) % 4]
        out = np.clip(np.floor(base + (noise() - 0.5) * 4 + 0.5), 0, 10)   # JS Math.round：half-up
    elif key == "dew_point":
        needs = PRESET_NEEDS[key]
        missing = [c for c in needs if c not in [str(x) for x in frame.columns]]
        if missing:
            raise ValueError(f"预设 {key} 需要 {'/'.join(missing)} 列，当前数据集没有")
        # 浏览器写法是 (temp[i] ?? 20)：只有整格缺失才回落到 20
        temp = pd.to_numeric(frame[needs[0]], errors="coerce").to_numpy("float64")
        temp = np.where(np.isnan(temp), 20.0, temp)
        if temp.shape[0] < n:
            temp = np.concatenate((temp, np.full(n - temp.shape[0], 20.0)))
        out = _roundk(temp[:n] - 5 - noise() * 3, 1)
    elif key in ("radiation_ghi", "radiation_dni"):
        peak, spread = (800.0, 50.0) if key == "radiation_ghi" else (650.0, 40.0)
        day = (hour >= 6) & (hour <= 19)            # 与浏览器同一判据：NaN 时刻两个比较都是 False → 走夜间分支
        v = np.where(day, np.sin((hour - 6) / 13 * np.pi) * peak + (noise() - 0.5) * spread, 0.0)
        out = _roundk(v, 1)
    elif key == "electricity_price":
        peak = (hour >= 8) & (hour <= 21)
        v = np.where(peak, 0.85 + noise() * 0.15, 0.35 + noise() * 0.1)
        out = _roundk(v, 3)
    else:  # grid_frequency
        out = _roundk(50 + (noise() - 0.5) * 0.1, 3)

    out = _finite_or_null(out)
    if key not in PRESET_TIMELESS:
        out = np.where(np.isnan(hour), np.nan, out)   # 时间戳读不出来 → 该行为空
    return out, {"label": PRESET_LABELS[key], "rows": n, "seed": int(seed),
                 "validRows": int(np.count_nonzero(~np.isnan(out)))}


# ---------------------------------------------------------------- 时间公式

_FORMULA_CHARS = re.compile(r"^[0-9a-zA-Z_+\-*/().,<>=!&|\s%]+$")
_FORMULA_NAMES = ("hour", "day", "month", "weekday", "idx")
_FORMULA_CONSTS = {"PI": np.pi, "E": np.e}


def _js_round(x):
    """JS Math.round 是 half-up，numpy 是 half-even，这里保住界面侧的口径。"""
    return np.floor(np.asarray(x, dtype="float64") + 0.5)


def _js_min(*args):
    acc = np.asarray(args[0], dtype="float64")
    for a in args[1:]:
        acc = np.minimum(acc, np.asarray(a, dtype="float64"))
    return acc


def _js_max(*args):
    acc = np.asarray(args[0], dtype="float64")
    for a in args[1:]:
        acc = np.maximum(acc, np.asarray(a, dtype="float64"))
    return acc


_FORMULA_FUNCS = {
    "abs": np.abs, "sqrt": np.sqrt, "log": np.log, "log2": np.log2, "log10": np.log10,
    "exp": np.exp, "sin": np.sin, "cos": np.cos, "tan": np.tan,
    "asin": np.arcsin, "acos": np.arccos, "atan": np.arctan,
    "floor": np.floor, "ceil": np.ceil, "round": _js_round, "pow": np.power,
    "hypot": np.hypot, "min": _js_min, "max": _js_max,
}
# JS 的 Math.log 就是自然对数，界面上写 log 也必须是 ln，这一点两边同源
FORMULA_HELP = ("可用变量 hour（含分钟小数）/day/month/weekday（周日为 0）/idx、"
                "常量 PI/E，函数 " + "/".join(sorted(_FORMULA_FUNCS)))


def compile_formula(src: str):
    """把界面的公式表达式编成「按时间分量数组求一列值」的函数；不合法一律 ValueError。

    与异常判定表达式（quality.compile_expr）不是同一套变量：那条对单列取值 v 判真伪，
    这条按时间轴造数，所以白名单各自维护，共享的只是 AST 结构校验与布尔算子替换。
    """
    text = (src or "").strip()
    if not text:
        raise ValueError("请输入生成公式")
    if not _FORMULA_CHARS.match(text):
        raise ValueError("表达式包含不支持的字符")
    py = text.replace("||", " or ").replace("&&", " and ")
    py = re.sub(r"!(?!=)", " not ", py)
    try:
        tree = ast.parse(py, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"公式无法解析：{exc.msg}（{FORMULA_HELP}）") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in _FORMULA_NAMES \
                and node.id not in _FORMULA_FUNCS and node.id not in _FORMULA_CONSTS:
            raise ValueError(f"公式含未知变量：{node.id}（{FORMULA_HELP}）")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name)
                                               and node.func.id in _FORMULA_FUNCS):
            raise ValueError("公式只允许调用 " + "/".join(sorted(_FORMULA_FUNCS)))
        if isinstance(node, ast.Compare) and len(node.ops) > 1:
            raise ValueError("不支持连续比较（如 6 < hour < 19），请改用 and 连接")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            raise ValueError("公式不支持字符串")
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"公式含不支持的语法：{type(node).__name__}")
    tree = _BoolToBit().visit(tree)
    ast.fix_missing_locations(tree)
    code = compile(tree, "<exo-formula>", "eval")

    def evaluate(components: dict[str, np.ndarray]) -> np.ndarray:
        env = {**components, **_FORMULA_CONSTS, **_FORMULA_FUNCS}
        with np.errstate(all="ignore"):
            out = eval(code, {"__builtins__": {}}, env)  # noqa: S307 - AST 已按白名单校验
        arr = np.asarray(out, dtype="float64")
        if arr.ndim == 0:
            arr = np.full(len(components["idx"]), float(arr))
        elif arr.shape[0] == 1:
            arr = np.full(len(components["idx"]), float(arr[0]))
        return arr

    return evaluate


def generate_formula(expr: str, time_values) -> np.ndarray:
    dt = _dt(time_values)
    n = int(dt.shape[0])
    hour = _frac_hour(dt)
    values = compile_formula(expr)(_time_components(dt, n))
    values = _roundk(_finite_or_null(values), 4)         # 浏览器是 parseFloat(val.toFixed(4))
    return np.where(np.isnan(hour), np.nan, values)[:n]   # 单行时间戳读不出来 → 该行 null（浏览器逐行 catch）


# ---------------------------------------------------------------- 侧表对齐

def _secs(s: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """截到「整秒」的整数秒 + 有效位掩码。

    与浏览器 normKey(slice(0,19)) 同口径：两侧都只看得到秒，毫秒级侧表时间戳会落到同一格，
    否则会因 0.5 秒之差整体对不齐。
    """
    dt = pd.to_datetime(s, errors="coerce")
    secs = np.floor_divide(dt.astype("int64").to_numpy("int64"), 1_000_000_000)
    return secs, dt.notna().to_numpy()


def _first_positions(secs: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """主表时间去重：返回 (升序唯一秒数, 各自第一次出现的行号)。

    浏览器用 `if (!map.has(t)) map.set(t, i)`，重复时间戳保留最早那行；
    这里靠 stable argsort 复现同一规则（np.unique 的 return_index 不保证给的是首个）。
    """
    vals = secs[valid]
    idxs = np.nonzero(valid)[0]
    if vals.size == 0:
        return np.empty(0, dtype="int64"), np.empty(0, dtype="int64")
    order = np.argsort(vals, kind="stable")
    sv = vals[order]
    keep = np.concatenate(([True], sv[1:] != sv[:-1]))
    return sv[keep], idxs[order[keep]]


def _last_per_target(targets: np.ndarray) -> np.ndarray:
    """同一个主表行被侧表多行命中时取最后那条（浏览器是后写覆盖前写）。回源行下标。"""
    if targets.size == 0:
        return targets
    order = np.argsort(targets, kind="stable")
    t = targets[order]
    keep = np.ones(t.size, dtype=bool)
    keep[:-1] = t[:-1] != t[1:]
    return order[keep]


def parse_side_table(content: bytes, filename: str) -> pd.DataFrame:
    from . import workspace as ws_store      # 表格解析器只有一份，侧表与主表不能两种口径
    df, _meta = ws_store.dataframe_from_bytes(content, filename)
    return df


def pick_side_time_col(side: pd.DataFrame) -> str | None:
    from . import workspace as ws_store
    cols = ws_store.build_meta_columns(side, None)
    return ws_store.pick_time_col(cols, side)


def inspect_side_table(content: bytes, filename: str) -> dict:
    """把侧表的表头/可用变量名/时间列候选摊开给界面，先看清楚再决定挂哪些列。

    needsName 为真的列必须由用户显式命名后才能提交：这类表头（含中文，或纯符号）转写成
    标识符要么变空、要么只剩 `C` 这种看不出含义的碎片，静默改名会让界面上出现没人认得的列。
    """
    side = parse_side_table(content, filename)
    names = [str(c) for c in side.columns]
    time_col = pick_side_time_col(side)
    columns, used = [], set()
    for h in names:
        numeric = bool(pd.to_numeric(side[h], errors="coerce").notna().any())
        is_time = h == time_col
        key = "" if is_time else target_key(h)
        ok = bool(key) and key not in used and is_ascii_header(h)
        if ok:
            used.add(key)
        columns.append({
            "from": h, "key": key if ok else None, "label": h,
            "numeric": numeric, "time": is_time,
            "needsName": not (is_time or ok),
            "reason": "" if (is_time or ok) else _naming_reason(h, key, used),
        })
    return {"filename": filename, "sha": content_sha(content), "rows": int(side.shape[0]),
            "columns": columns, "sideTimeCol": time_col,
            "importable": sum(1 for c in columns if not c["time"] and c["numeric"])}


def is_ascii_header(header) -> bool:
    return all(ord(ch) < 128 for ch in str(header or ""))


def _naming_reason(header, key: str, used: set) -> str:
    if not is_ascii_header(header):
        return "列名含非 ASCII 字符（中文等），转写后会丢失含义"
    if not key:
        return "列名里没有可用的字母或数字"
    if key in used:
        return f"转写后的变量名与前面的列重名（都会变成 {key}）"
    return "列名不是合法的变量名"


def align_side_table(main_time_values, side: pd.DataFrame, side_time_col: str | None,
                     mode: str = "left", tolerance_minutes: int | None = None,
                     cols: list[str] | None = None) -> dict:
    """把侧表按时间戳对齐到主表时间轴，回 {列名: 与主表等长的数组} 与匹配统计。

    mode 只有两种真实语义：left = 精确时间戳匹配，未命中留空；nearest = 就近匹配。
    界面上曾有第三项「内连接」，而浏览器实现里它与 left 一模一样（它删不掉主表行）——
    外生变量是往主表上挂列，主表行数不由侧表决定，所以这里不提供假选项。
    """
    if mode not in ("left", "nearest"):
        raise ValueError(f"未知的对齐方式：{mode}（只支持 left 精确匹配 / nearest 就近匹配）")
    if side.shape[0] > MAX_SIDE_ROWS:
        raise ValueError(f"侧表 {side.shape[0]} 行，超过单次对齐上限 {MAX_SIDE_ROWS} 行，请先聚合再导入")
    main_secs, main_valid = _secs(pd.Series(list(main_time_values)))
    if not main_valid.any():
        raise ValueError("主表时间列整列无法解析，无法按时间戳对齐")
    uniq, first_i = _first_positions(main_secs, main_valid)
    last = max(uniq.size - 1, 0)

    names = [str(c) for c in side.columns]
    if not side_time_col or side_time_col not in names:
        raise ValueError(f"侧表里没有找到时间列 {side_time_col or '(未指定)'}，请重新指定用于对齐的列")
    side_secs, side_valid = _secs(side[side_time_col])

    wanted = [c for c in (cols or names) if c in names and c != side_time_col]
    if not wanted:
        raise ValueError("侧表里除时间列外没有可导入的变量列")
    if len(wanted) > MAX_EXO_COLS:
        raise ValueError(f"侧表将导入 {len(wanted)} 列，超过上限 {MAX_EXO_COLS} 列，请指定需要的列")

    pos = np.searchsorted(uniq, side_secs)
    if mode == "left":
        hit = side_valid & (pos < uniq.size) & (uniq[np.clip(pos, 0, last)] == side_secs)
        chosen = np.where(hit, first_i[np.clip(pos, 0, last)], -1)
    else:
        tol = int(tolerance_minutes) * 60 if tolerance_minutes else None
        ri = np.where(pos < uniq.size, first_i[np.clip(pos, 0, last)], -1)
        lv = np.where(pos > 0, uniq[np.clip(pos - 1, 0, last)], 0)
        li = np.where(pos > 0, first_i[np.clip(pos - 1, 0, last)], -1)
        dist_r = np.abs(np.where(ri >= 0, uniq[np.clip(pos, 0, last)], 0) - side_secs)
        dist_l = np.abs(lv - side_secs)
        # 距离相同取行号更小的那个：浏览器是顺序扫描 + 严格小于，等价于取更早的主表行
        take_left = (li >= 0) & ((ri < 0) | (dist_l <= dist_r))
        chosen = np.where(take_left, li, ri)
        dist = np.where(take_left, dist_l, dist_r)
        hit = side_valid & (chosen >= 0)
        if tol is not None:
            hit &= dist <= tol
        chosen = np.where(hit, chosen, -1)

    src = _last_per_target(chosen[hit].astype("int64"))
    targets = chosen[hit][src]
    if src.size == 0:
        # 一行都对不上时必须说明"对不上"，而不是回一句"没有数值列"——前者是时间戳/模式问题，
        # 用户改一下对齐方式就能解决，后者会把他引向完全错误的方向
        raise ValueError(f"侧表与主表时间戳没有任何匹配（模式：{'精确匹配' if mode == 'left' else '就近匹配'}" +
                         (f"，容差 {int(tolerance_minutes)} 分钟）" if tolerance_minutes else "）") +
                       "，请检查侧表时间列或改用另一种对齐方式")

    columns: dict[str, np.ndarray] = {}
    coerced: dict[str, int] = {}
    non_numeric: list[str] = []
    for c in wanted:
        raw = side[c]
        arr = pd.to_numeric(raw, errors="coerce").to_numpy("float64")
        if np.all(np.isnan(arr[src])):
            non_numeric.append(c)      # 整列挂进来只会得到一列空值，明确拒绝而不是静默塞进去
            continue
        out = np.full(main_secs.shape, np.nan, dtype="float64")
        out[targets] = _finite_or_null(arr[src])
        columns[c] = out
        coerced[c] = int(np.count_nonzero(np.isnan(arr[src])))
    if not columns:
        raise ValueError("侧表里没有可导入的数值列" +
                         (f"（非数值列：{'、'.join(non_numeric)}）" if non_numeric else ""))
    return {
        "columns": columns,
        "keys": list(columns),
        "labels": {c: str(c) for c in columns},
        "nonNumeric": non_numeric,
        "coerced": coerced,
        "stats": {
            "sideRows": int(side.shape[0]),
            "sideCols": list(names),
            "sideTimeCol": str(side_time_col),
            "mode": mode,
            "toleranceMinutes": int(tolerance_minutes) if tolerance_minutes else None,
            "matchedSideRows": int(src.size),
            "matchedMainRows": int(targets.size),
            "mainRows": int(main_secs.shape[0]),
            "unmatchedSideRows": int(side.shape[0]) - int(src.size),
            "coverage": (int(targets.size) / int(main_secs.shape[0]) * 100) if main_secs.shape[0] else 0.0,
        },
    }


# ---------------------------------------------------------------- 侧表内容指纹

def content_sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16]


def target_key(header: str) -> str:
    """侧表列名 → 主表列名：只保留 [A-Za-z0-9_]，字母开头（否则加 c_ 前缀）。

    浏览器旧版用 safe_key 一路转写，两个中文表头会双双变成 `___` 并互相覆盖，界面上
    却是两个不同变量。这里转不出可用名字的表头直接报出来，让用户改名而不是静默撞车。
    """
    raw = re.sub(r"[^A-Za-z0-9_]", "_", str(header or ""))
    raw = re.sub(r"_+", "_", raw).strip("_")
    if not raw:
        return ""
    if not re.match(r"^[A-Za-z]", raw):
        raw = "c_" + raw
    return raw[:64]


def default_targets(headers: list[str]) -> list[dict]:
    out, seen = [], set()
    bad = []
    for h in headers:
        key = target_key(h)
        if not key or key in seen or not is_ascii_header(h):
            bad.append(str(h))
            continue
        seen.add(key)
        out.append({"from": str(h), "key": key, "label": str(h)})
    if bad:
        raise ValueError(f"侧表列名 {'、'.join(bad)} 转不成合法列名（需为纯 ASCII、字母开头、"
                         f"只含字母数字下划线且不重名），请在界面上为它们指定变量名")
    return out


def check_content_sha(filename: str, content: bytes, expected: str) -> None:
    """重放时确认侧表还是当初那份：同名文件被覆盖过就必须报错，不能静默换一列数据。"""
    if expected and content_sha(content) != expected:
        raise ValueError(f"侧表文件 {filename} 的内容已变化（命令里记录 sha {expected}），"
                         f"无法重放该外生变量导入，请重新导入或撤销到该步之前")
