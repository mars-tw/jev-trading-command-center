# -*- coding: utf-8 -*-
"""Optional local decision comparison; disabled unless explicitly configured."""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SYSTEM_PROMPT = (
    "You are a trading decision model running locally. Given a decision snapshot and a fixed "
    "option list, choose exactly one option id. Reply with ONLY a JSON object: "
    '{"choice": "<option id>", "confidence": <0..1>, "reason": "<one short sentence>"}. '
    "No markdown, no extra text. Confidence is your certainty, not a win rate."
)
MAX_PROMPT_BYTES = 17500  # Bounded optional comparison prompt.



def _scalars(d):
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items() if isinstance(v, (int, float, str, bool)) or v is None}


def build_prompt(state, options):
    """組緊湊提示詞並強制 byte 上限（證據過長時先砍理由與細節）。"""
    ev = state.get("market_evidence") or {}
    votes = (state.get("six_model_analysis_evidence_only") or {}).get("votes") or {}
    snap = {
        "symbol": state.get("symbol"),
        "candidate_plan": _scalars(state.get("candidate_plan") or {}),
        "decision_snapshot": _scalars(state.get("decision_snapshot") or {}),
        "market": {"orderbook": _scalars(ev.get("orderbook") or {}),
                   "price_action": _scalars(ev.get("price_action") or {}),
                   "rule_direction": ev.get("rule_direction"), "rule_score": ev.get("rule_score")},
        "analysis_votes": {k: {"decision": v.get("decision"), "confidence": v.get("confidence")}
                           for k, v in votes.items() if isinstance(v, dict)},
        "account": _scalars(state.get("account") or {}),
        "risk_precheck": _scalars(state.get("risk_precheck") or {}),
    }
    opts = "\n".join(f"- {o['id']}: {o['criteria']}" for o in options)
    text = (SYSTEM_PROMPT + "\nOptions:\n" + opts + "\nSnapshot: "
            + json.dumps(snap, ensure_ascii=False))
    while len(text.encode("utf-8")) > MAX_PROMPT_BYTES and snap["analysis_votes"]:
        snap["analysis_votes"].popitem()
        text = (SYSTEM_PROMPT + "\nOptions:\n" + opts + "\nSnapshot: "
                + json.dumps(snap, ensure_ascii=False))
    if len(text.encode("utf-8")) > MAX_PROMPT_BYTES:
        text = text.encode("utf-8")[:MAX_PROMPT_BYTES].decode("utf-8", "ignore")
    return text


def parse_decision_text(text, options):
    """嚴格解析本機模型回覆；格式不符/未知選項回 None。"""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None
    ids = [o["id"] for o in options]
    choice = obj.get("choice")
    if choice not in ids:
        return None
    try:
        conf = float(obj.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= conf <= 1.0):
        return None
    return {"choice": choice, "confidence": conf, "reason": str(obj.get("reason") or "")[:200]}


def _via_http(prompt, options, base_url, model, timeout):
    base = (base_url or "").rstrip("/")
    if not base:
        return None
    mdl = model
    if not mdl:
        try:
            req = urllib.request.Request(base + "/models", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            mdl = (body.get("data") or [{}])[0].get("id")
        except Exception:
            mdl = None
    if not mdl:
        return {"status": "UNAVAILABLE", "error": "no_model_listed", "latency_ms": 0}
    payload = {"model": mdl, "temperature": 0.1, "max_tokens": 220,
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"status": "UNAVAILABLE", "model": mdl,
                "error": f"request_failed:{type(e).__name__}:{str(e)[:80]}",
                "latency_ms": int((time.time() - t0) * 1000)}
    latency = int((time.time() - t0) * 1000)
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"status": "INVALID", "model": mdl, "error": "reply_shape", "latency_ms": latency}
    parsed = parse_decision_text(text, options)
    if parsed is None:
        return {"status": "INVALID", "model": mdl, "error": "reply_unparsable",
                "raw": text[:200], "latency_ms": latency}
    parsed.update({"status": "OK", "model": mdl, "latency_ms": latency})
    return parsed


def decide_local(state, options, base_url="", model="", timeout=240, strata_entry=None):
    """回影子決策紀錄 dict；任何失敗都如實標 status，不假裝有答案。"""
    prompt = build_prompt(state, options)
    # Compatibility parameter is ignored; no external script is bundled or executed.
    rec = _via_http(prompt, options, base_url, model, timeout)
    if rec is not None:
        return rec
    return {"status": "UNAVAILABLE", "error": "no_transport", "latency_ms": 0}
