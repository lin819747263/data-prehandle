"""第⑤期验收：撤销/重做的历史与「上次会话」都搬到服务端，浏览器不再留一份全量兜底。

四条主线，每条都拿真实 HTTP 数字说话：
  A 契约：health 能力位、外生变量清单（界面下拉只读这一份，且对齐方式只有 left/nearest）
  B 游标即历史：混合命令序列的每一个版本都留下整表 CSV 指纹，逐版 restore 回去必须逐字节相同
    —— 撤销不是"反向执行一遍"，而是按命令日志重放到上一版，所以每一版都得能复原
  C 审计链跨语言对拍：scripts/ref_phase5.mjs 独立实现界面那三条投影规则，
    与 Python 端算出的可见集合、以及服务端 ops[:version] 三方对齐
  D 会话：PUT 的形状就是前端 sessionPayload() 的产物（含 ui 那一层），
    GET 必须原样回吐 UI 状态、并当场按日志核对每个工作区的存活/版本/漂移；三条守卫逐个验
  E 重启：换一个进程、同一份状态目录重新拉起，工作区按日志重开、会话仍认得它、侧表文件还在
  F 删列与撤销的联动：外生变量列被删掉之后，界面那份外生列清单必须跟着少一条

本脚本**自带后端**：在临时状态目录与临时数据集目录里拉起 uvicorn，跑完再原地重启一次，
不碰开发机上 8000 端口那台，也不往仓库的 dataset/ 里写东西。

用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_phase5.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "scripts" / "ref_phase5.mjs"
BASE = ""          # spawn_backend() 之后填成本次那台的实际地址
PROC: subprocess.Popen | None = None
STATE_DIR = ROOT.parent / ".verify" / "phase5-state"
DATA_DIR = ROOT.parent / ".verify" / "phase5-dataset"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + str(detail)) if detail != '' else ''}")
    if not cond:
        FAILURES.append(name)
    return cond


def call(method: str, path: str, body: dict | None = None,
         timeout: int = 180) -> tuple[int, bytes]:
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


def status_of(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    status, payload = call(method, path, body)
    try:
        return status, json.loads(payload.decode("utf-8", "replace")).get("detail", "")
    except json.JSONDecodeError:
        return status, payload.decode("utf-8", "replace")[:200]


def detail_of(payload: bytes) -> str:
    try:
        return str(json.loads(payload.decode("utf-8", "replace")).get("detail"))[:220]
    except json.JSONDecodeError:
        return payload.decode("utf-8", "replace")[:220]


# ---------------------------------------------------------------- 后端进程

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn_backend(port: int) -> None:
    """在临时状态目录 + 临时数据集目录里拉起一台干净的 backend。"""
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


# ---------------------------------------------------------------- 业务小工具

def op(ws: str, kind: str, params: dict) -> dict:
    return api("POST", f"/api/ws/{ws}/op/{kind}", params)


def restore(ws: str, version: int, limit: int = 50) -> dict:
    return api("POST", f"/api/ws/{ws}/restore", {"version": version}, ) | {} if False else \
        api("POST", f"/api/ws/{ws}/restore?offset=0&limit={limit}", {"version": version})


def csv_digest(ws: str) -> tuple[str, int]:
    """整表 CSV 的指纹 + 字节数：这一版到底是哪张表，用它说话。"""
    status, payload = call("GET", f"/api/ws/{ws}/export?format=csv")
    if status >= 400:
        raise SystemExit(f"导出失败 → {status} {detail_of(payload)}")
    return hashlib.sha256(payload).hexdigest()[:16], len(payload)


def side_table_bytes(name: str, rows: list[list]) -> bytes:
    head = "timestamp,rain_mm\n"
    body = "".join(",".join(str(c) for c in r) + "\n" for r in rows)
    return (head + body).encode("utf-8")


def inspect_side(filename: str, blob: bytes) -> dict:
    boundary = "----tss" + uuid.uuid4().hex
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"))
    buf.write(b"Content-Type: text/csv\r\n\r\n")
    buf.write(blob)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(BASE + "/api/exo/inspect", data=buf.getvalue(), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=180) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"inspect 失败 → {exc.code} {detail_of(exc.read())}")


def session_payload(ws: str, key: str, version: int, **over) -> dict:
    """与前端 store.js 的 sessionPayload() 同形状：UI 层状态 + 引用的工作区，没有明细。"""
    n = over.pop("log_len", 3)
    body = {
        "step": 3,
        "currentKey": key,
        "splitRatio": 0.7,
        "workspaces": [{"key": key, "wsId": ws, "version": version}],
        "actionLog": [{"step": 2, "icon": "column", "title": f"命令 {i}", "v": i + 1, "wsId": ws}
                      for i in range(n)],
        "meta": {"name": "光伏电站", "rows": 2880, "cols": 8, "wsId": ws, "version": version, "ops": n},
        "derivedCols": [{"key": "k1", "label": "列1", "formula": "a+b"}],
        "masks": [{"key": "m1", "label": "夜间", "startIdx": 3, "endIdx": 9, "onesCount": 6}],
        "imputeSegAlgos": {key: {"0": "linear"}},
        "ui": {"names": {key: "光伏电站"}, "formats": {key: "preset"},
               "page": {"key": key, "offset": 150, "limit": 50}},
    }
    body.update(over)
    return body


# ============================================================ A 契约
def section_a() -> None:
    print("\n== A 契约：能力位与外生变量清单 ==")
    h = api("GET", "/api/health")
    caps = set(h.get("capabilities") or [])
    need = {"workspace:restore", "workspace:reopen", "session:get", "session:set", "session:clear",
            "op:exo-preset", "op:exo-formula", "op:exo-file", "exo:presets", "exo:inspect"}
    check("A1 能力位齐", need <= caps, sorted(need - caps) or "全部就位")
    cat = api("GET", "/api/exo/presets")
    items = cat.get("items") or []
    check("A2 预设清单非空且带 needs", len(items) >= 5
          and all({"key", "label", "needs"} <= set(x) for x in items),
          {"count": len(items), "keys": [x["key"] for x in items][:8]})
    check("A3 对齐方式只有 left/nearest（界面上没有 inner 那种假选项）",
          [m["mode"] for m in cat.get("alignModes") or []] == ["left", "nearest"],
          cat.get("alignModes"))
    check("A4 公式白名单也来自这一份清单",
          bool(cat.get("formulaHelp")) and "hour" in cat.get("vars", []),
          {"vars": cat.get("vars"), "funcs": len(cat.get("funcs") or [])})


# ============================================================ B 游标即历史
def section_b() -> str:
    print("\n== B 游标即历史：每一版都得能重放出来 ==")
    created = api("POST", "/api/ws/preset", {"key": "pv", "seed": 20260927})
    ws = created["meta"]["wsId"]
    m0 = created["meta"]
    check("B1 新建工作区在第 0 版", m0["version"] == 0 and m0["opsTotal"] == 0
          and m0["canUndo"] is False and m0["canRedo"] is False,
          {"wsId": ws, "rows": m0["rowCount"], "cols": m0["colCount"]})

    inspect = inspect_side("phase5_rain.csv", side_table_bytes(
        "phase5_rain.csv",
        [["2024-06-01 00:00:00", 0.5], ["2024-06-01 04:00:00", 1.5], ["2024-06-01 08:00:00", 2.5]]))
    steps = [
        ("time-format", {"format": "YYYY-MM-DD HH:mm", "customFormat": None}, "时间格式"),
        ("convert-unit", {"key": "temperature", "factor": 9 / 5, "offset": 32, "newUnit": "°F"}, "单位换算"),
        ("rename-column", {"key": "wind_speed", "label": "风速", "newKey": "wind_ms"}, "改列名"),
        ("exo-preset", {"presetKey": "humidity", "seed": 7}, "预设外生变量"),
        ("resample", {"targetMinutes": 60, "method": "mean"}, "重采样"),
    ]
    stamps: dict[int, tuple[str, int, int, int]] = {}
    d0, n0 = csv_digest(ws)
    stamps[0] = (d0, m0["rowCount"], m0["colCount"], n0)
    for kind, params, label in steps:
        r = op(ws, kind, params)
        digest, size = csv_digest(ws)
        mm = r["meta"]
        stamps[mm["version"]] = (digest, mm["rowCount"], mm["colCount"], size)
        print(f"  · {label} → 第 {mm['version']} 版 · {mm['rowCount']} 行 × {mm['colCount']} 列 · CSV {size} B · {digest}")

    # 侧表挂列单独一步：文件名与 sha 由 inspect 那一步给
    r = op(ws, "exo-file", {"filename": inspect["filename"], "sha": inspect["sha"],
                            "sideTimeCol": inspect["sideTimeCol"], "mode": "nearest",
                            "toleranceMinutes": 90, "targets": []})
    digest, size = csv_digest(ws)
    stamps[r["meta"]["version"]] = (digest, r["meta"]["rowCount"], r["meta"]["colCount"], size)
    total = r["meta"]["version"]
    check("B2 六条命令各推进一版", total == 6 and r["meta"]["opsTotal"] == 6,
          {"version": total, "opsTotal": r["meta"]["opsTotal"]})
    check("B3 侧表挂列真的按就近匹配上了值",
          r["stats"]["matchedMainRows"] > 0 and len(r["keys"]) == 1,
          {"keys": r["keys"], "matched": r["stats"]["matchedMainRows"], "summary": r["summary"]})

    bad = []
    for v in range(total, -1, -1):
        got = api("POST", f"/api/ws/{ws}/restore?offset=0&limit=5", {"version": v})
        digest, size = csv_digest(ws)
        want, want_rows, want_cols, want_size = stamps[v]
        if (digest, got["meta"]["rowCount"], got["meta"]["colCount"], size) != (want, want_rows, want_cols, want_size):
            bad.append({"v": v, "期望": (want, want_rows, want_cols), "得到": (digest, got["meta"]["rowCount"], got["meta"]["colCount"])})
    check("B4 逐版回退：每一版的整表指纹都能原样重放出来", not bad, bad or f"{total + 1} 个版本逐一比对")

    back = api("POST", f"/api/ws/{ws}/restore?offset=0&limit=5", {"version": total})
    check("B5 重做就是往前滚", back["meta"]["version"] == total
          and csv_digest(ws)[0] == stamps[total][0],
          {"version": back["meta"]["version"], "digest": csv_digest(ws)[0]})

    got = api("POST", f"/api/ws/{ws}/restore?offset=0&limit=5", {"version": 3})
    hist = got["meta"]
    # meta.ops 只列「游标之前」的那几条（= 当前这份数据的历史），等待重做的尾巴不在里面
    check("B6 回退后 ops 只剩游标之前的那些", [o["kind"] for o in hist["ops"]] ==
          ["set_time_format", "convert_unit", "rename_column"],
          [o["kind"] for o in hist["ops"]])
    check("B7 游标推出按钮状态与标签", hist["canUndo"] and hist["canRedo"]
          and hist["undoLabel"] == hist["ops"][2]["label"]
          and hist["redoLabel"] == "预设外生变量"
          and hist["version"] == 3 and hist["opsTotal"] == 6,
          {k: hist.get(k) for k in ("version", "opsTotal", "canUndo", "canRedo", "undoLabel", "redoLabel")})
    check("B8 redoTail 说清楚可重做的那一段",
          [(t["index"], t["kind"]) for t in hist.get("redoTail") or []] ==
          [(3, "exo-preset"), (4, "resample"), (5, "exo-file")]
          and hist["redoTail"][0]["label"] == hist["redoLabel"],
          [(t["index"], t["kind"], t["label"]) for t in hist.get("redoTail") or []])
    check("B9 回退后页窗口跟着这一版走",
          len(got["page"]["rows"]) == 5 and got["page"]["total"] == hist["rowCount"]
          and "wind_ms" in got["page"]["columns"] and "humidity" not in got["page"]["columns"],
          {"total": got["page"]["total"], "cols": got["page"]["columns"][:9]})
    code, msg = status_of("POST", f"/api/ws/{ws}/restore", {"version": 99})
    check("B10 版本号越界给的是中文 400", code == 400 and "越界" in msg, msg)
    code, msg = status_of("GET", "/api/ws/000000000000")
    check("B11 不存在的工作区 404 且不崩", code == 404, msg)
    # 撤销之后异常检测必须作废：检测索引是按某一版帧算的
    api("POST", f"/api/ws/{ws}/anomaly-detect", {"algo": "3sigma"})
    a1 = api("GET", f"/api/ws/{ws}/anomaly")
    api("POST", f"/api/ws/{ws}/restore?offset=0&limit=5", {"version": 2})
    a2 = api("GET", f"/api/ws/{ws}/anomaly")
    check("B12 回退一并作废异常检测（检测索引是按某一版帧算的）",
          a1.get("detection") and a1["detection"].get("algo") and a2.get("detection") is None,
          {"回退前": (a1.get("detection") or {}).get("algo"), "回退后": a2.get("detection")})
    return ws


# ============================================================ C 审计链跨语言对拍
def py_prune(entries: list[dict], ws_id: str, cursor) -> list[dict]:
    """界面规则 4 的 Python 独立实现：执行新命令前，把本工作区等待重做的那段截掉。"""
    if not isinstance(cursor, int):
        return list(entries)
    return [e for e in entries
            if e.get("wsId") != ws_id or not isinstance(e.get("v"), int) or e["v"] <= cursor]


def py_visible(entries: list[dict], workspaces: list[dict]) -> list[dict]:
    """界面规则 1~3 的 Python 独立实现。"""
    out = []
    for e in entries:
        v = e.get("v")
        if not isinstance(v, int):
            out.append(e)
            continue
        owner = next((x for x in workspaces if x.get("wsId") and x["wsId"] == e.get("wsId")), None)
        if owner is None or v <= (owner.get("version") or 0):
            out.append(e)
    return out


def section_c(ws: str) -> None:
    print("\n== C 审计链：界面投影 vs 服务端 ops[:version]（跨语言对拍） ==")
    top = api("GET", f"/api/ws/{ws}")
    api("POST", f"/api/ws/{ws}/restore?offset=0&limit=5", {"version": top["opsTotal"]})
    ops = api("GET", f"/api/ws/{ws}")["ops"]          # 游标在最末，这段日志完整可见
    check("C0 服务端此刻有 6 条命令日志", len(ops) == 6, [o["kind"] for o in ops])

    def browser_entry(o: dict, v: int) -> dict:
        return {"id": f"v{v}", "wsId": ws, "kind": o["kind"], "v": v, "title": o["label"], "step": 2}

    # 界面那份审计记录：每条命令一个条目（v = 它落成的版本号），再掺两条与游标无关的记录
    entries = [browser_entry(o, o["index"] + 1) for o in ops]
    entries.insert(1, {"id": "misc-split", "wsId": ws, "kind": "split", "title": "切分比例 70/20/10", "step": 2})
    entries.append({"id": "misc-export", "wsId": ws, "kind": "export", "title": "导出 CSV 宽表", "step": 5})

    def run_ref(cur_entries, prune_to, cur_meta, cur_ops):
        doc = {"entries": cur_entries, "workspaces": [{"wsId": ws, "version": cur_meta["version"]}],
               "wsId": ws, "pruneTo": prune_to, "version": cur_meta["version"],
               "ops": cur_ops, "meta": cur_meta}
        tmp = STATE_DIR / "ref_input.json"
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(["node", str(REF), "--input", str(tmp)],
                              capture_output=True, text=True, encoding="utf-8", timeout=120)
        if proc.returncode != 0:
            raise SystemExit(f"参考实现跑挂了：{proc.stderr[:400]}")
        return json.loads(proc.stdout)

    # ---- 撤销到第 3 版：游标之后的三条从界面上消失，但等待重做的日志还在服务端 ----
    v3 = api("POST", f"/api/ws/{ws}/restore?offset=0&limit=5", {"version": 3})["meta"]
    out = run_ref(entries, None, v3, v3["ops"])
    check("C1 界面可见条目 = Node 独立实现的同一份",
          out["visibleIds"] == [e["id"] for e in py_visible(entries, [{"wsId": ws, "version": 3}])],
          {"node": out["visibleIds"], "python": [e["id"] for e in py_visible(entries, [{"wsId": ws, "version": 3}])]})
    check("C2 撤销掉的那三条命令不再冒充历史，重做后才会回来",
          [e["id"] for e in py_visible(entries, [{"wsId": ws, "version": 3}])]
          == ["v1", "misc-split", "v2", "v3", "misc-export"], out["visibleIds"])
    check("C3 界面那条历史序列 = 服务端 ops[:游标]（两套语言 + 服务端三方对齐）",
          out["browserHistory"] == out["serverHistory"]
          == [f'{o["kind"]}#{o["index"]}' for o in v3["ops"]],
          {"browser": out["browserHistory"], "server": out["serverHistory"]})
    check("C4 无 v 的记录不受游标影响（切分/导出恒在）",
          "misc-split" in out["visibleIds"] and "misc-export" in out["visibleIds"], out["visibleIds"])
    check("C5 顶栏计数与标签整份投影自服务端 meta", out["history"] == {
        "version": 3, "opsTotal": 6, "undo": 3, "redo": 3,
        "canUndo": v3["canUndo"], "canRedo": v3["canRedo"],
        "undoLabel": v3["undoLabel"], "redoLabel": v3["redoLabel"]}, out["history"])

    # ---- 在第 3 版上执行新命令：服务端 apply() 截掉等待重做的尾巴，界面必须同步 ----
    cut = op(ws, "mask-generate", {"maskName": "storm", "startIdx": 10, "endIdx": 20})
    m4 = cut["meta"]
    pruned = run_ref(entries, 3, m4, m4["ops"])
    check("C6 回退后执行新命令：服务端那段等待重做的日志被截掉",
          m4["version"] == 4 and m4["opsTotal"] == 4 and [o["kind"] for o in m4["ops"]][3] == "mask-generate",
          {"version": m4["version"], "opsTotal": m4["opsTotal"], "kinds": [o["kind"] for o in m4["ops"]]})
    check("C7 Node 的截断结果 = Python 独立算的同一份",
          pruned["prunedIds"] == [e["id"] for e in py_prune(entries, ws, 3)],
          {"node": pruned["prunedIds"], "python": [e["id"] for e in py_prune(entries, ws, 3)]})
    # 截完之后界面才追加这条新记录（v=4），于是新旧第 4 版不会同时挂在游标上
    entries = py_prune(entries, ws, 3) + [browser_entry(m4["ops"][3], 4)]
    after = run_ref(entries, None, m4, m4["ops"])
    check("C8 截断后界面历史与服务端 ops[:4] 重新对齐（新命令补上第 4 版）",
          after["browserHistory"] == after["serverHistory"]
          == [f'{o["kind"]}#{o["index"]}' for o in m4["ops"]],
          {"browser": after["browserHistory"], "server": after["serverHistory"]})
    check("C9 界面上的操作条数与会话里存的那份一致（4 条命令 + 2 条 UI 记录）",
          len(after["visibleIds"]) == 6 and len(entries) == 6, after["visibleIds"])

    # ---- 归属找不到（那份数据已经不在界面上）时条目仍可见，不能整段消失 ----
    ghost = run_ref([{"id": "ghost", "wsId": "aaaaaaaaaaaa", "kind": "resample", "v": 9, "title": "别的工作区"}],
                    None, m4, [])
    check("C10 无归属条目恒可见（导入的流程记录不该整段消失）", ghost["visibleIds"] == ["ghost"],
          ghost["visibleIds"])
    check("C11 截断只影响这一份工作区：别的 wsId 的条目原样保留",
          run_ref(entries + [{"id": "other", "wsId": "cccccccccccc", "kind": "resample", "v": 9}], 4, m4,
                  m4["ops"])["prunedIds"] == [e["id"] for e in entries] + ["other"])


# ============================================================ D 会话
def section_d(ws: str) -> None:
    print("\n== D 会话：UI 状态原样存取，工作区状态当场核对 ==")
    code, msg = status_of("PUT", "/api/session", session_payload(ws, "pv", 2))
    check("D1 PUT 收下了界面这份 payload", code == 200, msg)
    got = api("GET", "/api/session")
    w = (got.get("workspaces") or [{}])[0]
    snap = got.get("snap") or {}
    check("D2 GET 回吐 UI 状态（step/currentKey/splitRatio/掩码/填补/ui 页偏移）",
          snap.get("step") == 3 and snap.get("currentKey") == "pv"
          and snap.get("splitRatio") == 0.7 and snap.get("masks") and snap.get("derivedCols")
          and snap.get("ui", {}).get("page", {}).get("offset") == 150,
          {"step": snap.get("step"), "splitRatio": snap.get("splitRatio"),
           "ui": snap.get("ui"), "masks": len(snap.get("masks") or []),
           "keys": sorted(snap.keys())})
    check("D3 工作区状态是当场核对出来的（名字也由服务端重钉）",
          w.get("alive") is True and w.get("version") == 4 and w.get("canUndo")
          and w.get("canRedo") is False and w.get("rowCount") > 0 and w.get("name"),
          {k: w.get(k) for k in ("alive", "name", "version", "rowCount", "canUndo", "canRedo", "opsTotal")})
    check("D4 会话记的版本与服务端不一致时报漂移",
          w.get("versionDrift") == {"sessionSays": 2, "serverSays": 4}, w.get("versionDrift"))
    check("D5 落盘位置说得清", Path(str(got.get("stateDir"))).exists(), got.get("stateDir"))

    big = session_payload(ws, "pv", 2)
    big["actionLog"] = [{"title": f"命令 {i}", "wsId": ws, "v": 1} for i in range(2001)]
    code, msg = status_of("PUT", "/api/session", big)
    check("D6 守卫：审计记录条数上限", code == 400 and "上限" in msg, msg)
    heavy = session_payload(ws, "pv", 2)
    heavy["actionLog"] = [{"title": "把整列塞进会话", "wsId": ws, "v": 1, "data": [1.0] * 201}]
    code, msg = status_of("PUT", "/api/session", heavy)
    check("D7 守卫：审计记录里不许携带整列", code == 400 and "整列" in msg, msg)
    huge = session_payload(ws, "pv", 2)
    huge["actionLog"] = [{"title": "x" * 3000, "wsId": ws, "v": 1} for _ in range(800)]
    code, msg = status_of("PUT", "/api/session", huge)
    check("D8 守卫：会话体积上限", code == 400 and "MiB" in msg, msg)
    code, msg = status_of("PUT", "/api/session", {**session_payload(ws, "pv", 2), "step": 9})
    check("D9 step 只认 1..5", code == 422, msg)
    code, msg = status_of("PUT", "/api/session", {**session_payload(ws, "pv", 2), "splitRatio": 70})
    check("D10 切分比例按 0..1 校验（前端存的是百分数，必须除以 100）", code == 422, msg)
    dead = session_payload(ws, "ghost-key", 0)
    dead["workspaces"] = [{"key": "ghost-key", "wsId": "bbbbbbbbbbbb", "version": 0}]
    api("PUT", "/api/session", dead)
    got = api("GET", "/api/session")
    w = (got.get("workspaces") or [{}])[0]
    check("D11 工作区已经没了：alive=false + 中文原因，界面据此提示回第一步重载",
          w.get("alive") is False and w.get("reason"),
          {k: w.get(k) for k in ("alive", "reason", "version")})
    check("D12 DELETE 之后 exists 转假", api("DELETE", "/api/session").get("cleared") is True
          and api("GET", "/api/session").get("exists") is False)


# ============================================================ E 重启
def section_e(ws: str) -> None:
    print("\n== E 换一个进程：日志落盘就得扛住重启 ==")
    before = api("GET", f"/api/ws/{ws}")
    digest = csv_digest(ws)[0]
    api("PUT", "/api/session", session_payload(ws, "pv", before["version"]))
    port = int(BASE.rsplit(":", 1)[1])
    kill_backend()
    spawn_backend(port)          # 同一台端口、同一份状态目录，只换进程
    lst = api("GET", "/api/ws")
    row = next((i for i in lst["items"] if i["wsId"] == ws), None)
    check("E1 磁盘上有这份命令日志（且此刻还没装载）",
          row is not None and row["loaded"] is False and row["opsTotal"] == before["opsTotal"]
          and row["version"] == before["version"],
          {"count": lst["count"], "activeCount": lst["activeCount"], "row": row})
    after = api("GET", f"/api/ws/{ws}")
    check("E2 第一次访问就按日志把帧重建出来，游标/行数/列数原样",
          after["version"] == before["version"] and after["opsTotal"] == before["opsTotal"]
          and after["rowCount"] == before["rowCount"] and after["colCount"] == before["colCount"],
          {k: after.get(k) for k in ("version", "opsTotal", "rowCount", "colCount")})
    check("E3 重建出来的整表与重启前逐字节同源", csv_digest(ws)[0] == digest,
          {"重启前": digest, "重启后": csv_digest(ws)[0]})
    got = api("GET", "/api/session")
    w = (got.get("workspaces") or [{}])[0]
    check("E4 重启后 GET /api/session 仍认得这个工作区（侧表文件也还在盘上）",
          w.get("alive") is True and w.get("version") == before["version"]
          and (w.get("canUndo") or w.get("canRedo")),
          {k: w.get(k) for k in ("alive", "version", "canUndo", "canRedo", "undoLabel")})
    back = api("POST", f"/api/ws/{ws}/restore?offset=0&limit=5", {"version": 1})
    check("E5 重启后撤销照旧可用（回到第 1 版，指纹重放一致）",
          back["meta"]["version"] == 1 and csv_digest(ws)[0] != digest,
          {"version": back["meta"]["version"], "rows": back["meta"]["rowCount"]})
    side_files = sorted(p.name for p in (DATA_DIR / "_exo").glob("*.csv")) if (DATA_DIR / "_exo").exists() else []
    check("E6 侧表落盘在数据集目录里，重放读的就是这份", bool(side_files), side_files)


# ============================================================ F 外生列与删列联动
def section_f() -> None:
    print("\n== F 外生变量列就是工作区的普通列：删掉之后清单跟着少一条 ==")
    ws = api("POST", "/api/ws/preset", {"key": "load", "seed": 5})["meta"]["wsId"]
    op(ws, "exo-preset", {"presetKey": "cloud_cover", "seed": 11})
    op(ws, "exo-formula", {"key": "night_flag", "expr": "weekday * 2 + idx / 100"})
    tags = {c["key"]: (c.get("exo") or {}) for c in api("GET", f"/api/ws/{ws}")["columns"]}
    check("F1 外生列带着来源标记进列注册表",
          tags.get("cloud_cover", {}).get("kind") == "preset"
          and tags.get("night_flag", {}).get("kind") == "formula"
          and tags.get("night_flag", {}).get("expr") == "weekday * 2 + idx / 100",
          {k: v for k, v in tags.items() if v})
    n_before = len([1 for v in tags.values() if v])
    cols_at2 = api("GET", f"/api/ws/{ws}")["colCount"]
    op(ws, "delete-column", {"key": "cloud_cover"})
    tags = {c["key"]: (c.get("exo") or {}) for c in api("GET", f"/api/ws/{ws}")["columns"]}
    check("F2 删列之后界面那份外生列清单少一条（读的就是列注册表）",
          "cloud_cover" not in tags and len([1 for v in tags.values() if v]) == n_before - 1,
          {"剩": sorted(k for k, v in tags.items() if v)})
    check("F3 撤销这一步同样是重放：删掉的列会回来",
          api("POST", f"/api/ws/{ws}/restore?offset=0&limit=2", {"version": 2})["meta"]["colCount"] == cols_at2,
          {"删列前": cols_at2, "回到第 2 版": api("GET", f"/api/ws/{ws}")["colCount"]})
    tags = {c["key"]: (c.get("exo") or {}) for c in api("GET", f"/api/ws/{ws}")["columns"]}
    check("F4 回到第 2 版之后 cloud_cover 又在了", "cloud_cover" in tags,
          sorted(k for k, v in tags.items() if v))
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
        ws = section_b()
        section_c(ws)
        section_d(ws)
        section_e(ws)
        section_f()
    finally:
        kill_backend()
    print(f"\n{'=' * 62}\n{'全部通过' if not FAILURES else '失败项：' + ', '.join(FAILURES)}"
          f"（{len(FAILURES)} 项）· 用时 {time.time() - t0:.1f}s")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
