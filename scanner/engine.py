# -*- coding: utf-8 -*-
"""即時引擎 v2：全市場秒級決策 -> 深掃確認名單 -> Jev 確認 -> 模擬執行。

三層節奏（全部自動）：
1) 快層（每 fast_interval 秒）：全部 USDT 永續以即時價/最佳盤口算即時分，
   即時產生 long/short/watch 決策，並以即時價管理模擬持倉停損停利。
2) 深層（每 deep_interval 秒）：即時分最強且成交額達標者抓訂單簿+K線，
   算完整訂單簿 x 價格行為規則分。
3) Jev 層（緊接深層）：深層最強且冷卻期外者送 Jev 五題確認；
   雙閘門（Jev 與規則同向 + 門檻全過）才模擬開倉。
Jev 只確認漏斗頂端：700+ 交易對逐一送審在延遲與成本上不可行，快層決策
負責即時性，Jev 負責最終品質把關，面板會如實標示每一層的覆蓋範圍。
"""
import datetime
import json
import math
import os
import threading
import time
from collections import deque

from . import jev_client, jev_decision, kelly as kelly_mod, local_decider, multi_review, plans as plans_mod
from .binance_client import BinancePublicClient
from .snapshot import build_snapshot, content_hash
from .config import typesafe_api_key
from .live_feed import LiveFeed
from .orderbook import orderbook_features
from .paper_broker import PaperBroker
from .price_action import entry_filter_reasons, instant_score, parse_klines, price_action_features, rule_score


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class ScanEngine:
    def __init__(self, cfg, base_dir):
        self.cfg = cfg
        self.base_dir = base_dir
        self.state_path = os.path.join(base_dir, "paper_state.json")
        self.latest_path = os.path.join(base_dir, "latest.json")
        self.client = BinancePublicClient()
        self.feed = LiveFeed(interval=int(cfg.get("feed_interval_seconds", 3)))
        self.feed.start()
        self.broker = PaperBroker(cfg, self._load_state())
        self.last_scan = None
        self.scan_count = 0
        self.day_stats = {}          # symbol -> {quote_volume, change_pct_24h}，深層週期刷新
        self.day_stats_at = 0.0
        self.price_hist = {}         # symbol -> deque[(ts, price)] 約保留 6 分鐘
        self.deep_cache = {}         # symbol -> 深層特徵 dict
        self.deep_at = 0.0
        self.jev_cooldown = {}       # symbol -> 上次送 Jev 的 epoch
        self.events = deque(maxlen=40)
        self._last_signal = {}
        self.orb = {"state": "SCANNING", "symbol": None, "direction": "watch",
                    "headline": "全市場掃描中", "detail": "", "updated_at": _now()}
        self._event("scan", None, "即時引擎啟動：全市場快層 + 深層 + Jev 確認")
        self.lock = threading.RLock()
        self.orders = []             # 訂單生命週期（含演示單，demo=True 明確標示）
        self.order_seq = 0
        self.paused_new = False      # 暫停新增交易（持倉管理仍持續）
        self._macd_seen = set()      # (symbol, bar_time, cross) 避免同一根 K 重複觸發
        self.demo_stale_until = 0.0  # 演示：資料中斷旗標到期時間
        self.strategy_version = "MACD15m-JEV-v4"
        self.filtered_total = 0      # 濾網累計擋下筆數
        self.plans = []              # 條件交易計畫（最新在前，含雜湊綁定）
        self.plan_seq = 0
        self.last_snapshot = None    # 共同資料快照（工單 §7.3）
        self.reviews = []            # 六模型分析紀錄（最新在前；僅證據，不授權下單）
        self.decisions = []          # JEV 決策回執紀錄（最新在前，typed decision protocol §13.3）
        self.decision_seq = 0
        self.decision_busy = False   # 單一決策鏈：同一時間只處理一個觸發
        self.carousel_busy = False   # 多 AI 持續輪巡：同一時間只跑一包分析
        self._carousel_started = False
        self.analysis_cooldown = {}  # symbol -> epoch（輪巡節流）
        self.decision_cooldown = {}  # symbol -> epoch（共識觸發節流）
        self.jev_health = {"required": True, "enabled": True, "status": "UNVERIFIED",
                           "model": cfg.get("jev_model", "jev-latest"), "actual_model": None,
                           "last_decision_at": None, "last_latency_ms": None,
                           "last_error": None, "blocked_count": 0}
        self._load_runtime()
        self.review_cooldown = {}    # symbol -> 上次複審 epoch
        self.review_busy = False

    # ---------- 狀態存取 ----------
    def _load_state(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                return None
        return None

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.broker.to_state(), f, ensure_ascii=False)
        os.replace(tmp, self.state_path)
        self._save_runtime()

    # ---------- 運行狀態持久化與重啟核對（工單 §12.4） ----------
    def _runtime_path(self):
        return os.path.join(self.base_dir, "runtime_state.json")

    def _save_runtime(self):
        try:
            tmp = self._runtime_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"orders": self.orders[:30], "reviews": self.reviews[:10],
                           "filtered_total": self.filtered_total, "order_seq": self.order_seq,
                           "plans": self.plans[:10], "plan_seq": self.plan_seq,
                           "decisions": self.decisions[:10], "decision_seq": self.decision_seq,
                           "jev_health": self.jev_health},
                          f, ensure_ascii=False)
            os.replace(tmp, self._runtime_path())
        except OSError:
            pass  # 持久化失敗不阻塞交易管理，下一輪再寫

    def _load_runtime(self):
        p = self._runtime_path()
        if not os.path.exists(p):
            return
        try:
            with open(p, encoding="utf-8") as f:
                rt = json.load(f)
        except (OSError, ValueError):
            return
        self.orders = rt.get("orders", [])
        self.reviews = rt.get("reviews", [])
        self.filtered_total = int(rt.get("filtered_total", 0))
        self.order_seq = int(rt.get("order_seq", 0))
        self.plans = rt.get("plans", [])
        self.plan_seq = int(rt.get("plan_seq", 0))
        self.decisions = rt.get("decisions", [])
        self.decision_seq = int(rt.get("decision_seq", 0))
        if isinstance(rt.get("jev_health"), dict):
            self.jev_health.update(rt["jev_health"])
        # 重啟核對：送出後未確認的委託一律先標 UNKNOWN，不假設未成交也不盲目重送
        for o in self.orders:
            if o.get("status") in ("WAITING_FILL", "PARTIALLY_FILLED"):
                self._order_step(o, "exchange_wait", "blocked",
                                 "重啟後核對：委託結果未知，先核對不重送", "PENDING_CONFIRM")
        if self.orders:
            self._event("recovery", None,
                        f"重啟核對完成：恢復 {len(self.orders)} 筆委託紀錄，未知委託已標 UNKNOWN")

    def _event(self, kind, symbol, text):
        self.events.appendleft({"t": _now(), "kind": kind, "symbol": symbol, "text": text})

    def _set_orb(self, state, symbol, direction, headline, detail):
        self.orb = {"state": state, "symbol": symbol, "direction": direction,
                    "headline": headline, "detail": detail, "updated_at": _now()}

    # ---------- 訂單生命週期 ----------
    STAGES = ["market", "strategy", "sizing_risk", "submit", "exchange_wait", "position", "exit_record"]

    def _new_order(self, symbol, side, qty, trigger, demo=False, kind="entry"):
        self.order_seq += 1
        oid = f"{'DEMO' if demo else 'SIM'}-{self.order_seq:04d}"
        order = {
            "id": oid, "symbol": symbol, "side": side, "kind": kind, "demo": demo,
            "qty": qty, "filled_qty": 0.0, "avg_price": None, "status": "STRATEGY_OK",
            "stage": "strategy", "trigger": trigger, "risk_reasons": [],
            "jev_summary": None, "strategy_version": self.strategy_version,
            "created_at": _now(), "updated_at": _now(),
            "path": [
                {"stage": "market", "status": "done", "t": _now(), "note": "已收到即時行情"},
                {"stage": "strategy", "status": "done", "t": _now(), "note": trigger},
            ],
        }
        self.orders.insert(0, order)
        self.orders = self.orders[:30]
        return order

    def _order_step(self, order, stage, status, note, order_status=None):
        order["stage"] = stage
        order["updated_at"] = _now()
        if order_status:
            order["status"] = order_status
        order["path"] = [p for p in order["path"] if p["stage"] != stage]
        order["path"].append({"stage": stage, "status": status, "t": _now(), "note": note})

    def _progress_orders(self, marks):
        """推進模擬訂單：等待回報 -> 部分成交 -> 全部成交建倉（皆標示模擬）。"""
        opened = []
        for o in list(self.orders):
            if o["demo"] or o["kind"] != "entry":
                continue
            px = marks.get(o["symbol"])
            if px is None:
                continue
            if o["status"] == "WAITING_FILL":
                o["filled_qty"] = round(o["qty"] * 0.6, 8)
                o["avg_price"] = px
                self._order_step(o, "exchange_wait", "working",
                                 f"模擬部分成交 {o['filled_qty']:.6g}/{o['qty']:.6g}，等待其餘回報",
                                 "PARTIALLY_FILLED")
                self._event("order", o["symbol"], f"{o['id']} 部分成交（模擬）")
            elif o["status"] == "PARTIALLY_FILLED":
                pos, msg = self.broker.open_with_qty(o["symbol"], o["side"], o["qty"], px)
                if pos:
                    # 新系統出場：以進場 ATR 設定停損/停利（1.5/3.0），由 mark_and_manage 每快層檢查
                    c = self.deep_cache.get(o["symbol"]) or {}
                    atr = (c.get("price_action") or {}).get("atr")
                    if atr and atr > 0:
                        sl_m, tp_m = float(self.cfg["atr_stop_mult"]), float(self.cfg["atr_tp_mult"])
                        if pos["side"] == "long":
                            pos["stop"], pos["take_profit"] = pos["entry_price"] - sl_m * atr, pos["entry_price"] + tp_m * atr
                        else:
                            pos["stop"], pos["take_profit"] = pos["entry_price"] + sl_m * atr, pos["entry_price"] - tp_m * atr
                        pos["atr_at_entry"] = atr
                    pos["opened_epoch"] = time.time()
                    o["filled_qty"] = o["qty"]
                    o["avg_price"] = pos["entry_price"]
                    self._order_step(o, "position", "done",
                                     f"模擬全部成交，均價 {pos['entry_price']:.6g}，持倉已建立", "FILLED")
                    opened.append(pos)
                    self._event("open", o["symbol"], f"{o['id']} 全部成交（模擬）{o['side']} @ {pos['entry_price']:.6g}")
                    self._set_orb("EXECUTED", o["symbol"], o["side"],
                                  f"已執行 {o['symbol']} {o['side']}", f"訂單 {o['id']} 模擬全部成交")
                else:
                    self._order_step(o, "exchange_wait", "blocked", f"建倉失敗：{msg}", "PENDING_CONFIRM")
        return opened

    def _progress_demo(self, marks):
        """演示單只演流程、不動模擬帳戶，畫面與紀錄皆標 DEMO。"""
        for o in list(self.orders):
            if not o["demo"]:
                continue
            px = marks.get(o["symbol"]) or 100.0
            if o["status"] == "WAITING_FILL":
                o["filled_qty"] = round(o["qty"] * 0.6, 8)
                o["avg_price"] = px
                self._order_step(o, "exchange_wait", "working", "DEMO 部分成交，等待其餘回報", "PARTIALLY_FILLED")
            elif o["status"] == "PARTIALLY_FILLED" and o.get("demo_plan") == "success":
                o["filled_qty"] = o["qty"]
                o["avg_price"] = px
                self._order_step(o, "position", "done", "DEMO 全部成交（虛擬持倉，不計入帳戶）", "FILLED")
            elif o["status"] == "FILLED" and o.get("demo_plan") == "success":
                pnl = (px - (o["avg_price"] or px)) * o["qty"] * (1 if o["side"] == "long" else -1)
                self._order_step(o, "exit_record", "done",
                                 f"DEMO 出場完成，虛擬損益 ${pnl:+.2f}（不計入帳戶）", "CLOSED")

    # ---------- 控制（面板操作） ----------
    def set_paused_new(self, paused):
        with self.lock:
            self.paused_new = bool(paused)
            self._event("control", None, "已暫停新增交易（持倉管理持續）" if paused else "已恢復策略，重新接受新增交易")
            return {"ok": True, "paused_new": self.paused_new}

    def cancel_order(self, order_id):
        with self.lock:
            for o in self.orders:
                if o["id"] == order_id and o["status"] in ("WAITING_FILL", "PARTIALLY_FILLED", "PENDING_CONFIRM"):
                    self._order_step(o, "exchange_wait", "blocked", "使用者取消指定委託", "CANCELLED")
                    self._event("order", o["symbol"], f"{o['id']} 已取消（模擬）")
                    return {"ok": True, "id": order_id}
            return {"ok": False, "reason": "order_not_cancellable"}

    def close_position(self, symbol):
        with self.lock:
            feed = self.feed.snapshot()
            px = feed["prices"].get(symbol)
            if px is None:
                return {"ok": False, "reason": "no_live_price"}
            rec = self.broker.close_position(symbol, px, "manual_close")
            if rec is None:
                return {"ok": False, "reason": "position_not_found"}
            self._event("close", symbol, f"手動平倉 損益 ${rec['pnl_net']:+.2f}")
            self._save()
            return {"ok": True, "symbol": symbol, "pnl_net": rec["pnl_net"]}

    def inject_demo(self, kind):
        """明確標示的演示事件（DEMO），不觸發真實模擬帳戶下單。"""
        with self.lock:
            feed = self.feed.snapshot()
            sym = "BTCUSDT" if "BTCUSDT" in feed["prices"] else (sorted(feed["prices"])[0] if feed["prices"] else "BTCUSDT")
            px = feed["prices"].get(sym, 100.0)
            if kind == "risk_reject":
                o = self._new_order(sym, "long", 1.0, "DEMO 演示觸發（非即時訊號）", demo=True)
                o["risk_reasons"] = ["DEMO：可用資金不足（演示原因，非真實風控結果）"]
                self._order_step(o, "sizing_risk", "blocked", "DEMO 風控拒絕：可用資金不足", "RISK_REJECTED")
                self._event("demo", sym, f"{o['id']} DEMO 風控拒絕流程")
                return {"ok": True, "id": o["id"]}
            if kind == "partial_pending":
                o = self._new_order(sym, "long", 1.0, "DEMO 演示觸發（非即時訊號）", demo=True)
                o["demo_plan"] = "partial"
                self._order_step(o, "sizing_risk", "done", "DEMO 部位計算完成", None)
                self._order_step(o, "submit", "done", "DEMO 已送出委託（模擬）", "WAITING_FILL")
                self._event("demo", sym, f"{o['id']} DEMO 部分成交/待確認流程")
                return {"ok": True, "id": o["id"]}
            if kind == "feed_stale":
                self.demo_stale_until = time.time() + 30
                self._event("demo", None, "DEMO 資料中斷演示 30 秒（非真實斷線）")
                return {"ok": True, "kind": kind}
            # success：完整流程 送單->部分->全部->出場（虛擬，不計帳戶）
            o = self._new_order(sym, "long", 1.0, "DEMO 演示觸發（非即時訊號）", demo=True)
            o["demo_plan"] = "success"
            self._order_step(o, "sizing_risk", "done", "DEMO 部位計算完成（每倉 $5,000 示意）", None)
            self._order_step(o, "submit", "done", "DEMO 已送出委託（模擬）", "WAITING_FILL")
            self._event("demo", sym, f"{o['id']} DEMO 完整成交流程（虛擬，不計入帳戶） @ {px:.6g}")
            return {"ok": True, "id": o["id"]}

    # ---------- 主週期 ----------
    def scan_once(self):
        with self.lock:
            return self._scan_once_impl()

    def _scan_once_impl(self):
        cfg = self.cfg
        now = time.time()
        feed = self.feed.snapshot()
        prices, books = feed["prices"], feed["books"]

        # 日統計（成交額/漲跌）低頻刷新，供活躍度與深層篩選
        if now - self.day_stats_at >= float(cfg["deep_interval_seconds"]) or not self.day_stats:
            try:
                self.day_stats = {u["symbol"]: u for u in self.client.universe_all()}
                self.day_stats_at = now
            except Exception as e:
                self._event("scan", None, f"日統計刷新失敗，沿用上一份：{str(e)[:80]}")

        symbols = sorted(set(prices) & set(self.day_stats)) or sorted(prices)
        # 活躍度：log 成交額正規化 0..1
        logs = {s: math.log10(self.day_stats[s]["quote_volume"] + 1.0) for s in symbols if s in self.day_stats}
        lo = min(logs.values()) if logs else 0.0
        hi = max(logs.values()) if logs else 1.0

        live_rows = []
        marks = {}
        for sym in symbols:
            px = prices.get(sym)
            if not px or px <= 0:
                continue
            marks[sym] = px
            hist = self.price_hist.setdefault(sym, deque(maxlen=80))
            hist.append((now, px))
            mom = 0.0
            if len(hist) >= 2:
                past = next((p for t, p in hist if now - t >= 55), hist[0][1])
                mom = (px / past - 1.0) * 100.0 if past > 0 else 0.0
            b = books.get(sym)
            book_imb = 0.0
            if b:
                tot = b[1] + b[3]
                book_imb = (b[1] - b[3]) / tot if tot > 0 else 0.0
            day = self.day_stats.get(sym, {})
            act = (logs.get(sym, lo) - lo) / (hi - lo) if hi > lo else 0.5
            score, direction = instant_score(book_imb, mom, day.get("change_pct_24h", 0.0), act)
            live_rows.append({
                "symbol": sym, "price": px,
                "change_pct_24h": day.get("change_pct_24h", 0.0),
                "quote_volume": day.get("quote_volume", 0.0),
                "book_imb": round(book_imb, 4), "mom_1m_pct": round(mom, 4),
                "live_score": score, "live_direction": direction,
            })
        live_rows.sort(key=lambda r: abs(r["live_score"]), reverse=True)
        for r in live_rows:
            if r["live_direction"] != "watch":
                self._maybe_signal_event(r)

        closed_now = self.broker.mark_and_manage(marks)
        # 最長持有時間停損（24h）：到期以市價平倉並留下訂單路徑
        max_hold = float(cfg.get("max_hold_hours", 24)) * 3600
        for pos in list(self.broker.positions):
            oe = pos.get("opened_epoch")
            if oe and now - oe >= max_hold and marks.get(pos["symbol"]):
                rec = self.broker.close_position(pos["symbol"], marks[pos["symbol"]], "time_stop_24h")
                if rec:
                    closed_now.append(rec)
        for c in closed_now:
            self._event("close", c["symbol"], f"平倉 {c['exit_reason']} 損益 ${c['pnl_net']:+.2f}")
            if c["exit_reason"] in ("stop_loss", "take_profit", "time_stop_24h"):
                o = self._new_order(c["symbol"], c["side"], c["qty"],
                                    f"出場：{c['exit_reason']}（ATR 停損停利/時間停損）", kind="exit")
                self._order_step(o, "sizing_risk", "done", "出場數量=持倉數量", None)
                self._order_step(o, "submit", "done", "已送出平倉模擬委託", "WAITING_FILL")
                self._order_step(o, "position", "done", "持倉已平倉", "FILLED")
                self._order_step(o, "exit_record", "done",
                                 f"{c['exit_reason']} 出場，損益 ${c['pnl_net']:+.2f}", "CLOSED")
        filled_now = self._progress_orders(marks)
        self._progress_demo(marks)
        # 凱莉艙位與即時風險（每快層更新，部位計算員據此定量，剎車時擋新倉）
        self.current_kelly = kelly_mod.compute_kelly(self.broker.closed, cfg, self.broker.equity(marks))
        self.current_risk = kelly_mod.risk_snapshot(self.broker, marks, cfg, self.current_kelly)

        deep_due = now - self.deep_at >= float(cfg["deep_interval_seconds"])
        if deep_due:
            opened = self._deep_and_jev(live_rows, marks) + filled_now
            self.deep_at = now
        else:
            opened = filled_now
        # 多 AI 持續輪巡（背景緒，首次掃描時啟動；不阻塞快層）
        if self.cfg.get("external_analysis_enabled", False) and self.cfg.get("analysis_carousel", False) and not self._carousel_started:
            self._carousel_started = True
            threading.Thread(target=self._carousel_loop, daemon=True).start()

        ranked_all = sorted(self.deep_cache.values(), key=lambda c: c["rule_strength"], reverse=True)
        focus = ranked_all[0] if ranked_all else None
        candidates = []
        for c in ranked_all:
            row = {k: v for k, v in c.items() if k not in ("bars", "depth")}
            row["problem"] = self._problem_reason(c)
            row["jev_problem"] = self._jev_problem(c)
            dec = self._latest_decision(c["symbol"])
            row["decision_status"] = dec.get("status") if dec else None
            row["decision_id"] = dec.get("decision_id") if dec else None
            candidates.append(row)
        if focus is None and live_rows:
            top = live_rows[0]
            self._set_orb("SCANNING", top["symbol"], top["live_direction"],
                          f"即時最強 {top['symbol']} {top['live_direction']}",
                          f"即時分 {top['live_score']:+.3f}，等待深層確認")
        self.scan_count += 1
        account = self.broker.snapshot(marks)
        self.last_snapshot = build_snapshot(
            self.scan_count, _now(), cfg["timeframe"],
            feed.get("age_seconds"), candidates, account, self.strategy_version)
        self.broker.peak_equity = max(self.broker.peak_equity, account["equity"])
        self.last_scan = {
            "scanned_at": _now(), "scan_count": self.scan_count,
            "timeframe": cfg["timeframe"], "jev_available": bool(typesafe_api_key()),
            "universe_size": len(symbols),
            "feed": {"poll_count": feed["poll_count"], "symbols": feed["symbols"],
                     "age_seconds": feed["age_seconds"], "last_error": feed["last_error"]},
            "live_rows": live_rows[:48],
            "live_signals": sum(1 for r in live_rows if r["live_direction"] != "watch"),
            "candidates": candidates,
            "opened": opened, "closed_now": closed_now,
            "account": account, "equity_curve": self.broker.equity_curve,
            "focus": {"symbol": focus["symbol"] if focus else (live_rows[0]["symbol"] if live_rows else None),
                      "bars": focus["bars"] if focus else [],
                      "depth": focus["depth"] if focus else {"bids": [], "asks": []}},
            "orb": self.orb, "events": list(self.events),
            "mode": {
                "trading": "模擬交易（即時行情）",
                "exchange": "Binance USDT-M 永續（公開行情，免金鑰）",
                "account_id": "PAPER-100K-001",
                "paused_new": self.paused_new,
                "risk_control": bool(cfg.get("use_risk_control", True)),
                "strategy_version": self.strategy_version,
                "feed_stale_demo": time.time() < self.demo_stale_until,
            },
            "roles": self._roles(feed, account),
            "risk": getattr(self, "current_risk", None),
            "orders": self.orders[:20],
            "reviews": self.reviews[:6],
            "decisions": self.decisions[:6],
            "carousel_busy": self.carousel_busy,
            "jev_core": {**self.jev_health, "busy": self.decision_busy,
                         "last_decision": self.decisions[0] if self.decisions else None},
            "filtered_total": self.filtered_total,
            "snapshot": self.last_snapshot,
            "plans": self.plans[:6],
            "models": self._models_payload(),
            "settings": [
                {"label": "交易所", "value": "Binance（已設定）"},
                {"label": "市場", "value": "USDT-M 永續合約（已設定）"},
                {"label": "交易商品", "value": f"全部 USDT 永續（{len(symbols)} 個）"},
                {"label": "時間週期", "value": str(cfg["timeframe"])},
                {"label": "策略來源", "value": "15m MACD 交叉 + 濾網產生候選 → 六模型分析 → JEV 最終決策 → 風控 → 執行（MACD15m-JEV-v4）"},
                {"label": "部位大小", "value": "凱莉艙位（半凱莉、上限 20% 權益；無正期望自動降艙）"},
                {"label": "槓桿", "value": "待設定（模擬保證金暫以名目 1/3 記帳，僅帳務展示）"},
                {"label": "停損停利", "value": "ATR 停損 1.5×／停利 3.0×（進場 ATR）＋最長持有 24h（回測選參，詳策略驗證報告）"},
                {"label": "交易限制", "value": "進場濾網啟用（趨勢/量能/價差/RSI/盤口反向）；另查重複持倉/現金/行情新鮮度；停損停利待設定" if not cfg.get("use_risk_control", True) else "風控門檻啟用"},
            ],
            "thresholds": {k: cfg[k] for k in (
                "max_spread_bps", "risk_per_trade_pct", "max_concurrent_positions",
                "atr_stop_mult", "atr_tp_mult", "jev_min_entry_quality",
                "jev_min_valid_setup", "jev_max_trap", "jev_min_confidence")},
        }
        self.last_scan["thresholds"]["use_risk_control"] = bool(cfg.get("use_risk_control", True))
        self.last_scan["thresholds"]["simple_position_usd"] = float(cfg.get("simple_position_usd", 5000))
        self.broker.equity_curve.append({"t": self.last_scan["scanned_at"], "equity": account["equity"]})
        self._save()
        tmp = self.latest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.last_scan, f, ensure_ascii=False)
        os.replace(tmp, self.latest_path)
        return self.last_scan

    def _roles(self, feed, account):
        """八角色即時狀態：每個角色對應真實模組，狀態由本輪資料推導，不編造。"""
        stale = (feed.get("age_seconds") is not None and feed["age_seconds"] > 15) or time.time() < self.demo_stale_until
        working_orders = [o for o in self.orders if o["status"] in ("WAITING_FILL", "PARTIALLY_FILLED", "PENDING_CONFIRM")]
        rejected = [o for o in self.orders if o["status"] == "RISK_REJECTED"]
        now_s = _now()
        base = [
            ("chief", "總控", "#ff2ea6", "掌握整體狀態與等待原因"),
            ("feed", "行情員", "#37e0ff", "接收行情、掃描標的、維護關注清單"),
            ("strategy", "策略分析員", "#ffb020", "檢查 MACD 與輔助條件、整理觸發依據"),
            ("sizer", "部位計算員", "#2dff8f", "計算委託數量與資金占用"),
            ("risk", "風控員", "#ff4d6d", "檢查可執行性與交易限制"),
            ("exec", "交易執行員", "#dbe4f5", "送出委託並追蹤成交回報"),
            ("position", "持倉管理員", "#2dd4bf", "追蹤持倉、出場條件與未實現損益"),
            ("record", "紀錄員", "#9fb2d8", "整理成交、費用與已實現損益"),
        ]
        info = {
            "chief": ("blocked" if self.paused_new else "working",
                      "已暫停新增交易" if self.paused_new else self.orb.get("headline", "監控中"),
                      f"權益 ${account['equity']:,.2f}，持倉 {account['open_positions']} 筆",
                      [o["id"] for o in self.orders[:3]]),
            "feed": ("error" if stale else "working",
                     "行情異常/過期" if stale else f"即時盤更新 {feed.get('symbols', 0)} 對",
                     f"資料年齡 {feed.get('age_seconds')} 秒" + ("（DEMO 中斷演示中）" if time.time() < self.demo_stale_until else ""),
                     []),
            "strategy": ("working" if self.deep_cache else "idle",
                         "以 15 分鐘 MACD 交叉為主判斷" if self.deep_cache else "等待深層分析",
                         f"深層候選 {len(self.deep_cache)} 個，即時訊號 {sum(1 for c in self.deep_cache.values() if c.get('price_action', {}).get('macd_cross') != 'none')} 個交叉",
                         [c["symbol"] for c in list(self.deep_cache.values())[:3]]),
            "sizer": ("working" if working_orders else "idle",
                      "計算委託數量" if working_orders else "待命（無新委託）",
                      f"每倉 ${float(self.cfg.get('simple_position_usd', 5000)):,.0f}，可用現金 ${account['cash']:,.2f}", []),
            "risk": ("blocked" if rejected else "working",
                     "有委託被拒絕" if rejected else "檢查重複持倉/現金/行情新鮮度",
                     ("最近拒絕：" + rejected[0]["id"] + " " + "；".join(rejected[0].get("risk_reasons", []))) if rejected else "風控門檻關閉（停損停利待設定），僅做可執行性檢查",
                     [o["id"] for o in rejected[:3]]),
            "exec": ("working" if working_orders else "idle",
                     f"追蹤 {len(working_orders)} 筆委託回報" if working_orders else "待命（無未完成委託）",
                     "模擬成交分兩段回報：部分成交後全部成交" if working_orders else "尚無送出中的委託",
                     [o["id"] for o in working_orders[:3]]),
            "position": ("working" if account["open_positions"] else "idle",
                         f"管理 {account['open_positions']} 筆持倉" if account["open_positions"] else "目前無持倉",
                         f"未實現損益 ${account['unrealized_pnl']:+,.2f}，出場條件：MACD 反向交叉（停損停利待設定）",
                         [p["symbol"] for p in account.get("positions", [])[:3]]),
            "record": ("working", "整理成交與事件紀錄",
                       f"已平倉 {account['closed_trades']} 筆，已實現 ${account['realized_pnl']:+,.2f}，勝率 {account['win_rate_pct']}%（區間：全部期間）",
                       [o["id"] for o in self.orders[:3]]),
        }
        return [
            {"id": rid, "name": name, "color": color, "duty": duty,
             "status": info[rid][0], "task": info[rid][1], "summary": info[rid][2],
             "related": info[rid][3], "updated_at": now_s}
            for rid, name, color, duty in base
        ]

    def _maybe_signal_event(self, r):
        prev = self._last_signal.get(r["symbol"])
        if prev != r["live_direction"]:
            self._last_signal[r["symbol"]] = r["live_direction"]
            self._event("signal", r["symbol"],
                        f"即時決策 {r['live_direction']}（分數 {r['live_score']:+.3f}）")

    def _models_payload(self):
        """六模型註冊表：身分與即時狀態分開，未接通如實標示。"""
        last = self.reviews[0] if self.reviews else {}
        votes = last.get("votes", {}) if last else {}

        def seat(alias, seat_key, entry, model, role):
            v = votes.get(seat_key) if seat_key else None
            if seat_key is None:
                status, note = "READY", "本機彙整（規則層），即時可用"
            elif not self.cfg.get("external_analysis_enabled", False):
                status, note = "DISABLED", "外部分析預設未啟用"
            elif v is None:
                status, note = "IDLE", "尚未呼叫（待命）"
            elif v.get("decision") == "unavailable":
                status, note = "UNAVAILABLE", str(v.get("reason", ""))[:60]
            else:
                status, note = "READY", f"最近投票 {v.get('decision')}"
            return {"alias": alias, "entry": entry, "model": model, "role": role,
                    "status": status, "note": note,
                    "last_decision": (v or {}).get("decision"),
                    "latency_ms": (v or {}).get("latency_ms"),
                    "updated_at": last.get("at") if v else None}

        return [
            seat("GROK", "grok", "grok CLI 單次呼叫", "未核實（CLI 預設）", "事件情報席"),
            seat("CLAUDE", "claude", "claude -p --model opus", "opus（設定值，實際版本未核實）", "策略提案席"),
            seat("MUSE", "muse", "本機彙整（rules-summary（規則彙整，非 MUSE API））", "rules-summary（規則彙整，非 MUSE API）", "彙整/反證席"),
            seat("CODEX", "gpt", "codex exec 唯讀沙箱", "gpt-6.1-sol", "派工/稽核席"),
            {"alias": "QWEN", "entry": "qwen code（node cli-entry）", "model": "未設定",
             "role": "初篩席", "status": "DISABLED",
             "note": "預設未啟用此適配器",
             "last_decision": None, "latency_ms": None, "updated_at": None},
            seat("AGY", "gemini", "agy -p（Google AI）", "gemini-4-argon-medium", "跨週期複核席"),
        ]

    # ---------- JEV 最終決策鏈（六模型分析 -> JEV Choice -> 風控 -> 單一執行） ----------
    def _latest_decision(self, symbol):
        for d in self.decisions:
            if d.get("symbol") == symbol:
                return d
        return None

    def _jev_problem(self, c):
        """候選的決策狀態提示（v1.1：未取得 JEV 決策 ≠ JEV 決定等待，必須分開）。"""
        d = self._latest_decision(c.get("symbol"))
        if not d:
            return ""
        if d.get("status") == "NO_JEV_DECISION":
            return "未取得 JEV 決策（禁止下單）：" + "；".join(d.get("reasons") or [])
        if d.get("status") == "RISK_REJECTED":
            return "JEV 已選中計畫，但被風控拒絕（風控不改選）"
        return ""

    def _problem_reason(self, c):
        risk = getattr(self, "current_risk", {}) or {}
        if risk.get("brake"):
            return "風險剎車中：" + "；".join(risk.get("brake_reasons", []))
        return self._jev_problem(c)

    def _make_candidate_plan(self, cand, side, trigger):
        """候選計畫（待 JEV 選擇）：有期限、綁快照與內容雜湊，條件成立≠已授權下單。"""
        self.plan_seq += 1
        plan = plans_mod.make_plan(
            self.plan_seq, cand["symbol"], side, trigger,
            (self.last_snapshot or {}).get("snapshot_id"),
            cand.get("price"),
            ["macd_reverse", "plan_expired", "risk_brake"],
            ttl_seconds=float(self.cfg.get("plan_ttl_seconds", 900)),
            exit_policy="atr_tpsl_1.5_3.0_time24h")
        self.plans.insert(0, plan)
        self.plans = self.plans[:10]
        return plan

    def request_review(self, symbol, auto=False):
        """相容舊 API 名稱；v1.1 起改為「送 JEV 決策鏈」（分析僅供證據）。"""
        return self.request_decision(symbol, auto=auto)

    def request_decision(self, symbol, auto=False, cand=None, plan=None):
        with self.lock:
            if self.decision_busy:
                return {"ok": False, "reason": "decision_in_progress"}
            cand = cand or self.deep_cache.get(symbol)
            if cand is None:
                return {"ok": False, "reason": "candidate_not_in_deep_list"}
            if plan is None:
                side = cand.get("macd_side") or cand.get("rule_direction")
                if side not in ("long", "short"):
                    return {"ok": False, "reason": "no_directional_candidate"}
                plan = self._make_candidate_plan(
                    cand, side, {"type": "manual_decision_trigger" if not auto else "macd_cross_15m"})
            self.decision_busy = True
            cand = dict(cand)
        threading.Thread(target=self._decision_worker, args=(cand, plan), daemon=True).start()
        self._event("decision", symbol,
                    f"已啟動 JEV 決策鏈（{plan['plan_id']}，六模型分析→JEV 最終決策→風控→執行）")
        self._set_orb("JEV_DECISION", symbol, plan["side"],
                      f"JEV 決策中 {symbol}", f"候選計畫 {plan['plan_id']}，等六模型分析與 JEV 選擇")
        return {"ok": True, "symbol": symbol, "plan_id": plan["plan_id"]}

    def _analysis_evidence(self, symbol, packet):
        """External analysis requires an explicit configuration opt-in."""
        if not self.cfg.get("external_analysis_enabled", False):
            return {"votes": {}, "consensus": "no_trade", "support": 0,
                    "source": "DISABLED", "reason": "external_analysis_not_enabled"}
        fresh = self._fresh_analysis(symbol)
        if fresh is not None:
            return fresh
        try:
            return multi_review.review(packet, os.path.dirname(self.base_dir), enabled=True)
        except Exception:
            return {"votes": {}, "consensus": "no_trade", "support": 0,
                    "error": "external_analysis_unavailable"}

    def _decision_worker(self, cand, plan):
        """決策鏈背景工作：六模型分析（證據）-> JEV Choice -> 風控 -> 單一執行。

        任一階段失敗都如實記錄；沒有有效 JEV 決策，絕不建立策略委託。
        """
        symbol = plan["symbol"]
        try:
            packet = multi_review.build_packet(
                symbol, cand, None,
                getattr(self, "current_kelly", {}), getattr(self, "current_risk", {}))
            packet["candidate_plan"] = {
                "plan_id": plan["plan_id"], "side": plan["side"],
                "plan_hash": plan.get("content_hash"), "expires_epoch": plan.get("expires_epoch")}
            packet["question"] = ("Analyze this candidate plan and vote long/short/no_trade. "
                                  "Your vote is evidence only; a separate final authority decides.")
            analysis = self._analysis_evidence(symbol, packet)
            votes = dict(analysis.get("votes") or {})
            votes = self._with_local_seats(votes)

            with self.lock:
                feed = self.feed.snapshot()
                px = feed["prices"].get(symbol)
                account = self.broker.snapshot(feed["prices"])
                risk = getattr(self, "current_risk", {}) or {}
                kelly = getattr(self, "current_kelly", {}) or {}
            account_hint = {
                "equity_usd": account["equity"], "cash_usd": account["cash"],
                "open_positions": account["open_positions"],
                "free_slots": max(0, int(self.cfg["max_concurrent_positions"]) - account["open_positions"]),
                "closed_trades": account["closed_trades"], "realized_pnl": account["realized_pnl"],
            }
            options = jev_decision.build_options(plan)
            state = {
                "task": "Final trading decision for this single trigger. Choose exactly one option.",
                "symbol": symbol,
                "decision_snapshot": {
                    "analysis_snapshot_id": plan.get("snapshot_id"),
                    "plan_reference_price": plan.get("reference_price"),
                    "current_price": px, "feed_age_seconds": feed.get("age_seconds"),
                    "candidate_set_hash": jev_decision.candidate_set_hash(options, plan.get("content_hash") or ""),
                },
                "candidate_plan": {
                    "plan_id": plan["plan_id"], "side": plan["side"], "trigger": plan.get("trigger"),
                    "plan_hash": plan.get("content_hash"),
                    "exit_policy": "ATR stop 1.5x / take-profit 3.0x / 24h time stop (standing protection)",
                },
                "market_evidence": {
                    "orderbook": cand.get("orderbook"), "price_action": cand.get("price_action"),
                    "rule_direction": cand.get("rule_direction"), "rule_score": cand.get("rule_score"),
                },
                "six_model_analysis_evidence_only": {
                    "votes": {k: {"decision": v.get("decision"), "confidence": v.get("confidence"),
                                  "reason": v.get("reason")} for k, v in votes.items()},
                    "reference_consensus_not_authoritative": analysis.get("consensus"),
                    "note": "Model votes are evidence only; they cannot authorize execution.",
                },
                "account": account_hint,
                "risk_precheck": {
                    "brake": bool(risk.get("brake")), "margin_pct": risk.get("margin_pct"),
                    "drawdown_pct": risk.get("drawdown_pct"), "kelly_state": kelly.get("state"),
                    "max_suggested_notional_usd": kelly.get("suggested_notional"),
                },
            }
            resp = jev_client.decide_choice(
                typesafe_api_key(), self.cfg.get("jev_model", "jev-latest"), state, options,
                timeout=int(self.cfg.get("jev_decision_timeout_seconds", 60)))
            receipt = jev_decision.evaluate_response(
                resp, options, plan, time.time(),
                receipt_ttl_s=int(self.cfg.get("jev_receipt_ttl_seconds", 120)))
            executed, order_id = False, None
            with self.lock:
                self.decision_seq += 1
                receipt["decision_id"] = f"DEC-{self.decision_seq:04d}"
                receipt["decision_snapshot_id"] = f"DSNAP-{self.decision_seq:04d}"
                receipt["decision_snapshot_hash"] = content_hash(state["decision_snapshot"])
                receipt["analysis_snapshot_id"] = plan.get("snapshot_id")
                receipt["at"] = _now()
                receipt["trigger"] = plan.get("trigger")
                receipt["analysis"] = {"votes": votes, "consensus": analysis.get("consensus"),
                                       "support": analysis.get("support", 0)}
                self.decisions.insert(0, receipt)
                self.decisions = self.decisions[:10]
                self.jev_health.update({
                    "status": "READY" if resp.get("ok") else "UNAVAILABLE",
                    "actual_model": resp.get("model") or self.jev_health.get("actual_model"),
                    "last_decision_at": receipt["at"],
                    "last_latency_ms": resp.get("latency_ms"),
                    "last_error": None if resp.get("ok") else resp.get("error")})
                if receipt["status"] == "NO_JEV_DECISION":
                    self.jev_health["blocked_count"] = int(self.jev_health.get("blocked_count", 0)) + 1
                    self._event("decision", symbol,
                                "未取得 JEV 決策，禁止下單：" + "；".join(receipt.get("reasons") or []))
                    self._set_orb("NO_JEV_DECISION", symbol, "watch",
                                  f"未取得 JEV 決策 {symbol}", "JEV 缺答不等於等待，本次不下單")
                elif receipt["status"] == "JEV_WAIT":
                    self._event("decision", symbol, f"JEV 決定等待（{receipt['decision_id']}）")
                    self._set_orb("SCANNING", symbol, "watch",
                                  f"JEV 決定等待 {symbol}", f"決策 {receipt['decision_id']}：本次不交易")
                else:
                    executed, order_id = self._execute_decision(receipt, plan, cand)
                rec = {"symbol": symbol, "at": receipt["at"], "at_epoch": time.time(),
                       "problem": "JEV 決策鏈（分析僅供證據）",
                       "votes": votes, "consensus": analysis.get("consensus"),
                       "support": analysis.get("support", 0), "executed": executed,
                       "order_id": order_id, "decision_id": receipt["decision_id"],
                       "jev_status": receipt["status"],
                       "jev_choice": (receipt.get("jev") or {}).get("choice"),
                       "plan": {"plan_id": plan["plan_id"], "content_hash": plan.get("content_hash")},
                       "revalidate_reasons": receipt.get("risk_reasons", []),
                       "snapshot_id": plan.get("snapshot_id")}
                self.reviews.insert(0, rec)
                self.reviews = self.reviews[:10]
                self._save_runtime()
            # 開源影子決策（同題對照，不授權下單；獨立緒跑，本機模型載入慢也不卡決策鏈）
            if self.cfg.get("local_decider_enabled", False):
                threading.Thread(target=self._shadow_worker,
                                 args=(receipt, state, options), daemon=True).start()
        finally:
            with self.lock:
                self.decision_busy = False

    def _shadow_worker(self, receipt, state, options):
        """Optional explicitly configured local comparison; no bundled model."""
        if not self.cfg.get("local_decider_enabled", False):
            return
        shadow = local_decider.decide_local(
            state, options,
            self.cfg.get("local_decider_base_url", ""),
            self.cfg.get("local_decider_model", ""),
            timeout=int(self.cfg.get("local_decider_timeout_seconds", 240)))
        with self.lock:
            receipt["shadow_local"] = shadow
            receipt["shadow_agree"] = (
                shadow.get("choice") == receipt.get("selected_option")
                if shadow.get("status") == "OK" and receipt.get("selected_option") else None)
            self._save_runtime()
        if shadow.get("status") == "OK":
            self._event("decision", receipt.get("symbol"),
                        f"開源影子決策 {shadow['choice']}（本機 {shadow.get('model')}，"
                        f"與 JEV {'一致' if receipt['shadow_agree'] else '分歧'}，僅對照）")

    # ---------- 多 AI 持續輪巡（CLI 席位不停工；決策複用最新分析） ----------
    def _with_local_seats(self, votes):
        """補上本機席位：MUSE 由 rules-summary（規則彙整，非 MUSE API） 本機彙整擔任（即時）；QWEN 預設未提供。"""
        votes = dict(votes or {})
        synth = votes.get("synth") or {}
        if synth.get("decision") in ("long", "short", "no_trade"):
            votes["muse"] = {"decision": synth["decision"],
                             "confidence": synth.get("confidence", 0),
                             "reason": (synth.get("reason") or "") + "（MUSE·rules-summary（規則彙整，非 MUSE API） 本機彙整）",
                             "latency_ms": synth.get("latency_ms", 0)}
        else:
            votes.setdefault("muse", {"decision": "unavailable", "confidence": 0,
                                      "reason": "本機彙整尚未產生（rules-summary（規則彙整，非 MUSE API））"})
        votes["qwen"] = {"decision": "unavailable", "confidence": 0,
                         "reason": "未提供此適配器"}
        return votes

    def _fresh_analysis(self, symbol):
        """找該幣最新且夠新鮮的分析紀錄（決策複用，省 CLI 等待）。無則 None。"""
        max_age = float(self.cfg.get("fresh_analysis_max_age_seconds", 600))
        now = time.time()
        for rec in self.reviews:
            if rec.get("symbol") != symbol:
                continue
            at = rec.get("at_epoch")
            if at is None or now - at > max_age:
                return None
            if not rec.get("votes"):
                return None
            return {"votes": rec["votes"], "consensus": rec.get("consensus"),
                    "support": rec.get("support", 0), "_age_epoch": at}
        return None

    def _carousel_pick(self):
        now = time.time()
        cd = float(self.cfg.get("carousel_symbol_cooldown_seconds", 600))
        with self.lock:
            cands = sorted(self.deep_cache.values(),
                           key=lambda c: (1 if c["price_action"].get("macd_cross") in ("golden", "death") else 0,
                                          c["rule_strength"]), reverse=True)
            held = {p["symbol"] for p in self.broker.positions}
            opened = {o["symbol"] for o in self.orders
                      if o["status"] in ("WAITING_FILL", "PARTIALLY_FILLED", "PENDING_CONFIRM")}
            for c in cands:
                sym = c["symbol"]
                if sym in held or sym in opened:
                    continue
                if now - self.analysis_cooldown.get(sym, 0) < cd:
                    continue
                return dict(c)
        return None

    def _carousel_loop(self):
        if not self.cfg.get("external_analysis_enabled", False):
            return
        import os as _os
        while True:
            time.sleep(8)
            try:
                if self.carousel_busy or self.decision_busy:
                    continue
                cand = self._carousel_pick()
                if cand is None:
                    continue
                symbol = cand["symbol"]
                self.carousel_busy = True
                self.analysis_cooldown[symbol] = time.time()
                packet = multi_review.build_packet(
                    symbol, cand, None,
                    getattr(self, "current_kelly", {}), getattr(self, "current_risk", {}))
                packet["question"] = ("Continuous watchlist analysis. Vote long/short/no_trade with confidence. "
                                      "Your vote is evidence for the final decision authority.")
                try:
                    analysis = multi_review.review(packet, _os.path.dirname(self.base_dir), enabled=True)
                except Exception as e:
                    analysis = {"votes": {}, "consensus": "no_trade", "support": 0, "error": str(e)[:120]}
                votes = dict(analysis.get("votes") or {})
                votes = self._with_local_seats(votes)
                with self.lock:
                    rec = {"symbol": symbol, "at": _now(), "at_epoch": time.time(),
                           "problem": "持續分析輪巡（多 AI 即時研判）",
                           "votes": votes, "consensus": analysis.get("consensus"),
                           "support": analysis.get("support", 0), "executed": False,
                           "order_id": None, "decision_id": None,
                           "jev_status": None, "jev_choice": None,
                           "plan": None, "revalidate_reasons": [],
                           "snapshot_id": (self.last_snapshot or {}).get("snapshot_id")}
                    self.reviews.insert(0, rec)
                    self.reviews = self.reviews[:10]
                    self._save_runtime()
                self._event("analysis", symbol,
                            f"輪巡分析完成：參考共識 {analysis.get('consensus')}（{analysis.get('support', 0)} 席），"
                            f"外部支持 {multi_review.external_support(votes)}")
                self._maybe_consensus_trigger(cand, votes)
            except Exception:
                pass
            finally:
                self.carousel_busy = False

    def _maybe_consensus_trigger(self, cand, votes):
        """外部四席 >=3 席同向 + 濾網通過 -> 產生計畫送 JEV（找交易更快，JEV 仍是最終決策）。"""
        side, support = multi_review.external_support(votes)
        if side not in ("long", "short"):
            return
        if support < int(self.cfg.get("consensus_trigger_min_seats", 3)):
            return
        symbol = cand["symbol"]
        with self.lock:
            if self.decision_busy or self.paused_new:
                return
            if time.time() - self.decision_cooldown.get(symbol, 0) < float(self.cfg.get("decision_cooldown_seconds", 900)):
                return
            if any(p["symbol"] == symbol for p in self.broker.positions):
                return
            if any(o["symbol"] == symbol and o["status"] in ("WAITING_FILL", "PARTIALLY_FILLED", "PENDING_CONFIRM")
                   for o in self.orders):
                return
            freasons = entry_filter_reasons(cand["price_action"], cand["orderbook"], side, self.cfg)
            if freasons:
                return
            plan = self._make_candidate_plan(
                cand, side, {"type": "ai_consensus", "support": support,
                             "source": "carousel_external_seats"})
            self.decision_cooldown[symbol] = time.time()
        r = self.request_decision(symbol, auto=True, cand=cand, plan=plan)
        if r.get("ok"):
            self._event("decision", symbol,
                        f"AI 共識觸發（外部 {support} 席同向 {side}）→ 已送 JEV 決策（{plan['plan_id']}）")

    def _execute_decision(self, receipt, plan, cand):
        """JEV 已選中計畫後：最新狀態重驗 + 確定性風控（只能拒絕，不能改選）-> 單一送單。

        呼叫端須持有 self.lock。回 (executed, order_id)。
        """
        symbol = plan["symbol"]
        feed = self.feed.snapshot()
        px = feed["prices"].get(symbol)
        risk = getattr(self, "current_risk", {}) or {}
        kelly = getattr(self, "current_kelly", {}) or {}
        latest = self.deep_cache.get(symbol) or cand
        ok, reasons = plans_mod.revalidate(plan, {
            "price": px, "spread_bps": (latest.get("orderbook") or {}).get("spread_bps"),
            "brake": bool(risk.get("brake")), "paused_new": self.paused_new,
            "has_position": any(p["symbol"] == symbol for p in self.broker.positions),
            "has_open_order": any(o["symbol"] == symbol and o["status"] in
                                  ("WAITING_FILL", "PARTIALLY_FILLED", "PENDING_CONFIRM") for o in self.orders),
            "data_stale": (feed.get("age_seconds") or 0) > 15})
        size = float(kelly.get("suggested_notional") or self.cfg.get("simple_position_usd", 5000.0))
        extra = []
        if len(self.broker.positions) >= int(self.cfg["max_concurrent_positions"]):
            extra.append("同時持倉已達上限")
        if px and size / 3.0 + size * float(self.cfg["fee_taker_pct"]) / 100.0 > self.broker.cash:
            extra.append("可用資金不足")
        all_reasons = list(reasons) + extra
        trigger_text = (f"JEV 決策 {receipt['decision_id']} 選中 {plan['plan_id']}"
                        f"（{plan['side']}，觸發 {plan.get('trigger')}）")
        if all_reasons:
            risk_markers = ("暫停", "剎車", "持倉", "委託", "資金", "上限")
            receipt["status"] = ("RISK_REJECTED" if any(
                any(m in r for m in risk_markers) for r in all_reasons) else "PLAN_INVALIDATED")
            receipt["risk_reasons"] = all_reasons
            o = self._new_order(symbol, plan["side"], size / px if px else 0, trigger_text)
            o["decision_id"], o["plan_id"] = receipt["decision_id"], plan["plan_id"]
            o["origin"] = "JEV_DECISION"
            o["jev_summary"] = f"JEV 選中 {plan['plan_id']}，但被風控拒絕（風控不改選其他計畫）"
            o["risk_reasons"] = all_reasons
            self._order_step(o, "sizing_risk", "blocked",
                             "風控拒絕：" + "；".join(all_reasons), "RISK_REJECTED")
            self._event("decision", symbol,
                        f"JEV 選中 {plan['plan_id']} 但風控拒絕：{'；'.join(all_reasons)}")
            self._set_orb("RISK_REJECTED", symbol, plan["side"],
                          f"風控拒絕 {symbol}", "JEV 原選擇保留稽核，不改選：" + "；".join(all_reasons))
            return False, o["id"]
        qty = size / px if px else 0.0
        o = self._new_order(symbol, plan["side"], qty, trigger_text)
        o["decision_id"], o["plan_id"], o["plan_hash"] = (
            receipt["decision_id"], plan["plan_id"], plan.get("content_hash"))
        o["origin"] = "JEV_DECISION"
        conf = (receipt.get("jev") or {}).get("confidence")
        o["jev_summary"] = (f"JEV 最終決策選中 {plan['plan_id']}"
                            + (f"（信心 {conf:.2f}，非勝率）" if isinstance(conf, (int, float)) else ""))
        used, why = jev_decision.consume_receipt(receipt, o["id"], time.time())
        if not used:
            receipt["status"] = "PLAN_INVALIDATED"
            receipt["risk_reasons"] = [f"決策回執不可用：{why}"]
            self._order_step(o, "sizing_risk", "blocked", f"決策回執不可用：{why}", "RISK_REJECTED")
            self._event("decision", symbol, f"決策回執不可用（{why}），未送單")
            return False, o["id"]
        receipt["intent_id"] = o["id"]
        receipt["final_outcome"] = "ORDER_SUBMITTED"
        self._order_step(o, "sizing_risk", "done",
                         f"凱莉艙位 ${size:,.0f}（程式計算，JEV 不定量）-> 數量 {qty:.6g}", None)
        self._order_step(o, "submit", "done", "已送出模擬委託，等待交易所回報", "WAITING_FILL")
        self._event("order", symbol, f"{o['id']} JEV 決策 {receipt['decision_id']} 已送出模擬委託 {plan['side']}")
        self._set_orb("SIGNAL", symbol, plan["side"],
                      f"JEV 已決策 {symbol} {plan['side']}", f"訂單 {o['id']} 等待模擬成交回報")
        return True, o["id"]

    # ---------- 深層 + Jev ----------
    def _strategy_triggers(self, ranked, marks):
        """MACD 交叉只產生候選計畫，不直接下單（v1.1）。

        流程：交叉 -> 濾網 -> 候選計畫 -> 啟動單一 JEV 決策鏈（背景）。
        反向交叉不再主動平倉；出場只走 ATR 停損停利／24h 時間停損（既定保護政策）
        與使用者手動平倉。
        """
        started = False
        for c in ranked:
            cross = c["price_action"].get("macd_cross")
            if cross not in ("golden", "death"):
                c["gate_allowed"], c["gate_reasons"] = False, ["等待 MACD 交叉（15m）"]
                continue
            side = "long" if cross == "golden" else "short"
            c["macd_side"] = side
            key = (c["symbol"], c.get("bar_time"), cross)
            if key in self._macd_seen:
                c["gate_allowed"], c["gate_reasons"] = False, ["本根 K 已觸發過，等下一根"]
                continue
            if self.paused_new:
                c["gate_allowed"], c["gate_reasons"] = False, ["已暫停新增交易"]
                continue
            if any(p["symbol"] == c["symbol"] for p in self.broker.positions):
                c["gate_allowed"], c["gate_reasons"] = False, ["已有同商品持倉"]
                continue
            if any(o["symbol"] == c["symbol"] and o["status"] in ("WAITING_FILL", "PARTIALLY_FILLED")
                   for o in self.orders):
                c["gate_allowed"], c["gate_reasons"] = False, ["已有同商品未完成委託"]
                continue
            self._macd_seen.add(key)
            freasons = entry_filter_reasons(c["price_action"], c["orderbook"], side, self.cfg)
            if freasons:
                self.filtered_total += 1
                c["decision_status"] = "NO_ELIGIBLE_PLAN"
                c["gate_allowed"], c["gate_reasons"] = False, ["濾網擋下：" + "；".join(freasons)]
                continue
            plan = self._make_candidate_plan(
                c, side, {"type": "macd_cross_15m", "cross": cross, "bar_time": c.get("bar_time")})
            c["plan_id"] = plan["plan_id"]
            if not started:
                r = self.request_decision(c["symbol"], auto=True, cand=c, plan=plan)
                if r.get("ok"):
                    started = True
                    c["gate_allowed"] = False
                    c["gate_reasons"] = ["候選成立，已送 JEV 決策鏈（分析→JEV→風控→執行）"]
                else:
                    c["gate_allowed"], c["gate_reasons"] = False, [f"決策鏈未啟動：{r.get('reason')}"]
            else:
                c["gate_allowed"] = False
                c["gate_reasons"] = ["候選成立，本輪決策名額已用，等下一輪觸發"]
        return []

    def _deep_and_jev(self, live_rows, marks):
        cfg = self.cfg
        open_syms = {p["symbol"] for p in self.broker.positions}
        pool = [r for r in live_rows
                if r["quote_volume"] >= float(cfg["deep_min_quote_volume_usd"]) or r["symbol"] in open_syms]
        pool.sort(key=lambda r: abs(r["live_score"]), reverse=True)
        selected = pool[: int(cfg["deep_top_n"])]
        for r in pool:
            if r["symbol"] in open_syms and r not in selected:
                selected.append(r)
        focus_depth = {"bids": [], "asks": []}
        for r in selected:
            sym = r["symbol"]
            try:
                depth = self.client.depth(sym, limit=50)
                bars = parse_klines(self.client.klines(sym, interval=cfg["timeframe"], limit=120))
            except Exception:
                continue
            if not bars:
                continue
            # 最後一根以即時價覆寫，讓深層判斷跟上盤面
            bars[-1] = {**bars[-1], "c": marks.get(sym, bars[-1]["c"])}
            ob, pa = orderbook_features(depth), price_action_features(
                bars, macd_params=(int(cfg.get("macd_fast", 12)), int(cfg.get("macd_slow", 26)), int(cfg.get("macd_signal", 9))))
            if ob is None or pa is None:
                continue
            rule = rule_score(ob, pa)
            self.deep_cache[sym] = {
                "symbol": sym, "bar_time": bars[-1]["t"], "price": marks.get(sym, r["price"]),
                "change_pct_24h": r["change_pct_24h"], "quote_volume": r["quote_volume"],
                "orderbook": {k: round(v, 6) if isinstance(v, float) else v for k, v in ob.items()},
                "price_action": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in pa.items()},
                "bars": bars[-60:],
                "depth": {"bids": [[float(p), float(q)] for p, q in (depth.get("bids") or [])[:12]],
                          "asks": [[float(p), float(q)] for p, q in (depth.get("asks") or [])[:12]]},
                **rule,
            }
            if sym == (pool[0]["symbol"] if pool else None):
                focus_depth = self.deep_cache[sym]["depth"]
        # 只保留最近深掃名單，避免舊訊號冒充即時
        keep = {r["symbol"] for r in selected}
        self.deep_cache = {k: v for k, v in self.deep_cache.items() if k in keep}

        ranked = sorted(self.deep_cache.values(), key=lambda c: c["rule_strength"], reverse=True)
        for c in ranked:  # 決策球摘要仍可用（事實彙整，不代替 JEV 下結論）
            c["claude_brief"] = jev_client.claude_brief(
                c["orderbook"], c["price_action"], {"rule_direction": c.get("rule_direction")})
        # v1.1 單一路徑：交叉只產生候選，是否下單一律由 JEV 決策鏈決定
        self._strategy_triggers(ranked, marks)
        if ranked and self.orb["state"] not in ("EXECUTED", "JEV_DECISION", "NO_JEV_DECISION", "RISK_REJECTED"):
            top = ranked[0]
            self._set_orb("SIGNAL" if top["rule_direction"] != "none" else "SCANNING",
                          top["symbol"], top["rule_direction"],
                          f"深層最強 {top['symbol']} {top['rule_direction']}",
                          f"規則分 {top['rule_score']:+.3f}，等 MACD 交叉產生候選後送 JEV 決策")
        return []
