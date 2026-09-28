"""生成第二步浏览器验收用的 fixture CSV（只写文件，不发请求）。"""
import os
from datetime import datetime, timedelta

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".verify", "step2-browser")
os.makedirs(OUT, exist_ok=True)


def w(name: str, text: str) -> str:
    p = os.path.join(OUT, name)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return p


paths = []

paths.append(w("no_time.csv",
    "站点,功率,备注\n北京,12.5,正常\n上海,13.2,异常\n广州,11.8,正常\n深圳,14.1,正常\n成都,10.9,异常\n"))

paths.append(w("cn_time.csv",
    "时间,功率\n二零二四年六月一日,12.5\n昨天,13.2\n下周三,11.8\n前天,9.4\n今天,10.1\n"))

ms_rows = []
for i in range(12):
    ms_rows.append(f"2024-06-01 {i:02d}:15:30.{(i*123)%1000:03d},{1000 + i * 7},{200 + i * 3}")
paths.append(w("ms_time.csv",
    "采集时刻,有功功率,无功功率\n" + "\n".join(ms_rows) + "\n"))

paths.append(w("unpad_time.csv",
    "采集时刻,交单时间,功率\n" + "\n".join(
        f"2024-6-{d} {h}:{m:02d}:00,2024/6/{d} {h}:{m:02d},{'%.1f' % (d + h / 10)}"  # 第二列故意用斜杠分隔
        for d in range(1, 7) for h in (8, 9, 10, 11, 12, 13, 14, 15) for m in (0, 30)
    ) + "\n"))

# 宽表 + 长时间轴：1200 行 @5min，14 列（1 时间 + 13 数值）
# → 验末页/每页行数、重采样方向、表头横向滚动
cols = ["采集时刻", "有功功率", "无功功率", "电压A", "电压B", "电压C",
        "电流A", "电流B", "电流C", "频率", "温度", "辐照度", "风速", "机位角"]
rows = []
start = datetime(2024, 1, 1, 0, 0, 0)
for i in range(1200):
    # 故意在第 400 行留一个 10 分钟的缺口：重采样的「有观测/没有观测」两个桶数才有东西可数
    if i == 400:
        continue
    ts = (start + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S")
    vals = [f"{round(100 + 20 * (i % 7) / 6 + (i % 13) * 0.7, 2)}",
            f"{round(30 + (i % 5) * 1.3, 2)}",
            f"{round(60 + (i % 11) * 0.9, 2)}",
            f"{round(220 + (i % 3) * 1.7, 2)}",
            f"{round(221 + (i % 4) * 1.1, 2)}",
            f"{round(219 + (i % 6) * 0.8, 2)}",
            f"{round(120 + (i % 9) * 2.5, 2)}",
            f"{round(118 + (i % 8) * 2.1, 2)}",
            f"{round(50 + (i % 3) * 0.02, 2)}",
            f"{round(25 + (i % 17) * 0.6, 2)}",
            f"{round(800 + (i % 23) * 7.5, 2)}",
            f"{round(4 + (i % 12) * 0.35, 2)}",
            f"{round((i % 360) / 10, 2)}"]
    assert len(vals) == len(cols) - 1, f"{len(vals)} vs {len(cols)}"
    rows.append(",".join([ts] + vals))
paths.append(w("wide_1200.csv", ",".join(cols) + "\n" + "\n".join(rows) + "\n"))

for p in paths:
    with open(p, encoding="utf-8") as f:
        lines = sum(1 for _ in f)
    print(f"{p}  rows(incl header)={lines}")
