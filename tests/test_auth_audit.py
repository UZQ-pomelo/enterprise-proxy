"""auth + audit 模块单元测试。"""
import base64
import os
import tempfile
import unittest

from auth import authenticate
from audit import AuditLog
from config import UserCfg
from http_message import Headers

USERS = {
    "admin": UserCfg("admin", "admin123", "admin"),
    "employee": UserCfg("employee", "emp123", "employee"),
}


def auth_header(user, pw):
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return Headers([("Proxy-Authorization", f"Basic {token}")])


class TestAuth(unittest.TestCase):
    def test_valid_credentials(self):
        self.assertEqual(authenticate(auth_header("admin", "admin123"), USERS),
                         "admin")

    def test_wrong_password(self):
        self.assertIsNone(authenticate(auth_header("admin", "nope"), USERS))

    def test_unknown_user(self):
        self.assertIsNone(authenticate(auth_header("ghost", "admin123"), USERS))

    def test_missing_header(self):
        self.assertIsNone(authenticate(Headers([]), USERS))

    def test_not_basic_scheme(self):
        h = Headers([("Proxy-Authorization", "Bearer abc")])
        self.assertIsNone(authenticate(h, USERS))

    def test_garbage_base64(self):
        h = Headers([("Proxy-Authorization", "Basic !!!not-base64!!!")])
        self.assertIsNone(authenticate(h, USERS))


REC_A = dict(ts="2026-09-03T10:00:00", user="employee", role="employee",
             method="GET", host="corp-doc.com", url_path="/doc",
             port=18001, decision="allow", rule_id="", reason="",
             bytes_up=120, bytes_down=5000, resp_status=200, cache_hit=0,
             duration_ms=12)
REC_B = dict(REC_A, ts="2026-09-03T10:01:00", host="game-site.com",
             url_path="/", decision="block_category", rule_id="category:game",
             reason="类别被禁", resp_status=403)
REC_C = dict(REC_A, ts="2026-09-04T09:00:00", user="manager", role="manager",
             host="shop.example", bytes_down=3000)


class AuditTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "audit.db")
        self.log = AuditLog(self.path)

    def tearDown(self):
        self.log.close()
        self._tmp.cleanup()


class TestAudit(AuditTestCase):
    def test_add_and_query(self):
        self.log.add(REC_A)
        self.log.add(REC_B)
        rows = self.log.query()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["host"], "game-site.com")  # 倒序

    def test_query_filters(self):
        self.log.add(REC_A)
        self.log.add(REC_B)
        self.log.add(REC_C)
        day1 = self.log.query(day="2026-09-03")
        self.assertEqual(len(day1), 2)
        by_user = self.log.query(user="manager")
        self.assertEqual(len(by_user), 1)
        blocked = self.log.query(decision="block_category")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["rule_id"], "category:game")

    def test_decision_counts(self):
        self.log.add(REC_A)
        self.log.add(REC_B)
        counts = self.log.decision_counts()
        self.assertEqual(counts["allow"], 1)
        self.assertEqual(counts["block_category"], 1)

    def test_top_by_flow(self):
        self.log.add(REC_A)
        self.log.add(REC_B)
        self.log.add(REC_C)
        top = self.log.top("host")
        self.assertEqual(len(top), 3)
        self.assertEqual(top[0][1], 5000)   # corp-doc / game-site 并列最高
        self.assertIn(top[0][0], ("corp-doc.com", "game-site.com"))
        top_users = self.log.top("user")
        self.assertEqual(top_users[0][0], "employee")   # employee 10000 字节最高
        self.assertEqual(top_users[0][1], 10000)

    def test_summary_totals(self):
        self.log.add(REC_A)   # up 120 down 5000
        self.log.add(REC_B)   # up 120 down 5000
        self.log.add(REC_C)   # up 120 down 3000
        s = self.log.summary()
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["up"], 360)
        self.assertEqual(s["down"], 13000)
        day = self.log.summary(day="2026-09-03")
        self.assertEqual(day["total"], 2)
        self.assertEqual(day["down"], 10000)

    def test_summary_empty_db(self):
        s = self.log.summary()
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["up"], 0)

    def test_missing_keys_default_to_none(self):
        rec = dict(REC_A)
        del rec["reason"]
        self.log.add(rec)  # 不应抛异常
        self.assertEqual(self.log.query()[0]["reason"], None)


if __name__ == "__main__":
    unittest.main()
