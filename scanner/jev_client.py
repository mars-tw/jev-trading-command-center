# -*- coding: utf-8 -*-
"""Jev（TypeSafe System One）決策：一次呼叫、多題並行；程式端握有最終開倉權。

題型對照 TypeSafe 官方 primitives：
- direction      -> Choice(long / short / no_trade)，回 choice + probabilities + confidence
- entry_quality  -> Score(poor / fair / good / excellent)，回加權分數 0..3
- valid_setup    -> Noul，回 0..1「是」機率
- trap_risk      -> Noul，回 0..1（誘多/誘空/假突破陷阱機率）
- risk_ok        -> Noul，回 0..1
無金鑰或呼叫失敗時回傳 None，由 engine 走規則備援並在報表標示，不假裝有 AI 決策。
"""
import json
import math
import urllib.error
import urllib.request

API_URL = "https://api.typesafe.ai/v1/systemone"


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect_blocked", headers, None)


def _open_request(request, timeout):
    # A fixed HTTPS endpoint never forwards the bearer token through a redirect or ambient proxy.
    if request.full_url != API_URL or not request.full_url.startswith("https://"):
        raise urllib.error.URLError("endpoint_rejected")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirect())
    return opener.open(request, timeout=timeout)


def _probability(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 <= value <= 1)


def claude_brief(ob, pa, rule):
    """Muse 彙整給決策模型的判讀摘要：只陳述可稽核的事實與提醒，不代替 Jev 下結論。"""
    bull, bear, caution = [], [], []
    if ob["imbalance_10"] >= 0.15:
        bull.append(f"bid pressure imbalance_10={ob['imbalance_10']:+.2f}")
    if ob["imbalance_10"] <= -0.15:
        bear.append(f"ask pressure imbalance_10={ob['imbalance_10']:+.2f}")
    if ob["micro_dev_bps"] >= 1.0:
        bull.append(f"microprice above mid {ob['micro_dev_bps']:+.1f}bps")
    if ob["micro_dev_bps"] <= -1.0:
        bear.append(f"microprice below mid {ob['micro_dev_bps']:+.1f}bps")
    if pa["trend"] == "up":
        bull.append("trend up (price>EMA20>EMA50)")
    if pa["trend"] == "down":
        bear.append("trend down (price<EMA20<EMA50)")
    if pa["breakout"] == "up":
        bull.append("breakout above 20-bar high")
    if pa["breakout"] == "down":
        bear.append("breakdown below 20-bar low")
    if pa["pattern"] in ("bullish_engulfing", "hammer"):
        bull.append(f"bullish pattern {pa['pattern']}")
    if pa["pattern"] in ("bearish_engulfing", "shooting_star"):
        bear.append(f"bearish pattern {pa['pattern']}")
    if ob["spread_bps"] > 8:
        caution.append(f"spread wide {ob['spread_bps']:.1f}bps")
    if ob["wall_ask"] >= 0.35 and rule.get("rule_direction") == "long":
        caution.append(f"heavy ask wall {ob['wall_ask']:.2f} against long")
    if ob["wall_bid"] >= 0.35 and rule.get("rule_direction") == "short":
        caution.append(f"heavy bid wall {ob['wall_bid']:.2f} against short")
    if pa["rsi14"] is not None and pa["rsi14"] >= 75:
        caution.append(f"RSI overbought {pa['rsi14']:.0f}")
    if pa["rsi14"] is not None and pa["rsi14"] <= 25:
        caution.append(f"RSI oversold {pa['rsi14']:.0f}")
    if abs(pa["vwap_dev_pct"]) >= 2.0:
        caution.append(f"extended from VWAP {pa['vwap_dev_pct']:+.2f}%")
    if pa["atr_pct"] is not None and pa["atr_pct"] >= 1.5:
        caution.append(f"high volatility ATR% {pa['atr_pct']:.2f}")
    return {"bullish_factors": bull, "bearish_factors": bear, "cautions": caution}


def build_state(symbol, ob, pa, rule, account_hint, market=None, decision_bar=None):
    return {
        "symbol": symbol,
        "task": "Judge whether this Binance USDT perpetual setup is tradable now.",
        "market": market or {},
        "claude_brief": claude_brief(ob, pa, rule),
        "decision_bar": decision_bar or {},
        "orderbook": {
            "spread_bps": round(ob["spread_bps"], 3),
            "imbalance_5": round(ob["imbalance_5"], 4),
            "imbalance_10": round(ob["imbalance_10"], 4),
            "imbalance_20": round(ob["imbalance_20"], 4),
            "micro_dev_bps": round(ob["micro_dev_bps"], 3),
            "wall_bid": round(ob["wall_bid"], 4),
            "wall_ask": round(ob["wall_ask"], 4),
            "depth_1pct_usd": round(ob["depth_1pct_usd"], 2),
        },
        "price_action": {
            "trend": pa["trend"],
            "breakout": pa["breakout"],
            "pattern": pa["pattern"],
            "rsi14": None if pa["rsi14"] is None else round(pa["rsi14"], 2),
            "atr_pct": None if pa["atr_pct"] is None else round(pa["atr_pct"], 4),
            "ret_4bars_pct": round(pa["ret_4bars_pct"], 4),
            "ret_16bars_pct": round(pa["ret_16bars_pct"], 4),
            "vol_ratio": round(pa["vol_ratio"], 3),
            "vwap_dev_pct": round(pa["vwap_dev_pct"], 4),
        },
        "rule_prefilter": rule,
        "account": account_hint,
        "scoring_rubric": (
            "Tradable only when orderbook pressure and price structure agree, "
            "spread is tight, and the move is not a late chase into a wall."
        ),
    }


def build_questions():
    return {
        "direction": {
            "type": "choice",
            "instructions": (
                "Based only on `orderbook`, `price_action`, `rule_prefilter`, `claude_brief`, "
                "`market`, `account` and `decision_bar` in the state, "
                "choose the best action for the next 1-4 bars. "
                "Respect `decision_bar`: only pick long/short when quality would clear it."
            ),
            "criteria": {
                "long": "Orderbook bid pressure and bullish price structure align for upside continuation or reversal.",
                "short": "Orderbook ask pressure and bearish price structure align for downside continuation or reversal.",
                "no_trade": "Signals conflict, are too weak, too extended, or liquidity/spread makes the setup poor.",
            },
        },
        "entry_quality": {
            "type": "score",
            "instructions": "Rate the quality of entering a trade right now at market.",
            "criteria": [
                "poor: conflicting signals or bad location, do not trade",
                "fair: mixed signals, only a small probe would make sense",
                "good: orderbook and price action agree with acceptable risk",
                "excellent: strong agreement, clean breakout/rejection and healthy liquidity",
            ],
        },
        "valid_setup": {
            "type": "noul",
            "instructions": "Is this a valid, tradable setup rather than noise?",
            "criteria": {
                "true": "Orderbook and price action confirm each other and location is not extremely extended.",
                "false": "Choppy noise, conflicting signals, or chasing far from value.",
            },
        },
        "trap_risk": {
            "type": "noul",
            "instructions": "Is this likely a trap: spoofed wall, fake breakout, or late FOMO chase that will reverse?",
            "criteria": {
                "true": "High trap risk: one-sided wall without follow-through, extreme extension, or breakout into heavy opposing depth.",
                "false": "No strong trap evidence; pressure is broad and price confirms.",
            },
        },
        "risk_ok": {
            "type": "noul",
            "instructions": "Given spread, ATR percent and depth, is risk acceptable for a small leveraged futures position with a 1.5xATR stop?",
            "criteria": {
                "true": "Spread and volatility allow a defined stop without excessive slippage risk.",
                "false": "Too wide, too thin, or too volatile for a clean stop.",
            },
        },
    }


def decide(api_key, model, symbol, ob, pa, rule, account_hint, market=None, decision_bar=None, timeout=30):
    """成功回傳正規化 dict；無金鑰/失敗回傳 None（呼叫端必須標示備援，不可冒充）。"""
    if not api_key:
        return None
    payload = {
        "state": build_state(symbol, ob, pa, rule, account_hint, market=market, decision_bar=decision_bar),
        "model": model or "jev-latest",
        "questions": build_questions(),
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with _open_request(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        return None
    answers = body["answers"]
    d = answers.get("direction") or {}
    q = answers.get("entry_quality") or {}
    vs = answers.get("valid_setup") or {}
    tr = answers.get("trap_risk") or {}
    rk = answers.get("risk_ok") or {}
    parts = (d, q, vs, tr, rk)
    if not all(isinstance(value, dict) for value in parts):
        return None
    values = ((d.get("confidence"), 1), (q.get("score"), 3),
              (q.get("confidence"), 1), (vs.get("noul"), 1),
              (tr.get("noul"), 1), (rk.get("noul"), 1))
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(value) or not 0 <= value <= upper for value, upper in values):
        return None
    direction_probs = d.get("probabilities")
    direction = d.get("choice")
    if (direction not in ("long", "short", "no_trade") or
            not isinstance(direction_probs, dict) or
            set(direction_probs) != {"long", "short", "no_trade"} or
            not all(_probability(v) for v in direction_probs.values()) or
            abs(sum(direction_probs.values()) - 1) > .02 or
            direction_probs[direction] < max(direction_probs.values()) - 1e-9):
        return None
    try:
        return {
            "source": "jev",
            "model": body.get("model"),
            "direction": d.get("choice"),
            "direction_probabilities": d.get("probabilities") or {},
            "confidence": float(d.get("confidence") or 0.0),
            "entry_quality": float(q.get("score") or 0.0),
            "entry_quality_confidence": float(q.get("confidence") or 0.0),
            "valid_setup": float(vs.get("noul") or 0.0),
            "trap_risk": float(tr.get("noul") or 0.0),
            "risk_ok": float(rk.get("noul") or 0.0),
        }
    except (TypeError, ValueError):
        return None


def build_choice_questions(options):
    """單一權威 Choice（typed decision protocol §13.2）：選項綁定計畫/動作 + WAIT，程式不要求自由文字。"""
    criteria = {}
    for o in options:
        criteria[o["id"]] = o["criteria"]
    return {
        "final_decision": {
            "type": "choice",
            "instructions": (
                "You are the final trading decision authority for this trigger. "
                "Based only on the decision snapshot in `state` (market, candidate plan, "
                "six-model analysis with disagreements, account and risk precheck), "
                "choose exactly one option: execute the bound plan, or WAIT. "
                "Do not invent prices, quantities or new plans."
            ),
            "criteria": criteria,
        }
    }


def parse_choice_body(body, options):
    """Validate JSON numbers without coercing booleans, strings or non-finite values."""
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        return None
    decision = body["answers"].get("final_decision")
    if not isinstance(decision, dict):
        return None
    choice, probabilities = decision.get("choice"), decision.get("probabilities")
    confidence = decision.get("confidence")
    ids = {item.get("id") for item in options if isinstance(item, dict)}
    if (not isinstance(choice, str) or choice not in ids or
            not isinstance(probabilities, dict) or set(probabilities) != ids or
            not _probability(confidence) or
            not all(_probability(value) for value in probabilities.values())):
        return None
    if abs(sum(probabilities.values()) - 1.0) > 0.02:
        return None
    if probabilities[choice] < max(probabilities.values()) - 1e-9:
        return None
    return {"choice": choice, "probabilities": dict(probabilities),
            "confidence": confidence, "model": body.get("model"), "raw": body}


def decide_choice(api_key, model, state, options, timeout=60):
    """送出單一 Choice 決策請求。回 dict（含 ok/error/latency_ms/raw）；失敗不假裝有決策。"""
    import time as _time
    if not api_key:
        return {"ok": False, "error": "jev_key_missing", "latency_ms": 0}
    payload = {"state": state, "model": model or "jev-latest",
               "questions": build_choice_questions(options)}
    req = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST")
    t0 = _time.time()
    try:
        with _open_request(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"ok": False, "error": f"jev_request_failed:{type(e).__name__}",
                "latency_ms": int((_time.time() - t0) * 1000)}
    parsed = parse_choice_body(body, options)
    if parsed is None:
        return {"ok": False, "error": "jev_reply_malformed", "raw": body,
                "latency_ms": int((_time.time() - t0) * 1000)}
    parsed.update({"ok": True, "error": None, "latency_ms": int((_time.time() - t0) * 1000)})
    return parsed


def gate(decision, cfg):
    """程式端閘門：全部達標才允許模擬開倉。回 (allowed, reasons)。"""
    if decision is None:
        return False, ["jev_unavailable_rule_only"]
    for key, upper in (("entry_quality", 3), ("valid_setup", 1),
                       ("trap_risk", 1), ("confidence", 1), ("risk_ok", 1)):
        value = decision.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value < 0 or value > upper):
            return False, ["jev_invalid_numeric_response"]
    reasons = []
    if decision["direction"] not in ("long", "short"):
        reasons.append("direction_no_trade")
    if decision["entry_quality"] < float(cfg["jev_min_entry_quality"]):
        reasons.append("entry_quality_low")
    if decision["valid_setup"] < float(cfg["jev_min_valid_setup"]):
        reasons.append("valid_setup_low")
    if decision["trap_risk"] > float(cfg["jev_max_trap"]):
        reasons.append("trap_risk_high")
    if decision["confidence"] < float(cfg["jev_min_confidence"]):
        reasons.append("confidence_low")
    if decision["risk_ok"] < 0.5:
        reasons.append("risk_not_ok")
    return (len(reasons) == 0), reasons
