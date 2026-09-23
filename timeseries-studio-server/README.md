# TimeSeries Studio Server

时序数据清洗工作台（`../timeseries-studio`）的 FastAPI 后端。

只承担浏览器单文件做不到的三件事，其余计算仍在前端本地完成：

| 能力 | 为什么需要后端 |
| --- | --- |
| Parquet / Feather 解析 | 列式压缩二进制，浏览器无可靠解码器 → `pyarrow` |
| Parquet / Feather 导出 | 需要真实 thrift / Arrow IPC 编码，前端伪造会产出损坏文件 |
| 完整版孤立森林 | `scikit-learn.IsolationForest` 模型训练与打分 |
| 数据集目录读写 | 浏览器无法枚举或持久化本地目录 → `dataset/` |

**除数据集目录外无状态**：前端随每个请求携带数据（JSON 行或 multipart 文件），服务端不使用任何数据库。
唯一的落盘位置是数据集目录，用于给前端「最近打开的数据集」提供真实文件来源。

## 数据集目录

默认 `<后端进程 cwd>/dataset`，即在本目录下；可用环境变量改到别处：

```bash
TSS_DATASET_DIR="D:/Users/81974/Desktop/444/dataset" .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

- 目录不存在时自动创建，无需手工建
- 只列举/接收表格扩展名：`.csv .tsv .txt .xlsx .xls .parquet .feather .ft`，跳过点开头文件
- 前端导入（`POST /api/datasets`）会先落盘再解析，因此下次打开即出现在最近列表
- 重名不覆盖：追加 `__YYYYmmdd_HHMMSS` 后缀
- 文件名经 `safe_name` 校验（拒绝路径分隔符、`..`、控制字符、非法扩展名），
  `resolve_in_dir` 再确认解析后的绝对路径仍在目录内，杜绝目录穿越

## 运行

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

接口文档：http://127.0.0.1:8000/docs

前端默认地址 `http://127.0.0.1:8000`，如需改动在浏览器控制台执行
`localStorage.setItem('tss.apiBase', 'http://127.0.0.1:9000')` 后刷新。
后端未启动时前端会自动回退：CSV/Excel 仍可本地解析（但不写入数据集目录），
Parquet/Feather 解析导出、sklearn 完整版检测、以及「最近打开的数据集」目录列举
都会明确提示「需后端」而不是降级为假数据。

## 接口

- `GET /api/health` → `{ status, version, capabilities[], datasetDir }`，前端据此切换在线/离线呈现并展示目录绝对路径
- `POST /api/parse`（multipart `file`）→ `{ name, format, columns[], timeCol, freqMinutes, rowCount, data[] }`
  - 无状态解析，不落盘；前端导入走下面的 `POST /api/datasets`
  - 支持 `.parquet .feather/.ft .xlsx/.xls .csv/.tsv`；CSV 编码依次尝试 utf-8-sig / utf-8 / gbk / gb18030
  - NaN / NaT / inf 统一转为 `null`，时间转为 `YYYY-MM-DD HH:MM:SS`
  - `format` 取值为 `parquet / feather / xlsx / xls / csv`，与前端 `d.format` 一致，
    决定生成脚本用 `read_parquet` 还是 `read_excel` / `read_csv`
- `POST /api/export`（JSON `{ format, columns[], rows[], filename }`）→ 附件字节流
- `POST /api/anomaly/iforest`（JSON `{ columns[{key,values}], n_estimators, contamination, max_samples, random_state, normal_lower_q, normal_upper_q }`）
  → `{ engine, params, perColumn{key:{anomalyIndices,scores,count,validCount,lower,upper}}, summary }`
  - 逐列拟合并打分，缺失点自动剔除
  - `lower` / `upper` 取正常点的分位数边界，供前端「阈值截断修复」直接复用
- `GET /api/datasets` → `{ dir, count, items[{filename,name,ext,format,size,sizeText,modifiedAt,mtime}] }`
  - 按修改时间倒序，供前端「最近打开的数据集」渲染
- `POST /api/datasets`（multipart `file`）→ 解析结果 + `{ saved, dir }`（落盘 + 解析）
- `GET /api/datasets/{filename}/parse` → 同 `/api/parse` 的解析结果（打开目录内已有文件）
  - 文件名非法 → 400；不存在 → 404

## 目录

```
app/
  main.py                 FastAPI 实例、CORS、七个端点
  schemas.py              Pydantic 请求模型与取值校验
  services/parser.py      表格解析 → 前端数据集结构
  services/exporter.py    行数据 → 四种格式字节流
  services/anomaly.py     sklearn 孤立森林
  services/dataset_store.py 数据集目录：建目录/列举/读取/保存 + 文件名防护
dataset/                  数据集目录（首次运行自动创建，存放导入的表格文件）
fixtures/                 手工验证用样例数据（parquet/feather/csv 各一份，含注入尖峰与缺失）
```
