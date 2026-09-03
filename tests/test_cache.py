"""cache 模块单元测试:命中、失效条件、LRU 淘汰、统计。"""
import os
import tempfile
import unittest

from cache import DiskCache, scan_stats, type_matches
from config import CacheCfg
from http_message import Headers

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


def make_cfg(tmp, **kw):
    defaults = dict(dir=os.path.join(tmp, "cache"), max_files=5, max_bytes=1 << 20)
    defaults.update(kw)
    return CacheCfg(**defaults)


def resp_headers(content_type="image/png", extra=None):
    h = Headers([("Content-Type", content_type), ("Content-Length", str(len(PNG)))])
    for k, v in (extra or []):
        h.append((k, v))
    return h


class TestTypeMatching(unittest.TestCase):
    def test_wildcard_and_exact(self):
        self.assertTrue(type_matches("image/png", ["image/*"]))
        self.assertTrue(type_matches("text/css; charset=utf-8", ["text/css"]))
        self.assertFalse(type_matches("text/html", ["image/*"]))


class DiskCacheTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = DiskCache(make_cfg(self._tmp.name))
        self.url = "http://corp-doc.com:18001/static/logo.png"

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, url=None):
        self.cache.put(url or self.url, resp_headers(), PNG)


class TestGetPut(DiskCacheTestCase):
    def test_roundtrip(self):
        self.put()
        got = self.cache.get(self.url)
        self.assertIsNotNone(got)
        status, headers, body = got
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG)
        self.assertEqual(headers.get("content-type"), "image/png")

    def test_miss_before_put(self):
        self.assertIsNone(self.cache.get(self.url))

    def test_put_idempotent_and_hits_counter(self):
        self.put()
        self.put()
        self.cache.get(self.url)
        stats = self.cache.stats()
        self.assertEqual(stats["files"], 1)
        self.assertEqual(stats["hits"], 1)


class TestCacheability(DiskCacheTestCase):
    def test_dynamic_content_not_cached(self):
        html = Headers([("Content-Type", "text/html")])
        self.assertFalse(self.cache.put(self.url, html, b"<p>x</p>"))

    def test_set_cookie_not_cached(self):
        h = resp_headers(extra=[("Set-Cookie", "sid=1")])
        self.assertFalse(self.cache.put(self.url, h, PNG))

    def test_cache_control_no_store(self):
        h = resp_headers(extra=[("Cache-Control", "no-store")])
        self.assertFalse(self.cache.put(self.url, h, PNG))

    def test_private_not_cached(self):
        h = resp_headers(extra=[("Cache-Control", "private")])
        self.assertFalse(self.cache.put(self.url, h, PNG))


class TestExpiry(DiskCacheTestCase):
    def test_expired_entry_not_served(self):
        self.put()
        # 手工把 meta 的过期时间改成过去
        from cache import _entry_paths
        meta_path, _ = _entry_paths(self.cache._dir, self.url)
        import json
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        meta["expires"] = "2020-01-01T00:00:00"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        self.assertIsNone(self.cache.get(self.url))

    def test_max_age_stored(self):
        import datetime
        h = resp_headers(extra=[("Cache-Control", "max-age=600")])
        self.cache.put(self.url, h, PNG)
        got = self.cache.get(self.url)
        self.assertIsNotNone(got)
        # 600 秒后必然过期
        from cache import _entry_paths
        meta_path, _ = _entry_paths(self.cache._dir, self.url)
        import json
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        expiry = datetime.datetime.fromisoformat(meta["expires"])
        self.assertGreater(expiry, datetime.datetime.now() + datetime.timedelta(minutes=9))


class TestLRUEviction(DiskCacheTestCase):
    def test_evict_oldest_beyond_max_files(self):
        self.cache = DiskCache(make_cfg(self._tmp.name, max_files=3))
        urls = [f"http://x.test/{i}.png" for i in range(3)]
        for u in urls:
            self.cache.put(u, resp_headers(), PNG)
        # 触碰 u0 使其最新,再加 u3 应淘汰 u1
        self.cache.get(urls[0])
        self.cache.put("http://x.test/3.png", resp_headers(), PNG)
        self.assertIsNone(self.cache.get(urls[1]))
        self.assertIsNotNone(self.cache.get(urls[0]))
        self.assertEqual(self.cache.stats()["files"], 3)


class TestScanStats(unittest.TestCase):
    def test_scan_counts_disk(self):
        with tempfile.TemporaryDirectory() as d:
            cache = DiskCache(CacheCfg(dir=d, max_files=100, max_bytes=1 << 20))
            cache.put("http://x.test/a.png", resp_headers(), PNG)
            cache.put("http://x.test/b.png", resp_headers(), PNG)
            stats = scan_stats(d)
            self.assertEqual(stats["files"], 2)
            self.assertEqual(stats["bytes"], 2 * len(PNG))  # 口径:body 字节


if __name__ == "__main__":
    unittest.main()
