# -*- coding: utf-8 -*-
"""JEV 最終決策回執：選項綁定、回覆驗證、單次使用。

原則：
- 六模型只提供分析與候選；JEV 用單一 Choice 在「執行計畫 / WAIT」間作最終選擇。
- 回執只授權一個交易意圖；重用、過期、綁定不符一律拒絕。
- 風控只能否決本回執選定的計畫，不能改選；驗證失敗＝NO_JEV_DECISION（≠ WAIT）。
"""
import hashlib
import json
import math

WAIT_OPTION = "WAIT"


def plan_option_id(plan_id):
    return f"EXEC_{plan_id}"


def plan_hash_of(plan):
    return plan.get("plan_hash") or plan.get("content_hash") or ""


def build_options(plan):
    """固定選項集合：執行本計畫 / 等待。"""
    ph = plan_hash_of(plan)
    return [
        {"id": plan_option_id(plan["plan_id"]), "plan_id": plan["plan_id"],
         "criteria": (f"Execute plan {plan['plan_id']} v{plan.get('version', 1)}: "
                      f"{plan.get('side')} {plan.get('symbol')} "
                      f"(Binance USDT-M perpetual), trigger={plan.get('trigger')}, "
                      f"exit={plan.get('exit_policy')}, plan_hash={ph[:16]}")},
        {"id": WAIT_OPTION, "plan_id": None,
         "criteria": "Do not trade this trigger. Wait for a better setup."},
    ]


def candidate_set_hash(options, plan_hash):
    raw = json.dumps([o["id"] for o in options] + [plan_hash], sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def validate_choice(choice, probabilities, options):
    """驗證 Choice 回覆（§13.3）：已知選項、機率鍵集合、有限值、總和≈1、選中屬最高機率。"""
    reasons = []
    ids = [o["id"] for o in options]
    if choice not in ids:
        reasons.append("choice_unknown_option")
    if not isinstance(probabilities, dict) or set(probabilities) != set(ids):
        reasons.append("probabilities_keys_mismatch")
        probs = {}
    else:
        probs = probabilities
    for k, v in probs.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or v > 1:
            reasons.append(f"probability_invalid:{k}")
    if reasons:
        return False, reasons
    if probs:
        total = sum(probs.values())
        if abs(total - 1.0) > 0.02:
            reasons.append(f"probabilities_sum_off:{total:.3f}")
        if choice in probs:
            top = max(probs.values())
            if probs[choice] < top - 1e-9:
                reasons.append("choice_not_argmax")
    return (len(reasons) == 0), reasons


def evaluate_response(resp, options, plan, now, receipt_ttl_s=120):
    """把 Jev 回應（jev_client.decide_choice 結果）封裝成決策紀錄。

    回傳 dict：status ∈ JEV_ACTION_SELECTED / JEV_WAIT / NO_JEV_DECISION，
    can_execute 僅在 ACTION 且驗證通過時 True（仍須風控）。
    """
    base = {
        "selected_option": None,
        "plan_id": plan["plan_id"], "plan_version": plan.get("version", 1),
        "plan_hash": plan_hash_of(plan), "symbol": plan["symbol"],
        "candidate_set_hash": candidate_set_hash(options, plan_hash_of(plan)),
        "options": [{"id": o["id"], "plan_id": o.get("plan_id")} for o in options],
        "jev": None, "reasons": [], "can_execute": False,
        "expires_at": now + receipt_ttl_s, "used_intent_id": None,
    }
    if not isinstance(resp, dict):
        resp = {}
    if not resp or resp.get("ok") is not True:
        base.update(status="NO_JEV_DECISION",
                    reasons=[(resp or {}).get("error") or "jev_no_response"])
        return base
    confidence = resp.get("confidence")
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or
            not math.isfinite(confidence) or not 0 <= confidence <= 1):
        base.update(status="NO_JEV_DECISION", reasons=["confidence_invalid"])
        return base
    ok, reasons = validate_choice(resp.get("choice"), resp.get("probabilities") or {}, options)
    base["jev"] = {"choice": resp.get("choice"), "probabilities": resp.get("probabilities"),
                   "confidence": resp.get("confidence"), "model": resp.get("model"),
                   "latency_ms": resp.get("latency_ms"), "raw": resp.get("raw")}
    if not ok:
        base.update(status="NO_JEV_DECISION", reasons=reasons)
        return base
    if resp["choice"] == WAIT_OPTION:
        base.update(status="JEV_WAIT", selected_option=WAIT_OPTION)
        return base
    base.update(status="JEV_ACTION_SELECTED", selected_option=resp["choice"],
                can_execute=True)
    return base


def consume_receipt(receipt, intent_id, now):
    """回執單次使用：標記已被某 intent 使用；重用/過期回 (False, reason)。"""
    if receipt.get("used_intent_id"):
        return False, "receipt_already_used"
    if receipt.get("status") != "JEV_ACTION_SELECTED":
        return False, "receipt_not_actionable"
    if now > receipt.get("expires_at", 0):
        return False, "receipt_expired"
    receipt["used_intent_id"] = intent_id
    return True, None
