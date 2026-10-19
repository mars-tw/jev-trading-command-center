"""Optional tool-free public-feature review. Disabled unless explicitly opted in."""
import json
import math
import re
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from . import agent_cli

EXTERNAL_SEATS = ("gpt", "claude", "gemini", "grok")
ALIASES = {"gpt": "CODEX", "claude": "CLAUDE", "gemini": "AGY", "grok": "GROK"}


def build_packet(symbol, cand, jev, kelly, risk):
    pa, ob = cand.get("price_action", {}), cand.get("orderbook", {})
    return {"symbol": symbol,
            "rule": {"direction": cand.get("rule_direction"), "score": cand.get("rule_score")},
            "macd_1m": {key: pa.get(key) for key in ("macd_hist", "rsi14", "atr_pct")},
            "orderbook": {key: ob.get(key) for key in ("imbalance_10", "spread_bps", "micro_dev_bps")},
            # These two entries are used only by the deterministic local summary.
            "jev_first_opinion": jev, "kelly": {"state": kelly.get("state")}}


def _public_packet(packet):
    symbol = packet.get("symbol")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{3,24}", symbol):
        return None
    rule = packet.get("rule") or {}
    direction = rule.get("direction")
    if direction not in ("long", "short"):
        return None
    public = {"symbol": symbol, "candidate_direction": direction, "features": {}}
    sources = [("rule_score", rule.get("score"))]
    for parent, fields in (("macd_1m", ("macd_hist", "rsi14", "atr_pct")),
                           ("orderbook", ("imbalance_10", "spread_bps", "micro_dev_bps"))):
        value = packet.get(parent) or {}
        if isinstance(value, dict):
            sources.extend((key, value.get(key)) for key in fields)
    for key, value in sources:
        if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and abs(value) <= 1e15:
            public["features"][key] = value
    return public if public["features"] else None


def _parse_vote(text):
    """Compatibility parser: only complete typed JSON, never prose/fragment rescue."""
    value = agent_cli.loads_strict(text) if isinstance(text, str) else None
    if not isinstance(value, dict) or agent_cli.is_error_envelope(value):
        return None
    confidence = value.get("confidence")
    if (value.get("decision") not in ("long", "short", "no_trade") or
            isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or
            not math.isfinite(confidence) or not 0 <= confidence <= 1 or
            not isinstance(value.get("reason"), str)):
        return None
    return {"decision": value["decision"], "confidence": confidence,
            "reason": agent_cli.clean_text(value["reason"], 140)}


def external_support(votes):
    counts = {side: sum((votes.get(seat) or {}).get("decision") == side for seat in EXTERNAL_SEATS)
              for side in ("long", "short")}
    if not max(counts.values()) or counts["long"] == counts["short"]:
        return "no_trade", max(counts.values())
    side = max(counts, key=counts.get)
    return side, counts[side]


def _summary(packet):
    jev, rule, kelly = packet.get("jev_first_opinion") or {}, packet.get("rule") or {}, packet.get("kelly") or {}
    direction = jev.get("direction")
    if direction in ("long", "short") and direction == rule.get("direction") and kelly.get("state") != "no_edge":
        return {"decision": direction, "confidence": None,
                "reason": "Deterministic rule agreement; not a model/API call", "source": "RULES"}
    return {"decision": "no_trade", "confidence": None,
            "reason": "Deterministic rule summary; not a model/API call", "source": "RULES"}


def review(packet, root_dir=None, enabled=False):
    votes = {seat: {"decision": "unavailable", "confidence": None,
                    "reason": "External analysis disabled; explicit opt-in required"}
             for seat in EXTERNAL_SEATS}
    votes["synth"] = _summary(packet)
    public = _public_packet(packet)
    if enabled is not True or public is None:
        return {"votes": votes, "consensus": "no_trade", "support": 0, "source": "DISABLED"}
    prompt = ("Research only. Assess whether these public numeric features support the stated candidate direction. "
              "Do not use tools or make orders. Reply only with JSON "
              '{"verdict":"support|challenge|unclear","finding":"short public-feature observation",'
              '"evidence":["short evidence"]}.\n' + json.dumps(public, ensure_ascii=True))
    temp_root = tempfile.mkdtemp(prefix="jev-public-review-")
    runner = agent_cli.NativeCliRunner(temp_root, timeout_seconds=150)
    runner.discover()

    def execute(seat):
        handle = None
        start = time.monotonic()
        try:
            handle = runner.start(ALIASES[seat], "review-" + uuid.uuid4().hex, prompt)
            if isinstance(handle, agent_cli.Outcome):
                outcome = handle
            else:
                outcome = None
                while outcome is None and time.monotonic() - start <= 160:
                    outcome = handle.poll()
                    if outcome is None:
                        time.sleep(.05)
            if outcome is not None and outcome.validated:
                return {"decision": public["candidate_direction"] if outcome.verdict == "support" else "no_trade",
                        "confidence": None, "reason": outcome.finding,
                        "source": "NATIVE_CLI", "model": runner.configured_model(ALIASES[seat]),
                        "model_verified": False, "latency_ms": outcome.duration_ms}
            return {"decision": "unavailable", "confidence": None,
                    "reason": "CLI unavailable or response not validated", "source": "NATIVE_CLI"}
        except Exception:
            return {"decision": "unavailable", "confidence": None, "reason": "CLI review failed"}
        finally:
            if handle is not None and not isinstance(handle, agent_cli.Outcome):
                handle.close()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(execute, EXTERNAL_SEATS))
        votes.update(dict(zip(EXTERNAL_SEATS, results)))
    finally:
        import os
        agent_cli.recycle_owned_dir(temp_root, os.path.dirname(temp_root))
    side, support = external_support(votes)
    return {"votes": votes, "consensus": side if support >= 2 else "no_trade",
            "support": support, "source": "NATIVE_CLI"}
