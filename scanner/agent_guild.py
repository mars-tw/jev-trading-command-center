# -*- coding: utf-8 -*-
"""工作公會（research companion guild）：獨立 sidecar，用真實原生 CLI 子行程做公開數據研究。

這支模組「只做研究」：
  * 輸入：從 127.0.0.1:8765 的 /api/status 取公開盤面數值，白名單抽取後立刻丟棄原始回應。
  * 處理：六條研究線路 × 四個原生 CLI 家族，真的開子行程、驗 JSON、記durable 帳本。
  * 輸出：127.0.0.1:8766 的唯讀狀態 API（給遊戲寵物畫面用）+ pause/resume/capacity 控制。

它刻意「做不到」的事（安全界線）：
  * 不碰任何下單／帳戶／部位／JEV／engine.reviews，不引用 engine 物件，不寫任何交易 API。
  * 不把來源原始回應、不把供應商原始輸出（stdout/stderr）寫進磁碟、API 或日誌。
  * 不繼承任何金鑰環境變數；CLI 自己的原生登入資料由 CLI 內部處理，本程式不讀不印。
  * verdict 只代表「證據支持／反駁該線路假設」，永遠不是交易許可。
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from . import agent_cli
except ImportError:  # 直接以腳本方式載入時
    import agent_cli

SCHEMA_VERSION = 1
SERVICE_NAME = "agent-guild"

# ------------------------------------------------------------------ 併發規格
#
# 永遠對外曝光固定 12 個工位：四個家族 × 3 個槽。
# capacity（4|8|12）只限制「同時真正活著的子行程總數」，
# 每個家族最多 3 個同時執行，與 capacity 無關 ——
# 所以少一個家族時，其餘家族仍能把 8 個併發填滿。
SLOTS_PER_PROVIDER = 3
PER_PROVIDER_MAX_ACTIVE = 3
MAX_CAPACITY = 12
CAPACITY_CHOICES = (4, 8, 12)
DEFAULT_CAPACITY = 8
STALE_AFTER_MS = 5000

DEFAULT_CYCLE_SECONDS = 60
MIN_CYCLE_SECONDS = 1              # 只有測試會調到這麼低；正式預設 60
DEFAULT_SOURCE_URL = "http://127.0.0.1:8765"
DEFAULT_PORT = 8766

QUOTA_COOLDOWN_SECONDS = 15 * 60   # 額度／認證：至少 15 分鐘，不重複敲
ERROR_COOLDOWN_BASE = 30
ERROR_COOLDOWN_MAX = 600
DISCOVERY_REFRESH_SECONDS = 600

QUEUE_MAX = 96
QUEUE_TTL_SECONDS = 3600
MAX_JOB_INPUT_AGE_SECONDS = 600    # 排隊工作的輸入快照超過這個年齡就不執行（誠實標記中斷）
JOB_HISTORY_MAX = 400
EVENT_HISTORY_MAX = 600
DEDUP_HISTORY_MAX = 5000
API_JOBS_LIMIT = 60
API_EVENTS_LIMIT = 40

# ------------------------------------------------------------------ 研究線路

LANES = (
    ("market", "行情結構", "這個標的目前的行情結構仍在延續"),
    ("signal", "訊號品質", "目前的訊號品質足以採信"),
    ("risk", "風險警訊", "目前的公開數據顯示明顯風險警訊"),
    ("counterevidence", "反面證據", "有足夠反面證據可以反對順勢看法"),
    ("comparison", "同類比較", "相較同批標的，這個標的更值得注意"),
    ("summary", "重點摘要", "這些公開數據可以歸納出一致的重點"),
)
LANE_IDS = tuple(l[0] for l in LANES)
LANE_LABEL = {l[0]: l[1] for l in LANES}
LANE_HYPOTHESIS = {l[0]: l[2] for l in LANES}

# ------------------------------------------------------------ 公開輸入白名單
#
# 只收「已確認是數值的公開盤面欄位」+ 標準化代號字串，其餘一律不進記憶體。
# 帳戶／權益／部位／kelly／decisions／reviews／任意巢狀文字一律不可能通過。

SYMBOL_RE = re.compile(r"^[A-Z0-9_]{1,32}$")
MAX_SYMBOLS = 48
MAX_FEATURES_PER_SYMBOL = 12
MAX_ABS_VALUE = 1e15

LIVE_ROW_FIELDS = ("price", "change_pct_24h", "quote_volume", "book_imb", "mom_1m_pct",
                   "live_score")
CANDIDATE_FIELDS = ("price", "change_pct_24h", "quote_volume", "rule_score", "rule_strength",
                    "ob_component", "pa_component")
# 明確列舉的巢狀數值欄位（不是任意巢狀展開）
CANDIDATE_NESTED = (("orderbook", "spread_bps", "ob_spread_bps"),
                    ("orderbook", "imbalance_20", "ob_imbalance_20"),
                    ("orderbook", "imbalance_10", "ob_imbalance_10"),
                    ("orderbook", "mid", "ob_mid"),
                    ("price_action", "macd", "macd"),
                    ("price_action", "hist", "macd_hist"),
                    ("price_action", "rsi14", "rsi14"),
                    ("price_action", "atr_pct", "atr_pct"),
                    ("price_action", "vol_ratio", "vol_ratio"))

MAX_SOURCE_BYTES = 2 * 1024 * 1024
SOURCE_TIMEOUT_SECONDS = 3.0
MAX_SOURCE_AGE_SECONDS = 180
MAX_FEED_AGE_SECONDS = 120
MAX_PROMPT_CHARS = 2600


def now_ms():
    return int(time.time() * 1000)


def _num(value):
    """只接受有限的 int/float（bool 不算），回 float 或 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or abs(val) > MAX_ABS_VALUE:
        return None
    return val


def _symbol(value):
    if not isinstance(value, str):
        return None
    sym = value.strip().upper()
    return sym if SYMBOL_RE.match(sym) else None


def _fmt_num(val):
    if val == int(val) and abs(val) < 1e12:
        return str(int(val))
    return "%.6g" % val


def _parse_scanned_at(value):
    """支援 'YYYY-MM-DD HH:MM:SS UTC' 與 epoch 毫秒；回 ms 或 None。"""
    num = _num(value)
    if num is not None:
        if num > 1e12:
            return int(num)
        if num > 1e9:
            return int(num * 1000)
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            import calendar
            return int(calendar.timegm(time.strptime(text, fmt)) * 1000)
        except ValueError:
            continue
    return None


class PublicSnapshot(object):
    """已淨化的公開輸入；原始來源回應在此物件誕生前就被丟棄。"""

    __slots__ = ("ok", "reason", "symbols", "hash", "scanned_at_ms", "age_ms", "feed_age_s",
                 "fetched_at_ms")

    def __init__(self, ok, reason="", symbols=None, snapshot_hash="", scanned_at_ms=None,
                 age_ms=None, feed_age_s=None):
        self.ok = bool(ok)
        self.reason = reason
        self.symbols = list(symbols or [])
        self.hash = snapshot_hash
        self.scanned_at_ms = scanned_at_ms
        self.age_ms = age_ms
        self.feed_age_s = feed_age_s
        self.fetched_at_ms = now_ms()


def extract_public_features(doc):
    """從來源文件抽出 {symbol, features} 清單；只走白名單，不保留任何原始字串。

    回 (rows, scanned_at_ms, feed_age_s)。
    """
    rows = {}
    order = []

    def _touch(sym):
        if sym not in rows:
            if len(order) >= MAX_SYMBOLS:
                return None
            rows[sym] = {}
            order.append(sym)
        return rows[sym]

    def _put(feat, key, val):
        if feat is None or len(feat) >= MAX_FEATURES_PER_SYMBOL:
            return
        num = _num(val)
        if num is not None:
            feat[key] = num

    if not isinstance(doc, dict):
        return [], None, None
    live = doc.get("live_rows")
    if isinstance(live, list):
        for item in live[:MAX_SYMBOLS * 2]:
            if not isinstance(item, dict):
                continue
            sym = _symbol(item.get("symbol"))
            if not sym:
                continue
            feat = _touch(sym)
            for key in LIVE_ROW_FIELDS:
                _put(feat, key, item.get(key))
    cands = doc.get("candidates")
    if isinstance(cands, list):
        for item in cands[:MAX_SYMBOLS * 2]:
            if not isinstance(item, dict):
                continue
            sym = _symbol(item.get("symbol"))
            if not sym:
                continue
            feat = _touch(sym)
            for key in CANDIDATE_FIELDS:
                _put(feat, key, item.get(key))
            for parent, child, out_key in CANDIDATE_NESTED:
                sub = item.get(parent)
                if isinstance(sub, dict):
                    _put(feat, out_key, sub.get(child))
    out = [{"symbol": sym, "features": dict(sorted(rows[sym].items()))}
           for sym in order if rows[sym]]
    feed = doc.get("feed")
    feed_age = _num(feed.get("age_seconds")) if isinstance(feed, dict) else None
    return out, _parse_scanned_at(doc.get("scanned_at")), feed_age


def snapshot_hash(rows):
    blob = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _loopback_url(source_url):
    """Only the existing scanner endpoint on fixed port 8765 is a source."""
    try:
        from urllib.parse import urlsplit
    except ImportError:  # pragma: no cover
        return None
    try:
        parts = urlsplit(source_url or "")
        if (parts.scheme != "http" or parts.path not in ("", "/", "/api/status")
                or parts.query or parts.fragment or parts.username or parts.password
                or parts.hostname not in ("127.0.0.1", "localhost") or parts.port != 8765):
            return None
    except (TypeError, ValueError):
        return None
    return "http://%s:8765/api/status" % parts.hostname


def fetch_public_input(source_url=DEFAULT_SOURCE_URL, timeout=SOURCE_TIMEOUT_SECONDS,
                       max_bytes=MAX_SOURCE_BYTES, max_age_seconds=MAX_SOURCE_AGE_SECONDS,
                       urlopen=None):
    """抓 /api/status，立刻白名單淨化；原始回應只存在這個函式的區域變數中。"""
    url = _loopback_url(source_url)
    if not url:
        return PublicSnapshot(False, "來源網址不是允許的本機位址，已拒絕")
    if urlopen is None:  # pragma: no cover - 真實網路呼叫由 root 的執行時驗收
        from urllib.request import Request, HTTPRedirectHandler, build_opener

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        def urlopen_impl(u, t):
            req = Request(u, method="GET", headers={"Accept": "application/json",
                                                    "User-Agent": "agent-guild/1"})
            return build_opener(NoRedirect).open(req, timeout=t)
        urlopen = urlopen_impl
    try:
        resp = urlopen(url, timeout)
    except Exception:
        return PublicSnapshot(False, "取不到本機掃描器狀態（來源未啟動或拒絕連線）")
    try:
        try:
            raw = resp.read(max_bytes + 1)
        finally:
            try:
                resp.close()
            except Exception:
                pass
        if not raw:
            return PublicSnapshot(False, "來源回應為空")
        if len(raw) > max_bytes:
            return PublicSnapshot(False, "來源回應超過容量上限，已丟棄")
        doc = agent_cli.loads_strict(raw.decode("utf-8", "replace"))
        if not isinstance(doc, dict):
            return PublicSnapshot(False, "來源回應不是合規 JSON 物件")
        rows, scanned_ms, feed_age = extract_public_features(doc)
    except Exception:
        return PublicSnapshot(False, "來源回應無法解析，已丟棄")
    finally:
        doc = None
        raw = None
    if not rows:
        return PublicSnapshot(False, "來源沒有可用的公開數值（可能仍在暖機）")
    age_ms = None
    if scanned_ms is not None:
        age_ms = max(0, now_ms() - scanned_ms)
        if age_ms > max_age_seconds * 1000:
            return PublicSnapshot(False, "來源資料過舊（%d 秒），不派新工作" % int(age_ms / 1000),
                                  scanned_at_ms=scanned_ms, age_ms=age_ms, feed_age_s=feed_age)
    else:
        return PublicSnapshot(False, "來源沒有可信的時間戳，不派新工作")
    if feed_age is not None and feed_age > MAX_FEED_AGE_SECONDS:
        return PublicSnapshot(False, "行情推送延遲 %d 秒，不派新工作" % int(feed_age),
                              scanned_at_ms=scanned_ms, age_ms=age_ms, feed_age_s=feed_age)
    return PublicSnapshot(True, "", symbols=rows, snapshot_hash=snapshot_hash(rows),
                          scanned_at_ms=scanned_ms, age_ms=age_ms, feed_age_s=feed_age)


# ------------------------------------------------------------------ 提示文字

_PROMPT_HEAD = (
    "你是公開市場數據的研究助手。以下只有公開數值特徵，沒有任何帳戶、持倉或下單資訊。\n"
    "研究線路：%s（%s）\n"
    "線路假設：%s\n"
    "標的代號：%s\n"
    "公開數值：%s\n"
)
_PROMPT_TAIL = (
    "請判斷「這些公開數值」是支持（support）、反駁（challenge）還是不足以判斷（unclear）"
    "上述線路假設，並給出可驗證的觀察。\n"
    "只輸出一個 JSON 物件，不要有任何其他文字、說明或程式碼圍欄：\n"
    '{"verdict":"support|challenge|unclear","finding":"<=350 字的中文觀察",'
    '"evidence":["<=4 條短證據"]}\n'
    "嚴禁輸出任何交易建議、下單、方向建議、倉位或槓桿資訊；這是純觀察研究。\n"
)


def build_prompt(lane_id, symbol, features):
    """組出提示文字；內容只有線路說明、代號與數值（任何來源原文都不會進來）。"""
    sym = _symbol(symbol)
    if not sym or lane_id not in LANE_LABEL:
        return None
    pairs = []
    for key in sorted(features or {}):
        if key not in LIVE_ROW_FIELDS and key not in CANDIDATE_FIELDS and \
                key not in [n[2] for n in CANDIDATE_NESTED]:
            continue
        val = _num(features[key])
        if val is None:
            continue
        pairs.append("%s=%s" % (key, _fmt_num(val)))
    if not pairs:
        return None
    text = _PROMPT_HEAD % (lane_id, LANE_LABEL[lane_id], LANE_HYPOTHESIS[lane_id], sym,
                           ", ".join(pairs[:MAX_FEATURES_PER_SYMBOL])) + _PROMPT_TAIL
    return text[:MAX_PROMPT_CHARS]


def dedup_key(alias, symbol, lane_id, snap_hash):
    raw = "%s|%s|%s|%s" % (alias, symbol, lane_id, snap_hash)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def summarize_outcome(outcome):
    """給 API 看的精簡摘要；只用驗證過的欄位或固定失敗句子。"""
    if outcome is None:
        return ""
    if outcome.status == "completed":
        return agent_cli.clean_text("%s：%s" % (outcome.verdict, outcome.finding), 160)
    return agent_cli.clean_text(outcome.failure_reason or outcome.status, 160)


# ------------------------------------------------------------------ 獨佔檔鎖

class InstanceLock(object):
    """OS 層獨佔檔鎖，避免兩個協調者同時跑（不刪檔、不殺人）。"""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._fh = None
        self.locked = False

    def acquire(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError:
            return False
        try:
            self._fh = open(self.path, "a+b")
        except OSError:
            return False
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(1024)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
            return False
        self.locked = True
        try:
            self._fh.seek(0)
            self._fh.write(("%d\n" % os.getpid()).encode("ascii"))
            self._fh.flush()
        except OSError:
            pass
        return True

    def read_owner_pid(self):
        try:
            with open(self.path, "rb") as fh:
                return int((fh.read(32).split(b"\n")[0] or b"0").decode("ascii", "ignore") or 0)
        except (OSError, ValueError):
            return 0

    def release(self):
        if not self._fh:
            return
        try:
            if self.locked:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(1024)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None
        self.locked = False


# ------------------------------------------------------------------ 帳本

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS jobs(
         id TEXT PRIMARY KEY, lane_id TEXT NOT NULL, worker_id TEXT, alias TEXT NOT NULL,
         status TEXT NOT NULL, symbol TEXT NOT NULL, snapshot_hash TEXT, dedup_key TEXT,
         features TEXT, scanned_at_ms INTEGER,
         queued_at_ms INTEGER NOT NULL, started_at_ms INTEGER, completed_at_ms INTEGER,
         deadline_ms INTEGER, heartbeat_at_ms INTEGER, pid INTEGER,
         verdict TEXT, finding TEXT, evidence TEXT, model TEXT,
         failure_kind TEXT, failure_reason TEXT, cycle INTEGER)""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
    """CREATE TABLE IF NOT EXISTS events(
         event_id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id TEXT, job_id TEXT,
         lane_id TEXT, alias TEXT, kind TEXT NOT NULL, occurred_at_ms INTEGER NOT NULL,
         summary TEXT)""",
    "CREATE TABLE IF NOT EXISTS dedup(dedup_key TEXT PRIMARY KEY, created_at_ms INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS cycles(
         cycle INTEGER PRIMARY KEY, started_at_ms INTEGER NOT NULL, planned INTEGER,
         reason TEXT)""",
)

_COUNTER_KEYS = ("lifetime_completed", "lifetime_failed", "lifetime_interrupted")


class GuildLedger(object):
    """durable SQLite 帳本：有界保留、只存精簡結果、永不存原始輸出。

    終身計數（completed/failed/interrupted）與可修剪的 job/event 歷史分開存，
    所以修剪歷史或重啟都不會讓 XP 倒退。
    """

    def __init__(self, path):
        self.path = os.path.abspath(path)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.execute("PRAGMA synchronous=NORMAL")
            for stmt in _SCHEMA:
                self._conn.execute(stmt)
            for key in _COUNTER_KEYS + ("cycle_counter",):
                self._conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,'0')", (key,))
            self._conn.commit()

    # -- 基本 --------------------------------------------------------------
    def close(self):
        with self._lock:
            try:
                self._conn.commit()
            except sqlite3.DatabaseError:
                pass
            self._conn.close()

    def meta_get(self, key, default=""):
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key, value):
        with self._lock:
            self._conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, str(value)))
            self._conn.commit()

    def _counter(self, key):
        try:
            return int(self.meta_get(key, "0") or 0)
        except ValueError:
            return 0

    def counter_add(self, key, delta=1):
        """只加不減；XP 在任何情況下都不會倒退。"""
        if delta <= 0:
            return self._counter(key)
        with self._lock:
            cur = self._counter(key) + int(delta)
            self._conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, str(cur)))
            self._conn.commit()
        return cur

    def lifetime(self):
        return {"completed": self._counter("lifetime_completed"),
                "failed": self._counter("lifetime_failed"),
                "interrupted": self._counter("lifetime_interrupted")}

    def next_cycle(self):
        """單調遞增的 cycle 序號（重啟也不回頭）。"""
        return self.counter_add("cycle_counter", 1)

    def cycle_value(self):
        return self._counter("cycle_counter")

    def record_cycle(self, cycle, planned, reason):
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO cycles(cycle,started_at_ms,planned,reason)"
                               " VALUES(?,?,?,?)", (int(cycle), now_ms(), int(planned),
                                                    agent_cli.clean_text(reason, 120)))
            self._conn.execute("DELETE FROM cycles WHERE cycle IN (SELECT cycle FROM cycles "
                               "ORDER BY cycle DESC LIMIT -1 OFFSET 200)")
            self._conn.commit()

    # -- 去重 --------------------------------------------------------------
    def seen(self, key):
        with self._lock:
            return self._conn.execute("SELECT 1 FROM dedup WHERE dedup_key=?",
                                      (key,)).fetchone() is not None

    def remember(self, key):
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO dedup(dedup_key,created_at_ms) VALUES(?,?)",
                               (key, now_ms()))
            self._conn.commit()

    # -- 工作 --------------------------------------------------------------
    def enqueue(self, job):
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE status='queued'"
                                   ).fetchone()["c"]
            if n >= QUEUE_MAX:
                return False
            self._conn.execute(
                "INSERT OR IGNORE INTO jobs(id,lane_id,alias,status,symbol,snapshot_hash,"
                "dedup_key,features,scanned_at_ms,queued_at_ms,cycle) "
                "VALUES(?,?,?,'queued',?,?,?,?,?,?,?)",
                (job["id"], job["lane_id"], job["alias"], job["symbol"], job["snapshot_hash"],
                 job["dedup_key"],
                 json.dumps(job.get("features") or {}, sort_keys=True, separators=(",", ":")),
                 int(job.get("scanned_at_ms") or 0), job["queued_at_ms"], job.get("cycle", 0)))
            self._conn.commit()
        return True

    def queued(self, limit=QUEUE_MAX):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status='queued' "
                "ORDER BY queued_at_ms ASC, rowid ASC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def count_status(self, status):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE status=?",
                                      (status,)).fetchone()["c"]

    def mark_started(self, job_id, worker_id, pid, started_at_ms, deadline_ms):
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='running',worker_id=?,pid=?,started_at_ms=?,"
                "deadline_ms=?,heartbeat_at_ms=? WHERE id=?",
                (worker_id, int(pid or 0), int(started_at_ms), int(deadline_ms),
                 int(started_at_ms), job_id))
            self._conn.commit()

    def heartbeat(self, job_id, ts_ms):
        with self._lock:
            self._conn.execute("UPDATE jobs SET heartbeat_at_ms=? WHERE id=? AND status='running'",
                               (int(ts_ms), job_id))
            self._conn.commit()

    def finish(self, job_id, status, outcome=None):
        """One guarded transition and all lifetime/lane counters in one transaction."""
        if status not in ("completed", "failed", "timed_out", "interrupted"):
            return None
        if status == "completed" and not (outcome is not None and outcome.validated):
            return None
        ts = now_ms()
        verdict = finding = evidence = model = kind = reason = None
        if outcome is not None:
            verdict = outcome.verdict
            finding = agent_cli.clean_text(outcome.finding, agent_cli.FINDING_MAX)
            evidence = json.dumps(outcome.evidence[:agent_cli.EVIDENCE_MAX_ITEMS],
                                  ensure_ascii=False)
            model = agent_cli.clean_text(outcome.model_reported or "", 80)
            kind = outcome.failure_kind
            reason = agent_cli.clean_text(outcome.failure_reason, 160)
        with self._lock:
            with self._conn:
                row = self._conn.execute("SELECT status,lane_id FROM jobs WHERE id=?", (job_id,)).fetchone()
                # Queued work may be rejected before spawn, but can never earn XP.
                if row is None or row["status"] not in ("queued", "running"):
                    return None
                if status == "completed" and row["status"] != "running":
                    return None
                self._conn.execute(
                    "UPDATE jobs SET status=?,completed_at_ms=?,verdict=?,finding=?,evidence=?,"
                    "model=?,failure_kind=?,failure_reason=? WHERE id=?",
                    (status, ts, verdict, finding, evidence, model, kind, reason, job_id))
                key = "lifetime_" + (status if status in ("completed", "interrupted") else "failed")
                self._conn.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key=?", (key,))
                if status == "completed":
                    self._conn.execute("INSERT INTO meta(key,value) VALUES(?,'1') "
                        "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1",
                        ("lane_completed_" + row["lane_id"],))
        return ts

    def add_event(self, kind, job_id=None, worker_id=None, lane_id=None, alias=None, summary=""):
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(worker_id,job_id,lane_id,alias,kind,occurred_at_ms,summary)"
                " VALUES(?,?,?,?,?,?,?)",
                (worker_id, job_id, lane_id, alias, kind, now_ms(),
                 agent_cli.clean_text(summary, 160)))
            self._conn.commit()

    def recent_jobs(self, limit=API_JOBS_LIMIT):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, "
                "COALESCE(completed_at_ms,started_at_ms,queued_at_ms) DESC, rowid DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, limit=API_EVENTS_LIMIT):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY event_id DESC LIMIT ?",
                                      (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def lane_stats(self):
        out = {lid: {"queued": 0, "active": 0, "completed": 0, "latest": None} for lid in LANE_IDS}
        with self._lock:
            rows = self._conn.execute(
                "SELECT lane_id,status,COUNT(*) AS c FROM jobs GROUP BY lane_id,status"
            ).fetchall()
            for r in rows:
                box = out.get(r["lane_id"])
                if box is None:
                    continue
                if r["status"] == "queued":
                    box["queued"] += r["c"]
                elif r["status"] == "running":
                    box["active"] += r["c"]
            for lid in LANE_IDS:
                last = self._conn.execute(
                    "SELECT id,alias,symbol,verdict,finding,evidence,model,completed_at_ms "
                    "FROM jobs WHERE lane_id=? AND status='completed' "
                    "ORDER BY completed_at_ms DESC LIMIT 1", (lid,)).fetchone()
                if last:
                    out[lid]["latest"] = dict(last)
        # 完成數用「終身線路計數」（存在 meta，修剪歷史不影響）
        for lid in LANE_IDS:
            out[lid]["completed"] = self._counter("lane_completed_" + lid)
        return out

    # -- 復原 / 修剪 --------------------------------------------------------
    def recover(self):
        """啟動時：running -> interrupted（絕不聲稱舊 PID 還活著）；過期 queued 丟棄。"""
        with self._lock:
            with self._conn:
                rows = self._conn.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
                n = len(rows)
                if n:
                    self._conn.execute(
                        "UPDATE jobs SET status='interrupted',completed_at_ms=?,pid=NULL,"
                        "failure_kind='interrupted',failure_reason=? WHERE status='running'",
                        (now_ms(), "服務重啟，原行程已不存在，標記為中斷"))
                    self._conn.execute("UPDATE meta SET value=CAST(value AS INTEGER)+? "
                                       "WHERE key='lifetime_interrupted'", (n,))
                cutoff = now_ms() - QUEUE_TTL_SECONDS * 1000
                dropped = self._conn.execute(
                    "DELETE FROM jobs WHERE status='queued' AND queued_at_ms<?", (cutoff,)).rowcount
        if n:
            self.add_event("interrupted", summary="啟動復原：%d 件執行中工作標記為中斷" % n)
        return {"interrupted": n, "dropped_queued": max(0, dropped or 0)}

    def prune(self):
        with self._lock:
            self._conn.execute(
                "DELETE FROM jobs WHERE status NOT IN ('queued','running') AND rowid IN ("
                "SELECT rowid FROM jobs WHERE status NOT IN ('queued','running') "
                "ORDER BY COALESCE(completed_at_ms,queued_at_ms) DESC LIMIT -1 OFFSET ?)",
                (JOB_HISTORY_MAX,))
            self._conn.execute(
                "DELETE FROM events WHERE event_id IN (SELECT event_id FROM events "
                "ORDER BY event_id DESC LIMIT -1 OFFSET ?)", (EVENT_HISTORY_MAX,))
            self._conn.execute(
                "DELETE FROM dedup WHERE dedup_key IN (SELECT dedup_key FROM dedup "
                "ORDER BY created_at_ms DESC LIMIT -1 OFFSET ?)", (DEDUP_HISTORY_MAX,))
            self._conn.commit()


# ------------------------------------------------------------------ 工位

class Worker(object):
    """固定曝光的工位。狀態一定對應真實情況，不會假裝在跑。"""

    __slots__ = ("worker_id", "alias", "slot", "handle", "job", "status", "result_summary",
                 "started_at_ms", "heartbeat_at_ms", "model_label", "pid")

    def __init__(self, alias, slot):
        self.worker_id = "%s-%d" % (alias, slot)
        self.alias = alias
        self.slot = slot
        self.handle = None
        self.job = None
        self.status = "idle"
        self.result_summary = ""
        self.started_at_ms = None
        self.heartbeat_at_ms = None
        self.model_label = agent_cli.honest_model_label(
            agent_cli.SPEC_TEMPLATES.get(alias, {}).get("configured_model"), None)
        self.pid = None

    def busy(self):
        return self.handle is not None

    def clear(self):
        self.handle = None
        self.job = None
        self.pid = None
        self.started_at_ms = None
        self.heartbeat_at_ms = None


def build_workers():
    """Exactly 12 fixed worker instances; disabled aliases are metadata only."""
    workers = []
    for slot in range(1, SLOTS_PER_PROVIDER + 1):
        for alias in agent_cli.ALIAS_ORDER:
            workers.append(Worker(alias, slot))
    workers.sort(key=lambda w: (agent_cli.ALIAS_ORDER.index(w.alias), w.slot))
    return workers


# ------------------------------------------------------------------ 排程器

class GuildScheduler(object):
    """真實的研究排程器：真的開子行程、真的驗結果、真的持久化。"""

    def __init__(self, ledger, runner, capacity=DEFAULT_CAPACITY,
                 cycle_seconds=DEFAULT_CYCLE_SECONDS, source_url=DEFAULT_SOURCE_URL,
                 fetcher=None, instance_id=None):
        self.ledger = ledger
        self.runner = runner
        self.capacity = self._clamp_capacity(capacity)
        self.cycle_seconds = max(MIN_CYCLE_SECONDS, int(cycle_seconds))
        self.source_url = source_url
        self._fetch = fetcher or (lambda: fetch_public_input(source_url))
        self.instance_id = instance_id or uuid.uuid4().hex[:12]
        self.workers = build_workers()
        self._by_id = {w.worker_id: w for w in self.workers}
        self.paused = False
        self.running = False
        self.started_at_ms = now_ms()
        self.heartbeat_at_ms = now_ms()
        self.reason = "尚未開始第一輪"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._cooldown_until = {}
        self._fail_streak = {}
        self._discovery = {}
        self._discovered_at = 0.0
        self._next_cycle_at = 0.0
        self._rr_lane = 0
        self._rr_symbol = 0
        self._rr_provider = 0
        self._snapshot = None
        self._cleanup_warnings = []
        self.cycle = self.ledger.cycle_value()
        rec = self.ledger.recover()
        self.recovered = rec

    # -- 設定 --------------------------------------------------------------
    @staticmethod
    def _clamp_capacity(value):
        try:
            val = int(value)
        except (TypeError, ValueError):
            return DEFAULT_CAPACITY
        return val if val in CAPACITY_CHOICES else DEFAULT_CAPACITY

    def set_capacity(self, value):
        with self._lock:
            self.capacity = self._clamp_capacity(value)
            return self.capacity

    def pause(self):
        with self._lock:
            self.paused = True
            self.reason = "已暫停：不派新工作，等現有工作自然結束"
        self.ledger.add_event("interrupted", summary="使用者暫停派工")
        return True

    def resume(self):
        with self._lock:
            self.paused = False
            self.reason = "已恢復，等待下一輪派工"
            self._next_cycle_at = 0.0
        return True

    # -- 探測 / 冷卻 --------------------------------------------------------
    def discovery(self, force=False):
        now = time.time()
        if force or not self._discovery or now - self._discovered_at > DISCOVERY_REFRESH_SECONDS:
            try:
                self._discovery = self.runner.discover(refresh=force) or {}
            except Exception:
                self._discovery = {}
            self._discovered_at = now
        return self._discovery

    def _available_aliases(self):
        disc = self.discovery()
        out = []
        for alias in agent_cli.ALIAS_ORDER:
            info = disc.get(alias) or {}
            if info.get("available") and not self._in_cooldown(alias):
                out.append(alias)
        return out

    def _in_cooldown(self, alias):
        return self._cooldown_until.get(alias, 0) > now_ms()

    def _cooldown(self, alias, kind):
        if kind in ("quota", "auth"):
            secs = QUOTA_COOLDOWN_SECONDS
            self._fail_streak[alias] = 0
        else:
            streak = self._fail_streak.get(alias, 0) + 1
            self._fail_streak[alias] = streak
            secs = min(ERROR_COOLDOWN_MAX, ERROR_COOLDOWN_BASE * (2 ** (streak - 1)))
        self._cooldown_until[alias] = max(self._cooldown_until.get(alias, 0),
                                          now_ms() + int(secs * 1000))
        return secs

    def cooldown_left_ms(self, alias):
        return max(0, self._cooldown_until.get(alias, 0) - now_ms())

    # -- 一個節拍 ------------------------------------------------------------
    def tick(self, force_cycle=False):
        """一次非阻塞節拍：收結果 -> 必要時開新一輪 -> 盡量填滿併發。"""
        with self._lock:
            self.heartbeat_at_ms = now_ms()
            done = self._collect()
            planned = 0
            if force_cycle or self._cycle_due():
                planned = self._new_cycle()
            launched = self._launch()
            self._update_statuses()
            return {"collected": done, "planned": planned, "launched": launched,
                    "reason": self.reason}

    def _cycle_due(self):
        if self.paused:
            return False
        return time.time() >= self._next_cycle_at

    def _collect(self):
        done = 0
        for w in self.workers:
            if not w.busy():
                continue
            handle = w.handle
            try:
                outcome = handle.poll()
            except Exception:
                # Never drop a handle while an owned child might still be alive.
                try:
                    handle.terminate()
                except Exception:
                    pass
                try:
                    handle.close()
                except Exception:
                    pass
                try:
                    if handle.alive():
                        continue
                except Exception:
                    continue
                outcome = agent_cli.Outcome("failed", failure_kind="spawn_error",
                                            failure_reason=agent_cli.FAILURE_TEXT["spawn_error"])
            if outcome is None:
                ts = now_ms()
                w.heartbeat_at_ms = ts
                self.ledger.heartbeat(w.job["id"], ts)
                deadline = w.job.get("deadline_ms") or 0
                if deadline and ts > deadline + 30000:
                    try:
                        handle.terminate()
                    except Exception:
                        pass
                continue
            self._finish_worker(w, outcome)
            done += 1
        return done

    def _finish_worker(self, worker, outcome):
        job = worker.job or {}
        warn = getattr(worker.handle, "cleanup_warning", "")
        if warn:
            self._cleanup_warnings.append("%s %s" % (job.get("id", "-"), warn))
            self._cleanup_warnings = self._cleanup_warnings[-20:]
        if outcome.validated:
            status = "completed"
            self._fail_streak[worker.alias] = 0
            if outcome.model_reported:
                worker.model_label = agent_cli.honest_model_label(None, outcome.model_reported)
        elif outcome.status == "timed_out":
            status = "timed_out"
            self._cooldown(worker.alias, "timeout")
        else:
            status = "failed"
            self._cooldown(worker.alias, outcome.failure_kind or "nonzero_exit")
        transitioned = self.ledger.finish(job.get("id"), status, outcome)
        summary = summarize_outcome(outcome)
        worker.result_summary = summary
        if transitioned is not None:
            self.ledger.add_event(status, job_id=job.get("id"), worker_id=worker.worker_id,
                                  lane_id=job.get("lane_id"), alias=worker.alias, summary=summary)
        worker.clear()

    # -- 新一輪 --------------------------------------------------------------
    def _new_cycle(self):
        snap = None
        try:
            snap = self._fetch()
        except Exception:
            snap = PublicSnapshot(False, "取輸入時發生例外，不派新工作")
        self._snapshot = snap
        self._next_cycle_at = time.time() + self.cycle_seconds
        if not snap.ok:
            self.reason = snap.reason or "輸入不可用，排程等待中"
            return 0
        avail = self._available_aliases()
        if not avail:
            self.reason = "找不到可用的原生 CLI，排程等待中（daemon 仍在）"
            return 0
        queued_now = self.ledger.count_status("queued")
        target = min(QUEUE_MAX, max(self.capacity, MAX_CAPACITY))
        want = max(0, target - queued_now)
        planned = self._plan(snap, avail, want)
        self.cycle = self.ledger.next_cycle()
        self.ledger.record_cycle(self.cycle, planned,
                                 "派出 %d 件（來源 %d 秒前）" % (planned,
                                                           int((snap.age_ms or 0) / 1000)))
        self.ledger.prune()
        if planned:
            self.reason = "第 %d 輪已派 %d 件研究工作" % (self.cycle, planned)
        else:
            self.reason = "第 %d 輪沒有新題目（去重後都做過了），等下一次新資料" % self.cycle
        return planned

    def _plan(self, snap, avail, want):
        """公平輪替：線路、標的、供應商各自獨立前進，前 8 件必然涵蓋多線路多家族。"""
        if want <= 0 or not snap.symbols or not avail:
            return 0
        planned = 0
        attempts = 0
        max_attempts = max(8, want * 8)
        while planned < want and attempts < max_attempts:
            attempts += 1
            position = self._rr_provider
            provider_index = position % len(avail)
            alias = avail[provider_index]
            lane_id = LANE_IDS[(position // len(avail) + provider_index) % len(LANE_IDS)]
            row = snap.symbols[(position // (len(avail) * len(LANE_IDS))) % len(snap.symbols)]
            self._rr_provider += 1
            key = dedup_key(alias, row["symbol"], lane_id, snap.hash)
            if self.ledger.seen(key):
                continue
            prompt = build_prompt(lane_id, row["symbol"], row["features"])
            if not prompt:
                continue
            job_id = "%s-%s" % (time.strftime("%H%M%S"), uuid.uuid4().hex[:10])
            ok = self.ledger.enqueue({"id": job_id, "lane_id": lane_id, "alias": alias,
                                      "symbol": row["symbol"], "snapshot_hash": snap.hash,
                                      "dedup_key": key, "queued_at_ms": now_ms(),
                                      "features": row["features"],
                                      "scanned_at_ms": snap.scanned_at_ms or 0,
                                      "cycle": self.ledger.cycle_value() + 1})
            if not ok:
                break
            self.ledger.remember(key)
            planned += 1
        return planned

    # -- 派工 ----------------------------------------------------------------
    def _active_total(self):
        return sum(1 for w in self.workers if w.busy())

    def _active_by_alias(self, alias):
        return sum(1 for w in self.workers if w.alias == alias and w.busy())

    def _free_worker(self, alias):
        for w in self.workers:
            if w.alias == alias and not w.busy():
                return w
        return None

    def _launch(self):
        if self.paused:
            self.reason = "已暫停：不派新工作，等現有工作自然結束"
            return 0
        launched = 0
        blocked_capacity = False
        blocked_provider = False
        pending = self.ledger.queued()
        for job in pending:
            if now_ms() - job["queued_at_ms"] > QUEUE_TTL_SECONDS * 1000:
                self.ledger.finish(job["id"], "timed_out",
                    agent_cli.Outcome("timed_out", failure_kind="queue_ttl",
                                      failure_reason="Queued work expired before launch"))
                self.ledger.add_event("timed_out", job_id=job["id"], lane_id=job["lane_id"],
                                      alias=job["alias"], summary="Queued work expired before launch")
                continue
            if self._active_total() >= self.capacity:
                blocked_capacity = True
                break
            alias = job["alias"]
            info = self.discovery().get(alias) or {}
            if not info.get("available"):
                continue
            if self._in_cooldown(alias):
                continue
            if self._active_by_alias(alias) >= PER_PROVIDER_MAX_ACTIVE:
                blocked_provider = True
                continue
            worker = self._free_worker(alias)
            if worker is None:
                blocked_provider = True
                continue
            if self._start_job(worker, job):
                launched += 1
        if launched:
            self.reason = "同時執行 %d/%d 件（已派出 %d 件）" % (self._active_total(),
                                                        self.capacity, launched)
        elif blocked_capacity:
            self.reason = "容量已滿：%d/%d 同時執行中" % (self._active_total(), self.capacity)
        elif pending and blocked_provider:
            self.reason = "各家族槽位已滿，等工作結束後續派"
        elif pending:
            self.reason = "有 %d 件排隊中，但供應商都在冷卻或不可用" % len(pending)
        return launched

    def _start_job(self, worker, job):
        """真的 Popen 成功才把工作改成 running（排隊中的工作絕不假裝在跑）。"""
        scanned = int(job.get("scanned_at_ms") or 0)
        age_ms = now_ms() - scanned if scanned else None
        if age_ms is None or age_ms > MAX_JOB_INPUT_AGE_SECONDS * 1000:
            # 這題的輸入快照已經太舊：誠實標記中斷，不拿舊數據假裝在做新研究
            self.ledger.finish(job["id"], "interrupted",
                               agent_cli.Outcome("interrupted", failure_kind="stale_input",
                                                 failure_reason="輸入快照已過期，未執行"))
            self.ledger.add_event("interrupted", job_id=job["id"], lane_id=job["lane_id"],
                                  alias=job["alias"], summary="輸入快照已過期，未執行")
            return False
        features = agent_cli.loads_strict(job.get("features") or "{}")
        if not isinstance(features, dict):
            features = {}
        prompt = build_prompt(job["lane_id"], job["symbol"], features)
        if not prompt:
            self.ledger.finish(job["id"], "failed",
                               agent_cli.Outcome("failed", failure_kind="invalid_json",
                                                 failure_reason="無法組出合規提示，未執行"))
            return False
        try:
            handle = self.runner.start(job["alias"], job["id"], prompt)
        except Exception:
            handle = agent_cli.Outcome("failed", failure_kind="spawn_error",
                                       failure_reason=agent_cli.FAILURE_TEXT["spawn_error"])
        if isinstance(handle, agent_cli.Outcome):
            self.ledger.finish(job["id"], "failed", handle)
            self.ledger.add_event("failed", job_id=job["id"], lane_id=job["lane_id"],
                                  alias=job["alias"], summary=summarize_outcome(handle))
            self._cooldown(job["alias"], handle.failure_kind or "spawn_error")
            return False
        started = now_ms()
        timeout_ms = int(getattr(self.runner, "timeout_seconds",
                                 agent_cli.DEFAULT_TIMEOUT_SECONDS) * 1000)
        worker.handle = handle
        worker.job = dict(job, deadline_ms=started + timeout_ms)
        worker.pid = getattr(handle, "pid", None)
        worker.started_at_ms = started
        worker.heartbeat_at_ms = started
        worker.result_summary = ""
        self.ledger.mark_started(job["id"], worker.worker_id, worker.pid, started,
                                 started + timeout_ms)
        self.ledger.add_event("started", job_id=job["id"], worker_id=worker.worker_id,
                              lane_id=job["lane_id"], alias=job["alias"],
                              summary="%s｜%s" % (LANE_LABEL[job["lane_id"]], job["symbol"]))
        return True

    def _update_statuses(self):
        disc = self.discovery()
        for w in self.workers:
            info = disc.get(w.alias) or {}
            if info.get("disabled") or w.alias in agent_cli.DISABLED_ALIASES:
                w.status = "disabled"
                continue
            if w.busy():
                w.status = "running"
                continue
            if not info.get("available"):
                w.status = "unavailable"
                continue
            w.status = "cooldown" if self._in_cooldown(w.alias) else "idle"

    # -- 背景執行 ------------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return False
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="guild-scheduler", daemon=True)
        self._thread.start()
        return True

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                self.reason = "排程節拍發生例外，已跳過本次並繼續"
            self._stop.wait(1.0)
        self.running = False

    def stop(self, timeout=10.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self.running = False

    def shutdown(self):
        """只收自己的行程樹，把進行中的工作誠實標為 interrupted，然後關帳本。"""
        self.stop()
        with self._lock:
            for w in self.workers:
                if not w.busy():
                    continue
                job = w.job or {}
                try:
                    w.handle.close()
                except Exception:
                    pass
                warn = getattr(w.handle, "cleanup_warning", "")
                if warn:
                    self._cleanup_warnings.append("%s %s" % (job.get("id", "-"), warn))
                self.ledger.finish(job.get("id"), "interrupted",
                                   agent_cli.Outcome("interrupted", failure_kind="interrupted",
                                                     failure_reason="服務停止，已終止自有行程樹"))
                self.ledger.add_event("interrupted", job_id=job.get("id"),
                                      worker_id=w.worker_id, lane_id=job.get("lane_id"),
                                      alias=w.alias, summary="服務停止")
                w.clear()
                w.status = "idle"
            self.reason = "服務已停止"
        return {"cleanup_warnings": list(self._cleanup_warnings)}

    def run_once(self, max_wait_seconds=None):
        """--once：跑一輪、等這一輪自己派出的工作收完（有界等待），然後收工。"""
        budget = max_wait_seconds
        if budget is None:
            budget = getattr(self.runner, "timeout_seconds",
                             agent_cli.DEFAULT_TIMEOUT_SECONDS) + 30
        self.tick(force_cycle=True)
        deadline = time.time() + float(budget)
        while time.time() < deadline:
            self.tick()
            if self._active_total() == 0 and self.ledger.count_status("queued") == 0:
                break
            time.sleep(0.5)
        return self.status()

    # -- 對外狀態 ------------------------------------------------------------
    def status(self):
        """唯讀：絕不啟動任何工作。"""
        with self._lock:
            life = self.ledger.lifetime()
            lanes = self.ledger.lane_stats()
            queued = self.ledger.count_status("queued")
            active = self._active_total()
            disc = self.discovery()
            workers = []
            for w in self.workers:
                job = w.job or {}
                alive = False
                if w.handle is not None:
                    try:
                        alive = bool(w.handle.alive())
                    except Exception:
                        alive = False
                info = disc.get(w.alias) or {}
                task_label = ""
                if job:
                    task_label = "%s｜%s" % (LANE_LABEL.get(job.get("lane_id"), "-"),
                                             job.get("symbol", "-"))
                elif w.status == "cooldown":
                    task_label = "冷卻中（%d 秒後重試）" % int(self.cooldown_left_ms(w.alias) / 1000)
                elif w.status in ("unavailable", "disabled"):
                    task_label = agent_cli.clean_text(info.get("reason", ""), 80)
                workers.append({
                    "worker_id": w.worker_id,
                    "alias": w.alias,
                    "model": w.model_label,
                    "lane_id": job.get("lane_id"),
                    "job_id": job.get("id"),
                    "status": w.status,
                    "task_label": task_label,
                    "symbol": job.get("symbol"),
                    "started_at_ms": w.started_at_ms,
                    "heartbeat_at_ms": w.heartbeat_at_ms,
                    "process_alive": alive,
                    "pid": w.pid,
                    "result_summary": w.result_summary,
                })
            jobs = []
            for row in self.ledger.recent_jobs():
                jobs.append({
                    "id": row["id"],
                    "lane_id": row["lane_id"],
                    "worker_id": row["worker_id"],
                    "alias": row["alias"],
                    "status": row["status"],
                    "symbol": row["symbol"],
                    "queued_at_ms": row["queued_at_ms"],
                    "started_at_ms": row["started_at_ms"],
                    "completed_at_ms": row["completed_at_ms"],
                    "result_summary": (("%s：%s" % (row["verdict"], row["finding"]))
                                       if row["status"] == "completed" and row["verdict"]
                                       else (row["failure_reason"] or "")),
                })
            events = []
            for row in self.ledger.recent_events():
                events.append({
                    "event_id": row["event_id"],
                    "worker_id": row["worker_id"],
                    "job_id": row["job_id"],
                    "lane_id": row["lane_id"],
                    "alias": row["alias"],
                    "kind": row["kind"],
                    "occurred_at_ms": row["occurred_at_ms"],
                    "summary": row["summary"],
                })
            lane_payload = []
            for lid in LANE_IDS:
                box = lanes[lid]
                latest = box["latest"]
                latest_out = None
                if latest:
                    try:
                        ev = json.loads(latest["evidence"] or "[]")
                    except (ValueError, TypeError):
                        ev = []
                    latest_out = {"job_id": latest["id"], "alias": latest["alias"],
                                  "symbol": latest["symbol"], "verdict": latest["verdict"],
                                  "finding": latest["finding"],
                                  "evidence": ev if isinstance(ev, list) else [],
                                  "model": latest["model"],
                                  "completed_at_ms": latest["completed_at_ms"]}
                lane_payload.append({"id": lid, "label": LANE_LABEL[lid],
                                     "queued": box["queued"], "active": box["active"],
                                     "completed": box["completed"],
                                     "latest_result": latest_out})
            return {
                "schema_version": SCHEMA_VERSION,
                "instance_id": self.instance_id,
                "server_time_ms": now_ms(),
                "heartbeat_at_ms": self.heartbeat_at_ms,
                "stale_after_ms": STALE_AFTER_MS,
                "scheduler": {
                    "running": bool(self.running and not self.paused),
                    "capacity": self.capacity,
                    "max_capacity": MAX_CAPACITY,
                    "queued": queued,
                    "active": active,
                    "completed": life["completed"],
                    "failed": life["failed"],
                    "interrupted": life["interrupted"],
                    "cycle": self.cycle,
                    "started_at_ms": self.started_at_ms,
                    "reason": self.reason,
                },
                "lanes": lane_payload,
                "workers": workers,
                "jobs": jobs,
                "events": events,
                "disabled_aliases": list(agent_cli.DISABLED_ALIASES),
            }

    def health(self):
        return {"ok": True, "service": SERVICE_NAME, "pid": os.getpid(),
                "instance_id": self.instance_id, "schema_version": SCHEMA_VERSION,
                "server_time_ms": now_ms(), "heartbeat_at_ms": self.heartbeat_at_ms,
                "scheduler_running": bool(self.running and not self.paused),
                "capacity": self.capacity, "max_capacity": MAX_CAPACITY,
                "cycle": self.cycle}


# ------------------------------------------------------------------ HTTP API

ALLOWED_ORIGINS = ("http://127.0.0.1:8765", "http://localhost:8765")
MAX_BODY_BYTES = 4096
CONTROL_ACTIONS = ("pause", "resume", "capacity")
_CONTROL_KEYS = ("action", "capacity")


def apply_control(scheduler, payload):
    """純函式控制入口（HTTP 與測試共用）；回 (http_code, body_dict)。"""
    if not isinstance(payload, dict):
        return 400, {"ok": False, "reason": "body_not_object"}
    for key in payload:
        if key not in _CONTROL_KEYS:
            return 400, {"ok": False, "reason": "unknown_key"}
    action = payload.get("action")
    if not isinstance(action, str) or action not in CONTROL_ACTIONS:
        return 400, {"ok": False, "reason": "unknown_action"}
    if action == "capacity":
        cap = payload.get("capacity")
        if isinstance(cap, bool) or not isinstance(cap, int) or cap not in CAPACITY_CHOICES:
            return 400, {"ok": False, "reason": "bad_capacity"}
        return 200, {"ok": True, "action": "capacity", "capacity": scheduler.set_capacity(cap)}
    if "capacity" in payload:
        return 400, {"ok": False, "reason": "unknown_key"}
    if action == "pause":
        scheduler.pause()
    else:
        scheduler.resume()
    return 200, {"ok": True, "action": action, "paused": scheduler.paused}


def _allowed_origin(origin):
    return origin if isinstance(origin, str) and origin in ALLOWED_ORIGINS else None


def make_handler(scheduler, port=DEFAULT_PORT):
    class GuildHandler(BaseHTTPRequestHandler):
        server_version = "agent-guild/1"
        protocol_version = "HTTP/1.1"
        _scheduler = scheduler
        _port = port

        def log_message(self, fmt, *args):        # 保持安靜，不記任何請求內容
            pass

        # -- 回應 -----------------------------------------------------------
        def _send(self, code, obj, origin=None):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def _host_ok(self):
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return True
            allowed = ("127.0.0.1:%d" % self._port, "localhost:%d" % self._port,
                       "127.0.0.1", "localhost", "[::1]:%d" % self._port)
            return host in allowed

        # -- GET ------------------------------------------------------------
        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"ok": False, "reason": "host_rejected"})
            path = (self.path or "").split("?", 1)[0].rstrip("/") or "/"
            origin = _allowed_origin(self.headers.get("Origin"))
            if path == "/api/guild/status":
                return self._send(200, self._scheduler.status(), origin)
            if path == "/api/health":
                return self._send(200, self._scheduler.health(), origin)
            return self._send(404, {"ok": False, "reason": "not_found"}, origin)

        def do_OPTIONS(self):
            path = (self.path or "").split("?", 1)[0].rstrip("/") or "/"
            origin = _allowed_origin(self.headers.get("Origin"))
            if not origin:
                return self._send(403, {"ok": False, "reason": "origin_rejected"})
            if path != "/api/guild/control":
                return self._send(404, {"ok": False, "reason": "not_found"}, origin)
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "60")
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        # -- POST -----------------------------------------------------------
        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"ok": False, "reason": "host_rejected"})
            path = (self.path or "").split("?", 1)[0].rstrip("/") or "/"
            origin = _allowed_origin(self.headers.get("Origin"))
            if not origin:
                return self._send(403, {"ok": False, "reason": "origin_rejected"})
            if path != "/api/guild/control":
                return self._send(404, {"ok": False, "reason": "not_found"}, origin)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._send(415, {"ok": False, "reason": "bad_content_type"}, origin)
            if (self.headers.get("Transfer-Encoding") or "").strip().lower() == "chunked":
                return self._send(400, {"ok": False, "reason": "chunked_rejected"}, origin)
            raw_len = self.headers.get("Content-Length")
            try:
                length = int(raw_len)
            except (TypeError, ValueError):
                return self._send(400, {"ok": False, "reason": "missing_length"}, origin)
            if length < 0 or length > MAX_BODY_BYTES:
                return self._send(413, {"ok": False, "reason": "body_too_large"}, origin)
            try:
                raw = self.rfile.read(length) if length else b""
            except OSError:
                return self._send(400, {"ok": False, "reason": "read_error"}, origin)
            payload = agent_cli.loads_strict(raw.decode("utf-8", "replace")) if raw else None
            if not isinstance(payload, dict):
                return self._send(400, {"ok": False, "reason": "bad_json"}, origin)
            code, body = apply_control(self._scheduler, payload)
            return self._send(code, body, origin)

    return GuildHandler


def serve(scheduler, port=DEFAULT_PORT, host="127.0.0.1"):
    """只綁 loopback。回 (httpd, thread)。"""
    httpd = ThreadingHTTPServer((host, port), make_handler(scheduler, port))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="guild-http", daemon=True)
    thread.start()
    return httpd, thread
