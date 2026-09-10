# -*- coding: utf-8 -*-
"""V1 正式版独立窗口应用：本地 HTTP 服务 + 原生窗口（WebView2/EdgeChromium）。"""
from __future__ import annotations

import socket
import threading
from http.server import ThreadingHTTPServer

from .db import init_db
from .serve import Handler, _safe_fetch_thumbs, _sites_cache_loop


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_app() -> int:
    # 工作目录切到用户数据目录：jmcomic（禁漫）会在 cwd 找 option.yml，
    # 用户把登录配置放到数据目录即可被识别。
    import os

    from .config import ROOT

    try:
        os.chdir(str(ROOT))
    except OSError:  # noqa: BLE001
        pass
    init_db()
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # 后台预热：收藏列表封面 + 站点可用性缓存（弹窗秒开）
    threading.Thread(target=_safe_fetch_thumbs, daemon=True).start()
    threading.Thread(target=_sites_cache_loop, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    _startup_log(url)

    import webview

    window = webview.create_window(
        "漫画推荐 V1", url, width=1280, height=860, min_size=(980, 660)
    )
    try:
        webview.start(gui="edgechromium" if _edge_runtime() else None)
    finally:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass
    return 0


def _startup_log(url: str) -> None:
    from .config import ROOT

    try:
        (ROOT / "data").mkdir(parents=True, exist_ok=True)
        (ROOT / "data" / "app_startup.log").write_text(
            f"窗口应用已启动: {url}\n数据目录: {ROOT}\n", encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


def _edge_runtime() -> bool:
    """检测 WebView2 运行时是否可用（Win10/11 一般已预装）。"""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        ):
            return True
    except OSError:
        pass
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        ):
            return True
    except OSError:
        pass
    return False
