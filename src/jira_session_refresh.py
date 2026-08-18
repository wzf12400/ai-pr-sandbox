"""Auto-refresh the Jira session cookie through the user's real browser.

The Jira deployment sits behind a DingTalk SSO gateway that only issues
session cookies to browsers. When JIRA_SESSION_COOKIE expires, this module
drives Chrome (via the local Kimi WebBridge daemon at 127.0.0.1:10086) through
the SSO flow — the browser's long-lived DingTalk login state makes it a
one-click "立即登录" — then extracts the fresh cookies over CDP, verifies them
against the REST API, persists them to .env, and updates os.environ so the
running process picks them up immediately.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

WEBBRIDGE_URL = "http://127.0.0.1:10086/command"
WEBBRIDGE_SESSION = "jira-session-refresh"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
LOGIN_WAIT_SECONDS = 45
ENV_PATH = Path(".env")
COOKIE_ENV = "JIRA_SESSION_COOKIE"
BASE_URL_ENV = "JIRA_BASE_URL"

# 同一进程内刷新冷却，避免认证失败时反复打开浏览器
_last_attempt_at = 0.0
_last_failure_at = 0.0
REFRESH_COOLDOWN_SECONDS = 600
FAILURE_RETRY_SECONDS = 120


class SessionRefreshError(RuntimeError):
    """自动续期失败（浏览器没开、扩展没连、需要人工登录等）。"""


def _bridge(action: str, args: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    request = urllib.request.Request(
        WEBBRIDGE_URL,
        data=json.dumps(
            {"action": action, "args": args, "session": WEBBRIDGE_SESSION}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError) as exc:
        raise SessionRefreshError(f"WebBridge 守护进程不可达: {exc}") from exc
    if not payload.get("ok"):
        message = (payload.get("error") or {}).get("message", "unknown")
        raise SessionRefreshError(f"WebBridge {action} 失败: {message}")
    return payload.get("data") or {}


def _evaluate(code: str) -> Any:
    data = _bridge("evaluate", {"code": code})
    return data.get("value")


def _current_url() -> str:
    value = _evaluate("location.href")
    return value if isinstance(value, str) else ""


def _click_dingtalk_login() -> bool:
    code = (
        '(()=>{const els=[...document.querySelectorAll("button,div,span,a")]'
        '.filter(e=>e.textContent.trim()==="立即登录"&&e.offsetParent!==null);'
        'if(!els.length)return false;els[els.length-1].click();return true;})()'
    )
    return bool(_evaluate(code))


def _extract_cookies() -> str:
    data = _bridge("cdp", {"method": "Network.getAllCookies", "params": {}})
    cookies = [
        c
        for c in data.get("cookies", [])
        if isinstance(c, dict) and "xinmei365" in str(c.get("domain", ""))
    ]
    if not any(c.get("name") == "JSESSIONID" for c in cookies):
        raise SessionRefreshError("浏览器里没有 JSESSIONID，可能还没登录成功")
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def _verify_cookie(base: str, cookie: str) -> None:
    request = urllib.request.Request(
        f"{base}/rest/api/2/myself",
        headers={
            "Cookie": cookie,
            "Accept": "application/json",
            "User-Agent": BROWSER_UA,
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("name"):
        raise SessionRefreshError("新 Cookie 验证失败：/myself 未返回用户身份")


def _persist_env(cookie: str, env_path: Path = ENV_PATH) -> None:
    if not env_path.is_file():
        raise SessionRefreshError(f"{env_path} 不存在，无法写回 Cookie")
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    replaced = False
    out = []
    for line in lines:
        if line.startswith(f"{COOKIE_ENV}="):
            out.append(f'{COOKIE_ENV}="{cookie}"\n')
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f'{COOKIE_ENV}="{cookie}"\n')
    env_path.write_text("".join(out), encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass


def refresh_session(env_path: Path = ENV_PATH) -> str:
    """走完整个续期流程，返回新 Cookie 字符串。失败抛 SessionRefreshError。"""
    global _last_attempt_at, _last_failure_at
    now = time.time()
    if now - _last_attempt_at < REFRESH_COOLDOWN_SECONDS:
        raise SessionRefreshError("刷新冷却中（10 分钟内已成功续期）")
    if now - _last_failure_at < FAILURE_RETRY_SECONDS:
        raise SessionRefreshError("刚失败过，2 分钟后再试")
    try:
        return _refresh_session_inner(env_path)
    except Exception:
        _last_failure_at = time.time()
        raise


def _refresh_session_inner(env_path: Path = ENV_PATH) -> str:
    global _last_attempt_at
    _last_attempt_at = time.time()

    base = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
    if not base:
        raise SessionRefreshError(f"{BASE_URL_ENV} 未设置")

    username = os.environ.get("JIRA_USERNAME", "").strip()
    password = os.environ.get("JIRA_PASSWORD", "").strip()

    if username and password:
        # 首选：Jira 原生表单登录（确定性高，不依赖钉钉 SSO 页面结构）
        _bridge(
            "navigate",
            {"url": f"{base}/login.jsp", "newTab": True,
             "group_title": "Jira 会话自动续期"},
        )
        time.sleep(4)
        _bridge("fill", {"selector": "#login-form-username", "value": username})
        _bridge("fill", {"selector": "#login-form-password", "value": password})
        _bridge("click", {"selector": "#login-form-submit"})
        deadline = time.time() + LOGIN_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(3)
            url = _current_url()
            if url.startswith(base) and "login" not in url:
                break
        else:
            raise SessionRefreshError("原生登录后未跳转，可能密码错误或需验证码")
    else:
        # 兜底：钉钉 SSO 一键登录（依赖浏览器里的钉钉登录态）
        _bridge(
            "navigate",
            {"url": f"{base}/secure/Dashboard.jspa", "newTab": True,
             "group_title": "Jira 会话自动续期"},
        )
        deadline = time.time() + LOGIN_WAIT_SECONDS
        clicked = False
        while time.time() < deadline:
            time.sleep(3)
            url = _current_url()
            if url.startswith(base) and "login" not in url:
                break
            if "login.dingtalk.com" in url and not clicked:
                clicked = _click_dingtalk_login()
        else:
            raise SessionRefreshError("等待 SSO 登录跳转超时")

    url = _current_url()
    if not url.startswith(base):
        raise SessionRefreshError(f"登录后未回到 Jira（当前 {url}），可能需人工处理")

    cookie = _extract_cookies()
    _verify_cookie(base, cookie)
    _persist_env(cookie, env_path)
    os.environ[COOKIE_ENV] = cookie
    return cookie
