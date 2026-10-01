"""服务端工作区（workspace）：DataFrame 常驻后端，浏览器只取元数据与分页窗口。

为什么要有这一层：原先整张表放在浏览器里（2880×6 起步，实测最大的导入文件 11000×40
＝440,000 格），每步加工都遍历整表，再加上撤销栈要深拷贝 25 份快照，数据量一大就撑不住。
现在改成：
- 注册表 ws_id → Workspace（进程内，有条数与单元格上限，超量按最久未访问淘汰）；
- 每次加工以"命令"形式记进 ops，当前帧 = base 帧按顺序重放，
  所以撤销不必存全量拷贝，回到版本号 v 就是重放 ops[:v]；
  最近访问过的几版帧会留在内存里（见 Workspace._remember），重放从就近的一版起步，
  于是"撤销一步"通常是换个指针而不是把整条历史再跑一遍；
- 行数据只在浏览器留在当前页窗口（默认 50 行），统计量全部在后端算；
- 命令日志同时落盘到 state 目录（`state_store`），所以撤销/重做的历史**跨页面刷新、
  跨后端重启**都还在：工作区被淘汰或服务端重启后，第一次访问那个 wsId 会按
  「来源 + 日志」重建同一颗帧。落盘的只有命令规格与检测索引，明细始终来自数据集目录里的原始文件。

时间列内部一律用 datetime64 存储，timeFormat 只决定"渲染成什么字符串"，
因此采样频率、重复时间戳这类统计量不受显示格式影响（旧版按字符串比较，
把时间格式改成只到天时会把同一天的 96 行全判成重复）。
"""
from __future__ import annotations

import copy
import io
import math
import random
import re
import threading
import uuid
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from . import exo, features, quality, state_store

MAX_WORKSPACES = 8
MAX_CELLS = 5_000_000          # 单个工作区单元格上限（11000×40 是 44 万，留足余量）
MAX_PAGE = 500                 # 单次行窗口上限
DEFAULT_PAGE = 50
# 一次合并导入最多带几份文件：行数与格数另有 MAX_CELLS 兜底，这条只挡住"一次拖进上百个分片"
MAX_MERGE_FILES = 12
# 撤销重放的帧缓存（见 Workspace._remember）：一颗帧的 dtype 字节数超过这个数就不留档，
# 退回"从载入帧整段重放"的老行为；连同下面的版本条数上限，缓存不会变成第二份内存压力。
SNAP_MAX_BYTES = 64 * 1024 * 1024
SNAP_MAX_VERSIONS = 3
# 重放跨这么多条命令以上时，在中途留一颗检查点：继续往前翻版本就不用每次从头重放
SNAP_CHECKPOINT_GAP = 8

# 只保护注册表与访问计数这三个进程级结构（_REGISTRY/_ACCESS/_REBUILD_LOCKS）。
# 帧内部的读与写各由 Workspace.lock 负责：一把全局锁会把「A 工作区在跑异常检测」
# 变成「B 工作区翻页也要等」，而 FastAPI 本来就是多线程派发请求的。
_LOCK = threading.RLock()
_REGISTRY: dict[str, "Workspace"] = {}
_ACCESS: dict[str, int] = {}
_ACCESS_TICK = 0
# 每个 wsId 一把「重建锁」：内存里没有这颗帧时，两个并发请求会各自从磁盘日志重放一遍
# （重放要解析原始文件 + 逐条执行命令，是这里最贵的一段），后注册的那颗把前一颗顶掉，
# 于是同一个 wsId 在短时间内存在过两份帧。锁按 wsId 分，不共用一把，重建 A 不阻塞 B。
_REBUILD_LOCKS: dict[str, threading.Lock] = {}

# 与前端 utils.js 的 TIME_FORMAT_PATTERNS 一一对应（顺序也必须一致：格式投票取首个胜出者）
# 带毫秒的三条排在各自的"秒级"版本之前：同一批样本只会命中一条（秒级模式要求 $ 收尾），
# 顺序只为并列胜出时的取舍稳定。
# 月/日/时分秒都放宽成 1~2 位（`2024/6/1 8:30` 这种不补零的写法很常见）：投票只判"形状"，
# 它报出来的分隔符就是数据里真写的那个。紧凑格式（无分隔符的 8/14/10/13 位数字）不放宽，
# 放宽后 6 位纯数字也会被认成 YYYYMMDD，那是凭形状编时间。
TIME_FORMAT_PATTERNS: list[tuple[str, re.Pattern, int]] = [
    ("YYYY-MM-DD HH:mm:ss.SSS", re.compile(r"^\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{1,2}:\d{1,2}\.\d{1,3}$"), 98),
    ("YYYY-MM-DD HH:mm:ss", re.compile(r"^\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{1,2}:\d{1,2}$"), 98),
    ("YYYY/MM/DD HH:mm:ss.SSS", re.compile(r"^\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{1,2}:\d{1,2}\.\d{1,3}$"), 95),
    ("YYYY/MM/DD HH:mm", re.compile(r"^\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{1,2}$"), 95),
    ("YYYY-MM-DDTHH:mm:ss.SSS", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}T\d{1,2}:\d{1,2}:\d{1,2}\.\d{1,3}"), 96),
    ("YYYY-MM-DDTHH:mm:ss", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}T\d{1,2}:\d{1,2}:\d{1,2}"), 96),
    ("YYYY-MM-DD HH:mm", re.compile(r"^\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{1,2}$"), 92),
    ("YYYY-MM-DD", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$"), 90),
    ("YYYY/MM/DD", re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$"), 88),
    ("epoch_ms", re.compile(r"^\d{13}$"), 99),
    ("epoch_s", re.compile(r"^\d{10}$"), 97),
    ("YYYYMMDDHHmmss", re.compile(r"^\d{14}$"), 94),
    ("YYYYMMDD", re.compile(r"^\d{4}\d{2}\d{2}$"), 85),
    ("MM/DD/YYYY", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}"), 80),
    ("DD-MM-YYYY", re.compile(r"^\d{1,2}-\d{1,2}-\d{4}"), 78),
]

# 解析用的 pandas format（只用于读入源字符串，pandas 自己的分词器，不受 Windows CRT 影响）
# 顺序就是严格解析的尝试顺序：越靠前的越"标准"，并列时取排在前面的。
_PARSE_FORMAT = {
    "YYYY-MM-DD HH:mm:ss.SSS": "%Y-%m-%d %H:%M:%S.%f",
    "YYYY-MM-DD HH:mm:ss": "%Y-%m-%d %H:%M:%S",
    "YYYY/MM/DD HH:mm:ss.SSS": "%Y/%m/%d %H:%M:%S.%f",
    "YYYY/MM/DD HH:mm": "%Y/%m/%d %H:%M",
    "YYYY-MM-DDTHH:mm:ss.SSS": "%Y-%m-%dT%H:%M:%S.%f",
    "YYYY-MM-DDTHH:mm:ss": "%Y-%m-%dT%H:%M:%S",
    "YYYY-MM-DD HH:mm": "%Y-%m-%d %H:%M",
    "YYYY-MM-DD": "%Y-%m-%d",
    "YYYY/MM/DD": "%Y/%m/%d",
    "YYYYMMDDHHmmss": "%Y%m%d%H%M%S",
    "YYYYMMDD": "%Y%m%d",
    "MM/DD/YYYY": "%m/%d/%Y",
    "DD-MM-YYYY": "%d-%m-%Y",
}

# 可切换的显示格式：与前端 convertSingleTime 的 switch 分支一致，
# 每项都是 YYYY/MM/DD/HH/mm/ss(/SSS) 占位符模板（渲染时自己做替换，不用 strftime）。
DISPLAY_FORMATS = (
    "YYYY-MM-DD HH:mm:ss", "YYYY-MM-DD HH:mm", "YYYY-MM-DD",
    "YYYY/MM/DD HH:mm", "YYYY/MM/DD", "YYYY-MM-DDTHH:mm:ss",
    "YYYY-MM-DD HH:mm:ss.SSS",
    "YYYYMMDDHHmmss", "YYYYMMDD", "epoch_ms", "epoch_s",
)
_TOKEN_FORMATS = tuple(f for f in DISPLAY_FORMATS if not f.startswith("epoch_"))
# 投票能识别、但不在可切换列表里的源格式：入库解析照旧，显示一律退回 ISO
_LOCALE_FORMATS = ("MM/DD/YYYY", "DD-MM-YYYY")   # 只用于解析，不作为显示格式

# 模板里的占位符就是这一套（顺序很重要：多位在前，单位在后，"MM" 永远优先于 "M"）。
# 单位版是不补零的写法（2024/6/1 8:30），项目内外两边都常见，所以两套都吃；
# 单字母 's' 刻意不做占位符，否则 "秒"/"seconds" 这类字面量会被吃掉。
_FORMAT_TOKENS = ("YYYY", "SSS", "MM", "DD", "HH", "mm", "ss", "M", "D", "H", "m")
# 自定义模板 → pandas format：本项目这套占位符翻成 strptime；单字母与双字母都落同一个
# %x（pandas 的 %m/%d/%H/%M 本来就容得下 1~2 位），显示侧才区分补零与否。
_STRPTIME_TOKENS = (("SSS", "%f"), ("YYYY", "%Y"), ("MM", "%m"), ("M", "%m"),
                    ("DD", "%d"), ("D", "%d"), ("HH", "%H"), ("H", "%H"),
                    ("mm", "%M"), ("m", "%M"), ("ss", "%S"))

# 本机时区偏移：JS 的 new Date('2024-06-01 00:00:00') 按本地时区解释，
# pandas 的 naive Timestamp 不带时区，换算 epoch 时必须自己补这一段偏移。
_LOCAL_OFFSET = (datetime.now().astimezone().utcoffset() or timedelta(0)).total_seconds()

_TIME_NAME_HINTS = ("timestamp", "time", "date", "datetime", "日期", "时间", "时刻", "ts")
# 一列要被判成时间列，至少这么多比例的有值样本能按某个已知格式解析出来。
# 列名只是"值得一试"的线索，从来不是证据：叫「时间」的中文文本列、叫 time 的浮点读数
# 都过不了这道闸门，宁可回一句"没有时间列"，也不能把整列洗成 NaT 或 1970 年的假时间。
MIN_TIME_HIT_RATE = 0.6

RESAMPLE_RATES = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60, "120min": 120, "1440min": 1440}
RESAMPLE_METHODS = ("mean", "sum", "first", "interpolate")


# ---------------------------------------------------------------- 工具函数

def _set_col_key(name: str, work: pd.DataFrame, what: str) -> str:
    """新列/改名后的列键 = 用户写的那个名字，原样落进 DataFrame 的列头。

    旧的 safe_key 规则先把名字转写成小写、非 [a-z0-9_] 换下划线：两个中文表头
    （「有功功率」「出力利用率」）会双双压成 `____`/`___` 并互相覆盖，第二次就报
    「列名 [____] 已存在」，界面上看到的名字和真实键还对不上。现在键与显示名同值，
    撞名直接报错让用户换。
    掩码列与外生变量列仍走 _MASK_KEY 的 ASCII 变量名契约——那是对外声明的变量名，
    要进导出列头和公式，不在这条规则里。
    """
    key = str(name or "").strip()
    if not key:
        raise ValueError(f"{what}不能为空")
    if len(key) > 128:
        raise ValueError(f"{what}过长（{len(key)} 字符，上限 128）")
    if any(ch in key for ch in ("\n", "\r", "\x00")):
        raise ValueError(f"{what}不能包含换行或空字符")
    if key in [str(c) for c in work.columns]:
        raise ValueError(f"{what} [{key}] 已存在，请更换名称")
    return key


def _is_missing(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return v is pd.NaT


def _jsonable(v):
    """numpy / pandas 标量 → JSON 原生类型，缺失 → None。"""
    if v is None or v is pd.NaT:
        return None
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    if isinstance(v, pd.Timestamp):
        return v.isoformat(sep=" ")
    if isinstance(v, (datetime,)):
        return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def _log_jsonable(v):
    """命令日志用的深度转换：容器递归、numpy 数组转 list、标量走 `_jsonable`。

    检测索引（`indices`）必须能过这一关：修复命令把它钉进日志才能重放，而它落盘后要能
    在**新进程**里回流给 `quality.apply_repair`，所以那边已改成 `np.asarray(...)` 兼容 list。
    逐列的 `mask` 是整条布尔向量（n 个 true/false），修复只用 `indices`，落盘时丢掉——
    11000 行的表上它能占几十 KB，且重放结果不受影响。
    """
    if isinstance(v, dict):
        drop_mask = "indices" in v and "key" in v
        return {k: _log_jsonable(val) for k, val in v.items() if not (drop_mask and k == "mask")}
    if isinstance(v, (list, tuple, set, pd.Index)):
        return [_log_jsonable(x) for x in v]
    if isinstance(v, np.ndarray):
        return _log_jsonable(v.tolist())
    return _jsonable(v)


# 命令种类 → 界面上的短名。撤销/重做按钮要说"撤销「重采样」"，靠 summary 太长且带数字。
OP_KIND_LABELS = {
    "set_time_format": "时间格式转换", "set_time_col": "指定时间列",
    "rename_column": "重命名列", "delete_column": "删除列",
    "convert_unit": "单位换算", "derived_column": "列运算生成",
    "resample": "重采样", "impute": "缺失值填补", "anomaly-repair": "异常修复",
    "mask-generate": "生成掩码列", "mask-delete": "删除掩码列",
    "feature_time": "时间与日历特征", "feature_lag": "滞后与滑动窗口",
    "feature_diff": "差分与频域", "feature_cat": "类别特征编码",
    "exo-preset": "预设外生变量", "exo-formula": "公式生成外生变量", "exo-file": "侧表对齐合并",
    "split_apply": "生成数据集划分列", "holidays": "配置节假日表",
}

# 滞后/窗口、差分/频域拆成两次独立生成之后，同一条命令种类要看 group 才知道生成了哪一半
GROUPED_OP_LABELS = {
    "feature_lag": {"lag": "滞后特征", "window": "滑动窗口特征"},
    "feature_diff": {"diff": "差分特征", "fft": "频域特征"},
}


def _op_label(op: dict) -> str:
    kind = op.get("kind")
    base = OP_KIND_LABELS.get(kind, "")
    by_group = GROUPED_OP_LABELS.get(kind)
    if by_group:
        got = by_group.get((op.get("params") or {}).get("group"))
        if got:
            return got
    return base or str(op.get("summary") or kind or "")

# 这些 params 键装的是整列/整份检测数据：可以进日志与重放，但**绝不进 HTTP 响应**
# （meta.ops 每次操作都回给浏览器，带着数组就等于把明细又送回前端）。
HEAVY_PARAM_KEYS = frozenset({"data", "detection"})


def _op_params_view(op: dict) -> dict:
    params = op.get("params") or {}
    return {k: v for k, v in params.items() if k not in HEAVY_PARAM_KEYS}


def _op_brief(op: dict) -> dict:
    return {"kind": op.get("kind"), "label": _op_label(op),
            "summary": op.get("summary", ""), "at": op.get("at", "")}


def vote_time_format(samples: list[str]) -> dict | None:
    """对原始字符串样本做格式投票，等价于前端 detectTimeFormatOfSamples。

    一条模式都没命中就返回 None：过去这里会退回一个"看起来最标准"的 ISO 格式，
    于是中文列、纯数字列也带着 0% 命中率被当成识别成功，界面上那颗绿色徽章纯属白送。
    """
    strs = [s for s in (str(x).strip() for x in samples) if s]
    if not strs:
        return None
    votes = {fmt: 0 for fmt, _, _ in TIME_FORMAT_PATTERNS}
    for s in strs:
        for fmt, pat, _ in TIME_FORMAT_PATTERNS:
            if pat.match(s):
                votes[fmt] += 1
    best_format, best_count = "", 0
    for fmt, count in votes.items():
        if count > best_count:
            best_format, best_count = fmt, count
    if not best_count:
        return None
    confidence = next((c for f, _, c in TIME_FORMAT_PATTERNS if f == best_format), 80)
    return {
        "format": best_format,
        # 源格式可能落在 MM/DD/YYYY 这类不可切换的分支上：解析按源格式，显示退回 ISO
        "displayFormat": best_format if best_format in _TOKEN_FORMATS else "YYYY-MM-DD HH:mm:ss",
        "confidence": confidence,
        "matchRate": best_count / len(strs) * 100,
        "matched": best_count,
        "sampled": len(strs),
    }


# 用户手写的模板很常见是 JS/moment 那套大小写（yyyy-MM-dd HH:mm:ss.SSS），和项目内的
# 占位符只差三个词的写法。这里一次性归一，免得"格式识别不出来"变成"你得会写我们的方言"。
_FORMAT_ALIASES = (("yyyy", "YYYY"), ("dd", "DD"), ("hh", "HH"))


def canonicalize_format(tpl: str) -> str:
    out = tpl.strip()
    for src, dst in _FORMAT_ALIASES:
        out = out.replace(src, dst)
    return out


def _iter_template(tpl: str):
    """从左到右切模板：对得上占位符的吃占位符，其余字符原样当字面量。

    不用 str.replace 逐词替换：那种写法在引入单位占位符后会自我踩踏
    （"MM"→"%m" 之后，"%m" 里那个 m 又被"m"→"%M" 吃掉，格式串直接废掉）。
    """
    i = 0
    while i < len(tpl):
        for tok in _FORMAT_TOKENS:
            if tpl.startswith(tok, i):
                yield True, tok
                i += len(tok)
                break
        else:
            yield False, tpl[i]
            i += 1


_STRPTIME_MAP = dict(_STRPTIME_TOKENS)
_TOKEN_RE = re.compile(r"YYYY|SSS|MM|DD|HH|mm|ss|M|D|H")


def _format_pattern(fmt: str) -> str | None:
    """源解析用的严格格式串：登记过的直接给，含占位符的自定义模板逐词翻译给 pandas。"""
    if fmt in _PARSE_FORMAT:
        return _PARSE_FORMAT[fmt]
    if fmt in _TIME_UNITS:
        return None
    if not _TOKEN_RE.search(fmt):
        return None
    return "".join(_STRPTIME_MAP[val] if is_tok else ("%%" if val == "%" else val)
                   for is_tok, val in _iter_template(fmt))


def normalize_format(fmt: str, custom: str | None = None) -> str:
    """校验可切换的显示格式；custom 必须是含占位符的模板。

    三条来路都归一到同一个东西：登记过的格式名直接用；`custom` 用模板；直接把模板写在
    `format` 上也认（只要它含占位符）——前端两个入口（源格式手填 / 目标格式自定义）共用
    这一份校验，规则只有一套。
    """
    c = canonicalize_format(custom or "") if (fmt or "").strip() == "custom" else ""
    if (fmt or "").strip() == "custom":
        if not c or not re.search(r"YYYY|MM|DD|HH|mm|ss|SSS", c):
            raise ValueError("自定义格式需至少包含 YYYY/MM/DD/HH/mm/ss/SSS 之一")
        if len(c) > 64:
            raise ValueError(f"自定义格式过长（{len(c)} 字符，上限 64）")
        return c
    name = canonicalize_format(fmt or "")
    if name in DISPLAY_FORMATS:
        return name
    if re.search(r"YYYY|MM|DD|HH|mm|ss|SSS", name):
        if len(name) > 64:
            raise ValueError(f"时间格式过长（{len(name)} 字符，上限 64）")
        return name
    raise ValueError(f"不支持的时间格式：{fmt}（可选：{'、'.join(DISPLAY_FORMATS)}，或含占位符的自定义模板）")


def reformat_iso(iso: str, fmt: str) -> str:
    """iso 形如 'YYYY-MM-DD HH:MM:SS'（带毫秒时再跟 '.SSS'），按显示占位符重排。

    双字母补零、单字母不补零，两边共用 `_iter_template` 那一个切分器：显示与解析
    只会对同一份模板有两种读法，绝不会出现"解析认得、显示写回一半字面量"。
    """
    def num(part: str) -> str:
        return str(int(part))
    parts = {
        "YYYY": iso[0:4], "MM": iso[5:7], "M": num(iso[5:7]), "DD": iso[8:10], "D": num(iso[8:10]),
        "HH": iso[11:13], "H": num(iso[11:13]), "mm": iso[14:16], "m": num(iso[14:16]),
        "ss": iso[17:19], "SSS": iso[20:23] if len(iso) > 19 else "000",
    }
    return "".join(parts[val] if is_tok else val for is_tok, val in _iter_template(fmt))


# 整串里每个占位符落在哪两个下标（'YYYY-MM-DD HH:mm:ss.SSS' 的固定位置）。
_ISO_TOKEN_SPAN = {"YYYY": (0, 4), "MM": (5, 7), "DD": (8, 10), "HH": (11, 13),
                   "mm": (14, 16), "ss": (17, 19), "SSS": (20, 23)}
# 单位版 = 同一个位置，只是去掉前导零
_ISO_TOKEN_PLAIN = {"M": (5, 7), "D": (8, 10), "H": (11, 13), "m": (14, 16)}
# 渲染计划：0=字面量，1=切片，2=切片后去前导零
_RENDER_PLANS: dict[str, list[tuple[int, object]]] = {}


def _render_plan(fmt: str) -> list[tuple[int, object]]:
    """把显示模板编成一份「下标指令」，一条命令里只切一次，不必逐行重跑切分器。

    缓存按 fmt 收，条目就是几个小元组；自定义模板五花八门，所以超过 64 种就整张清一次
    ——宁可重编，也不要一个按用户输入无限增长的字典。
    """
    plan = _RENDER_PLANS.get(fmt)
    if plan is not None:
        return plan
    ops: list[tuple[int, object]] = []
    for is_tok, val in _iter_template(fmt):
        if not is_tok:
            ops.append((0, val))
        elif val in _ISO_TOKEN_SPAN:
            ops.append((1, _ISO_TOKEN_SPAN[val]))
        else:
            ops.append((2, _ISO_TOKEN_PLAIN[val]))
    if len(_RENDER_PLANS) >= 64:
        _RENDER_PLANS.clear()
    _RENDER_PLANS[fmt] = ops
    return ops


def reformat_many(isos: list, fmt: str) -> list:
    """整列按显示模板重排，与逐值调 `reformat_iso` 等价（等值比较见 verify_step2_time 的渲染对拍）。

    快在两处：切分器每列跑一次而不是每行一次，行内只剩切片与拼串。
    2880 行实测 17 ms → 3 ms；缺失位仍然是 None，不化成 'None' 那种假时间。
    """
    ops = _render_plan(fmt)
    out: list = []
    for s in isos:
        if s is None:
            out.append(None)
            continue
        if len(s) == 19:
            s += ".000"      # 没有毫秒位时 SSS 一律渲染成 000，与 reformat_iso 同值
        pieces: list[str] = []
        for code, arg in ops:
            if code == 0:
                pieces.append(arg)
            elif code == 1:
                pieces.append(s[arg[0]:arg[1]])
            else:
                pieces.append(str(int(s[arg[0]:arg[1]])))
        out.append("".join(pieces))
    return out


_TIME_UNITS = {"epoch_ms": "ms", "epoch_s": "s"}


def iso_strings(ts: pd.Series, with_ms: bool = False) -> list:
    """datetime64 → 'YYYY-MM-DD HH:MM:SS'（with_ms 时再跟 '.SSS'；缺失为 None）。

    不用 astype(str)：pandas 在整列时间全为午夜时会输出省略时分的 '2024-06-01'，
    按页渲染时同一天数据会时带时分、时不带，切片取值就不稳；也不用 strftime
    （Windows 的 CRT 会把 '/' 改写成本地化分隔符，实测 '%Y/%m/%d' 出来是 '2024-06-01'）。
    这里走 numpy 的 datetime64[s] → str：它给的是固定宽度的 'YYYY-MM-DDTHH:MM:SS'（NaT 就是 'NaT'），
    把 'T' 换成空格即成 ISO 串；秒以下从 ns 整数取，与旧的 dt.microsecond // 1000 同值。
    整列一次做完，2880 行 10 ms → 2.6 ms。缺失位一律回 None，不能让它穿过格式化变成
    'Na -- ::' 那种像时间又不是时间的字符串（第一版就栽在 'NaT' 里那个 T 被换成了空格，
    判_missing_ 因此失灵，靠 .verify 的等值比较才揪出来）。
    """
    raw = ts.values.astype("datetime64[s]").astype(str)
    naive = pd.Series(raw).str.replace("T", " ", regex=False).tolist()
    missing = ts.isna().tolist()
    if not with_ms:
        return [None if is_na else s for is_na, s in zip(missing, naive)]
    ns = ts.values.astype("datetime64[ns]").astype("int64")
    ms = pd.Series((ns // 1_000_000) % 1000).astype(str).str.zfill(3).tolist()
    return [None if is_na else f"{s}.{m}" for is_na, s, m in zip(missing, naive, ms)]


def render_times(series: pd.Series, fmt: str) -> list:
    """把 datetime64 列按显示格式渲染（epoch 格式渲染成数字）。"""
    ts = pd.to_datetime(series, errors="coerce")
    iso = iso_strings(ts, with_ms="SSS" in fmt)
    if fmt in _TIME_UNITS:
        unit = _TIME_UNITS[fmt]
        shift = int(_LOCAL_OFFSET) * (1000 if unit == "ms" else 1)
        values = (ts.astype("int64") // (1_000_000 if unit == "ms" else 1_000_000_000) - shift).tolist()
        return [None if s is None else int(v) for s, v in zip(iso, values)]
    return reformat_many(iso, fmt)


def _strict_parse_strings(strs: pd.Series, fmt: str) -> pd.Series:
    """只按登记过或自定义的严格格式解析；解析不动就是 NaT，绝不猜。"""
    pattern = _format_pattern(fmt)
    if pattern is None:
        return pd.Series([pd.NaT] * len(strs), index=strs.index, dtype="datetime64[ns]")
    return pd.to_datetime(strs, format=pattern, errors="coerce")


def _candidate_formats(strs: pd.Series) -> list[str]:
    """候选格式的尝试顺序：形状投票胜出的那个排第一，其余按登记表兜底。

    为什么要投票先试：pandas 的 `format=` 只卡**位数**，字面分隔符是宽容的
    （`%Y/%m/%d` 能把 `2024-6-1` 读进去），所以按登记表顺序试会先撞上斜杠版格式，
    界面上连字符数据就被报成"源格式 YYYY/MM/DD HH:mm"。分隔符应当念数据里真写的那个。
    """
    vote = vote_time_format(strs.dropna().head(20).tolist()) or {}
    first = vote.get("format")
    keys = list(_PARSE_FORMAT)
    if first in _PARSE_FORMAT:
        keys.remove(first)
        keys.insert(0, first)
    return keys


def best_strict_parse(values: pd.Series) -> tuple[pd.Series, str, float] | None:
    """逐个候选格式严格解析这一列字符串，返回 (结果, 胜出格式, 命中率)。

    这里刻意**没有** `dayfirst=True` 那类自由解析：pandas 的 dateutil 兜底会把
    '2024-06-01 00:00:00.123' 读成 2024-01-06（月日对调），把 '12.5' 读成 2001-12-05，
    错得安静——列照样变成时间列，界面上一个错字都没有。认不出就返回 None。
    """
    strs = values.astype("string").str.strip()
    non_empty = int((strs.notna() & (strs != "")).sum())
    if not non_empty:
        return None
    best: tuple[pd.Series, str, int] | None = None
    for fmt in _candidate_formats(strs):
        parsed = _strict_parse_strings(strs, fmt)
        hits = int(parsed.notna().sum())
        if hits and (best is None or hits > best[2]):
            best = (parsed, fmt, hits)
    return (best[0], best[1], best[2] / non_empty) if best else None


def epoch_unit_of(values: pd.Series) -> str | None:
    """数值列只在"每一位都像时间戳"时才认：10 位当秒、13 位当毫秒，别的都是普通数字。

    没有这条闸门，一列叫 time 的浮点读数（12.5 / 13.2）会被当成 epoch 秒，
    整列变成 1970-01-01 08:00:12 这种没人认得出的假时间。
    """
    nums = pd.to_numeric(values, errors="coerce").dropna()
    if nums.empty:
        return None
    if not bool((nums == nums.round().astype("int64")).all()):
        return None
    med = float(nums.median())
    if 1e11 <= med < 1e13:
        return "ms"
    if 1e8 <= med < 1e10:
        return "s"
    return None


def parse_time_column(values: pd.Series, fmt: str) -> pd.Series:
    """按指定源格式把时间列解析成 datetime64（严格模式，解析不动给 NaT）。"""
    if pd.api.types.is_datetime64_any_dtype(values):
        return values
    if pd.api.types.is_numeric_dtype(values):
        arr = pd.to_numeric(values, errors="coerce")
        unit = "ms" if fmt == "epoch_ms" else "s" if fmt == "epoch_s" else None
        if unit:
            return pd.to_datetime(arr, unit=unit, errors="coerce") + timedelta(seconds=_LOCAL_OFFSET)
        guessed = epoch_unit_of(values)
        if guessed:
            return pd.to_datetime(arr, unit=guessed, errors="coerce") + timedelta(seconds=_LOCAL_OFFSET)
        return pd.Series([pd.NaT] * len(values), index=values.index, dtype="datetime64[ns]")
    strs = values.astype("string").str.strip()
    if fmt == "epoch_ms":
        return pd.to_datetime(pd.to_numeric(strs, errors="coerce"), unit="ms", errors="coerce") \
            + timedelta(seconds=_LOCAL_OFFSET)
    if fmt == "epoch_s":
        return pd.to_datetime(pd.to_numeric(strs, errors="coerce"), unit="s", errors="coerce") \
            + timedelta(seconds=_LOCAL_OFFSET)
    if _format_pattern(fmt):
        return _strict_parse_strings(strs, fmt)
    best = best_strict_parse(values)
    return best[0] if best else pd.Series([pd.NaT] * len(values), index=values.index, dtype="datetime64[ns]")


def col_type_of(series: pd.Series) -> str:
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    if pd.api.types.is_numeric_dtype(series):
        return "float"
    return "category"


def detect_freq_minutes(ts: pd.Series) -> int | None:
    """与前端 detectSamplingMinutes 同口径：排序后取正间隔的中位数（分钟，至少 1）。"""
    t = pd.to_datetime(ts, errors="coerce").dropna()
    if len(t) < 2:
        return None
    deltas = t.diff().dt.total_seconds().dropna()
    deltas = deltas[deltas > 0]
    if deltas.empty:
        return None
    return max(1, int(round(float(deltas.median()) / 60)))


def freq_label(minutes: int | None) -> str:
    if not minutes:
        return "未知"
    return f"{minutes} min ({1440 // minutes}点/天)"


# ---------------------------------------------------------------- 载入

def _read_raw_df(content: bytes, fmt: str) -> pd.DataFrame:
    buf = io.BytesIO(content)
    if fmt == "parquet":
        return pd.read_parquet(buf, engine="pyarrow")
    if fmt == "feather":
        return pd.read_feather(buf)
    if fmt in ("xlsx", "xls"):
        return pd.read_excel(buf, engine="openpyxl")
    raw = content
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return pd.read_csv(io.BytesIO(raw), encoding=enc)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(io.BytesIO(raw))


def detect_format(filename: str) -> str:
    name = (filename or "").lower()
    for ext, fmt in ((".parquet", "parquet"), (".feather", "feather"), (".ft", "feather"),
                     (".xlsx", "xlsx"), (".xls", "xls"), (".tsv", "tsv"), (".txt", "txt")):
        if name.endswith(ext):
            return fmt
    return "csv"


def _clean_cell(v):
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return v


def _dedupe_column_names(df: pd.DataFrame) -> pd.DataFrame:
    """去空格并消除重名列：重名列会让按 key 取值的整条链路串味。"""
    seen: dict[str, int] = {}
    names = []
    for c in df.columns:
        base = str(c).strip() or "unnamed"
        if base in seen:
            seen[base] += 1
            base = f"{base}_{seen[base]}"
        else:
            seen[base] = 0
        names.append(base)
    df.columns = names
    return df


def build_meta_columns(df: pd.DataFrame, time_col: str | None) -> list[dict]:
    cols = []
    for c in df.columns:
        cols.append({
            "key": str(c),
            "label": str(c),
            "type": col_type_of(df[c]),
            "isTime": str(c) == str(time_col),
        })
    return cols


def dataframe_from_bytes(content: bytes, filename: str) -> tuple[pd.DataFrame, dict]:
    """解析上传字节 → (DataFrame, 载入期元信息)。时间列转 datetime64，其余保持原样。"""
    fmt = detect_format(filename)
    df = _dedupe_column_names(_read_raw_df(content, fmt))
    if df.shape[1] == 0:
        raise ValueError("文件没有可解析的列")
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(_clean_cell)

    columns = build_meta_columns(df, None)
    time_col, detect, parsed, rejected = pick_time_column(df, columns)
    if time_col is not None:
        # 只有真解析得动的列才会被改写：认不出就整列原样留着，绝不先把中文/数字洗成 NaT
        df[time_col] = pd.to_datetime(parsed, errors="coerce")
        columns = build_meta_columns(df, time_col)
    return df, {"format": fmt, "timeCol": time_col, "timeDetect": detect,
                "timeRejected": rejected, "columns": columns}


def time_col_candidates(columns: list[dict], df: pd.DataFrame) -> list[str]:
    """给出"值得一试"的时间列候选，按优先级排：已是 datetime 的列 > 列名带时间提示的非数值列
    > 列名带时间提示的数值列（只有每一位都像 epoch 的才可能通过下一步闸门）。"""
    hinted = [c["key"] for c in columns if any(h in c["key"].lower() for h in _TIME_NAME_HINTS)]
    datetime_cols = [c["key"] for c in columns if pd.api.types.is_datetime64_any_dtype(df[c["key"]])]
    ordered: list[str] = []
    for key in datetime_cols + [k for k in hinted if k not in datetime_cols]:
        if key not in ordered:
            ordered.append(key)
    return ordered


def try_time_column(values: pd.Series, fmt: str | None = None
                    ) -> tuple[pd.Series, dict] | None:
    """把一列试着当时间列解析：认得出返回 (datetime64 列, 识别回执)，认不出返回 None。

    判定只看一件事——**有多少比例的样本真能按某个严格格式解析出来**。列名像不像时间
    只决定试的顺序，不决定结论，所以中文文本列、普通读数列都不会再被"改名成时间列"。

    `fmt` 是用户在界面上手填的源格式（自动识别不出来时的出路）：只用这一个格式解析，
    命中率照实计算，闸门不放宽——手填错格式就该报"这一列按 X 只解析出 N 个"，
    而不是把一半行洗成 NaT 还自称成功。
    """
    total = int(values.shape[0])
    non_empty = int(values.notna().sum())
    if not non_empty:
        return None
    via = "dtype" if pd.api.types.is_datetime64_any_dtype(values) else "parse"
    display = None
    if fmt:
        via = "manual"
        if pd.api.types.is_datetime64_any_dtype(values):
            # 已经是 datetime64：解析这一步没什么可做的，但用户指定的格式仍要当作显示格式
            parsed, src = values, "YYYY-MM-DD HH:mm:ss"
            display = fmt
        elif fmt in _TIME_UNITS:
            parsed, src = parse_time_column(values, fmt), fmt
        else:
            parsed, src = _strict_parse_strings(values.astype("string").str.strip(), fmt), fmt
    elif pd.api.types.is_datetime64_any_dtype(values):
        parsed, src = values, "YYYY-MM-DD HH:mm:ss"
    elif pd.api.types.is_numeric_dtype(values):
        unit = epoch_unit_of(values)
        if not unit:
            return None
        src = "epoch_ms" if unit == "ms" else "epoch_s"
        parsed = parse_time_column(values, src)
    else:
        best = best_strict_parse(values)
        if not best:
            return None
        parsed, src = best[0], best[1]
    hits = int(pd.to_datetime(parsed, errors="coerce").notna().sum())
    hit_rate = hits / non_empty
    if hit_rate < MIN_TIME_HIT_RATE:
        return None
    vote = vote_time_format([str(v) for v in values.dropna().head(20).tolist()]) or {}
    return pd.to_datetime(parsed, errors="coerce"), {
        "format": src,
        # 源格式可能落在 MM/DD/YYYY 这类不可切换的分支上：解析按源格式，显示退回 ISO
        "displayFormat": display or (src if src in _TOKEN_FORMATS or _format_pattern(src) else "YYYY-MM-DD HH:mm:ss"),
        "confidence": vote.get("confidence") or (100 if fmt else 90),
        # matchRate 一律按整列真实解析数给，界面那句"命中率"和导出行数才对得上同一件事
        "matchRate": hit_rate * 100,
        "matched": hits,
        "sampled": non_empty,
        "totalRows": total,
        "unparsed": non_empty - hits,
        "via": via,
    }


def pick_time_column(df: pd.DataFrame, columns: list[dict] | None = None
                     ) -> tuple[str | None, dict | None, pd.Series | None, list[dict]]:
    """挑时间列：按候选顺序逐个真解析一遍，第一个过闸门的胜出，全不过就是"没有时间列"。

    返回里的 rejected 是**试过又退回**的列与原因，界面要照着念，不能只说"没找到"。
    """
    cols = columns if columns is not None else build_meta_columns(df, None)
    rejected: list[dict] = []
    for key in time_col_candidates(cols, df):
        got = try_time_column(df[key])
        if got:
            return key, got[1], got[0], rejected
        rejected.append({"key": key, "reason": _reject_reason(df[key])})
    return None, None, None, rejected


def _reject_reason(values: pd.Series, fmt: str | None = None) -> str:
    """为什么这一列不能当时间列——只描述真实测到的数，不猜。"""
    non_empty = int(values.notna().sum())
    if not non_empty:
        return "整列没有值"
    if fmt and not pd.api.types.is_datetime64_any_dtype(values):
        if fmt in _TIME_UNITS:
            parsed = parse_time_column(values, fmt)
        else:
            parsed = _strict_parse_strings(values.astype("string").str.strip(), fmt)
        hits = int(pd.to_datetime(parsed, errors="coerce").notna().sum())
        return (f"按「{fmt}」只解析出 {hits}/{non_empty} 个值"
                f"（低于要求的 {MIN_TIME_HIT_RATE * 100:.0f}%）——检查格式串里的分隔符与位数")
    if pd.api.types.is_numeric_dtype(values):
        return f"{non_empty} 个数值都不是 10/13 位时间戳（中位数 {float(pd.to_numeric(values, errors='coerce').median()):g}）"
    best = best_strict_parse(values)
    rate = 0.0 if not best else best[2] * 100
    return f"{non_empty} 个样本里只有 {rate:.0f}% 能按任何已知时间格式解析"


def pick_time_col(columns: list[dict], df: pd.DataFrame) -> str | None:
    """只要列名的调用方（侧表时间列）用这个：认不出就是 None，不再按列名硬给一个。"""
    return pick_time_column(df, columns)[0]


# ---------------------------------------------------------------- 多文件按行合并

def _time_series(df: pd.DataFrame, col: str) -> pd.Series:
    """排序与计数都用这一份 datetime 口径，避免同一列在两处判出不同的 NaT 数。"""
    return pd.to_datetime(df[col], errors="coerce")


def _backjumps(ts: pd.Series) -> int:
    """相邻两行里"后一行的时间反而更早"的位置数（NaT 不参与比较）。"""
    v = ts.dropna().to_numpy()
    if v.size < 2:
        return 0
    return int((v[:-1] > v[1:]).sum())


def merge_frames(parts: list[tuple[str, pd.DataFrame, dict]]) -> tuple[pd.DataFrame, dict]:
    """把多份已解析的表按行拼成一张，并按时间列稳定排序。

    三条口径必须写清楚，因为它们决定界面上的数字能不能对账：
    - 列取**并集**：只在部分文件里出现的列，其余文件的这些格子成为真实缺失（不补值、不丢行）；
    - 排序是**稳定**的：同一时刻的行保持原来的文件先后，合并前后的行数必然相等；
    - 这里**不判重复时间戳**：去重是第②步的一条命令，本次只把重复计数如实写进回执。
    """
    if len(parts) < 2:
        raise ValueError("合并至少需要两份文件（单份请直接走普通导入）")
    if len(parts) > MAX_MERGE_FILES:
        raise ValueError(f"一次最多合并 {MAX_MERGE_FILES} 份文件，本次收到 {len(parts)} 份，请分批导入")

    named = [(str(name), df, meta) for name, df, meta in parts]
    empty = [n for n, df, _ in named if int(df.shape[0]) == 0]
    if empty:
        raise ValueError(f"这些文件解析后没有数据行：{'、'.join(empty)}")

    time_cols = [m.get("timeCol") for _, _, m in named]
    real_cols = {t for t in time_cols if t}
    if len(real_cols) > 1:
        detail = "、".join(f"{n}→{t or '未识别'}" for n, t in zip([x[0] for x in named], time_cols))
        raise ValueError(f"各文件识别到的时间列名不一致，无法按时间排序合并：{detail}")
    if None in time_cols:
        missing = "、".join(n for n, t in zip([x[0] for x in named], time_cols) if not t)
        raise ValueError(
            f"这些文件没识别到时间列，无法按时间排序合并：{missing}"
            f"（其余文件的时间列为 {sorted(real_cols)[0]}；请在文件里把时间列名改成一致，或单独导入后自行处理）")
    time_col = next(iter(real_cols)) if real_cols else None

    dfs = [df for _, df, _ in named]
    df = pd.concat(dfs, ignore_index=True, sort=False)
    if time_col and not pd.api.types.is_datetime64_any_dtype(df[time_col]):
        df[time_col] = _time_series(df, time_col)

    file_cols = [set(str(c) for c in d.columns) for _, d, _ in named]
    all_cols = [str(c) for c in df.columns]
    union_only = [c for c in all_cols if not all(c in s for s in file_cols)]
    # 因"列取并集"而必然为空的格子数：某文件没有这一列，它的每一行在这一列上就是缺失
    gap_cells = sum(int((len(all_cols) - len(s)) * d.shape[0]) for s, (_, d, _) in zip(file_cols, named))

    rows_moved = back_before = back_after = dup_times = time_na = 0
    if time_col:
        ts = df[time_col]
        back_before = _backjumps(ts)
        time_na = int(ts.isna().sum())
        stamp = df.assign(_merge_pos=np.arange(len(df)))
        stamp = stamp.sort_values(time_col, kind="stable", na_position="last")
        pos = stamp["_merge_pos"].to_numpy()
        rows_moved = int((pos != np.arange(len(pos))).sum())
        df = stamp.drop(columns="_merge_pos").reset_index(drop=True)
        back_after = _backjumps(df[time_col])
        dup_times = int(df[time_col].duplicated().sum())

    base = dict(named[0][2])
    base["format"] = "merge"
    base["timeCol"] = time_col
    base["columns"] = build_meta_columns(df, time_col)
    receipt = {
        "fileCount": len(named),
        "files": [
            {"filename": n, "rows": int(d.shape[0]), "cols": int(d.shape[1]),
             "timeFormat": (m.get("timeDetect") or {}).get("displayFormat")}
            for n, d, m in named
        ],
        "totalRows": int(df.shape[0]),
        "colCount": int(df.shape[1]),
        "unionOnlyCols": union_only,
        "gapCells": int(gap_cells),
        "timeCol": time_col,
        "sorted": bool(time_col),
        "sortNote": (f"按「{time_col}」稳定升序（同一时刻保持原文件先后，空时间排在最后）"
                     if time_col else "未排序：各文件都没识别到时间列"),
        "rowsMoved": int(rows_moved),
        "backjumpsBefore": back_before,
        "backjumpsAfter": back_after,
        "duplicateTimes": dup_times,
        "emptyTimes": time_na,
        "timeRange": {
            "start": None, "end": None,
        },
    }
    if time_col and df[time_col].notna().any():
        span = pd.to_datetime(df[time_col], errors="coerce").dropna()
        receipt["timeRange"] = {"start": span.min().isoformat(sep=" "),
                               "end": span.max().isoformat(sep=" ")}
    base["merge"] = receipt
    return df, base


# ---------------------------------------------------------------- 预设数据集（种子化，可重放）

def _round(x, nd):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), nd)


def preset_dataframe(key: str, seed: int | None = None) -> tuple[pd.DataFrame, dict]:
    """把原先散在浏览器里的模拟数据生成搬到 numpy：同一 seed 必然得到同一张表。

    结构与旧版 generateSyntheticData 完全一致（含人为埋的缺失段、尖峰与负值）。
    """
    rng = np.random.default_rng(seed)
    start = datetime(2024, 6, 1, 0, 0, 0)
    if key == "pv":
        n, step_min = 2880, 15
        times = pd.date_range(start, periods=n, freq=f"{step_min}min")
        hours = times.hour + times.minute / 60
        base_irr = np.zeros(n)
        day = (hours >= 6) & (hours <= 19)
        base_irr[day] = np.sin((hours[day] - 6) / 13 * np.pi) * 920 + (rng.random(day.sum()) - 0.5) * 60
        base_irr = np.maximum(base_irr, 0.0)
        power = np.where(base_irr > 0, base_irr * 1.85 + (rng.random(n) - 0.5) * 30, 0.0)
        temp = 20 + np.sin((hours - 4) / 24 * 2 * np.pi) * 10 + (rng.random(n) - 0.5) * 2
        wind = 2.5 + rng.random(n) * 3.5
        weather = np.array(["晴朗", "多云", "少云", "阴天"])[(np.arange(n) // 96) % 4]
        power[380:389] = np.nan          # 人为缺失段（清洗步骤的靶子）
        power[550] = 3100.0              # 尖峰
        power[820] = -80.0               # 负值越界
        df = pd.DataFrame({
            "timestamp": times,
            "active_power": np.round(power, 2),
            "irradiance": np.round(base_irr, 1),
            "temperature": np.round(temp, 1),
            "wind_speed": np.round(wind, 1),
            "weather_type": weather.tolist(),
        })
        meta = {
            "name": "光伏电站实测出力数据 (PV-15min)",
            "unit": "kW",
            "format": "preset",
            "timeCol": "timestamp",
            "timeDetect": {"format": "YYYY-MM-DD HH:mm:ss", "confidence": 98, "matchRate": 100.0, "matched": 20, "sampled": 20},
            "columns": [
                {"key": "timestamp", "label": "时间戳", "type": "datetime", "isTime": True},
                {"key": "active_power", "label": "实际有功出力(kW)", "type": "float", "isMain": True, "unit": "kW"},
                {"key": "irradiance", "label": "斜面总辐照度(W/m²)", "type": "float", "unit": "W/m²"},
                {"key": "temperature", "label": "环境温度(°C)", "type": "float", "unit": "°C"},
                {"key": "wind_speed", "label": "风速(m/s)", "type": "float", "unit": "m/s"},
                {"key": "weather_type", "label": "天气类型", "type": "category"},
            ],
        }
        return df, meta

    if key == "load":
        n, step_min = 720, 60
        times = pd.date_range(start, periods=n, freq=f"{step_min}min")
        # times.hour 是 Index，pandas 2.x 上 np.sin(Index) 仍返回 Index，Index 不许原地赋值
        # （base_load[120] = 890 会抛 "Index does not support mutable operations"），先转成 ndarray。
        hour = np.asarray(times.hour)
        base_load = 350 + np.sin((hour - 3) / 24 * 2 * np.pi) * 80
        base_load = base_load + np.where(((hour >= 9) & (hour <= 11)) | ((hour >= 19) & (hour <= 21)), 110, 0)
        base_load = base_load + (rng.random(n) - 0.5) * 25
        base_load[120] = 890
        temp = 22 + np.sin((hour - 5) / 24 * 2 * np.pi) * 8
        humidity = 60 + (rng.random(n) - 0.5) * 20
        tier = np.where((hour >= 8) & (hour <= 21), "高峰电价", "低谷电价")
        df = pd.DataFrame({
            "timestamp": times,
            "load_demand": [_round(v, 2) for v in base_load],
            "temperature": [_round(v, 1) for v in temp],
            "humidity": [_round(v, 1) for v in humidity],
            "price_tier": tier.tolist(),
        })
        meta = {
            "name": "区域工商业电力负荷数据 (Load-60min)",
            "unit": "MW",
            "format": "preset",
            "timeCol": "timestamp",
            "timeDetect": {"format": "YYYY-MM-DD HH:mm:ss", "confidence": 98, "matchRate": 100.0, "matched": 20, "sampled": 20},
            "columns": [
                {"key": "timestamp", "label": "时间戳", "type": "datetime", "isTime": True},
                {"key": "load_demand", "label": "总负荷需求(MW)", "type": "float", "isMain": True, "unit": "MW"},
                {"key": "temperature", "label": "室外气温(°C)", "type": "float", "unit": "°C"},
                {"key": "humidity", "label": "相对湿度(%)", "type": "float", "unit": "%"},
                {"key": "price_tier", "label": "分时电价区间", "type": "category"},
            ],
        }
        return df, meta

    raise ValueError(f"未知预设数据集：{key}（可选 pv / load）")


# ---------------------------------------------------------------- 工作区

@dataclass
class Workspace:
    id: str
    base_df: pd.DataFrame
    base_meta: dict            # 载入期的元信息，重放前先复位，保证回到同一版本得到同一结果
    meta: dict
    source: dict
    df: pd.DataFrame = None
    ops: list[dict] = field(default_factory=list)
    # cursor = 当前帧重放到第几条命令。撤销只是把游标往回挪（ops 尾部留着，重做要再放一遍）；
    # 若在回退之后又执行新命令，apply() 才把尾部截掉（和编辑器的 undo/redo 同一语义）。
    cursor: int = 0
    # 异常检测不是加工命令（它不改数据），但"修复"必须按检测时的行索引执行，
    # 所以检测结果留在工作区里，并用 value_epoch 标记它是否还对应当前帧。
    anomaly: dict | None = None
    value_epoch: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = ""
    # 日志落盘失败只降级为「历史不跨重启」，不能让一条已经执行成功的命令假装报错返回：
    # 版本号已经涨了，此时回 4xx 会让界面与后端的版本对不上。失败原因记在这里，由 meta 透出去。
    log_error: str = ""
    # 撤销重放的帧缓存：版本号 → {"df","meta","epoch","bytes"}，机制见 _remember 的说明。
    snaps: dict = field(default_factory=dict)
    snap_bytes: int = 0
    # 最近一次 restore 的真实代价（从哪一版起步、重放了几条命令），界面与验收脚本读它。
    restore_trace: dict = field(default_factory=dict)
    # 这颗帧自己的读写锁：执行命令、撤销重放、异常检测与所有整表读取都在它里面跑。
    # 用 RLock 是因为 restore 会被 apply 在同一线程里再调（重放路径），且路由层的读
    # 包装器先进锁、服务方法里再进一次；换成普通 Lock 就是自己锁死自己。
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # 整帧派生值的缓存（time_labels / memory_bytes）：见各自的方法说明。
    # 两颗缓存都只装「当前这一颗帧」的结果，槽位互相覆盖，不留历史。
    _labels_frame: object = field(default=None, repr=False)
    _labels_key: tuple = field(default=(), repr=False)
    _labels_val: list = field(default_factory=list, repr=False)
    _mem_frame: object = field(default=None, repr=False)
    _mem_bytes: int = 0

    def __post_init__(self):
        self.df = self.base_df
        self.updated_at = self.created_at

    def _frame_alive(self, ref) -> bool:
        """缓存里那颗帧还是当前帧吗。

        用 weakref 而不是 id() 比：DataFrame 被回收后 id 会被新帧复用，那种「缓存命中到
        别的帧」是数字全对、只是错得没有规律；存强引用又等于多养一帧（大表几十 MB）。
        帧死了 ref 返回 None，比不过 self.df，缓存自然失效。
        """
        return ref is not None and ref() is self.df

    # ---- 列分类 ----
    def float_columns(self) -> list[dict]:
        return [c for c in self.meta["columns"] if c["type"] == "float"]

    def source_float_columns(self) -> list[dict]:
        """原始数值列：不含第五步衍生出的特征列。

        缺失诊断、填补、去重、异常检测四条通道都只认这个口径。原因是特征列的空是"结构性的"
        （lag_t1 第 0 行、diff1 首行本就为 null），一旦被当成缺失值填补，特征本身就废了；
        浏览器旧实现里特征从不进列注册表，所以这些扫描天然看不到它，这里显式保持同一口径。
        """
        return [c for c in self.float_columns() if not c.get("feature")]

    def time_labels(self) -> list:
        """当前帧时间列的渲染标签，带一颗「跟着帧走」的缓存。

        一次整列渲染实测 7 ms（2880×6）/ 26 ms（11000×40），而曲线、质量快照、导出、
        重复行定位这些入口各自都要一份完整标签：界面停在同一步连着发几个请求，算的就是
        同一颗帧的同一段标签。

        缓存键 = 帧身份（_frame_alive）+ 时间列 + 显示格式 + 行数，外面再套版本号与数值世代
        两道保险：改既有值的命令必定推进 valueEpoch，改显示配置（时间列、格式）的命令必定
        推进版本号。留着它们不是冗余——撤销重放那条路是"先把草稿帧挂上 self.df、再逐条就地
        放"，中途游标还停在旧版，只靠帧身份认不出这种就地改过的帧。

        返回的是缓存里那份列表，调用方只读；要写回列得先有自己的帧
        （export_dataframe 就是先 df.copy() 再赋值，别照它改这里）。
        """
        fmt = self.meta.get("timeFormat") or "YYYY-MM-DD HH:mm:ss"
        col = self.time_col
        has_col = bool(col) and col in self.df.columns
        key = (self.cursor, self.value_epoch, col if has_col else None, fmt, int(self.df.shape[0]))
        if self._labels_key == key and self._frame_alive(self._labels_frame):
            return self._labels_val
        if has_col:
            labels = render_times(self.df[col], fmt)
        else:
            labels = [str(i) for i in range(int(self.df.shape[0]))]
        self._labels_frame = weakref.ref(self.df)
        self._labels_key = key
        self._labels_val = labels
        return labels

    def memory_bytes(self) -> int:
        """当前帧的内存占用（字节）。

        memory_usage(deep=True) 要把每个 object 单元格真的数一遍，宽表上是整毫秒级；
        meta 和 overview 每次都报它，所以按帧身份缓存。只认帧对象本身：它的内容只会随
        apply/restore 换帧而变（那条路径一律 deep copy 出新的再赋值，不就地改当前帧），
        同一颗对象的字节数不会自己变。
        """
        if self._mem_bytes and self._frame_alive(self._mem_frame):
            return self._mem_bytes
        self._mem_frame = weakref.ref(self.df)
        self._mem_bytes = int(self.df.memory_usage(deep=True).sum())
        return self._mem_bytes

    def holiday_days(self) -> list[str]:
        """当前生效的节假日表：没配置过就是内置预设，配置过就是用户那份（空表也算配置过）。"""
        days = self.meta.get("holidayDays")
        if days is None:
            return sorted(features.HOLIDAY_PRESETS[features.DEFAULT_HOLIDAY_YEAR])
        return sorted(days)

    # ---- 元信息 ----
    @property
    def time_col(self) -> str | None:
        return self.meta.get("timeCol")

    @property
    def cell_count(self) -> int:
        return int(self.df.shape[0] * self.df.shape[1])

    @property
    def version(self) -> int:
        """当前帧对应的版本号 = 重放游标，而不是日志长度（撤销后两者不同）。"""
        return self.cursor

    @property
    def applied_ops(self) -> list[dict]:
        """构成当前帧的那段命令（重做尾部不算，界面与导出都不能看见它）。"""
        return self.ops[:self.cursor]

    @property
    def pending_ops(self) -> list[dict]:
        """游标之后那段「已撤销、等待重做」的命令尾巴。"""
        return self.ops[self.cursor:]

    # ---- 命令日志落盘 ----
    def log_document(self) -> dict:
        m = self.meta
        return {
            "v": state_store.LOG_VERSION,
            "wsId": self.id,
            "source": self.source,
            "cursor": self.cursor,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at or self.created_at,
            # 只存列表页要的摘要字段：整份 meta（列注册表）由重放重建，存两份必然漂移
            "meta": {"name": m.get("name"), "timeCol": self.time_col,
                     "timeFormat": m.get("timeFormat"), "rowCount": int(self.df.shape[0]),
                     "colCount": int(self.df.shape[1])},
            "ops": _log_jsonable(self.ops),
        }

    def persist_log(self) -> None:
        """把命令日志写到 state 目录。失败不抛：见 `log_error` 字段的说明。"""
        try:
            state_store.save_workspace_log(self.id, self.log_document())
            self.log_error = ""
        except Exception as exc:
            self.log_error = f"{type(exc).__name__}: {exc}"

    def history_view(self) -> dict:
        """撤销/重做的状态由服务端给出：游标与日志长度就是那两个按钮的全部依据。"""
        applied, pending = self.applied_ops, self.pending_ops
        return {
            "opsTotal": len(self.ops),
            "canUndo": self.cursor > 0,
            "canRedo": bool(pending),
            "undoLabel": _op_label(applied[-1]) if applied else "",
            "redoLabel": _op_label(pending[0]) if pending else "",
            "redoTail": [{"index": self.cursor + i, **_op_brief(o)} for i, o in enumerate(pending)],
        }

    def meta_view(self) -> dict:
        # 回出去的必须是一份快照：调用方（路由）序列化 JSON 时已经放开 ws.lock，
        # 下一条命令的 rebuild_meta_columns 会就地改列字典的 type/label。直接给 m["columns"]
        # 就是让界面读到半新半旧的列注册表。
        m = copy.deepcopy(self.meta)
        return {
            "wsId": self.id,
            "name": m.get("name") or "workspace",
            "format": m.get("format") or "csv",
            "unit": m.get("unit") or "",
            "timeCol": self.time_col,
            "columns": m["columns"],
            "rowCount": int(self.df.shape[0]),
            "colCount": int(self.df.shape[1]),
            "freqMinutes": m.get("freqMinutes"),
            "freqLabel": m.get("freqLabel") or "未知",
            "timeFormat": m.get("timeFormat") or "YYYY-MM-DD HH:mm:ss",
            "timeDetect": m.get("timeDetect"),
            # 载入时"名字像时间列却解析不动"的那些列与真实原因：界面没有识别到时间列时，
            # 要能回答"为什么没有"，而不是只留一句"未识别"。
            "timeRejected": m.get("timeRejected") or [],
            "derivedCols": m.get("derivedCols", []),
            "version": self.version,
            # valueEpoch = 到目前为止改动了既有数值的命令条数（新增列、只改配置的命令不推进它）。
            # version 每执行一条命令都前进，所以「第④步的整表扫描要不要重来」不能看 version，
            # 只能看这颗帧的数值有没有真的变过——界面据此把缓存的失效粒度收到后端这一份计数上。
            "valueEpoch": self.value_epoch,
            # 撤销重放用的帧缓存此刻装着哪几版：决定下一次撤销是从载入帧整段重放还是就近起步
            "frameCache": {"versions": sorted(self.snaps), "bytes": self.snap_bytes,
                           "maxVersions": SNAP_MAX_VERSIONS, "maxBytes": SNAP_MAX_BYTES},
            # 上一次撤销/重做的真实代价（从哪一版起步、重放了几条命令），界面把它的数字念出来
            "restoreTrace": dict(self.restore_trace) or None,
            "anomaly": self.anomaly_summary(),
            # 撤销/重做是服务端游标的属性：按钮能不能点、下一次叫什么，全部由这里给
            **self.history_view(),
            "logError": self.log_error or None,
            "ops": [
                {"index": i, "kind": o["kind"], "label": _op_label(o),
                 "params": _op_params_view(o),
                 "summary": o.get("summary", ""), "at": o.get("at", "")}
                for i, o in enumerate(self.applied_ops)
            ],
            "source": dict(self.source),
            # 合并导入的回执（几份文件、各自行数、并集列、排序前后的真实计数）。
            # 它描述的是"这颗帧怎么来的"，不随后续命令变化，所以界面上要按导入回执念，
            # 别拿它的 totalRows 当作当前行数（当前行数只看 rowCount）。
            "merge": m.get("merge"),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at or self.created_at,
            "memoryBytes": self.memory_bytes(),
            "cellCount": self.cell_count,
        }

    # ---- 行窗口 ----
    def rows(self, offset: int, limit: int) -> dict:
        """一页窗口：`limit` 是**页大小**（调用方要多少），`returned` 才是这一页实际几行。

        末页天然装不满：把 returned 回写成 limit，界面那个"20/50/100/200/500 行/页"的
        下拉就会对不上任何一项（显示成未选中），翻页步长也会跟着变成末页那几行。
        """
        n = int(self.df.shape[0])
        offset = max(0, min(int(offset or 0), n))
        requested = max(1, min(int(limit or DEFAULT_PAGE), MAX_PAGE))
        limit = requested
        chunk = self.df.iloc[offset:offset + limit]
        keys = [str(c) for c in chunk.columns]
        rendered: list[list] = []
        for c in chunk.columns:
            if str(c) == str(self.time_col):
                rendered.append(render_times(chunk[c], self.meta.get("timeFormat") or "YYYY-MM-DD HH:mm:ss"))
            else:
                rendered.append([_jsonable(v) for v in chunk[c].tolist()])
        rows = [[col[i] for col in rendered] for i in range(len(chunk))]
        return {"wsId": self.id, "version": self.version, "offset": offset,
                "limit": limit, "returned": len(rows), "total": n, "columns": keys, "rows": rows}

    def export_dataframe(self) -> pd.DataFrame:
        """导出用的帧：时间列按当前显示格式渲染成字符串，其余保持服务端 dtype。

        导出的时间串必须和界面预览里看到的完全一致，否则用户拿 CSV 一比对就会发现两边不同。
        """
        df = self.df
        if self.time_col and self.time_col in df.columns:
            df = df.copy()
            df[self.time_col] = self.time_labels()
        return df

    def column_values(self, keys: list[str], max_rows: int | None = None) -> dict:
        """整列取数：过渡期通道，给尚未迁到服务端的算法与图表用。"""
        out = {}
        for k in keys:
            if k not in [str(c) for c in self.df.columns]:
                raise ValueError(f"列不存在：{k}")
            s = self.df[k]
            if str(k) == str(self.time_col):
                vals = render_times(s, self.meta.get("timeFormat") or "YYYY-MM-DD HH:mm:ss")
            else:
                vals = [_jsonable(v) for v in s.tolist()]
            if max_rows:
                vals = vals[: int(max_rows)]
            out[k] = vals
        return {"wsId": self.id, "rowCount": int(self.df.shape[0]), "columns": out}

    # ---- 统计概览 ----
    def overview(self) -> dict:
        """第二步那张概览卡：行数/列数、缺失、重复时间戳、采样频率与时间范围。

        这里的 missingRate 只按**原始数值列**（source_float_columns）算：类别列与时间列
        不进分子，第五步特征列也不进。第四步 quality 那个同名字段的分子却把时间列解析不动、
        类别列空串都算成缺，参与列也更多。两个数的分子与分母都不一样，谁也不比谁小
        （一列全空的表在这里是 100%，在第四步可能被几列干净的类别列摊薄），
        所以各自随响应给出 missingDenominator，界面按各自的分母念，不跨步比较。
        """
        df = self.df
        n = int(df.shape[0])
        # 与 quality/impute/检测同一口径：只数原始数值列。
        # 第五步的特征列前若干行必然为 null（窗口还没盖到），算进缺失率就是把定义当成脏数据。
        float_cols = [c["key"] for c in self.source_float_columns()]
        missing_cells = int(sum(int(df[k].isna().sum()) for k in float_cols if k in df.columns))
        total_cells = n * len(float_cols)
        dup = 0
        time_na = None
        tmin = tmax = None
        if self.time_col and self.time_col in df.columns:
            ts = pd.to_datetime(df[self.time_col], errors="coerce")
            dup = int(ts.duplicated().sum())
            time_na = int(ts.isna().sum())
            if ts.notna().any():
                tmin, tmax = ts.min(), ts.max()
        return {
            "wsId": self.id,
            "version": self.version,
            "rowCount": n,
            "colCount": int(df.shape[1]),
            "floatCols": float_cols,
            "missingCells": missing_cells,
            "missingDenominator": total_cells,
            "missingRate": (missing_cells / total_cells * 100) if total_cells else 0.0,
            "duplicateRows": dup,
            "duplicateRate": (dup / n * 100) if n else 0.0,
            "freqMinutes": self.meta.get("freqMinutes"),
            "freqLabel": self.meta.get("freqLabel"),
            "timeFormat": self.meta.get("timeFormat"),
            "unparsedTimes": time_na,
            "timeRange": {
                "start": tmin.isoformat(sep=" ") if tmin is not None and pd.notna(tmin) else None,
                "end": tmax.isoformat(sep=" ") if tmax is not None and pd.notna(tmax) else None,
            },
            "cellCount": self.cell_count,
            "memoryBytes": self.memory_bytes(),
        }

    # ---- 质量诊断（第四步）----
    def quality(self) -> dict:
        """各列缺失统计 + 数值列缺失段明细 + 重复时间戳计数。

        第五步的特征列不进这个口径：它们的 null 是定义的一部分（lag/diff 的开头几行），
        把它们列进缺失诊断就等于邀请用户去填补，而填补会把特征本身抹掉。
        """
        snap = quality.quality_snapshot(
            self.df, [c for c in self.meta["columns"] if not c.get("feature")],
            self.time_col, self.time_labels())
        # valueEpoch 一起回：界面按「这份快照属于哪一代数值」缓存，而不是按版本号——
        # 版本号每执行一条命令都前进（生成特征也前进），但那些命令一改这份快照的内容。
        return {"wsId": self.id, "version": self.version, "valueEpoch": self.value_epoch, **snap}

    # ---- 异常检测缓存（检测改数据之外的第四步计算）----
    def anomaly_stale(self) -> bool:
        return bool(self.anomaly is not None and self.anomaly.get("epoch") != self.value_epoch)

    def anomaly_summary(self) -> dict | None:
        if self.anomaly is None:
            return None
        return {
            "algo": self.anomaly.get("algo"), "at": self.anomaly.get("at"),
            "epoch": self.anomaly.get("epoch"), "stale": self.anomaly_stale(),
            "totalAnomalies": self.anomaly.get("summary", {}).get("totalAnomalies"),
        }

    def anomaly_view(self) -> dict:
        return {"wsId": self.id, "version": self.version,
                "detection": quality.detection_view(self.anomaly, self.anomaly_stale())}

    def run_detection(self, algo: str, expr: str | None = None, params: dict | None = None) -> dict:
        cols = self.source_float_columns()
        if not cols:
            raise ValueError("当前工作区没有数值列可检测")
        with self.lock:
            detection = quality.detect(self.df, cols, algo, expr, params)
            detection["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            detection["epoch"] = self.value_epoch
            detection["version"] = self.version
            detection["stale"] = False
            self.anomaly = detection
            self.updated_at = detection["at"]
            return quality.detection_view(detection, False)

    # ---- 加工命令 ----
    # ---- 撤销重放的帧缓存 ----
    # restore(v) 的朴素写法是「从载入帧重放 ops[:v]」：撤销一步也要把前面所有命令重跑一遍，
    # 数据量一大就是 O(版本数 × 整帧)，这正是这次 Python 化要解决的开销。这里留住最近访问过的
    # 几版帧，重放就近起步：
    #   · apply() 不会就地改旧帧（新命令拿的是它的深拷贝），所以给「执行前那一版」留档零拷贝，
    #     只是多握一个引用；撤销一步因此就是换个指针；
    #   · 从缓存往回重放前先复制那一版，缓存自身永远干净；
    #   · 长距离重放（≥ SNAP_CHECKPOINT_GAP 条）在中途补一颗检查点，继续往前翻不必再从头放；
    #   · 只缓存够小的帧（按 dtype 字节数，见 SNAP_MAX_BYTES）：超大帧退回整段重放，
    #     宁可慢也不能让缓存变成第二份内存压力。
    @staticmethod
    def _frame_bytes(frame: pd.DataFrame) -> int:
        # deep=False：只按 dtype 求和，object 列会低估，但这是每条命令都要问一次的数字，不能贵
        return int(frame.memory_usage(index=False, deep=False).sum())

    def _remember(self, version: int, frame: pd.DataFrame, meta: dict, epoch: int,
                  near: int | None = None) -> None:
        size = self._frame_bytes(frame)
        if size > SNAP_MAX_BYTES:
            return
        # 先摘再插 = 移到队尾：新摸到的那一版永远是最新的一份，淘汰时才不会被它自己挤掉
        old = self.snaps.pop(version, None)
        if old is not None:
            self.snap_bytes -= old["bytes"]
        # handler 会就地改 self.meta（rebuild_meta_columns），所以留档必须单独抄一份。
        # 帧本身不抄：调用方要么给的是刚复制出来的私有帧，要么是没人再改的旧帧。
        self.snaps[version] = {"df": frame, "meta": copy.deepcopy(meta), "epoch": epoch, "bytes": size}
        self.snap_bytes += size
        # near = 这一次围绕哪一版在忙。重放中途补检查点时游标还没挪过去，
        # 拿旧游标算距离会把刚补的那颗当场淘汰掉（实测就会这样），所以由调用方指定。
        self._trim_snaps(self.cursor if near is None else near)

    def _trim_snaps(self, near: int) -> None:
        """淘汰：离 `near` 最远的那一版先走。

        撤销/重做一步通常就在游标 ±1 里，留着它才是热的那几颗；中途补的检查点离得远，
        真要用时（翻很远）它的价值才体现出来，所以按"离参照版本的距离"淘汰而不是按插入顺序。
        """
        while len(self.snaps) > SNAP_MAX_VERSIONS or self.snap_bytes > SNAP_MAX_BYTES:
            if not self.snaps:
                return
            # 距离最远的那版先走；同样远时摘旧的一版 —— 游标通常往前翻（重做），留着新的更划算
            victim = max(self.snaps, key=lambda v: (abs(v - near), -v))
            self.snap_bytes -= self.snaps.pop(victim)["bytes"]

    def _snap_up_to(self, version: int) -> tuple[int, dict | None]:
        """≤ version 里最靠近的那一版缓存（命令不可逆，重放只能往前走）。"""
        candidates = [v for v in self.snaps if v <= version]
        if not candidates:
            return 0, None
        src = max(candidates)
        return src, self.snaps[src]

    def _forget_snaps_after(self, version: int) -> None:
        """回退后又执行新命令时，游标之后那段日志被截掉，属于它的那些帧缓存从此不再成立。"""
        for v in [k for k in self.snaps if k > version]:
            self.snap_bytes -= self.snaps.pop(v)["bytes"]

    def _execute(self, frame: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, dict]:
        handler = _OPS.get(op["kind"])
        if handler is None:
            raise ValueError(f"不支持的加工命令：{op['kind']}")
        # op["replay"]：命令当时执行所依赖的服务端上下文（如异常检测的逐列行索引）。
        # 它不进 HTTP 响应、也不进 meta.ops，只在按日志重放（撤销/重做）时回流给 handler，
        # 否则重放到那条命令时缓存已被清空，就会拿"当时的索引"去撞"现在的新鲜度闸门"。
        params = dict(op.get("params") or {})
        params.update(op.get("replay") or {})
        result = handler(self, frame, params)
        new_frame = result.pop("_frame", None)
        if "_replay" in result:
            result["_replayPayload"] = result.pop("_replay")
        return (frame if new_frame is None else new_frame), result

    def apply(self, op: dict) -> dict:
        """执行一条命令并记进 ops，返回 {result, version, meta}。"""
        with self.lock:
            # 游标之后还留着「被撤销、等待重做」的那段日志：新命令会把当前帧改到另一条分支上，
            # 那条尾巴从此不再成立，先截掉再记新的（那几版的帧缓存也一起作废）。
            if self.cursor < len(self.ops):
                del self.ops[self.cursor:]
                self._forget_snaps_after(self.cursor)
            prev_version, prev_frame, prev_epoch = self.cursor, self.df, self.value_epoch
            # 旧帧接下来不会被任何人改动（新命令拿的是深拷贝），所以能零拷贝留档；
            # 它的 meta 却会被就地改，要提前抄一份才配得上「执行前那一版」。
            cacheable = self._frame_bytes(prev_frame) <= SNAP_MAX_BYTES
            prev_meta = copy.deepcopy(self.meta) if cacheable else None
            frame, result = self._execute(self.df.copy(deep=True), op)
            self.df = frame
            # _valueChange：命令是否改动了既有数值。掩码列只是新增一列，行列位置不变，
            # 因此不能把上一次检测的索引作废（界面允许"检测 → 生成掩码 → 再截断"连着做）。
            if result.pop("_valueChange", True):
                self.value_epoch += 1
            # 要回写的参数（seed、侧表推导出的 targets 之类）：handler 拿到的是 _execute 的
            # params 副本，就地改回不到日志，只能经这个通道显式交回来。日志里没有它们，
            # 淘汰/重启后重放就会换一个 seed 或换一批列名，界面上的数字不再可复现。
            pinned = result.pop("_pinParams", None)
            record = {
                "kind": op["kind"],
                "params": {**(op.get("params") or {}), **(pinned or {})},
                "summary": result.get("summary", ""),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "epoch": self.value_epoch,
            }
            replay = result.pop("_replayPayload", None)
            if replay:
                record["replay"] = replay
            self.ops.append(record)
            self.cursor = len(self.ops)
            self.updated_at = record["at"]
            if self.anomaly is not None and self.anomaly_stale():
                self.anomaly["stale"] = True
            # 两版都留：撤销一步直接换回执行前那颗帧，重做回来时同样不用重放
            if cacheable:
                self._remember(prev_version, prev_frame, prev_meta, prev_epoch)
            self._remember(self.version, self.df, self.meta, self.value_epoch)
            # 先落盘再回 meta：logError 要一起带出去
            self.persist_log()
            result["version"] = self.version
            result["meta"] = self.meta_view()
            return result

    def restore(self, version: int) -> dict:
        """把当前帧重放到第 version 个版本：从最近的帧缓存起步，没有缓存才回到载入帧。

        必须可逆：撤销把游标往回挪、重做把它往回推，两种情况都从同一份日志重放，
        日志本身不截断（截断会让重做永远拿不到那一版，界面上就是一个报错的按钮）。
        真正丢弃尾巴的时机是"回退后又执行了新命令"，那由 apply() 负责。

        重放中途出错时一位都不能落：先在局部变量里放完，全部成功才替换当前帧。
        否则报错之后界面还停在旧版本、后端帧却已被半重放，之后每个数字都是错的。
        """
        version = int(version)
        if version < 0 or version > len(self.ops):
            raise ValueError(f"版本号越界：{version}（日志共 {len(self.ops)} 条，当前第 {self.cursor} 版）")
        with self.lock:
            src_version, snap = self._snap_up_to(version)
            backup = (self.meta, self.df, self.value_epoch, self.cursor, self.anomaly)
            replayed = 0
            checkpoint_at = None
            checkpoint_laid = None
            try:
                if snap is not None and src_version == version:
                    # 命中：这一版的帧就在缓存里，换个指针，一条命令都不用重放。
                    # meta 必须复制：下一次 apply 会就地改 self.meta，缓存那份要留着下次换回来。
                    self.meta = copy.deepcopy(snap["meta"])
                    self.df = snap["df"]
                    self.value_epoch = snap["epoch"]
                else:
                    if snap is None:
                        src_frame, src_meta, src_epoch = self.base_df, self.base_meta, 0
                    else:
                        src_frame, src_meta, src_epoch = snap["df"], snap["meta"], snap["epoch"]
                    # 重放是就地改帧的，所以先把起步那一版复制出来；缓存与载入帧都不碰
                    meta = copy.deepcopy(src_meta)
                    frame = src_frame.copy(deep=True)
                    # handler 会就地改 self.meta（rebuild_meta_columns），所以重放期间先挂上草稿
                    self.meta, self.df, self.value_epoch = meta, frame, src_epoch
                    gap = version - src_version
                    # 长距离重放中途补一颗检查点：继续往前翻版本就不用再从头放一遍
                    checkpoint_at = src_version + gap // 2 if gap >= SNAP_CHECKPOINT_GAP else None
                    # 快到站的那几版顺手留档：连着往回撤销是最常见的动作，只存目的地的话
                    # 下一步又要从载入帧整段重放，一路退到底就是 O(版本数²)
                    tail_from = version - (SNAP_MAX_VERSIONS - 1)
                    for o in self.ops[src_version:version]:
                        frame, result = self._execute(self.df, o)
                        self.df = frame
                        replayed += 1
                        if result.get("_valueChange", True):
                            self.value_epoch += 1
                        v_now = src_version + replayed
                        if checkpoint_at == v_now:
                            self._remember(checkpoint_at, self.df.copy(deep=True), self.meta,
                                           self.value_epoch, near=version)
                            checkpoint_laid = checkpoint_at
                            checkpoint_at = None   # 一次重放只补一颗，别把缓存全吃在别人身上
                        elif tail_from <= v_now < version:
                            # 这一版之后还会被就地改，所以留档要自己抄一份
                            self._remember(v_now, self.df.copy(deep=True), self.meta,
                                           self.value_epoch, near=version)
            except Exception as exc:
                self.meta, self.df, self.value_epoch, self.cursor, self.anomaly = backup
                raise ValueError(f"重放到版本 {version} 失败，已回到原来的帧：{exc}") from exc
            self.cursor = version
            # 检测索引是按某一颗帧算的，回退后一律要求重测（界面据此把结果标成失效）
            self.anomaly = None
            self.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._remember(version, self.df, self.meta, self.value_epoch)
            # 这一次撤销到底花了多少真功夫：命中缓存还是要重放、从哪一版起步
            self.restore_trace = {
                "requested": version, "fromVersion": src_version, "replayedOps": replayed,
                # 命中 = 目的地那一版就在缓存里；从载入帧起步放回 0 条命令不算命中（那是本来就不用放）
                "cacheHit": snap is not None and replayed == 0,
                "fromBase": snap is None,
                "checkpointAt": checkpoint_laid,
                "cachedVersions": sorted(self.snaps), "snapBytes": self.snap_bytes,
            }
            # 游标位置本身也是历史的一部分：重启后要回到同一版，靠的就是落盘的 cursor
            self.persist_log()
            return self.meta_view()

    def rebuild_meta_columns(self, frame: pd.DataFrame) -> None:
        """DataFrame 列变了（新增/改名/删除）后，把 meta.columns 与实际列对齐。

        注意必须显式传帧：命令 handler 改的是 apply() 复制出来的 work，
        此时 self.df 还是上一版，读 self.df 会把新增列漏掉（实测踩过）。
        """
        old = {c["key"]: c for c in self.meta["columns"]}
        cols = []
        for c in frame.columns:
            prev = old.get(str(c))
            if prev:
                # 掩码列是 0/1 标记，不是观测量：让它跟着 dtype 变回 float 会被异常检测当成数据列扫
                if prev.get("type") != "binary":
                    prev["type"] = col_type_of(frame[c])
                prev["isTime"] = str(c) == str(self.time_col)
                cols.append(prev)
            else:
                cols.append({"key": str(c), "label": str(c), "type": col_type_of(frame[c]),
                             "isTime": str(c) == str(self.time_col)})
        self.meta["columns"] = cols

    def refresh_freq(self, frame: pd.DataFrame) -> None:
        if self.time_col and self.time_col in frame.columns:
            minutes = detect_freq_minutes(frame[self.time_col])
            self.meta["freqMinutes"] = minutes
            self.meta["freqLabel"] = freq_label(minutes)


# ---- 命令实现：每个 handler 就地改写 work（DataFrame），并返回给前端的 result

def _require_col(ws: Workspace, work: pd.DataFrame, key: str) -> str:
    names = [str(c) for c in work.columns]
    if key in names:
        return key
    raise ValueError(f"列不存在：{key}（现有列：{'、'.join(names[:12])}）")


def _op_set_time_format(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    fmt = normalize_format(p.get("format") or "", p.get("customFormat"))
    if not ws.time_col or ws.time_col not in work.columns:
        raise ValueError("本工作区没有可用的时间列，无法转换时间格式："
                         "先在「时间列」里指定一列能解析成时间的数据（点「识别格式」看结果）")
    old = ws.meta.get("timeFormat") or "YYYY-MM-DD HH:mm:ss"
    # 同一个格式再提交一次（界面重复点、重放同一条命令）：整列渲染两遍要 52 ms，
    # 而"变了几个格子"在这种情况下的答案就是 0，不必真的去比。
    if old == fmt:
        changed = 0
    else:
        changed = sum(1 for a, b in zip(render_times(work[ws.time_col], old),
                                        render_times(work[ws.time_col], fmt)) if a != b)
    ws.meta["timeFormat"] = fmt
    return {"summary": f"时间格式 {old} → {fmt}", "changed": changed, "format": fmt,
            "timeCol": ws.time_col, "rowCount": int(work.shape[0]), "_valueChange": False}


def time_detect(ws: Workspace, work: pd.DataFrame | None, key: str, fmt: str | None = None) -> dict:
    """按整列真解析一次，回答"这一列能不能当时间列、按什么格式、多少个样本解析动了"。

    浏览器只拿到几个样本回显，判定全部在这里发生：一页数据推不出整表结论。
    `fmt`（已 normalize 过的模板或 epoch_*）给定时按用户指定的源格式解析，
    这是自动识别认不出时的出路——命中率照样照实报。
    """
    frame = work if work is not None else ws.df
    names = [str(c) for c in frame.columns]
    if key not in names:
        raise ValueError(f"列不存在：{key}（现有列：{'、'.join(names[:12])}）")
    series = frame[key]
    got = try_time_column(series, fmt)
    total = int(series.shape[0])
    non_empty = int(series.notna().sum())
    if not got:
        return {"col": key, "ok": False, "reason": _reject_reason(series, fmt),
                "requestedFormat": fmt,
                "totalRows": total, "nonEmpty": non_empty, "parsedCount": 0,
                "matchRate": 0.0, "format": None, "displayFormat": None,
                "samplesBefore": [str(v) for v in series.dropna().head(3).tolist()],
                "samplesAfter": [], "isCurrent": key == ws.time_col}
    parsed, detect = got
    # dtype 分支给不出任何来源信息（只会回一句 ISO 占位）。这一列如果是当前时间列，
    # 上传/指定时按真实源串测出的格式要留着，否则界面会同时念出
    # 「按 YYYY-MM-DD HH:mm:ss 解析出 12/12」和「当前显示 YYYY-MM-DD HH:mm:ss.SSS」两句话。
    prev = ws.meta.get("timeDetect") or {}
    if (not fmt and detect.get("via") == "dtype" and key == ws.time_col
            and prev.get("displayFormat")):
        detect = {**detect, "format": prev.get("format") or detect["format"],
                  "displayFormat": prev["displayFormat"],
                  "confidence": prev.get("confidence", detect["confidence"])}
    src_fmt = detect["format"]
    samples = series.dropna().head(3)
    before = [str(v) for v in samples.tolist()]
    after = render_times(parsed.head(3), detect["displayFormat"])
    return {"col": key, "ok": True, "reason": "", "requestedFormat": fmt,
            "totalRows": total, "nonEmpty": non_empty,
            "parsedCount": int(detect["matched"]), "matchRate": round(float(detect["matchRate"]), 2),
            "unparsed": int(detect.get("unparsed") or 0),
            "format": src_fmt, "displayFormat": detect["displayFormat"],
            "confidence": detect["confidence"], "via": detect["via"],
            "samplesBefore": before, "samplesAfter": [str(v) for v in after],
            "isCurrent": key == ws.time_col}


def _op_set_time_col(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    """把某一列指定为时间列：整列真解析得过才改，改完时间列就是 datetime64。

    与"换显示格式"是两件事，所以是独立一条命令：撤销它要连列的 dtype 一起退回去，
    而换格式只动 meta["timeFormat"]。
    自动识别认不出时，调用方可以带 `format`/`customFormat` 指定源格式再试一次。
    """
    key = _require_col(ws, work, p.get("key") or "")
    fmt = normalize_format(p.get("format") or "", p.get("customFormat")) if p.get("format") else None
    got = try_time_column(work[key], fmt)
    if not got:
        raise ValueError(f"「{key}」不能当时间列：{_reject_reason(work[key], fmt)}")
    parsed, detect = got
    old_col = ws.time_col
    prev = ws.meta.get("timeDetect") or {}
    # 同一列再指定一次、且它已经是 datetime64（via=dtype）：这次解析给不出任何新的来源信息，
    # 只会回一句 ISO。拿它覆盖 meta 就把上传时按真实源串测出的格式（例如带 .SSS）抹平了，
    # 重启重放时尤其明显——重放的是已经改过 dtype 的帧，第二条 set_time_col 必然走 dtype 分支。
    keep = key == old_col and detect.get("via") == "dtype" and bool(prev.get("format"))
    if keep:
        detect = prev
    work[key] = pd.to_datetime(got[0], errors="coerce")
    ws.meta["timeCol"] = key
    ws.meta["timeDetect"] = detect
    ws.meta["timeFormat"] = detect["displayFormat"]
    ws.meta["sourceTimeFormat"] = detect["displayFormat"]
    ws.meta["timeRejected"] = []
    for c in ws.meta["columns"]:
        c["isTime"] = c["key"] == key
    ws.rebuild_meta_columns(work)
    ws.refresh_freq(work)
    return {"summary": f"时间列 {old_col or '（无）'} → {key}"
                      f"（按 {detect['format']} 解析出 {detect['matched']}/{detect['sampled']} 个值）",
            "timeCol": key, "prevTimeCol": old_col, "format": detect["format"],
            "displayFormat": detect["displayFormat"], "matchRate": round(detect["matchRate"], 2),
            "parsedCount": int(detect["matched"]), "unparsed": int(detect.get("unparsed") or 0),
            "rowCount": int(work.shape[0]), "freqMinutes": ws.meta.get("freqMinutes"),
            "_valueChange": True}


def _op_rename_column(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    key = _require_col(ws, work, p["key"])
    label = (p.get("label") or "").strip()
    if not label:
        raise ValueError("新列名不能为空")
    # newKey 显式给定时按它改名（第五步的重命名会带上前端的冲突后缀 _lu7xxx），
    # 没给定时键就跟显示名同一个字符串——改名不再转写，原始列只换显示名时把 newKey 传成 key 本身。
    explicit = p.get("newKey")
    new_key = str(explicit if explicit is not None else label).strip()
    if new_key != key:
        new_key = _set_col_key(new_key, work, "新列名")
    if new_key != key:
        work.rename(columns={key: new_key}, inplace=True)
    for c in ws.meta["columns"]:
        if c["key"] == key:
            c["key"] = new_key
            c["label"] = label
    if ws.time_col == key:
        ws.meta["timeCol"] = new_key
        for c in ws.meta["columns"]:
            c["isTime"] = c["key"] == new_key
    for dc in ws.meta.get("derivedCols", []):
        if dc.get("key") == key:
            dc["key"] = new_key
            dc["label"] = label
    ws.refresh_freq(work)
    return {"summary": f'重命名 "{key}" → "{label}"', "oldKey": key, "newKey": new_key,
            "_valueChange": False}


def _op_delete_column(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    key = _require_col(ws, work, p["key"])
    if key == ws.time_col:
        raise ValueError("时间列不可删除")
    label = next((c["label"] for c in ws.meta["columns"] if c["key"] == key), key)
    work.drop(columns=[key], inplace=True)
    ws.meta["columns"] = [c for c in ws.meta["columns"] if c["key"] != key]
    ws.meta["derivedCols"] = [dc for dc in ws.meta.get("derivedCols", []) if dc.get("key") != key]
    return {"summary": f'删除列 "{label}"', "key": key, "colCount": int(work.shape[1])}


def _op_convert_unit(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    key = _require_col(ws, work, p["key"])
    col = next(c for c in ws.meta["columns"] if c["key"] == key)
    factor, offset = float(p["factor"]), float(p.get("offset") or 0)
    new_unit = (p.get("newUnit") or "").strip()
    if not new_unit:
        raise ValueError("目标单位不能为空")
    series = pd.to_numeric(work[key], errors="coerce")
    touched = int(series.notna().sum())
    # 进位口径统一走 features.round_half_up（= 前端 parseFloat(v.toFixed(4))）：
    # Series.round 是银行家舍入，而第二步单位转换的预览表用的是 toFixed，两套口径会让
    # "预览 4.47 / 落列 4.466"这种半数值格子当场对不上。
    work[key] = features.round_half_up(series.to_numpy(dtype="float64") * factor + offset, 4)
    base_name = re.sub(r"\s*\([^)]*\)\s*$", "", col["label"]).strip()
    old_unit = col.get("unit") or ""
    col["label"] = f"{base_name}({new_unit})" if new_unit else base_name
    col["unit"] = new_unit
    col["type"] = "float"
    return {"summary": f"单位转换 {col['label']} y={factor}x+{offset}", "changed": touched,
            "oldUnit": old_unit, "newUnit": new_unit, "label": col["label"]}


def _derived_formula(ws: Workspace, terms: list[dict]) -> str:
    op_label = {"+": "+", "-": "−", "*": "×", "/": "÷"}
    parts = []
    for i, t in enumerate(terms):
        col = next((c for c in ws.meta["columns"] if c["key"] == t["col"]), None)
        label = col["label"] if col else t["col"]
        parts.append(label if i == 0 else f"{op_label.get(t['op'], t['op'])} {label}")
    return " ".join(parts)


def _op_derived_column(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    name = _set_col_key(p["name"], work, "新列名")
    terms = p["terms"]
    if len(terms) < 2:
        raise ValueError("列运算至少需要两个操作数")
    keys = [_require_col(ws, work, t["col"]) for t in terms]
    result = pd.to_numeric(work[keys[0]], errors="coerce").astype("float64")
    for t, k in zip(terms[1:], keys[1:]):
        operand = pd.to_numeric(work[k], errors="coerce").astype("float64")
        op = t["op"]
        if op == "+":
            result = result + operand
        elif op == "-":
            result = result - operand
        elif op == "*":
            result = result * operand
        elif op == "/":
            result = result / operand.where(operand != 0)
        else:
            raise ValueError(f"不支持的运算符：{op}")
    result = result.replace([np.inf, -np.inf], np.nan)
    work[name] = features.round_half_up(result.to_numpy(dtype="float64"), 4)
    ws.rebuild_meta_columns(work)
    next(c for c in ws.meta["columns"] if c["key"] == name)["label"] = name
    formula = _derived_formula(ws, terms)
    ws.meta.setdefault("derivedCols", []).append({"key": name, "label": name, "formula": formula})
    valid = int(result.notna().sum())
    return {"summary": f"列运算生成 {name}：{formula}", "key": name, "formula": formula,
            "validRows": valid, "nullRows": int(result.shape[0]) - valid}


def _attach_column(ws: Workspace, work: pd.DataFrame, key: str, label: str,
                   values, source: dict) -> tuple[str, bool]:
    """把一列服务端算好的数值挂进帧与列注册表，回 (真实列名, 是否新增)。

    名字规则沿用掩码列（`_MASK_KEY`）：字母开头、只留 [A-Za-z0-9_]。
    这里刻意不做静默转写——旧 add-columns 会把中文名一路 `safe_key` 成 `___`，
    两列中文别名会撞成同一个名字并互相覆盖，而界面上看到的是两个不同的变量。
    """
    name = str(key or "")
    if not _MASK_KEY.match(name):
        raise ValueError(f"变量名 {name or '(空)'} 不合法：需以字母开头，只含字母、数字与下划线，长度 ≤64")
    if name in [str(c) for c in work.columns]:
        raise ValueError(f"列 {name} 已存在，换一个变量名或先删除它")
    arr = np.asarray(values, dtype="float64")
    n = int(work.shape[0])
    if arr.shape[0] < n:
        arr = np.concatenate((arr, np.full(n - arr.shape[0], np.nan)))
    elif arr.shape[0] > n:
        arr = arr[:n]
    work[name] = pd.Series(arr, index=work.index)
    ws.meta["columns"].append({"key": name, "label": label or name, "type": "float",
                               "isTime": False, "exo": source})
    ws.rebuild_meta_columns(work)
    return name, True


def _require_time_col(ws: Workspace, work: pd.DataFrame) -> str:
    key = ws.time_col
    if not key or key not in [str(c) for c in work.columns]:
        raise ValueError("工作区没有可用的时间列，外生变量要按时间轴生成")
    return key


_EXO_KIND_LABELS = {"preset": "预设模板（模拟）", "formula": "时间公式", "file": "侧表按时间戳对齐"}


def _op_exo_preset(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    """按主表时间列生成一列模拟外生变量：随机源是 seeded numpy，重放必然同一串值。"""
    preset_key = p.get("presetKey") or ""
    time_col = _require_time_col(ws, work)
    # 没给 seed 就现取一个，但必须经 _pinParams 交回 apply 写进日志：handler 拿到的 p 是
    # _execute 复制出来的副本，就地改回不到 record（旧注释以为那是同一个字典，实测落盘 seed=None，
    # 重启后重放换一个 seed、整列换值）。副本不能直接改回去，是因为 replay 载荷也并进同一个
    # params，污染日志。
    seed = int(p["seed"]) if p.get("seed") is not None else random.randrange(2 ** 31)
    values, spec = exo.generate_preset(preset_key, work[time_col], work, seed)
    key = p.get("key") or preset_key
    label = p.get("label") or spec["label"]
    name, _new = _attach_column(ws, work, key, label, values,
                                {"kind": "preset", "presetKey": preset_key, "seed": seed})
    return {
        "summary": f"{_EXO_KIND_LABELS['preset']} {name} · {spec['rows']} 行 · seed={seed}",
        "key": name, "label": label, "presetKey": preset_key, "seed": seed,
        "rowCount": spec["rows"], "validRows": spec["validRows"],
        "preview": [_jsonable(v) for v in values[:5]],
        "_pinParams": {"seed": seed},
    }


def _op_exo_formula(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    """按时间分量公式生成一列：hour/day/month/weekday/idx 在服务端向量化求值。"""
    expr = (p.get("expr") or "").strip()
    time_col = _require_time_col(ws, work)
    values = exo.generate_formula(expr, work[time_col])
    key = p.get("key") or ""
    name, _new = _attach_column(ws, work, key, p.get("label") or key, values,
                                {"kind": "formula", "expr": expr})
    return {
        "summary": f"{_EXO_KIND_LABELS['formula']} {name} · {int(work.shape[0])} 行 · {expr}",
        "key": name, "label": p.get("label") or key, "expr": expr,
        "rowCount": int(work.shape[0]), "validRows": int(np.count_nonzero(~np.isnan(values))),
        "preview": [_jsonable(v) for v in values[:5]],
    }


def _op_exo_file(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    """侧表落盘后由服务端解析并按时间戳对齐挂列：明细不过网络，重放只依赖那份文件。"""
    from . import dataset_store
    filename = p.get("filename") or ""
    content, real = dataset_store.read_exo_bytes(filename)
    exo.check_content_sha(real, content, p.get("sha") or "")
    side = exo.parse_side_table(content, real)
    time_col = _require_time_col(ws, work)
    targets = p.get("targets")
    aligned = exo.align_side_table(
        work[time_col], side, p.get("sideTimeCol"),
        mode=p.get("mode") or "left",
        tolerance_minutes=p.get("toleranceMinutes"),
        cols=[t["from"] for t in targets] if targets else None)
    if not targets:
        # 没指定目标列名时按表头推导，并把推导结果经 _pinParams 钉进日志：重放用的名字与首次一致
        targets = exo.default_targets(aligned["keys"])
    by_from = {t["from"]: t for t in targets}
    added = []
    for header in aligned["keys"]:
        t = by_from.get(header) or {}
        name, _new = _attach_column(ws, work, t.get("key") or header, t.get("label") or header,
                                    aligned["columns"][header],
                                    {"kind": "file", "filename": real,
                                     "sideTimeCol": aligned["stats"]["sideTimeCol"]})
        added.append(name)
    stats = aligned["stats"]
    return {
        "summary": f"{_EXO_KIND_LABELS['file']} {len(added)} 列 · 匹配 {stats['matchedMainRows']}/"
                   f"{stats['mainRows']} 行 · {real}",
        "keys": added, "count": len(added), "sha": p.get("sha"), "stats": stats,
        "targets": targets, "coerced": aligned["coerced"], "nonNumeric": aligned["nonNumeric"],
        "_pinParams": {"targets": targets},
    }


def _resample_frame(src: pd.DataFrame, time_col: str, minutes: int, method: str
                    ) -> tuple[pd.DataFrame, dict]:
    """按目标粒度分桶重排整表，并如实说明这一趟是降采样还是升采样、空桶怎么来。

    方向必须分开算，因为两者根本不是一件事：
    - 降采样（目标比原始间隔粗）：一个桶里有多行，按 mean/sum/first 聚合成一行；
      没有数据的桶就留空——那段时间确实一个观测都没有，补出来就是造假。
    - 升采样（目标粒度比原始间隔更细）：绝大多数桶里一行都没有，"聚合"无从谈起。
      旧实现在这里照样按均值聚合，于是 720 行 60min 数据"重采样成 15min"变成 2877 行、
      其中 2157 行整行全空，看着像数据翻了四倍、其实四分之三是洞。
      现在明确成填充语义：mean/sum/first 都按"值延续"（前一个观测一直有效到下一个），
      interpolate 走线性插值，两种都在回执里写清是"补"出来的、补了多少格。
    """
    step = pd.Timedelta(minutes=minutes)
    step_ns = minutes * 60 * 1_000_000_000
    ts = pd.to_datetime(src[time_col], errors="coerce")
    keep = ts.notna()
    frame = src.loc[keep].assign(_ts=ts[keep]).sort_values("_ts")
    if frame.empty:
        raise ValueError("时间列没有可解析的时间戳，无法重采样")
    source_minutes = detect_freq_minutes(frame["_ts"])
    direction = "same"
    if source_minutes:
        direction = "up" if minutes < source_minutes else ("down" if minutes > source_minutes else "same")
    t0, t_end = frame["_ts"].min(), frame["_ts"].max()
    # 桶起点按 epoch 零点对齐（naive 时间戳下等价于本地整点/整刻），空桶也补出来
    first_bucket = pd.Timestamp(int(t0.value // step_ns) * step_ns)
    last_bucket = pd.Timestamp(int(t_end.value // step_ns) * step_ns)
    bucket_count = int((last_bucket.value - first_bucket.value) // step_ns) + 1
    buckets = pd.date_range(first_bucket, periods=bucket_count, freq=step)
    idx = pd.DatetimeIndex(buckets)
    frame = frame.assign(_bucket=frame["_ts"].dt.floor(step))
    grouped = frame.groupby("_bucket", sort=True)

    filled_by_fill = 0
    out: dict[str, pd.Series] = {time_col: pd.Series(buckets, index=idx, name=time_col)}
    for c in frame.columns:
        if c in ("_ts", "_bucket", time_col):
            continue
        agg = grouped[c]
        if pd.api.types.is_numeric_dtype(frame[c]):
            if method == "sum":
                s = agg.sum(min_count=1)
            elif method == "first":
                s = agg.first()
            elif method == "interpolate" and direction == "up":
                # 升采样时"插值"的落点在桶与桶之间：先取桶内首值，再线性插
                s = agg.first()
            else:
                s = agg.mean()
            s = s.reindex(idx).astype("float64")
            if direction == "up":
                gaps = int(s.isna().sum())
                s = (s.interpolate(method="linear", limit_direction="both") if method == "interpolate"
                     else s.ffill().bfill())
                filled_by_fill = max(filled_by_fill, gaps)
            elif method == "interpolate":
                # 真正的线性插值：空桶用相邻桶值插出来（旧版浏览器实现把 interpolate 当成均值做了）
                s = s.interpolate(method="linear", limit_direction="both")
            out[c] = features.round_half_up(s.to_numpy(dtype="float64"), 2)
        else:
            s = agg.first().reindex(idx)
            if direction == "up":
                filled_by_fill = max(filled_by_fill, int(s.isna().sum()))
                s = s.ffill().bfill()
            out[c] = s
    resampled = pd.DataFrame(out).reset_index(drop=True)
    covered = int(grouped.size().reindex(idx).notna().sum())
    stats = {
        "sourceRows": int(frame.shape[0]),
        "unparsedDropped": int((~keep).sum()),
        "targetMinutes": minutes,
        "sourceMinutes": source_minutes,
        "direction": direction,
        "method": method,
        "bucketCount": int(len(buckets)),
        "filledBuckets": covered,
        "emptyBuckets": int(len(buckets)) - covered,
        "filledByFill": int(filled_by_fill),
        "fillNote": _resample_fill_note(direction, method, filled_by_fill),
    }
    return resampled, stats


def _resample_fill_note(direction: str, method: str, filled: int) -> str:
    """把"多出来的行是怎么来的"讲明白：升采样只有两种诚实说法，都不该被写成"聚合"。"""
    if direction != "up":
        return "降采样：没有观测的桶留空，不补值" if direction == "down" else "目标粒度与原始间隔相同：逐桶聚合，行数只可能因空桶而变"
    if method == "interpolate":
        return f"升采样：{filled} 格由相邻桶线性插值得到"
    return f"升采样：{filled} 格沿用上一个观测的值（值延续），不是新测到的数据"


def _op_resample(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    if not ws.time_col or ws.time_col not in work.columns:
        raise ValueError("没有可用的时间列，无法重采样")
    minutes = int(p["targetMinutes"])
    method = p.get("method") or "mean"
    if minutes <= 0:
        raise ValueError("目标采样间隔必须为正整数分钟")
    if method not in RESAMPLE_METHODS:
        raise ValueError(f"不支持的重采样方法：{method}")
    old_count = int(work.shape[0])
    resampled, stats = _resample_frame(work, ws.time_col, minutes, method)
    ws.rebuild_meta_columns(resampled)
    ws.refresh_freq(resampled)
    label = f"{minutes} min"
    ws.meta["freq"] = f"{label} ({1440 // minutes}点/天)"
    direction_label = {"up": "升采样", "down": "降采样", "same": "同粒度重排"}[stats["direction"]]
    return {
        "summary": f"{direction_label} {old_count}→{len(resampled)} 行 · {label} · {METHOD_LABELS[method]}",
        "directionLabel": direction_label,
        "oldCount": old_count, "newCount": int(len(resampled)), "_frame": resampled, **stats,
    }


METHOD_LABELS = {"mean": "均值聚合", "sum": "求和聚合", "first": "首值采样", "interpolate": "线性插值"}


_MASK_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

IMPUTE_LABELS = {"linear": "线性插值", "ffill": "前向填充", "spline": "三次样条", "zero": "常数0"}
REPAIR_LABELS = {"clip": "阈值截断", "nan_impute": "置缺失并重插值", "mask_only": "生成布尔掩码"}


def _float_col_or_raise(ws: Workspace, key: str) -> dict:
    col = next((c for c in ws.meta["columns"] if c["key"] == key), None)
    if col is None:
        raise ValueError(f"列不存在：{key}")
    if col["type"] != "float":
        raise ValueError(f"列 [{col['label']}] 不是数值列，无法用 {'/'.join(quality.IMPUTE_ALGOS)} 填补")
    return col


def _op_impute(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    """按缺失段填补（可跨列），并可选执行重复时间戳合并。

    两种给法：
    - targets：区间由界面从 /quality 拿到的缺失段快照给出，这里只填区间内真正缺失的单元格，
      一个区间里已经不含缺失值就报错中止——快照过期说明数据已被别的操作改过；
    - all=true：由服务端自己扫全表逐段填补。缺失段超过 /quality 上限的大表只能走这条，
      否则界面会"填了前 300 段"却报"全部填补完成"。
    """
    targets = [dict(t) for t in (p.get("targets") or [])]
    if p.get("all"):
        wanted = p.get("keys") or None
        algos = p.get("algos") or {}
        default_algo = p.get("defaultAlgo") or "linear"
        if default_algo not in quality.IMPUTE_ALGOS:
            raise ValueError(f"不支持的填补算法：{default_algo}")
        targets = []
        for col in ws.source_float_columns():
            key = col["key"]
            if wanted and key not in wanted:
                continue
            algo = algos.get(key) or default_algo
            if algo not in quality.IMPUTE_ALGOS:
                raise ValueError(f"不支持的填补算法：{algo}")
            for start, end in quality.missing_runs(quality.numeric_array(work, key)):
                targets.append({"key": key, "startIdx": start, "endIdx": end, "algo": algo})
        if not targets and not p.get("dedupe"):
            raise ValueError("服务端扫描未发现缺失值，本次没有可填补的内容")
    dedupe = p.get("dedupe")
    if dedupe and dedupe not in quality.DUP_STRATEGIES:
        raise ValueError(f"不支持的去重策略：{dedupe}")
    if not targets and not dedupe:
        raise ValueError("没有要执行的填补区间")
    grouped: dict[str, list[dict]] = {}
    for t in targets:
        _float_col_or_raise(ws, t["key"])
        algo = t.get("algo") or "linear"
        if algo not in quality.IMPUTE_ALGOS:
            raise ValueError(f"不支持的填补算法：{algo}")
        grouped.setdefault(t["key"], []).append(
            {"startIdx": int(t["startIdx"]), "endIdx": int(t["endIdx"]), "algo": algo})
    filled = 0
    segments: list[dict] = []
    for key, runs in grouped.items():
        arr = quality.numeric_array(work, key)
        new_arr, n, detail = quality.apply_impute(arr, runs)
        silent = [d for d in detail if d["filled"] == 0]
        if silent:
            label = next(c["label"] for c in ws.meta["columns"] if c["key"] == key)
            s = silent[0]
            raise ValueError(f"列 [{label}] 第 {s['startIdx']}–{s['endIdx']} 行已无缺失值"
                             f"（缺失段快照已过期，请重新获取缺失段）")
        work[key] = new_arr
        filled += n
        segments.extend({"colKey": key, **d} for d in detail)
    removed = groups = 0
    frame = work
    if dedupe:
        frame, removed, groups = quality.merge_duplicates(
            work, ws.time_col, [c["key"] for c in ws.source_float_columns()], dedupe)
    seg_count = len(segments)
    parts = []
    if filled:
        used = "、".join(IMPUTE_LABELS[a] for a in sorted({r["algo"] for runs in grouped.values() for r in runs}))
        how = "服务端扫描全部缺失段" if p.get("all") else f"{seg_count} 段"
        parts.append(f"填补 {len(grouped)} 列 · {how}（{used}）· {filled} 个缺失值")
    if dedupe:
        parts.append(f"按{DUP_LABELS[dedupe]}合并重复时间戳 {removed} 行（{groups} 组）")
    if not parts:
        raise ValueError("本次执行没有改动任何数据")
    summary = " · ".join(parts)
    out = {"summary": summary, "filled": filled, "colsFixed": len(grouped),
           # 逐段明细只回前若干条给界面做抽样展示；全量段数在 segmentCount 里
           "segments": segments[:quality.MAX_IMPUTE_DETAIL],
           "segmentCount": seg_count,
           "segmentsTruncated": seg_count > quality.MAX_IMPUTE_DETAIL,
           "scanned": bool(p.get("all")),
           "dedupe": dedupe, "deduped": removed, "duplicateGroups": groups,
           "rowCount": int(frame.shape[0])}
    if frame is not work:
        out["_frame"] = frame
    return out


def _op_anomaly_repair(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    mode = p.get("repair") or ""
    if mode not in quality.REPAIR_MODES:
        raise ValueError(f"不支持的修复方案：{mode}")
    # 重放（撤销/重做）时用的是这条命令当时随日志存下的那份检测；
    # 只有界面新发起的修复才去查服务端缓存，并且必须新鲜。
    det = p.get("detection")
    if det is None:
        det = ws.anomaly
        if det is None:
            raise ValueError("后端没有留存的检测结果：请先执行检测再修复")
        if ws.anomaly_stale():
            raise ValueError("检测之后数据又被改过（行位置已变），请重新检测后再修复")
    labels = {c["key"]: c["label"] for c in ws.meta["columns"]}
    frame, touched, mask_keys = quality.apply_repair(work, det, mode, labels)
    if touched == 0:
        raise ValueError("本次检测没有可修复的点")
    if mask_keys:
        ws.rebuild_meta_columns(frame)
        for k in mask_keys:
            col = next(c for c in ws.meta["columns"] if c["key"] == k)
            col["type"] = "binary"
            col["label"] = labels.get(k, col["label"])
    algo_label = ANOMALY_LABELS.get(det.get("algo"), det.get("algo") or "")
    return {
        "summary": f"{algo_label} → {REPAIR_LABELS[mode]}：处理 {touched} 个数据点",
        "repair": mode, "touched": touched, "maskCols": mask_keys,
        "algo": det.get("algo"), "anomalyTotal": det.get("summary", {}).get("totalAnomalies"),
        # 掩码只新增列、不动既有数值，检测索引仍然有效
        "_valueChange": mode != "mask_only", "_frame": frame,
        # 把这次的判定索引钉进日志：这一条命令以后重放多少次都是同一个结果
        "_replay": {"detection": det, "repair": mode},
    }


def _op_mask_generate(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    name = (p.get("maskName") or "").strip()
    if not _MASK_KEY.match(name):
        raise ValueError(f"掩码列名不合法：{name or '(空)'}（需以字母开头，只能用字母/数字/下划线）")
    n = int(work.shape[0])
    start, end = int(p.get("startIdx", -1)), int(p.get("endIdx", -1))
    if start < 0 or end >= n or start > end:
        raise ValueError(f"掩码区间越界：{start}–{end}（当前共 {n} 行）")
    column = np.zeros(n, dtype="int64")
    column[start:end + 1] = 1
    work[name] = column
    existing = next((c for c in ws.meta["columns"] if c["key"] == name), None)
    if existing is None:
        ws.meta["columns"].append({"key": name, "label": f"掩码:{name}", "type": "binary", "isTime": False})
    else:
        existing["type"] = "binary"
    labels = ws.time_labels()
    ones = int(column.sum())
    return {"summary": f"生成布尔掩码 {name}：第 {start}–{end} 行 · {ones} 个 1",
            "key": name, "label": f"掩码:{name}", "startIdx": start, "endIdx": end,
            "startTime": labels[start], "endTime": labels[end], "onesCount": ones,
            "colCount": int(work.shape[1]), "_valueChange": False}


def _op_mask_delete(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    keys = [k for k in (p.get("keys") or []) if k]
    if not keys:
        raise ValueError("没有要删除的掩码列")
    names = [str(c) for c in work.columns]
    deleted = [k for k in keys if k in names]
    if not deleted:
        raise ValueError(f"掩码列都不存在：{'、'.join(keys[:6])}")
    work.drop(columns=deleted, inplace=True)
    ws.meta["columns"] = [c for c in ws.meta["columns"] if c["key"] not in deleted]
    return {"summary": f"删除 {len(deleted)} 个掩码列：{'、'.join(deleted[:6])}",
            "deleted": deleted, "colCount": int(work.shape[1]), "_valueChange": False}


ANOMALY_LABELS = {
    "3sigma": "3-Sigma", "iqr": "IQR 箱线法", "iforest": "孤立森林(近似 MAD)",
    "iforest_sklearn": "孤立森林(sklearn)", "expr": "自定义表达式",
}
DUP_LABELS = {"mean": "均值", "first": "保留首行", "last": "保留末行"}
# ---------------------------------------------------------------- 第五步：特征构建（第③期起全部在后端执行）
#
# 特征列写进 DataFrame 同时也进 meta.columns，并带一个 feature=<族> 标记：
# 界面的目标列选择器据此把衍生列挡在外面（旧的浏览器实现里特征从不进 d.columns，
# 所以「用特征做滞后」本来就不可能），异常检测的列扫描口径保持与第②期一致不受影响。

MAX_FEATURE_COLS_PER_OP = 400
MAX_ONEHOT_LEVELS = 200


def _require_feature_target(ws: Workspace, work: pd.DataFrame, key: str) -> str:
    _require_col(ws, work, key)
    col = next(c for c in ws.meta["columns"] if c["key"] == key)
    if col.get("type") != "float":
        raise ValueError(f"列 {key} 不是数值列，无法作为特征目标（当前类型 {col.get('type')}）")
    return key


def _feature_commit(ws: Workspace, work: pd.DataFrame, family: str,
                    items: list[tuple[str, str]], columns: dict, summary: str) -> dict:
    if not items:
        raise ValueError("本次没有要生成的特征列")
    if len(items) > MAX_FEATURE_COLS_PER_OP:
        raise ValueError(f"一次生成 {len(items)} 列，超过单次上限 {MAX_FEATURE_COLS_PER_OP} 列：请减少目标列或窗口数量")
    existed = {str(c) for c in work.columns}
    # 同一族重新生成一次就是「换成这一批」：上一批里没被再次选中的列必须退场，
    # 否则取消勾选「保留数值原列」之后 feat_hour 还赖在表里，界面上就是假的。
    planned = {k for k, _ in items}
    prev_by_key = {c["key"]: c for c in ws.meta["columns"]}
    removed = [str(c) for c in work.columns
               if (prev_by_key.get(str(c)) or {}).get("feature") == family and str(c) not in planned]
    if removed:
        work.drop(columns=removed, inplace=True)
    for key, values in columns.items():
        work[key] = values
    ws.rebuild_meta_columns(work)
    meta_by_key = {c["key"]: c for c in ws.meta["columns"]}
    for key, label in items:
        col = meta_by_key.get(key)
        if col is None:
            continue
        col["label"] = label
        col["feature"] = family
    created = sum(1 for key, _ in items if key not in existed)
    if removed:
        summary += f" · 换掉上一批未再勾选的 {len(removed)} 列"
    return {
        "summary": summary, "featureType": family,
        "cols": len(items), "created": created, "removed": removed,
        "keys": [k for k, _ in items],
        "features": [{"key": k, "label": meta_by_key.get(k, {}).get("label", k), "feature": family}
                     for k, _ in items],
        "colCount": int(work.shape[1]), "rowCount": int(work.shape[0]),
        # 特征只新增列、不动既有数值，第四步留存的检测索引仍然有效
        "_valueChange": False, "_frame": work,
    }


def _op_split_apply(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    """把切分落成真实的一列：时序不打乱，按整表行序前 train% / 中 val% / 末 test%。

    行数只由这里的公式决定（features.split_counts），界面与导出脚本读的是同一份，
    所以「界面上 2016 / 2016 / 2016 条」和列里真正写进去的值必然一致。
    """
    ratio = int(p.get("ratio") or 70)
    if not 50 <= ratio <= 85:
        raise ValueError(f"训练集占比需在 50~85 之间，收到 {ratio}")
    n = int(work.shape[0])
    if n < 3:
        raise ValueError(f"整表只有 {n} 行，切不出训练/验证/测试三段")
    key = (p.get("key") or features.SPLIT_COL_KEY).strip() or features.SPLIT_COL_KEY
    label = (p.get("label") or features.SPLIT_COL_LABEL).strip() or features.SPLIT_COL_LABEL
    c = features.split_counts(n, ratio)
    values = ["train"] * c["train"] + ["val"] * c["val"] + ["test"] * c["test"]
    existed = key in {str(x) for x in work.columns}
    work[key] = values
    ws.rebuild_meta_columns(work)
    col = next((x for x in ws.meta["columns"] if x["key"] == key), None)
    if col is not None:
        col["label"] = label
        # feature=split：让它进第五步的特征登记表（可改名、可撤销），同时被数值列选择器挡在外面
        col["feature"] = "split"
    counts = "/".join(f"{v} {c[v]}" for v in features.SPLIT_VALUES)
    return {
        "summary": f"{'更新' if existed else '生成'}划分列 {key}：{label} {ratio}% · {counts}（共 {n} 行）",
        "key": key, "label": label, "ratio": ratio, "replaced": existed,
        "train": c["train"], "val": c["val"], "test": c["test"], "rowCount": n,
        "colCount": int(work.shape[1]),
        "counts": {v: {"rows": c[v], "pct": round(c[v] / n * 100, 2)} for v in features.SPLIT_VALUES},
        "_valueChange": False, "_frame": work,
    }


def _op_holidays(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    """配置节假日表：只改工作区配置，不动帧，但同样是一条可撤销、可重放的命令。"""
    days = features.check_holiday_days(p.get("days"))
    source = (p.get("source") or "").strip() or "custom"
    ws.meta["holidayDays"] = days
    ws.meta["holidaySource"] = source
    return {
        "summary": f"节假日表：{len(days)} 天（{source}）" +
                   (f" · {days[0]} ~ {days[-1]}" if days else " · 不认任何节假日"),
        "days": days, "count": len(days), "source": source,
        "rowCount": int(work.shape[0]), "colCount": int(work.shape[1]),
        "_valueChange": False,
    }


def _op_feature_time(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    dims, cyc = features.check_time_params(p.get("dims"), p.get("cyclical"), p.get("cycDims"))
    # 拆分前的旧命令没有这个字段，缺省保留原列 == 旧行为，老日志重放出来的列一颗不差
    keep = bool(p.get("keepCycOriginal", True))
    time_col = ws.time_col
    if not time_col or time_col not in [str(c) for c in work.columns]:
        raise ValueError("当前工作区没有可用的时间列，无法生成日历特征")
    ts = work[time_col]
    if not pd.api.types.is_datetime64_any_dtype(ts):
        ts = pd.to_datetime(ts, errors="coerce")
    holidays = frozenset(ws.holiday_days())
    plan = features.time_plan(dims, cyc, keep)
    columns = features.build_time(ts, dims, cyc, keep, holidays)
    sin_cos = 2 * len(cyc)          # 每个被编码的维度出 sin、cos 两列
    detail = ",".join(dims + [f"cyclical_{d}" for d in cyc] + ([] if keep else ["replace_original"]))
    result = _feature_commit(ws, work, "time", plan, columns,
                             f"时间日历特征：{len(plan)} 列（正余弦 {sin_cos}）· {detail}")
    # 把这次真正用上的日期表回带出去：审计记录留着它，导出脚本才复现得出同一批 feat_holiday 值
    result["holidays"] = {"used": "holiday" in dims,
                          "source": ws.meta.get("holidaySource") or f"preset-{features.DEFAULT_HOLIDAY_YEAR}",
                          "days": sorted(holidays)}
    return result


def _op_feature_lag(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    group = p.get("group")
    if group not in (None, "lag", "window"):
        raise ValueError(f"不支持的生成组：{group}（可选 lag / window）")
    cols = [c for c in (p.get("cols") or []) if c]
    if not cols:
        raise ValueError("请先选择要构造滞后/窗口特征的目标列")
    for c in cols:
        _require_feature_target(ws, work, c)
    lags = features.check_int_list("滞后步长", p.get("lags"))
    windows = features.check_int_list("滚动窗口", p.get("windows"))
    stats = [s for s in (p.get("stats") or []) if s in features.ROLL_STATS]
    if windows and not stats:
        raise ValueError("选了滚动窗口但没选统计量")
    expanding = bool(p.get("expanding"))
    ewm = bool(p.get("ewm"))
    span = features.normalize_span(p.get("ewmSpan"))
    if group == "lag":
        if windows or expanding or ewm:
            raise ValueError("这一组只生成滞后特征：滚动窗口/高级窗口请改用「滑动窗口」那一组提交")
        if not lags:
            raise ValueError("请先填写滞后阶数")
    elif group == "window":
        if lags:
            raise ValueError("这一组只生成滑动窗口特征：滞后阶数请改用「滞后特征」那一组提交")
        if not (windows or expanding or ewm):
            raise ValueError("请先填写滚动窗口，或勾选 Expanding / EWM")
    elif not (lags or windows or expanding or ewm):
        raise ValueError("至少选择一个滞后步长、滚动窗口或高级窗口")
    plan = features.lag_plan(cols, lags, windows, stats, expanding, ewm, span)
    columns: dict = {}
    for col in cols:
        for s in lags:
            columns[f"lag_{col}_t{s}"] = features.lag_column(work[col], s)
        vals = features.numeric_column(work, col)
        for w in windows:
            for fn in stats:
                columns[f"roll_{features.ROLL_STATS[fn]}_{col}_w{w}"] = features.rolling_agg(vals, w, fn)
        if expanding:
            columns[f"expanding_mean_{col}"] = features.expanding_mean(vals)
        if ewm:
            columns[f"ewm_{col}_s{span}"] = features.ewm_prev(vals, span)
    detail = (f"cols:{','.join(cols)}|lag:{','.join(map(str, lags))}|roll:{','.join(map(str, windows))}"
              f"|stats:{','.join(stats)}|exp:{1 if expanding else 0}|ewm:{span if ewm else 0}")
    family = {"lag": "lag", "window": "window"}.get(group, "lag_roll")
    label = {"lag": "滞后特征", "window": "滑动窗口特征"}.get(group, "滞后与滑动窗口特征")
    return _feature_commit(ws, work, family, plan, columns,
                           f"{label}：{len(plan)} 列 · {detail}")


def _op_feature_diff(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    group = p.get("group")
    if group not in (None, "diff", "fft"):
        raise ValueError(f"不支持的生成组：{group}（可选 diff / fft）")
    cols = [c for c in (p.get("cols") or []) if c]
    if not cols:
        raise ValueError("请先选择要做差分/频域分析的目标列")
    for c in cols:
        _require_feature_target(ws, work, c)
    d1, d2 = bool(p.get("d1")), bool(p.get("d2"))
    seasonal = bool(p.get("seasonal"))
    period = features.check_period(p.get("period"))
    dominant = bool(p.get("fftDominant"))
    entropy = bool(p.get("fftEntropy"))
    power_ratio = bool(p.get("fftPowerRatio"))
    if group == "diff":
        if dominant or entropy or power_ratio:
            raise ValueError("这一组只生成差分特征：频域项请改用「频域特征」那一组提交")
        if not (d1 or d2 or seasonal):
            raise ValueError("请至少勾选一阶、二阶或季节性差分")
    elif group == "fft":
        if d1 or d2 or seasonal:
            raise ValueError("这一组只生成频域特征：差分项请改用「差分特征」那一组提交")
        if not (dominant or entropy or power_ratio):
            raise ValueError("请至少勾选一项频域特征")
    elif not (d1 or d2 or seasonal or dominant or entropy or power_ratio):
        raise ValueError("至少选择一种差分或频域特征")
    plan = features.diff_plan(cols, d1, d2, seasonal, period, dominant, entropy, power_ratio)
    columns: dict = {}
    for col in cols:
        s = pd.to_numeric(work[col], errors="coerce")
        if d1:
            columns[f"diff1_{col}"] = features.round_half_up(s.diff().to_numpy(dtype="float64"), 4)
        if d2:
            columns[f"diff2_{col}"] = features.round_half_up(s.diff().diff().to_numpy(dtype="float64"), 4)
        if seasonal:
            columns[f"diff_season{period}_{col}"] = features.round_half_up(
                s.diff(period).to_numpy(dtype="float64"), 4)
    head = cols[0]
    if dominant or entropy or power_ratio:
        vals = features.numeric_column(work, head)
        vals = np.where(np.isnan(vals), 0.0, vals)     # 缺失按 0 计入频谱（与浏览器端同）
        by_key = {k: i for i, (k, _) in enumerate(plan)}
        if dominant:
            extra, d_items = features.dominant_columns(vals, head)
            columns.update(extra)
            # 标签里的全谱能量只有真算过才知道，覆盖 plan 里的通用标签
            for key, label in d_items:
                plan[by_key[key]] = (key, label)
        if entropy or power_ratio:
            extra, s_items = features.spectral_columns(vals, head)
            for key, label in s_items:
                if key not in by_key:
                    continue
                columns[key] = extra[key]
                plan[by_key[key]] = (key, label)
    fft = [n for n, on in (("dom", dominant), ("ent", entropy), ("pow", power_ratio)) if on]
    detail = (f"cols:{','.join(cols)}|d1:{1 if d1 else 0}|d2:{1 if d2 else 0}"
              f"|seas:{period if seasonal else 0}|fft:{','.join(fft)}")
    family = {"diff": "diff", "fft": "fft"}.get(group, "diff_freq")
    label = {"diff": "差分特征", "fft": "频域特征"}.get(group, "差分与频域特征")
    return _feature_commit(ws, work, family, plan, columns,
                           f"{label}：{len(plan)} 列 · {detail}")


CAT_METHOD_LABELS = {"onehot": "独热编码", "ordinal": "序数编码", "target": "目标均值编码"}


def _default_target_col(ws: Workspace) -> str | None:
    """目标均值编码的参照列：先看主数值列（isMain），再退到第一个非衍生浮点列。"""
    floats = [c for c in ws.meta["columns"]
              if c.get("type") == "float" and not c.get("feature")
              and not str(c["key"]).endswith(("_ordinal", "_target"))]
    main = next((c for c in floats if c.get("isMain")), None) or (floats[0] if floats else None)
    return main["key"] if main else None


def _op_feature_cat(ws: Workspace, work: pd.DataFrame, p: dict) -> dict:
    cols = [c for c in (p.get("cols") or []) if c]
    method = p.get("method") or "onehot"
    if not cols:
        raise ValueError("请先在左侧勾选至少一个类别列")
    if method not in features.CAT_METHODS:
        raise ValueError(f"不支持的编码方式：{method}")
    for c in cols:
        _require_col(ws, work, c)
        if c == ws.time_col:
            raise ValueError("时间列不能作为类别编码的输入")
    # 取值顺序 = 首次出现顺序：独热列名、序数编号、目标均值分组三处都靠它，只算一次并复用
    levels = {c: features.unique_in_order(work[c]) for c in cols}
    if method == "onehot":
        for c in cols:
            if len(levels[c]) > MAX_ONEHOT_LEVELS:
                raise ValueError(f"列 {c} 有 {len(levels[c])} 个取值，超过独热单列上限 {MAX_ONEHOT_LEVELS}："
                                 f"高基数列请改用序数或目标均值编码")
        total = sum(len(levels[c]) for c in cols)
        if total > MAX_FEATURE_COLS_PER_OP:
            raise ValueError(f"独热将展开 {total} 列，超过单次上限 {MAX_FEATURE_COLS_PER_OP} 列")
    target_col = p.get("targetColumn") or _default_target_col(ws)
    if method == "target":
        if not target_col:
            raise ValueError("没有可用作目标均值参照的数值列")
        _require_feature_target(ws, work, target_col)
    plan = features.cat_plan(cols, method, levels)
    columns, _items = features.build_cat(work, cols, method, target_col, levels)
    detail = f"cols:{','.join(cols)}|method:{method}"
    result = _feature_commit(ws, work, "cat", plan, columns,
                             f"类别特征编码（{CAT_METHOD_LABELS[method]}）：{len(plan)} 列 · {detail}")
    result["targetColumn"] = target_col if method == "target" else None
    return result

_OPS = {
    "set_time_format": _op_set_time_format,
    "set_time_col": _op_set_time_col,
    "rename_column": _op_rename_column,
    "delete_column": _op_delete_column,
    "convert_unit": _op_convert_unit,
    "derived_column": _op_derived_column,
    "resample": _op_resample,
    "split_apply": _op_split_apply,
    "impute": _op_impute,
    "anomaly-repair": _op_anomaly_repair,
    "mask-generate": _op_mask_generate,
    "mask-delete": _op_mask_delete,
    "holidays": _op_holidays,
    "feature_time": _op_feature_time,
    "feature_lag": _op_feature_lag,
    "feature_diff": _op_feature_diff,
    "feature_cat": _op_feature_cat,
    "exo-preset": _op_exo_preset,
    "exo-formula": _op_exo_formula,
    "exo-file": _op_exo_file,
}


# ---------------------------------------------------------------- 注册表
#
# 内存里只放"最近用过的 8 颗帧"，磁盘上的命令日志才是历史的住处：被淘汰或服务端重启后，
# 第一次访问那个 wsId 会按「来源 + 日志」把帧重建回来，版本号与撤销/重做尾巴都在。
# 所以 get() 不是查表，而是"要么在内存、要么能从磁盘复原"，两条路给同一个 wsId。

def _evict_if_needed() -> None:
    while len(_REGISTRY) > MAX_WORKSPACES:
        victim = min(_ACCESS.items(), key=lambda kv: kv[1])[0]
        # 只是把帧从内存里请出去：命令日志留在 state 目录，下次访问按日志重建。
        # 这里若顺手删掉日志，"淘汰"就变成了"历史消失"，界面上再也回不去那一版。
        _REGISTRY.pop(victim, None)
        _ACCESS.pop(victim, None)
        # 重建锁不跟着清：那几把锁可能正被别的线程握着（它重建到一半），删了字典条目
        # 只会让后来者拿到一把新锁，两个人同时重建同一颗帧。每个 wsId 一把小锁，
        # 上限就是进程里见过的 wsId 数，不值得为它做回收。


def _rebuild_lock(ws_id: str) -> threading.Lock:
    """这个 wsId 的重建锁（进程内每颗帧一把，注册表锁保护它自己的登记）。"""
    with _LOCK:
        lk = _REBUILD_LOCKS.get(ws_id)
        if lk is None:
            lk = _REBUILD_LOCKS[ws_id] = threading.Lock()
        return lk


def peek(ws_id: str) -> "Workspace | None":
    """只查内存、不重建：给「这帧在不在」的判断用，别拿它当 get 的替代。"""
    with _LOCK:
        return _REGISTRY.get(ws_id)


def _register(df: pd.DataFrame, meta: dict, source: dict,
              ws_id: str | None = None, created_at: str | None = None,
              persist: bool = True) -> Workspace:
    cells = int(df.shape[0] * df.shape[1])
    if cells > MAX_CELLS:
        raise ValueError(f"数据量 {df.shape[0]} 行 × {df.shape[1]} 列 = {cells} 格，超过后端单工作区上限 {MAX_CELLS} 格")
    ws_id = ws_id or uuid.uuid4().hex[:12]
    meta = dict(meta)
    meta.setdefault("timeFormat", (meta.get("timeDetect") or {}).get("displayFormat") or "YYYY-MM-DD HH:mm:ss")
    meta["sourceTimeFormat"] = meta["timeFormat"]
    detected = detect_freq_minutes(df[meta["timeCol"]]) if meta.get("timeCol") else None
    meta["freqMinutes"] = detected
    meta["freqLabel"] = freq_label(detected)
    meta["freq"] = meta["freqLabel"]
    meta.setdefault("derivedCols", [])
    kwargs = {"created_at": created_at} if created_at else {}
    ws = Workspace(id=ws_id, base_df=df.copy(deep=True), base_meta=copy.deepcopy(meta),
                   meta=meta, source=source, df=df, **kwargs)
    with _LOCK:
        global _ACCESS_TICK
        _ACCESS_TICK += 1
        _REGISTRY[ws_id] = ws
        _ACCESS[ws_id] = _ACCESS_TICK
        _evict_if_needed()
    if persist:
        ws.persist_log()   # 空日志也要落盘：没有它，重启后这个 wsId 就彻底找不回来了
    return ws


def create_from_bytes(content: bytes, filename: str, display_name: str | None = None) -> Workspace:
    df, meta = dataframe_from_bytes(content, filename)
    meta["name"] = display_name or (filename or "workspace")
    return _register(df, meta, {"kind": "upload", "filename": filename})


def create_from_files(parts: list[tuple[bytes, str]], display_name: str | None = None,
                      persist: bool = True) -> tuple[Workspace, list[str]]:
    """多份上传文件按行合并 + 按时间排序成一颗帧，返回 (工作区, 各份的落盘文件名)。

    source 记的是**全部落盘文件名**：合并帧没有"原始整表"可退回去，服务端重启后只能靠
    数据集目录里的这几份文件依次再解析一遍，所以文件名丢了这颗帧就废了。
    顺序刻意是"先解析合并、后写盘"：拼不成一张表的那一批（时间列名不一致等）不该在
    数据集目录里留下孤儿文件——它们会出现在「最近打开的数据集」列表里。
    """
    parsed = []
    for content, filename in parts:
        df, meta = dataframe_from_bytes(content, filename)
        parsed.append((filename, df, meta))
    df, meta = merge_frames(parsed)
    from . import dataset_store   # 与 dataset_read_bytes 同一口径的延迟导入，避免循环依赖
    names = [str(f) for _, f in parts]
    saved = [dataset_store.save_bytes(n, c) for c, n in parts] if persist else names
    meta["name"] = display_name or f"{display_stem(names[0])} 等 {len(names)} 份合并"
    return _register(df, meta, {"kind": "merge", "filenames": list(saved)}), saved


def create_from_dataframe(df: pd.DataFrame, meta: dict, source: dict) -> Workspace:
    meta = dict(meta)
    if "name" not in meta:
        meta["name"] = source.get("filename") or "workspace"
    return _register(df, meta, source)


def create_preset(key: str, seed: int | None = None) -> Workspace:
    # 没给 seed 就现场挑一个并记进来源：预设帧要能事后重建，否则服务端一重启，
    # 这个工作区就再也拼不回来了（同 seed 必然同一张表，这条不变）。
    if seed is None:
        seed = random.randrange(2 ** 31)
    df, meta = preset_dataframe(key, seed)
    return _register(df, meta, {"kind": "preset", "presetKey": key, "seed": seed})


def create_from_dataset(filename: str) -> tuple[Workspace, str]:
    content, real_name = dataset_read_bytes(filename)
    df, meta = dataframe_from_bytes(content, real_name)
    meta["name"] = display_stem(real_name)
    ws = _register(df, meta, {"kind": "dataset", "filename": real_name})
    return ws, real_name


def dataset_read_bytes(filename: str) -> tuple[bytes, str]:
    from . import dataset_store
    return dataset_store.read_bytes(filename)


def _rebuild_base(source: dict) -> tuple[pd.DataFrame, dict]:
    """按来源重新解析出载入帧：明细始终来自数据集目录里的原始文件，日志里只有命令。"""
    kind = (source or {}).get("kind")
    if kind == "preset":
        return preset_dataframe(source["presetKey"], source.get("seed"))
    if kind in ("dataset", "upload"):
        name = source.get("filename") or ""
        try:
            content, real = dataset_read_bytes(name)
        except FileNotFoundError as exc:
            raise ValueError(f"来源文件 {name} 已不在数据集目录里，无法重建该工作区") from exc
        except ValueError as exc:
            raise ValueError(f"来源文件 {name} 不可读（上传时未落盘？）：{exc}") from exc
        return dataframe_from_bytes(content, real)
    if kind == "merge":
        names = source.get("filenames") or []
        if len(names) < 2:
            raise ValueError(f"合并工作区的来源记录不完整（应含两份以上文件名，实际 {len(names)} 份），无法重建")
        parsed = []
        for name in names:
            try:
                content, real = dataset_read_bytes(name)
            except FileNotFoundError as exc:
                raise ValueError(
                    f"合并来源文件 {name} 已不在数据集目录里，无法重建该工作区"
                    f"（合并帧只能靠这几份原始文件重拼）") from exc
            except ValueError as exc:
                raise ValueError(f"合并来源文件 {name} 不可读：{exc}") from exc
            df, meta = dataframe_from_bytes(content, real)
            parsed.append((real, df, meta))
        return merge_frames(parsed)
    raise ValueError(f"未知的载入来源：{kind}")


def reopen(ws_id: str) -> Workspace | None:
    """从命令日志重建一个内存里没有的工作区；没有日志返回 None（调用方按 404 处理）。"""
    doc = state_store.load_workspace_log(ws_id)
    if not doc:
        return None
    source = doc.get("source") or {}
    df, meta = _rebuild_base(source)
    meta["name"] = (doc.get("meta") or {}).get("name") or meta.get("name")
    # persist=False：日志还没重放完就落盘会把刚读到的那份历史写成空日志（实测踩过），
    # 重放失败时磁盘上留下的就是被清空的历史——一次只读访问不该能删掉别人的过去。
    ws = _register(df, meta, source, ws_id=ws_id, created_at=doc.get("createdAt"), persist=False)
    ops = doc.get("ops") or []
    if not ops:
        ws.persist_log()
        return ws
    ws.ops = ops
    try:
        # 整段日志按顺序重放，再回到断开时的游标位置：撤销与重做两边的历史都保留
        ws.restore(min(int(doc.get("cursor") or 0), len(ops)))
    except Exception as exc:
        # 重放半途而废就别把这颗帧留在注册表里：下一次 get() 会直接命中它，
        # 界面拿到的是一个少了外生变量列、版本号也不对的帧，每个数字都是错的。
        # 只摘"自己刚注册的那颗"：并发下这里可能已经是别人重建好的帧，无条件 pop
        # 等于把别人那一颗好帧删掉（它自己还握着引用继续跑，界面上就是两份历史）。
        with _LOCK:
            if _REGISTRY.get(ws_id) is ws:
                _REGISTRY.pop(ws_id, None)
                _ACCESS.pop(ws_id, None)
        raise ValueError(f"工作区 {ws_id} 按命令日志重建失败：{exc}") from exc
    return ws


def display_stem(filename: str) -> str:
    """数据集文件名去掉扩展名，作为工作区显示名（与前端 stem 习惯一致）。"""
    return re.sub(r"\.[^.]+$", "", filename or "dataset")


def get(ws_id: str) -> Workspace:
    global _ACCESS_TICK
    with _LOCK:
        ws = _REGISTRY.get(ws_id)
        if ws is not None:
            _ACCESS_TICK += 1
            _ACCESS[ws_id] = _ACCESS_TICK
            return ws
    # 不在内存不等于不存在：先看命令日志能否把它重建回来（重启/被淘汰都走这条路）。
    # 重建按 wsId 上锁并二次检查注册表：FastAPI 是多线程派发请求的，界面进第②步往往同时
    # 打 /meta、/rows、/quality 三条——没有这把锁就是三次各自解析原始文件、重放整段日志，
    # 后注册的那颗把前两颗顶掉（同一 wsId 短时间存在两份帧，数字还各不相同）。
    with _rebuild_lock(ws_id):
        with _LOCK:
            ws = _REGISTRY.get(ws_id)
            if ws is not None:
                _ACCESS_TICK += 1
                _ACCESS[ws_id] = _ACCESS_TICK
                return ws
        ws = reopen(ws_id)
        if ws is None:
            raise KeyError(ws_id)
        return ws


def close(ws_id: str) -> bool:
    """显式关闭：内存与命令日志一起清掉。被淘汰不是关闭，日志要留着。"""
    with _LOCK:
        _ACCESS.pop(ws_id, None)
        existed = _REGISTRY.pop(ws_id, None) is not None
    removed = state_store.delete_workspace_log(ws_id)
    return existed or removed


def resample_preview(ws: Workspace, minutes: int) -> dict:
    """重采样前给出的预测行数（真实分桶计数，不是估算），并说清这一趟是升还是降。

    升采样时"空桶"不是会丢的数据，而是**将要被填出来的格子**——预演里必须这么标，
    否则确认框上写着"2157 个桶为空"，用户点完才发现表里多了四分之三的补值。
    """
    if not ws.time_col or ws.time_col not in ws.df.columns:
        raise ValueError("没有可用的时间列")
    ts = pd.to_datetime(ws.df[ws.time_col], errors="coerce").dropna()
    if ts.empty:
        raise ValueError("时间列没有可解析的时间戳")
    step_ns = minutes * 60 * 1_000_000_000
    first = int(ts.min().value // step_ns) * step_ns
    last = int(ts.max().value // step_ns) * step_ns
    projected = (last - first) // step_ns + 1
    covered = len(set(int(v.value // step_ns) for v in ts))
    source_minutes = detect_freq_minutes(ts)
    direction = "same"
    if source_minutes:
        direction = "up" if minutes < source_minutes else ("down" if minutes > source_minutes else "same")
    empty = int(projected - covered)
    return {"targetMinutes": minutes, "projectedRows": int(projected), "currentRows": int(ws.df.shape[0]),
            "sourceMinutes": source_minutes, "direction": direction,
            "directionLabel": {"up": "升采样", "down": "降采样", "same": "同粒度重排"}[direction],
            "filledBuckets": int(covered), "emptyBuckets": empty,
            # 降采样时只有真数出了空桶才提「留空」：一个都不空还说这句话，等于在描述一次不会发生的丢数据。
            "emptyNote": ("这些空桶会被填上（值延续或线性插值），不是真实观测" if direction == "up"
                          else ("这些桶在原始数据里就没有观测，重采样后留空" if empty else "")),
            "compression": (int(ws.df.shape[0]) / int(projected)) if projected else None}


def list_workspaces() -> dict:
    """内存里的帧 + 磁盘上还没装载的命令日志。

    两者必须一起列：后端重启之后内存是空的，若只列 `_REGISTRY`，界面会显示"没有工作区"，
    而那几个 wsId 的历史其实好好躺在 state 目录里，一次访问就能重建。
    """
    with _LOCK:
        # 字段必须与磁盘那些条目一一对应：界面拿这份列表渲染"最近的工作区"，
        # 内存里少了 opsTotal/source 就等于这些工作区没有历史
        loaded = [{"wsId": w.id, "name": w.meta.get("name"), "rowCount": int(w.df.shape[0]),
                   "colCount": int(w.df.shape[1]), "version": w.version, "updatedAt": w.updated_at,
                   "opsTotal": len(w.ops), "source": dict(w.source), "loaded": True}
                  for w in _REGISTRY.values()]
    by_id = {item["wsId"]: item for item in loaded}
    for item in state_store.list_workspace_logs():
        cur = by_id.get(item["wsId"])
        # 内存里那份是活的，磁盘上的只会旧不会新；但 loaded/version 磁盘上更有说服力的是游标
        by_id[item["wsId"]] = cur or item
    items = sorted(by_id.values(), key=lambda x: x.get("updatedAt") or "", reverse=True)
    return {"count": len(items), "activeCount": len(loaded), "stateDir": str(state_store.state_dir()),
            "items": items}
