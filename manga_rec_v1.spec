# -*- coding: utf-8 -*-
"""MangaRecV1 Windows 打包配置（onedir + 独立窗口）。

用法: .venv\Scripts\pyinstaller.exe manga_rec_v1.spec --noconfirm
"""
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs, collect_submodules

datas = []
binaries = []
hiddenimports = []

# 依赖包整体收集（数据文件 + 二进制 + 子模块）
for pkg in ("opencc", "pykakasi", "pythonnet", "clr_loader", "jmcomic", "webview"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# curl_cffi：自带 libcurl DLL
binaries += collect_dynamic_libs("curl_cffi")

# playwright：驱动（node）由 hooks-contrib 处理，这里兜底收集 driver 数据
datas += collect_data_files("playwright", includes=["driver/**/*"])
hiddenimports += collect_submodules("playwright")

# pywin32（jm-login 读取 Chrome cookie 需要）
hiddenimports += ["win32crypt", "win32api", "win32file"]

# 项目数据（安装版首次启动会播种到 %APPDATA%\MangaRecommender）
datas += [
    ("config.json", "."),
    ("folder_list.txt", "."),
    ("data", "data"),
]

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PySide6", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="MangaRecV1",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    version="version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MangaRecV1",
)
