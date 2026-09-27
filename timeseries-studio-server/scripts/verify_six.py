"""六项修复的后端验收：划分列 / 时间窗口 / 分组生成 / sin-cos 替换原列 / 节假日表可配置。

每一项都不信 handler 自己回的那串数字：整表用 /export?format=csv 拉回来交给 pandas 重算，
两边对得上才算过。

  A 契约：health 的能力位与上限（界面只读这一份）
  B 项2 时间窗口：all/year/month/week/day 的窗口行数、边界、画出来的点，与 pandas 按日期重筛逐档对拍
  C 项1 划分列：/op/split 写的 train/val/test 三段计数 = 独立按公式算的数，CSV 里逐行验顺序不打乱
  D 项5 时间特征：sin/cos 选哪几维、是否替换数值原列，列名与数值都按 numpy 重算一遍
  E 项6 节假日表：GET/POST /holidays 与 feat_holiday 命中数三方一致；改表→撤销→重放都跟着变
  F 项4 分组生成：滞后/滑动窗口、差分/频域四组各自只出各自的列，跨组提交一律拒绝
  G 可逆与重建：逐版 restore 指纹、关掉工作区再按日志重开，列一颗不差

本脚本**自带后端**：在临时状态目录与临时数据集目录里拉起 uvicorn，不碰开发机 8000 端口那台。

用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_six.py
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
BASE = ""
PROC: subprocess.Popen | None = None
STATE_DIR = ROOT.parent / ".verify" / "six-state"
DATA_DIR = ROOT.parent / ".verify" / "six-dataset"
FAILURES: list[str] = []

# 造一份有明确自然周期含义的表：2024-01-01 起、整点、1000 天 = 24000 行。
# 2024-01-01 是周一，2024 是闰年 → 四个窗口的行数事先就能算死：
#   day 24 / week 168 / month 744 / year 8784
HOURS = 1000 * 24
EXPECT_SPAN = {"day": 24, "week": 168, "month": 31 * 24, "year": 366 * 24}
START = pd.Timestamp("2024-01-01 00:00:00")
CUSTOM_HOLIDAYS = ["2024-01-01", "2024-01-22", "2024-02-10", "2024-03-12"]


def check(name: str, cond: bool, detail="") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + str(detail)) if detail != '' else ''}")
    if not cond:
        FAILURES.append(name)
    return cond


def call(method: str, path: str, body: dict | None = None,
         timeout: int = 240) -> tuple[int, bytes]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def api(method: str, path: str, body: dict | None = None) -> dict:
    status, payload = call(method, path, body)
    text = payload.decode("utf-8", "replace")
    if status >= 400:
        raise SystemExit(f"请求失败 {method} {path} → {status} {text[:400]}")
    return json.loads(text)


def status_detail(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    status, payload = call(method, path, body)
    try:
        return status, str(json.loads(payload.decode("utf-8", "replace")).get("detail"))[:200]
    except json.JSONDecodeError:
        return status, payload.decode("utf-8", "replace")[:200]


# ---------------------------------------------------------------- 后端进程

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn_backend(port: int) -> None:
    global BASE, PROC
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"TSS_STATE_DIR": str(STATE_DIR), "TSS_DATASET_DIR": str(DATA_DIR),
                "PYTHONIOENCODING": "utf-8"})
    PROC = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    BASE = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=3) as res:
                if res.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise SystemExit("临时后端起不来")


def kill_backend() -> None:
    global PROC
    if PROC is None:
        return
    PROC.terminate()
    try:
        PROC.wait(timeout=30)
    except subprocess.TimeoutExpired:
        PROC.kill()
        PROC.wait(timeout=10)
    PROC = None


# ---------------------------------------------------------------- 数据与工具

def make_csv() -> bytes:
    """每小时一行，power 带几颗缺失，temp 是一条平滑日变化。"""
    out = io.StringIO()
    out.write("timestamp,power,temp\n")
    for i in range(HOURS):
        t = START + pd.Timedelta(hours=i)
        # 第 100、101、9000 行的 power 留空：窗口抽稀要能带着缺失点走
        v = "" if i in (100, 101, 9000) else f"{400.0 + 120.0 * math.sin(i / 24 * 2 * math.pi) + (i % 7):.2f}"
        temp = f"{12.0 + 9.0 * math.sin((i % 24) / 24 * 2 * math.pi) - 4.0 * math.cos(i / 24 / 365 * 2 * math.pi):.2f}"
        out.write(f"{t.strftime('%Y-%m-%d %H:%M:%S')},{v},{temp}\n")
    return out.getvalue().encode("utf-8")


def upload_csv(blob: bytes, filename: str = "six.csv") -> dict:
    """persist=true：来源文件落进数据集目录，重启之后才拼得回这张表（G 段要用）。"""
    boundary = "----tss" + uuid.uuid4().hex
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"))
    buf.write(b"Content-Type: text/csv\r\n\r\n")
    buf.write(blob)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(BASE + "/api/ws?persist=true", data=buf.getvalue(), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=240) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"上传失败 → {exc.code} {exc.read().decode('utf-8', 'replace')[:300]}")


def op(ws: str, kind: str, params: dict) -> dict:
    return api("POST", f"/api/ws/{ws}/op/{kind}", params)


def frame(ws: str) -> pd.DataFrame:
    """整表拉回来交给 pandas：这是与 handler 完全无关的第二套计算。"""
    status, payload = call("GET", f"/api/ws/{ws}/export?format=csv")
    if status >= 400:
        raise SystemExit(f"导出失败 → {status} {payload.decode('utf-8', 'replace')[:300]}")
    return pd.read_csv(io.BytesIO(payload))


def digest(ws: str) -> str:
    status, payload = call("GET", f"/api/ws/{ws}/export?format=csv")
    if status >= 400:
        raise SystemExit(f"导出失败 → {status}")
    return hashlib.sha256(payload).hexdigest()[:16]


def col_labels(ws: str) -> dict[str, str]:
    m = api("GET", f"/api/ws/{ws}")
    return {c["key"]: c for c in m["columns"]}


# ============================================================ A 契约
def section_a() -> None:
    print("\n== A 契约：health 能力位与上限 ==")
    h = api("GET", "/api/health")
    caps = set(h.get("capabilities") or [])
    need = {"op:split", "workspace:holidays", "op:holidays",
            "feature-group:lag-window", "feature-group:diff-fft", "feature-sincos-replace",
            "series-window:year-month-week-day", "series-period-paging"}
    check("A1 六项对应的能力位全部已声明", need <= caps, sorted(need - caps) or "齐")
    lim = h.get("limits") or {}
    check("A2 时间窗口档位与后端 SPANS 一致",
          list(lim.get("seriesSpans") or []) == ["all", "year", "month", "week", "day"],
          lim.get("seriesSpans"))
    check("A3 节假日预设年份/上限给出", lim.get("holidayPresetYears") == ["2024"]
          and lim.get("maxHolidayDays") == 200,
          f"{lim.get('holidayPresetYears')} / {lim.get('maxHolidayDays')}")


# ============================================================ B 项2：时间窗口
def section_b(ws: str) -> None:
    print("\n== B 项2：第三步 年/月/周/日/全量 窗口（窗口行数由后端按自然周期筛） ==")
    df = frame(ws)
    ts = pd.to_datetime(df["timestamp"])
    numeric = "power,temp"
    for span in ("all", "year", "month", "week", "day"):
        r = api("GET", f"/api/ws/{ws}/series-multi?cols={numeric}&mode=extremes&points=3000&span={span}")
        w = r["window"]
        if span == "all":
            expect_rows, expect_from, expect_to = HOURS, None, None
        else:
            expect_rows = EXPECT_SPAN[span]
            t0 = ts.iloc[0]
            if span == "day":
                s = t0.normalize()
                e = s + pd.Timedelta(days=1)
            elif span == "week":      # 2024-01-01 本身就是周一
                s = t0.normalize()
                e = s + pd.Timedelta(days=7)
            elif span == "month":
                s = t0.normalize().replace(day=1)
                e = s + pd.DateOffset(months=1)
            else:
                s = t0.normalize().replace(month=1, day=1)
                e = s + pd.DateOffset(years=1)
            expect_from, expect_to = s.strftime("%Y-%m-%d %H:%M:%S"), e.strftime("%Y-%m-%d %H:%M:%S")
        mask = (ts >= pd.Timestamp(expect_from)) & (ts < pd.Timestamp(expect_to)) if expect_from else pd.Series(True, index=ts.index)
        pandas_rows = int(mask.sum())
        ok_rows = r["windowRows"] == expect_rows == pandas_rows
        x = pd.to_datetime(pd.Series(r["x"])) if r["x"] else pd.Series(dtype="datetime64[ns]")
        in_window = bool(len(x)) and (x.min() >= pd.Timestamp(expect_from or ts.min())) \
            and (x.max() < pd.Timestamp(expect_to or (ts.max() + pd.Timedelta(seconds=1))))
        head = f"B{span} 窗口 {expect_rows} 行（pandas 重筛 {pandas_rows}）· 边界 {expect_from} ~ {expect_to}"
        check(f"{head} · 三点一致", ok_rows and in_window,
              f"接口 windowRows={r['windowRows']} 边界={w.get('from')}~{w.get('to')} 画了 {r['points']} 点")
        check(f"B{span} 接口边界与窗口标注同源", w.get("from") == expect_from and w.get("to") == expect_to,
              f"{w.get('label')}")
        check(f"B{span} rowCount 仍是整表 {HOURS}", r["rowCount"] == HOURS, r["rowCount"])
    bad, d = status_detail("GET", f"/api/ws/{ws}/series-multi?cols=power&span=3d")
    check("B99 旧的 3天/7天 档位已不存在（4xx 明确拒绝）", bad >= 400, f"{bad} {d[:60]}")

    # ---- B2 翻页：offset 是在「有数据的自然周期」清单里的第几期，左右切换下一周期 ----
    def page(span, off):
        r = api("GET", f"/api/ws/{ws}/series-multi?cols={numeric}&mode=raw&points=3000"
                       f"&span={span}&offset={off}")
        return r, r["window"]

    freq = {"day": "D", "week": "W", "month": "M", "year": "Y"}
    counts = {s: ts.dt.to_period(freq[s]).value_counts() for s in freq}
    for span in ("day", "week", "year"):
        _, w = page(span, 0)
        real = int(counts[span].size)
        check(f"B2 {span} 档可翻周期数 = pandas 数出的 {real} 期",
              w["periodTotal"] == real, f"接口 {w['periodTotal']} 期")
    # 月档逐期翻完：每期行数、边界、期号都要与 pandas 重筛一致，且加起来正好是整表（不漏不重）
    months = sorted(counts["month"].index)
    rows_sum, bad_detail = 0, ""
    for i, p in enumerate(months):
        r, w = page("month", i)
        expect = int(counts["month"][p])
        rows_sum += r["windowRows"]
        if not (r["windowRows"] == expect and w["periodIndex"] == i + 1
                and w["from"] == p.start_time.strftime("%Y-%m-%d %H:%M:%S")):
            bad_detail = f"第 {i + 1} 期：接口 {r['windowRows']} 行 / pandas {expect} 行 / 边界 {w['from']}"
    check(f"B2 month 逐期翻完 {len(months)} 期，行数与边界全对", not bad_detail,
          bad_detail or f"{len(months)} 期 · 每期 {months[0]}…{months[-1]}")
    check(f"B2 逐期行数加起来正好等于整表 {HOURS}（周期之间不漏不重）", rows_sum == HOURS, rows_sum)
    _, w = page("month", -5)
    check("B2 offset=-5 贴到第一期并说明请求值", w["offset"] == 0 and w["requested"] == -5 and w["clamped"],
          f"offset={w['offset']} requested={w['requested']}")
    last = len(months) - 1
    _, w = page("month", 9999)
    check(f"B2 offset=9999 贴到最后一期（第 {last + 1} 期）", w["offset"] == last and w["clamped"],
          f"offset={w['offset']}")
    _, w = page("all", 3)
    check("B2 all 档不分期（periodTotal=1、offset 恒 0）",
          w["periodTotal"] == 1 and w["offset"] == 0, f"{w['periodTotal']}/{w['offset']}")
    bad, d = status_detail("GET", f"/api/ws/{ws}/series-multi?cols=power&span=3d&offset=2")
    check("B2 翻页不救非法档位", bad >= 400, f"{bad} {d[:60]}")


# ============================================================ C 项1：划分列
def section_c(ws: str) -> None:
    print("\n== C 项1：第二步「生成划分列」写进真实一列 ==")
    for ratio in (49, 86):
        bad, d = status_detail("POST", f"/api/ws/{ws}/op/split", {"ratio": ratio})
        check(f"C0 ratio={ratio} 越界被拒", bad >= 400, f"{bad} {d[:60]}")
    r = op(ws, "split", {"ratio": 70})
    n = HOURS
    train, rest = n * 70 // 100, n - n * 70 // 100
    test, val = rest // 2, rest - rest // 2
    check("C1 三段计数 = 独立按公式算的数",
          (r["train"], r["val"], r["test"]) == (train, val, test),
          f"接口 {r['train']}/{r['val']}/{r['test']} vs 手算 {train}/{val}/{test}（共 {n} 行）")
    check("C2 列名与标记", r["key"] == "dataset_split" and r["label"] == "数据集划分", r["summary"])
    col = col_labels(ws).get("dataset_split") or {}
    check("C3 登记表里带着 feature=split", col.get("feature") == "split", f"type={col.get('type')}")
    df = frame(ws)
    vals = df["dataset_split"].tolist()
    check("C4 CSV 里逐行数出的三段与接口一致",
          vals.count("train") == train and vals.count("val") == val and vals.count("test") == test,
          f"{vals.count('train')}/{vals.count('val')}/{vals.count('test')}")
    check("C5 时序不打乱：前 train 全 train、尾 test 全 test、中间全 val",
          vals == ["train"] * train + ["val"] * val + ["test"] * test,
          f"首={vals[0]} 第 {train} 行={vals[train - 1]} 第 {train + 1} 行={vals[train]} 末={vals[-1]}")
    check("C6 划分列不是数值列（异常检测/滞后目标都碰不到它）", col.get("type") == "category", col.get("type"))
    bad, d = status_detail("POST", f"/api/ws/{ws}/op/feature-lag", {"cols": ["dataset_split"], "lags": [1], "group": "lag"})
    check("C7 拿划分列做滞后会被拒绝", bad >= 400, f"{bad} {d[:60]}")
    r2 = op(ws, "split", {"ratio": 60})
    n2 = HOURS
    t2 = n2 * 60 // 100
    rest2 = n2 - t2
    check("C8 改比例是覆盖同一列、不新增列",
          r2["replaced"] is True and r2["key"] == "dataset_split" and "dataset_split" in frame(ws).columns,
          f"列数 {r2['colCount']}")
    check("C9 ratio=60 的三段同样与手算一致", (r2["train"], r2["val"], r2["test"]) == (t2, rest2 - rest2 // 2, rest2 // 2),
          f"{r2['train']}/{r2['val']}/{r2['test']}")


# ============================================================ D 项5：sin/cos 与是否保留原列
def section_d(ws: str) -> None:
    print("\n== D 项5：时间特征「用不用 sin/cos」「要不要留数值原列」是两件事 ==")
    api("POST", f"/api/ws/{ws}/op/holidays", {"days": [], "source": "clear-for-D"})
    plain = op(ws, "feature-time", {"dims": ["hour", "day", "month", "weekday"], "cycDims": []})
    keys = set(plain["keys"])
    check("D1 完全不编码：只有 4 颗数值列，没有 _sin/_cos",
          keys == {"feat_hour", "feat_day", "feat_month", "feat_weekday"} and plain["cols"] == 4,
          plain["summary"])

    rep = op(ws, "feature-time", {"dims": ["hour", "day", "month", "weekday", "is_weekend"],
                                  "cycDims": ["hour", "weekday", "month"], "keepCycOriginal": False})
    got = set(rep["keys"])
    want = {"feat_day", "feat_is_weekend",
            "feat_hour_sin", "feat_hour_cos", "feat_weekday_sin", "feat_weekday_cos",
            "feat_month_sin", "feat_month_cos"}
    check("D2 编码后不留原列：被编码的三维只剩 sin/cos（8 列）", got == want and rep["cols"] == 8,
          f"实得 {sorted(got)}")
    check("D3 feat_hour / feat_weekday / feat_month 确实不在了",
          not ({"feat_hour", "feat_weekday", "feat_month"} & set(frame(ws).columns)),
          "整表列名里没有这三颗")

    keep = op(ws, "feature-time", {"dims": ["hour", "day", "month", "weekday"],
                                   "cycDims": ["hour", "month"], "keepCycOriginal": True})
    got_k = set(keep["keys"])
    want_k = {"feat_hour", "feat_day", "feat_month", "feat_weekday",
              "feat_hour_sin", "feat_hour_cos", "feat_month_sin", "feat_month_cos"}
    check("D4 保留原列：数值列与 sin/cos 并存（8 列）", got_k == want_k and keep["cols"] == 8, keep["summary"])

    legacy = op(ws, "feature-time", {"dims": ["hour", "day", "month", "weekday"], "cyclical": True})
    check("D5 旧命令形状（只有 cyclical=true）= 新形状全维编码且保留原列，列一颗不差",
          set(legacy["keys"]) == {"feat_hour", "feat_day", "feat_month", "feat_weekday",
                                  "feat_hour_sin", "feat_hour_cos",
                                  "feat_month_sin", "feat_month_cos",
                                  "feat_weekday_sin", "feat_weekday_cos"},
          f"{legacy['cols']} 列")

    df = frame(ws)
    ts = pd.to_datetime(df["timestamp"])
    sin_h = df["feat_hour_sin"].to_numpy(dtype="float64")
    direct = [round(float(math.sin(int(h) / 24 * 2 * math.pi)), 3) for h in ts.dt.hour]
    check("D6 feat_hour_sin 与按 24 小时周期手算的 sin 逐行一致",
          all(abs(a - b) < 1e-9 for a, b in zip(sin_h, direct)),
          f"前 6 行 {list(sin_h[:6])} vs {direct[:6]}")
    cos_m = df["feat_month_cos"].to_numpy(dtype="float64")
    direct_m = [round(float(math.cos((int(m) - 1) / 12 * 2 * math.pi)), 3) for m in ts.dt.month]
    check("D7 feat_month_cos 用 0 基月份（JS getMonth 口径）",
          all(abs(a - b) < 1e-9 for a, b in zip(cos_m, direct_m)),
          f"首月 {cos_m[0]} vs {direct_m[0]}")
    check("D8 sin/cos 都落在 [-1,1]", bool((sin_h >= -1.001).all() and (sin_h <= 1.001).all()),
          f"min={sin_h.min()} max={sin_h.max()}")
    for bad_body, tag in (
            ({"dims": ["hour"], "cycDims": ["day"], "keepCycOriginal": False}, "D9 day 无固定周期，拒绝编码"),
            ({"dims": [], "cycDims": []}, "D10 一个维度都不勾")):
        bad, d = status_detail("POST", f"/api/ws/{ws}/op/feature-time", bad_body)
        check(f"{tag} → 4xx", bad >= 400, f"{bad} {d[:70]}")


# ============================================================ E 项6：节假日表
def section_e(ws: str) -> None:
    print("\n== E 项6：节假日日历可配置、可显示，特征跟着配的这份算 ==")
    h = api("GET", f"/api/ws/{ws}/holidays")
    check("E1 清空之后 configured=true、0 天", h["configured"] is True and h["count"] == 0,
          f"{h['count']} 天 · source={h['source']}")
    r = op(ws, "feature-time", {"dims": ["holiday"], "cycDims": []})
    df = frame(ws)
    check("E2 空表时 feat_holiday 全 0", float(df["feat_holiday"].sum()) == 0.0, r["summary"])

    api("POST", f"/api/ws/{ws}/op/holidays", {"days": CUSTOM_HOLIDAYS, "source": "custom"})
    h2 = api("GET", f"/api/ws/{ws}/holidays")
    check("E3 配置 4 天后 GET 原样回吐", h2["count"] == 4 and h2["days"] == sorted(CUSTOM_HOLIDAYS)
          and h2["configured"] and h2["source"] == "custom",
          f"{h2['days']} · {h2['source']}")
    check("E4 按年分组给界面显示", [y["year"] for y in h2["byYear"]] == ["2024"]
          and len(h2["byYear"][0]["days"]) == 4, h2["byYear"])
    preset = next(p for p in h2["presets"] if str(p["year"]) == "2024")
    check("E5 预设一并给出（2024 年 19 天）", preset["count"] == 19, f"{preset['label']}")

    rt = op(ws, "feature-time", {"dims": ["holiday"], "cycDims": []})
    df2 = frame(ws)
    days2 = pd.to_datetime(df2["timestamp"]).dt.strftime("%Y-%m-%d")
    hand = int(days2.isin(set(CUSTOM_HOLIDAYS)).sum())
    check("E6 feat_holiday 命中数 = pandas 按配好的 4 天重数",
          float(df2["feat_holiday"].sum()) == float(hand), f"列里 {float(df2['feat_holiday'].sum()):.0f} · 手算 {hand}")
    check("E7 审计里记下这次真的用了哪份表",
          rt["holidays"]["used"] is True and sorted(rt["holidays"]["days"]) == sorted(CUSTOM_HOLIDAYS),
          rt["holidays"]["source"])

    ver = rt["version"]
    api("POST", f"/api/ws/{ws}/op/holidays", {"days": sorted(preset["days"]), "source": "preset-2024"})
    rp = op(ws, "feature-time", {"dims": ["holiday"], "cycDims": []})
    df3 = frame(ws)
    hand3 = int(pd.to_datetime(df3["timestamp"]).dt.strftime("%Y-%m-%d").isin(set(preset["days"])).sum())
    check("E8 换成 2024 预设，列里数出的命中行 = 预设天数 × 24（闰年 2024 全覆盖）",
          float(df3["feat_holiday"].sum()) == hand3 == 19 * 24,
          f"列 {float(df3['feat_holiday'].sum()):.0f} · 手算 {hand3} · 接口 {rp['cols']} 列")
    back = api("POST", f"/api/ws/{ws}/restore?limit=1", {"version": ver})
    df4 = frame(ws)
    check("E9 撤销回「4 天自定义」那一版，feat_holiday 又变回 96 行",
          float(df4["feat_holiday"].sum()) == 4 * 24 == 96,
          f"恢复到 v{ver}，列里 {float(df4['feat_holiday'].sum()):.0f}")
    for body, tag in (
            ({"days": ["2024-1-1"]}, "E10 日期格式不合 YYYY-MM-DD"),
            ({"days": ["2024-02-30"]}, "E11 不存在的日期"),
            ({"days": [f"2024-01-{i % 28 + 1:02d}" for i in range(201)]}, "E12 超过 200 天上限"),
    ):
        bad, d = status_detail("POST", f"/api/ws/{ws}/op/holidays", body)
        check(f"{tag} → 4xx", bad >= 400, f"{bad} {d[:70]}")
    dup = api("POST", f"/api/ws/{ws}/op/holidays", {"days": ["2024-05-01", "2024-05-01", "2024-05-01"], "source": "dup"})
    check("E13 重复日期去重成 1 天", dup["count"] == 1 and dup["days"] == ["2024-05-01"], dup["summary"])
    api("POST", f"/api/ws/{ws}/op/holidays", {"days": sorted(CUSTOM_HOLIDAYS), "source": "custom"})


# ============================================================ F 项4：四组分开生成
def section_f(ws: str) -> None:
    print("\n== F 项4：滞后 / 滑动窗口 / 差分 / 频域 各自成组，不再绑在一起 ==")
    lag = op(ws, "feature-lag", {"cols": ["power"], "lags": [1, 24], "windows": [], "group": "lag"})
    check("F1 滞后组只出 lag_*（2 列，featureType=lag）",
          lag["featureType"] == "lag" and set(lag["keys"]) == {"lag_power_t1", "lag_power_t24"},
          lag["summary"])
    win = op(ws, "feature-lag", {"cols": ["power"], "windows": [6, 12], "stats": ["mean", "std"],
                                 "expanding": True, "ewm": True, "ewmSpan": 8, "group": "window"})
    keys_w = set(win["keys"])
    check("F2 滑动窗口组只出 roll_*/expanding_*/ewm_*（6 列，featureType=window）",
          win["featureType"] == "window" and len(keys_w) == 6
          and all(not k.startswith("lag_") for k in keys_w),
          f"{sorted(keys_w)}")
    check("F3 两组列名互不重叠", not (keys_w & set(lag["keys"])), f"lag {len(lag['keys'])} + window {len(keys_w)}")
    dd = op(ws, "feature-diff", {"cols": ["power"], "d1": True, "d2": True, "seasonal": True,
                                 "period": 24, "group": "diff"})
    check("F4 差分组只出 diff_*（3 列，featureType=diff）",
          dd["featureType"] == "diff" and set(dd["keys"]) == {"diff1_power", "diff2_power", "diff_season24_power"},
          dd["summary"])
    ff = op(ws, "feature-diff", {"cols": ["power"], "d1": False, "d2": False, "seasonal": False,
                                 "fftDominant": True, "fftEntropy": True,
                                 "fftPowerRatio": True, "group": "fft"})
    keys_f = set(ff["keys"])
    check("F5 频域组只出频域列、没有 diff_*（featureType=fft）",
          ff["featureType"] == "fft" and all(not k.startswith("diff") for k in keys_f) and len(keys_f) >= 3,
          f"{sorted(keys_f)}")
    cases = (
        ("feature-lag", {"cols": ["power"], "lags": [1], "windows": [6], "stats": ["mean"], "group": "lag"}, "F6 滞后组夹带滚动窗口"),
        ("feature-lag", {"cols": ["power"], "windows": [], "stats": ["mean"], "group": "window"}, "F7 滑动窗口组没填窗口"),
        ("feature-lag", {"cols": ["power"], "windows": [6], "stats": ["mean"], "lags": [3], "group": "window"}, "F8 滑动窗口组夹带滞后"),
        ("feature-diff", {"cols": ["power"], "d1": True, "fftDominant": True, "group": "diff"}, "F9 差分组夹带频域"),
        ("feature-diff", {"cols": ["power"], "d1": True, "group": "fft"}, "F10 频域组夹带差分"),
    )
    for kind, body, tag in cases:
        bad, d = status_detail("POST", f"/api/ws/{ws}/op/{kind}", body)
        check(f"{tag} → 4xx", bad >= 400, f"{bad} {d[:70]}")
    lg = op(ws, "feature-lag", {"cols": ["temp"], "lags": [1], "windows": [3], "stats": ["max"], "expanding": False})
    check("F11 不带 group 的旧命令仍两类一起出（featureType=lag_roll）",
          lg["featureType"] == "lag_roll" and set(lg["keys"]) == {"lag_temp_t1", "roll_max_temp_w3"},
          lg["summary"])
    ld = op(ws, "feature-diff", {"cols": ["temp"], "d1": True, "fftEntropy": True})
    check("F12 旧差分命令仍差分和频域一起出（featureType=diff_freq）",
          ld["featureType"] == "diff_freq" and "diff1_temp" in ld["keys"], f"{len(ld['keys'])} 列")

    df = frame(ws)
    p = df["power"].astype("float64")
    check("F13 diff1_power 逐行 = 相邻两行之差（前一行缺失则不落地重算）",
          abs(float(df.loc[3, "diff1_power"]) - float(p.iloc[3] - p.iloc[2])) < 1e-6,
          f"{df.loc[3, 'diff1_power']} vs {p.iloc[3] - p.iloc[2]:.2f}")
    check("F14 lag_power_t24 第 30 行 = power 第 6 行",
          abs(float(df.loc[30, "lag_power_t24"]) - float(p.iloc[6])) < 1e-9,
          f"{df.loc[30, 'lag_power_t24']} / {p.iloc[6]}")
    check("F15 开头的滞后/差分留空而不是补 0",
          math.isnan(float(df.loc[0, "lag_power_t24"])) and math.isnan(float(df.loc[0, "diff1_power"])),
          f"lag[0]={df.loc[0, 'lag_power_t24']} diff[0]={df.loc[0, 'diff1_power']}")
    cols = col_labels(ws)
    fam = {k: cols[k]["feature"] for k in ["lag_power_t1", "roll_mean_power_w6", "diff1_power"] if k in cols}
    check("F16 登记表按组分别标记 feature",
          fam.get("lag_power_t1") == "lag" and fam.get("roll_mean_power_w6") == "window"
          and fam.get("diff1_power") == "diff", fam)


# ============================================================ G 可逆与重建
def section_g(ws: str) -> None:
    print("\n== G 可逆与重建：逐版撤销/重做 + 换进程按日志重开，列一颗不差 ==")
    meta = api("GET", f"/api/ws/{ws}")
    total = meta["version"]
    kinds = [o["kind"] for o in meta["ops"]]
    check("G0 三类新命令都进了日志并带中文标签",
          all(k in kinds for k in ("split_apply", "holidays", "feature_time"))
          and "feature_lag" in kinds and "feature_diff" in kinds,
          f"{total} 版 · {sorted(set(kinds))}")
    labels = {o["kind"]: o["label"] for o in meta["ops"]}
    check("G0b 日志标签按组分开写（滞后/滑动窗口/差分/频域各叫各的）",
          {"滞后特征", "滑动窗口特征", "差分特征", "频域特征"} <= {o["label"] for o in meta["ops"]},
          sorted({o["label"] for o in meta["ops"] if o["kind"] in ("feature_lag", "feature_diff")}))
    check("G0c 划分与节假日两条也是独立标签",
          labels.get("split_apply") == "生成数据集划分列" and labels.get("holidays") == "配置节假日表",
          {k: labels.get(k) for k in ("split_apply", "holidays")})

    snaps: dict[int, str] = {}
    for v in range(0, total + 1):            # 正向重做：每版留一份整表指纹
        api("POST", f"/api/ws/{ws}/restore?limit=1", {"version": v})
        snaps[v] = digest(ws)
    check("G1 逐版重做到最新版（每版指纹都取得到）", len(snaps) == total + 1, f"{total + 1} 个版本")
    bad_v = None
    for v in range(total, -1, -1):           # 反向撤销：同一版重放两次必须逐字节相同
        api("POST", f"/api/ws/{ws}/restore?limit=1", {"version": v})
        if digest(ws) != snaps[v]:
            bad_v = v
            break
    check("G2 逐版撤销回去，整表指纹与正向重做时一致", bad_v is None,
          f"全部 {total + 1} 版一致" if bad_v is None else f"v{bad_v} 不一致：{digest(ws)} != {snaps[bad_v]}")
    api("POST", f"/api/ws/{ws}/restore?limit=1", {"version": total})
    m = api("GET", f"/api/ws/{ws}")
    cols_before = [c["key"] for c in m["columns"]]
    check("G3 最新版本上有划分列与四组特征列",
          "dataset_split" in cols_before and "lag_power_t1" in cols_before
          and "feat_holiday" in cols_before, f"{len(cols_before)} 列")
    last = snaps[total]

    port = int(BASE.rsplit(":", 1)[1])
    kill_backend()
    spawn_backend(port)                      # 同一端口、同一份状态目录，只换进程
    listed = next((i for i in api("GET", "/api/ws")["items"] if i["wsId"] == ws), None)
    check("G4 磁盘上有这份日志、且此刻还没装载",
          listed is not None and listed["loaded"] is False and listed["version"] == total,
          {k: listed.get(k) for k in ("loaded", "version", "opsTotal")} if listed else None)
    m2 = api("GET", f"/api/ws/{ws}")
    check("G5 第一次访问就按日志重建：版本与列名一颗不差",
          m2["version"] == total and [c["key"] for c in m2["columns"]] == cols_before,
          f"v{m2['version']} · {len(m2['columns'])} 列")
    check("G6 重建后的整表指纹与重启前逐字节相同", digest(ws) == last, f"{last} → {digest(ws)}")
    h2 = api("GET", f"/api/ws/{ws}/holidays")
    check("G7 重启后生效的仍是那 4 天节假日（配置也在日志里）",
          h2["days"] == sorted(CUSTOM_HOLIDAYS) and h2["source"] == "custom",
          f"{h2['count']} 天 · {h2['source']}")
    df = frame(ws)
    check("G8 重启后 feat_holiday 依旧命中 96 行、划分列依旧 14400/4800/4800",
          float(df["feat_holiday"].sum()) == 96.0
          and df["dataset_split"].value_counts().to_dict() == {"train": 14400, "val": 4800, "test": 4800},
          f"holiday {float(df['feat_holiday'].sum()):.0f} · {df['dataset_split'].value_counts().to_dict()}")
    back = api("POST", f"/api/ws/{ws}/restore?limit=1", {"version": 0})
    cols0 = set(frame(ws).columns)
    check("G9 重启后撤销照旧可用（回到载入帧 v0：划分列与特征列全部退场）",
          back["meta"]["version"] == 0 and not ({"dataset_split", "feat_holiday", "lag_power_t1"} & cols0),
          f"v{back['meta']['version']} · {back['meta']['colCount']} 列 · {sorted(cols0)}")
    api("POST", f"/api/ws/{ws}/restore?limit=1", {"version": total})
    check("G10 重做到最新版，指纹与重启前仍然一致", digest(ws) == last, f"{digest(ws)} == {last}")
    api("DELETE", f"/api/ws/{ws}")


def main() -> int:
    t0 = time.time()
    print(f"状态目录：{STATE_DIR}\n数据集目录：{DATA_DIR}")
    for p in (STATE_DIR, DATA_DIR):
        if p.exists():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    f.unlink()
    spawn_backend(free_port())
    try:
        section_a()
        created = upload_csv(make_csv())
        ws = created["meta"]["wsId"]
        check("B0 造表：24000 行、时间列被识别",
              created["meta"]["rowCount"] == HOURS and created["meta"]["timeCol"] == "timestamp",
              f"{created['meta']['rowCount']} 行 · 时间列 {created['meta']['timeCol']} · {created['meta']['freqLabel']}")
        section_b(ws)
        section_c(ws)
        section_d(ws)
        section_e(ws)
        section_f(ws)
        section_g(ws)
    finally:
        kill_backend()
    print(f"\n{'=' * 62}\n{'全部通过' if not FAILURES else '失败项：' + ', '.join(FAILURES)}"
          f"（{len(FAILURES)} 项）· 用时 {time.time() - t0:.1f}s")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
