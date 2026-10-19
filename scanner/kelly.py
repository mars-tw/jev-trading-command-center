# -*- coding: utf-8 -*-
"""凱莉艙位：以已平倉實績估計優勢，無正期望時自動降艙避免爆倉。

公式：b = 平均獲利/平均虧損，p = 勝率，q = 1-p
      凱莉 f* = p - q/b；期望值/筆 = p*均獲利 - q*均虧損
實際艙位採分數凱莉（預設半凱莉）並設上限；樣本不足用探測艙，
期望值不正時降為探測艙以下，並由引擎的風險剎車（保證金/回撤）把關。
凱莉只決定大小，不保證獲利：實績惡化時 f* 會自行縮小。
"""


def compute_kelly(closed, cfg, equity):
    pnls = [float(c.get("pnl_net", 0.0)) for c in closed or []]
    n = len(pnls)
    base = float(cfg.get("simple_position_usd", 5000.0))
    out = {
        "trades": n, "win_rate": None, "payoff_b": None, "expectancy_usd": None,
        "kelly_full": None, "kelly_applied": 0.0, "suggested_notional": base,
        "state": "insufficient_sample", "state_text": f"樣本不足（{n} 筆），使用探測艙",
    }
    if n == 0 or equity <= 0:
        out["suggested_notional"] = round(base * float(cfg.get("kelly_probe_factor", 0.5)), 2)
        return out
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    p = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    out["win_rate"] = round(p, 4)
    out["payoff_b"] = round(avg_win / avg_loss, 4) if avg_loss > 0 else None
    out["expectancy_usd"] = round(p * avg_win - (1 - p) * avg_loss, 2)
    if n < int(cfg.get("kelly_min_samples", 10)):
        out["suggested_notional"] = round(base * float(cfg.get("kelly_probe_factor", 0.5)), 2)
        return out
    if avg_loss <= 0 or avg_win <= 0 or out["expectancy_usd"] <= 0:
        out["kelly_full"] = 0.0
        out["state"] = "no_edge"
        out["state_text"] = "無正期望，降低艙位避免爆倉（僅探測）"
        out["suggested_notional"] = round(base * float(cfg.get("kelly_negative_factor", 0.25)), 2)
        return out
    b = avg_win / avg_loss
    full = p - (1 - p) / b
    out["kelly_full"] = round(full, 4)
    if full <= 0:
        out["state"] = "no_edge"
        out["state_text"] = "凱莉值不正，降低艙位避免爆倉（僅探測）"
        out["suggested_notional"] = round(base * float(cfg.get("kelly_negative_factor", 0.25)), 2)
        return out
    applied = max(0.0, min(full * float(cfg.get("kelly_fraction", 0.5)),
                           float(cfg.get("kelly_cap_pct", 20.0)) / 100.0))
    out["kelly_applied"] = round(applied, 4)
    out["state"] = "positive_edge"
    out["state_text"] = f"具正期望，半凱莉艙位 {applied * 100:.1f}% 權益"
    out["suggested_notional"] = round(applied * equity, 2)
    return out


def risk_snapshot(broker, marks, cfg, kelly):
    """即時風險輸出：保證金占用、回撤、爆倉距離估算與剎車狀態。"""
    snap = broker.snapshot(marks)
    equity = snap["equity"]
    margin_used = sum(p.get("margin_locked", 0.0) for p in broker.positions)
    margin_pct = (margin_used / equity * 100.0) if equity > 0 else 0.0
    peak = max(getattr(broker, "peak_equity", equity), equity)
    drawdown_pct = ((equity / peak - 1.0) * 100.0) if peak > 0 else 0.0
    worst_liq = None
    for p in broker.positions:
        mark = marks.get(p["symbol"], p["entry_price"])
        if p["entry_price"] <= 0:
            continue
        adverse = ((p["entry_price"] - mark) / p["entry_price"] * 100.0) if p["side"] == "long" else ((mark - p["entry_price"]) / p["entry_price"] * 100.0)
        dist = 33.3 - max(0.0, adverse)  # 保證金名目 1/3 的粗估爆倉距離（示意，非交易所公式）
        worst_liq = dist if worst_liq is None else min(worst_liq, dist)
    brake_reasons = []
    if margin_pct > float(cfg.get("risk_max_margin_pct", 60.0)):
        brake_reasons.append(f"保證金占用 {margin_pct:.1f}% 超過上限")
    if drawdown_pct < -abs(float(cfg.get("risk_max_drawdown_pct", 5.0))):
        brake_reasons.append(f"回撤 {drawdown_pct:.1f}% 觸及剎車")
    return {
        "equity": equity, "margin_used": round(margin_used, 2),
        "margin_pct": round(margin_pct, 2), "peak_equity": round(peak, 2),
        "drawdown_pct": round(drawdown_pct, 2),
        "worst_liq_distance_pct": None if worst_liq is None else round(worst_liq, 2),
        "unrealized_pnl": snap["unrealized_pnl"],
        "brake": len(brake_reasons) > 0, "brake_reasons": brake_reasons,
        "kelly": kelly,
    }
