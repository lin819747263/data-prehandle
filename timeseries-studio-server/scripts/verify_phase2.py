"""第②期验收：清洗与异常检测全部走后端，并用一份独立实现逐格对拍。

为什么要"独立实现"：本脚本里参考版是照旧浏览器版（utils.js / store.js 里那套已被删掉的
循环写法）用纯 Python 重写一遍，不使用 app.services.quality 的任何函数。两套代码
（numpy 向量化 vs 逐行循环）给出同一批数字，才算跨实现自洽，而不是自己和自己比。

前置：uvicorn app.main:app --port 8000（本脚本不自启后端）。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_phase2.py
"""
from __future__ import annotations

import io
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- HTTP

def call(method: str, path: str, body: dict | None = None, raw: bytes | None = None,
         content_type: str | None = None) -> tuple[int, bytes]:
    data = None
    ct = content_type
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        ct = "application/json"
    elif raw is not None:
        data = raw
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if ct:
        req.add_header("Content-Type", ct)
    try:
        with urllib.request.urlopen(req, timeout=180) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def api(method: str, path: str, body: dict | None = None) -> dict:
    status, payload = call(method, path, body)
    text = payload.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"_raw": text[:400]}
    if status >= 400:
        raise SystemExit(f"请求失败 {method} {path} → {status} {text[:400]}")
    return parsed


def multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----tss" + uuid.uuid4().hex
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode("utf-8"))
    buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
    buf.write(content)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def upload(csv_text: str, filename: str = "probe.csv") -> dict:
    body, ctype = multipart("file", filename, csv_text.encode("utf-8"))
    status, payload = call("POST", "/api/ws", raw=body, content_type=ctype)
    if status >= 400:
        raise SystemExit(f"建区失败 → {status} {payload.decode('utf-8','replace')[:300]}")
    return json.loads(payload.decode("utf-8"))


# ---------------------------------------------------------------- 参考实现（逐行循环，照旧浏览器版）

def round4(x):
    """JS 的 parseFloat(v.toFixed(4))：按二进制精确值远离 0 进位。"""
    if x is None:
        return None
    v = float(x)
    if math.isnan(v) or math.isinf(v):
        return v
    return float(Decimal(v).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def num(cell: str):
    """CSV 单元格 → float；空串/非数字算缺失（与 pd.to_numeric(errors='coerce') 同）。"""
    s = (cell or "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def time_of(cell: str):
    s = (cell or "").strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def ref_runs(arr):
    """连续缺失闭区间，照旧 detectMissingSegments。"""
    runs = []
    start = -1
    for i, v in enumerate(arr):
        if v is None and start == -1:
            start = i
        elif v is not None and start != -1:
            runs.append((start, i - 1))
            start = -1
    if start != -1:
        runs.append((start, len(arr) - 1))
    return runs


def ref_fill(arr, start, end, algo):
    """照旧 imputeSegment：对区间内每个缺失点写入填补值（返回写入个数）。"""
    targets = [i for i in range(start, end + 1) if arr[i] is None]
    if not targets:
        return 0
    before, after = -1, -1
    i = start - 1
    while i >= 0:
        if arr[i] is not None:
            before = i
            break
        i -= 1
    i = end + 1
    while i < len(arr):
        if arr[i] is not None:
            after = i
            break
        i += 1
    v_before = arr[before] if before != -1 else 0.0
    v_after = arr[after] if after != -1 else v_before
    if algo == "zero":
        for k in targets:
            arr[k] = 0.0
    elif algo == "ffill":
        for k in targets:
            arr[k] = v_before
    elif algo == "spline":
        length = end - start + 1
        for k in targets:
            t = (k - start + 1) / (length + 1)
            h = -2 * t ** 3 + 3 * t * t
            arr[k] = round4(v_before + (v_after - v_before) * h)
    else:  # linear
        span = (after if after != -1 else end + 1) - (before if before != -1 else start - 1)
        for k in targets:
            pos = k - before if before != -1 else k - start + 1
            ratio = pos / span if span > 1 else 0.5
            arr[k] = round4(v_before + (v_after - v_before) * ratio)
    return len(targets)


def ref_quantile(sorted_vals, q):
    return sorted_vals[int(len(sorted_vals) * q)]


def ref_stats(arr):
    valid = sorted(v for v in arr if v is not None)
    n = len(valid)
    if n == 0:
        return {"n": 0, "missing": len(arr), "mean": None, "std": None, "median": None,
                "q1": None, "q3": None, "min": None, "max": None}
    mean = sum(valid) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in valid) / n)   # 总体标准差 ÷n
    mid = n // 2
    median = valid[mid] if n % 2 else (valid[mid - 1] + valid[mid]) / 2
    return {"n": n, "missing": len(arr) - n, "mean": mean, "std": std, "median": median,
            "q1": ref_quantile(valid, 0.25), "q3": ref_quantile(valid, 0.75),
            "min": valid[0], "max": valid[-1]}


def ref_median(values):
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def ref_detect(arr, algo, expr=None):
    """返回 (异常索引集合, lower, upper)，判定语义照旧 detectAnomalies。

    整列皆缺失（探针里的 allnan）时两边都必须给 (空集, None, None)，而不是 0 或 NaN：
    后端这时回 None，界面渲染成「—」；参考版若拿 None 参与算术就会自己先崩。
    """
    st = ref_stats(arr)
    hits = set()
    if st["n"] == 0:
        return hits, None, None
    if algo == "3sigma":
        lower, upper = st["mean"] - 3 * st["std"], st["mean"] + 3 * st["std"]
        hits = {i for i, v in enumerate(arr) if v is not None and (v < lower or v > upper)}
    elif algo == "iqr":
        iqr = st["q3"] - st["q1"]
        lower, upper = st["q1"] - 1.5 * iqr, st["q3"] + 1.5 * iqr
        hits = {i for i, v in enumerate(arr) if v is not None and (v < lower or v > upper)}
    elif algo == "iforest":
        win_size = 32
        lower, upper = math.inf, -math.inf
        for i, v in enumerate(arr):
            if v is None:
                continue
            win = [x for x in arr[max(0, i - win_size):i + 1] if x is not None]
            if len(win) < 5:
                continue
            med = ref_median(win)
            mad = sum(abs(x - med) for x in win) / len(win)
            lo, hi = med - 4 * mad, med + 4 * mad
            if v < lo or v > hi:
                hits.add(i)
            lower = min(lower, lo)
            upper = max(upper, hi)
        if lower == math.inf:
            lower, upper = st["min"], st["max"]
    elif algo == "expr":
        v = None
        for i, x in enumerate(arr):
            if x is None:
                continue
            v = x
            if eval(expr, {"__builtins__": {}}, {**st, "v": x, "abs": abs, "sqrt": math.sqrt,
                                                 "min": min, "max": max, "pow": pow}):
                hits.add(i)
        normal = [x for i, x in enumerate(arr) if x is not None and i not in hits]
        lower = min(normal) if normal else st["min"]
        upper = max(normal) if normal else st["max"]
    else:
        raise AssertionError(f"参考实现没有 {algo}")
    return hits, lower, upper


def ref_dedupe(rows, cols, time_col, float_keys, strategy):
    """照旧 imputeAllAndDedupe 的分组段：按时间分组、均值写回首行、其余删除。"""
    order = []
    groups = {}
    for i, r in enumerate(rows):
        key = r[time_col]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(i)
    dup_keys = [k for k in order if len(groups[k]) > 1]
    keep = []
    for k in order:
        idxs = groups[k]
        if len(idxs) == 1:
            keep.append(idxs[0])
            continue
        if strategy == "first":
            keep.append(idxs[0])
        elif strategy == "last":
            keep.append(idxs[-1])
        else:
            base = dict(rows[idxs[0]])
            for key in float_keys:
                vals = [rows[i][key] for i in idxs if rows[i][key] is not None]
                if vals:
                    base[key] = round4(sum(vals) / len(vals))
            rows[idxs[0]] = base
            keep.append(idxs[0])
    kept = [rows[i] for i in sorted(keep)]
    return kept, len(rows) - len(kept), len(dup_keys)


# ---------------------------------------------------------------- 断言

FAILURES: list[str] = []


def show(label, value):
    print(f"  {label}: {value}")


def check(name: str, cond: bool, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + str(detail)) if detail != '' else ''}")
    if not cond:
        FAILURES.append(name)
    return cond


def close_enough(a, b, tol=1e-9):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))


def column_of(ws: str, key: str) -> list:
    return api("GET", f"/api/ws/{ws}/columns?keys={urllib.parse.quote(key)}")["columns"][key]


def close_workspace(ws: str):
    call("DELETE", f"/api/ws/{ws}")


# ---------------------------------------------------------------- 探针数据

PROBE_HEADER = "timestamp,flow,temp,allnan,sparse,site"
# (时间, flow, temp, allnan, sparse, site)；空串=缺失
# 尾部 8 行是"正常密度"的观测点：3σ 判据里单个尖峰会把 σ 自己撑大，
# 有效样本 ≤10 时 9999 会躲在 mean+3σ 内侧（n>9 才抓得到），探针必须留出足够多的正常点。
PROBE_ROWS = [
    ("2024-05-01 00:00:00", "", "21.0", "", "5.0", "A"),        # 0 段首（左侧无观测）
    ("2024-05-01 00:15:00", "", "21.5", "", "5.5", "A"),        # 1 flow 0–1 段
    ("2024-05-01 00:30:00", "10.0", "22.0", "", "6.0", "A"),    # 2
    ("2024-05-01 00:45:00", "11.0", "22.5", "", "", "B"),       # 3 sparse 单点段（右侧有观测）
    ("2024-05-01 01:00:00", "12.0", "23.0", "", "7.0", "B"),    # 4
    ("2024-05-01 01:15:00", "", "", "", "", "B"),               # 5 flow 5–7 · temp 5–6 · sparse 5–6
    ("2024-05-01 01:30:00", "", "", "", "9.0", "B"),            # 6
    ("2024-05-01 01:45:00", "", "24.0", "", "10.0", "B"),       # 7
    ("2024-05-01 02:00:00", "20.0", "24.5", "", "11.0", "C"),   # 8
    ("2024-05-01 02:15:00", "9999.0", "25.0", "", "12.0", "C"), # 9 尖峰：3σ/IQR 都该抓到
    ("2024-05-01 02:30:00", "21.0", "25.5", "", "", "C"),       # 10 sparse 尾段起点
    ("2024-05-01 02:30:00", "22.0", "26.0", "", "13.0", "C"),   # 11 与 10 同一时间戳（重复）
    ("bad-time", "30.0", "27.0", "", "14.0", "D"),              # 12 NaT
    ("bad-time", "31.0", "27.5", "", "15.0", "D"),              # 13 NaT 之间也算重复
    ("2024-05-01 03:15:00", "23.0", "28.0", "", "16.0", "D"),   # 14
    ("2024-05-01 03:30:00", "24.0", "28.5", "", "17.0", "D"),   # 15
    ("2024-05-01 03:45:00", "25.0", "29.0", "", "18.0", "D"),   # 16
    ("2024-05-01 04:00:00", "26.0", "29.5", "", "19.0", "D"),   # 17
    ("2024-05-01 04:15:00", "27.0", "30.0", "", "20.0", "A"),   # 18
    ("2024-05-01 04:30:00", "28.0", "30.5", "", "21.0", "A"),   # 19
    ("2024-05-01 04:45:00", "29.0", "31.0", "", "22.0", "A"),   # 20
    ("2024-05-01 05:00:00", "32.0", "31.5", "", "23.0", "A"),   # 21
    ("2024-05-01 05:15:00", "33.0", "32.0", "", "24.0", "A"),   # 22
]


def probe_csv():
    lines = [PROBE_HEADER]
    for r in PROBE_ROWS:
        lines.append(",".join(r))
    return "\n".join(lines) + "\n"


def probe_arrays():
    """参考版看到的列（flow/temp/allnan/sparse 为 float 数组，site 为类别）。"""
    return {
        "flow": [num(r[1]) for r in PROBE_ROWS],
        "temp": [num(r[2]) for r in PROBE_ROWS],
        "allnan": [num(r[3]) for r in PROBE_ROWS],
        "sparse": [num(r[4]) for r in PROBE_ROWS],
    }, [time_of(r[0]) for r in PROBE_ROWS]


FLOAT_KEYS = ["flow", "temp", "allnan", "sparse"]


# ---------------------------------------------------------------- 主流程

def main() -> int:
    health = api("GET", "/api/health")
    print("\n== /api/health ==")
    show("版本", health["version"])
    need = ["workspace:quality", "workspace:series", "workspace:anomaly", "anomaly:detect",
            "op:impute", "op:anomaly-repair", "op:mask-generate", "op:mask-delete"]
    check("能力声明含第②期全部入口", all(c in health["capabilities"] for c in need),
          [c for c in need if c not in health["capabilities"]])
    check("旧的 /api/anomaly/iforest 通道已下线", "anomaly:iforest" not in health["capabilities"])

    arrays, times = probe_arrays()
    labels = [t.strftime("%Y-%m-%d %H:%M:%S") if t else None for t in times]

    # ---------- 1. /quality 与参考实现对拍 ----------
    print("\n== 1. GET /quality：缺失段与重复时间戳 ==")
    created = upload(probe_csv())
    ws = created["meta"]["wsId"]
    show("工作区", f"{ws} · {created['meta']['rowCount']} 行 × {created['meta']['colCount']} 列")
    show("列类型", [(c["key"], c["type"]) for c in created["meta"]["columns"]])
    q = api("GET", f"/api/ws/{ws}/quality")
    total_missing_ref = 0
    for key in FLOAT_KEYS:
        miss = sum(1 for v in arrays[key] if v is None)
        total_missing_ref += miss
        row = next(c for c in q["columns"] if c["key"] == key)
        check(f"{key} 缺失数", row["missing"] == miss, f"服务端 {row['missing']} / 参考 {miss}")
        check(f"{key} 缺失率", close_enough(row["rate"], miss / len(PROBE_ROWS) * 100),
              f"{row['rate']:.4f}%")
    site_missing = sum(1 for r in PROBE_ROWS if r[5].strip() == "")
    time_missing = sum(1 for t in times if t is None)
    total_missing_ref += site_missing + time_missing
    check("类别列按空串算缺失",
          next(c for c in q["columns"] if c["key"] == "site")["missing"] == site_missing, site_missing)
    check("整列为空的列也算数值列", next(c for c in q["columns"] if c["key"] == "allnan")["type"] == "float")
    check("时间列缺失=NaT 个数", next(c for c in q["columns"] if c["key"] == "timestamp")["missing"] == 2,
          next(c for c in q["columns"] if c["key"] == "timestamp")["missing"])
    check("总缺失单元格", q["totalMissingCells"] == total_missing_ref,
          f"{q['totalMissingCells']} vs {total_missing_ref}")
    check("重复行数", q["duplicateRows"] == 2, q["duplicateRows"])
    check("重复组数", q["duplicateGroups"] == 2, q["duplicateGroups"])
    for key in FLOAT_KEYS:
        ref = [{"startIdx": a, "endIdx": b, "count": b - a + 1,
                "startTime": labels[a], "endTime": labels[b]} for a, b in ref_runs(arrays[key])]
        got = q["segments"].get(key, [])
        check(f"{key} 缺失段逐条一致", got == ref, f"服务端 {len(got)} 段 / 参考 {len(ref)} 段")
        if ref:
            show(f"{key} 段", "、".join(f"[{s['startIdx']}–{s['endIdx']}]{s['startTime']}" for s in ref))
    check("无缺失列不出现在 segments 里", "timestamp" not in q["segments"])
    check("响应体不含明细行", len(json.dumps(q)) < 4096, f"{len(json.dumps(q))} 字节")

    # ---------- 2. 四种填补算法逐格对拍 ----------
    print("\n== 2. POST /op/impute：四种算法逐格对拍 ==")
    for algo in ("linear", "ffill", "spline", "zero"):
        w = upload(probe_csv())["meta"]["wsId"]
        qq = api("GET", f"/api/ws/{w}/quality")
        targets = [{"key": key, "startIdx": s["startIdx"], "endIdx": s["endIdx"], "algo": algo}
                   for key in FLOAT_KEYS for s in qq["segments"].get(key, [])]
        ref_arrays = {k: list(v) for k, v in arrays.items()}
        filled_ref = 0
        for key in FLOAT_KEYS:
            for a, b in ref_runs(ref_arrays[key]):
                filled_ref += ref_fill(ref_arrays[key], a, b, algo)
        r = api("POST", f"/api/ws/{w}/op/impute", {"targets": targets})
        bad = []
        for key in FLOAT_KEYS:
            got = column_of(w, key)
            for i, expect in enumerate(ref_arrays[key]):
                if got[i] is None and expect is None:
                    continue
                if not close_enough(got[i], expect):
                    bad.append(f"{key}[{i}] 服务端 {got[i]} 参考 {expect}")
        check(f"{algo}：填补 {filled_ref} 个单元格后逐格一致", not bad, f"{r['summary']}｜{bad[:3]}")
        check(f"{algo}：响应自报填补数与逐格差异一致", r["filled"] == filled_ref,
              f"{r['filled']} vs {filled_ref}")
        # 填补后重新诊断：数值段全空，只剩下时间列那两个解析不出的时间戳（time_missing）
        q2 = api("GET", f"/api/ws/{w}/quality")
        check(f"{algo}：填补后 segments 全空、缺失只剩时间列",
              q2["segments"] == {} and q2["totalMissingCells"] == time_missing,
              q2["totalMissingCells"])
        close_workspace(w)

    # ---------- 3. 重复时间戳合并 ----------
    print("\n== 3. /op/impute + dedupe：重复时间戳合并 ==")
    for strategy in ("mean", "first", "last"):
        w = upload(probe_csv())["meta"]["wsId"]
        rows = [{k: (times[i] if k == "timestamp" else arrays[k][i]) for k in ["timestamp", *FLOAT_KEYS]}
                | {"site": PROBE_ROWS[i][5]} for i in range(len(PROBE_ROWS))]
        kept, removed, groups = ref_dedupe([dict(r) for r in rows], None, "timestamp",
                                           FLOAT_KEYS, strategy)
        r = api("POST", f"/api/ws/{w}/op/impute", {"targets": [], "dedupe": strategy})
        check(f"{strategy}：删除行数", r["deduped"] == removed, f"{r['deduped']} vs {removed}")
        check(f"{strategy}：组数", r["duplicateGroups"] == groups and r["duplicateGroups"] == 2, groups)
        check(f"{strategy}：剩余行数", r["rowCount"] == len(kept), f"{r['rowCount']} vs {len(kept)}")
        ts_col = column_of(w, "timestamp")
        flow_col = column_of(w, "flow")
        sparse_col = column_of(w, "sparse")
        bad = []
        for i, row in enumerate(kept):
            if ts_col[i] != (row["timestamp"].strftime("%Y-%m-%d %H:%M:%S") if row["timestamp"] else None):
                bad.append(f"第 {i} 行时间 {ts_col[i]} 参考 {row['timestamp']}")
            for key, got in (("flow", flow_col), ("sparse", sparse_col)):
                if not close_enough(got[i], row[key]):
                    bad.append(f"{key}[{i}] 服务端 {got[i]} 参考 {row[key]}")
        check(f"{strategy}：合并后逐行逐格一致", not bad, bad[:3])
        close_workspace(w)

    # ---------- 4. all 模式：超过缺失段上限也能全表填补 ----------
    print("\n== 4. /op/impute all=true：缺失段超过上限时的全表扫描 ==")
    big_lines = ["timestamp,lonely"]
    seg_total = 0
    for i in range(1200):
        if i % 3 == 1:
            v = ""
            seg_total += 1
        else:
            v = f"{i % 97}.5"
        big_lines.append(f"2024-07-01 {i // 60:02d}:{i % 60:02d}:00,{v}")
    bw = upload("\n".join(big_lines) + "\n", "lonely.csv")["meta"]["wsId"]
    bq = api("GET", f"/api/ws/{bw}/quality")
    check("缺失段列表被截断", bq["segmentsTruncated"]["lonely"] is True,
          f"{len(bq['segments']['lonely'])} 段 · 上限 {bq['segmentCap']}")
    check("整列缺失数真实", bq["columns"][1]["missing"] == seg_total, f"{seg_total} 个")
    br = api("POST", f"/api/ws/{bw}/op/impute", {"all": True, "keys": ["lonely"], "defaultAlgo": "linear"})
    check("all 模式填掉全部缺失", br["filled"] == seg_total, f"{br['filled']} vs {seg_total}")
    check("段数以 segmentCount 回带", br["segmentCount"] == seg_total, br["segmentCount"])
    check("明细按上限截断", len(br["segments"]) <= 200 and br["segmentsTruncated"] is True, len(br["segments"]))
    check("填补后无缺失", api("GET", f"/api/ws/{bw}/quality")["totalMissingCells"] == 0)
    close_workspace(bw)

    # ---------- 5. 检测：五种算法的判定集合逐点一致 ----------
    print("\n== 5. /anomaly-detect：判定集合与边界 ==")
    expr = "v > mean + 2.5 * std"
    for algo, expr_str in (("3sigma", None), ("iqr", None), ("iforest", None), ("expr", expr)):
        w = upload(probe_csv())["meta"]["wsId"]
        payload = {"algo": algo}
        if expr_str:
            payload["expr"] = expr_str
        det = api("POST", f"/api/ws/{w}/anomaly-detect", payload)["detection"]
        check(f"{algo}：engine 说明真实", bool(det["engine"]), det["engine"])
        # 用 mask_only 修复把判定集合落成 0/1 列，再逐点比对（顺带验证修复链路）
        ref_all = {key: ref_detect(arrays[key], algo, expr_str) for key in FLOAT_KEYS}
        total_hits = sum(len(h) for h, _, _ in ref_all.values())
        check(f"{algo}：探针确实制造出了判定点", total_hits > 0, total_hits)
        api("POST", f"/api/ws/{w}/op/anomaly-repair", {"repair": "mask_only"})
        bad = []
        for key in FLOAT_KEYS:
            hits, lower, upper = ref_all[key]
            row = next(r for r in det["results"] if r["key"] == key)
            if row["anomalies"] != len(hits):
                bad.append(f"{key} 个数 {row['anomalies']} 参考 {len(hits)}")
            for f, expect in (("lower", lower), ("upper", upper)):
                if not close_enough(row[f], expect):
                    bad.append(f"{key}.{f} 服务端 {row[f]} 参考 {expect}")
            if not hits:
                # 零命中列不会生成掩码列（后端跳过），两边都当"无异常"
                continue
            mask = column_of(w, f"anomaly_mask_{key}")
            for i in range(len(arrays[key])):
                expect = 1 if i in hits else 0
                if mask[i] != expect:
                    bad.append(f"{key}[{i}] 掩码 {mask[i]} 参考 {expect}")
        check(f"{algo}：{total_hits} 个判定点逐点一致（掩码列回读）", not bad, bad[:4])
        check(f"{algo}：summary 合计", det["summary"]["totalAnomalies"] == total_hits,
              f"{det['summary']['totalAnomalies']} · 整体率 {det['summary']['overallRate']:.4f}%")
        close_workspace(w)

    print("\n== 5b. sklearn 完整版孤立森林（同一列两条实现）==")
    w = upload(probe_csv())["meta"]["wsId"]
    det = api("POST", f"/api/ws/{w}/anomaly-detect",
              {"algo": "iforest_sklearn", "nEstimators": 120, "contamination": 0.1,
               "randomState": 42})["detection"]
    show("engine", det["engine"])
    for r in det["results"]:
        show(f"{r['key']}", f"异常 {r['anomalies']}/{r['total']} · 边界 [{r['lower']}, {r['upper']}]")
    check("行数与列数来自真实帧", det["rowCount"] == len(PROBE_ROWS) and det["summary"]["numCols"] == 4)
    check("同参数两次检测结果一致", json.dumps(api("POST", f"/api/ws/{w}/anomaly-detect",
          {"algo": "iforest_sklearn", "nEstimators": 120, "contamination": 0.1, "randomState": 42})
          ["detection"]["results"], sort_keys=True)
          == json.dumps(det["results"], sort_keys=True), "random_state 固定后可复现")
    close_workspace(w)

    # ---------- 6. 修复：clip / nan_impute 逐格对拍 ----------
    print("\n== 6. /op/anomaly-repair：clip 与 nan_impute ==")
    for mode in ("clip", "nan_impute"):
        w = upload(probe_csv())["meta"]["wsId"]
        api("POST", f"/api/ws/{w}/anomaly-detect", {"algo": "iqr"})
        r = api("POST", f"/api/ws/{w}/op/anomaly-repair", {"repair": mode})
        expect_touched = sum(len(ref_detect(arrays[k], "iqr")[0]) for k in FLOAT_KEYS)
        check(f"{mode}：处理点数", r["touched"] == expect_touched, f"{r['touched']} vs {expect_touched}")
        bad = []
        for key in FLOAT_KEYS:
            hits, lower, upper = ref_detect(arrays[key], "iqr")
            expect = list(arrays[key])
            if mode == "clip":
                for i in hits:
                    expect[i] = round4(lower) if expect[i] < lower else round4(upper)
            else:
                # nan_impute 只处理"有异常点的那些列"（旧浏览器版同样按检测结果的列循环）：
                # 没有异常点的列里，原本就存在的缺失段一律不动
                if hits:
                    for i in hits:
                        expect[i] = None
                    for a, b in ref_runs(expect):
                        ref_fill(expect, a, b, "linear")
            got = column_of(w, key)
            for i in range(len(expect)):
                if not close_enough(got[i], expect[i]):
                    bad.append(f"{key}[{i}] 服务端 {got[i]} 参考 {expect[i]}")
        check(f"{mode}：修复后逐格一致", not bad, bad[:4])
        close_workspace(w)

    # ---------- 7. 新鲜度闸门 ----------
    print("\n== 7. 检测缓存的新鲜度：改过数据就必须重测 ==")
    w = upload(probe_csv())["meta"]["wsId"]
    st, pl = call("POST", f"/api/ws/{w}/op/anomaly-repair", {"repair": "clip"})
    check("没有检测就修复被拒绝", st == 400, pl.decode("utf-8", "replace")[:80])
    api("POST", f"/api/ws/{w}/anomaly-detect", {"algo": "3sigma"})
    check("刚检测完 meta.anomaly.stale=False",
          api("GET", f"/api/ws/{w}")["anomaly"]["stale"] is False)
    mask = api("POST", f"/api/ws/{w}/op/mask-generate", {"maskName": "curtail", "startIdx": 2, "endIdx": 4})
    check("生成掩码不算改数值（检测仍新鲜）",
          api("GET", f"/api/ws/{w}")["anomaly"]["stale"] is False, mask["summary"])
    api("POST", f"/api/ws/{w}/op/convert-unit", {"key": "flow", "factor": 2, "offset": 0, "newUnit": "x2"})
    meta_after = api("GET", f"/api/ws/{w}")
    check("换算数值后检测被判失效", meta_after["anomaly"]["stale"] is True, meta_after["anomaly"])
    st, pl = call("POST", f"/api/ws/{w}/op/anomaly-repair", {"repair": "clip"})
    check("失效后拒绝修复", st == 400, pl.decode("utf-8", "replace")[:80])
    s = api("GET", f"/api/ws/{w}/series?cols=flow")
    sc = s["series"][0]
    check("失效后曲线不再带异常覆盖层", sc["anomalyCount"] == 0 and s["anomaly"]["stale"] is True,
          f"{sc['anomalyCount']} 点 · stale={s['anomaly']['stale']}")
    back = api("POST", f"/api/ws/{w}/restore", {"version": 1})
    check("回退版本后检测缓存被清空", back["meta"]["anomaly"] is None, back["meta"]["anomaly"])
    close_workspace(w)

    # ---------- 8. 曲线 /series（多列）----------
    print("\n== 8. GET /series：多列共享抽稀时间轴、原始行号与逐列覆盖层 ==")
    w = upload(probe_csv())["meta"]["wsId"]
    s = api("GET", f"/api/ws/{w}/series?cols=flow&points=20")
    sc = s["series"][0]
    check("抽稀后点数不超请求上限", len(s["x"]) <= 20, f"{len(s['x'])} 点")
    check("原始行号严格递增", all(b > a for a, b in zip(s["idx"], s["idx"][1:])), s["idx"])
    check("抽稀点保留尖峰所在行", 9 in s["idx"], f"idx={s['idx']}（9999 在第 9 行）")
    check("缺失点留在序列里", sc["y"].count(None) >= 1 and sc["missingCount"] == 5,
          f"None×{sc['y'].count(None)} · 整列缺失 {sc['missingCount']}")
    # 抽稀把缺失行抽掉时虚线会少画，这时必须点亮截断标志：图不能反过来证明「数据里没有」
    flow_missing = {i for i, v in enumerate(arrays["flow"]) if v is None}
    on_axis_missing = len(flow_missing & set(s["idx"]))
    check("虚线只画抽稀轴上留下的缺失行，少画则点亮截断",
          all(t in s["x"] for t in sc["missingMarks"]) and len(sc["missingMarks"]) == on_axis_missing
          and sc["marksTruncated"] is (on_axis_missing < sc["missingCount"]),
          f"画 {len(sc['missingMarks'])}/{sc['missingCount']} 条 · 轴上缺失 {on_axis_missing} · 截断={sc['marksTruncated']}")
    check("x 用时间渲染", s["x"][0] == "2024-05-01 00:00:00", s["x"][0])
    det = api("POST", f"/api/ws/{w}/anomaly-detect", {"algo": "iqr"})["detection"]
    s2 = api("GET", f"/api/ws/{w}/series?cols=flow,temp,sparse")
    check("逐列各回一条序列且与时间轴等长",
          [m["col"] for m in s2["series"]] == ["flow", "temp", "sparse"]
          and all(len(m["y"]) == len(s2["x"]) for m in s2["series"]),
          f"{[(m['col'], len(m['y'])) for m in s2['series']]} / x {len(s2['x'])}")
    check("检测算法随响应带回", s2["anomaly"]["algo"] == "iqr" and s2["anomaly"]["stale"] is False, s2["anomaly"])
    ref_hits = sorted(ref_detect(arrays["flow"], "iqr")[0])
    sc2 = next(m for m in s2["series"] if m["col"] == "flow")
    check("覆盖层点数与检测一致", sc2["anomalyCount"] == len(ref_hits), f"{sc2['anomalyCount']} vs {len(ref_hits)}")
    check("覆盖层就是检测到的那些行", [labels[i] for i in ref_hits] == [a[0] for a in sc2["anomalies"]],
          f"{[a[0] for a in sc2['anomalies']]}")
    check("覆盖层带原始值", all(close_enough(a[1], arrays['flow'][labels.index(a[0])]) for a in sc2["anomalies"]))
    per_col = {m["col"]: m for m in api("GET", f"/api/ws/{w}/series?cols=flow")["series"]}
    check("单列与多列的覆盖层一致", per_col["flow"]["anomalyCount"] == sc2["anomalyCount"]
          and per_col["flow"]["anomalies"] == sc2["anomalies"],
          f"{sc2['anomalyCount']} vs {per_col['flow']['anomalyCount']}")
    check("曲线响应很小（明细不出后端）", len(json.dumps(s2)) < 16384, f"{len(json.dumps(s2))} 字节")
    close_workspace(w)

    # ---------- 9. 掩码列 ----------
    print("\n== 9. 掩码：生成 / 删除 / 类型保留 ==")
    w = upload(probe_csv())["meta"]["wsId"]
    m = api("POST", f"/api/ws/{w}/op/mask-generate", {"maskName": "mask_curtail", "startIdx": 3, "endIdx": 6})
    check("掩码区间与计数", m["onesCount"] == 4 and m["startIdx"] == 3 and m["endIdx"] == 6, m["summary"])
    check("起止时间用真实渲染值", m["startTime"] == labels[3] and m["endTime"] == labels[6],
          f"{m['startTime']} ~ {m['endTime']}")
    col = column_of(w, "mask_curtail")
    check("列内容就是 0/1", col == [1 if 3 <= i <= 6 else 0 for i in range(len(PROBE_ROWS))])
    types = {c["key"]: c["type"] for c in api("GET", f"/api/ws/{w}")["columns"]}
    check("掩码列类型为 binary（不会混进数值检测）", types["mask_curtail"] == "binary", types)
    det_cols = api("POST", f"/api/ws/{w}/anomaly-detect", {"algo": "3sigma"})["detection"]
    check("检测只看数值列", [r["key"] for r in det_cols["results"]] == FLOAT_KEYS,
          [r["key"] for r in det_cols["results"]])
    api("POST", f"/api/ws/{w}/op/convert-unit", {"key": "temp", "factor": 1.8, "offset": 32, "newUnit": "F"})
    types = {c["key"]: c["type"] for c in api("GET", f"/api/ws/{w}")["columns"]}
    check("改完数据后掩码列仍是 binary", types["mask_curtail"] == "binary", types)
    d = api("POST", f"/api/ws/{w}/op/mask-delete", {"keys": ["mask_curtail", "不存在的"]})
    check("删除掩码：只删真实存在的", d["deleted"] == ["mask_curtail"], d["summary"])
    check("掩码列已从元数据消失",
          "mask_curtail" not in [c["key"] for c in api("GET", f"/api/ws/{w}")["columns"]])
    st, pl = call("POST", f"/api/ws/{w}/op/mask-delete", {"keys": ["mask_curtail"]})
    check("全都不存在时明确报错", st == 400, pl.decode("utf-8", "replace")[:80])
    close_workspace(w)

    # ---------- 10. 错误路径与安全 ----------
    print("\n== 10. 错误路径：过期快照 / 越界 / 表达式白名单 ==")
    w = upload(probe_csv())["meta"]["wsId"]
    qq = api("GET", f"/api/ws/{w}/quality")
    first = qq["segments"]["flow"][0]
    tgt = [{"key": "flow", "startIdx": first["startIdx"], "endIdx": first["endIdx"], "algo": "linear"}]
    api("POST", f"/api/ws/{w}/op/impute", {"targets": tgt})
    st, pl = call("POST", f"/api/ws/{w}/op/impute", {"targets": tgt})
    check("重复填补同一段被拒绝（快照过期）", st == 400, pl.decode("utf-8", "replace")[:90])
    st, pl = call("POST", f"/api/ws/{w}/op/impute", {"targets": []})
    check("空 targets 且无 dedupe 被拒绝", st == 400, pl.decode("utf-8", "replace")[:70])
    st, pl = call("POST", f"/api/ws/{w}/op/impute", {"targets": [{"key": "site", "startIdx": 0, "endIdx": 1}]})
    check("对类别列填补被拒绝", st == 400, pl.decode("utf-8", "replace")[:90])
    frame_after = api("GET", f"/api/ws/{w}")
    check("失败的命令不进版本序列", frame_after["version"] == 1, frame_after["version"])
    st, pl = call("POST", f"/api/ws/{w}/op/mask-generate", {"maskName": "2bad", "startIdx": 0, "endIdx": 1})
    check("非法掩码名被拒绝", st == 400, pl.decode("utf-8", "replace")[:90])
    st, pl = call("POST", f"/api/ws/{w}/op/mask-generate", {"maskName": "ok", "startIdx": 0, "endIdx": 99})
    check("掩码越界被拒绝", st == 400, pl.decode("utf-8", "replace")[:90])
    for bad_expr, why in (("__import__('os').system('ls')", "导入被白名单挡住"),
                          ("v > mean; import os", "分号/多语句被挡住"),
                          ("lambda: 1", "lambda 被挡住"),
                          ("v > mean < std", "链式比较被挡住"),
                          ("foo(v)", "未知函数被挡住")):
        st, pl = call("POST", f"/api/ws/{w}/anomaly-detect", {"algo": "expr", "expr": bad_expr})
        check(f"表达式拒绝：{why}", st in (400, 422),
              f"HTTP {st} · {pl.decode('utf-8','replace')[:90]}")
    st, pl = call("POST", f"/api/ws/{w}/anomaly-detect", {"algo": "不存在"})
    check("未知算法被 schema 拒绝", st == 422, f"HTTP {st}")
    st, pl = call("POST", f"/api/ws/{w}/anomaly-detect", {"algo": "iforest_sklearn", "contamination": 5})
    check("污染率越界被拒绝", st == 422, f"HTTP {st}")
    check("错误路径没有改数据", api("GET", f"/api/ws/{w}")["version"] == 1)
    close_workspace(w)

    # ---------- 10b. 撤销 / 重做：命令日志是游标，回退不销毁尾部 ----------
    print("\n== 10b. 撤销与重做：restore 只挪游标，新命令才截掉重做尾部 ==")
    w = upload(probe_csv())["meta"]["wsId"]
    seg = api("GET", f"/api/ws/{w}/quality")["segments"]["flow"][0]
    api("POST", f"/api/ws/{w}/op/impute",
        {"targets": [{"key": "flow", "startIdx": seg["startIdx"], "endIdx": seg["endIdx"], "algo": "linear"}]})
    flow_v1 = column_of(w, "flow")
    temp_plain = column_of(w, "temp")
    api("POST", f"/api/ws/{w}/op/convert-unit", {"key": "temp", "factor": 2, "offset": 0, "newUnit": "x2"})
    temp_x2 = column_of(w, "temp")
    v2 = api("GET", f"/api/ws/{w}")
    check("两条命令 → 版本 2、日志两条", v2["version"] == 2 and len(v2["ops"]) == 2,
          f"version={v2['version']} · ops={len(v2['ops'])}")
    check("命令的入参如实记录", [o["params"].get("factor") for o in v2["ops"] if o["kind"] == "convert_unit"] == [2.0],
          [o["kind"] for o in v2["ops"]])

    u = api("POST", f"/api/ws/{w}/restore", {"version": 1})
    check("撤销：版本回到 1", u["meta"]["version"] == 1, u["meta"]["version"])
    check("撤销：界面只见已生效的那 1 条命令（重做尾部不属于当前帧）",
          len(u["meta"]["ops"]) == 1 and u["meta"]["ops"][0]["kind"] == "impute", u["meta"]["ops"])
    check("撤销：temp 逐格回到换算前", column_of(w, "temp") == temp_plain)
    check("撤销：更早的填补仍然生效", column_of(w, "flow") == flow_v1)

    r = api("POST", f"/api/ws/{w}/restore", {"version": 2})
    check("重做：同一份日志往前再放一遍即可回到版本 2", r["meta"]["version"] == 2 and len(r["meta"]["ops"]) == 2,
          f"version={r['meta']['version']} · ops={len(r['meta']['ops'])}")
    check("重做：temp 与撤销前逐格相同（不是重算出另一套值）", column_of(w, "temp") == temp_x2)

    api("POST", f"/api/ws/{w}/restore", {"version": 1})
    api("POST", f"/api/ws/{w}/op/convert-unit", {"key": "temp", "factor": 10, "offset": 0, "newUnit": "x10"})
    branch = api("GET", f"/api/ws/{w}")
    check("撤销后另起一条命令 → 分支仍是版本 2", branch["version"] == 2, branch["version"])
    check("撤销后另起一条命令 → 旧的 x2 那条被截掉",
          [o["params"].get("factor") for o in branch["ops"] if o["kind"] == "convert_unit"] == [10.0],
          branch["ops"])
    check("分支帧用的是新参数（temp×10，不是 (temp×2)×…）",
          all(close_enough(a, (b or 0) * 10) if b is not None else a is None
              for a, b in zip(column_of(w, "temp"), temp_plain)))
    st, pl = call("POST", f"/api/ws/{w}/restore", {"version": 3})
    check("日志只剩 2 条时，重做不到第 3 版并说明原因", st == 400,
          pl.decode("utf-8", "replace")[:90])
    close_workspace(w)

    # ---------- 10c. 跨"异常修复"的往返：修复命令必须自带检测索引才可重放 ----------
    print("\n== 10c. 检测→修复→撤销→重做：修复命令按日志重放，不依赖当时的检测缓存 ==")
    w = upload(probe_csv())["meta"]["wsId"]
    api("POST", f"/api/ws/{w}/anomaly-detect", {"algo": "3sigma"})
    api("POST", f"/api/ws/{w}/op/anomaly-repair", {"repair": "clip"})
    keys_v1 = [c["key"] for c in api("GET", f"/api/ws/{w}")["columns"]]
    flow_clip = column_of(w, "flow")
    temp_clip = column_of(w, "temp")
    det2 = api("POST", f"/api/ws/{w}/anomaly-detect", {"algo": "3sigma"})["detection"]  # 截断后必须重测
    counts = {r["key"]: r["anomalies"] for r in det2["results"] if r["anomalies"]}
    mk = api("POST", f"/api/ws/{w}/op/anomaly-repair", {"repair": "mask_only"})
    masks = mk["maskCols"]
    keys_v2 = [c["key"] for c in api("GET", f"/api/ws/{w}")["columns"]]
    check("生成掩码只加列、不动数值", mk["touched"] == sum(counts.values()) and set(keys_v1) < set(keys_v2),
          f"{len(masks)} 个掩码列 · 处理 {mk['touched']} 点")
    check("掩码列与检出列一一对应", sorted(masks) == sorted(f"anomaly_mask_{k}" for k in counts), masks)
    check("每个掩码列的 1 个数等于该列检出异常数",
          all(sum(column_of(w, f"anomaly_mask_{k}")) == c for k, c in counts.items()),
          {k: sum(column_of(w, f"anomaly_mask_{k}")) for k in list(counts)[:3]})
    masks_before = {k: column_of(w, k) for k in masks}

    back = api("POST", f"/api/ws/{w}/restore", {"version": 1})
    keys_back = [c["key"] for c in back["meta"]["columns"]]
    check("撤销掉掩码命令：列回到截断后那一版", keys_back == keys_v1, f"{len(keys_back)} 列 vs {len(keys_v1)} 列")
    check("撤销后重放出的数值与当时逐格一致（clip 可重放）",
          column_of(w, "flow") == flow_clip and column_of(w, "temp") == temp_clip)
    check("撤销后不再挂着任何掩码列",
          not [k for k in keys_back if k.startswith("anomaly_mask_")], keys_back)

    fwd = api("POST", f"/api/ws/{w}/restore", {"version": 2})
    keys_fwd = [c["key"] for c in fwd["meta"]["columns"]]
    check("重做掩码命令：不必重新检测也能把列加回来", keys_fwd == keys_v2, f"{len(keys_fwd)} 列 vs {len(keys_v2)} 列")
    check("重做后掩码列内容与被撤销前逐格一致",
          all(column_of(w, k) == masks_before[k] for k in masks), list(masks)[:2])
    check("重做之后仍然要求重新检测（检测缓存不随重做复活）",
          api("GET", f"/api/ws/{w}")["anomaly"] is None)
    close_workspace(w)

    # ---------- 11. 大表：第②期入口的体积与耗时 ----------
    print("\n== 11. 大表 big40.csv（11000 行 × 40 列 = 440000 格）==")
    big_path = ROOT / "dataset" / "big40.csv"
    if not big_path.exists():
        raise SystemExit("缺少 dataset/big40.csv，请先运行 scripts/make_big_csv.py")
    show("文件", f"{big_path.stat().st_size/1024/1024:.2f} MiB")
    t0 = time.perf_counter()
    big = upload(big_path.read_bytes().decode("utf-8"), "big40.csv")
    bw = big["meta"]["wsId"]
    show("建区", f"{(time.perf_counter()-t0)*1000:.0f} ms · {big['meta']['rowCount']} 行 × "
         f"{big['meta']['colCount']} 列")
    t0 = time.perf_counter()
    status, payload = call("GET", f"/api/ws/{bw}/quality")
    bq = json.loads(payload.decode("utf-8"))
    show("/quality", f"{(time.perf_counter()-t0)*1000:.0f} ms · 响应 {len(payload)/1024:.1f} KiB · "
         f"{bq['rowCount']} 行 · 缺失 {bq['totalMissingCells']} 格 · 段数上限 {bq['segmentCap']}")
    check("大表 quality 响应远小于原表", len(payload) < big_path.stat().st_size / 20,
          f"{len(payload)/1024:.1f} KiB vs {big_path.stat().st_size/1024/1024:.2f} MiB")
    t0 = time.perf_counter()
    status, payload = call("POST", f"/api/ws/{bw}/anomaly-detect", {"algo": "3sigma"})
    bd = json.loads(payload.decode("utf-8"))["detection"]
    show("/anomaly-detect(3σ)", f"{(time.perf_counter()-t0)*1000:.0f} ms · 响应 {len(payload)/1024:.1f} KiB · "
         f"{bd['summary']['totalAnomalies']} 点 / {bd['summary']['numCols']} 列 · 整体率 "
         f"{bd['summary']['overallRate']:.4f}%")
    check("检测响应不含行索引", "perColumn" not in json.dumps(bd) and "anomalyIndices" in bd)
    t0 = time.perf_counter()
    status, payload = call("GET", f"/api/ws/{bw}/series?cols=%E4%BC%A0%E6%84%9F%E5%99%A801,%E4%BC%A0%E6%84%9F%E5%99%A802&points=1200")
    bs = json.loads(payload.decode("utf-8"))
    b0 = bs["series"][0]
    show("/series(2 列)", f"{(time.perf_counter()-t0)*1000:.0f} ms · {len(bs['x'])} 点 · 响应 "
         f"{len(payload)/1024:.1f} KiB · 覆盖层 {b0['anomalyCount']} 点（画 {len(b0['anomalies'])} 个）")
    check("抽稀后点数远小于行数", len(bs["x"]) <= 1200 and len(bs["x"]) < bs["rowCount"] / 4,
          f"{len(bs['x'])} / {bs['rowCount']}")
    check("每列都按同一条时间轴返回", all(len(m["y"]) == len(bs["x"]) for m in bs["series"])
          and len(bs["series"]) == 2, [m["col"] for m in bs["series"]])
    check("覆盖层未超过上限", b0["anomalyCount"] <= 4000
          and len(b0["anomalies"]) <= 2000,
          f"{b0['anomalyCount']} · 截断={b0['anomaliesTruncated']}")
    # 与 pandas 独立对拍：3σ 判定点数 = 各列 |x-mean|/std 超 3 的个数
    import numpy as np
    import pandas as pd
    ref = pd.read_csv(big_path, encoding="utf-8")
    total_ref = 0
    for key in [c for c in ref.columns if c != "timestamp"]:
        s = pd.to_numeric(ref[key], errors="coerce").to_numpy(dtype="float64")
        v = s[~np.isnan(s)]
        mean = float(v.mean())
        std = float(np.sqrt(((v - mean) ** 2).sum() / v.size))
        total_ref += int(((s < mean - 3 * std) | (s > mean + 3 * std)).sum())
    check("大表 3σ 点数与 pandas/numpy 独立重算一致", bd["summary"]["totalAnomalies"] == total_ref,
          f"{bd['summary']['totalAnomalies']} vs {total_ref}")
    check("大表缺失格数与 pandas 一致", bq["totalMissingCells"] == int(ref.isna().sum().sum()),
          f"{bq['totalMissingCells']} vs {int(ref.isna().sum().sum())}")
    axis = set(bs["idx"])
    for m in bs["series"]:
        na_rows = set(np.flatnonzero(ref[m["col"]].isna().to_numpy()).tolist())
        check(f"大表 {m['col']} 虚线数 = 抽稀轴上的真实缺失行数",
              len(m["missingMarks"]) == len(na_rows & axis) == m["missingCount"] - len(na_rows - axis)
              and m["marksTruncated"] is (len(na_rows & axis) < m["missingCount"]),
              f"画 {len(m['missingMarks'])} 条 / 整列缺失 {m['missingCount']} 格 · 截断={m['marksTruncated']}")
    close_workspace(bw)

    print("\n== 结果 ==")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
