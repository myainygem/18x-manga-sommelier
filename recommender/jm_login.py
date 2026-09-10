# -*- coding: utf-8 -*-
"""18comic（禁漫）浏览器登录。

策略：从本机真实 Chrome 读取 18comic 相关 cookie（含 cf_clearance），注入 Playwright
持久化浏览器上下文 → 打开 18comic 验证。cookie 存于 data/browser_profile 供适配器复用。

用法: python cli.py jm-login
注意: 读取 cookie 时 Chrome 必须关闭（文件被占用），读完后可重新打开。
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import time

from .config import ROOT, config

PROFILE_DIR = ROOT / "data" / "browser_profile"


def _chrome_cookies() -> list[dict]:
    """读取本机 Chrome 中 18comic/jmcomic 域名相关 cookie（解密后）。"""
    src = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Google", "Chrome", "User Data", "Default", "Network", "Cookies",
    )
    if not os.path.exists(src):
        raise RuntimeError(f"找不到 Chrome Cookies 文件: {src}")
    tmp = os.path.join(tempfile.gettempdir(), "eh_chrome_cookies.sqlite3")
    try:
        shutil.copy2(src, tmp)
    except PermissionError:
        raise RuntimeError(
            "Chrome 正在运行，Cookies 文件被占用。请先完全关闭 Chrome（含后台进程），"
            "再重新运行 jm-login。"
        ) from None
    try:
        import win32crypt  # noqa: F401
    except ImportError:
        raise RuntimeError("缺少 pywin32 依赖: pip install pywin32") from None
    conn = sqlite3.connect(tmp)
    conn.text_factory = bytes
    rows = conn.execute(
        "SELECT host_key, name, path, expires_utc, is_secure, is_httponly, encrypted_value "
        "FROM cookies WHERE host_key LIKE '%18comic%' OR host_key LIKE '%jmcomic%'"
    ).fetchall()
    conn.close()
    cookies = []
    decrypt_ok = False
    for host_key, name, path, expires_utc, is_secure, is_httponly, enc in rows:
        host_key = host_key.decode("utf-8", errors="replace")
        name = name.decode("utf-8", errors="replace")
        path = (path or b"/").decode("utf-8", errors="replace")
        try:
            value = win32crypt.CryptUnprotectData(bytes(enc), None, None, None, 0)[1]
            value = value.decode("utf-8", errors="replace")
            decrypt_ok = True
        except Exception:  # noqa: BLE001 —— 新版 Chrome 应用绑定加密可能解不开
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": host_key,
                "path": path,
                "secure": bool(is_secure),
                "httpOnly": bool(is_httponly),
                "expires": (expires_utc / 1e6) if expires_utc else -1,
            }
        )
    if not cookies:
        if not rows:
            raise RuntimeError("Chrome 里没有 18comic 相关 cookie（你最近没在该浏览器登录/访问过？）")
        raise RuntimeError(
            "cookie 解密失败（新版 Chrome 应用绑定加密）。替代方案：用 Cookie-Editor 扩展导出 "
            "18comic.vip 的 cookie JSON 存到 data/jm_cookies.json 后重试。"
        )
    if not decrypt_ok:
        raise RuntimeError("cookie 解密失败（应用绑定加密），请用扩展导出方案。")
    return cookies


def _manual_cookies() -> list[dict]:
    """从 data/jm_cookies.txt（Netscape 格式）或 data/jm_cookies.json 读取用户提供的 cookie。"""
    import json

    path_txt = ROOT / "data" / "jm_cookies.txt"
    if path_txt.exists():
        out = []
        for line in path_txt.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, flag, path, secure, expiry, name, value = parts[:7]
            try:
                exp = float(expiry)
            except ValueError:
                exp = -1
            out.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path or "/",
                    "secure": secure.upper() == "TRUE",
                    "httpOnly": False,
                    "expires": exp if exp > 0 else -1,
                }
            )
        if out:
            return out
    path_json = ROOT / "data" / "jm_cookies.json"
    if not path_json.exists():
        return []
    data = json.loads(path_json.read_text(encoding="utf-8"))
    out = []
    for c in data:
        out.append(
            {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False)),
                "expires": c.get("expirationDate") or c.get("expires", -1),
            }
        )
    return out


def login(timeout_s: int = 360) -> int:
    try:
        cookies = _chrome_cookies()
        source = "Chrome"
    except RuntimeError as e:
        cookies = _manual_cookies()
        if not cookies:
            print(f"无法读取 cookie: {e}")
            return 1
        source = "data/jm_cookies.json"
    print(f"已取得 {len(cookies)} 个 cookie（来源: {source}）")

    from playwright.sync_api import sync_playwright

    proxy = config.get("proxy", "")
    launch_opts = {
        "channel": "chrome",
        "headless": False,
        "locale": "zh-CN",
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ["--enable-automation"],
    }
    if proxy:
        launch_opts["proxy"] = {"server": proxy}
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print("正在打开 Chrome 窗口（若仍需验证，请点一下）...")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), **launch_opts
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        try:
            ctx.add_cookies(cookies)
        except Exception as e:  # noqa: BLE001
            print(f"注入 cookie 失败: {e}")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://18comic.vip/search/photos?search_query=gonza", timeout=60000)
        except Exception as e:  # noqa: BLE001
            print(f"页面加载异常: {type(e).__name__} {str(e)[:80]}")
        t0 = time.time()
        ok = False
        while time.time() - t0 < timeout_s:
            try:
                page.wait_for_timeout(5000)
            except Exception:  # noqa: BLE001 —— 窗口被手动关闭
                print("浏览器窗口已关闭。")
                break
            try:
                html = page.content()
                title = page.title()
            except Exception:  # noqa: BLE001
                continue
            n_photo = len(re.findall(r"/album/\d+", html)) + len(re.findall(r"/photo/\d+", html))
            el = time.time() - t0
            print(f"[{int(el)}s] title={title[:25]} | 结果链接={n_photo}", flush=True)
            if n_photo > 0 or "禁漫" in title:
                ok = True
                print("✅ 验证通过，18comic 已可访问（cookie 已持久化）。")
                break
        ctx.close()
        return 0 if ok else 1
