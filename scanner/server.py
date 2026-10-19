# -*- coding: utf-8 -*-
"""儀表板 HTTP 服務（標準函式庫，無額外依賴）。

安全界線（DEMO 與正式模式共用同一套檢查）：
- 只綁 127.0.0.1，且 Host 標頭必須剛好等於本機位址加實際連接埠。
- 路由一律「完全比對」，沒有 startswith 前綴比對。
- POST 必須同源（Origin 相符）、Content-Type 為 JSON、長度 <= 4096 bytes，
  內容只能是扁平物件，鍵名白名單，拒絕布林值／NaN／Infinity／巢狀結構。
- 靜態檔只從 web/ 底下供應，副檔名白名單，拒絕 .. 穿越與符號連結。
- 回應與記錄都不吐原始例外、標頭或金鑰內容。
"""
import json
import base64
import hashlib
import re
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 4096
MAX_STR_LEN = 64
JSON_CONTENT_TYPES = ("application/json",)
STATIC_EXT = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}
STATIC_EXTRA_FILES = {"LICENSE": "text/plain; charset=utf-8"}
DEFAULT_DOC = "/stage.html"


def reject_constant(_name):
    """json.loads 遇到 NaN / Infinity / -Infinity 時直接失敗。"""
    raise ValueError("non_finite_literal")


def parse_json_object(raw):
    """回 (obj, None) 或 (None, 錯誤代碼)。只接受扁平物件。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "bad_encoding"
    try:
        obj = json.loads(text, parse_constant=reject_constant)
    except ValueError:
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_shape"
    return obj, None


def validate_payload(obj, allowed_keys):
    """鍵名白名單 + 純量值檢查；布林／巢狀／非有限數一律拒絕。"""
    for key, value in obj.items():
        if key not in allowed_keys:
            return "unknown_key"
        if isinstance(value, bool) or value is None:
            return "bad_value_type"
        if isinstance(value, str):
            if len(value) > MAX_STR_LEN:
                return "value_too_long"
            continue
        if isinstance(value, int):
            continue
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return "bad_value_type"
            continue
        return "bad_value_type"
    return None


class GuardedHandler(BaseHTTPRequestHandler):
    """共用安全層：主機／同源／內容型別／大小／路由／靜態路徑檢查。"""

    server_version = "JEVCommandCenter"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    webroot = None          # 由 serve() 設定
    bound_port = None       # 實際綁定的連接埠
    get_routes = ()         # (path, method_name)
    post_routes = ()

    # ---------- 基礎工具 ----------
    def log_message(self, fmt, *args):  # 不記錄路徑／標頭／金鑰，保持主控台安靜
        pass

    def log_error(self, fmt, *args):
        pass

    def _native_connect_enabled(self):
        return False

    def _content_security_policy(self):
        hashes = set()
        for name in ("stage.html", "index.html"):
            try:
                with open(os.path.join(self.webroot or "", name), encoding="utf-8") as handle:
                    html = handle.read()
            except (OSError, UnicodeError):
                continue
            for content in re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, re.I | re.S):
                if content.strip():
                    digest = base64.b64encode(hashlib.sha256(content.encode("utf-8")).digest()).decode("ascii")
                    hashes.add("'sha256-" + digest + "'")
        connects = "'self'"
        if self._native_connect_enabled():
            connects += " http://127.0.0.1:8766"
        return ("default-src 'none'; script-src 'self' " + " ".join(sorted(hashes)) +
                "; connect-src " + connects +
                "; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
                "media-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
                "frame-ancestors 'none'")

    def _send(self, body, content_type, code=200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", self._content_security_policy())
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", code)

    def deny(self, code, reason):
        """只回固定代碼字串，不回原始例外或標頭內容。"""
        self.json({"ok": False, "reason": str(reason)[:48]}, code=code)

    # ---------- 檢查 ----------
    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        return host in ("127.0.0.1:%d" % self.bound_port, "localhost:%d" % self.bound_port)

    def _same_origin(self):
        origin = (self.headers.get("Origin") or "").strip().lower()
        return origin in ("http://127.0.0.1:%d" % self.bound_port,
                          "http://localhost:%d" % self.bound_port)

    def _route(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        while len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        return path or "/"

    def read_payload(self, allowed_keys):
        """回 (payload, None) 或 (None, (code, reason))。"""
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            return None, (400, "invalid_framing")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in JSON_CONTENT_TYPES:
            return None, (415, "content_type_required")
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or not str(raw_len).strip().isdigit():
            return None, (411, "length_required")
        length = int(raw_len)
        if length > MAX_BODY_BYTES:
            return None, (413, "body_too_large")
        raw = self.rfile.read(length) if length else b"{}"
        obj, err = parse_json_object(raw)
        if err:
            return None, (400, err)
        bad = validate_payload(obj, allowed_keys)
        if bad:
            return None, (400, bad)
        return obj, None

    # ---------- 靜態檔 ----------
    def _static_path(self, route):
        """把 URL 轉成 web/ 底下的實體路徑；不合格回 None。"""
        if self.webroot is None or ".." in route or "\\" in route or "%" in route:
            return None
        rel = route.lstrip("/") or "stage.html"
        if rel.endswith("/"):
            return None
        parts = [p for p in rel.split("/") if p]
        if not parts or any(p.startswith(".") for p in parts):
            return None
        name = parts[-1]
        ext = os.path.splitext(name)[1].lower()
        if ext not in STATIC_EXT and name not in STATIC_EXTRA_FILES:
            return None
        root = os.path.realpath(self.webroot)
        target = os.path.realpath(os.path.join(root, *parts))
        if target != root and not target.startswith(root + os.sep):
            return None
        probe = os.path.join(root, *parts)
        if os.path.islink(probe):
            return None
        walk = root
        for part in parts:
            walk = os.path.join(walk, part)
            if os.path.islink(walk):
                return None
        if not os.path.isfile(target):
            return None
        return target, STATIC_EXT.get(ext) or STATIC_EXTRA_FILES[name]

    def serve_static(self, route):
        if route == "/":
            route = DEFAULT_DOC
        found = self._static_path(route)
        if not found:
            return self.deny(404, "not_found")
        path, ctype = found
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self.deny(404, "not_found")
        self._send(body, ctype)

    # ---------- 分派 ----------
    def do_GET(self):
        if not self._host_ok():
            return self.deny(403, "bad_host")
        route = self._route()
        for path, name in self.get_routes:
            if route == path:
                return getattr(self, name)()
        if route.startswith("/api/"):
            return self.deny(404, "unknown_endpoint")
        return self.serve_static(route)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self._host_ok():
            return self.deny(403, "bad_host")
        if not self._same_origin():
            return self.deny(403, "origin_required")
        route = self._route()
        for path, name in self.post_routes:
            if route == path:
                return getattr(self, name)()
        return self.deny(404, "unknown_endpoint")

    def do_PUT(self):
        self.deny(405, "method_not_allowed")

    def do_DELETE(self):
        self.deny(405, "method_not_allowed")

    def do_OPTIONS(self):
        self.deny(405, "method_not_allowed")


# ------------------------------------------------------------ 正式（紙上交易）模式

_state = {"engine": None, "cfg": None, "lock": threading.Lock(), "scanning": False,
          "native_optin": False}

CONTROL_ACTIONS = ("pause_new", "resume")
SYMBOL_MAX = 24


def _symbol_ok(symbol):
    return bool(symbol) and len(symbol) <= SYMBOL_MAX and symbol.isalnum() and symbol.isupper()


def _order_id_ok(order_id):
    return bool(order_id) and len(order_id) <= 32 and all(
        c.isalnum() or c in "-_" for c in order_id)


def _scan_loop():
    eng, cfg = _state["engine"], _state["cfg"]
    while True:
        with _state["lock"]:
            busy = _state["scanning"]
            if not busy:
                _state["scanning"] = True
        if not busy:
            try:
                eng.scan_once()
            except Exception:
                print("[server] 掃描失敗（詳情不外流）")
            finally:
                with _state["lock"]:
                    _state["scanning"] = False
        time.sleep(int(cfg.get("fast_interval_seconds", cfg["scan_interval_seconds"])))


class RealHandler(GuardedHandler):
    """真實公開行情 + 紙上交易模式。控制端點只會動紙上帳本，且強制同源。"""

    get_routes = (("/api/status", "get_status"), ("/api/health", "get_health"),
                  ("/api/bootstrap.js", "get_bootstrap"))
    post_routes = (("/api/control", "post_control"), ("/api/review", "post_decision"),
                   ("/api/decision", "post_decision"), ("/api/scan", "post_scan"),
                   ("/api/order/cancel", "post_order_cancel"),
                   ("/api/position/close", "post_position_close"))

    def _native_connect_enabled(self):
        return bool(_state["native_optin"])

    def get_status(self):
        eng = _state["engine"]
        if eng.last_scan is None:
            return self.json({"status": "warming_up", "scanning": _state["scanning"]})
        return self.json({"status": "ok", "scanning": _state["scanning"],
                          "demo": False, "synthetic": False, "source": "LIVE_PAPER",
                          **eng.last_scan})

    def get_health(self):
        return self.json({
            "ok": True, "mode": "REAL_PAPER", "synthetic": False,
            "network_allowed": True, "paper_only": True,
            "native_cli_count": 0, "native_companion_optin": bool(_state["native_optin"]),
            "jev_key_configured": bool(os.environ.get("TYPESAFE_API_KEY")),
            "port": self.bound_port, "service": "jev-command-center",
        })

    def get_bootstrap(self):
        native = ('window.__JEV_NATIVE_ORIGIN__="http://127.0.0.1:8766";'
                  if _state["native_optin"] else "")
        body = ('window.__JEV_DEMO__=false;window.__JEV_MODE__="REAL_PAPER";'
                'window.__JEV_DEMO_INFO__=null;' + native).encode("utf-8")
        self._send(body, "text/javascript; charset=utf-8")

    def post_control(self):
        payload, err = self.read_payload(("action",))
        if err:
            return self.deny(*err)
        action = payload.get("action")
        if action not in CONTROL_ACTIONS:
            return self.deny(400, "unknown_action")
        return self.json(_state["engine"].set_paused_new(action == "pause_new"))

    def post_decision(self):
        payload, err = self.read_payload(("symbol",))
        if err:
            return self.deny(*err)
        symbol = str(payload.get("symbol", "")).upper()
        if not _symbol_ok(symbol):
            return self.deny(400, "bad_symbol")
        # 一律 200 + ok 旗標：預期中的拒絕不該被瀏覽器當成載入錯誤
        return self.json(_state["engine"].request_decision(symbol))

    def post_scan(self):
        payload, err = self.read_payload(())
        if err:
            return self.deny(*err)
        del payload
        with _state["lock"]:
            if _state["scanning"]:
                return self.json({"ok": False, "reason": "scan_in_progress"})
            _state["scanning"] = True
        try:
            scan = _state["engine"].scan_once()
            return self.json({"ok": True, "scanned_at": scan["scanned_at"]})
        except Exception:
            return self.json({"ok": False, "reason": "scan_failed"}, code=500)
        finally:
            with _state["lock"]:
                _state["scanning"] = False

    def post_order_cancel(self):
        payload, err = self.read_payload(("id",))
        if err:
            return self.deny(*err)
        order_id = str(payload.get("id", ""))
        if not _order_id_ok(order_id):
            return self.deny(400, "bad_id")
        return self.json(_state["engine"].cancel_order(order_id))

    def post_position_close(self):
        payload, err = self.read_payload(("symbol",))
        if err:
            return self.deny(*err)
        symbol = str(payload.get("symbol", "")).upper()
        if not _symbol_ok(symbol):
            return self.deny(400, "bad_symbol")
        return self.json(_state["engine"].close_position(symbol))


def make_server(handler_cls, webroot, port, host="127.0.0.1"):
    """綁定 loopback 並把實際連接埠寫回 handler（port=0 測試用）。"""
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    handler_cls.webroot = os.path.abspath(webroot)
    handler_cls.bound_port = httpd.server_address[1]
    return httpd


def serve(engine, cfg, base_dir, port, native_optin=False):
    _state.update({"engine": engine, "cfg": cfg, "native_optin": bool(native_optin)})
    httpd = make_server(RealHandler, os.path.join(base_dir, "web"), port)
    threading.Thread(target=_scan_loop, daemon=True).start()
    print("儀表板（紙上交易）：http://127.0.0.1:%d/  （Ctrl+C 停止）" % RealHandler.bound_port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
