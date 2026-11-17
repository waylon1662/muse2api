"""固定会话对照表：(key_id, user) -> thread URL + account_id，JSON 落盘，原子写。

只存“具体会话”的 URL（形如 https://muse.ai/thread/<id>），/thread/new 不是
一个会话，永远不存。key_id 现在不存在多 Key 的概念，单全局 Key 时取 "default"。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time

DEFAULT_KEY_ID = "default"

_THREAD_RE = re.compile(r"^https://(?:www\.)?muse\.ai/thread/([A-Za-z0-9][A-Za-z0-9_-]*)/?$")


def thread_id_of(url) -> str | None:
    """url 是具体会话 URL 时返回 thread id，否则返回 None。/thread/new 不算会话。"""
    if not isinstance(url, str):
        return None
    m = _THREAD_RE.match(url.strip())
    if not m:
        return None
    tid = m.group(1)
    return tid if tid != "new" else None


def normalize_thread_url(url) -> str | None:
    """把具体会话 URL 规范成 https://muse.ai/thread/<id>；不是会话 URL 时返回 None。"""
    tid = thread_id_of(url)
    return f"https://muse.ai/thread/{tid}" if tid else None


def is_thread_url(url) -> bool:
    return thread_id_of(url) is not None


def _read(path: str, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write(path: str, obj):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class PinnedThreads:
    """(key_id, user) -> {account_id, thread_url, updated_at} 的持久化对照表。

    文件结构：{key_id: {user: record}}，单独一个 data/pinned-threads.json，
    不塞进 accounts.json（后者每次选号/标记都会全量重写，混进去会放大写冲突）。
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        raw = _read(path, {})
        self._data: dict[str, dict[str, dict]] = raw if isinstance(raw, dict) else {}

    def get(self, key_id: str, user: str) -> dict | None:
        with self._lock:
            rec = (self._data.get(key_id) or {}).get(user)
            return dict(rec) if rec else None

    def save(self, key_id: str, user: str, account_id: str, thread_url: str) -> dict | None:
        """保存一条映射。thread_url 不是具体会话 URL 时拒绝写入，返回 None。"""
        url = normalize_thread_url(thread_url)
        if not url or not account_id:
            return None
        with self._lock:
            rec = {"key_id": key_id, "user": user, "account_id": account_id,
                   "thread_url": url, "updated_at": int(time.time())}
            self._data.setdefault(key_id, {})[user] = rec
            _write(self.path, self._data)
            return dict(rec)

    def delete(self, key_id: str, user: str) -> bool:
        with self._lock:
            bucket = self._data.get(key_id) or {}
            if user in bucket:
                del bucket[user]
                if not bucket:
                    del self._data[key_id]
                _write(self.path, self._data)
                return True
            return False

    def all(self) -> dict[str, dict[str, dict]]:
        with self._lock:
            return {k: {u: dict(r) for u, r in v.items()}
                    for k, v in self._data.items()}
