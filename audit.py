"""流量审计:SQLite 持久化 + 报表查询。

单连接 + 锁,WAL 模式;写入失败只告警不阻断转发(规格书 §8)。
add() 只取固定列集合,容忍调用方附带内部键。
"""
import os
import sqlite3
import threading

COLUMNS = ("ts", "user", "role", "method", "host", "url_path", "port",
           "decision", "rule_id", "reason", "bytes_up", "bytes_down",
           "resp_status", "cache_hit", "duration_ms")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  user TEXT, role TEXT,
  method TEXT, host TEXT, url_path TEXT, port INTEGER,
  decision TEXT,
  rule_id TEXT, reason TEXT,
  bytes_up INTEGER, bytes_down INTEGER,
  resp_status INTEGER, cache_hit INTEGER DEFAULT 0,
  duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_user ON requests(user);
CREATE INDEX IF NOT EXISTS idx_host ON requests(host);
"""


class AuditLog:
    def __init__(self, db_path):
        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- 写入 ----
    def add(self, rec: dict):
        rec = {k: rec.get(k) for k in COLUMNS}
        sql = ("INSERT INTO requests (" + ", ".join(COLUMNS) + ") VALUES ("
               + ", ".join(":" + c for c in COLUMNS) + ")")
        try:
            with self._lock:
                self._conn.execute(sql, rec)
                self._conn.commit()
        except sqlite3.Error as e:
            print(f"[audit] 写入失败(不阻断转发): {e}", file=__import__("sys").stderr)

    # ---- 查询 ----
    def _where(self, day=None, user=None, decision=None):
        conds, args = [], []
        if day:
            conds.append("ts LIKE ?")
            args.append(day + "%")
        if user:
            conds.append("user = ?")
            args.append(user)
        if decision:
            conds.append("decision = ?")
            args.append(decision)
        return (" WHERE " + " AND ".join(conds)) if conds else "", args

    def query(self, *, day=None, user=None, decision=None, limit=200):
        where, args = self._where(day, user, decision)
        sql = "SELECT * FROM requests" + where + " ORDER BY id DESC LIMIT ?"
        args = args + [limit]
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def decision_counts(self, *, day=None, user=None):
        where, args = self._where(day, user)
        sql = ("SELECT decision, COUNT(*) AS cnt FROM requests"
               + where + " GROUP BY decision")
        with self._lock:
            return {r["decision"]: r["cnt"]
                    for r in self._conn.execute(sql, args).fetchall()}

    def top(self, field, *, day=None, n=10):
        """按下行流量对 user/host 排行 → [(value, bytes_down, 请求数)]。"""
        if field not in ("user", "host"):
            raise ValueError("field 仅支持 user/host")
        where, args = self._where(day)
        sql = (f"SELECT {field} AS v, SUM(bytes_down) AS bd, COUNT(*) AS cnt "
               f"FROM requests{where} GROUP BY {field} "
               "ORDER BY bd DESC LIMIT ?")
        args = args + [n]
        with self._lock:
            return [(r["v"], r["bd"], r["cnt"])
                    for r in self._conn.execute(sql, args).fetchall()]

    def close(self):
        with self._lock:
            self._conn.close()
