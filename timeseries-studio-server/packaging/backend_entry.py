"""打包版后端入口：把 `python -m uvicorn app.main:app` 装进一个 exe。

one-file 每次运行都会把自己解到 %TEMP%\\_MEIPASSxxxx，所以数据目录绝不能跟着走：
默认把 cwd 钉到 exe 所在目录，服务端的 <cwd>/dataset、<cwd>/.tss-state 就落在 exe 旁边，
双击运行与 Electron 托管看到的是同一份数据。要换位置用 --dataset-dir / --state-dir。

参数与源码态保持一致（--host / --port），Electron 那边只换可执行文件、不换命令行。
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
from pathlib import Path


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent  # 源码态：packaging/ 的上一级就是后端项目根


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="timeseries-backend", description="TimeSeries Studio 后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dataset-dir", default="", help="数据集目录，默认 <exe 目录>/dataset")
    parser.add_argument("--state-dir", default="", help="命令日志与会话目录，默认 <exe 目录>/.tss-state")
    parser.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # sklearn / joblib 在 Windows 冻结态派生子进程时会重新执行程序入口，没有这行会无限套娃
    multiprocessing.freeze_support()
    args = parse_args(sys.argv[1:] if argv is None else argv)

    os.chdir(runtime_root())
    if args.dataset_dir:
        os.environ["TSS_DATASET_DIR"] = str(Path(args.dataset_dir).expanduser().resolve())
    if args.state_dir:
        os.environ["TSS_STATE_DIR"] = str(Path(args.state_dir).expanduser().resolve())

    import uvicorn

    from app import __version__
    from app.main import app
    from app.services import dataset_store, state_store

    # 报服务端真正会用的那两个目录（读 TSS_*_DIR 覆盖），别报 cwd 拼出来的假路径
    print(
        f"[timeseries-backend] v{__version__} frozen={bool(getattr(sys, 'frozen', False))} "
        f"http://{args.host}:{args.port} dataset={dataset_store.dataset_dir()} state={state_store.state_dir()}",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
