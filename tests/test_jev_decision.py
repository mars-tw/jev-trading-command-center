# -*- coding: utf-8 -*-
"""JEV 決策回執測試：無有效 JEV 不得執行、回執不可重用。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scanner import jev_decision as jd  # noqa: E402
from scanner.plans import make_plan  # noqa: E402


def _plan():
    return make_plan(1, "BTCUSDT", "long", {"type": "macd_cross", "tf": "15m"},
                     "SNAP-1", 100000.0, ["macd_reverse"])


def test_options_bind_plan_and_wait():
    p = _plan()
    opts = jd.build_options(p)
    assert [o["id"] for o in opts] == [f"EXEC_{p['plan_id']}", "WAIT"]
    assert opts[0]["plan_id"] == p["plan_id"]


def test_validate_choice_ok_and_argmax():
    opts = jd.build_options(_plan())
    ok, _ = jd.validate_choice("WAIT", {opts[0]["id"]: 0.3, "WAIT": 0.7}, opts)
    assert ok
    ok, reasons = jd.validate_choice(opts[0]["id"], {opts[0]["id"]: 0.3, "WAIT": 0.7}, opts)
    assert not ok and "choice_not_argmax" in reasons


def test_validate_choice_unknown_and_sum():
    opts = jd.build_options(_plan())
    ok, reasons = jd.validate_choice("EXEC_X", {"EXEC_X": 1.0}, opts)
    assert not ok
    ok, reasons = jd.validate_choice("WAIT", {opts[0]["id"]: 0.3, "WAIT": 0.4}, opts)
    assert not ok and any("sum" in r for r in reasons)


def test_no_jev_decision_on_failure():
    p = _plan()
    opts = jd.build_options(p)
    rec = jd.evaluate_response({"ok": False, "error": "jev_request_failed:TimeoutError"}, opts, p, now=1000)
    assert rec["status"] == "NO_JEV_DECISION" and rec["selected_option"] is None
    assert not rec["can_execute"]


def test_wait_is_not_no_decision():
    p = _plan()
    opts = jd.build_options(p)
    resp = {"ok": True, "choice": "WAIT", "probabilities": {opts[0]["id"]: 0.2, "WAIT": 0.8},
            "confidence": 0.6, "model": "jev-test", "latency_ms": 900, "raw": {}}
    rec = jd.evaluate_response(resp, opts, p, now=1000)
    assert rec["status"] == "JEV_WAIT" and not rec["can_execute"]
    assert rec["jev"]["model"] == "jev-test"


def test_action_selected_and_single_use():
    p = _plan()
    opts = jd.build_options(p)
    resp = {"ok": True, "choice": opts[0]["id"],
            "probabilities": {opts[0]["id"]: 0.75, "WAIT": 0.25},
            "confidence": 0.5, "model": "jev-test", "latency_ms": 800, "raw": {}}
    rec = jd.evaluate_response(resp, opts, p, now=1000, receipt_ttl_s=120)
    assert rec["status"] == "JEV_ACTION_SELECTED" and rec["can_execute"]
    ok, _ = jd.consume_receipt(rec, "INT-1", now=1010)
    assert ok
    ok, reason = jd.consume_receipt(rec, "INT-2", now=1011)
    assert not ok and reason == "receipt_already_used"


def test_receipt_expired_cannot_execute():
    p = _plan()
    opts = jd.build_options(p)
    resp = {"ok": True, "choice": opts[0]["id"],
            "probabilities": {opts[0]["id"]: 0.9, "WAIT": 0.1},
            "confidence": 0.8, "model": "jev-test", "latency_ms": 800, "raw": {}}
    rec = jd.evaluate_response(resp, opts, p, now=1000, receipt_ttl_s=10)
    ok, reason = jd.consume_receipt(rec, "INT-1", now=1011)
    assert not ok and reason == "receipt_expired"


def load_tests(loader, tests, pattern):
    """Expose the original standalone assertions to the stdlib test runner."""
    import unittest
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            tests.addTest(unittest.FunctionTestCase(function))
    return tests
