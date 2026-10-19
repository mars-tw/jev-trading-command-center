# -*- coding: utf-8 -*-
"""離線 DEMO 引擎：純合成、可重現的畫面資料來源（零金鑰、零網路、零模型呼叫）。

設計原則：
- 只做「畫面資料」：每個 frame 都由注入的時鐘 + 固定種子算出來，沒有任何 I/O。
- 不匯入 ScanEngine / LiveFeed / 原生 CLI / JEV 客戶端，因此不可能連線或花錢。
- 幣別一律是明顯假的 DEMO1..DEMO12，帳戶是 DEMO-PAPER，權益與損益固定不動，
  不做任何績效或獲利暗示。
- 每 8 秒輪一個結果：JEV 選中送單（PASS）→ JEV 決定等待（WAIT）→ 風控拒絕
  （RISK_REJECTED），所以 30 秒的錄影一定看得到三種狀態。
"""
import datetime
import math
import time

SOURCE = "DEMO"
PROVIDER = "DEMO"
PHASES = ("success", "wait", "risk_reject")
SCENARIOS = PHASES + ("auto",)
PHASE_SECONDS = 8.0
BUSY_SECONDS = 2.2
SCENARIO_PIN_SECONDS = 60.0
ACCOUNT_ID = "DEMO-PAPER"
EQUITY = 100000.0
STRATEGY_VERSION = "DEMO-MACD15m-JEV-v4"
JEV_MODEL = "DEMO-JEV-SIM"
DEMO_NOTE = "DEMO 合成資料，未呼叫任何模型或交易所"

# 明顯假的標的：UI 會把尾巴的 USDT 去掉，畫面上顯示 DEMO1 … DEMO12。
SYMBOLS = (
    ("DEMO1USDT", 142.5), ("DEMO2USDT", 23.84), ("DEMO3USDT", 1.0742),
    ("DEMO4USDT", 318.6), ("DEMO5USDT", 0.4381), ("DEMO6USDT", 76.2),
    ("DEMO7USDT", 9.615), ("DEMO8USDT", 204.3), ("DEMO9USDT", 2.806),
    ("DEMO10USDT", 55.41), ("DEMO11USDT", 0.1273), ("DEMO12USDT", 612.9),
)
LANES = (
    ("market", "行情結構"), ("signal", "訊號品質"), ("risk", "風險警訊"),
    ("counterevidence", "反面證據"), ("comparison", "同類比較"), ("summary", "重點摘要"),
)
# 八隻 DEMO 寵物（家族標籤一律帶 DEMO- 前綴，絕不聲稱有真實 CLI 在跑）
DEMO_WORKERS = (
    ("demo-codex-1", "CODEX", "market"), ("demo-claude-1", "CLAUDE", "signal"),
    ("demo-grok-1", "GROK", "risk"), ("demo-agy-1", "AGY", "counterevidence"),
    ("demo-codex-2", "CODEX", "comparison"), ("demo-claude-2", "CLAUDE", "summary"),
    ("demo-grok-2", "GROK", "market"), ("demo-agy-2", "AGY", "signal"),
)
SEATS = (
    ("GROK", "grok", "事件情報席"), ("CLAUDE", "claude", "策略提案席"),
    ("MUSE", "muse", "彙整/反證席"), ("CODEX", "gpt", "派工/稽核席"),
    ("QWEN", "qwen", "初篩席"), ("AGY", "gemini", "跨週期複核席"),
)
PHASE_TEXT = {
    "success": ("JEV_ACTION_SELECTED", "JEV 已選中計畫（DEMO）"),
    "wait": ("JEV_WAIT", "JEV 決定等待（DEMO）"),
    "risk_reject": ("RISK_REJECTED", "風控拒絕（DEMO）"),
}
PROBS = {"success": (0.72, 0.28, 0.58), "wait": (0.31, 0.69, 0.44),
         "risk_reject": (0.66, 0.34, 0.61)}


def _utc(epoch):
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _wave(x):
    """0..1 的平滑確定性波形（不用隨機數，frame 才能完全重現）。"""
    return 0.5 + 0.5 * math.sin(x)


class DemoEngine:
    """所有畫面資料的唯一來源；clock 與 seed 可注入，方便測試固定 frame。"""

    def __init__(self, clock=time.time, seed=20261019, port=8775, scenario="auto"):
        self.clock = clock
        self.seed = int(seed)
        self.port = int(port)
        self.started_epoch = float(clock())
        self.scenario = scenario if scenario in SCENARIOS else "auto"
        self._pinned_until = (0.0 if self.scenario == "auto" else
                              self.started_epoch + SCENARIO_PIN_SECONDS)
        self._phase_bias = (self.seed % 3)
        self.guild_paused = False
        self.paused_new = False

    # ---------- 時間軸與情境 ----------
    def elapsed(self):
        return max(0.0, float(self.clock()) - self.started_epoch)

    def set_scenario(self, kind):
        """只改合成 frame；不呼叫任何引擎 / API / 模型。"""
        if kind not in SCENARIOS:
            return None
        now = float(self.clock())
        self.scenario = kind
        self._pinned_until = 0.0 if kind == "auto" else now + SCENARIO_PIN_SECONDS
        return {
            "ok": True, "source": SOURCE, "synthetic": True, "provider": PROVIDER,
            "kind": kind, "mode": "DEMO", "jev_api_called": False,
            "native_cli_count": 0, "network_allowed": False,
            "pinned_seconds": 0 if kind == "auto" else int(SCENARIO_PIN_SECONDS),
            "note": "DEMO 情境切換只改合成畫面，不會下單也不會呼叫模型",
        }

    def set_guild_paused(self, paused):
        """DEMO 派工暫停：只讓合成寵物停下來，沒有任何行程或排程被影響。"""
        self.guild_paused = bool(paused)
        return self.guild_paused

    def phase(self):
        """回傳 (kind, cycle, u, busy)；u 是本段已過秒數。"""
        el = self.elapsed()
        cycle = int(el // PHASE_SECONDS)
        u = el - cycle * PHASE_SECONDS
        kind = PHASES[(cycle + self._phase_bias) % len(PHASES)]
        if self.scenario != "auto" and float(self.clock()) < self._pinned_until:
            kind = self.scenario
        return kind, cycle, u, u < BUSY_SECONDS

    # ---------- 合成行情 ----------
    def rows(self):
        el = self.elapsed()
        out = []
        for i, (sym, base) in enumerate(SYMBOLS):
            px = base * (1.0 + 0.0035 * math.sin(el / 7.0 + i * 0.7))
            score = round(0.62 * math.sin(el / 11.0 + i * 0.9), 3)
            direction = "long" if score > 0.18 else "short" if score < -0.18 else "watch"
            out.append({
                "symbol": sym, "price": round(px, 6),
                "change_pct_24h": round(2.4 * math.sin(el / 23.0 + i * 1.3), 2),
                "quote_volume": round(4_000_000 + 1_500_000 * _wave(el / 31.0 + i), 0),
                "book_imb": round(0.42 * math.sin(el / 9.0 + i * 1.7), 4),
                "mom_1m_pct": round(0.35 * math.sin(el / 5.0 + i * 2.1), 4),
                "live_score": score, "live_direction": direction,
                "synthetic": True, "source": SOURCE,
            })
        out.sort(key=lambda r: abs(r["live_score"]), reverse=True)
        return out

    def _bars(self, base):
        el = self.elapsed()
        bars = []
        for k in range(24):
            mid = base * (1.0 + 0.006 * math.sin(el / 13.0 + k * 0.42))
            hi = mid * 1.0024
            lo = mid * 0.9976
            op = mid * (1.0 + 0.0008 * math.cos(el / 6.0 + k))
            bars.append({"t": int(self.clock()) - (24 - k) * 900,
                         "o": round(op, 6), "h": round(hi, 6), "l": round(lo, 6),
                         "c": round(mid, 6), "v": round(900 + 400 * _wave(k + el / 8.0), 2)})
        return bars

    def _depth(self, px):
        bids = [{"price": round(px * (1 - 0.0004 * (k + 1)), 6),
                 "qty": round(120 + 35 * k, 2)} for k in range(10)]
        asks = [{"price": round(px * (1 + 0.0004 * (k + 1)), 6),
                 "qty": round(118 + 33 * k, 2)} for k in range(10)]
        return {"bids": bids, "asks": asks}

    # ---------- 決策 / 回執 ----------
    def _decision(self, kind, cycle, u):
        status, label = PHASE_TEXT[kind]
        p_exec, p_wait, conf = PROBS[kind]
        sym = SYMBOLS[cycle % len(SYMBOLS)][0]
        plan_id = "PLAN-DEMO-%04d" % cycle
        at = self.started_epoch + cycle * PHASE_SECONDS + BUSY_SECONDS
        rec = {
            "decision_id": "DEC-DEMO-%04d" % cycle,
            "decision_snapshot_id": "DSNAP-DEMO-%04d" % cycle,
            "analysis_snapshot_id": "SNAP-DEMO-%04d" % cycle,
            "status": status, "symbol": sym, "plan_id": plan_id, "plan_version": 1,
            "plan_hash": "demo%04dhash" % cycle,
            "selected_option": "WAIT" if kind == "wait" else "EXEC_" + plan_id,
            "options": [{"id": "EXEC_" + plan_id, "plan_id": plan_id},
                        {"id": "WAIT", "plan_id": None}],
            "can_execute": False, "used_intent_id": None,
            "reasons": [], "risk_reasons": [],
            "at": _utc(at), "at_epoch": at,
            "trigger": {"type": "demo_cycle", "tf": "15m"},
            "expires_at": at + 120,
            "source": SOURCE, "synthetic": True, "provider": PROVIDER,
            "jev_api_called": False, "demo": True,
            "jev": {
                "choice": "WAIT" if kind == "wait" else "EXEC_" + plan_id,
                "probabilities": {"EXEC_" + plan_id: p_exec, "WAIT": p_wait},
                "confidence": conf, "model": JEV_MODEL,
                "latency_ms": 820 + (cycle % 5) * 90,
                "source": SOURCE, "synthetic": True,
                "confidence_note": "信心＝選項集中度，不是勝率；DEMO 數值為合成",
            },
            "shadow_local": {"status": "DISABLED", "error": "DEMO：本機影子決策未啟用",
                             "source": SOURCE, "synthetic": True},
            "note": DEMO_NOTE,
        }
        if kind == "success":
            rec["used_intent_id"] = "INT-DEMO-%04d" % cycle
            rec["final_outcome"] = "ORDER_SUBMITTED" if u >= BUSY_SECONDS else None
            rec["intent_id"] = "INT-DEMO-%04d" % cycle
        elif kind == "risk_reject":
            rec["risk_reasons"] = ["DEMO：合成保證金占用超過上限",
                                   "DEMO：同標的已有合成持倉"]
        return rec

    def decisions(self):
        """最新在前，含前幾輪（DEMO 回執一樣是合成的）。"""
        kind, cycle, u, _busy = self.phase()
        out = [self._decision(kind, cycle, u)]
        for back in range(1, 4):
            c = cycle - back
            if c < 0:
                break
            k = PHASES[(c + self._phase_bias) % len(PHASES)]
            out.append(self._decision(k, c, PHASE_SECONDS))
        return out

    def _orders(self, kind, cycle, u):
        orders = []
        if u >= BUSY_SECONDS and kind == "success":
            # 決策之後才會出現委託，狀態隨本段時間推進（送出 -> 部分成交 -> 全部成交）
            status = ("WAITING_FILL" if u < 4 else
                      "PARTIALLY_FILLED" if u < 6 else "FILLED")
            orders.append(self._order(cycle, status, "entry"))
        elif u >= BUSY_SECONDS and kind == "risk_reject":
            orders.append(self._order(cycle, "RISK_REJECTED", "entry",
                                      ["DEMO：合成保證金占用超過上限"]))
        for back in range(1, 4):
            c = cycle - back
            if c < 0:
                break
            if PHASES[(c + self._phase_bias) % len(PHASES)] == "success":
                orders.append(self._order(c, "CLOSED", "exit"))
        return orders

    def _order(self, cycle, status, kind, risk_reasons=None):
        sym = SYMBOLS[cycle % len(SYMBOLS)][0]
        base = SYMBOLS[cycle % len(SYMBOLS)][1]
        at = self.started_epoch + cycle * PHASE_SECONDS + BUSY_SECONDS
        return {
            "id": "DEMO-ORD-%04d" % cycle, "symbol": sym,
            "side": "long" if cycle % 2 == 0 else "short",
            "qty": round(25.0 / base, 4), "status": status, "kind": kind,
            "trigger": "DEMO 合成觸發（15m MACD 交叉示意）",
            "created_at": _utc(at), "demo": True, "synthetic": True,
            "source": SOURCE, "provider": PROVIDER,
            "risk_reasons": list(risk_reasons or []),
            "steps": [{"stage": "sizing_risk", "status": "done",
                       "note": "DEMO 合成部位計算（不動用資金）"},
                      {"stage": "submit", "status": "done",
                       "note": "DEMO 合成委託，未送交易所"}],
        }

    # ---------- 角色 / 席位 / 事件 ----------
    def _roles(self, kind, cycle, u, busy):
        el = self.elapsed()
        work_exec = kind == "success" and u >= BUSY_SECONDS
        table = [
            ("chief", "總控", "#ff2ea6", "working", "DEMO：協調合成決策鏈"),
            ("feed", "行情員", "#37e0ff", "working", "DEMO：接收合成行情 12 對"),
            ("strategy", "策略分析員", "#ffb020", "working" if busy else "idle",
             "DEMO：檢查合成 MACD 交叉"),
            ("sizer", "部位計算員", "#2dff8f", "working" if work_exec else "idle",
             "DEMO：計算合成艙位"),
            ("risk", "風控員", "#ff4d6d",
             "blocked" if kind == "risk_reject" and not busy else "working",
             "DEMO：合成風控檢查（只能拒絕）"),
            ("exec", "交易執行員", "#dbe4f5", "working" if work_exec else "idle",
             "DEMO：追蹤合成委託回報"),
            ("position", "持倉管理員", "#2dd4bf", "working", "DEMO：追蹤合成持倉"),
            ("record", "紀錄員", "#9fb2d8", "working", "DEMO：整理合成紀錄"),
        ]
        return [{"id": rid, "name": name, "color": color, "status": status,
                 "task": task, "task_label": task, "duty": task,
                 "summary": "DEMO 合成狀態（無真實交易、無網路）",
                 "related": [], "updated_at": _utc(self.started_epoch + el),
                 "source": SOURCE, "synthetic": True}
                for rid, name, color, status, task in table]

    def _votes(self, cycle, kind):
        votes = {}
        for i, (_alias, key, _role) in enumerate(SEATS):
            if key == "qwen":
                votes[key] = {"decision": "unavailable", "confidence": 0,
                              "reason": "DEMO：此席位在示範中關閉", "latency_ms": None,
                              "source": SOURCE, "synthetic": True}
                continue
            dec = ("no_trade" if kind == "wait" and i % 2 == 0 else
                   "long" if (cycle + i) % 3 != 2 else "short")
            votes[key] = {
                "decision": dec, "confidence": round(0.45 + 0.08 * ((cycle + i) % 4), 2),
                "reason": "DEMO 合成票（證據用，未呼叫任何模型）",
                "latency_ms": 600 + 140 * ((cycle + i) % 5),
                "source": SOURCE, "synthetic": True, "provider": PROVIDER,
            }
        return votes

    def _reviews(self, kind, cycle):
        out = []
        for back in range(0, 3):
            c = cycle - back
            if c < 0:
                break
            k = kind if back == 0 else PHASES[(c + self._phase_bias) % len(PHASES)]
            at = self.started_epoch + c * PHASE_SECONDS + BUSY_SECONDS
            out.append({
                "symbol": SYMBOLS[c % len(SYMBOLS)][0], "at": _utc(at), "at_epoch": at,
                "problem": "DEMO 合成決策鏈（分析僅供證據）",
                "votes": self._votes(c, k), "consensus": "long" if k == "success" else "no_trade",
                "support": 2 if k == "success" else 1,
                "executed": k == "success", "order_id": "DEMO-ORD-%04d" % c if k == "success" else None,
                "decision_id": "DEC-DEMO-%04d" % c, "jev_status": PHASE_TEXT[k][0],
                "jev_choice": "WAIT" if k == "wait" else "EXEC_PLAN-DEMO-%04d" % c,
                "plan": {"plan_id": "PLAN-DEMO-%04d" % c, "content_hash": "demo%04dhash" % c},
                "snapshot_id": "SNAP-DEMO-%04d" % c,
                "source": SOURCE, "synthetic": True, "provider": PROVIDER,
            })
        return out

    def _models(self, busy):
        out = []
        for alias, key, role in SEATS:
            disabled = key == "qwen"
            out.append({
                "alias": alias, "entry": "DEMO 合成席位（無任何外部呼叫）",
                "model": "DEMO-%s（合成）" % alias, "role": role,
                "status": "DISABLED" if disabled else ("READY" if not busy else "IDLE"),
                "note": "DEMO：示範中關閉" if disabled else ("DEMO 合成分析中" if busy else "DEMO 合成待命"),
                "last_decision": None if disabled else ("long" if busy else "no_trade"),
                "latency_ms": None if disabled else 700,
                "updated_at": _utc(self.clock()), "source": SOURCE, "synthetic": True,
                "process_alive": False, "pid": None, "native": False,
            })
        return out

    def _events(self, kind, cycle):
        out = []
        for back in range(0, 5):
            c = cycle - back
            if c < 0:
                break
            k = kind if back == 0 else PHASES[(c + self._phase_bias) % len(PHASES)]
            sym = SYMBOLS[c % len(SYMBOLS)][0]
            t0 = self.started_epoch + c * PHASE_SECONDS
            lines = [(t0, "scan", "[DEMO] 合成掃描第 %d 輪：12 個 DEMO 標的" % (c + 1)),
                     (t0 + 0.6, "decision", "[DEMO] 送出合成決策請求 PLAN-DEMO-%04d（未呼叫 JEV）" % c)]
            if k == "success":
                lines.append((t0 + BUSY_SECONDS,
                              "decision", "[DEMO] JEV 已選中計畫 → 合成委託 DEMO-ORD-%04d 送出" % c))
            elif k == "wait":
                lines.append((t0 + BUSY_SECONDS, "decision", "[DEMO] JEV 決定等待，本輪不交易"))
            else:
                lines.append((t0 + BUSY_SECONDS,
                              "risk", "[DEMO] 風控拒絕 DEMO-ORD-%04d（合成保證金上限）" % c))
            for ts, ev_kind, text in lines:
                out.append({"t": _utc(ts), "time": _utc(ts), "kind": ev_kind,
                            "symbol": sym, "text": text, "source": SOURCE,
                            "synthetic": True, "provider": PROVIDER,
                            "jev_api_called": False})
        out.sort(key=lambda e: e["time"])
        return out[-16:]

    # ---------- 對外 frame ----------
    def status(self):
        kind, cycle, u, busy = self.phase()
        el = self.elapsed()
        rows = self.rows()
        decisions = self.decisions()
        orders = self._orders(kind, cycle, u)
        reviews = self._reviews(kind, cycle)
        top = rows[0]
        mark = top["price"]
        cand_rows = []
        for i, r in enumerate(rows[:6]):
            side = "long" if r["live_score"] >= 0 else "short"
            cand_rows.append({
                "symbol": r["symbol"], "price": r["price"], "rule_direction": side,
                "macd_side": side, "rule_score": r["live_score"],
                "rule_strength": round(abs(r["live_score"]), 3),
                "orderbook": {"imbalance_10": r["book_imb"],
                              "spread_bps": round(1.4 + 0.6 * _wave(el / 5.0 + i), 2),
                              "depth_1pct_usd": 450000 + i * 12000},
                "price_action": {"trend": "up" if side == "long" else "down",
                                 "macd_cross": "up" if side == "long" else "down",
                                 "rsi14": round(48 + 12 * math.sin(el / 9.0 + i), 1),
                                 "atr_pct": round(0.8 + 0.3 * _wave(el / 7.0 + i), 3)},
                "problem": "" if i else ("DEMO：風控拒絕示範中" if kind == "risk_reject" else ""),
                "jev_problem": "", "decision_status": decisions[0]["status"] if i == 0 else None,
                "decision_id": decisions[0]["decision_id"] if i == 0 else None,
                "source": SOURCE, "synthetic": True,
            })
        position = {
            "symbol": SYMBOLS[1][0], "side": "long", "qty": 0.25,
            "entry_price": SYMBOLS[1][1], "mark_price": SYMBOLS[1][1],
            "unrealized_pnl": 0.0, "opened_at": _utc(self.started_epoch),
            "stop_price": round(SYMBOLS[1][1] * 0.985, 4),
            "take_profit": round(SYMBOLS[1][1] * 1.03, 4),
            "source": SOURCE, "synthetic": True,
        }
        account = {
            "equity": EQUITY, "cash": EQUITY - 25.0, "initial_capital": EQUITY,
            "realized_pnl": 0.0, "unrealized_pnl": 0.0, "return_pct": 0.0,
            "win_rate_pct": 0.0, "closed_trades": 0, "open_positions": 1,
            "positions": [position], "account_id": ACCOUNT_ID,
            "source": SOURCE, "synthetic": True,
        }
        risk = {
            "brake": False, "brake_reasons": [], "margin_pct": 12.5,
            "drawdown_pct": 0.0, "worst_liq_distance_pct": 68.0,
            "suggest_pause_entries": kind == "risk_reject" and not busy,
            "kelly": {"state": "probe", "state_text": "DEMO：樣本不足，合成探路艙位",
                      "suggested_notional": 2500.0, "expectancy_usd": None, "samples": 0,
                      "fraction": 0.5, "cap_pct": 20.0},
            "source": SOURCE, "synthetic": True,
        }
        headline = ("DEMO：六席合成分析中 → 等 JEV 合成決策" if busy else
                    "DEMO：合成委託已送出" if kind == "success" else
                    "DEMO：JEV 決定等待，本輪不交易" if kind == "wait" else
                    "DEMO：風控拒絕本輪合成計畫")
        return {
            "status": "ok", "scanning": busy,
            "mode_name": "DEMO", "demo": True, "synthetic": True, "source": SOURCE,
            "provider": PROVIDER, "network_allowed": False, "native_cli_count": 0,
            "jev_api_called": False, "demo_scenario": self.scenario,
            "scanned_at": _utc(self.clock()), "scan_count": int(el // 2) + 1,
            "timeframe": "15m", "jev_available": False, "universe_size": len(SYMBOLS),
            "feed": {"poll_count": int(el) + 1, "symbols": len(SYMBOLS),
                     "age_seconds": round(el % 1.0, 1), "last_error": "",
                     "source": SOURCE, "synthetic": True},
            "live_rows": rows, "live_signals": sum(1 for r in rows if r["live_direction"] != "watch"),
            "filtered_total": 3 * (cycle + 1), "candidates": cand_rows,
            "opened": [], "closed_now": [],
            "account": account,
            "equity_curve": [{"t": _utc(self.started_epoch + k * 8), "equity": EQUITY}
                             for k in range(max(0, cycle - 11), cycle + 1)],
            "focus": {"symbol": top["symbol"], "bars": self._bars(mark),
                      "depth": self._depth(mark)},
            "orb": {"state": "JEV_DECISION" if busy else "SCANNING",
                    "symbol": decisions[0]["symbol"], "direction": "watch",
                    "headline": headline,
                    "detail": "DEMO 合成資料：不下真單、不連交易所、不呼叫模型",
                    "updated_at": _utc(self.clock()), "source": SOURCE, "synthetic": True},
            "events": self._events(kind, cycle),
            "mode": {"label": "DEMO 離線示範（合成資料）",
                     "trading": "DEMO 合成（無任何真實委託）",
                     "exchange": "DEMO 合成行情（未連線任何交易所）",
                     "account_id": ACCOUNT_ID, "paused_new": self.paused_new,
                     "risk_control": True, "strategy_version": STRATEGY_VERSION,
                     "feed_stale_demo": False, "demo": True, "synthetic": True,
                     "network_allowed": False},
            "roles": self._roles(kind, cycle, u, busy),
            "risk": risk, "orders": orders, "reviews": reviews, "decisions": decisions,
            "plans": [{"plan_id": "PLAN-DEMO-%04d" % cycle, "symbol": decisions[0]["symbol"],
                       "side": "long", "version": 1, "content_hash": "demo%04dhash" % cycle,
                       "expires_epoch": self.started_epoch + (cycle + 1) * PHASE_SECONDS,
                       "source": SOURCE, "synthetic": True}],
            "carousel_busy": busy,
            "jev_core": {"required": True, "enabled": True,
                         "status": "DEMO", "model": JEV_MODEL, "actual_model": JEV_MODEL,
                         "busy": busy, "last_decision": decisions[0],
                         "last_decision_at": decisions[0]["at"],
                         "last_latency_ms": decisions[0]["jev"]["latency_ms"],
                         "last_error": None, "blocked_count": 0,
                         "source": SOURCE, "synthetic": True, "jev_api_called": False},
            "snapshot": {"snapshot_id": "SNAP-DEMO-%04d" % cycle, "at": _utc(self.clock()),
                         "scan_count": int(el // 2) + 1, "timeframe": "15m",
                         "source": SOURCE, "synthetic": True},
            "models": self._models(busy),
            "settings": [{"label": "執行模式", "value": "DEMO 離線示範（合成資料，零網路）"},
                         {"label": "帳戶", "value": "%s（合成模擬金，非真實帳戶）" % ACCOUNT_ID},
                         {"label": "標的", "value": "DEMO1 … DEMO12（虛構）"},
                         {"label": "原生 CLI", "value": "0（DEMO 不啟動任何子行程）"},
                         {"label": "JEV", "value": "未呼叫（DEMO 合成回執）"}],
            "thresholds": {"max_spread_bps": 12.0, "risk_per_trade_pct": 0.5,
                           "max_concurrent_positions": 50, "atr_stop_mult": 1.5,
                           "atr_tp_mult": 3.0, "jev_min_entry_quality": 2,
                           "jev_min_valid_setup": 0.6, "jev_max_trap": 0.4,
                           "jev_min_confidence": 0.55, "use_risk_control": True,
                           "simple_position_usd": 5000.0},
            "note": DEMO_NOTE,
        }

    def guild(self):
        """DEMO 工作公會狀態：寵物會動，但 pid/process_alive 一律是空的、原生數 0。"""
        kind, cycle, u, busy = self.phase()
        el = self.elapsed()
        now_ms = int(self.clock() * 1000)
        workers, jobs, events = [], [], []
        for i, (wid, alias, lane) in enumerate(DEMO_WORKERS):
            working = ((int(el / 3.0) + i) % 4) != 3 and not self.guild_paused
            job_id = "DEMO-JOB-%04d-%d" % (cycle, i)
            sym = SYMBOLS[(cycle + i) % len(SYMBOLS)][0]
            lane_label = dict(LANES)[lane]
            workers.append({
                "worker_id": wid, "alias": alias, "family_label": "DEMO-" + alias,
                "display_family": "DEMO-" + alias,
                "model": "DEMO-%s（合成標籤，無真實模型）" % alias,
                "lane_id": lane, "job_id": job_id if working else None,
                "status": "running" if working else "idle",
                "task_label": ("%s｜%s（DEMO 合成）" % (lane_label, sym) if working
                               else "DEMO 合成待命巡線"),
                "symbol": sym if working else None,
                "started_at_ms": now_ms - 4000, "heartbeat_at_ms": now_ms,
                "process_alive": False, "pid": None,
                "demo_working": bool(working), "native": False,
                "source": SOURCE, "synthetic": True, "provider": PROVIDER,
                "result_summary": "",
            })
            if working:
                jobs.append({"id": job_id, "lane_id": lane, "worker_id": wid,
                             "alias": alias, "status": "running", "symbol": sym,
                             "queued_at_ms": now_ms - 6000, "started_at_ms": now_ms - 4000,
                             "completed_at_ms": None, "result_summary": "",
                             "source": SOURCE, "synthetic": True})
                events.append({"event_id": "DEMO-EV-%04d-%d" % (cycle, i),
                               "worker_id": wid, "job_id": job_id, "lane_id": lane,
                               "alias": alias, "kind": "job_started",
                               "occurred_at_ms": now_ms - 4000,
                               "summary": "[DEMO] %s 合成研究 %s" % (lane_label, sym),
                               "source": SOURCE, "synthetic": True})
        active = sum(1 for w in workers if w["demo_working"])
        return {
            "schema_version": 1, "source": SOURCE, "simulated": True, "synthetic": True,
            "demo": True, "provider": PROVIDER, "native_cli_count": 0,
            "real_cli_agents": 0, "demo_agents": len(DEMO_WORKERS),
            "network_allowed": False, "jev_api_called": False,
            "instance_id": "demo-%06d" % (self.seed % 1000000),
            "server_time_ms": now_ms, "heartbeat_at_ms": now_ms, "stale_after_ms": 5000,
            "scheduler": {"running": not self.guild_paused,
                          "capacity": len(DEMO_WORKERS), "max_capacity": 12,
                          "queued": 2, "active": active, "completed": 3 * (cycle + 1),
                          "failed": 0, "interrupted": 0, "cycle": cycle + 1,
                          "started_at_ms": int(self.started_epoch * 1000),
                          "reason": "DEMO 合成排程（無任何子行程）"},
            "lanes": [{"id": lid, "label": label, "queued": 0,
                       "active": sum(1 for w in workers if w["lane_id"] == lid and w["demo_working"]),
                       "completed": cycle + 1,
                       "latest_result": {"job_id": "DEMO-JOB-%04d" % cycle, "alias": "DEMO-CODEX",
                                         "symbol": SYMBOLS[cycle % len(SYMBOLS)][0],
                                         "verdict": "DEMO", "finding": "[DEMO] 合成研究結論",
                                         "evidence": ["[DEMO] 合成證據"], "model": "DEMO",
                                         "completed_at_ms": now_ms - 2000}}
                      for lid, label in LANES],
            "workers": workers, "jobs": jobs, "events": events,
            "disabled_aliases": [], "busy_phase": busy, "phase_kind": kind,
            "note": "DEMO 寵物是合成動畫；真實原生 CLI 數量 0",
        }

    def health(self):
        kind, cycle, _u, busy = self.phase()
        return {
            "ok": True, "mode": "DEMO", "synthetic": True, "network_allowed": False,
            "native_cli_count": 0, "jev_api_called": False, "source": SOURCE,
            "provider": PROVIDER, "demo_agents": len(DEMO_WORKERS), "real_cli_agents": 0,
            "service": "jev-command-center-demo", "port": self.port,
            "scenario": self.scenario, "phase_kind": kind, "cycle": cycle, "busy": busy,
            "scan_count": int(self.elapsed() // 2) + 1,
            "server_time_ms": int(self.clock() * 1000),
            "key_env_read": False, "paper_account_id": ACCOUNT_ID,
            "note": DEMO_NOTE,
        }

    def receipt(self, action, extra=None):
        """所有控制端點都回明確的 DEMO 回執：不呼叫引擎、不下單、不碰模型。"""
        kind, cycle, _u, _busy = self.phase()
        out = {
            "ok": True, "demo": True, "synthetic": True, "source": SOURCE,
            "provider": PROVIDER, "action": action, "applied": False,
            "mode": "DEMO", "network_allowed": False, "native_cli_count": 0,
            "jev_api_called": False, "phase_kind": kind,
            "receipt_id": "DEMO-RCPT-%04d-%s" % (cycle, action),
            "at": _utc(self.clock()),
            "reason": "demo_mode_no_real_action",
            "note": "DEMO 模式只回合成回執；沒有任何真實委託、模型或網路呼叫",
        }
        if extra:
            out.update(extra)
        return out
