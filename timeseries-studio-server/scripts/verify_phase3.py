"""第③期验收：特征构建四 Tab 全部走后端，并用浏览器旧实现的导出产物逐格对拍。

对拍基准 from where：before_i3.csv / before_na3.csv 是在真实浏览器里点完四个 Tab
（含重命名、撤销特征列）后，由「导出处理结果 → CSV 宽表」产出的字节流，通过
.verify/sink.mjs 回传落盘的。它就是"要被替换掉的那份实现"的产物快照，
所以本脚本比的是两套语言（JS ↔ Python）的真实产物，不是自己和自己比。

前置：uvicorn app.main:app --port 8000（本脚本不自启后端）。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_phase3.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]
REF_DIR = Path(__file__).resolve().parents[2] / ".verify"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + str(detail)) if detail != '' else ''}")
    if not cond:
        FAILURES.append(name)
    return cond


def show(label, value):
    print(f"  {label}: {value}")


def call(method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=300) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def api(method: str, path: str, body: dict | None = None) -> dict:
    status, payload = call(method, path, body)
    text = payload.decode("utf-8", "replace")
    if status >= 400:
        raise SystemExit(f"请求失败 {method} {path} → {status} {text[:300]}")
    return json.loads(text)


def err_text(payload: bytes) -> str:
    try:
        return json.loads(payload.decode("utf-8", "replace")).get("detail", "")[:120]
    except json.JSONDecodeError:
        return payload.decode("utf-8", "replace")[:120]


def open_dataset(filename: str) -> str:
    status, payload = call("POST", "/api/ws/dataset", {"filename": filename})
    if status >= 400:
        raise SystemExit(f"打开 {filename} 失败 → {status} {err_text(payload)}")
    meta = json.loads(payload.decode("utf-8"))["meta"]
    return meta["wsId"]


def columns_of(ws: str, keys: list[str]) -> dict:
    """分批复取整列（一次 20 列，避免超长 query）。"""
    out: dict = {}
    for i in range(0, len(keys), 20):
        chunk = keys[i:i + 20]
        got = api("GET", f"/api/ws/{ws}/columns?keys={urllib.parse.quote(','.join(chunk))}")["columns"]
        out.update(got)
    return out


def close_workspace(ws: str):
    call("DELETE", f"/api/ws/{ws}")


def upload_csv(text: str, filename: str) -> str:
    import io
    import uuid as _uuid
    boundary = "----tss" + _uuid.uuid4().hex
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"))
    buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
    buf.write(text.encode("utf-8"))
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(BASE + "/api/ws", data=buf.getvalue(), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            return json.loads(res.read().decode("utf-8"))["meta"]["wsId"]
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"上传 {filename} 失败 → {exc.code} {err_text(exc.read())}")


def first_seen_order(values: list[str]) -> list[str]:
    """非空取值按首次出现排序，和 JS 的 Set 保序一致。"""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        s = str(v or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---------------------------------------------------------------- 参考基准读取

def read_ref(name: str):
    import csv
    path = REF_DIR / f"before_{name}.csv"
    if not path.exists():
        raise SystemExit(f"缺少浏览器基准 {path}，请先跑 .verify/phase3_parity.py 的记录流程采集")
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    header, body = rows[0], rows[1:]
    return header, body


def is_blank(v):
    return v is None or (isinstance(v, float) and v != v) or str(v).strip() == ""


def as_float(v):
    """能当数字比就当数字比，其余加前缀避免和真数字撞车。"""
    if isinstance(v, bool):
        return "str:" + str(v)
    if isinstance(v, (int, float)):
        return None if v != v else float(v)
    s = str(v or "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return "str:" + s


def cell_equal(a, b):
    """空值（None/NaN/空串）视为同一类；数字按浮点比；其余去空格后按字符串比。"""
    if is_blank(a) and is_blank(b):
        return True
    an, bn = as_float(a), as_float(b)
    if isinstance(an, float) and isinstance(bn, float):
        return abs(an - bn) <= 1e-9
    return str(a).strip() == str(b).strip()


ALL_DIMS = ["hour", "day", "month", "weekday", "is_weekend", "holiday"]

CASES = [
    {
        "name": "i3", "dataset": "i3.csv",
        "ops": [
            ("feature-time", {"dims": ALL_DIMS, "cyclical": True}),
            ("feature-lag", {"cols": ["temperature", "pressure"], "lags": [1, 2, 4, 96],
                             "windows": [4, 16, 96], "stats": ["mean", "std"]}),
            ("feature-diff", {"cols": ["temperature", "pressure"], "d1": True,
                              "seasonal": True, "period": 96, "fftDominant": True}),
            ("feature-cat", {"cols": ["weather"], "method": "onehot"}),
        ],
    },
    {
        # 密布缺失值/负值/并列取值，覆盖 max/min/median、Expanding、EWM 跨空洞、
        # 二阶差分、谱熵、频带能量比、序数/目标编码，外加改名与撤销列两种改表操作
        "name": "na3", "dataset": "na3.csv",
        "ops": [
            ("feature-time", {"dims": ALL_DIMS, "cyclical": True}),
            ("feature-lag", {"cols": ["temperature", "pressure"], "lags": [1, 2, 4, 96],
                             "windows": [3, 5, 16],
                             "stats": ["mean", "std", "max", "min", "median"],
                             "expanding": True, "ewm": True, "ewmSpan": 7}),
            ("feature-diff", {"cols": ["temperature", "pressure"], "d1": True, "d2": True,
                              "seasonal": True, "period": 24, "fftDominant": True,
                              "fftEntropy": True, "fftPowerRatio": True}),
            ("feature-cat", {"cols": ["weather", "grade"], "method": "onehot"}),
            ("feature-cat", {"cols": ["weather", "grade"], "method": "ordinal"}),
            ("feature-cat", {"cols": ["weather", "grade"], "method": "target"}),
            # 浏览器里这两下是在第五步矩阵上点的：重命名带前端算出的冲突键，撤销列删掉频带能量比
            ("rename-column", {"key": "weather_晴", "label": "SunnyDay", "newKey": "sunnyday"}),
            ("delete-column", {"key": "fft_power_ratio_temperature"}),
        ],
    },
]


# ---------------------------------------------------------------- 主流程

def section_parity():
    for case in CASES:
        name = case["name"]
        print(f"\n== 用例 {name}：四个 Tab 走 HTTP，逐格对齐浏览器导出 ==")
        header, body = read_ref(name)
        ws = open_dataset(case["dataset"])
        t0 = time.perf_counter()
        results = []
        for kind, params in case["ops"]:
            r = api("POST", f"/api/ws/{ws}/op/{kind}", params)
            results.append(r)
            show(f"op {kind}", f"{r['summary'][:66]} · {r.get('cols', 0)} 列（新增 {r.get('created', 0)}）· "
                               f"v{r['meta']['version']}")
        ms = (time.perf_counter() - t0) * 1000
        keys = [str(c["key"]) for c in results[-1]["meta"]["columns"]]
        check(f"{name}：列名与列序和浏览器导出完全一致", keys == header,
              f"服务端 {len(keys)} / 浏览器 {len(header)}")
        got = columns_of(ws, keys)
        total_bad, first_bad = 0, None
        for ci, col in enumerate(header):
            col_vals = got.get(col, [])
            for ri in range(len(body)):
                a = col_vals[ri] if ri < len(col_vals) else "<缺>"
                b = body[ri][ci] if ci < len(body[ri]) else "<缺>"
                if not cell_equal(a, b):
                    total_bad += 1
                    if first_bad is None:
                        first_bad = f"{col}[{ri}] 服务端 {a!r} 浏览器 {b!r}"
        check(f"{name}：{len(header)} 列 × {len(body)} 行逐格一致", total_bad == 0,
              f"{total_bad} 格不一致 · {first_bad}")
        feats = [c for c in results[-1]["meta"]["columns"] if c.get("feature")]
        check(f"{name}：新增列都带 feature 族标记", len(feats) == len(header) - 5,
              f"{len(feats)} 列 / 应为 {len(header) - 5}")
        types = {c["key"]: c["type"] for c in results[-1]["meta"]["columns"]}
        check(f"{name}：特征列一律按 float 记账", all(types[c["key"]] == "float" for c in feats))
        check(f"{name}：四 Tab 全部走后端，耗时 {ms:.0f} ms", ms < 20000)
        # 私有重放参数不能顺着 HTTP 泄漏给前端
        ops_public = json.dumps(results[-1]["meta"]["ops"], ensure_ascii=False)
        check(f"{name}：审计日志不含私有 replay 字段", "replay" not in ops_public)
        # 撤销 → 重做：同一份日志再放一遍必须回到同一张表
        snapshot = {k: list(v) for k, v in got.items()}
        api("POST", f"/api/ws/{ws}/restore", {"version": 0})
        base_cols = [c["key"] for c in api("GET", f"/api/ws/{ws}")["columns"]]
        check(f"{name}：回到 v0 后只剩原始 5 列", len(base_cols) == 5, base_cols)
        api("POST", f"/api/ws/{ws}/restore", {"version": len(case["ops"])})
        again = columns_of(ws, keys)
        diff = [k for k in keys if list(again.get(k, [])) != list(snapshot.get(k, []))]
        check(f"{name}：重放到 v{len(case['ops'])} 与首次逐格相同", not diff, diff[:4])
        close_workspace(ws)


def section_value_counts():
    print("\n== GET /value-counts：类别分布与「预计新增列数」由服务端数出来 ==")
    import pandas as pd
    ws = open_dataset("na3.csv")
    vc = api("GET", f"/api/ws/{ws}/value-counts?keys={urllib.parse.quote('weather,grade')}&method=onehot")
    ref = pd.read_csv(ROOT / "dataset" / "na3.csv", keep_default_na=False, dtype=str)
    cap = api("GET", "/api/health")["limits"]["uniqueValuesReported"]
    check("整表行数随响应回带", vc["rowCount"] == len(ref), f"{vc['rowCount']} / {len(ref)}")
    for col in vc["columns"]:
        counts = ref[col["key"]].str.strip().replace("", pd.NA).dropna().value_counts()
        check(f"{col['key']}：真实取值个数（uniqueTotal）", col["uniqueTotal"] == len(counts),
              f"{col['uniqueTotal']} vs {len(counts)}")
        check(f"{col['key']}：非缺失行数", col["nonMissingRows"] == int(counts.sum()),
              f"{col['nonMissingRows']} / 整表 {col['total']}")
        check(f"{col['key']}：total 是整表行数而非非缺失行数", col["total"] == len(ref), col["total"])
        shown = {v: int(n) for v, n in counts.items() if v in set(col["uniqueVals"])}
        check(f"{col['key']}：列出的每个取值行数逐一对齐 pandas", col["counts"] == shown,
              f"服务端 {col['counts']}")
        check(f"{col['key']}：未列出的取值合并进行数无误", col["restCount"] == int(counts.sum()) - sum(shown.values()),
              col["restCount"])
        check(f"{col['key']}：取值按首次出现顺序（独热列名依赖它）",
              col["uniqueVals"] == first_seen_order(ref[col["key"]].tolist()), col["uniqueVals"])
        check(f"{col['key']}：低基数列不标记截断", col["truncated"] is False)
    check("独热预计新增列数 = 各列真实取值数之和",
          vc["totalNewCols"] == sum(c["uniqueTotal"] for c in vc["columns"]), vc["totalNewCols"])
    check("预计新增列数与浏览器实际生成的独热列数一致（8 列）", vc["totalNewCols"] == 8, vc["totalNewCols"])
    ord_vc = api("GET", f"/api/ws/{ws}/value-counts?keys=weather&method=ordinal")
    check("序数/目标编码预计新增 = 列数", ord_vc["totalNewCols"] == 1, ord_vc["totalNewCols"])
    st, pl = call("GET", f"/api/ws/{ws}/value-counts?keys={urllib.parse.quote('不存在的列')}")
    check("列不存在被拒绝", st == 400, err_text(pl))
    st, pl = call("GET", f"/api/ws/{ws}/value-counts?keys=weather&method=kmeans")
    check("未知编码方式被拒绝", st == 400, err_text(pl))
    st, pl = call("GET", f"/api/ws/{ws}/value-counts?keys=")
    check("keys 为空被拒绝（422 参数校验）", st in (400, 422), err_text(pl))
    close_workspace(ws)

    print("\n== 高基数列：取值分布有界，但「共计多少取值」仍是真数 ==")
    lines = ["timestamp,uid"]
    for i in range(1000):
        lines.append(f"2024-06-01 00:{i % 60:02d}:{i // 60 % 60:02d},u{i}")
    hw = upload_csv("\n".join(lines) + "\n", "highcard2.csv")
    hv = api("GET", f"/api/ws/{hw}/value-counts?keys=uid&method=onehot")
    col = hv["columns"][0]
    check("取值列表被上限截住", len(col["uniqueVals"]) == cap, f"{len(col['uniqueVals'])} / 上限 {cap}")
    check("真实取值个数仍是 1000", col["uniqueTotal"] == 1000, col["uniqueTotal"])
    check("截断标记为真", col["truncated"] is True)
    check("未列出的 800 行合并进 restCount", col["restCount"] == 1000 - cap, col["restCount"])
    check("面板上的预计新增列数仍是真数（独热会拒绝它，两处数字得对得上）",
          hv["totalNewCols"] == 1000, hv["totalNewCols"])
    st, pl = call("POST", f"/api/ws/{hw}/op/feature-cat", {"cols": ["uid"], "method": "onehot"})
    check("同一列走独热被上限拒绝且报错里的列数和面板一致",
          st == 400 and "1000" in err_text(pl), err_text(pl))
    check("响应体受控（没有把 1000 个取值全吐出来）", len(json.dumps(hv)) < 20000,
          f"{len(json.dumps(hv))} 字节")
    close_workspace(hw)


def section_feature_registry():
    print("\n== 特征列进了列注册表：第二步/第四步的四条扫描仍只认原始列 ==")
    ws = open_dataset("na3.csv")
    before_q = api("GET", f"/api/ws/{ws}/quality")
    before_o = api("GET", f"/api/ws/{ws}/overview")
    api("POST", f"/api/ws/{ws}/op/feature-time", {"dims": ["hour"], "cyclical": False})
    meta = api("GET", f"/api/ws/{ws}")
    feats = [c for c in meta["columns"] if c.get("feature")]
    check("特征列出现在 meta.columns 且带 feature=time", len(feats) == 1 and feats[0]["feature"] == "time",
          [(c["key"], c.get("feature")) for c in meta["columns"]])
    check("特征列不顶替原始列位置", meta["columns"][-1]["key"] == feats[0]["key"])
    det = api("POST", f"/api/ws/{ws}/anomaly-detect", {"algo": "3sigma"})["detection"]
    check("异常检测的列口径不含特征列",
          sorted(r["key"] for r in det["results"]) == sorted(["temperature", "pressure"]),
          [r["key"] for r in det["results"]])
    q2 = api("GET", f"/api/ws/{ws}/quality")
    check("缺失诊断不把特征列算进去（数字与建特征前完全一致）",
          q2["columns"] == before_q["columns"] and q2["totalMissingCells"] == before_q["totalMissingCells"],
          f"{len(q2['columns'])} 列 / 建特征前 {len(before_q['columns'])} 列")
    o1 = api("GET", f"/api/ws/{ws}/overview")
    after = api("POST", f"/api/ws/{ws}/op/feature-lag",
                {"cols": ["temperature"], "lags": [1]})
    check("加特征列不作废检测缓存（只增列、不动既有数值）",
          after["meta"]["anomaly"] is not None and after["meta"]["anomaly"]["stale"] is False,
          after["meta"]["anomaly"])
    # 滞后列首行本就是 null：缺失率口径一旦把特征列算进去，这里的分子分母都会跟着变
    o2 = api("GET", f"/api/ws/{ws}/overview")
    check("第二步概览的缺失率只数原始数值列（建了日历+滞后特征也没被拉高）",
          o1["missingCells"] == before_o["missingCells"] and o2["missingCells"] == before_o["missingCells"]
          and o2["missingDenominator"] == before_o["missingDenominator"],
          f"建特征后 {o2['missingCells']}/{o2['missingDenominator']} · 建特征前 {before_o['missingCells']}/{before_o['missingDenominator']}")

    print("\n== 拿特征列再滞后：浏览器做不到的组合，现在按显式能力放开 ==")
    lagged = api("POST", f"/api/ws/{ws}/op/feature-lag", {"cols": [feats[0]["key"]], "lags": [1]})
    check("特征列可以作为滞后目标（旧实现靠列注册表缺席蒙住，现在放开但要记账）",
          lagged["created"] == 1 and lagged["features"][0]["feature"] == "lag_roll",
          lagged["features"])
    dup = api("GET", f"/api/ws/{ws}/columns?keys={lagged['keys'][0]}")["columns"][lagged["keys"][0]]
    check("新滞后列的第 0 行仍是 null（结构性缺失，不会被填补通道误伤）", dup[0] is None, dup[:3])
    src_missing = sum(c["missing"] for c in q2["columns"] if c["type"] == "float")
    filled = api("POST", f"/api/ws/{ws}/op/impute", {"all": True, "defaultAlgo": "linear"})
    check(f"全部填补按缺失诊断的口径动数据（原始数值列 {src_missing} 个缺失，特征列不在其列）",
          filled["filled"] == src_missing, f"{filled['filled']} vs 诊断 {src_missing} · {filled['summary']}")
    still = api("GET", f"/api/ws/{ws}/columns?keys={lagged['keys'][0]}")["columns"][lagged["keys"][0]]
    check("填补后该列首行依旧为 null（特征列的结构性缺失没被抹掉）", still[0] is None, still[:3])
    q3 = api("GET", f"/api/ws/{ws}/quality")
    check("填补后缺失诊断只剩类别/时间列（数值列归零且列数没被特征列撑大）",
          all(c["missing"] == 0 for c in q3["columns"] if c["type"] == "float")
          and len(q3["columns"]) == len(q2["columns"]),
          f"{len(q3['columns'])} 列 · " + str([(c["key"], c["missing"]) for c in q3["columns"]]))
    det2 = api("POST", f"/api/ws/{ws}/anomaly-detect", {"algo": "3sigma"})["detection"]
    check("检测口径依旧不含任何特征列",
          sorted(r["key"] for r in det2["results"]) == sorted(["temperature", "pressure"]),
          [r["key"] for r in det2["results"]])

    print("\n== 频域特征的列名：界面矩阵表头读的就是这份字面量 ==")
    dom = api("POST", f"/api/ws/{ws}/op/feature-diff",
              {"cols": ["temperature"], "d1": True, "seasonal": False,
               "fftDominant": True, "fftEntropy": True, "fftPowerRatio": True})
    tops = [(f["key"], f["label"]) for f in dom["features"] if f["key"].startswith("fft_top")]
    check("主导频率标签 = FFT_Top{n}_E{全谱能量}（与浏览器旧实现的表头字面量同格式，不含列名）",
          len(tops) == 3 and all(re.fullmatch(r"FFT_Top\d_E\d+\.\d", label) for _, label in tops), tops)
    spec = {f["key"]: f["label"] for f in dom["features"]}
    check("谱熵 / 频带能量比的标签沿用旧实现的 spectral_entropy_ / power_ratio_ 前缀",
          spec.get("fft_entropy_temperature") == "spectral_entropy_temperature"
          and spec.get("fft_power_ratio_temperature") == "power_ratio_temperature",
          [spec.get("fft_entropy_temperature"), spec.get("fft_power_ratio_temperature")])
    close_workspace(ws)


def section_errors():
    print("\n== 错误路径与上限 ==")
    ws = open_dataset("na3.csv")
    cases = [
        ("目标列不存在", "feature-lag", {"cols": ["不存在的列"], "lags": [1]}, 400),
        ("类别列不能当滞后目标", "feature-cat", {"cols": ["timestamp"], "method": "ordinal"}, 400),
        ("只选窗口不给统计量", "feature-lag",
         {"cols": ["temperature"], "windows": [4], "stats": []}, 400),
        ("滞后/窗口/高级全空", "feature-lag", {"cols": ["temperature"]}, 400),
        ("窗口超过上限", "feature-lag", {"cols": ["temperature"], "windows": [99999]}, 400),
        ("差分什么都不选", "feature-diff", {"cols": ["temperature"], "d1": False}, 400),
        ("时间特征维度为空", "feature-time", {"dims": ["不存在"]}, 400),
        ("一次生成列数超上限", "feature-lag",
         {"cols": ["temperature", "pressure"], "windows": list(range(1, 101)),
          "stats": ["mean", "std", "max", "min", "median"]}, 400),
    ]
    for label, kind, body, want in cases:
        st, pl = call("POST", f"/api/ws/{ws}/op/{kind}", body)
        check(f"{label} → HTTP {want}", st == want, f"{st} · {err_text(pl)}")
    st, pl = call("POST", f"/api/ws/{ws}/op/feature-lag",
                  {"cols": ["temperature"], "lags": [1], "ewmSpan": 1})
    check("EWM span<2 被 schema 拒绝", st == 422, f"{st}")
    st, pl = call("POST", f"/api/ws/{ws}/op/feature-diff",
                  {"cols": ["temperature"], "d1": True, "period": 0})
    check("季节差分 period=0 被 schema 拒绝", st == 422, f"{st}")
    st, pl = call("POST", f"/api/ws/{ws}/op/feature-cat", {"cols": ["weather"], "method": "kmeans"})
    check("未知编码方式被 schema 拒绝", st == 422, f"{st}")
    check("失败命令一条都没进日志", api("GET", f"/api/ws/{ws}")["version"] == 0)
    close_workspace(ws)

    print("\n== 独热层数上限（高基数列不允许炸穿矩阵）==")
    big_cat = "timestamp,x\n" + "\n".join(
        f"2024-06-01 {i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d},v{i}" for i in range(300))
    hw = upload_csv(big_cat + "\n", "highcard.csv")
    st, pl = call("POST", f"/api/ws/{hw}/op/feature-cat", {"cols": ["x"], "method": "onehot"})
    check("300 层独热被拒绝并说明上限", st == 400, err_text(pl))
    st, pl = call("POST", f"/api/ws/{hw}/op/feature-cat", {"cols": ["x"], "method": "ordinal"})
    check("同一列改走序数编码可以过（1 列）", st == 200 and json.loads(pl)["cols"] == 1,
          err_text(pl) if st >= 400 else json.loads(pl)["summary"])
    close_workspace(hw)


def section_rename_drop():
    print("\n== 重命名 / 撤销特征列（第五步矩阵上的两个改表动作）==")
    ws = open_dataset("na3.csv")
    api("POST", f"/api/ws/{ws}/op/feature-cat", {"cols": ["weather"], "method": "onehot"})
    onehot = [c["key"] for c in api("GET", f"/api/ws/{ws}")["columns"] if c.get("feature") == "cat"]
    api("POST", f"/api/ws/{ws}/op/rename-column",
        {"key": onehot[0], "label": "SunnyDay", "newKey": "sunnyday"})
    meta = api("GET", f"/api/ws/{ws}")
    keys = [c["key"] for c in meta["columns"]]
    labels = {c["key"]: c["label"] for c in meta["columns"]}
    check("改名后旧键消失、新键就位", onehot[0] not in keys and "sunnyday" in keys)
    check("新键仍带 feature=cat 标记",
          next(c for c in meta["columns"] if c["key"] == "sunnyday")["feature"] == "cat")
    check("显示名按前端给的字面量记账", labels["sunnyday"] == "SunnyDay", labels["sunnyday"])
    col_new = columns_of(ws, ["sunnyday"])["sunnyday"]
    check("改名只动列键、不动数据", col_new.count(1) == 40, f"{col_new.count(1)} 个 1")
    api("POST", f"/api/ws/{ws}/op/delete-column", {"key": onehot[1]})
    meta = api("GET", f"/api/ws/{ws}")
    check("撤销特征列后矩阵少一列", onehot[1] not in [c["key"] for c in meta["columns"]])
    check("矩阵里剩下的独热列仍完整", "sunnyday" in [c["key"] for c in meta["columns"]])
    st, pl = call("POST", f"/api/ws/{ws}/op/rename-column",
                  {"key": "weather", "label": "天气", "newKey": "temperature"})
    check("改名撞到已有列被拒绝", st == 400, err_text(pl))
    check("被拒绝的改名没动列注册表",
          [c["key"] for c in api("GET", f"/api/ws/{ws}")["columns"]].count("temperature") == 1)
    close_workspace(ws)


def section_big_table():
    print("\n== 大表 big40.csv（11000 行 × 40 列 = 440000 格）：第五步不再吃整表 ==")
    path = ROOT / "dataset" / "big40.csv"
    if not path.exists():
        raise SystemExit("缺少 dataset/big40.csv，请先运行 scripts/make_big_csv.py")
    ws = open_dataset("big40.csv")
    meta = api("GET", f"/api/ws/{ws}")
    num_cols = [c["key"] for c in meta["columns"] if c["type"] == "float" and not c.get("feature")][:3]
    t0 = time.perf_counter()
    r = api("POST", f"/api/ws/{ws}/op/feature-time", {"dims": ALL_DIMS, "cyclical": True})
    show("feature-time", f"{(time.perf_counter() - t0) * 1000:.0f} ms · {r['summary'][:50]}")
    t0 = time.perf_counter()
    st, pl = call("POST", f"/api/ws/{ws}/op/feature-lag",
                  {"cols": num_cols, "lags": [1, 2, 4, 96], "windows": [4, 16, 96],
                   "stats": ["mean", "std", "max", "min", "median"],
                   "expanding": True, "ewm": True, "ewmSpan": 12})
    ms = (time.perf_counter() - t0) * 1000
    if st >= 400:
        check("大表滞后/窗口能算完", False, err_text(pl))
    else:
        body = json.loads(pl.decode())
        show("feature-lag", f"{ms:.0f} ms · {body['cols']} 列（新增 {body['created']}）· "
                            f"响应 {len(pl) / 1024:.1f} KiB · 帧 {body['meta']['rowCount']} 行 × "
                            f"{body['meta']['colCount']} 列")
        check("一次滞后+5 统计量+Expanding/EWM 造出全部窗口列", body["cols"] == len(num_cols) * (4 + 15 + 2),
              body["cols"])
        check("响应只有元数据，没有明细行", "rows" not in json.dumps(body["meta"]) and
              len(pl) < 200 * 1024, f"{len(pl) / 1024:.1f} KiB")
        check("页窗口默认 50 行、总行数按整表记账",
              len(body["page"]["rows"]) == 50 and body["page"]["total"] == 11000,
              f"{len(body['page']['rows'])} 行 / 共 {body['page']['total']} 行")
        check("大表 40 万格 60 秒内算完", ms < 60000, f"{ms:.0f} ms")
    close_workspace(ws)


def main() -> int:
    health = api("GET", "/api/health")
    print("== /api/health ==")
    need = ["op:feature-time", "op:feature-lag", "op:feature-diff", "op:feature-cat",
            "workspace:value-counts"]
    check("能力声明含第③期全部入口", all(c in health["capabilities"] for c in need),
          [c for c in need if c not in health["capabilities"]])
    show("特征相关上限", {k: v for k, v in health["limits"].items() if k.startswith(("feature", "onehot", "unique"))})

    section_parity()
    section_value_counts()
    section_feature_registry()
    section_errors()
    section_rename_drop()
    section_big_table()

    print("\n== 结果 ==")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
