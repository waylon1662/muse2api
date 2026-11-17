"""固定会话（pin thread）回归测试：离线，不调 Muse 接口、不启浏览器。"""
import asyncio
import inspect
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = tempfile.TemporaryDirectory(prefix='muse-pin-test-')
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name,
                  MUSE2API_KEY='pin-test', MUSE2API_AUTO_UPDATE='0')
import app
from threads import PinnedThreads, is_thread_url, normalize_thread_url, thread_id_of


class FakePage:
    """最小 CDP page 替身：记录 js/send 调用，模拟地址栏。"""

    def __init__(self, url="https://muse.ai/thread/new", redirect_to=None):
        self.url = url
        self.redirect_to = redirect_to
        self.js_calls = []
        self.navigations = []

    def js(self, expr, **kw):
        self.js_calls.append(expr)
        if "bubbleCount" in expr and "forChat" in expr:
            return "true"  # needs_nav
        if "hydration" in expr or "Connecting" in expr:
            return "ready"
        if "length === 0" in expr:
            return "true"
        if expr.strip() == "location.href":
            return self.redirect_to or self.url
        if expr.strip().startswith("!!document.querySelector"):
            return "true"
        return "true"

    def send(self, method, params=None, **kw):
        if method == "Page.navigate":
            self.navigations.append(params["url"])
            self.url = params["url"]
        return {}


class PinnedThreadsTests(unittest.TestCase):
    def setUp(self):
        app.store.accounts = []
        app.store.tasks = {}
        self.acc_a = app.store.add_account({"hatch_sess": "a"}, "a")
        self.acc_b = app.store.add_account({"hatch_sess": "b"}, "b")
        self.pinned = PinnedThreads(str(Path(HOME.name) / f"pinned-{id(self)}.json"))
        self.patches = [
            patch.object(app, "PINNED_THREADS", self.pinned),
            patch.object(app, "GEN_LOCK", app.ConcurrencyLock(8)),
            patch.object(app, "ACCOUNT_POOL", app.AccountPoolManager(app.store)),
            patch.object(app, "_renew_and_persist", return_value=None),
            patch.object(app, "_sync_cookies"),
            patch.object(app.engine, "start"),
            patch.object(app.engine, "close_current_page"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.addCleanup(lambda: setattr(app.engine, "page", None))

    # ---------- URL 识别 ----------
    def test_thread_url_helpers(self):
        self.assertEqual(thread_id_of("https://muse.ai/thread/abc-123"), "abc-123")
        self.assertEqual(normalize_thread_url("https://www.muse.ai/thread/abc-123"),
                         "https://muse.ai/thread/abc-123")
        self.assertEqual(normalize_thread_url("https://muse.ai/thread/abc-123/"),
                         "https://muse.ai/thread/abc-123")
        self.assertIsNone(thread_id_of("https://muse.ai/thread/new"))
        self.assertIsNone(thread_id_of("https://muse.ai/thread/"))
        self.assertIsNone(thread_id_of("https://muse.ai/"))
        self.assertIsNone(thread_id_of(None))
        self.assertIsNone(thread_id_of(123))
        self.assertTrue(is_thread_url("https://muse.ai/thread/xyz"))
        self.assertFalse(is_thread_url("https://muse.ai/thread/new"))

    # ---------- 对照表存储 ----------
    def test_pinned_store_roundtrip(self):
        rec = self.pinned.save("default", "thea", self.acc_b["id"],
                               "https://muse.ai/thread/abc123")
        self.assertEqual(rec["thread_url"], "https://muse.ai/thread/abc123")
        self.assertEqual(rec["account_id"], self.acc_b["id"])
        got = self.pinned.get("default", "thea")
        self.assertEqual(got["thread_url"], "https://muse.ai/thread/abc123")
        # /thread/new 不是会话，拒绝写入
        self.assertIsNone(self.pinned.save("default", "x", self.acc_a["id"],
                                           "https://muse.ai/thread/new"))
        self.assertIsNone(self.pinned.get("default", "x"))
        # 落盘后新实例能读回
        again = PinnedThreads(self.pinned.path)
        self.assertEqual(again.get("default", "thea")["account_id"], self.acc_b["id"])
        self.assertTrue(self.pinned.delete("default", "thea"))
        self.assertIsNone(self.pinned.get("default", "thea"))
        self.assertFalse(self.pinned.delete("default", "thea"))

    def test_save_pin_thread_ignores_new_url(self):
        app.engine.last_thread_url = "https://muse.ai/thread/new"
        app._save_pin_thread({"key_id": "default", "user": "thea"}, self.acc_a["id"])
        self.assertIsNone(self.pinned.get("default", "thea"))
        app.engine.last_thread_url = "https://muse.ai/thread/abc123"
        app._save_pin_thread({"key_id": "default", "user": "thea"}, self.acc_a["id"])
        self.assertEqual(self.pinned.get("default", "thea")["thread_url"],
                         "https://muse.ai/thread/abc123")

    # ---------- 请求模型 ----------
    def test_user_field_passthrough(self):
        r = app.ChatRequest.model_validate(
            {"model": "muse-spark",
             "messages": [{"role": "user", "content": "hi"}], "user": "thea"})
        self.assertEqual(r.user, "thea")
        self.assertIsNone(app.ChatRequest.model_validate({"messages": []}).user)
        rr = app.ResponsesRequest.model_validate({"input": "hi", "user": "thea"})
        self.assertEqual(rr.user, "thea")
        # 不认识的字段仍忽略，不 422
        app.ChatRequest.model_validate({"messages": [], "nonsense": 1})

    # ---------- 解析与选号 ----------
    def test_resolve_pin_off_or_no_user(self):
        with patch.object(app.CFG, "pin_threads", False):
            self.assertIsNone(app._resolve_pin("thea"))
        self.assertIsNone(app._resolve_pin(None))
        self.assertIsNone(app._resolve_pin(""))

    def test_resolve_pin_uses_mapped_account(self):
        self.pinned.save("default", "thea", self.acc_b["id"],
                         "https://muse.ai/thread/abc123")
        pin = app._resolve_pin("thea")
        self.assertEqual(pin["account"]["id"], self.acc_b["id"])
        self.assertEqual(pin["thread_url"], "https://muse.ai/thread/abc123")
        acc, pin_arg = app._pick_account_for_chat("thea")
        self.assertEqual(acc["id"], self.acc_b["id"])
        self.assertEqual(pin_arg["thread_url"], "https://muse.ai/thread/abc123")
        self.assertEqual(pin_arg["user"], "thea")

    def test_resolve_pin_drops_mapping_when_account_gone(self):
        self.pinned.save("default", "thea", "gone-account",
                         "https://muse.ai/thread/abc123")
        pin = app._resolve_pin("thea")
        self.assertIsNone(pin["account"])
        self.assertIsNone(self.pinned.get("default", "thea"))

    def test_resolve_pin_fresh_user_picks_normally(self):
        acc, pin_arg = app._pick_account_for_chat("new-user")
        self.assertIn(acc["id"], (self.acc_a["id"], self.acc_b["id"]))
        self.assertEqual(pin_arg["user"], "new-user")
        self.assertIsNone(pin_arg["thread_url"])

    # ---------- safe_chat_stream：重试不换号 ----------
    def _run_pinned(self, pin, account_id, fake_chat):
        cookies = app.store.get_account(account_id)["cookies"]
        with patch.object(app.engine, "chat_stream", fake_chat):
            return "".join(app.safe_chat_stream(
                cookies, "hi", None, 60, account_id=account_id, pin=pin))

    def test_pinned_call_does_not_rotate_on_retry(self):
        pin = {"key_id": "default", "user": "thea",
               "thread_url": "https://muse.ai/thread/abc123"}
        calls = []
        acquire_calls = []
        real_acquire = app.ACCOUNT_POOL.acquire

        def fake_chat(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise app.MuseGenerationError("boom")
            app.engine.last_thread_url = "https://muse.ai/thread/abc123"
            yield "ok"

        def spy_acquire(*args, **kwargs):
            acquire_calls.append(kwargs)
            return real_acquire(*args, **kwargs)

        with patch.object(app.ACCOUNT_POOL, "acquire", spy_acquire):
            out = self._run_pinned(pin, self.acc_b["id"], fake_chat)
        self.assertEqual(out, "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["thread_url"], "https://muse.ai/thread/abc123")
        self.assertEqual(calls[1]["thread_url"], "https://muse.ai/thread/abc123")
        # 两次 acquire 都锁同一账号，没有任何一次 force_rotate
        self.assertTrue(all(c.get("account_id") == self.acc_b["id"] for c in acquire_calls))
        self.assertFalse(any(c.get("force_rotate") for c in acquire_calls))
        # 成功后映射已保存
        rec = self.pinned.get("default", "thea")
        self.assertEqual(rec["account_id"], self.acc_b["id"])
        self.assertEqual(rec["thread_url"], "https://muse.ai/thread/abc123")

    def test_pinned_thread_gone_falls_back_to_new_thread(self):
        self.pinned.save("default", "thea", self.acc_b["id"],
                         "https://muse.ai/thread/old999")
        pin = {"key_id": "default", "user": "thea",
               "thread_url": "https://muse.ai/thread/old999"}
        calls = []

        def fake_chat(*args, **kwargs):
            calls.append(kwargs.get("thread_url"))
            if len(calls) == 1:
                raise app.PinnedThreadGoneError("gone")
            app.engine.last_thread_url = "https://muse.ai/thread/def456"
            yield "ok"

        out = self._run_pinned(pin, self.acc_b["id"], fake_chat)
        self.assertEqual(out, "ok")
        self.assertEqual(calls, ["https://muse.ai/thread/old999", None])
        rec = self.pinned.get("default", "thea")
        self.assertEqual(rec["thread_url"], "https://muse.ai/thread/def456")
        self.assertEqual(rec["account_id"], self.acc_b["id"])

    # ---------- 每 user 串行 ----------
    def test_pin_lock_serializes_same_user(self):
        spans = []
        span_lock = threading.Lock()

        def fake_chat(*args, **kwargs):
            t0 = time.monotonic()
            time.sleep(0.3)
            with span_lock:
                spans.append((t0, time.monotonic()))
            yield "ok"

        results = {}
        pin = {"key_id": "default", "user": "thea", "thread_url": None}

        def run_one(name, acc_id):
            cookies = app.store.get_account(acc_id)["cookies"]
            results[name] = "".join(app.safe_chat_stream(
                cookies, "hi", None, 60, account_id=acc_id, pin=pin))

        # 两个调用用不同账号（账号池不串行），唯一串行的是 pin 锁；
        # mock 只开关一次，两个线程共用，避免互相还原。
        with patch.object(app.engine, "chat_stream", fake_chat):
            t1 = threading.Thread(target=run_one, args=("a", self.acc_a["id"]))
            t2 = threading.Thread(target=run_one, args=("b", self.acc_b["id"]))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
        self.assertEqual(results, {"a": "ok", "b": "ok"})
        self.assertEqual(len(spans), 2)
        spans.sort(key=lambda s: s[0])
        # 后一个开始时，前一个已结束（不重叠）
        self.assertGreaterEqual(spans[1][0], spans[0][1] - 0.05)

    # ---------- engine：reset_thread 固定分支 ----------
    def test_reset_thread_pinned_branch(self):
        # 固定：导航到保存的 URL，不要求气泡数为 0
        page = FakePage()
        app.engine.page = page
        app.engine.reset_thread(for_chat=True, pinned=True,
                                thread_url="https://muse.ai/thread/abc123")
        self.assertEqual(page.navigations, ["https://muse.ai/thread/abc123"])
        self.assertFalse(any("length === 0" in c for c in page.js_calls))
        # 不固定：导航回 /thread/new，并等气泡数为 0
        page = FakePage()
        app.engine.page = page
        app.engine.reset_thread(for_chat=True)
        self.assertEqual(page.navigations, ["https://muse.ai/thread/new"])
        self.assertTrue(any("length === 0" in c for c in page.js_calls))
        # 源码里固定分支跳过 24 气泡规则
        src = inspect.getsource(app.engine.reset_thread)
        self.assertIn("pinned ? false : bubbleCount >= 24", src)

    def test_sample_thread_url(self):
        page = FakePage(url="https://muse.ai/thread/xyz789")
        app.engine.page = page
        app.engine.last_thread_url = None
        self.assertEqual(app.engine._sample_thread_url(),
                         "https://muse.ai/thread/xyz789")
        # 还在 /thread/new 时不覆盖已采到的值
        page.url = "https://muse.ai/thread/new"
        self.assertEqual(app.engine._sample_thread_url(),
                         "https://muse.ai/thread/xyz789")

    # ---------- engine：ensure_page 落点校验 ----------
    def test_ensure_page_pinned_landing_check(self):
        acc_id = self.acc_b["id"]
        app.engine._last_http_renew = {acc_id: time.time()}
        fake = FakePage(url="https://muse.ai/thread/abc123",
                        redirect_to="https://muse.ai/thread/new")
        with patch.object(app.engine, "_close_page"), \
                patch.object(app.engine, "_open_page", return_value=fake), \
                patch.object(app.engine, "_apply_cookies"):
            app.engine.page = None
            with self.assertRaises(app.PinnedThreadGoneError):
                app.engine.ensure_page({"hatch_sess": "b"}, None, account_id=acc_id,
                                       thread_url="https://muse.ai/thread/abc123")
        # 落点正确时正常返回
        fake2 = FakePage(url="https://muse.ai/thread/abc123")
        with patch.object(app.engine, "_close_page"), \
                patch.object(app.engine, "_open_page", return_value=fake2), \
                patch.object(app.engine, "_apply_cookies"):
            app.engine.page = None
            page = app.engine.ensure_page({"hatch_sess": "b"}, None, account_id=acc_id,
                                          thread_url="https://muse.ai/thread/abc123")
            self.assertIs(page, fake2)
            self.assertEqual(app.engine.last_thread_url,
                             "https://muse.ai/thread/abc123")

    # ---------- 端点接线 ----------
    def test_chat_endpoint_passes_pin(self):
        seen = {}

        def fake_stream(*args, **kwargs):
            seen.update(kwargs)
            yield "ok"

        req = app.ChatRequest.model_validate(
            {"model": "muse-spark",
             "messages": [{"role": "user", "content": "hi"}], "user": "thea"})
        with patch.object(app, "safe_chat_stream", fake_stream):
            out = asyncio.run(app.chat_completions(req))
        self.assertEqual(out["choices"][0]["message"]["content"], "ok")
        self.assertEqual(seen["pin"]["user"], "thea")
        self.assertIsNone(seen["pin"]["thread_url"])
        self.assertIn(seen["account_id"], (self.acc_a["id"], self.acc_b["id"]))

    def test_chat_endpoint_passes_pinned_thread(self):
        self.pinned.save("default", "thea", self.acc_b["id"],
                         "https://muse.ai/thread/abc123")
        seen = {}

        def fake_stream(*args, **kwargs):
            seen.update(kwargs)
            yield "ok"

        req = app.ChatRequest.model_validate(
            {"model": "muse-spark", "stream": True,
             "messages": [{"role": "user", "content": "hi"}], "user": "thea"})
        with patch.object(app, "safe_chat_stream", fake_stream):
            resp = asyncio.run(app.chat_completions(req))
            chunks = []
            async def drain():
                async for c in resp.body_iterator:
                    chunks.append(c)
            asyncio.run(drain())
        self.assertEqual(seen["pin"]["thread_url"], "https://muse.ai/thread/abc123")
        self.assertEqual(seen["account_id"], self.acc_b["id"])
        self.assertTrue(any("ok" in c for c in chunks))


if __name__ == "__main__":
    unittest.main()
