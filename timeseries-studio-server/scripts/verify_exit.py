# -*- coding: utf-8 -*-
"""第⑥轮验收：另存为数据集（A1）+ 撤销重放的帧缓存（C8）+ 失效粒度（C10）。

跑法：python scripts/verify_exit.py

脚本自己起一个 uvicorn（独立端口、独立 state / dataset 目录），不碰开发用的 8000 端口。
三件事各自要钉死的结论：
  B 段：另存进数据集目录的字节，与浏览器点「导出」拿到的字节是同一份（同一条编码路径）。
  C 段：撤销一步不再等于「把整条历史重跑一遍」，而且换指针换回来的帧与只执行前 v 条命令的工作区逐字节相等。
  D 段：只读请求不推进 valueEpoch；新增列 / 改配置的命令也不推进它——界面就是按这个数决定整表扫描要不要重来。
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

ROOT = Path(__file__).resolve().parents[1]
BASE = ""
PROC: subprocess.Popen | None = None
STATE_DIR = ROOT.parent / ".verify" / "exit-state"
DATA_DIR = ROOT.parent / ".verify" / "exit-dataset"
FAILURES: list[str] = []
NOTES: list[str] = []

HOURS = 24000
START = pd.Timestamp("2024-01-01 00:00:00")


# ---------------------------------------------------------------- 通用工具

def check(name: str, cond: bool, detail="") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + str(detail)) if detail != '' else ''}")
    if not cond:
        FAILURES.append(name)
    return cond


def note(text: str) -> None:
    NOTES.append(text)
    print(f"  [NOTE] {text}")


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


def sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()[:16]


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


# ---------------------------------------------------------------- 工作区助手

def new_preset(key: str = "load", seed: int = 7) -> str:
    return api("POST", "/api/ws/preset", {"key": key, "seed": seed})["meta"]["wsId"]


def make_csv() -> bytes:
    """每小时一行的大表：让重放的耗时真正落在数据量上，而不是落在 HTTP 往返上。"""
    out = io.StringIO()
    out.write("timestamp,power,temp\n")
    for i in range(HOURS):
        t = START + pd.Timedelta(hours=i)
        v = "" if i in (100, 101, 9000) else f"{400.0 + 120.0 * math.sin(i / 24 * 2 * math.pi) + (i % 7):.2f}"
        temp = f"{12.0 + 9.0 * math.sin((i % 24) / 24 * 2 * math.pi):.2f}"
        out.write(f"{t.strftime('%Y-%m-%d %H:%M:%S')},{v},{temp}\n")
    return out.getvalue().encode("utf-8")


def upload_csv(blob: bytes, filename: str = "exit.csv") -> str:
    """persist=true：来源文件落进数据集目录，工作区才可能在重启后按日志重建。"""
    boundary = "----tss" + uuid.uuid4().hex
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"))
    buf.write(b"Content-Type: text/csv\r\n\r\n")
    buf.write(blob)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(BASE + "/api/ws?persist=true", data=buf.getvalue(), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=240) as res:
        return json.loads(res.read().decode("utf-8"))["meta"]["wsId"]


def op(ws: str, kind: str, params: dict) -> dict:
    return api("POST", f"/api/ws/{ws}/op/{kind}", params)


def meta(ws: str) -> dict:
    return api("GET", f"/api/ws/{ws}")


def digest(ws: str, fmt: str = "csv") -> str:
    """当前帧的指纹：整表由后端编码，指纹就是这一版数据的身份。"""
    return sha(call("GET", f"/api/ws/{ws}/export?format={fmt}")[1])


def encoded_equal(fmt: str, a: bytes, b: bytes) -> bool:
    """两份编码结果是不是同一张表。xlsx 是 zip 容器、成员带时间戳，逐字节比没意义，
    只能读回来比数据；parquet / feather / csv 的编码是确定的，直接比字节。"""
    if fmt != "xlsx":
        return a == b
    import io

    import pandas as pd
    return (pd.read_excel(io.BytesIO(a)).equals(pd.read_excel(io.BytesIO(b))))


def restore(ws: str, version: int) -> dict:
    return api("POST", f"/api/ws/{ws}/restore", {"version": version})["meta"]


def masks(ws: str, count: int) -> list[dict]:
    """执行 count 条掩码命令：加列、不动既有数值，是最便宜的「能进日志的命令」。"""
    out = []
    for i in range(count):
        out.append(op(ws, "mask-generate", {"maskName": f"mk{i}", "startIdx": i, "endIdx": i + 2}))
    return out


# ---------------------------------------------------------------- A 契约

def section_a() -> None:
    print("\n== A. 出口与缓存的对外契约 ==")
    health = api("GET", "/api/health")
    caps = set(health.get("capabilities") or [])
    check("capabilities 声明 workspace:save-as", "workspace:save-as" in caps)
    check("capabilities 声明 undo:frame-cache", "undo:frame-cache" in caps)
    limits = health.get("limits") or {}
    check("limits 给出 snapshotVersions", isinstance(limits.get("snapshotVersions"), int)
          and limits["snapshotVersions"] >= 1, limits.get("snapshotVersions"))
    check("limits 给出 snapshotMaxBytes", isinstance(limits.get("snapshotMaxBytes"), int)
          and limits["snapshotMaxBytes"] > 0, limits.get("snapshotMaxBytes"))

    ws = new_preset()
    m = meta(ws)
    check("meta 透出整数值 valueEpoch", isinstance(m.get("valueEpoch"), int), m.get("valueEpoch"))
    fc = m.get("frameCache") or {}
    check("meta 透出 frameCache 四件套",
          {"versions", "bytes", "maxVersions", "maxBytes"} <= set(fc), fc)
    check("载入帧还没有 restoreTrace", m.get("restoreTrace") is None, m.get("restoreTrace"))
    q = api("GET", f"/api/ws/{ws}/quality")
    check("/quality 的 valueEpoch 与 meta 同源", q.get("valueEpoch") == m.get("valueEpoch"),
          f'{q.get("valueEpoch")} vs {m.get("valueEpoch")}')


# ---------------------------------------------------------------- B 另存为数据集

def section_b() -> None:
    print("\n== B. 另存为数据集（A1）==")
    ws = new_preset()
    op(ws, "rename-column", {"key": "price_tier", "label": "电价类型"})
    op(ws, "mask-generate", {"maskName": "mk_peak", "startIdx": 10, "endIdx": 30})
    m = meta(ws)
    version, rows, cols = m["version"], m["rowCount"], m["colCount"]

    exported = call("GET", f"/api/ws/{ws}/export?format=csv")[1]
    r = api("POST", f"/api/ws/{ws}/save-as?format=csv", {"filename": "exit_saved"})
    on_disk = (DATA_DIR / r["filename"]).read_bytes()
    check("落盘字节与浏览器导出字节完全一致", sha(on_disk) == sha(exported),
          f'{r["filename"]} · {sha(on_disk)} vs {sha(exported)}')
    check("报告里的 size / sizeOnDisk / 真实文件大小一致",
          r["size"] == r["sizeOnDisk"] == len(on_disk), f'{r["size"]} / {r["sizeOnDisk"]} / {len(on_disk)}')
    check("报出的版本号与行列数是当前帧的", (r["version"], r["rows"], r["cols"]) == (version, rows, cols),
          f'v{r["version"]} · {r["rows"]}行×{r["cols"]}列')
    listed = api("GET", "/api/datasets")
    entry = next((i for i in listed["items"] if i["filename"] == r["filename"]), None)
    check("第一步的目录清单里有这一份", entry is not None, r["filename"])
    check("清单里的字节数与落盘一致", entry is not None and entry["size"] == len(on_disk),
          entry and f'{entry["sizeText"]}')

    reopened = api("POST", "/api/ws/dataset", {"filename": r["filename"]})["meta"]
    check("另存的那份能当数据集重开", reopened["rowCount"] == rows and reopened["colCount"] == cols,
          f'{reopened["rowCount"]}行×{reopened["colCount"]}列')
    check("重开后的内容与存进去的那一版逐字节相等",
          digest(reopened["wsId"]) == sha(exported), f'{digest(reopened["wsId"])} vs {sha(exported)}')
    check("重开后带掩码列（明细真的写进去了）",
          "mk_peak" in [c["key"] for c in reopened["columns"]])

    again = api("POST", f"/api/ws/{ws}/save-as?format=csv", {"filename": "exit_saved"})
    check("同名另存会另起名字而不是覆盖", again["renamed"] and again["filename"] != r["filename"],
          f'{r["filename"]} → {again["filename"]}')
    check("原名那一份的内容没被动过",
          (DATA_DIR / r["filename"]).read_bytes() == on_disk)

    # 其余格式逐个按后端探测结果决定：编不出来的（本机通常缺 pyarrow）如实记下，不硬测
    health = api("GET", "/api/health")
    codecs = health.get("exportCodecs") or {}
    caps = set(health.get("capabilities") or [])
    for fmt, why in codecs.items():
        if fmt == "csv":
            continue
        if why:
            note(f"{fmt} 后端编不出来（{str(why)[:110]}）——能力清单里应没有 export:{fmt}")
            check(f"编不出的 {fmt} 不进能力清单", f"export:{fmt}" not in caps)
            st, detail = status_detail("POST", f"/api/ws/{ws}/save-as?format={fmt}", {"filename": "exit_saved"})
            check(f"{fmt} 另存被明确拒绝而不是 500", st == 422, f"{st} · {detail[:90]}")
            continue
        st, raw = call("POST", f"/api/ws/{ws}/save-as?format={fmt}", {"filename": f"exit_saved_{fmt}"})
        if st != 200:
            check(f"{fmt} 另存成功", False, f"{st} · {raw[:120].decode('utf-8', 'replace')}")
            continue
        got = json.loads(raw.decode("utf-8"))
        saved = (DATA_DIR / got["filename"]).read_bytes()
        streamed = call("GET", f"/api/ws/{ws}/export?format={fmt}")[1]
        check(f"{fmt} 也走同一条编码路径",
              encoded_equal(fmt, saved, streamed) and got["filename"].endswith(f".{fmt}"),
              f'{got["filename"]} · {got["sizeText"]}')
        (DATA_DIR / got["filename"]).unlink(missing_ok=True)

    st, detail = status_detail("POST", f"/api/ws/{ws}/save-as?format=csv", {"filename": ".."})
    check("纯 .. 这类文件名被拒（不是 500）", st == 400, f'{st} · {detail}')
    # 带 .. 的路径干脆整个拒掉（safe_name 把隐藏文件/穿越一起挡了），比「收进目录内」更严
    for bad in ("../../../../tmp/exit_escape", "a/../b", ".exit_hidden"):
        st, detail = status_detail("POST", f"/api/ws/{ws}/save-as?format=csv", {"filename": bad})
        check(f"穿越/隐藏文件名被拒（不是 500）· {bad}", st == 400, f"{st} · {detail[:60]}")
    check("拒掉的名字一个字节都没落盘",
          not list(DATA_DIR.glob("exit_escape*")) and not list(DATA_DIR.glob(".exit_hidden*"))
          and not (DATA_DIR.parent / "exit_escape.csv").exists()
          and not Path("/tmp/exit_escape.csv").exists())
    # 只留一份自定义名字的另存结果，避免下一次跑脚本被同名规则改名
    for p in DATA_DIR.glob("exit_*"):
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- C 撤销重放的帧缓存

def section_c() -> None:
    print("\n== C. 撤销重放的帧缓存（C8）==")
    ws = new_preset()
    base = digest(ws)
    masks(ws, 6)
    v6 = meta(ws)
    v6_digest = digest(ws)
    check("6 条命令后游标在第 6 版", v6["version"] == 6, v6["version"])
    m5 = restore(ws, 5)
    tr5 = m5["restoreTrace"]
    check("单步撤销命中帧缓存：重放 0 条命令", tr5["cacheHit"] and tr5["replayedOps"] == 0, tr5)
    check("撤销后确实回到第 5 版", m5["version"] == 5 and "mk5" not in [c["key"] for c in m5["columns"]])

    # 换指针换回来的那一版，必须与「只执行前 5 条命令」的另一份工作区分毫不差
    fresh = new_preset()
    masks(fresh, 5)
    check("缓存换回的帧与只执行前 5 条命令的工作区逐字节相等",
          digest(ws) == digest(fresh), f'{digest(ws)} vs {digest(fresh)}')

    m6 = restore(ws, 6)
    check("重做回去同样命中缓存", m6["restoreTrace"]["cacheHit"]
          and m6["restoreTrace"]["replayedOps"] == 0, m6["restoreTrace"])
    check("重做回来的内容与撤销前那一版相同", digest(ws) == v6_digest, digest(ws))

    # 回退后又执行新命令：游标之后那段日志被截掉，属于它的帧缓存也必须一起作废
    restore(ws, 4)
    op(ws, "mask-generate", {"maskName": "other", "startIdx": 1, "endIdx": 2})
    cur = meta(ws)
    check("回退后执行新命令会截掉重做分支", cur["version"] == 5 and cur["opsTotal"] == 5,
          f'v{cur["version"]} / 日志 {cur["opsTotal"]} 条')
    bad = status_detail("POST", f"/api/ws/{ws}/restore", {"version": 6})
    check("被截掉的那一版不再可重做到", bad[0] == 400, f'{bad[0]} · {bad[1]}')
    gone = restore(ws, 5)
    check("旧分支的版本不再留在缓存里", 6 not in gone["frameCache"]["versions"]
          and 5 in gone["frameCache"]["versions"], gone["frameCache"]["versions"])
    check("新分支的第 5 版带着新命令的列", "other" in [c["key"] for c in gone["columns"]]
          and "mk5" not in [c["key"] for c in gone["columns"]])

    # 冷启动长距离重放：缓存够不着目标时从载入帧整段跑，中途埋一颗检查点、到站前留尾部档
    cold = new_preset()
    cold_base = digest(cold)
    masks(cold, 12)
    tr0 = restore(cold, 0)["restoreTrace"]
    check("回到第 0 版就是换回载入帧，一条命令都不用重放",
          tr0["replayedOps"] == 0 and tr0["fromBase"] and not tr0["cacheHit"], tr0)
    check("第 0 版的内容就是最初载入的那一版", digest(cold) == cold_base,
          f'{digest(cold)} vs {cold_base}')

    fresh9 = new_preset()
    masks(fresh9, 9)
    t0 = time.perf_counter()
    tr9 = restore(cold, 9)["restoreTrace"]
    t_cold = time.perf_counter() - t0
    check("缓存只够到第 0 版时，退到第 9 版要从它起步整段重放 9 条",
          tr9["fromVersion"] == 0 and tr9["replayedOps"] == 9 and not tr9["cacheHit"], tr9)
    # 检查点埋在起步与目的地的中点：0 + 9 // 2 = 4
    check("长距离重放中途埋了一颗检查点", tr9["checkpointAt"] == 4, tr9["checkpointAt"])
    check("重放到的第 9 版与只执行前 9 条命令的另一份工作区一致",
          digest(cold) == digest(fresh9), f'{digest(cold)} vs {digest(fresh9)}')

    t1 = time.perf_counter()
    tr8 = restore(cold, 8)["restoreTrace"]
    t_step = time.perf_counter() - t1
    check("再退一步命中到站前的尾部留档，不用重新整段放",
          tr8["cacheHit"] and tr8["replayedOps"] == 0, tr8)
    t2 = time.perf_counter()
    tr12 = restore(cold, 12)["restoreTrace"]
    t_walk = time.perf_counter() - t2
    check("第二次往前走从缓存里最近的一版起步，比重放整段便宜",
          0 < tr12["replayedOps"] < tr9["replayedOps"], tr12)
    note(f"2880 行工作表：整段重放 9 条 = {t_cold * 1000:.0f} ms · "
         f"命中尾部留档退一步 = {t_step * 1000:.0f} ms · "
         f"从第 8 版走到第 12 版 = {t_walk * 1000:.0f} ms")

    # 大表上的真实收益：逐条撤销（日常动作）应该是 O(1)
    big = upload_csv(make_csv())
    masks(big, 10)
    mbig = meta(big)
    check("大表已载入", mbig["rowCount"] == HOURS, mbig["rowCount"])
    t3 = time.perf_counter()
    steps = [restore(big, v) for v in range(mbig["version"] - 1, mbig["version"] - 6, -1)]
    t_hot = time.perf_counter() - t3
    hits = sum(1 for s in steps if s["restoreTrace"]["cacheHit"])
    # 只留最近 3 版：连退 5 步必然有步越出窗口，越出去的那一步才整段重放
    check("连续单步撤销：落在三版窗口里的都命中", hits >= 3, f"{hits}/5 命中")
    check("命中缓存的那些步一条命令都没重放",
          all(s["restoreTrace"]["replayedOps"] == 0
              for s in steps if s["restoreTrace"]["cacheHit"]))
    worst = max(s["restoreTrace"]["replayedOps"] for s in steps)
    note(f"24000 行 × 15 列工作表连退 5 步 = {t_hot * 1000:.0f} ms · "
         f"{hits}/5 命中 · 越出窗口的最坏一步重放 {worst} 条")
    t4 = time.perf_counter()
    restore(big, 0)
    t_walk0 = time.perf_counter() - t4
    check("整段回放到第 0 版依然是秒级以内", t_walk0 < 3.0, f"{t_walk0 * 1000:.0f} ms")

    # 缓存不能变成第二份内存压力
    for ws_id in (cold, big):
        fc = meta(ws_id)["frameCache"]
        check(f"工作区 {ws_id[:8]} 的缓存不越界",
              len(fc["versions"]) <= fc["maxVersions"] and fc["bytes"] <= fc["maxBytes"],
              f'{fc["versions"]} · {fc["bytes"] / 1024 / 1024:.1f} MB / 上限 {fc["maxBytes"] // 1024 // 1024} MB')


# ---------------------------------------------------------------- D 失效粒度

def section_d() -> None:
    print("\n== D. 失效粒度 valueEpoch（C10）==")
    # 用 pv 这一份：里面有故意埋下的缺失段，填补之后整表缺失数真的会动，数字才可对照
    ws = new_preset("pv", 7)
    m0 = meta(ws)
    float_col = next(c["key"] for c in m0["columns"] if c["type"] == "float")
    readonly = [
        f"/api/ws/{ws}", f"/api/ws/{ws}/rows?offset=0&limit=50",
        f"/api/ws/{ws}/columns?keys={float_col}&max_rows=20",
        f"/api/ws/{ws}/overview", f"/api/ws/{ws}/quality", f"/api/ws/{ws}/stats",
        f"/api/ws/{ws}/hist?col={float_col}&bins=10",
        f"/api/ws/{ws}/series-multi?cols={float_col}&mode=extremes&points=300",
        f"/api/ws/{ws}/holidays", f"/api/ws/{ws}/first-complete?cols={float_col}",
        f"/api/ws/{ws}/export?format=csv",
    ]
    for path in readonly:
        status, _ = call("GET", path)
        if status >= 400:
            note(f"只读接口 {path} 返回 {status}（不计入失效判定）")
    m1 = meta(ws)
    check(f"{len(readonly)} 个只读请求都不推进 valueEpoch / version",
          m1["valueEpoch"] == m0["valueEpoch"] and m1["version"] == m0["version"],
          f'valueEpoch {m0["valueEpoch"]}→{m1["valueEpoch"]} · version {m0["version"]}→{m1["version"]}')

    # 新增列 / 只改配置的命令：version 前进，valueEpoch 不动（界面就是按这个数决定整表扫描要不要重来）
    op(ws, "mask-generate", {"maskName": "mk_a", "startIdx": 0, "endIdx": 5})
    op(ws, "holidays", {"days": ["2024-01-01", "2024-05-01"], "source": "custom"})
    op(ws, "feature-time", {"dims": ["hour", "weekday"], "cyclical": True, "cycDims": ["hour"]})
    op(ws, "feature-lag", {"cols": [float_col], "lags": [1, 2], "windows": [], "stats": [],
                           "expanding": False, "ewm": False, "group": "lag"})
    # newKey 与 key 相同 = 只换显示名（不带 newKey 时后端会按 label 另起列键，那是另一种改法）
    op(ws, "rename-column", {"key": "mk_a", "newKey": "mk_a", "label": "掩码A"})
    m2 = meta(ws)
    check("加列 / 改配置 / 生成特征 / 改名都不推进 valueEpoch",
          m2["version"] == m1["version"] + 5 and m2["valueEpoch"] == m1["valueEpoch"],
          f'version {m1["version"]}→{m2["version"]} · valueEpoch {m1["valueEpoch"]}→{m2["valueEpoch"]}')
    q2 = api("GET", f"/api/ws/{ws}/quality")
    check("这五类命令之后 /quality 的 valueEpoch 仍然是同一代",
          q2["valueEpoch"] == m2["valueEpoch"], f'{q2["valueEpoch"]} vs {m2["valueEpoch"]}')
    feature_keys = {c["key"] for c in m2["columns"] if c.get("feature")}
    checked_keys = {c["key"] for c in q2["columns"]}
    check("特征列确实进了表，且 /quality 的口径里没有它们",
          len(feature_keys) >= 2 and not (feature_keys & checked_keys),
          f'特征 {len(feature_keys)} 列 / 表内 {m2["colCount"]} 列 / 诊断口径 {len(checked_keys)} 列')
    mk = next((c for c in q2["columns"] if c["key"] == "mk_a"), None)
    check("掩码列在诊断口径里（它不是特征列）", mk is not None,
          [c["key"] for c in q2["columns"]])
    check("改名只换了显示名，列键没动", mk is not None and mk["label"] == "掩码A",
          mk and mk["label"])

    digest2 = digest(ws)
    q_before = api("GET", f"/api/ws/{ws}/quality")
    op(ws, "impute", {"all": True, "keys": [float_col], "defaultAlgo": "linear"})
    m3 = meta(ws)
    q_after = api("GET", f"/api/ws/{ws}/quality")
    check("改动既有数值的清洗会推进 valueEpoch", m3["valueEpoch"] > m2["valueEpoch"],
          f'{m2["valueEpoch"]}→{m3["valueEpoch"]}')
    check("填补之后整表缺失数真的变小（这一代数值确实变了）",
          q_after["totalMissingCells"] < q_before["totalMissingCells"],
          f'{q_before["totalMissingCells"]}→{q_after["totalMissingCells"]}')
    check("填补之后的帧与填补前不是同一份", digest(ws) != digest2)

    # 撤销跨过数值代时缓存必须作废：回到填补之前那一版，缺失数与指纹都跟着退回去
    back = restore(ws, m2["version"])
    check("撤销回填补前那一版，valueEpoch 一起退回",
          back["valueEpoch"] == m2["valueEpoch"], f'{m3["valueEpoch"]}→{back["valueEpoch"]}')
    qb = api("GET", f"/api/ws/{ws}/quality")
    check("退回后的缺失数与帧指纹都回到填补前",
          qb["totalMissingCells"] == q_before["totalMissingCells"] and digest(ws) == digest2,
          f'{qb["totalMissingCells"]} vs {q_before["totalMissingCells"]}')


# ---------------------------------------------------------------- E 重建单飞

def section_e() -> None:
    print("\n== E. 被淘汰的帧：同一颗只重建一次，不同颗不互相排队 ==")
    # 这一段测的是**进程内的注册表**，不走 HTTP：界面进第②步会同时打 /meta、/rows、/quality，
    # 三个线程各自「解析原始文件 + 重放整段日志」跑一遍的话，同一颗帧短时间内存在三份，
    # 后注册的那份把前两份顶掉——手里还握着旧帧的请求，回给界面的就是另一版数字。
    # 放在这里测而不是开 HTTP，是因为「重建发生了几次」这件事在 HTTP 那头根本看不见。
    sys.path.insert(0, str(ROOT))
    import threading as th
    import app.services.workspace as wsvc

    ids = ["aaaa00000001", "bbbb00000002"]   # 必须过 state_store.is_workspace_id 那条十六进制闸门
    calls: list[str] = []
    broken: list[str] = []
    pair = th.Barrier(2)          # 两颗不同的帧应当同时在重建
    gate = th.Event()

    def fake_reopen(ws_id: str):
        calls.append(ws_id)
        try:
            pair.wait(3)          # 全局锁的话这里会超时（另一颗进不来）
        except th.BrokenBarrierError:
            broken.append(ws_id)
        gate.wait(10)             # 模拟一次昂贵重建：解析 + 逐条重放
        df = pd.DataFrame({"v": [1.0, 2.0, 3.0]})
        meta = {"columns": [{"key": "v", "label": "v", "type": "float"}], "timeCol": None}
        return wsvc._register(df, meta, {"kind": "unit-test"}, ws_id=ws_id, persist=False)

    orig = wsvc.reopen
    wsvc.reopen = fake_reopen
    results: list[str] = []
    try:
        threads = [th.Thread(target=lambda i=i: results.append(wsvc.get(i).id)) for i in ids * 5]
        for t in threads:
            t.start()
        time.sleep(0.6)     # 让同 ID 的其余四个都排到重建锁上，再放行那两次真重建
        gate.set()
        for t in threads:
            t.join(20)
        hold = [t for t in threads if t.is_alive()]
        check("E0 十个线程全部返回（没有谁卡在锁上）", not hold, f"仍存活 {len(hold)}")
    finally:
        gate.set()
        wsvc.reopen = orig
        for i in ids:
            wsvc._REGISTRY.pop(i, None)
            wsvc._ACCESS.pop(i, None)
    check("E1 同一颗帧被 5 个并发请求挤中：只重建一次（双检锁生效）",
          calls.count(ids[0]) == 1 and calls.count(ids[1]) == 1,
          {"重建记录": calls})
    check("E2 两颗不同帧的重建是同时进行的（锁按 wsId 分，不是一把全局）", not broken,
          {"被卡住的": broken, "重建记录": calls})
    check("E3 每个线程拿到的都是注册表里那一份", sorted(results) == sorted(ids * 5),
          {"得到": sorted(set(results)), "条数": len(results)})


def main() -> int:
    for d in (STATE_DIR, DATA_DIR):
        if d.exists():
            for p in sorted(d.rglob("*"), reverse=True):
                try:
                    p.unlink() if p.is_file() else p.rmdir()
                except OSError:
                    pass
    sys.path.insert(0, str(ROOT))
    os.environ["TSS_STATE_DIR"] = str(STATE_DIR)
    os.environ["TSS_DATASET_DIR"] = str(DATA_DIR)
    port = free_port()
    spawn_backend(port)
    t0 = time.time()
    try:
        section_a()
        section_b()
        section_c()
        section_d()
        section_e()
    finally:
        kill_backend()
    elapsed = time.time() - t0
    print(f"\n{'全部通过' if not FAILURES else '失败项：' + '、'.join(FAILURES) + f'（{len(FAILURES)} 项）'}"
          f" · 用时 {elapsed:.1f} s")
    if NOTES:
        print("· " + "\n· ".join(NOTES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
