# -*- mode: python ; coding: utf-8 -*-
"""TimeSeries Studio 后端 one-file 打包规格。

打出来的 exe 与 `python -m uvicorn app.main:app --host --port` 等价：
入口 packaging/backend_entry.py 直接 import app.main:app 交给 uvicorn.run，
所以这里最关键的是把 uvicorn 那批「按字符串动态加载」的 loop/protocol/lifespan
模块显式收进来——静态分析看不见它们。

数据集与状态目录不打包：服务端读的是 <cwd>/dataset 与 <cwd>/.tss-state（见 backend_entry），
exe 里没有用户数据，换机器也不会把谁的 CSV 带出去。
"""
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.dirname(os.path.abspath(SPECPATH))   # SPECPATH 是 packaging/，上一级才是后端项目根
ENTRY = os.path.join(ROOT, "packaging", "backend_entry.py")


def mods(pkg):
    """collect_submodules 会把包自带的 tests/ 一起收进来，对 exe 毫无用处，砍掉。"""
    return [m for m in collect_submodules(pkg) if ".tests" not in m and not m.endswith("_tests")]


hiddenimports = []
hiddenimports += mods("uvicorn")     # loops/protocols/lifespan 靠字符串选实现
hiddenimports += mods("app")         # 路由与服务全是包内相对导入
hiddenimports += mods("sklearn.ensemble")
hiddenimports += mods("sklearn.tree")  # IsolationForest 内部用到 _tree 的 cython 扩展
# sklearn 的 array-api 适配层走 scipy 里那份 vendored 拷贝，fft/linalg 是运行时才 import 的，
# 不显式收就会在孤立森林那一步炸 ModuleNotFoundError（冒烟测试就是这么抓到的）
hiddenimports += mods("scipy._external")
hiddenimports += ["python_multipart", "multipart"]   # fastapi 的 Form/File 解析，装了哪个算哪个

a = Analysis(
    [ENTRY],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "numpy.tests", "scipy.tests", "pandas.tests"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="timeseries-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 保留控制台：Electron 用 windowsHide 静默拉起，日志照样能从 stderr 读
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
