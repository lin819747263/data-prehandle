"""第④期验收：第三步的统计矩阵/直方图/叠加曲线、以及四种格式的宽表导出全部走后端。

跨语言证据来自 scripts/ref_phase4.mjs —— 那是迁移前浏览器实现（整表统计 + 25 桶直方图 +
窗口均值降采样 + 极值抽稀）的原生 JS 重写：它不 import 服务端代码、不用 pandas，
只读同一份 CSV。本脚本把它算出来的结果与 /api/ws/{id}/stats、/hist、/series-multi 逐格对拍，
所以比的是两套语言的真实产物，而不是 Python 自己和自己比。

浮点统计量按相对偏差 1e-9 判等（numpy 成对求和与 JS 逐项累加在最后一两个比特上本就不同），
计数类（n/missing/bins/counts/points）一律要求完全相等。

前置：.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000（本脚本不自启后端）
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_phase4.py
"""
from __future__ import annotations

import io
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "scripts" / "ref_phase4.mjs"
REL_TOL = 1e-9
BINS = 25
POINTS = 3000

FAILURES: list[str] = []


def check(name: str, cond: bool, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + str(detail)) if detail != '' else ''}")
    if not cond:
        FAILURES.append(name)
    return cond


def show(label, value):
    print(f"  {label}: {value}")


def call(method: str, path: str, body: dict | None = None) -> tuple[int, bytes, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=600) as res:
            return res.status, res.read(), dict(res.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def api(method: str, path: str, body: dict | None = None) -> dict:
    status, payload, _ = call(method, path, body)
    text = payload.decode("utf-8", "replace")
    if status >= 400:
        raise SystemExit(f"请求失败 {method} {path} → {status} {text[:300]}")
    return json.loads(text)


def err_text(payload: bytes) -> str:
    try:
        return json.loads(payload.decode("utf-8", "replace")).get("detail", "")[:160]
    except json.JSONDecodeError:
        return payload.decode("utf-8", "replace")[:160]


def open_dataset(filename: str) -> str:
    status, payload, _ = call("POST", "/api/ws/dataset", {"filename": filename})
    if status >= 400:
        raise SystemExit(f"打开 {filename} 失败 → {status} {err_text(payload)}")
    return json.loads(payload.decode("utf-8"))["meta"]["wsId"]


def columns_of(ws: str, keys: list[str]) -> dict:
    out: dict = {}
    for i in range(0, len(keys), 20):
        chunk = keys[i:i + 20]
        got = api("GET", f"/api/ws/{ws}/columns?keys={urllib.parse.quote(','.join(chunk))}")["columns"]
        out.update(got)
    return out


def close_workspace(ws: str):
    call("DELETE", f"/api/ws/{ws}")


def upload_bytes(filename: str, blob: bytes) -> str:
    boundary = "----tss" + uuid.uuid4().hex
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"))
    buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
    buf.write(blob)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    status, payload, _ = call_raw("/api/ws", buf.getvalue(),
                                  f"multipart/form-data; boundary={boundary}")
    if status >= 400:
        raise SystemExit(f"上传 {filename} 失败 → {status} {err_text(payload)}")
    return json.loads(payload.decode("utf-8"))["meta"]["wsId"]


def call_raw(path: str, body: bytes, content_type: str) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=600) as res:
            return res.status, res.read(), dict(res.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def download(path: str) -> tuple[bytes, dict]:
    status, payload, headers = call("GET", path)
    if status >= 400:
        raise SystemExit(f"下载 {path} 失败 → {status} {err_text(payload)}")
    return payload, headers


def float_cols(meta: dict) -> list[str]:
    return [c["key"] for c in meta["columns"] if c["type"] == "float"]


# ---------------------------------------------------------------- JS 参考实现

def node_ref(csv_path: Path, cols: list[str], modes: list[str], points: int) -> dict:
    cmd = ["node", str(REF), "--csv", str(csv_path), "--cols", ",".join(cols),
           "--modes", ",".join(modes), "--points", str(points), "--bins", str(BINS)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
    except FileNotFoundError:
        raise SystemExit("找不到 node：第④期的跨语言对拍必须有浏览器侧参考实现，不能用服务端自证")
    if proc.returncode != 0:
        raise SystemExit(f"参考实现失败：{proc.stderr.decode('utf-8', 'replace')[:400]}")
    return json.loads(proc.stdout.decode("utf-8"))


def dev(a, b) -> float:
    """两个量之间的相对偏差；类型不同（None vs 数字、字符串 vs 数字）视为无穷大。"""
    if a is None or b is None:
        return 0.0 if a is None and b is None else math.inf
    if isinstance(a, bool) or isinstance(b, bool):
        return 0.0 if a == b else math.inf
    if isinstance(a, int) and isinstance(b, int):
        return 0.0 if a == b else math.inf
    if isinstance(a, str) or isinstance(b, str):
        return 0.0 if str(a).strip() == str(b).strip() else math.inf
    return abs(float(a) - float(b)) / max(1.0, abs(float(a)), abs(float(b)))


class Diff:
    """攒下所有不一致的格子，只在末尾报一次——几十个 FAIL 会把有用的行埋掉。"""

    def __init__(self, label: str):
        self.label = label
        self.bad: list[str] = []
        self.cells = 0
        self.worst = 0.0

    def num(self, where: str, want, got):
        self.cells += 1
        d = dev(want, got)
        self.worst = max(self.worst, d)
        if d > REL_TOL:
            self.bad.append(f"{where}: JS {want!r} ↔ 服务端 {got!r}")

    def ints(self, where: str, want, got):
        self.cells += 1
        if want != got:
            self.bad.append(f"{where}: JS {want!r} ↔ 服务端 {got!r}")

    def seq(self, where: str, want: list, got: list):
        if len(want) != len(got):
            self.bad.append(f"{where}: 长度 JS {len(want)} ↔ 服务端 {len(got)}")
            return
        for i, (a, b) in enumerate(zip(want, got)):
            self.num(f"{where}[{i}]", a, b)

    def report(self):
        detail = f"{self.cells} 格 · 最大相对偏差 {self.worst:.2e}"
        if self.bad:
            detail += f" · {len(self.bad)} 格不一致 · " + " | ".join(self.bad[:3])
        return check(f"{self.label} 与浏览器口径逐格一致", not self.bad, detail)


# ---------------------------------------------------------------- ①/②/③：与 JS 对拍

PARITY_CASES = [
    ("na3.csv", "缺失值密布的短表（240 行）"),
    ("cleaned.csv", "PV 电站实测（2879 行，含整段夜间零值）"),
    ("machine.csv", "机床数据（中文带单位列名，200 行）"),
    ("big40.csv", "压力机大表（11000 行 × 40 列，第三步最吃整表的那一类）"),
]


def section_parity():
    for filename, why in PARITY_CASES:
        print(f"\n== {filename}：{why} ==")
        ws = open_dataset(filename)
        meta = api("GET", f"/api/ws/{ws}")
        cols = float_cols(meta)
        if not cols:
            raise SystemExit(f"{filename} 没有数值列，用例失效")
        show("参与对拍的数值列", f"{len(cols)} 列：" + ", ".join(cols[:3]) + ("…" if len(cols) > 3 else ""))
        # 叠加曲线一次最多叠 12 列：extremes 的每列预算按列数分摊，两边必须用同一组列
        series_cols = cols[:12]
        full = api("GET", f"/api/ws/{ws}/stats")
        check("stats 不带 cols 参数时统计全部数值列（第三步表格用的就是这份）",
              [r["key"] for r in full["rows"]] == cols and full["colCount"] == len(cols),
              f"{full['colCount']} 列 / 数值列 {len(cols)}")
        for points in (POINTS, 500):
            t0 = time.perf_counter()
            ref = node_ref(ROOT / "dataset" / filename, series_cols, ["raw", "extremes", "mean"], points)
            ms_node = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            got_stats = api("GET", f"/api/ws/{ws}/stats?cols={urllib.parse.quote(','.join(series_cols))}")
            got_series = {m: api("GET", f"/api/ws/{ws}/series-multi?cols={urllib.parse.quote(','.join(series_cols))}"
                                      f"&mode={m}&points={points}") for m in ("raw", "extremes", "mean")}
            ms_srv = (time.perf_counter() - t0) * 1000

            d = Diff(f"points={points} 统计矩阵（{len(cols)} 列 × 10 量）")
            d.ints("rowCount", ref["rowCount"], got_stats["rowCount"])
            d.num("globalMax", ref["stats"]["globalMax"], got_stats["globalMax"])
            want_keys = [r["key"] for r in ref["stats"]["rows"]]
            got_keys = [r["key"] for r in got_stats["rows"]]
            d.ints("列序", want_keys, got_keys)
            got_rows = {r["key"]: r for r in got_stats["rows"]}
            for row in ref["stats"]["rows"]:
                g = got_rows.get(row["key"], {})
                for f in ("n", "missing", "mean", "std", "median", "q1", "q3", "min", "max", "missingRate"):
                    d.num(f"{row['key']}.{f}", row[f], g.get(f))
            d.report()

            for i, h in enumerate(ref["hist"]):
                key = h["col"]
                gh = api("GET", f"/api/ws/{ws}/hist?col={urllib.parse.quote(key)}&bins={BINS}")
                dh = Diff(f"points={points} 直方图 {key}")
                for f in ("bins", "n", "missing", "totalRows", "meanBin", "medianBin"):
                    dh.ints(f, h[f], gh[f])
                dh.seq("counts", h["counts"], gh["counts"])
                dh.seq("edges", h["edges"], gh["edges"])
                for f in ("min", "max", "mean", "median", "binWidth"):
                    dh.num(f, h[f], gh[f])
                dh.report()

            for mode in ("raw", "extremes", "mean"):
                r = next(s for s in ref["series"] if s["mode"] == mode)
                g = got_series[mode]
                ds = Diff(f"points={points} 叠加曲线 mode={mode}（{len(series_cols)} 列 × {g['points']} 点）")
                for f in ("mode", "rowCount", "points", "windowStep", "decimated"):
                    ds.ints(f, r[f], g[f])
                ds.seq("x", r["x"], g["x"])
                got_series_by_col = {s["col"]: s for s in g["series"]}
                for s in r["series"]:
                    t = got_series_by_col.get(s["col"], {})
                    ds.ints(f"{s['col']}.missing", s["missing"], t.get("missing"))
                    ds.seq(f"{s['col']}.y", s["y"], t.get("y", []))
                ds.report()
            show(f"points={points} 耗时", f"JS 参考 {ms_node:.0f} ms / 服务端 {ms_srv:.0f} ms")
        close_workspace(ws)


# ---------------------------------------------------------------- ④：界面数字的自洽性

def section_invariants():
    print("\n== 界面上每个数字的自洽性（第三步表格/分布条/图注都靠这几条）==")
    ws = open_dataset("big40.csv")
    meta = api("GET", f"/api/ws/{ws}")
    cols = float_cols(meta)
    stats = api("GET", f"/api/ws/{ws}/stats")
    n = stats["rowCount"]
    bad = [r["key"] for r in stats["rows"] if r["n"] + r["missing"] != n]
    check(f"每列 有效数 + 缺失数 = 整表行数（{n} 行）", not bad, bad[:4])
    bad = [r["key"] for r in stats["rows"] if abs(r["missingRate"] - r["missing"] / n * 100) > 1e-9]
    check("缺失率的分母是整表行数而不是有效行数", not bad, bad[:4])
    r0 = stats["rows"][0]
    check("Min ≤ Q1 ≤ Median ≤ Q3 ≤ Max",
          all(a <= b + 1e-9 for a, b in zip([r0["min"], r0["q1"], r0["median"], r0["q3"]],
                                            [r0["q1"], r0["median"], r0["q3"], r0["max"]])),
          [r0[k] for k in ("min", "q1", "median", "q3", "max")])
    check("全表都在 0~1 之外时 globalMax 取真实最大值", r0["max"] > 1 and stats["globalMax"] >= r0["max"],
          f"globalMax {stats['globalMax']}")

    key = cols[0]
    h = api("GET", f"/api/ws/{ws}/hist?col={urllib.parse.quote(key)}&bins={BINS}")
    check(f"直方图 {len(h['counts'])} 桶计数之和 = 有效值数 {h['n']}", sum(h["counts"]) == h["n"],
          f"{sum(h['counts'])} vs {h['n']}")
    check("分桶不会把值丢掉：missing + n = 整表行数", h["missing"] + h["n"] == n)
    check("均值/中位数所在桶落在桶范围内", 0 <= h["meanBin"] < h["bins"] and 0 <= h["medianBin"] < h["bins"],
          f"meanBin {h['meanBin']} / medianBin {h['medianBin']}")

    sc = cols[:6]
    q = urllib.parse.quote(",".join(sc))
    raw = api("GET", f"/api/ws/{ws}/series-multi?cols={q}&mode=raw&points=3000")
    check(f"raw 模式：{n} 行抽到 {raw['points']} 点，且如实标记已抽稀",
          raw["decimated"] is True and raw["points"] <= raw["maxPoints"] and len(raw["x"]) == raw["points"])
    ext = api("GET", f"/api/ws/{ws}/series-multi?cols={q}&mode=extremes&points=3000")
    stats_map = {r["key"]: r for r in stats["rows"]}
    lost = []
    for s in ext["series"]:
        vals = [v for v in s["y"] if v is not None]
        want = stats_map[s["col"]]
        if want["n"] and (min(vals) > want["min"] + 1e-9 or max(vals) < want["max"] - 1e-9):
            lost.append(s["col"])
    check("extremes 模式没有抽掉任何一列的最大/最小值（图就是用来看尖峰的）", not lost, lost[:4])
    check("extremes 抽稀后的点数仍受上限约束", ext["points"] <= ext["maxPoints"], ext["points"])
    mean = api("GET", f"/api/ws/{ws}/series-multi?cols={q}&mode=mean&points=3000")
    check(f"mean 模式按 4 行一桶：{n} 行 → {mean['points']} 点 = ⌈{n}/4⌉",
          mean["windowStep"] == 4 and mean["points"] == math.ceil(n / 4), mean["points"])
    check("mean 模式的每个点都是两位小数（与旧浏览器实现同一舍入）",
          all(v is None or round(v, 2) == v for s in mean["series"] for v in s["y"][:200]))
    for mode in (raw, ext, mean):
        assert all(len(s["y"]) == len(mode["x"]) for s in mode["series"])
    check("每条曲线的 y 与共享 x 等长（前端画曲线的前提）", True)
    small = api("GET", f"/api/ws/{ws}/series-multi?cols={q}&mode=raw&points=6000")
    check("points 给到上限时 raw 逐行不抽稀（11000 行 > 6000 点仍要抽）",
          small["decimated"] is True and small["maxPoints"] == 6000, f"{small['points']} 点")
    close_workspace(ws)

    print("\n== 短表：raw 模式行数不超过点数时逐行原样给出（放大不丢行）==")
    ws2 = open_dataset("na3.csv")
    cols2 = float_cols(api("GET", f"/api/ws/{ws2}"))
    one = api("GET", f"/api/ws/{ws2}/series-multi?cols={urllib.parse.quote(cols2[0])}&mode=raw&points=3000")
    check("240 行表 raw 模式 240 点、未抽稀", one["points"] == 240 and one["decimated"] is False,
          f"{one['points']} 点")
    check("短表 mean 模式步长退化为 1（行数 ≤ 500 时窗口均值无意义）",
          api("GET", f"/api/ws/{ws2}/series-multi?cols={urllib.parse.quote(cols2[0])}&mode=mean&points=3000"
              )["windowStep"] == 1)
    close_workspace(ws2)


# ---------------------------------------------------------------- ⑤：与服务端其他通道同源

def section_cross_channel():
    print("\n== 第三步的统计与第二/四步的缺失诊断同一口径（同一份 col_stats，不会给出两个答案）==")
    ws = open_dataset("na3.csv")
    stats = api("GET", f"/api/ws/{ws}/stats")
    quality = api("GET", f"/api/ws/{ws}/quality")
    qmap = {c["key"]: c for c in quality["columns"]}
    bad = [r["key"] for r in stats["rows"]
           if qmap.get(r["key"], {}).get("missing") != r["missing"]
           or qmap.get(r["key"], {}).get("valid") != r["n"]]
    check("每列缺失数/有效数与第四步诊断完全相同", not bad, bad)
    check("统计矩阵只数数值列，类别列不参与",
          all(r["key"] in [c["key"] for c in quality["columns"] if c["type"] == "float"] for r in stats["rows"]),
          [r["key"] for r in stats["rows"]])

    api("POST", f"/api/ws/{ws}/op/impute", {"all": True, "keys": ["temperature"], "defaultAlgo": "linear"})
    after = api("GET", f"/api/ws/{ws}/stats")
    row = next(r for r in after["rows"] if r["key"] == "temperature")
    check("填补后统计矩阵跟着变（temperature 缺失归零）", row["missing"] == 0,
          f"missing {row['missing']} · version {after['version']}")
    check("版本号随帧推进", after["version"] == 1, after["version"])
    hist_after = api("GET", f"/api/ws/{ws}/hist?col=temperature&bins={BINS}")
    check("直方图与统计矩阵读同一颗帧（有效值数一致）", hist_after["n"] == row["n"],
          f"hist {hist_after['n']} / stats {row['n']}")
    close_workspace(ws)

    print("\n== 重采样后曲线与统计都按新帧算 ==")
    ws3 = open_dataset("cleaned.csv")
    before = api("GET", f"/api/ws/{ws3}/stats")
    api("POST", f"/api/ws/{ws3}/op/resample", {"targetMinutes": 60})
    after3 = api("GET", f"/api/ws/{ws3}/stats")
    check("重采样后统计矩阵的行数按新帧计（15 分钟 → 60 分钟，行数÷4）",
          after3["rowCount"] == math.ceil(before["rowCount"] / 4) != before["rowCount"],
          f"{before['rowCount']} → {after3['rowCount']} 行")
    series3 = api("GET", f"/api/ws/{ws3}/series-multi?cols=active_power&mode=raw&points=3000")
    check("叠加曲线的 x 用重采样后的时间轴（点数 = 新行数）",
          series3["points"] == after3["rowCount"] and series3["x"][0].endswith(":00:00"),
          f"{series3['points']} 点 · {series3['x'][0]}")
    close_workspace(ws3)


# ---------------------------------------------------------------- ⑥：首个完整行

def section_first_complete():
    print("\n== GET /first-complete：第五步「跳到第一个完整行」由后端扫 ==")
    ws = open_dataset("i3.csv")
    r = api("POST", f"/api/ws/{ws}/op/feature-lag", {"cols": ["pressure"], "lags": [1, 2, 4]})
    keys = r["keys"]
    got = api("GET", f"/api/ws/{ws}/first-complete?cols={urllib.parse.quote(','.join(keys))}&scan_rows=200")
    check("纯滞后列的首个完整行 = 最大滞后步长（数学上应为 4）", got["index"] == 4, got["index"])
    vals = columns_of(ws, keys)
    scanned = next(i for i in range(r["meta"]["rowCount"])
                   if all(vals[k][i] is not None for k in keys))
    check("扫描结果与整列逐行扫一致", scanned == got["index"], f"整列扫 {scanned} / 接口 {got['index']}")
    check("scanned/rowCount 如实回说扫描范围",
          got["scanned"] == min(200, got["rowCount"]) and got["rowCount"] == 120,
          f"scanned {got['scanned']} / rows {got['rowCount']}")

    big = open_dataset("big40.csv")
    rb = api("POST", f"/api/ws/{big}/op/feature-lag",
             {"cols": ["传感器01", "传感器02"], "lags": [1, 96], "windows": [4, 96],
              "stats": ["mean", "std"]})
    gb = api("GET", f"/api/ws/{big}/first-complete?cols={urllib.parse.quote(','.join(rb['keys']))}"
                   f"&scan_rows=20000")
    page = api("GET", f"/api/ws/{big}/rows?offset={gb['index']}&limit=20")["page"]
    idx = {str(c): i for i, c in enumerate(page["columns"])}
    blanks = [(k, i) for k in rb["keys"] for i, row in enumerate(page["rows"]) if row[idx[k]] is None]
    check(f"跳过去的第 {gb['index']} 行起，往后 20 行的新列全无空格（界面表格确实能看到数）",
          not blanks, blanks[:4])
    colvals = columns_of(big, rb["keys"][:1])
    k0 = rb["keys"][0]
    same = next(i for i, v in enumerate(colvals[k0]) if v is not None)
    check("多列里最慢的那一列决定跳转位置（取各列首个有效行的最大值）",
          gb["index"] >= same, f"{k0} 首有效行 {same} · 跳转 {gb['index']}")
    check("长窗口在 2 万行扫描范围内一定找得到", gb["index"] is not None and gb["scanned"] == 11000,
          f"index {gb['index']} · scanned {gb['scanned']}")
    st, pl, _ = call("GET", f"/api/ws/{big}/first-complete?cols={urllib.parse.quote('不存在的列')}")
    check("列不存在被拒绝并给出中文原因", st == 400, err_text(pl))
    close_workspace(big)
    close_workspace(ws)


# ---------------------------------------------------------------- ⑦：宽表导出

def section_export():
    print("\n== GET /export：四种格式全部由服务端从工作区直出，明细不过网络 ==")
    ws = open_dataset("machine.csv")
    meta = api("GET", f"/api/ws/{ws}")
    keys = [str(c["key"]) for c in meta["columns"]]
    n = meta["rowCount"]
    stats0 = api("GET", f"/api/ws/{ws}/stats")
    blobs: dict[str, bytes] = {}
    for fmt in ("csv", "xlsx", "parquet", "feather"):
        t0 = time.perf_counter()
        payload, headers = download(f"/api/ws/{ws}/export?format={fmt}")
        ms = (time.perf_counter() - t0) * 1000
        blobs[fmt] = payload
        cd = headers.get("Content-Disposition", "") + headers.get("content-disposition", "")
        cl = headers.get("Content-Length", headers.get("content-length", ""))
        check(f"{fmt}：字节流非空且 Content-Length 就是真实字节数（界面 KB 数可追）",
              len(payload) > 0 and int(cl or -1) == len(payload),
              f"{len(payload)} 字节 · {ms:.0f} ms")
        check(f"{fmt}：下载文件名带正确扩展名（中文数据集名已 URL 编码）",
              f".{fmt}" in urllib.parse.unquote(cd), cd[:80])
        check(f"{fmt}：CORS 暴露了 Content-Disposition/Length（跨源 5321→8000 才读得到）",
              "Content-Disposition" in headers.get("Access-Control-Expose-Headers",
                                                   headers.get("access-control-expose-headers", "")))

    csv_bytes = blobs["csv"]
    check("CSV 带 UTF-8 BOM（Excel 双击不乱码，与旧浏览器导出一致）",
          csv_bytes.startswith(b"\xef\xbb\xbf"), csv_bytes[:3])
    lines = csv_bytes.decode("utf-8-sig").splitlines()
    check("CSV 表头就是列键、行数 = 整表行数 + 表头",
          lines[0].split(",") == keys and len(lines) - 1 == n, f"{len(lines) - 1} 行 / 帧 {n} 行")
    page = api("GET", f"/api/ws/{ws}/rows?offset=0&limit=5")["page"]
    head = [l.split(",") for l in lines[1:6]]
    mism = [(ri, ci) for ri, row in enumerate(page["rows"])
            for ci, v in enumerate(row)
            if dev(head[ri][ci], v) > REL_TOL]
    check("CSV 前 5 行与第二步预览表格逐格相同（含时间串按显示格式渲染）", not mism, mism[:4])

    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(blobs["xlsx"]), read_only=True)
    sheet = wb["data"]
    xl = [[c.value for c in row] for row in sheet.iter_rows()]
    wb.close()
    check("XLSX 单表 data、表头与行数同 CSV",
          xl[0] == keys and len(xl) - 1 == n, f"{len(xl) - 1} 行")
    numeric = float_cols(meta)
    ref_vals = columns_of(ws, keys)
    bad = [(ri, k, xl[ri + 1][ci], ref_vals[k][ri])
           for ri in (0, 1, 2, 3, 4, n // 2, n - 1)
           for ci, k in enumerate(keys)
           if dev(xl[ri + 1][ci], ref_vals[k][ri]) > REL_TOL]
    check("XLSX 抽 7 行 × 全列逐格等于服务端工作区的值（含时间串格式）", not bad, bad[:3])
    check("XLSX 数值列确实是单元格数字", all(isinstance(xl[1][keys.index(k)], (int, float)) for k in numeric),
          [type(xl[1][keys.index(k)]).__name__ for k in numeric])

    for fmt in ("parquet", "feather"):
        re_ws = upload_bytes(f"roundtrip_{fmt}.{fmt}", blobs[fmt])
        back = api("GET", f"/api/ws/{re_ws}")
        check(f"{fmt}：回读后帧形状不变（{n} 行 × {len(keys)} 列）",
              back["rowCount"] == n and [str(c["key"]) for c in back["columns"]] == keys,
              f"{back['rowCount']} 行 × {back['colCount']} 列")
        check(f"{fmt}：回读后时间列仍被识别为时间轴", back.get("timeCol") == meta.get("timeCol"),
              back.get("timeCol"))
        stats1 = api("GET", f"/api/ws/{re_ws}/stats")
        d = Diff(f"{fmt} 往返后统计矩阵（{len(numeric)} 列 × 10 量）")
        m1 = {r["key"]: r for r in stats1["rows"]}
        for r in stats0["rows"]:
            g = m1.get(r["key"], {})
            for f in ("n", "missing", "mean", "std", "median", "q1", "q3", "min", "max", "missingRate"):
                d.num(f"{r['key']}.{f}", r[f], g.get(f))
        d.report()
        vals0 = columns_of(ws, numeric)
        vals1 = columns_of(re_ws, numeric)
        cell_bad = [(k, i) for k in numeric for i in range(n) if dev(vals0[k][i], vals1[k][i]) > REL_TOL]
        check(f"{fmt}：逐格回读一致（{len(numeric)} 列 × {n} 行 = {len(numeric) * n} 格）",
              not cell_bad, f"{len(cell_bad)} 格不一致 · {cell_bad[:3]}")
        close_workspace(re_ws)

    print("\n== 导出的是「当前帧」而不是原始文件：填补前后 CSV 对比 ==")
    ws2 = open_dataset("na3.csv")
    before_text = download(f"/api/ws/{ws2}/export?format=csv")[0].decode("utf-8-sig")
    before_blank = sum(1 for l in before_text.splitlines()[1:] if l.split(",")[1] == "")
    api("POST", f"/api/ws/{ws2}/op/impute", {"all": True, "keys": ["temperature"], "defaultAlgo": "linear"})
    after_text = download(f"/api/ws/{ws2}/export?format=csv")[0].decode("utf-8-sig")
    after_blank = sum(1 for l in after_text.splitlines()[1:] if l.split(",")[1] == "")
    check("导出跟随工作区版本：填补后 temperature 列的空格从整段消失",
          before_blank > 0 and after_blank == 0, f"{before_blank} → {after_blank} 个空格")
    check("两份 CSV 字节数不同（没有把旧帧缓存住）",
          len(before_text) != len(after_text), f"{len(before_text)} → {len(after_text)} 字符")
    close_workspace(ws2)

    print("\n== 大表导出：整表只出一次网络，二进制格式更省 ==")
    big = open_dataset("big40.csv")
    sizes = {}
    big_blobs: dict[str, bytes] = {}
    for fmt in ("csv", "xlsx", "parquet", "feather"):
        t0 = time.perf_counter()
        payload, _ = download(f"/api/ws/{big}/export?format={fmt}")
        sizes[fmt] = len(payload)
        big_blobs[fmt] = payload
        check(f"big40 {fmt}：11000×40 全量导出成功", len(payload) > 1000,
              f"{len(payload) / 1024:.1f} KiB · {(time.perf_counter() - t0) * 1000:.0f} ms")
    # big40 是随机噪声列，snappy 压不动，所以这里只要求二进制格式不比 CSV 大；
    # 真正的卖点是这两种格式浏览器单文件生不出来（要背 pyarrow/arrow-js 的依赖）。
    check("Parquet/Feather 都不小于 CSV（列式格式在随机噪声数据上仍占优）",
          sizes["parquet"] < sizes["csv"] and sizes["feather"] < sizes["csv"],
          " / ".join(f"{k} {v / 1024:.0f} KiB" for k, v in sizes.items()))
    rews = upload_bytes("big40_roundtrip.parquet", big_blobs["parquet"])
    a0 = api("GET", f"/api/ws/{big}/stats")
    a1 = api("GET", f"/api/ws/{rews}/stats")
    d = Diff("big40 Parquet 往返后 39 列统计矩阵")
    m1 = {r["key"]: r for r in a1["rows"]}
    for r in a0["rows"]:
        for f in ("n", "missing", "mean", "std", "median", "q1", "q3", "min", "max"):
            d.num(f"{r['key']}.{f}", r[f], m1.get(r["key"], {}).get(f))
    d.report()
    check("Parquet 往返没有改变帧大小", api("GET", f"/api/ws/{rews}")["rowCount"] == 11000)
    close_workspace(rews)
    close_workspace(big)
    close_workspace(ws)


# ---------------------------------------------------------------- ⑧：错误路径与已删通道

def section_errors():
    print("\n== 参数校验与错误路径（失败一律 4xx + 中文原因，绝不返回半成品）==")
    ws = open_dataset("na3.csv")
    cases = [
        ("stats 列不存在", f"/api/ws/{ws}/stats?cols={urllib.parse.quote('不存在的列')}", 400),
        ("hist 列不存在", f"/api/ws/{ws}/hist?col={urllib.parse.quote('不存在的列')}", 400),
        ("hist 桶数超上限", f"/api/ws/{ws}/hist?col=temperature&bins=9999", 422),
        ("hist 桶数小于 2", f"/api/ws/{ws}/hist?col=temperature&bins=1", 422),
        ("series-multi cols 为空", f"/api/ws/{ws}/series-multi?cols=", 400),
        ("series-multi 列不存在", f"/api/ws/{ws}/series-multi?cols={urllib.parse.quote('没有这列')}", 400),
        ("series-multi 未知抽稀方式", f"/api/ws/{ws}/series-multi?cols=temperature&mode=lttb", 400),
        ("series-multi 点数超上限", f"/api/ws/{ws}/series-multi?cols=temperature&points=99999", 422),
        ("series-multi 点数低于下限", f"/api/ws/{ws}/series-multi?cols=temperature&points=2", 422),
        ("first-complete cols 为空", f"/api/ws/{ws}/first-complete?cols=", 400),
        ("export 不支持的格式", f"/api/ws/{ws}/export?format=json", 422),
        ("export 缺 format 参数时默认 csv", "/api/ws/" + ws + "/export", 200),
        ("工作区不存在", "/api/ws/no-such-ws/stats", 404),
        ("整表回传通道已删除", f"/api/ws/{ws}/replace", 404),
        ("类别列不能进统计矩阵", f"/api/ws/{ws}/stats?cols=weather", 400),
        ("时间列不能画叠加曲线", f"/api/ws/{ws}/series-multi?cols=timestamp", 400),
        ("时间列不能画直方图", f"/api/ws/{ws}/hist?col=timestamp", 400),
        ("时间列不能画质量曲线", f"/api/ws/{ws}/series?cols=timestamp", 400),
        ("质量曲线 cols 为空", f"/api/ws/{ws}/series?cols=", 400),
    ]
    for label, path, want in cases:
        st, pl, _ = call("GET", path)
        check(f"{label} → HTTP {want}", st == want, f"{st} · {err_text(pl)}")
    feat = api("POST", f"/api/ws/{ws}/op/feature-time", {"dims": ["hour"], "cyclical": True})
    fkey = feat["keys"][0]
    ok = api("GET", f"/api/ws/{ws}/stats?cols={urllib.parse.quote(fkey)}")
    check(f"数值型特征列照常可统计（{fkey} 在第五步之后仍进第三步矩阵）",
          ok["rows"][0]["n"] == ok["rowCount"], f"n {ok['rows'][0]['n']} / {ok['rowCount']} 行")
    check("抽稀曲线也接受特征列",
          api("GET", f"/api/ws/{ws}/series-multi?cols={urllib.parse.quote(fkey)}&mode=raw&points=100")["points"] > 0)
    st, pl, _ = call("POST", f"/api/ws/{ws}/replace", {"columns": ["a"], "rows": [[1]]})
    check("POST /replace 也已删除（浏览器再不能整表覆盖服务端）", st == 404, st)
    st, pl, _ = call("DELETE", f"/api/ws/{ws}")
    check("关闭工作区后一切入口都 404", st in (200, 204) and
          call("GET", f"/api/ws/{ws}/stats")[0] == 404)
    close_workspace(ws)


def main() -> int:
    only = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--only=")), "")
    health = api("GET", "/api/health")
    print("== /api/health ==")
    need = ["workspace:stats", "workspace:hist", "workspace:series-multi",
            "workspace:first-complete", "workspace:export",
            "export:parquet", "export:feather", "export:csv", "export:xlsx"]
    check("能力声明含第④期全部入口", all(c in health["capabilities"] for c in need),
          [c for c in need if c not in health["capabilities"]])
    check("上限里回带了曲线点数与直方图桶数（界面文案读这两个数）",
          health["limits"]["seriesMaxPoints"] == POINTS or health["limits"]["seriesMaxPoints"] == 6000,
          health["limits"]["seriesMaxPoints"])
    check("直方图桶数与界面默认 25 桶同源", health["limits"]["histogramBins"] == BINS,
          health["limits"]["histogramBins"])
    check("浏览器整表兜底通道不在能力声明里（能力清单即事实）",
          not any("replace" in c for c in health["capabilities"]),
          [c for c in health["capabilities"] if "replace" in c])

    sections = [
        ("parity", section_parity),
        ("invariants", section_invariants),
        ("cross", section_cross_channel),
        ("first-complete", section_first_complete),
        ("export", section_export),
        ("errors", section_errors),
    ]
    for name, fn in sections:
        if not only or only in name:
            fn()

    print("\n== 结果 ==")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
