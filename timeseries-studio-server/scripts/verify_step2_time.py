# -*- coding: utf-8 -*-
"""第二步③④⑤⑥⑦验收：时间列判定、毫秒/自定义格式、末页页大小、重采样方向。

口径：每一条断言都打真实数字（命中率、解析数、行数、格数），并且尽量给出第二套实现——
参照值用纯 Python（str 切片 / 手算均值）算，不 import app.services.workspace，
这样"后端说的数"和"独立算出的数"必须是同一件事，而不是自己和自己比。

前置：uvicorn app.main:app --host 127.0.0.1 --port 8000（本脚本不自启后端）。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_step2_time.py
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
import urllib.parse
import urllib.request
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC: subprocess.Popen | None = None
STATE_DIR = os.path.join(ROOT, ".verify", "step2-time-state")
DATA_DIR = os.path.join(ROOT, ".verify", "step2-time-dataset")

FAILS: list[str] = []
CHECKS = 0


def check(name: str, got, want) -> None:
    """want 可以是值，也可以是 ('~=实际值', 判断函数)；打印两侧数字。"""
    global CHECKS
    CHECKS += 1
    if isinstance(want, tuple) and callable(want[1]):
        ok = bool(want[1](got))
        print(f"  {'OK ' if ok else 'FAIL'} {name}: {got}")
        if not ok:
            FAILS.append(name)
        return
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(name)


def call(method: str, path: str, body: dict | None = None, raw: bytes | None = None,
         content_type: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else raw
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if content_type or body is not None:
        req.add_header("Content-Type", content_type or "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as res:
            text = res.read().decode("utf-8", "replace")
            return res.status, json.loads(text or "{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(text or "{}")
        except json.JSONDecodeError:
            return exc.code, {"_raw": text[:300]}


def multipart(filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----tss" + uuid.uuid4().hex
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n\r\n'.encode())
    buf.write(content)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def upload(name: str, csv: str, limit: int = 500, persist: bool = False) -> dict:
    body, ctype = multipart(name, csv.encode("utf-8"))
    flag = "true" if persist else "false"
    status, payload = call("POST", f"/api/ws?persist={flag}&limit={limit}", raw=body, content_type=ctype)
    if status >= 400:
        raise SystemExit(f"上传 {name} 失败 {status}: {json.dumps(payload, ensure_ascii=False)}")
    return payload


def col_index(page: dict, key: str) -> int:
    return page["columns"].index(key)


def column(page: dict, key: str) -> list:
    i = col_index(page, key)
    return [r[i] for r in page["rows"]]


def close(ws_id: str) -> None:
    call("DELETE", f"/api/ws/{ws_id}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn_backend(port: int) -> None:
    """在临时状态目录里另起一台后端：验"命令日志能否重放出新命令"必须换进程才算。"""
    global BASE, PROC
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    env = dict(os.environ)
    env.update({"TSS_STATE_DIR": STATE_DIR, "TSS_DATASET_DIR": DATA_DIR, "PYTHONIOENCODING": "utf-8"})
    PROC = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                             "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


# ---------------------------------------------------------------- 样例数据

def rows_csv(header: str, rows: list[str]) -> str:
    return header + "\r\n" + "\r\n".join(rows) + "\r\n"


NO_TIME = rows_csv("站点,功率,备注", ["北京,12.5,正常", "上海,13.2,异常", "广州,11.8,正常"])
CN_TIME = rows_csv("时间,功率", ["二零二四年六月一日,12.5", "昨天,13.2", "下周三,11.8"])
FLOAT_TIME = rows_csv("time,功率", ["12.5,1", "13.2,2", "11.8,3"])
MS_TIME = rows_csv(
    "timestamp,功率",
    [f"2024-06-01 00:00:{i:02d}.{(123 + i * 137) % 1000:03d},1{i}" for i in range(12)])
US_TIME = rows_csv("stamp,功率", [
    "06/01/2024 08:30:00,1.0", "06/01/2024 08:45:00,2.0", "06/01/2024 09:00:00,3.0",
    "06/01/2024 09:15:00,4.0", "06/02/2024 08:30:00,5.0"])
UNPAD_TIME = rows_csv("采集时刻,交单时间,功率", [
    "2024-6-1 8:30,2024/6/1 08:30,1.0", "2024-6-1 8:45,2024/6/1 08:45,2.0",
    "2024-6-1 9:00,2024/6/1 09:00,3.0"])
# 升采样样本：720 行整点数据，中间留 3 个缺口（真实停机），站点列是文本
HOURY_HEADER = "timestamp,功率,站点"
HOURY_ROWS = []
_hole = {40, 41, 250}
for i in range(720):
    if i in _hole:
        continue
    day, hour = divmod(i, 24)
    HOURY_ROWS.append(f"2024-06-{day + 1:02d} {hour:02d}:00:00,{100 + i:.1f},A{day % 3}")
HOURY = rows_csv(HOURY_HEADER, HOURY_ROWS)
FIVEMIN_HEADER = "timestamp,功率"
FIVEMIN_ROWS = [f"2024-06-01 00:{m:02d}:00,{m + 1}.0" for m in range(0, 60, 5)] + \
               [f"2024-06-01 01:{m:02d}:00,{12 + m}.0" for m in range(0, 60, 5)]
FIVEMIN = rows_csv(FIVEMIN_HEADER, FIVEMIN_ROWS)


def main() -> int:
    global BASE
    status, health = call("GET", "/api/health")
    check("health", (status, health.get("status")), (200, "ok"))
    caps = health.get("capabilities") or []
    for need in ("op:time-col", "workspace:time-detect", "op:time-format"):
        check(f"capability {need}", need in caps, True)
    limits = health.get("limits") or {}
    print(f"  信息 闸门命中率={limits.get('minTimeHitRate')} 显示格式={len(limits.get('displayFormats') or [])} 种"
          f" 自动识别源格式={len(limits.get('parseFormats') or [])} 种")

    # ================= ③ 没有时间列：不许误认、不许误转 =================
    print("\n[1] 没有时间列的数据（站点/功率/备注）")
    r = upload("no_time.csv", NO_TIME)
    ws1, meta1, page1 = r["meta"]["wsId"], r["meta"], r["page"]
    check("no_time timeCol", meta1.get("timeCol"), None)
    check("no_time 列数", len(page1["columns"]), 3)
    check("no_time 功率列原值", column(page1, "功率")[:3], [12.5, 13.2, 11.8])
    check("no_time 站点列原值", column(page1, "站点")[:3], ["北京", "上海", "广州"])
    s, bad = call("POST", f"/api/ws/{ws1}/op/time-format", {"format": "YYYY年MM月DD日", "customFormat": None})
    print(f"  信息 无时间列执行转换 → {s} {bad.get('detail')}")
    check("no_time 转换被拒", s, 400)
    s, det = call("GET", f"/api/ws/{ws1}/time-detect?col={urllib.parse.quote('功率')}")
    check("no_time 功率列不能当时间", det.get("ok"), False)
    print(f"  信息 原因：{det.get('reason')}")
    close(ws1)

    print("\n[2] 中文文本列叫「时间」（曾经被洗成假时间）")
    r = upload("cn_time.csv", CN_TIME)
    ws2, meta2, page2 = r["meta"]["wsId"], r["meta"], r["page"]
    check("cn timeCol", meta2.get("timeCol"), None)
    rej = meta2.get("timeRejected") or []
    check("cn 有被退回的列", [x["key"] for x in rej], ["时间"])
    print(f"  信息 退回原因：{rej[0]['reason'] if rej else '（无）'}")
    check("cn 中文值一个没动", column(page2, "时间"), ["二零二四年六月一日", "昨天", "下周三"])
    check("cn 该列仍是文本", (next(c for c in meta2["columns"] if c["key"] == "时间"))["type"], "category")
    s, bad = call("POST", f"/api/ws/{ws2}/op/time-format", {"format": "YYYY-MM-DD", "customFormat": None})
    check("cn 转换被拒", s, 400)
    s, bad = call("POST", f"/api/ws/{ws2}/op/time-col", {"key": "时间"})
    print(f"  信息 指定中文列为时间列 → {s} {bad.get('detail')}")
    check("cn 指定也被拒", s, 400)
    close(ws2)

    print("\n[3] 叫 time 的浮点读数列（曾被当成 epoch 秒）")
    r = upload("float_time.csv", FLOAT_TIME)
    ws3, meta3, page3 = r["meta"]["wsId"], r["meta"], r["page"]
    check("float timeCol", meta3.get("timeCol"), None)
    rej3 = meta3.get("timeRejected") or []
    print(f"  信息 退回原因：{rej3[0]['reason'] if rej3 else '（无）'}")
    check("float 数值没被改成时间", column(page3, "time"), [12.5, 13.2, 11.8])
    close(ws3)

    # ================= ④ 毫秒格式 + 自定义格式 =================
    print("\n[4] YYYY-MM-DD HH:mm:ss.SSS（曾经识别失败）")
    r = upload("ms_time.csv", MS_TIME, limit=50)
    ws4, meta4, page4 = r["meta"]["wsId"], r["meta"], r["page"]
    check("ms timeCol", meta4.get("timeCol"), "timestamp")
    det4 = meta4.get("timeDetect") or {}
    check("ms 源格式", det4.get("format"), "YYYY-MM-DD HH:mm:ss.SSS")
    check("ms 命中率", det4.get("matchRate"), 100.0)
    print(f"  信息 解析 {det4.get('matched')}/{det4.get('sampled')} 个值 · 置信度 {det4.get('confidence')} · via={det4.get('via')}")
    # 参照实现：源串与显示串必须逐字符一致，且毫秒位与源数据的算术关系对得上
    shown = column(page4, "timestamp")
    want_first = [f"2024-06-01 00:00:{i:02d}.{(123 + i * 137) % 1000:03d}" for i in range(12)]
    check("ms 显示值与源串一致", shown, want_first)
    check("ms 第 2 行毫秒", shown[1][20:], str((123 + 137) % 1000).zfill(3))
    s, d4 = call("GET", f"/api/ws/{ws4}/time-detect?col=timestamp")
    check("ms time-detect ok", (s, d4["ok"], d4["matchRate"]), (200, True, 100.0))
    print(f"  信息 后端回显 before={d4['samplesBefore']} after={d4['samplesAfter']}")
    # 这一列已经是 datetime64（via=dtype），解析给不出来源信息；工作区上传时测出的 .SSS
    # 必须原样带回来，否则界面会同时念「按 YYYY-MM-DD HH:mm:ss 解析」和「当前显示 .SSS」。
    check("ms dtype 再识别不覆盖源格式", (d4["via"], d4["format"], d4["displayFormat"]),
          ("dtype", "YYYY-MM-DD HH:mm:ss.SSS", "YYYY-MM-DD HH:mm:ss.SSS"))
    check("ms dtype 再识别样本按 .SSS 渲染", d4["samplesAfter"][0], "2024-06-01 00:00:00.123")

    print("\n[5] 自定义显示格式（含中文literal 与 JS 写法归一）")
    s, res = call("POST", f"/api/ws/{ws4}/op/time-format",
                  {"format": "custom", "customFormat": "yyyy年MM月DD日 HH:mm:ss.SSS"})
    print(f"  信息 custom 中文格式 → {s} {res.get('summary')}")
    check("ms 自定义中文格式接受", s, 200)
    shown2 = column(res["page"], "timestamp")
    print(f"  信息 中文模板渲染首行 = {shown2[0]}")
    check("ms 中文格式渲染", shown2[0], "2024年06月01日 00:00:00.123")
    check("ms 中文格式第二行毫秒", shown2[1][-4:], ".260")
    # epoch_s：显示成数字，参照纯 Python 换算（naive 时间按本机偏移）
    s, res = call("POST", f"/api/ws/{ws4}/op/time-format", {"format": "epoch_s", "customFormat": None})
    epoch_vals = column(res["page"], "timestamp")
    print(f"  信息 epoch_s 前两个值 = {epoch_vals[:2]}")
    check("ms epoch 是整数", all(isinstance(v, int) for v in epoch_vals), True)
    check("ms epoch 相邻差 1 秒", epoch_vals[5] - epoch_vals[4], 1)
    # 一个占位符都没有的格式名必须报错，且报错里给出可选清单
    s, bad = call("POST", f"/api/ws/{ws4}/op/time-format", {"format": "LL", "customFormat": None})
    print(f"  信息 无占位符的格式名 → {s} {bad.get('detail')}")
    check("ms 非法格式被拒", s, 400)

    print("\n[6] 自动识别认不出 → 手填源格式（MM/DD/YYYY HH:mm:ss）")
    r = upload("us_time.csv", US_TIME)
    ws6, meta6, page6 = r["meta"]["wsId"], r["meta"], r["page"]
    check("us 自动识别放弃", meta6.get("timeCol"), None)
    rej6 = meta6.get("timeRejected") or []
    print(f"  信息 退回原因：{rej6[0]['reason'] if rej6 else '（无）'}")
    s, d6 = call("GET", f"/api/ws/{ws6}/time-detect?col={urllib.parse.quote('stamp')}")
    check("us 不带格式仍不行", d6.get("ok"), False)
    q = urllib.parse.urlencode({"col": "stamp", "format": "MM/DD/YYYY HH:mm:ss"})
    s, d6 = call("GET", f"/api/ws/{ws6}/time-detect?{q}")
    print(f"  信息 带格式 → ok={d6.get('ok')} 命中率={d6.get('matchRate')} "
          f"解析={d6.get('parsedCount')}/{d6.get('nonEmpty')} before={d6.get('samplesBefore')} after={d6.get('samplesAfter')}")
    check("us 带格式识别成功", (s, d6.get("ok"), d6.get("matchRate")), (200, True, 100.0))
    check("us 显示格式就是手填格式", d6.get("displayFormat"), "MM/DD/YYYY HH:mm:ss")
    s, res = call("POST", f"/api/ws/{ws6}/op/time-col",
                  {"key": "stamp", "format": "MM/DD/YYYY HH:mm:ss", "customFormat": None})
    print(f"  信息 指定时间列 → {s} {res.get('summary')}")
    check("us 指定成功", s, 200)
    meta6b, page6b = res["meta"], res["page"]
    check("us timeCol 已改", meta6b.get("timeCol"), "stamp")
    check("us 列类型变 datetime", (next(c for c in meta6b["columns"] if c["key"] == "stamp"))["type"], "datetime")
    # 参照实现：06/01/2024 08:30 → 2024-06-01 08:30，月日绝不对调
    want = ["06/01/2024 08:30:00", "06/01/2024 08:45:00", "06/01/2024 09:00:00",
            "06/01/2024 09:15:00", "06/02/2024 08:30:00"]
    check("us 显示串保持源写法", column(page6b, "stamp"), want)
    s, d6b = call("GET", f"/api/ws/{ws6}/time-detect?col=stamp&format=YYYY-MM-DDTHH:mm:ss")
    check("us 换 ISO 显示", d6b["ok"], True)
    s, res = call("POST", f"/api/ws/{ws6}/op/time-format", {"format": "YYYY-MM-DD HH:mm", "customFormat": None})
    check("us 转成 ISO 显示", column(res["page"], "stamp"),
          ["2024-06-01 08:30", "2024-06-01 08:45", "2024-06-01 09:00", "2024-06-01 09:15", "2024-06-02 08:30"])
    # 指定错格式应当照实报错，不能"多少算多少"
    s, bad = call("POST", f"/api/ws/{ws6}/op/time-col",
                  {"key": "功率", "format": "YYYY-MM-DD HH:mm:ss", "customFormat": None})
    print(f"  信息 用时间格式解析功率列 → {s} {bad.get('detail')}")
    check("us 错格式报错", s, 400)
    # 撤销这条命令：时间列与 dtype 一起退回去
    s, res = call("POST", f"/api/ws/{ws6}/restore", {"version": 0})
    check("us 撤销后无时间列", (s, res["meta"].get("timeCol")), (200, None))
    check("us 撤销后 dtype 退回", (next(c for c in res["meta"]["columns"] if c["key"] == "stamp"))["type"], "category")
    s, res = call("POST", f"/api/ws/{ws6}/restore", {"version": 1})
    check("us 重做又回来", (s, res["meta"].get("timeCol")), (200, "stamp"))
    close(ws6)

    print("\n[7] 无前导零的时间串 + 不补零的自定义显示模板")
    r = upload("unpad_time.csv", UNPAD_TIME)
    un_ws, meta7 = r["meta"]["wsId"], r["meta"]
    det7 = meta7.get("timeDetect") or {}
    print(f"  信息 自动识别：timeCol={meta7.get('timeCol')} 格式={det7.get('format')} "
          f"命中率={det7.get('matchRate')} 解析 {det7.get('matched')}/{det7.get('sampled')}")
    check("unpad 无前导零也能自动识别", (meta7.get("timeCol"), det7.get("format")), ("采集时刻", "YYYY-MM-DD HH:mm"))
    check("unpad 显示按登记格式补零", column(r["page"], "采集时刻"),
          ["2024-06-01 08:30", "2024-06-01 08:45", "2024-06-01 09:00"])
    # 自定义显示模板里的单位占位符（M/D/H/m）以前会被写成字面量，这里必须是真实数字
    s, res7 = call("POST", f"/api/ws/{un_ws}/op/time-format",
                   {"format": "custom", "customFormat": "YYYY/M/D H:m"})
    print(f"  信息 单字母模板 → {s} {res7.get('summary')} 渲染={column(res7['page'], '采集时刻')}")
    check("unpad 单字母模板不补零", column(res7["page"], "采集时刻"),
          ["2024/6/1 8:30", "2024/6/1 8:45", "2024/6/1 9:0"])
    # 斜杠模板打在"已经是 datetime64 的当前时间列"上：这一步没有源串可解析了，
    # 模板只决定显示格式，所以 ok 恒真、via=dtype。真实行为就这样念出来，不许假装它在解析。
    q = urllib.parse.urlencode({"col": "采集时刻", "format": "YYYY/M/D H:m"})
    s, d7 = call("GET", f"/api/ws/{un_ws}/time-detect?{q}")
    print(f"  信息 已是时间列再指定模板 → ok={d7.get('ok')} via={d7.get('via')} "
          f"显示={d7.get('displayFormat')}")
    check("unpad 已解析列的模板只当显示格式", (d7.get("ok"), d7.get("via"), d7.get("displayFormat")),
          (True, "manual", "YYYY/M/D H:m"))
    # 还没被转成 datetime 的第二列（交单时间）才是真考卷：模板分隔符与数据不符必须拒绝
    q = urllib.parse.urlencode({"col": "交单时间", "format": "YYYY-MM-DD HH:mm"})
    s, d7b = call("GET", f"/api/ws/{un_ws}/time-detect?{q}")
    print(f"  信息 连字符模板解析斜杠数据 → ok={d7b.get('ok')} 原因={d7b.get('reason')}")
    check("unpad 模板不符时拒绝", (d7b.get("ok"), d7b.get("parsedCount")), (False, 0))
    # 同一列换成斜杠+不补零模板：位数宽容（%m 吃 1~2 位）、分隔符严格，认不出才是 bug
    q = urllib.parse.urlencode({"col": "交单时间", "format": "YYYY/M/D H:m"})
    s, d7c = call("GET", f"/api/ws/{un_ws}/time-detect?{q}")
    print(f"  信息 斜杠模板解析斜杠数据 → ok={d7c.get('ok')} 解析={d7c.get('parsedCount')}/"
          f"{d7c.get('nonEmpty')} 显示={d7c.get('displayFormat')}")
    check("unpad 模板相符时认出文本列",
          (d7c.get("ok"), d7c.get("parsedCount"), d7c.get("displayFormat")),
          (True, 3, "YYYY/M/D H:m"))
    close(un_ws)

    # ================= ⑥ 重采样方向 =================
    print("\n[8] 升采样：717 行 60min 数据 → 15min（旧实现造出整行全空的洞）")
    r = upload("houry.csv", HOURY, limit=500)
    ws8, meta8 = r["meta"]["wsId"], r["meta"]
    total8 = meta8["rowCount"]
    check("houry 行数", total8, 717)
    check("houry 时间列", meta8.get("timeCol"), "timestamp")
    s, prev = call("GET", f"/api/ws/{ws8}/resample-preview?targetMinutes=15")
    print(f"  信息 预演：{prev.get('currentRows')} → {prev.get('projectedRows')} 行 · "
          f"{prev.get('directionLabel')}（源 {prev.get('sourceMinutes')}min）· "
          f"有值桶 {prev.get('filledBuckets')} · 空桶 {prev.get('emptyBuckets')} · {prev.get('emptyNote')}")
    check("升采样方向", prev.get("direction"), "up")
    check("升采样源间隔", prev.get("sourceMinutes"), 60)
    s, res = call("POST", f"/api/ws/{ws8}/op/resample", {"targetMinutes": 15, "method": "mean"})
    got8 = res
    print(f"  信息 执行：{got8.get('summary')} · 方向 {got8.get('directionLabel')} · {got8.get('fillNote')}")
    check("升采样行数=桶数", (got8.get("oldCount"), got8.get("newCount")), (717, prev.get("projectedRows")))
    filled = got8.get("filledBuckets")
    print(f"  信息 有观测桶 {filled} 个，其余 {got8.get('emptyBuckets')} 个桶靠填充/插值补齐")
    # 关键断言：升采样之后不该再有"整行全空"的行（旧实现是 2157 行）
    s, allrows = call("GET", f"/api/ws/{ws8}/rows?offset=0&limit=500")
    page_a = allrows["page"]
    null_power = sum(1 for v in column(page_a, "功率") if v is None)
    print(f"  信息 首页 {page_a['returned']} 行里功率为空 {null_power} 格（旧实现这一段几乎全空）")
    check("升采样首页没有空洞", null_power, 0)
    s, tail = call("GET", f"/api/ws/{ws8}/rows?offset={got8['newCount'] - 50}&limit=50")
    check("升采样末段也没有空洞", sum(1 for v in column(tail["page"], "功率") if v is None), 0)
    s, mid = call("GET", f"/api/ws/{ws8}/columns?keys={urllib.parse.quote('功率,站点', safe=',')}")
    pw = mid["columns"]["功率"]
    st = mid["columns"]["站点"]
    check("升采样全表功率无空值", sum(1 for v in pw if v is None), 0)
    check("升采样文本列也无空值", sum(1 for v in st if v is None), 0)
    # 参照实现（纯 Python，不碰 pandas）：源数据第 40/41 小时是真实停机（i=40,41 被跳过），
    # 值延续下 16:00、17:00 两格应等于缺口前最后一个观测 i=39 的值 100+39=139.0
    s, probe = call("GET", f"/api/ws/{ws8}/rows?offset=0&limit=500")
    pg = probe["page"]
    times = column(pg, "timestamp")
    powers = column(pg, "功率")
    idx1600 = times.index("2024-06-02 16:00:00")
    print(f"  信息 2024-06-02 15:00/16:00/17:00/18:00 = "
          f"{[powers[times.index(f'2024-06-02 {h}:00:00')] for h in ('15', '16', '17', '18')]}")
    check("升采样缺口按值延续补齐", (powers[idx1600], powers[idx1600 + 1]), (139.0, 139.0))
    check("升采样下一个真实观测不变", powers[times.index("2024-06-02 18:00:00")], 142.0)
    close(ws8)

    print("\n[9] 降采样：24 行 5min → 60min，均值必须是手算那个数，空桶不许补")
    r = upload("five.csv", FIVEMIN, limit=50)
    ws9 = r["meta"]["wsId"]
    s, prev9 = call("GET", f"/api/ws/{ws9}/resample-preview?targetMinutes=60")
    print(f"  信息 预演：{prev9.get('currentRows')} → {prev9.get('projectedRows')} 行 · {prev9.get('directionLabel')}"
          f"（源 {prev9.get('sourceMinutes')}min）· {prev9.get('emptyNote')}")
    check("降采样方向", prev9.get("direction"), "down")
    s, res9 = call("POST", f"/api/ws/{ws9}/op/resample", {"targetMinutes": 60, "method": "mean"})
    got9 = res9
    print(f"  信息 执行：{got9.get('summary')} · {got9.get('fillNote')}")
    check("降采样行数", got9.get("newCount"), 2)
    pg9 = res9["page"]
    # 参照实现（纯 Python，等差数列求和）：第一桶 = 00:00~00:55 的 12 个值 1,6,11..56
    ref_b0 = sum(m + 1 for m in range(0, 60, 5)) / 12
    ref_b1 = sum(12 + m for m in range(0, 60, 5)) / 12
    print(f"  信息 参照手算：第一桶 {ref_b0} · 第二桶 {ref_b1}")
    check("降采样第一桶均值", column(pg9, "功率")[0], ref_b0)
    check("降采样第二桶均值", column(pg9, "功率")[1], ref_b1)
    check("降采样桶起点整点对齐", column(pg9, "timestamp"), ["2024-06-01 00:00:00", "2024-06-01 01:00:00"])
    close(ws9)

    print("\n[10] 同粒度重排（60min → 60min）不许谎称降采样")
    r = upload("houry2.csv", HOURY, limit=50)
    ws10 = r["meta"]["wsId"]
    s, prev10 = call("GET", f"/api/ws/{ws10}/resample-preview?targetMinutes=60")
    print(f"  信息 同粒度：{prev10.get('currentRows')} → {prev10.get('projectedRows')} 行 · {prev10.get('directionLabel')}")
    check("同粒度方向", prev10.get("direction"), "same")
    close(ws10)

    # ================= ⑤ 末页与页大小 =================
    print("\n[11] 末页：limit 必须回显页大小，不是这一页实际行数")
    r = upload("houry3.csv", HOURY, limit=500)
    ws11 = r["meta"]["wsId"]
    total11 = r["meta"]["rowCount"]
    for size in (20, 50, 200, 500):
        max_off = max(0, (total11 - 1) // size * size)
        s, one = call("GET", f"/api/ws/{ws11}/rows?offset={max_off}&limit={size}")
        p = one["page"]
        print(f"  信息 {size} 行/页 · 末页 offset={p['offset']} limit={p['limit']} returned={p['returned']} total={p['total']}")
        check(f"末页 limit 回显 {size}", p["limit"], size)
        check(f"末页 returned ≤ {size}", p["returned"] <= size, True)
    s, one = call("GET", f"/api/ws/{ws11}/rows?offset=0&limit=1000")
    print(f"  信息 超上限请求 1000 → HTTP {s}（路由按 le=MAX_PAGE 校验，界面只提供 ≤500 的档）")
    check("页大小越界被拒", s, 422)
    close(ws11)
    close(ws4)

    # ================= ④' 命令日志重放：换一个进程也认得手填格式 =================
    print("\n[12] 重启后端：指定时间列 + 自定义格式这两条命令必须能重放")
    port = free_port()
    spawn_backend(port)
    # persist=true 才谈得上"重启还在"：载入帧留在数据集目录，日志里只有命令
    r = upload("restart_ms.csv", MS_TIME, limit=50, persist=True)
    ws12, meta12 = r["meta"]["wsId"], r["meta"]
    s, res = call("POST", f"/api/ws/{ws12}/op/time-col", {"key": "timestamp"})
    check("重启前 指定时间列", (s, res["meta"].get("timeCol")), (200, "timestamp"))
    s, res = call("POST", f"/api/ws/{ws12}/op/time-format",
                  {"format": "custom", "customFormat": "DD.MM.YYYY HH:mm:ss.SSS"})
    check("重启前 自定义格式", (s, res["meta"].get("timeFormat")), (200, "DD.MM.YYYY HH:mm:ss.SSS"))
    before_rows = column(res["page"], "timestamp")[:3]
    print(f"  信息 重启前首页前三行 = {before_rows} · 版本 {res['meta'].get('version')}")
    ops12 = [(o["kind"], o.get("summary")) for o in res["meta"].get("ops") or []]
    kill_backend()
    spawn_backend(free_port())
    s, again = call("GET", f"/api/ws/{ws12}/rows?offset=0&limit=50")
    if s != 200:
        print(f"  FAIL 重启后读取 {s}: {json.dumps(again, ensure_ascii=False)[:200]}")
        FAILS.append("重启后读取")
        kill_backend()
        return 1
    m2, p2 = again["meta"], again["page"]
    print(f"  信息 重启后：timeCol={m2.get('timeCol')} 格式={m2.get('timeFormat')} "
          f"版本={m2.get('version')} 命令={ops12}")
    check("重启后时间列还在", m2.get("timeCol"), "timestamp")
    check("重启后自定义格式还在", m2.get("timeFormat"), "DD.MM.YYYY HH:mm:ss.SSS")
    check("重启后渲染值逐字符一致", column(p2, "timestamp")[:3], before_rows)
    check("重启后源格式回执还在", (m2.get("timeDetect") or {}).get("format"), "YYYY-MM-DD HH:mm:ss.SSS")
    close(ws12)
    BASE = "http://127.0.0.1:8000"
    kill_backend()
    for path in (os.path.join(DATA_DIR, "restart_ms.csv"),):
        try:
            os.remove(path)
        except OSError:
            pass

    print(f"\n合计 {CHECKS} 项断言，失败 {len(FAILS)} 项")
    if FAILS:
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
