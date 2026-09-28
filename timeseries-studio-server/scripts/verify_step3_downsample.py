r"""第三步叠加曲线的降采样验收：LTTB 与「全量不抽点」两条新语义，外加旧档位的回归。

为什么每条都要独立证据：
1. LTTB 是这次新写的几何算法，服务端自证没有意义 —— 参照实现是 scripts/lttb_ref.mjs（纯 JS，
   按 Steinarsson 定义另写一遍），喂给它的原始值是从 mode=raw 端点取回的**整列全量**，
   两边逐下标比对。单列时这就是教科书 LTTB 的定义级对拍。
2. 「全量」这一档的语义承诺是「一个点都不抽」，所以拿 /columns 的真实列值逐格对回响应，
   并确认 points == 窗口行数、decimated 为假。
3. 全量超过单次格子数上限时必须**报错**，不许偷偷抽点 —— 那样界面上的「全量」就成了假话。

跑法（后端要在 127.0.0.1:8000 上）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_step3_downsample.py
"""
from __future__ import annotations

import json
import math
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
ROOT = pathlib.Path(__file__).resolve().parents[1]
LTTB_REF = ROOT / "scripts" / "lttb_ref.mjs"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + str(detail)) if detail != '' else ''}")
    if not cond:
        FAILURES.append(name)
    return cond


def api(method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw}


def open_dataset(filename: str) -> str:
    status, payload = api("POST", "/api/ws/dataset", {"filename": filename})
    if status >= 400:
        raise SystemExit(f"打开 {filename} 失败 → {status} {payload}")
    return payload["meta"]["wsId"]


def close(ws: str):
    api("DELETE", f"/api/ws/{ws}")


def q(cols: list[str]) -> str:
    return urllib.parse.quote(",".join(cols))


def series(ws: str, cols: list[str], mode: str | None = None, points: int = 3000,
           span: str = "all", offset: int = 0):
    tail = f"cols={q(cols)}&points={points}&span={span}&offset={offset}"
    if mode:
        tail += f"&mode={mode}"
    return api("GET", f"/api/ws/{ws}/series-multi?{tail}")


def float_cols(ws: str) -> list[str]:
    _, meta = api("GET", f"/api/ws/{ws}")
    return [c["key"] for c in meta["columns"] if c["type"] == "float"]


def lttb_ref(values_by_col: list[list], threshold: int) -> list[int]:
    """把全量取值交给纯 JS 参照实现，拿回它选的点。"""
    payload = json.dumps({"cols": values_by_col, "threshold": threshold})
    proc = subprocess.run(["node", str(LTTB_REF)], input=payload.encode("utf-8"),
                          capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise SystemExit("LTTB 参照实现失败：" + proc.stderr.decode("utf-8", "replace")[:400])
    return json.loads(proc.stdout.decode("utf-8"))["positions"]


# ---------------------------------------------------------------- A：档位与默认值

def section_defaults():
    print("\n== A. 后端声明的档位、默认模式与参数校验 ==")
    _, health = api("GET", "/api/health")
    lim = health["limits"]
    check("health 列出四档降采样方式", lim["seriesModes"] == ["raw", "extremes", "mean", "lttb"],
          lim["seriesModes"])
    check("health 声明的默认档位是 lttb（界面照这一份摆默认值，不自己猜）",
          lim["seriesDefaultMode"] == "lttb", lim["seriesDefaultMode"])
    check("health 给出全量档的格子数上限", lim["seriesFullRawMaxValues"] == 600_000,
          lim["seriesFullRawMaxValues"])

    ws = open_dataset("cleaned.csv")
    col = float_cols(ws)[0]
    st, got = series(ws, [col], mode=None, points=300)
    check("不带 mode 参数时后端默认走 lttb", (st, got["mode"]) == (200, "lttb"), got.get("mode"))
    check("lttb 的点数落在请求的上限上", (got["points"], got["maxPoints"]) == (300, 300),
          f"{got['points']} 点 / 上限 {got['maxPoints']}")
    st, bad = api("GET", f"/api/ws/{ws}/series-multi?cols={q([col])}&mode=nope")
    check("不认识的档位报 400 并列出可选值",
          st == 400 and "lttb" in bad["detail"] and "raw" in bad["detail"], bad["detail"])
    close(ws)


# ---------------------------------------------------------------- B：LTTB 定义级对拍

def section_lttb_parity():
    print("\n== B. LTTB 逐点对拍（原始值取自 mode=raw 全量，参照是纯 JS 独立实现） ==")
    cases = [("cleaned.csv", 3, "PV 实测：夜间整段零值 + 少量缺失"),
             ("na3.csv", 3, "缺失值密布的短表"),
             ("big40.csv", 12, "11000 行 × 40 列的大表")]
    for filename, ncol, why in cases:
        ws = open_dataset(filename)
        cols = float_cols(ws)[:ncol]
        # 全量档就是「一行都不抽」，所以它正好是参照实现的输入
        st, full = series(ws, cols, mode="raw")
        if not check(f"{filename}：全量档取回整表", st == 200 and not full["decimated"],
                     f"{full.get('points')} 点 / {full.get('rowCount')} 行"):
            close(ws)
            continue
        by_col = {s["col"]: s["y"] for s in full["series"]}
        values = [by_col[c] for c in cols]
        for points in (300, 1200):
            cap = max(20, min(points, 6000))
            if full["windowRows"] <= cap:
                continue
            t0 = time.perf_counter()
            want = lttb_ref(values, cap)
            ms_ref = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            st, got = series(ws, cols, mode="lttb", points=points)
            ms_srv = (time.perf_counter() - t0) * 1000
            if not check(f"{filename} × {len(cols)} 列 · points={points}：后端取回 lttb 结果",
                         st == 200, got.get("detail") if st >= 400 else got.get("points")):
                continue
            same = got["idx"] == want
            diff = [i for i, (a, b) in enumerate(zip(got["idx"], want)) if a != b]
            check(f"{filename} · points={points}：选点与 JS 参照逐下标相同（{len(want)} 个点）",
                  same, f"首个不同 {('idx ' + str(diff[0])) if diff else '—'}")
            check(f"{filename} · points={points}：点数落在上限、首尾两行必留",
                  got["points"] == len(want) == cap and got["idx"][0] == 0
                  and got["idx"][-1] == got["windowRows"] - 1,
                  f"{got['points']} 点，首 {got['idx'][0]} 尾 {got['idx'][-1]} / 窗口 {got['windowRows']} 行")
            mono = all(b > a for a, b in zip(got["idx"], got["idx"][1:]))
            check(f"{filename} · points={points}：行号严格递增（时间轴不会倒着走）", mono)
            # 取值必须就是那一行的真数：降采样只许挑行，不许改数
            mismatch = []
            for s in got["series"]:
                raw = by_col[s["col"]]
                for pos, row in enumerate(got["idx"]):
                    if raw[row] != s["y"][pos]:
                        mismatch.append((s["col"], row, raw[row], s["y"][pos]))
            check(f"{filename} · points={points}：每个点都等于窗口内那一行的原始值",
                  not mismatch, mismatch[:2])
            missing_ok = all(s["missing"] == sum(1 for v in s["y"] if v is None) for s in got["series"])
            check(f"{filename} · points={points}：缺失行仍是 null（没被代入值填成数）", missing_ok)
            print(f"      耗时：JS 参照 {ms_ref:.0f} ms / 服务端 {ms_srv:.0f} ms")
        close(ws)


# ---------------------------------------------------------------- C：全量语义

def section_full_mode():
    print("\n== C. 「全量」到底给不给整表 ==")
    ws = open_dataset("big40.csv")
    cols = float_cols(ws)[:6]
    st, got = series(ws, cols, mode="lttb", points=3000)
    st2, full = series(ws, cols, mode="raw")
    n = got["rowCount"]
    check("全量档在 11000 行表上一行不抽（lttb 同表只给 3000 点作对照）",
          (st2, full["points"], full["windowRows"], full["decimated"]) == (200, n, n, False),
          f"全量 {full['points']:,} 点 vs lttb {got['points']:,} 点（共 {n:,} 行）")
    check("全量档的 maxPoints 报成 null（点数上限对它不成立，不能一边抽点一边自称全量）",
          full["maxPoints"] is None and got["maxPoints"] == 3000,
          f"raw {full['maxPoints']!r} / lttb {got['maxPoints']}")
    columns = api("GET", f"/api/ws/{ws}/columns?keys={q(cols)}")[1]["columns"]
    bad = []
    for s in full["series"]:
        want = [None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
                for v in columns[s["col"]]]
        if want != s["y"]:
            bad.append(s["col"])
    check("全量档每条序列与 /columns 取回的真实整列逐格相等", not bad, bad[:3])
    check("全量档的时间轴与行号一一对应，最后一行也在轴上",
          len(full["x"]) == full["points"] == len(full["idx"]) and full["idx"][-1] == n - 1,
          f"x {len(full['x']):,} 条 / 末行 {full['idx'][-1]} / 共 {n:,} 行，末标签 {full['x'][-1]}")

    st, day = series(ws, [cols[0]], mode="raw", span="day", offset=3)
    check("全量档在时间窗口里同样一行不抽",
          st == 200 and day["points"] == day["windowRows"] and not day["decimated"],
          f"第 {day['window']['periodIndex']} 个自然日 {day['windowRows']} 行 → {day['points']} 点")
    close(ws)


# ---------------------------------------------------------------- D：全量上限守卫

def section_guard():
    print("\n== D. 全量档超过格子数上限时报错，不偷偷抽点 ==")
    probe = ROOT / "dataset" / "zz_full_guard_probe.csv"
    rows, ncols = 40_000, 20          # 80 万格 > 60 万上限
    if probe.exists():
        probe.unlink()
    with probe.open("w", encoding="utf-8") as f:
        f.write("采集时刻," + ",".join(f"c{i}" for i in range(ncols)) + "\n")
        t0 = time.time()
        for r in range(rows):
            f.write("2024-01-01 {:02d}:{:02d}:00,".format(r // 60 % 24, r % 60)
                    + ",".join(f"{(r + i) % 97}.5" for i in range(ncols)) + "\n")
    print(f"      造 {rows:,} 行 × {ncols} 列的探针文件用了 {(time.time() - t0) / 1:.0f} s")
    try:
        ws = open_dataset(probe.name)
        cols = [f"c{i}" for i in range(ncols)]
        st, got = series(ws, cols, mode="raw")
        check("全量档撞上上限时报 400", st == 400, f"{st} · {str(got.get('detail'))[:90]}")
        detail = str(got.get("detail", ""))
        check("报错里点名行数、列数、格子数与上限（让人知道该怎么收窄）",
              all(s in detail for s in ("40,000", "20", "800,000", "600,000")), detail)
        st, ok = series(ws, cols[:2], mode="raw")
        check("同样这份表，只画 2 列（8 万格）时全量档正常返回",
              st == 200 and ok["points"] == rows, f"{ok.get('points')} 点 / {rows:,} 行")
        st, lt = series(ws, cols, mode="lttb", points=3000)
        check("撞上上限的是全量档，不是 lttb：20 列 × 40000 行照样给 3000 点",
              st == 200 and lt["points"] == 3000, lt.get("detail") if st >= 400 else lt["points"])
        close(ws)
    finally:
        probe.unlink(missing_ok=True)


# ---------------------------------------------------------------- E：旧档位回归 + 形态

def section_regression():
    print("\n== E. 旧档位回归与「尖峰有没有被保住」 ==")
    ws = open_dataset("cleaned.csv")
    cols = float_cols(ws)[:4]
    n = None
    for mode in ("extremes", "mean", "lttb", "raw"):
        st, got = series(ws, cols, mode=mode, points=1200)
        if st < 400:
            n = got["rowCount"]
            print(f"      mode={mode}: {got['points']} 点 / 窗口 {got['windowRows']} 行 · "
                  f"windowStep {got['windowStep']} · decimated {got['decimated']}")
    st, ex = series(ws, cols, mode="extremes", points=1200)
    st2, full = series(ws, cols, mode="raw")
    by_col = {s["col"]: s["y"] for s in full["series"]}
    lost = []
    for s in ex["series"]:
        raw = [v for v in by_col[s["col"]] if v is not None]
        vals = [v for v in s["y"] if v is not None]
        if raw and (max(raw) not in vals or min(raw) not in vals):
            lost.append(s["col"])
    check("extremes 仍保留每列的最大/最小值（这一档存在的理由就是看尖峰）", not lost, lost[:3])
    st, mean = series(ws, cols, mode="mean", points=1200)
    expect = math.ceil(mean["windowRows"] / mean["windowStep"])
    check("mean 仍是每 windowStep 行一桶", mean["points"] == expect,
          f"{mean['points']} 点 = ceil({mean['windowRows']}/{mean['windowStep']})")

    # 单点尖峰：等距抽稀必把它抽掉，LTTB 应该选上
    spike = ROOT / "dataset" / "zz_spike_probe.csv"
    if spike.exists():
        spike.unlink()
    rows = 12_000
    peak = 7_777
    with spike.open("w", encoding="utf-8") as f:
        f.write("采集时刻,功率\n")
        for r in range(rows):
            f.write("2024-01-01 00:00:00,{}.0\n".format(3000 if r == peak else r % 5))
    try:
        ws2 = open_dataset(spike.name)
        st, lt = series(ws2, ["功率"], mode="lttb", points=600)
        st5, rawfull = series(ws2, ["功率"], mode="raw")
        # 等距取点是「全量」这一档以前的做法：步长 ceil(12000/600)=20，峰值行 7777 落在步长外
        step = math.ceil(rawfull["windowRows"] / 600)
        stride = set(range(0, rawfull["windowRows"], step)) | {rawfull["windowRows"] - 1}
        check("单点尖峰（12000 行里的第 7777 行）被 LTTB 选中", st == 200 and peak in lt["idx"],
              f"lttb {lt.get('points')} 点，含峰值行 {peak in lt.get('idx', [])}")
        check("同一根尖峰在旧的等距取点下会整个消失（证明 LTTB 不是换个名字的等距抽稀）",
              peak not in stride, f"等距步长 {step}，7777 % {step} = {peak % step}")
        check("全量档把 12000 行全给了（尖峰自然在）",
              rawfull["points"] == rows and peak in rawfull["idx"], f"{rawfull['points']} 点")
        close(ws2)
    finally:
        spike.unlink(missing_ok=True)
    close(ws)


def main():
    if not LTTB_REF.exists():
        raise SystemExit(f"缺少 LTTB 参照实现：{LTTB_REF}")
    section_defaults()
    section_lttb_parity()
    section_full_mode()
    section_guard()
    section_regression()
    print(f"\n结果：{'全部通过' if not FAILURES else str(len(FAILURES)) + ' 条未通过'}")
    for f in FAILURES:
        print("  FAIL · " + f)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
