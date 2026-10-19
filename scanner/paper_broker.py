# -*- coding: utf-8 -*-
"""模擬經紀：10 萬美元紙上帳戶。只記帳，不送任何真實訂單。"""
import datetime


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class PaperBroker:
    def __init__(self, cfg, state=None):
        self.cfg = cfg
        self.initial = float(cfg["initial_capital"])
        if state:
            self.cash = float(state["cash"])
            self.positions = state.get("positions", [])
            self.closed = state.get("closed", [])
            self.equity_curve = state.get("equity_curve", [])
            self.peak_equity = float(state.get("peak_equity", self.initial))
        else:
            self.cash = self.initial
            self.positions = []
            self.closed = []
            self.equity_curve = []
            self.peak_equity = self.initial

    def to_state(self):
        return {
            "cash": self.cash,
            "positions": self.positions,
            "closed": self.closed[-200:],
            "equity_curve": self.equity_curve[-500:],
            "peak_equity": self.peak_equity,
        }

    def equity(self, marks):
        # cash 已扣除開倉保證金與開倉費用；權益 = cash + 保證金 + 未實現
        eq = self.cash + sum(p["margin_locked"] for p in self.positions)
        for p in self.positions:
            px = marks.get(p["symbol"], p["entry_price"])
            eq += self._unreal(p, px)
        return eq

    def _unreal(self, p, px):
        if p["side"] == "long":
            return p["qty"] * (px - p["entry_price"])
        return p["qty"] * (p["entry_price"] - px)

    def open_position(self, symbol, side, price, atr, stop_mult, tp_mult):
        if len(self.positions) >= int(self.cfg["max_concurrent_positions"]):
            return None, "max_concurrent"
        if any(p["symbol"] == symbol for p in self.positions):
            return None, "already_open"
        if price <= 0:
            return None, "invalid_price"
        if not self.cfg.get("use_risk_control", True):
            # 無風控簡單模式：固定名目金額、無停損停利，出場靠反向訊號 close_position
            slip0 = float(self.cfg["slippage_pct"]) / 100.0
            fill0 = price * (1 + slip0) if side == "long" else price * (1 - slip0)
            qty0 = float(self.cfg.get("simple_position_usd", 5000.0)) / fill0
            notional0 = qty0 * fill0
            margin0 = notional0 / 3.0
            fee0 = notional0 * float(self.cfg["fee_taker_pct"]) / 100.0
            if margin0 + fee0 > self.cash:
                return None, "insufficient_cash"
            self.cash -= margin0 + fee0
            pos0 = {
                "symbol": symbol, "side": side, "qty": qty0, "entry_price": fill0,
                "stop": None, "take_profit": None, "atr_at_entry": atr,
                "margin_locked": margin0, "open_fee": fee0, "opened_at": _now(),
            }
            self.positions.append(pos0)
            return pos0, "opened_simple"
        if not atr or atr <= 0:
            return None, "invalid_atr"
        marks = {symbol: price}
        eq = self.equity(marks)
        risk_usd = eq * float(self.cfg["risk_per_trade_pct"]) / 100.0
        stop_dist = stop_mult * atr
        qty_by_risk = risk_usd / stop_dist
        max_notional = eq * float(self.cfg["max_position_notional_pct"]) / 100.0
        qty = min(qty_by_risk, max_notional / price)
        if qty <= 0:
            return None, "qty_zero"
        slip = float(self.cfg["slippage_pct"]) / 100.0
        fill = price * (1 + slip) if side == "long" else price * (1 - slip)
        notional = qty * fill
        # 模擬逐倉：鎖 1/3 名目作為保證金示意（最高 3x），只影響可用現金呈現
        margin = notional / 3.0
        fee = notional * float(self.cfg["fee_taker_pct"]) / 100.0
        if margin + fee > self.cash:
            return None, "insufficient_cash"
        self.cash -= margin + fee
        if side == "long":
            stop, tp = fill - stop_dist, fill + tp_mult * atr
        else:
            stop, tp = fill + stop_dist, fill - tp_mult * atr
        pos = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": fill,
            "stop": stop,
            "take_profit": tp,
            "atr_at_entry": atr,
            "margin_locked": margin,
            "open_fee": fee,
            "opened_at": _now(),
        }
        self.positions.append(pos)
        return pos, "opened"

    def open_with_qty(self, symbol, side, qty, price):
        """執行員成交後以指定數量/價格建倉（無風控記帳：無停損停利）。"""
        if len(self.positions) >= int(self.cfg["max_concurrent_positions"]):
            return None, "max_concurrent"
        if any(p["symbol"] == symbol for p in self.positions):
            return None, "already_open"
        if qty <= 0 or price <= 0:
            return None, "invalid_order"
        slip = float(self.cfg["slippage_pct"]) / 100.0
        fill = price * (1 + slip) if side == "long" else price * (1 - slip)
        notional = qty * fill
        margin = notional / 3.0
        fee = notional * float(self.cfg["fee_taker_pct"]) / 100.0
        if margin + fee > self.cash:
            return None, "insufficient_cash"
        self.cash -= margin + fee
        pos = {
            "symbol": symbol, "side": side, "qty": qty, "entry_price": fill,
            "stop": None, "take_profit": None, "atr_at_entry": None,
            "margin_locked": margin, "open_fee": fee, "opened_at": _now(),
        }
        self.positions.append(pos)
        return pos, "filled"

    def mark_and_manage(self, marks, highs=None, lows=None):
        """以最新價檢查停損/停利；有 K 線高低時用高低判定更合理。回平倉紀錄 list。"""
        highs, lows = highs or {}, lows or {}
        closed_now = []
        remain = []
        fee_rate = float(self.cfg["fee_taker_pct"]) / 100.0
        slip = float(self.cfg["slippage_pct"]) / 100.0
        for p in self.positions:
            sym = p["symbol"]
            px = marks.get(sym)
            if px is None:
                remain.append(p)
                continue
            hi, lo = highs.get(sym, px), lows.get(sym, px)
            exit_px, reason = None, ""
            # stop/take_profit 為 None（無風控模式）時不自動出場，靠訊號平倉
            if p["side"] == "long":
                if p.get("stop") is not None and lo <= p["stop"]:
                    exit_px, reason = p["stop"], "stop_loss"
                elif p.get("take_profit") is not None and hi >= p["take_profit"]:
                    exit_px, reason = p["take_profit"], "take_profit"
            else:
                if p.get("stop") is not None and hi >= p["stop"]:
                    exit_px, reason = p["stop"], "stop_loss"
                elif p.get("take_profit") is not None and lo <= p["take_profit"]:
                    exit_px, reason = p["take_profit"], "take_profit"
            if exit_px is None:
                remain.append(p)
                continue
            fill = exit_px * (1 - slip) if p["side"] == "long" else exit_px * (1 + slip)
            pnl = self._unreal(p, fill)
            exit_fee = abs(p["qty"] * fill) * fee_rate
            net = pnl - exit_fee
            self.cash += p["margin_locked"] + net
            rec = {**p, "exit_price": fill, "exit_reason": reason, "pnl_net": net, "exit_fee": exit_fee, "closed_at": _now()}
            self.closed.append(rec)
            closed_now.append(rec)
        self.positions = remain
        return closed_now

    def close_position(self, symbol, price, reason="signal_exit"):
        """訊號平倉（無風控模式以 MACD 反向交叉呼叫）。找不到回 None。"""
        for i, p in enumerate(self.positions):
            if p["symbol"] != symbol:
                continue
            slip = float(self.cfg["slippage_pct"]) / 100.0
            fill = price * (1 - slip) if p["side"] == "long" else price * (1 + slip)
            pnl = self._unreal(p, fill)
            exit_fee = abs(p["qty"] * fill) * float(self.cfg["fee_taker_pct"]) / 100.0
            net = pnl - exit_fee
            self.cash += p["margin_locked"] + net
            rec = {**p, "exit_price": fill, "exit_reason": reason, "pnl_net": net, "exit_fee": exit_fee, "closed_at": _now()}
            self.closed.append(rec)
            del self.positions[i]
            return rec
        return None

    def snapshot(self, marks):
        eq = self.equity(marks)
        unreal = sum(self._unreal(p, marks.get(p["symbol"], p["entry_price"])) for p in self.positions)
        realized = sum(c["pnl_net"] for c in self.closed)
        wins = sum(1 for c in self.closed if c["pnl_net"] > 0)
        return {
            "initial_capital": self.initial,
            "cash": round(self.cash, 2),
            "equity": round(eq, 2),
            "unrealized_pnl": round(unreal, 2),
            "realized_pnl": round(realized, 2),
            "return_pct": round((eq / self.initial - 1) * 100, 3) if self.initial else 0.0,
            "open_positions": len(self.positions),
            "closed_trades": len(self.closed),
            "win_rate_pct": round(wins / len(self.closed) * 100, 1) if self.closed else 0.0,
            "fees_paid": round(
                sum(p.get("open_fee", 0.0) for p in self.positions)
                + sum(c.get("open_fee", 0.0) + c.get("exit_fee", 0.0) for c in self.closed), 2),
            "positions": [
                {
                    **p,
                    "mark_price": marks.get(p["symbol"], p["entry_price"]),
                    "unrealized_pnl": round(self._unreal(p, marks.get(p["symbol"], p["entry_price"])), 2),
                }
                for p in self.positions
            ],
        }
