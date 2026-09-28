# -*- coding: utf-8 -*-
"""第一步多文件合并导入的验收：拼表与按时间排序全在后端，浏览器只拿回执。

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_merge.py

脚本自己起一个 uvicorn（独立端口、独立 state / dataset 目录），不碰开发用的 8000 端口。
要钉死的结论：
  A 段：合并 = 行数相加（一行不丢）+ 列取并集 + 稳定升序，回执里每个数字都能独立复算。
  B 段：坏输入一律 400 并给出中文原因（单份、时间列名不一致、超份数、空文件）。
  C 段：合并帧重启后按「数据集目录里的这几份原始文件」重建，回执与页窗口逐字节一致。
"""
from __future__ import annotations

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

ROOT = Path(__file__).resolve().parents[1]
BASE = ""
PROC: subprocess.Popen | None = None
PORT = 0
STATE_DIR = ROOT.parent / ".verify" / "merge-state"
DATA_DIR = ROOT.parent / ".verify" / "merge-dataset"
FAILURES: list[str] = []
NOTES: list[str] = []


def check(name: str, cond: bool, detail="") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + str(detail)) if detail != '' else ''}")
    if not cond:
        FAILURES.append(name)
    return cond


def note(text: str) -> None:
    NOTES.append(text)
    print(f"  [NOTE] {text}")


def call(method: str, path: str, body: dict | None = None,
         raw: bytes | None = None, ctype: str | None = None,
         timeout: int = 240) -> tuple[int, bytes]:
    data = raw if raw is not None else (
        json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype or "application/json")
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


def status_detail(method: str, path: str, body: dict | None = None,
                  raw: bytes | None = None, ctype: str | None = None) -> tuple[int, str]:
    status, payload = call(method, path, body, raw=raw, ctype=ctype)
    try:
        return status, str(json.loads(payload.decode("utf-8", "replace")).get("detail"))[:300]
    except json.JSONDecodeError:
        return status, payload.decode("utf-8", "replace")[:300]


def multipart(fields: list[tuple[str, str | bytes]]) -> tuple[bytes, str]:
    """一次请求带多份文件：字段名统一用 files，与 FastAPI 的 list[UploadFile] 对应。"""
    boundary = "----tss" + uuid.uuid4().hex
    buf = io.BytesIO()
    for name, content in fields:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'.encode())
        buf.write(b"Content-Type: text/csv\r\n\r\n")
        buf.write(content.encode("utf-8") if isinstance(content, str) else content)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def merge_uploads(files: list[tuple[str, str]], persist: bool = True,
                  extra: str = "") -> dict:
    body, ctype = multipart(files)
    path = f"/api/ws/merge?persist={'true' if persist else 'false'}&limit=50{extra}"
    status, payload = call("POST", path, raw=body, ctype=ctype)
    text = payload.decode("utf-8", "replace")
    if status >= 400:
        raise SystemExit(f"合并请求失败 → {status} {text[:400]}")
    return json.loads(text)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn_backend(port: int) -> None:
    global BASE, PROC, PORT
    PORT = port
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


# ---------------------------------------------------------------- 语料
#
# 三份故意打乱、列不齐、含重复时刻与坏时间的表：每个"坑"都对应回执里的一个数字。

P1 = (
    "timestamp,power\r\n"
    "2024-01-01 06:00:00,10\r\n"
    "2024-01-01 07:00:00,11\r\n"
    "2024-01-01 08:00:00,12\r\n"
)
P2 = (
    "timestamp,power,temp\r\n"
    "2024-01-01 05:00:00,20,5\r\n"
    "2024-01-01 07:30:00,21,6\r\n"
    "2024-01-01 09:00:00,22,7\r\n"
)
P3 = (
    "timestamp,power\r\n"
    "2024-01-01 07:00:00,30\r\n"
    "not-a-time,31\r\n"
    "2024-01-01 04:00:00,32\r\n"
)
# 与独立复算对照用的期望顺序（稳定升序 + 空时间排最后）
EXPECT_ORDER = [
    ("2024-01-01 04:00:00", 32.0),
    ("2024-01-01 05:00:00", 20.0),
    ("2024-01-01 06:00:00", 10.0),
    ("2024-01-01 07:00:00", 11.0),   # P1 在 P3 之前 → 稳定排序里它先来
    ("2024-01-01 07:00:00", 30.0),
    ("2024-01-01 07:30:00", 21.0),
    ("2024-01-01 08:00:00", 12.0),
    ("2024-01-01 09:00:00", 22.0),
    (None, 31.0),
]
EXPECT_POWER = [p for _, p in EXPECT_ORDER]


def page_pairs(resp: dict) -> list[tuple]:
    keys = resp["page"]["columns"]
    ti, pi = keys.index("timestamp"), keys.index("power")
    return [(r[ti], float(r[pi])) for r in resp["page"]["rows"]]


# ---------------------------------------------------------------- A 段：合并语义

def section_a() -> dict:
    print("\n=== A. 合并语义：行数相加、列取并集、稳定升序 ===")
    resp = merge_uploads([("shift_a.csv", P1), ("shift_b.csv", P2), ("shift_c.csv", P3)])
    meta, rcpt = resp["meta"], resp["meta"]["merge"]
    check("A1 三份各 3 行 → 合并 9 行（一行不丢）",
          rcpt["totalRows"] == 9 and meta["rowCount"] == 9,
          f"totalRows={rcpt['totalRows']} rowCount={meta['rowCount']}")
    check("A2 回执逐文件行数如实",
          [(f["filename"], f["rows"]) for f in rcpt["files"]] ==
          [("shift_a.csv", 3), ("shift_b.csv", 3), ("shift_c.csv", 3)],
          str([(f["filename"], f["rows"]) for f in rcpt["files"]]))
    check("A3 列取并集：timestamp/power/temp 三列",
          [c["key"] for c in meta["columns"]] == ["timestamp", "power", "temp"],
          str([c["key"] for c in meta["columns"]]))
    check("A4 union-only 列 = temp（只有 shift_b 带）", rcpt["unionOnlyCols"] == ["temp"], str(rcpt["unionOnlyCols"]))
    check("A5 因并集必然为空的格子 = 6（另外两份各 3 行 × 1 列）", rcpt["gapCells"] == 6, rcpt["gapCells"])
    temp_i = resp["page"]["columns"].index("temp")
    check("A6 temp 列只在 P2 的三行上有值（其余 6 格为空）",
          sum(1 for r in resp["page"]["rows"] if r[temp_i] is None) == 6,
          f"{sum(1 for r in resp['page']['rows'] if r[temp_i] is None)} 格为空")
    got = page_pairs(resp)
    check("A7 页窗口按时间稳定升序（含空时间排在最后）",
          [t for t, _ in got] == [t for t, _ in EXPECT_ORDER] and
          [p for _, p in got] == EXPECT_POWER,
          str([(t, p) for t, p in got]))
    check("A8 排序前确有 3 处时间回跳，排序后归零",
          rcpt["backjumpsBefore"] == 3 and rcpt["backjumpsAfter"] == 0,
          f"{rcpt['backjumpsBefore']} → {rcpt['backjumpsAfter']}")
    # rowsMoved 独立复算：拼表顺序里每行最终有没有挪窝
    concat = []
    for text in (P1, P2, P3):
        for line in text.split("\r\n")[1:]:
            if line.strip():
                t, v = line.split(",")[0], float(line.split(",")[1])
                concat.append((t, v))
    keyed = [(t if t != "not-a-time" else "\uffff", i, v) for i, (t, v) in enumerate(concat)]
    ordered = sorted(range(len(keyed)), key=lambda i: keyed[i])
    expected_moved = sum(1 for new, old in enumerate(ordered) if new != old)
    check("A9 独立复算的稳定排序位移数 = 回执 rowsMoved",
          rcpt["rowsMoved"] == expected_moved,
          f"回执 {rcpt['rowsMoved']} / 复算 {expected_moved}")
    check("A10 重复时刻不丢行、只如实计数（07:00 两次 → 1）",
          rcpt["duplicateTimes"] == 1 and rcpt["totalRows"] == 9,
          f"duplicateTimes={rcpt['duplicateTimes']}")
    check("A11 坏时间解析成空值并计入 emptyTimes=1", rcpt["emptyTimes"] == 1, rcpt["emptyTimes"])
    check("A12 排序后时间回跳为 0，说明空值没混进中间", rcpt["backjumpsAfter"] == 0)
    check("A13 时间跨度取合并后的两端",
          rcpt["timeRange"] == {"start": "2024-01-01 04:00:00", "end": "2024-01-01 09:00:00"},
          str(rcpt["timeRange"]))
    check("A14 来源记下 kind=merge 与三份落盘文件名",
          meta["source"]["kind"] == "merge" and len(meta["source"]["filenames"]) == 3,
          str(meta["source"]))
    check("A15 工作区名如实标注「等 3 份合并」",
          meta["name"] == "shift_a 等 3 份合并", meta["name"])
    check("A16 三份额外落盘文件各自独立存在于数据集目录",
          len(set(resp["persisted"])) == 3, str(resp["persisted"]))
    note(f"合并回执：{rcpt['fileCount']} 份 / {rcpt['totalRows']} 行 / {rcpt['colCount']} 列 · "
         f"位移 {rcpt['rowsMoved']} 行 · 并集空格 {rcpt['gapCells']} 格")
    return resp


def section_a2() -> None:
    print("\n=== A+. 同名文件同秒落盘不得互相覆盖（合并重建的前提）===")
    resp = merge_uploads([("same.csv", P1), ("same.csv", P2)])
    names = resp["persisted"]
    check("A17 两份同名文件写成两个不同文件名", len(set(names)) == 2, str(names))
    check("A18 合并行数与两份之和一致", resp["meta"]["rowCount"] == 6, resp["meta"]["rowCount"])
    note(f"同名落盘：{names[0]} / {names[1]}")


# ---------------------------------------------------------------- B 段：坏输入

def section_b() -> None:
    print("\n=== B. 坏输入：一律 4xx + 中文原因，绝不静默改数据 ===")
    body, ctype = multipart([("only.csv", P1)])
    st, msg = status_detail("POST", "/api/ws/merge?persist=false", raw=body, ctype=ctype)
    check("B1 只给一份文件 → 400 并指路单文件接口", st == 400 and "至少需要两份" in msg, f"{st} · {msg}")

    body, ctype = multipart([("a.csv", P1), ("b.csv", P2.replace("timestamp", "ts"))])
    st, msg = status_detail("POST", "/api/ws/merge?persist=false", raw=body, ctype=ctype)
    check("B2 时间列名不一致 → 400 且逐文件点名",
          st == 400 and "时间列名不一致" in msg and "b.csv" in msg, f"{st} · {msg}")

    body, ctype = multipart([("x.csv", P1), ("y.csv", "power,note\r\n1,k\r\n2,l\r\n")])
    st, msg = status_detail("POST", "/api/ws/merge?persist=false", raw=body, ctype=ctype)
    check("B3 有一份没识别到时间列 → 400 并点名那份文件",
          st == 400 and "没识别到时间列" in msg and "y.csv" in msg, f"{st} · {msg}")

    many = [(f"m{i}.csv", P1) for i in range(13)]
    body, ctype = multipart(many)
    st, msg = status_detail("POST", "/api/ws/merge?persist=false", raw=body, ctype=ctype)
    check("B4 超过 12 份 → 400 并给出上限", st == 400 and "12" in msg, f"{st} · {msg}")

    body, ctype = multipart([("empty.csv", b""), ("p.csv", P1)])
    st, msg = status_detail("POST", "/api/ws/merge?persist=false", raw=body, ctype=ctype)
    check("B5 空文件 → 400", st == 400 and "为空" in msg, f"{st} · {msg}")

    # 批次里有一份文件名非法：先校名再统一写盘，所以目录里不该留下这一批的任何半截文件
    body, ctype = multipart([("batch_ok.csv", P1), (".hidden_bad.csv", P2)])
    st, msg = status_detail("POST", "/api/ws/merge?persist=true", raw=body, ctype=ctype)
    check("B6 隐藏文件名 → 400", st == 400 and "隐藏" in msg, f"{st} · {msg}")
    check("B7 校验失败的那一批没有留下半截文件",
          not (DATA_DIR / "batch_ok.csv").exists(), str((DATA_DIR / "batch_ok.csv").exists()))

    body, ctype = multipart([("../evil.csv", P1), ("p2.csv", P2)])
    st, payload = call("POST", "/api/ws/merge?persist=true&limit=5", raw=body, ctype=ctype)
    resp = json.loads(payload.decode("utf-8", "replace")) if st < 400 else {}
    escaped = (DATA_DIR.parent / "evil.csv").exists()
    check("B8 带 .. 的文件名不会写出数据集目录", st < 400 and not escaped,
          f"{st} · 目录外 evil.csv 存在={escaped}")
    check("B9 越界写法被归一化成目录内的普通文件名", "evil.csv" in (resp.get("persisted") or []),
          str(resp.get("persisted")))

    # 拼不成一张表的一批（时间列名不一致）不该在数据集目录里留下孤儿文件
    before = set(p.name for p in DATA_DIR.iterdir())
    body, ctype = multipart([("orphans_a.csv", P1), ("orphans_b_naming.csv", P2.replace("timestamp", "ts"))])
    st, msg = status_detail("POST", "/api/ws/merge?persist=true", raw=body, ctype=ctype)
    after = set(p.name for p in DATA_DIR.iterdir())
    check("B10 合并失败的批次一个文件都不落盘（先解析合并、后写盘）",
          st == 400 and after == before, f"{st} · 新增文件 {sorted(after - before)}")


# ---------------------------------------------------------------- C 段：重启重建

def section_c(first: dict) -> None:
    print("\n=== C. 后端重启后：合并帧按数据集目录里的原始文件重建 ===")
    ws_id = first["meta"]["wsId"]
    before_rows = first["page"]["rows"]
    # 先挂一条命令，确认重放的仍然是同一条时间轴上的历史
    api("POST", f"/api/ws/{ws_id}/op/rename-column",
        {"key": "temp", "label": "环境温度", "newKey": "ambient"})
    mid = api("GET", f"/api/ws/{ws_id}")
    check("C0 改名后合并回执仍在（它描述的是导入，不随命令变化）",
          mid["merge"]["totalRows"] == 9 and mid["rowCount"] == 9, str(mid.get("merge")))

    pid = PROC.pid if PROC else None
    kill_backend()
    spawn_backend(PORT)
    note(f"后端已重启（旧进程 {pid} → 新进程 {PROC.pid}），state / dataset 目录不变")

    after = api("GET", f"/api/ws/{ws_id}")
    check("C1 重建后仍是同一颗帧：行数与列一致",
          after["rowCount"] == mid["rowCount"] and
          [c["key"] for c in after["columns"]] == [c["key"] for c in mid["columns"]],
          f"{after['rowCount']} 行 · {str([c['key'] for c in after['columns']])}")
    check("C2 版本号与命令日志一起回来", after["version"] == mid["version"],
          f"{after['version']} vs {mid['version']}")
    check("C3 合并回执逐项与重启前一致", after["merge"] == mid["merge"],
          json.dumps(after["merge"], ensure_ascii=False)[:160])
    page_after = api("GET", f"/api/ws/{ws_id}/rows?offset=0&limit=50")
    check("C4 页窗口逐行与导入时一致（改名只动了列名）",
          [r[:2] for r in page_after["page"]["rows"]] == [r[:2] for r in before_rows],
          f"{len(page_after['page']['rows'])} 行")
    check("C5 来源文件名仍指向数据集目录里的真实文件",
          all((DATA_DIR / n).exists() for n in after["source"]["filenames"]),
          str(after["source"]["filenames"]))
    # 删掉其中一份原始文件：下一次重建必须明确失败，而不是悄悄拼出半张表。
    # 走"再重启一次"而不是 DELETE：DELETE 会把命令日志一起清掉，那就变成"工作区不存在"，
    # 测不到"日志还在、来源文件没了"这条真实路径（用户在数据集目录里手删文件就是这种）。
    gone = after["source"]["filenames"][1]
    (DATA_DIR / gone).unlink()
    kill_backend()
    spawn_backend(PORT)
    st2, msg2 = status_detail("GET", f"/api/ws/{ws_id}")
    check("C6 原始文件被删后重建报 4xx 并点名缺失文件，不返回半张表",
          st2 >= 400 and gone in (msg2 or ""), f"{st2} · {msg2}"[:220])
    note(f"第二次重启后读取该工作区 → {st2}")


def main() -> int:
    global PORT
    print("== 第一步多文件合并导入验收 ==")
    for d in (STATE_DIR, DATA_DIR):
        if d.exists():
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    p.unlink()
                else:
                    try:
                        p.rmdir()
                    except OSError:
                        pass
    PORT = free_port()
    spawn_backend(PORT)
    try:
        health = api("GET", "/api/health")
        check("H1 能力清单声明 workspace:merge", "workspace:merge" in health["capabilities"])
        check("H2 上限声明 mergeFiles=12", health["limits"].get("mergeFiles") == 12,
              health["limits"].get("mergeFiles"))
        first = section_a()
        section_a2()
        section_b()
        section_c(first)
    finally:
        kill_backend()
    print("\n=== 结果 ===")
    for n in NOTES:
        print(f"  · {n}")
    if FAILURES:
        print(f"\nFAILED {len(FAILURES)} 项：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
