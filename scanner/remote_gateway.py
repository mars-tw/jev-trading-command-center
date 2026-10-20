# -*- coding: utf-8 -*-
"""遠端閘道：普通 HTTPS 反向代理後面的「使用者名稱＋密碼」登入與唯讀資料橋。

信任模型（單一主機信任界線）：
- 這支程式只綁 127.0.0.1。TLS、公開 DNS 與公開連接埠都由同一台主機上的
  反向代理負責；代理必須是受信任的，而且必須把 Host 設成公開來源主機。
- 身分證明「只有」工作階段 cookie。Tailscale / X-Forwarded-User / Forwarded
  之類的身分標頭永遠被忽略，絕對不能用來認證（IGNORED_IDENTITY_HEADERS）。
- 沒有公開註冊端點、沒有預設帳號、沒有自動建立擁有者；帳號存放區不存在
  就啟動失敗（fail-closed），要用 run_remote.py --setup-owner 在本機建立。
- 不讀真實 TLS / Tailscale / 供應商設定；文件與程式只用
  https://public.example.invalid 這個保留示範來源，不含任何真實 DNS。

協定界線（所有檢查都在讀 body 與跑 KDF 之前）：
- 標頭總量 <= 16 KB、數量 <= 48、拒絕 obs-fold 與重複的 Host／Origin／
  Content-Type／Content-Length／Cookie／CSRF 標頭、拒絕重複 cookie 名稱、
  拒絕 Transfer-Encoding 與不合法長度／框架。
- 路由一律完全比對，拒絕 absolute-form、反斜線、百分號編碼、穿越與查詢字串。
- body <= 4 KB，只接受有限、無重複鍵、鍵名白名單的扁平 JSON 物件。
- 連線數有界（<= 16），socket 有讀取時限，不用無上限的 ThreadingHTTPServer。
"""
import base64
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .remote_auth import (FailureBuckets, PasswordStore, PreauthStore, SessionStore, StoreError,
                          canonical_username, resolved_store_path, username_digest,
                          ABSOLUTE_SECONDS, IDLE_SECONDS, PREAUTH_TTL_SECONDS)
from .timeframe_analysis import (CANONICAL_INTERVALS, TimeframeService,
                                 antigravity_route, public_research_context)

DEFAULT_PORT = 8777
BIND_HOST = "127.0.0.1"
EXAMPLE_ORIGIN = "https://public.example.invalid"

MAX_HEADER_BYTES = 16 * 1024
MAX_HEADER_COUNT = 48
MAX_BODY_BYTES = 4096
MAX_COOKIE_BYTES = 4096
MAX_TARGET_CHARS = 256
MAX_CONCURRENT_REQUESTS = 16
SOCKET_TIMEOUT_SECONDS = 15.0

SESSION_COOKIE = "__Host-jev-session"
PREAUTH_COOKIE = "__Host-jev-preauth"
CSRF_HEADER = "X-JEV-CSRF"
SINGLE_HEADERS = ("Host", "Origin", "Content-Type", "Content-Length", "Cookie", CSRF_HEADER)

# 這些標頭永遠不會被當成身分；密碼模式的身分證明只有工作階段 cookie。
IGNORED_IDENTITY_HEADERS = (
    "Tailscale-User-Login", "Tailscale-User-Name", "Tailscale-User-Profile-Pic",
    "X-Forwarded-User", "X-Forwarded-Email", "X-Forwarded-Preferred-Username",
    "X-Auth-Request-User", "X-Auth-Request-Email", "X-Remote-User", "X-Authenticated-User",
    "Forwarded", "X-Forwarded-For",
)

ALLOWED_UPSTREAM = "http://127.0.0.1:8765"
ALLOWED_COMPANION = "http://127.0.0.1:8766"
UPSTREAM_TIMEOUT_SECONDS = 3.0
UPSTREAM_MAX_BYTES = 2 * 1024 * 1024
UPSTREAM_MAX_JSON_DEPTH = 64
UPSTREAM_MAP = {
    "/api/status": ("upstream", "/api/status"),
    "/api/health": ("upstream", "/api/health"),
    "/api/guild/status": ("companion", "/api/guild/status"),
    "/api/guild/health": ("companion", "/api/health"),
}

PRE_AUTH_ROUTES = {
    "/login": ("login", "text/html; charset=utf-8"),
    "/login.js": ("login.js", "text/javascript; charset=utf-8"),
    "/login.css": ("login.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
LOGIN_PATH = "/login"
STAGE_ROUTES = ("/", "/stage.html")
STATIC_EXT = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}
# 登入樣板含 CSRF 佔位字串，永遠不以靜態檔原樣供應。
STATIC_DENY_NAMES = frozenset({"login.html"})

# 既有 stage.html 的固定靜態版本查詢；供應時安全移除（原始檔不動）。
STAGE_VERSION_REFS = (("/three-viz.js?v=17", "/three-viz.js"),
                      ("/line-workers.js?v=4", "/line-workers.js"))
STAGE_INJECT_ANCHORS = ('<script>window.__vizMode', '<script type="importmap">', "<script ")
REMOTE_CLIENT_TAG = '<script src="/remote-client.js"></script>\n'

DEMO_CONTROL_ACTIONS = ("pause_new", "resume")
DEMO_GUILD_ACTIONS = ("pause", "resume")
HEADER_GUARD_CODES = {"headers_too_large": 431, "too_many_headers": 431,
                      "cookie_too_large": 431, "transfer_encoding": 400}

_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.I | re.S)
_HOST_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class ConfigError(Exception):
    """啟動設定錯誤；只帶固定代碼。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class UpstreamError(Exception):
    """唯讀上游讀取失敗；只帶固定代碼，不含上游回應內容。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ 設定

def parse_public_origin(raw):
    """只接受 https://host[:port]；路徑／查詢／片段／使用者資訊一律拒絕。

    回 (origin, allowed_hosts)。allowed_hosts 是 Host 標頭的完全比對集合。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError("public_origin_required")
    text = raw.strip()
    if any(ch.isspace() for ch in text) or len(text) > 255:
        raise ConfigError("public_origin_malformed")
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        raise ConfigError("public_origin_malformed")
    if parsed.scheme != "https":
        raise ConfigError("public_origin_not_https")
    if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ConfigError("public_origin_malformed")
    if "@" in parsed.netloc or "[" in parsed.netloc:
        raise ConfigError("public_origin_malformed")
    host = (parsed.hostname or "").lower()
    if not host or len(host) > 253 or host.endswith("."):
        raise ConfigError("public_origin_malformed")
    if not all(_HOST_LABEL_RE.match(label) for label in host.split(".")):
        raise ConfigError("public_origin_malformed")
    try:
        port = parsed.port
    except ValueError:
        raise ConfigError("public_origin_malformed")
    if port is not None and not (1 <= port <= 65535):
        raise ConfigError("public_origin_malformed")
    if port in (None, 443):
        return "https://" + host, frozenset((host, host + ":443"))
    return "https://%s:%d" % (host, port), frozenset(("%s:%d" % (host, port),))


class GatewayConfig:
    """公開密碼模式的啟動設定；任何缺漏或畸形都在啟動時就失敗。"""

    def __init__(self, public_origin, auth_store, port=DEFAULT_PORT,
                 upstream=None, companion=None, market_analysis=False,
                 enable_antigravity=False):
        self.public_origin, self.allowed_hosts = parse_public_origin(public_origin)
        try:
            self.auth_store = resolved_store_path(auth_store)
        except StoreError as exc:
            raise ConfigError(exc.code)
        if isinstance(port, bool) or not isinstance(port, int):
            raise ConfigError("port_invalid")
        if port != 0 and not (1024 <= port <= 65535):
            raise ConfigError("port_invalid")
        self.port = port
        self.upstream = _exact_base(upstream, ALLOWED_UPSTREAM, "upstream_not_allowed")
        self.companion = _exact_base(companion, ALLOWED_COMPANION, "companion_not_allowed")
        if not isinstance(market_analysis, bool):
            raise ConfigError("market_analysis_invalid")
        if not isinstance(enable_antigravity, bool):
            raise ConfigError("antigravity_invalid")
        if market_analysis and self.upstream is None and self.companion is None:
            raise ConfigError("market_analysis_requires_real_mode")
        self.market_analysis = market_analysis
        self.enable_antigravity = enable_antigravity

    @property
    def demo(self):
        """預設（也是唯一的零 IO 模式）：自帶 DemoEngine，不碰任何上游。"""
        return self.upstream is None and self.companion is None

    @property
    def mode(self):
        return "DEMO" if self.demo else "READ_ONLY_REAL"


def _exact_base(value, allowed, code):
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() != allowed:
        raise ConfigError(code)
    return allowed


# ------------------------------------------------------------------ 唯讀上游

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒絕所有重導：回 None 讓 urllib 直接把 3xx 當錯誤丟出。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_fetcher(url, timeout=UPSTREAM_TIMEOUT_SECONDS, max_bytes=UPSTREAM_MAX_BYTES):
    """固定 URL 的唯讀 GET：不帶標頭／cookie／Authorization，不走代理，不跟重導。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    request = urllib.request.Request(url, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise UpstreamError("upstream_status")
            ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise UpstreamError("upstream_content_type")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                text = str(declared).strip()
                if not text.isdigit() or int(text) > max_bytes:
                    raise UpstreamError("upstream_too_large")
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise UpstreamError("upstream_redirect")
        raise UpstreamError("upstream_status")
    except UpstreamError:
        raise
    except Exception:
        raise UpstreamError("upstream_unreachable")
    if len(raw) > max_bytes:
        raise UpstreamError("upstream_too_large")
    return raw


def _reject_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate_key")
        seen[key] = value
    return seen


def _reject_constant(_name):
    raise ValueError("non_finite_literal")


def _strict_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non_finite_number")
    return value


def _upstream_values_are_safe(root):
    """Reject non-finite numbers and unreasonable nesting before re-serialization."""
    pending = [(root, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, float) and not math.isfinite(value):
            return False
        if isinstance(value, dict):
            if depth >= UPSTREAM_MAX_JSON_DEPTH and value:
                return False
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            if depth >= UPSTREAM_MAX_JSON_DEPTH and value:
                return False
            pending.extend((item, depth + 1) for item in value)
    return True


def parse_json_object(raw, allowed_keys=None, max_keys=16):
    """回 (obj, 錯誤代碼)。只接受有限、無重複鍵、鍵名白名單的扁平物件。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "bad_encoding"
    try:
        obj = json.loads(text, parse_constant=_reject_constant,
                         object_pairs_hook=_reject_duplicate_keys)
    except ValueError:
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_shape"
    if len(obj) > max_keys:
        return None, "too_many_keys"
    for key, value in obj.items():
        if allowed_keys is not None and key not in allowed_keys:
            return None, "unknown_key"
        if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
            return None, "bad_value_type"
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return None, "bad_value_type"
    return obj, None


def parse_upstream_json(raw):
    """上游回應一律重新解析＋重新序列化，不原樣轉傳位元組。"""
    try:
        obj = json.loads(raw.decode("utf-8"), parse_float=_strict_float,
                         parse_constant=_reject_constant,
                         object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, RecursionError, OverflowError):
        raise UpstreamError("upstream_bad_json")
    if not isinstance(obj, dict):
        raise UpstreamError("upstream_bad_shape")
    if not _upstream_values_are_safe(obj):
        raise UpstreamError("upstream_bad_json")
    return obj


# ------------------------------------------------------------------ 閘道狀態

class Gateway:
    """把設定、存放區、工作階段與資料來源綁在一起；handler 只做協定檢查。"""

    def __init__(self, config, webroot, password_store=None, session_store=None,
                 preauth_store=None, buckets=None, clock=None, fetcher=None,
                 demo_engine=None, analysis_service=None):
        self.config = config
        self.webroot = os.path.abspath(webroot)
        self._real_webroot = os.path.realpath(self.webroot)
        tick = clock or time.monotonic
        self.passwords = password_store or PasswordStore(config.auth_store)
        if not self.passwords.exists():
            raise ConfigError("auth_store_missing")
        self.sessions = session_store or SessionStore(clock=tick)
        self.preauth = preauth_store or PreauthStore(clock=tick)
        self.buckets = buckets or FailureBuckets(clock=tick)
        self.fetcher = fetcher or default_fetcher
        self.bound_port = None
        self._lock = threading.Lock()
        self._stage = None
        self._engine = demo_engine
        if config.demo and self._engine is None:
            from .demo_engine import DemoEngine        # DEMO 自帶合成引擎，零 IO
            self._engine = DemoEngine(port=config.port)
        self.analysis_service = analysis_service or TimeframeService(
            demo=config.demo, enabled=getattr(config, "market_analysis", False))
        self._analysis_symbol = None
        self._analysis_last = None

    # ---------- 共用安全標頭 ----------
    def csp(self, script_hashes=()):
        return content_security_policy(script_hashes)

    # ---------- 靜態檔 ----------
    def read_web(self, name):
        path = os.path.join(self.webroot, name)
        with open(path, "rb") as handle:
            return handle.read()

    def static_file(self, route):
        """把路由轉成 web/ 底下的實體檔案；不合格回 None。"""
        rel = route.lstrip("/")
        if not rel or rel.endswith("/"):
            return None
        parts = [part for part in rel.split("/") if part]
        if not parts or any(part.startswith(".") or part in (".", "..") for part in parts):
            return None
        name = parts[-1]
        if name in STATIC_DENY_NAMES:
            return None
        ctype = STATIC_EXT.get(os.path.splitext(name)[1].lower())
        if ctype is None:
            return None
        probe = os.path.join(self._real_webroot, *parts)
        walk = self._real_webroot
        for part in parts:                              # 沿途每一段都不能是連結／重解析點
            walk = os.path.join(walk, part)
            if os.path.islink(walk):
                return None
        target = os.path.realpath(probe)
        if os.path.normcase(target) != os.path.normcase(probe):
            return None
        if not target.startswith(self._real_webroot + os.sep):
            return None
        try:
            if not stat.S_ISREG(os.lstat(target).st_mode):
                return None
        except OSError:
            return None
        return target, ctype

    # ---------- 供應用的 stage（原始檔不動） ----------
    def served_stage(self):
        """回 (html_bytes, script_hashes)。移除固定版本查詢並注入 remote-client。"""
        with self._lock:
            if self._stage is not None:
                return self._stage
            html = self.read_web("stage.html").decode("utf-8")
            for old, new in STAGE_VERSION_REFS:
                html = html.replace(old, new)
            index = min((html.index(anchor) for anchor in STAGE_INJECT_ANCHORS
                         if anchor in html), default=-1)
            if index < 0:
                raise ConfigError("stage_anchor_missing")
            html = html[:index] + REMOTE_CLIENT_TAG + html[index:]
            body = html.encode("utf-8")
            hashes = script_hashes(html)                # CSP 雜湊由「供應內容」重算
            self._stage = (body, hashes)
            return self._stage

    def bootstrap_js(self):
        """遠端 bootstrap 一律同源：原生伴跑來源永遠是空字串，手機不會去連 8766。"""
        if self.config.demo:
            return ('window.__JEV_DEMO__=true;window.__JEV_MODE__="DEMO";'
                    'window.__JEV_DEMO_INFO__={mode:"DEMO",synthetic:true,simulated:true,'
                    'source:"DEMO",provider:"DEMO",network_allowed:false,native_cli_count:0,'
                    'real_cli_agents:0,demo_agents:8,jev_api_called:false};'
                    'window.__JEV_NATIVE_ORIGIN__="";'
                    'window.__JEV_REMOTE__=true;window.__JEV_READ_ONLY__=false;')
        return ('window.__JEV_DEMO__=false;window.__JEV_MODE__="READ_ONLY_REAL";'
                'window.__JEV_DEMO_INFO__=null;window.__JEV_NATIVE_ORIGIN__="";'
                'window.__JEV_REMOTE__=true;window.__JEV_READ_ONLY__=true;')

    # ---------- 資料 ----------
    @property
    def engine(self):
        return self._engine

    def demo_data(self, route):
        engine = self._engine
        if route == "/api/status":
            return engine.status()
        if route == "/api/guild/status":
            return engine.guild()
        return engine.health()

    def upstream_data(self, route):
        """唯讀對應：固定四條 GET，沒有任何財務／排程 POST 轉送。"""
        which, path = UPSTREAM_MAP[route]
        base = self.config.upstream if which == "upstream" else self.config.companion
        if base is None:
            raise UpstreamError("upstream_not_configured")
        payload = parse_upstream_json(self.fetcher(base + path))
        payload["remote_read_only"] = True
        payload["remote_mode"] = "READ_ONLY_REAL"
        return payload

    # ---------- 多週期研究（與交易／排程完全分離） ----------
    def _analysis_connector(self, snapshot):
        context = None
        try:
            if isinstance(snapshot, dict) and isinstance(snapshot.get("frames"), dict):
                context = public_research_context(snapshot)
        except Exception:
            context = None
        frame_count = len(context["frames"]) if context is not None else 0
        context_ready = frame_count == len(CANONICAL_INTERVALS)
        connector = antigravity_route(context or {})
        enabled = getattr(self.config, "enable_antigravity", False)
        if context is not None:
            context_capability = dict(context.get("capability") or {})
            context_capability["connector_enabled"] = enabled
            context["capability"] = context_capability
        capability = dict(connector.get("capability") or {})
        capability["connector_enabled"] = enabled
        connector.update({
            "protocol_id": "AGY",
            "display_name": "Antigravity",
            "version": "1.4.2",
            "enabled": enabled,
            "status": "capability_unverified" if enabled else "disabled",
            "reason": "capability_unverified" if enabled else "connector_disabled",
            "context_ready": context_ready,
            "frame_count": frame_count,
            "research_request_enabled": False,
            "native_invocation_available": False,
            "capability": capability,
            "context": context if enabled and context_ready else None,
        })
        return connector

    def _analysis_response(self, snapshot):
        payload = dict(snapshot)
        payload["source"] = "DEMO" if self.config.demo else "PUBLIC_BINANCE"
        payload["antigravity"] = self._analysis_connector(payload)
        return payload

    def analysis_snapshot(self):
        """只讀目前選取項目的快照；不會排程、抓資料或啟動執行緒。"""
        with self._lock:
            symbol = self._analysis_symbol
            last = self._analysis_last
        if symbol is None:
            snapshot = {
                "symbol": None,
                "status": "unknown",
                "reason": "missing_source",
                "fetched_at": None,
                "server_time": None,
                "stale": False,
                "analysis_only": True,
                "execution_authority": False,
            }
        else:
            snapshot = self.analysis_service.snapshot(symbol)
            if (snapshot.get("status") == "unknown" and
                    isinstance(last, dict) and last.get("symbol") == symbol):
                snapshot = dict(last)
        return self._analysis_response(snapshot)

    def select_analysis(self, symbol):
        result = self.analysis_service.select(symbol, owner=True, csrf_valid=True)
        if result.get("reason") not in ("invalid_symbol", "forbidden"):
            with self._lock:
                self._analysis_symbol = symbol
                self._analysis_last = dict(result)
        return self._analysis_response(result)

    def close(self):
        close = getattr(self.analysis_service, "close", None)
        if callable(close):
            close()


def content_security_policy(script_hashes=()):
    """script-src 只有 'self' 加明確雜湊，永遠沒有 unsafe-inline；禁止被嵌框。"""
    allow = "'self'"
    for item in sorted(script_hashes):
        allow += " " + item
    return ("default-src 'none'; script-src " + allow + "; connect-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
            "media-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'")


def script_hashes(html):
    """從實際供應的 HTML 取出所有行內 script（含 importmap）的 sha256 CSP 雜湊。"""
    found = set()
    for content in _SCRIPT_RE.findall(html):
        if content.strip():
            digest = hashlib.sha256(content.encode("utf-8")).digest()
            found.add("'sha256-" + base64.b64encode(digest).decode("ascii") + "'")
    return frozenset(found)


# ------------------------------------------------------------------ HTTP

class BoundedThreadingServer(ThreadingHTTPServer):
    """有界併發：超過上限直接關閉連線，不會無限開執行緒。"""

    daemon_threads = True
    request_queue_size = 32
    max_concurrent = MAX_CONCURRENT_REQUESTS

    def __init__(self, address, handler_cls):
        self._slots = threading.BoundedSemaphore(self.max_concurrent)
        ThreadingHTTPServer.__init__(self, address, handler_cls)

    def process_request(self, request, client_address):
        if not self._slots.acquire(timeout=0.25):
            try:
                self.close_request(request)
            finally:
                return
        ThreadingHTTPServer.process_request(self, request, client_address)

    def shutdown_request(self, request):
        try:
            ThreadingHTTPServer.shutdown_request(self, request)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        pass                                            # 不吐原始例外或堆疊


class RemoteHandler(BaseHTTPRequestHandler):
    """協定檢查層；gateway 由 build_server 以動態子類別綁定。"""

    server_version = "JEVRemote"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_SECONDS                    # socket 讀取時限
    gateway = None

    def log_message(self, fmt, *args):                  # 不記錄路徑／標頭／cookie
        pass

    def log_error(self, fmt, *args):
        pass

    # ---------- 回應 ----------
    def _send(self, body, content_type, code=200, extra=(), script_hash_set=()):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", self.gateway.csp(script_hash_set))
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Connection", "close")
        for name, value in extra:
            self.send_header(name, value)
        self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, code=200, extra=()):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", code, extra=extra)

    def deny(self, code, reason):
        """固定代碼字串；不回原始例外、標頭、路徑或憑證內容。"""
        self.json({"ok": False, "reason": str(reason)[:48]}, code=code)

    def unauthenticated(self, route):
        """API 回固定 401 JSON；頁面只會導向 /login（固定字串，無開放導向）。"""
        if route.startswith("/api/"):
            return self.json({"ok": False, "reason": "auth_required"}, code=401)
        self._send(b"", "text/plain; charset=utf-8", 302, extra=(("Location", LOGIN_PATH),))

    # ---------- 檢查 ----------
    def _header_guard(self):
        try:
            raw = self.headers.as_bytes()
        except Exception:
            return "bad_headers"
        if len(raw) + len(self.requestline) > MAX_HEADER_BYTES:
            return "headers_too_large"
        keys = self.headers.keys()
        if len(keys) > MAX_HEADER_COUNT:
            return "too_many_headers"
        for name in keys:
            for value in self.headers.get_all(name) or []:
                if "\n" in value or "\r" in value:      # obs-fold / 注入
                    return "obs_fold"
        for name in SINGLE_HEADERS:
            if len(self.headers.get_all(name) or []) > 1:
                return "duplicate_header"
        if self.headers.get("Transfer-Encoding") is not None:
            return "transfer_encoding"
        if len(self.headers.get("Cookie") or "") > MAX_COOKIE_BYTES:
            return "cookie_too_large"
        return None

    def _cookies(self):
        """回 (dict, 錯誤代碼)；重複 cookie 名稱一律拒絕。"""
        raw = self.headers.get("Cookie")
        if not raw:
            return {}, None
        jar = {}
        for part in raw.split(";"):
            item = part.strip()
            if not item:
                continue
            if "=" not in item:
                return None, "bad_cookie"
            name, _, value = item.partition("=")
            name = name.strip()
            if not name or name in jar:
                return None, "duplicate_cookie"
            jar[name] = value.strip()
        return jar, None

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.gateway.config.allowed_hosts

    def _origin_ok(self):
        origin = (self.headers.get("Origin") or "").strip()
        return origin == self.gateway.config.public_origin

    def _route(self):
        """完全比對用的路由；任何可疑形式都回 None。"""
        raw_line = self.raw_requestline
        if not isinstance(raw_line, (bytes, bytearray)):
            return None
        try:
            line = bytes(raw_line).decode("ascii")
        except UnicodeDecodeError:
            return None
        if not line.endswith("\r\n"):
            return None
        words = line[:-2].split(" ")
        if len(words) != 3 or not all(words) or words[0] != self.command:
            return None
        raw_target = words[1]
        target = self.path
        if not isinstance(target, str) or not target or len(target) > MAX_TARGET_CHARS:
            return None
        if raw_target != target or len(raw_target) > MAX_TARGET_CHARS:
            return None
        if not target.startswith("/") or target.startswith("//"):
            return None
        for bad in ("?", "#", "\\", "%", "..", ";"):
            if bad in target:
                return None
        if any(ord(ch) < 0x21 or ord(ch) > 0x7e for ch in target):
            return None
        if target != "/" and target.endswith("/"):
            return None
        return target

    def _session(self):
        jar, err = self._cookies()
        if err:
            return None, err
        record = self.gateway.sessions.validate(jar.get(SESSION_COOKIE))
        return record, None

    def _read_json(self, allowed_keys, max_bytes=MAX_BODY_BYTES):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return None, (415, "content_type_required")
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            return None, (411, "length_required")
        text = str(raw_len).strip()
        if not text.isdigit() or (len(text) > 1 and text.startswith("0")):
            return None, (400, "invalid_length")
        length = int(text)
        if length > max_bytes:
            return None, (413, "body_too_large")
        raw = self.rfile.read(length) if length else b""
        if len(raw) != length:
            return None, (400, "invalid_framing")
        obj, err = parse_json_object(raw, allowed_keys)
        if err:
            return None, (400, err)
        return obj, None

    # ---------- 分派 ----------
    def _dispatch(self, method):
        err = self._header_guard()
        if err:
            return self.deny(HEADER_GUARD_CODES.get(err, 400), err)
        if not self._host_ok():
            return self.deny(403, "bad_host")
        route = self._route()
        if route is None:
            return self.deny(404, "not_found")
        if method == "POST":
            return self._post(route)
        return self._get(route)

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self.deny(405, "method_not_allowed")

    def do_DELETE(self):
        self.deny(405, "method_not_allowed")

    def do_PATCH(self):
        self.deny(405, "method_not_allowed")

    def do_OPTIONS(self):
        self.deny(405, "method_not_allowed")

    # ---------- GET ----------
    def _get(self, route):
        if route in PRE_AUTH_ROUTES:
            return self._pre_auth_asset(route)
        record, err = self._session()
        if err:
            return self.deny(400, err)
        if record is None:
            return self.unauthenticated(route)
        if route in STAGE_ROUTES:
            body, hashes = self.gateway.served_stage()
            return self._send(body, "text/html; charset=utf-8", script_hash_set=hashes)
        if route == "/api/auth/session":
            return self.json({"ok": True, "csrf": record["csrf"],
                              "mode": self.gateway.config.mode,
                              "read_only": not self.gateway.config.demo,
                              "demo": self.gateway.config.demo,
                              "idle_seconds": IDLE_SECONDS,
                              "absolute_seconds": ABSOLUTE_SECONDS})
        if route == "/api/bootstrap.js":
            return self._send(self.gateway.bootstrap_js().encode("utf-8"),
                              "text/javascript; charset=utf-8")
        if route == "/api/analysis/timeframes":
            return self.json(self.gateway.analysis_snapshot())
        if route in UPSTREAM_MAP:
            return self._data(route)
        if route.startswith("/api/"):
            return self.deny(404, "unknown_endpoint")
        return self._static(route)

    def _pre_auth_asset(self, route):
        """登入前只供應 /login、/login.js、/login.css、/favicon.svg。"""
        name, ctype = PRE_AUTH_ROUTES[route]
        if route != LOGIN_PATH:
            try:
                body = self.gateway.read_web(name)
            except OSError:
                return self.deny(404, "not_found")
            return self._send(body, ctype)
        token, csrf = self.gateway.preauth.issue()
        try:
            template = self.gateway.read_web("login.html").decode("utf-8")
        except (OSError, UnicodeError):
            return self.deny(500, "login_unavailable")
        html = template.replace("__CSRF__", csrf)
        cookie = ("%s=%s; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=%d"
                  % (PREAUTH_COOKIE, token, PREAUTH_TTL_SECONDS))
        self._send(html.encode("utf-8"), ctype, extra=(("Set-Cookie", cookie),),
                   script_hash_set=script_hashes(html))

    def _static(self, route):
        found = self.gateway.static_file(route)
        if not found:
            return self.deny(404, "not_found")
        path, ctype = found
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            return self.deny(404, "not_found")
        self._send(body, ctype)

    def _data(self, route):
        if self.gateway.config.demo:
            return self.json(self.gateway.demo_data(route))
        try:
            return self.json(self.gateway.upstream_data(route))
        except UpstreamError as exc:
            return self.json({"ok": False, "reason": exc.code, "remote_read_only": True},
                             code=502)
        except Exception:
            return self.json({"ok": False, "reason": "upstream_unreachable",
                              "remote_read_only": True}, code=502)

    # ---------- POST ----------
    def _post(self, route):
        if not self._origin_ok():
            return self.deny(403, "origin_required")
        if route == "/api/auth/login":
            return self._login()
        record, err = self._session()
        if err:
            return self.deny(400, err)
        if record is None:
            return self.json({"ok": False, "reason": "auth_required"}, code=401)
        if not self.gateway.sessions.csrf_ok(record, self.headers.get(CSRF_HEADER)):
            return self.deny(403, "csrf_required")
        if route == "/api/auth/logout":
            return self._logout()
        if route == "/api/analysis/select":
            payload, err = self._read_json(("symbol",))
            if err:
                return self.deny(*err)
            symbol = payload.get("symbol")
            if not isinstance(symbol, str):
                return self.deny(400, "invalid_symbol")
            result = self.gateway.select_analysis(symbol)
            if result.get("reason") == "invalid_symbol":
                return self.deny(400, "invalid_symbol")
            return self.json(result)
        action = DEMO_POST_ROUTES.get(route)
        if action is None:
            return self.deny(404, "unknown_endpoint")
        if not self.gateway.config.demo:
            return self.deny(403, "read_only_mode")     # 唯讀模式永不轉送任何 POST
        return action(self)

    def _login(self):
        """preauth cookie + preauth CSRF + 完全相同的公開 HTTPS Origin。"""
        jar, err = self._cookies()
        if err:
            return self.deny(400, err)
        scopes = ("ip:" + str(self.client_address[0]),)
        if self.gateway.buckets.blocked(scopes):        # 節流在讀 body 與 KDF 之前
            return self.json({"ok": False, "reason": "too_many_attempts"}, code=429)
        if not self.gateway.preauth.consume(jar.get(PREAUTH_COOKIE),
                                            self.headers.get(CSRF_HEADER)):
            self.gateway.buckets.record_failure(scopes)
            return self.deny(403, "preauth_required")
        payload, err = self._read_json(("username", "password"))
        if err:
            self.gateway.buckets.record_failure(scopes)
            return self.deny(*err)
        username = payload.get("username")
        password = payload.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            self.gateway.buckets.record_failure(scopes)
            return self.json({"ok": False, "reason": "invalid_credentials"}, code=401)
        name = canonical_username(username)
        account = ("account:" + username_digest(name),) if name else ()
        if self.gateway.buckets.blocked(scopes + account):
            return self.json({"ok": False, "reason": "too_many_attempts"}, code=429)
        if not self.gateway.passwords.verify(username, password):
            self.gateway.buckets.record_failure(scopes + account)
            return self.json({"ok": False, "reason": "invalid_credentials"}, code=401)
        self.gateway.buckets.clear(scopes + account)
        token, csrf = self.gateway.sessions.create()    # 全新 token：不重用任何輸入
        cookies = (
            ("Set-Cookie", "%s=%s; Path=/; Secure; HttpOnly; SameSite=Strict"
                           % (SESSION_COOKIE, token)),
            ("Set-Cookie", "%s=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"
                           % PREAUTH_COOKIE),
        )
        self.json({"ok": True, "csrf": csrf, "mode": self.gateway.config.mode,
                   "read_only": not self.gateway.config.demo}, extra=cookies)

    def _logout(self):
        payload, err = self._read_json(())
        if err:
            return self.deny(*err)
        del payload
        jar, _err = self._cookies()
        self.gateway.sessions.revoke((jar or {}).get(SESSION_COOKIE))
        cleared = ("Set-Cookie", "%s=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"
                                 % SESSION_COOKIE)
        self.json({"ok": True, "logged_out": True}, extra=(cleared,))

    # ---------- DEMO 控制（只改合成畫面；已驗證 + CSRF 之後才會到這裡） ----------
    def _demo_receipt(self, action, extra=None):
        return self.json(self.gateway.engine.receipt(action, extra))

    def post_demo(self):
        from .demo_engine import SCENARIOS
        payload, err = self._read_json(("kind",))
        if err:
            return self.deny(*err)
        kind = payload.get("kind", "auto")
        if not isinstance(kind, str) or kind not in SCENARIOS:
            return self.deny(400, "unknown_demo_kind")
        out = self.gateway.engine.set_scenario(kind)
        if out is None:
            return self.deny(400, "unknown_demo_kind")
        return self.json(out)

    def post_control(self):
        payload, err = self._read_json(("action",))
        if err:
            return self.deny(*err)
        action = payload.get("action")
        if action not in DEMO_CONTROL_ACTIONS:
            return self.deny(400, "unknown_action")
        self.gateway.engine.paused_new = action == "pause_new"
        return self._demo_receipt("control:" + action, {
            "paused_new": action == "pause_new",
            "note": "DEMO：只標記合成畫面狀態，沒有任何真實委託被暫停或送出"})

    def post_guild_control(self):
        payload, err = self._read_json(("action", "capacity"))
        if err:
            return self.deny(*err)
        action = payload.get("action")
        if action not in DEMO_GUILD_ACTIONS:
            return self.deny(400, "unknown_action")
        self.gateway.engine.set_guild_paused(action == "pause")
        out = self.gateway.engine.guild()
        out.update(self.gateway.engine.receipt("guild:" + action, {"applied": True}))
        return self.json(out)

    def post_scan(self):
        payload, err = self._read_json(())
        if err:
            return self.deny(*err)
        del payload
        return self._demo_receipt("scan", {
            "scanned_at": self.gateway.engine.status()["scanned_at"],
            "note": "DEMO：合成掃描畫面本來就持續推進，這裡不觸發任何連線"})

    def post_decision(self):
        payload, err = self._read_json(("symbol",))
        if err:
            return self.deny(*err)
        symbol = str(payload.get("symbol", "")).upper()
        if not symbol or len(symbol) > 24 or not symbol.isalnum():
            return self.deny(400, "bad_symbol")
        return self._demo_receipt("decision_request", {
            "symbol": symbol, "reason": "demo_mode_no_model_call",
            "note": "DEMO：不會呼叫 JEV 或任何模型"})


DEMO_POST_ROUTES = {
    "/api/demo": RemoteHandler.post_demo,
    "/api/control": RemoteHandler.post_control,
    "/api/guild/control": RemoteHandler.post_guild_control,
    "/api/scan": RemoteHandler.post_scan,
    "/api/review": RemoteHandler.post_decision,
    "/api/decision": RemoteHandler.post_decision,
}


def build_server(config, webroot=None, **kwargs):
    """只綁 127.0.0.1（port=0 給測試用）。回 (httpd, gateway)。"""
    root = webroot or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
    gateway = Gateway(config, root, **kwargs)
    handler_cls = type("BoundRemoteHandler", (RemoteHandler,), {"gateway": gateway})
    httpd = BoundedThreadingServer((BIND_HOST, config.port), handler_cls)
    gateway.bound_port = httpd.server_address[1]
    if gateway.engine is not None:
        gateway.engine.port = gateway.bound_port
    gateway.served_stage()                              # 啟動時就驗證注入點與 CSP 雜湊
    return httpd, gateway


def serve(config, webroot=None, **kwargs):
    httpd, gateway = build_server(config, webroot=webroot, **kwargs)
    print("遠端閘道（密碼模式）：只綁 %s:%d，公開來源 %s"
          % (BIND_HOST, gateway.bound_port, config.public_origin))
    print("模式：%s；請用受信任的 HTTPS 反向代理把公開來源轉到這個 loopback 連接埠。"
          % config.mode)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        gateway.close()
        httpd.shutdown()
        httpd.server_close()
    return 0
