"""Tests for jira_session_refresh（Jira 会话自动续期，含 SSO 截胡场景）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import jira_session_refresh as refresh


BASE = "https://jira.example.com"
DINGTALK = "https://login.dingtalk.com/oauth2/challenge.htm?client_id=x"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def monotonic(self) -> float:
        return self.t


class FakeBridge:
    """模拟 WebBridge + 浏览器：SSO 重定向、点"立即登录"、表单提交。"""

    def __init__(self, sso_redirect: bool, click_works: bool = True,
                 sso_lands: str = "form") -> None:
        self.url = ""
        self.form_present = False
        self.actions = []
        self.sso_redirect = sso_redirect
        self.click_works = click_works
        self.sso_lands = sso_lands  # 点完"立即登录"落到 login.jsp 表单还是直接进 Jira

    def __call__(self, action, args, timeout=30.0):
        self.actions.append(action)
        if action == "navigate":
            if self.sso_redirect:
                self.url = DINGTALK
                self.form_present = False
            else:
                self.url = f"{BASE}/login.jsp"
                self.form_present = True
            return {}
        if action == "evaluate":
            return {"value": self._evaluate(args.get("code") or "")}
        if action == "fill":
            if not self.form_present:
                raise refresh.SessionRefreshError(
                    f"WebBridge fill 失败: element not found: {args.get('selector')}"
                )
            return {}
        if action == "click":
            if args.get("selector") == "#login-form-submit":
                self.url = f"{BASE}/secure/Dashboard.jspa"
            return {}
        if action == "cdp":
            return {
                "cookies": [
                    {"name": "JSESSIONID", "value": "fresh", "domain": ".xinmei365.com"},
                    {"name": "other", "value": "1", "domain": ".xinmei365.com"},
                ]
            }
        return {}

    def _evaluate(self, code: str):
        if code == "location.href":
            return self.url
        if "login-form-username" in code:
            return self.form_present
        if "立即登录" in code:
            if "login.dingtalk.com" in self.url and self.click_works:
                if self.sso_lands == "dashboard":
                    self.url = f"{BASE}/secure/Dashboard.jspa"
                    self.form_present = False
                else:
                    self.url = f"{BASE}/login.jsp"
                    self.form_present = True
                return True
            return False
        return None


class RefreshSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = Path(self.tmp.name) / ".env"
        self.env_path.write_text('JIRA_SESSION_COOKIE="old"\n', encoding="utf-8")
        refresh._last_attempt_at = 0.0
        refresh._last_failure_at = 0.0
        self.addCleanup(setattr, refresh, "_last_attempt_at", 0.0)
        self.addCleanup(setattr, refresh, "_last_failure_at", 0.0)

    def _run(self, bridge: FakeBridge, with_credentials: bool = True) -> str:
        env = {"JIRA_BASE_URL": BASE}
        if with_credentials:
            env.update({"JIRA_USERNAME": "u", "JIRA_PASSWORD": "p"})
        with mock.patch.object(refresh, "_bridge", bridge), mock.patch.object(
            refresh, "time", FakeClock()
        ), mock.patch.object(
            refresh, "_verify_cookie", return_value=None
        ), mock.patch.dict("os.environ", env, clear=False):
            return refresh._refresh_session_inner(self.env_path)

    def test_native_form_direct_login(self):
        bridge = FakeBridge(sso_redirect=False)
        cookie = self._run(bridge)
        self.assertIn("JSESSIONID=fresh", cookie)
        self.assertIn("fill", bridge.actions)
        self.assertIn('JSESSIONID=fresh', self.env_path.read_text(encoding="utf-8"))

    def test_sso_redirect_clicks_then_fills_form(self):
        """2026-08-19 翻车场景：login.jsp 被钉钉 SSO 截胡，自动点'立即登录'回来再填表单。"""
        bridge = FakeBridge(sso_redirect=True, sso_lands="form")
        cookie = self._run(bridge)
        self.assertIn("JSESSIONID=fresh", cookie)
        # 先点 SSO 按钮，再填表单（顺序关键）
        first_eval = bridge.actions.index("evaluate")
        first_fill = bridge.actions.index("fill")
        self.assertLess(first_eval, first_fill)

    def test_sso_passthrough_skips_form(self):
        """点完'立即登录'SSO 直接放行进 Jira：不填表单也算成功。"""
        bridge = FakeBridge(sso_redirect=True, sso_lands="dashboard")
        cookie = self._run(bridge)
        self.assertIn("JSESSIONID=fresh", cookie)
        self.assertNotIn("fill", bridge.actions)

    def test_form_never_appears_raises(self):
        bridge = FakeBridge(sso_redirect=True, click_works=False)
        with self.assertRaises(refresh.SessionRefreshError) as ctx:
            self._run(bridge)
        self.assertIn("原生表单", str(ctx.exception))

    def test_no_credentials_falls_back_to_sso_click(self):
        bridge = FakeBridge(sso_redirect=True, sso_lands="dashboard")
        cookie = self._run(bridge, with_credentials=False)
        self.assertIn("JSESSIONID=fresh", cookie)

    def test_cooldown_blocks_repeat(self):
        refresh._last_attempt_at = 1000000.0  # 远超 FakeClock 之外的现实时间语义，直接测冷却分支
        with mock.patch.object(refresh.time, "time", return_value=1000001.0):
            with self.assertRaises(refresh.SessionRefreshError) as ctx:
                refresh.refresh_session(self.env_path)
        self.assertIn("冷却", str(ctx.exception))

    def test_failure_retry_window(self):
        refresh._last_failure_at = 1000000.0
        with mock.patch.object(refresh.time, "time", return_value=1000001.0):
            with self.assertRaises(refresh.SessionRefreshError):
                refresh.refresh_session(self.env_path)


if __name__ == "__main__":
    unittest.main()
