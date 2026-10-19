# -*- coding: utf-8 -*-
"""價格行為特徵：趨勢、動能、波動、突破、K 棒型態（只用已收盤 K 為主）。"""
import math


def _ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def _ema_series(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(float(v) * k + out[-1] * (1 - k))
    return out


def macd_features(closes, fast=12, slow=26, signal=9):
    """MACD(12,26,9)：回最新 MACD/訊號/柱狀與本根交叉（golden/death/none）。"""
    if len(closes) < slow + signal:
        return {"macd": None, "macd_signal": None, "macd_hist": None, "macd_cross": "none"}
    ef, es = _ema_series(closes, fast), _ema_series(closes, slow)
    line = [a - b for a, b in zip(ef, es)]
    sig = _ema_series(line, signal)
    hist = [a - b for a, b in zip(line, sig)]
    cross = "none"
    if hist[-2] <= 0 < hist[-1]:
        cross = "golden"
    elif hist[-2] >= 0 > hist[-1]:
        cross = "death"
    return {"macd": line[-1], "macd_signal": sig[-1], "macd_hist": hist[-1], "macd_cross": cross}


def _rsi(closes, period=14):
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(bars, period=14):
    if len(bars) <= period:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


def parse_klines(raw):
    bars = []
    for k in raw or []:
        try:
            bars.append(
                {
                    "t": int(k[0]),
                    "o": float(k[1]),
                    "h": float(k[2]),
                    "l": float(k[3]),
                    "c": float(k[4]),
                    "v": float(k[5]),
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    return bars


def price_action_features(bars, macd_params=(12, 26, 9)):
    if len(bars) < 60:
        return None
    closes = [b["c"] for b in bars]
    last = bars[-1]
    price = last["c"]
    ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
    macd = macd_features(closes, *macd_params)
    rsi = _rsi(closes)
    atr = _atr(bars)
    atr_pct = (atr / price * 100.0) if (atr and price > 0) else None

    def ret(n):
        return (price / closes[-1 - n] - 1.0) * 100.0 if len(closes) > n else 0.0

    highs = [b["h"] for b in bars[-21:-1]]
    lows = [b["l"] for b in bars[-21:-1]]
    hh, ll = max(highs), min(lows)
    breakout = "none"
    if price > hh:
        breakout = "up"
    elif price < ll:
        breakout = "down"

    vols = [b["v"] for b in bars]
    vol_avg = sum(vols[-21:-1]) / 20.0 if len(vols) >= 21 else 0.0
    vol_ratio = (last["v"] / vol_avg) if vol_avg > 0 else 0.0

    # VWAP（近 48 根，以典型價 x 量估算）
    window = bars[-48:]
    pv = sum(((b["h"] + b["l"] + b["c"]) / 3.0) * b["v"] for b in window)
    vv = sum(b["v"] for b in window)
    vwap = pv / vv if vv > 0 else price
    vwap_dev_pct = (price / vwap - 1.0) * 100.0 if vwap > 0 else 0.0

    # K 棒型態（最後一根已完成型態用倒數第二根對前一根判斷，避免未收盤誤判）
    pattern = "none"
    if len(bars) >= 3:
        p, c = bars[-3], bars[-2]
        body_c = c["c"] - c["o"]
        rng = max(c["h"] - c["l"], 1e-12)
        upper = c["h"] - max(c["o"], c["c"])
        lower = min(c["o"], c["c"]) - c["l"]
        if p["c"] < p["o"] and c["c"] > c["o"] and c["c"] >= p["o"] and c["o"] <= p["c"]:
            pattern = "bullish_engulfing"
        elif p["c"] > p["o"] and c["c"] < c["o"] and c["c"] <= p["o"] and c["o"] >= p["c"]:
            pattern = "bearish_engulfing"
        elif lower > 2.0 * abs(body_c) and upper < 0.3 * rng:
            pattern = "hammer"
        elif upper > 2.0 * abs(body_c) and lower < 0.3 * rng:
            pattern = "shooting_star"

    trend = "range"
    if ema20 and ema50:
        if price > ema20 > ema50:
            trend = "up"
        elif price < ema20 < ema50:
            trend = "down"

    return {
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "macd": macd["macd"],
        "macd_signal": macd["macd_signal"],
        "macd_hist": macd["macd_hist"],
        "macd_cross": macd["macd_cross"],
        "trend": trend,
        "rsi14": rsi,
        "atr": atr,
        "atr_pct": atr_pct,
        "ret_4bars_pct": ret(4),
        "ret_16bars_pct": ret(16),
        "breakout": breakout,
        "range_high_20": hh,
        "range_low_20": ll,
        "vol_ratio": vol_ratio,
        "vwap": vwap,
        "vwap_dev_pct": vwap_dev_pct,
        "pattern": pattern,
    }


def entry_filter_reasons(pa, ob, side, cfg):
    """進場濾網：MACD 交叉後、下單前的訊號品質過濾。回擋下原因 list（空=通過）。

    濾網只擋品質差的進場，不碰出場；門檻全在 cfg，可整組關閉（filter_enabled）。
    """
    if not cfg.get("filter_enabled", True):
        return []
    pa, ob = pa or {}, ob or {}
    reasons = []
    trend = pa.get("trend")
    if cfg.get("filter_trend_align", True):
        if side == "long" and trend != "up":
            reasons.append(f"趨勢未配合（{trend}，多單需 up）")
        if side == "short" and trend != "down":
            reasons.append(f"趨勢未配合（{trend}，空單需 down）")
    vol = pa.get("vol_ratio")
    min_vol = float(cfg.get("filter_min_vol_ratio", 0.8))
    if vol is not None and vol < min_vol:
        reasons.append(f"量能不足（量比 {vol:.2f} < {min_vol}）")
    atr_pct = pa.get("atr_pct")
    min_atr = float(cfg.get("filter_min_atr_pct", 0.0))
    if min_atr > 0 and atr_pct is not None and atr_pct < min_atr:
        reasons.append(f"波動不足（ATR {atr_pct:.2f}% < {min_atr}%，扣成本後難有空間）")
    spread = ob.get("spread_bps")
    max_spread = float(cfg.get("filter_max_spread_bps", 12.0))
    if spread is not None and spread > max_spread:
        reasons.append(f"價差過大（{spread:.1f}bps > {max_spread}）")
    rsi = pa.get("rsi14")
    if rsi is not None:
        if side == "long" and rsi > float(cfg.get("filter_rsi_long_max", 75.0)):
            reasons.append(f"RSI 過熱（{rsi:.0f}，追多風險）")
        if side == "short" and rsi < float(cfg.get("filter_rsi_short_min", 25.0)):
            reasons.append(f"RSI 過冷（{rsi:.0f}，追空風險）")
    imb = ob.get("imbalance_10")
    against = float(cfg.get("filter_book_against", 0.35))
    if imb is not None:
        if side == "long" and imb < -against:
            reasons.append(f"盤口反向（不平衡 {imb:+.2f}）")
        if side == "short" and imb > against:
            reasons.append(f"盤口反向（不平衡 {imb:+.2f}）")
    return reasons


def instant_score(book_imb, mom_short_pct, day_chg_pct, activity):
    """全市場即時分（每數秒更新）：正偏多、負偏空。activity 為 0..1 成交活躍度。"""
    def clip(x, lo=-1.0, hi=1.0):
        return max(lo, min(hi, x))

    s = (
        0.38 * clip(book_imb)
        + 0.34 * clip(mom_short_pct / 0.30)
        + 0.16 * clip(day_chg_pct / 5.0)
        + 0.12 * clip((activity - 0.5) * 2.0)
    )
    direction = "long" if s > 0.20 else ("short" if s < -0.20 else "watch")
    return round(s, 4), direction


def rule_score(ob, pa):
    """規則合成方向分：正偏多、負偏空；另回絕對強度 0..1（程式端權重，可稽核）。"""
    score = 0.0
    # 訂單簿（40%）：不平衡 + microprice
    score += 0.55 * ob["imbalance_10"] + 0.45 * max(-1.0, min(1.0, ob["micro_dev_bps"] / 5.0))
    ob_side = score
    pa_score = 0.0
    if pa["trend"] == "up":
        pa_score += 0.35
    elif pa["trend"] == "down":
        pa_score -= 0.35
    if pa["breakout"] == "up":
        pa_score += 0.30
    elif pa["breakout"] == "down":
        pa_score -= 0.30
    if pa["pattern"] in ("bullish_engulfing", "hammer"):
        pa_score += 0.20
    elif pa["pattern"] in ("bearish_engulfing", "shooting_star"):
        pa_score -= 0.20
    if pa["rsi14"] is not None:
        if pa["rsi14"] < 30:
            pa_score += 0.10
        elif pa["rsi14"] > 70:
            pa_score -= 0.10
    # MACD 為主決策：本根交叉給強分，未交叉時以柱狀方向給弱分
    if pa.get("macd_cross") == "golden":
        pa_score += 0.55
    elif pa.get("macd_cross") == "death":
        pa_score -= 0.55
    elif pa.get("macd_hist") is not None:
        pa_score += 0.15 if pa["macd_hist"] > 0 else -0.15
    pa_score += max(-0.25, min(0.25, pa["vwap_dev_pct"] / 4.0))
    total = 0.45 * ob_side + 0.55 * pa_score
    strength = min(1.0, abs(total))
    direction = "long" if total > 0.08 else ("short" if total < -0.08 else "none")
    return {
        "rule_direction": direction,
        "rule_score": round(total, 4),
        "rule_strength": round(strength, 4),
        "ob_component": round(ob_side, 4),
        "pa_component": round(pa_score, 4),
    }
