# -*- coding: utf-8 -*-
"""DEMO HTTP 服務：同一個連接埠供應靜態畫面與全部合成 API。

與正式模式共用 scanner.server.GuardedHandler 的安全檢查（Host／同源／JSON／
長度／白名單鍵／完全比對路由／靜態穿越防護）。這裡不匯入 ScanEngine、
LiveFeed、原生 CLI 或 JEV 客戶端，所以沒有任何網路或金鑰路徑。
"""
import os

from .demo_engine import SCENARIOS, DemoEngine
from .server import GuardedHandler, make_server

CONTROL_ACTIONS = ("pause_new", "resume")
GUILD_ACTIONS = ("pause", "resume")

_demo = {"engine": None}

BOOTSTRAP_JS = (
    'window.__JEV_DEMO__=true;'
    'window.__JEV_MODE__="DEMO";'
    'window.__JEV_DEMO_INFO__={mode:"DEMO",synthetic:true,source:"DEMO",provider:"DEMO",'
    'network_allowed:false,native_cli_count:0,real_cli_agents:0,demo_agents:8,'
    'jev_api_called:false};'
    'window.__JEV_NATIVE_ORIGIN__="";'
)


class DemoHandler(GuardedHandler):
    """只回合成資料；任何控制端點都只回 DEMO 回執。"""

    get_routes = (("/api/status", "get_status"),
                  ("/api/guild/status", "get_guild"),
                  ("/api/health", "get_health"),
                  ("/api/guild/health", "get_health"),
                  ("/api/bootstrap.js", "get_bootstrap"))
    post_routes = (("/api/demo", "post_demo"),
                   ("/api/control", "post_control"),
                   ("/api/guild/control", "post_guild_control"),
                   ("/api/review", "post_decision"),
                   ("/api/decision", "post_decision"),
                   ("/api/scan", "post_scan"),
                   ("/api/order/cancel", "post_order_cancel"),
                   ("/api/position/close", "post_position_close"))

    @property
    def engine(self):
        return _demo["engine"]

    # ---------- GET ----------
    def get_status(self):
        self.json(self.engine.status())

    def get_guild(self):
        self.json(self.engine.guild())

    def get_health(self):
        self.json(self.engine.health())

    def get_bootstrap(self):
        self._send(BOOTSTRAP_JS.encode("utf-8"), "text/javascript; charset=utf-8")

    # ---------- POST（全部只改合成畫面，不呼叫引擎／模型／交易所） ----------
    def post_demo(self):
        payload, err = self.read_payload(("kind",))
        if err:
            return self.deny(*err)
        kind = payload.get("kind", "auto")
        if not isinstance(kind, str) or kind not in SCENARIOS:
            return self.deny(400, "unknown_demo_kind")
        out = self.engine.set_scenario(kind)
        if out is None:
            return self.deny(400, "unknown_demo_kind")
        return self.json(out)

    def post_control(self):
        payload, err = self.read_payload(("action",))
        if err:
            return self.deny(*err)
        action = payload.get("action")
        if action not in CONTROL_ACTIONS:
            return self.deny(400, "unknown_action")
        self.engine.paused_new = action == "pause_new"
        return self.json(self.engine.receipt(
            "control:" + action,
            {"paused_new": action == "pause_new",
             "note": "DEMO：只標記合成畫面狀態，沒有任何真實委託被暫停或送出"}))

    def post_guild_control(self):
        payload, err = self.read_payload(("action", "capacity"))
        if err:
            return self.deny(*err)
        action = payload.get("action")
        if action not in GUILD_ACTIONS:
            return self.deny(400, "unknown_action")
        self.engine.set_guild_paused(action == "pause")
        out = self.engine.guild()
        out.update(self.engine.receipt("guild:" + action, {"applied": True}))
        return self.json(out)

    def post_decision(self):
        payload, err = self.read_payload(("symbol",))
        if err:
            return self.deny(*err)
        symbol = str(payload.get("symbol", "")).upper()
        if not symbol or len(symbol) > 24 or not symbol.isalnum():
            return self.deny(400, "bad_symbol")
        return self.json(self.engine.receipt(
            "decision_request", {"symbol": symbol,
                                 "reason": "demo_mode_no_model_call",
                                 "note": "DEMO：不會呼叫 JEV 或任何模型，畫面依固定節奏播放"}))

    def post_scan(self):
        payload, err = self.read_payload(())
        if err:
            return self.deny(*err)
        del payload
        return self.json(self.engine.receipt(
            "scan", {"scanned_at": self.engine.status()["scanned_at"],
                     "note": "DEMO：合成掃描畫面本來就持續推進，這裡不觸發任何連線"}))

    def post_order_cancel(self):
        payload, err = self.read_payload(("id",))
        if err:
            return self.deny(*err)
        order_id = str(payload.get("id", ""))
        if not order_id or len(order_id) > 32 or not all(
                c.isalnum() or c in "-_" for c in order_id):
            return self.deny(400, "bad_id")
        return self.json(self.engine.receipt("order_cancel", {"id": order_id}))

    def post_position_close(self):
        payload, err = self.read_payload(("symbol",))
        if err:
            return self.deny(*err)
        symbol = str(payload.get("symbol", "")).upper()
        if not symbol or len(symbol) > 24 or not symbol.isalnum():
            return self.deny(400, "bad_symbol")
        return self.json(self.engine.receipt("position_close", {"symbol": symbol}))


def build(base_dir, port, engine=None, host="127.0.0.1"):
    """建立 DEMO 伺服器（不啟動迴圈）；回 (httpd, engine)。"""
    eng = engine or DemoEngine(port=port)
    _demo["engine"] = eng
    httpd = make_server(DemoHandler, os.path.join(base_dir, "web"), port, host=host)
    eng.port = DemoHandler.bound_port
    return httpd, eng


def serve(base_dir, port, engine=None):
    httpd, _eng = build(base_dir, port, engine=engine)
    print("DEMO 離線示範：http://127.0.0.1:%d/  （合成資料、無網路、Ctrl+C 停止）"
          % DemoHandler.bound_port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
