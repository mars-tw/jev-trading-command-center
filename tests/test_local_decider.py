# -*- coding: utf-8 -*-
"""本機開源影子決策測試：嚴格解析、未知選項擋下、端點不通如實 UNAVAILABLE。"""
import os
import sys
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scanner import local_decider as ld  # noqa: E402

OPTS = [{"id": "EXEC_PLAN-0001", "criteria": "execute"}, {"id": "WAIT", "criteria": "wait"}]


def test_parse_ok():
    out = ld.parse_decision_text('noise {"choice": "WAIT", "confidence": 0.7, "reason": "weak"} tail', OPTS)
    assert out == {"choice": "WAIT", "confidence": 0.7, "reason": "weak"}


def test_parse_rejects_unknown_and_bad_confidence():
    assert ld.parse_decision_text('{"choice": "EXEC_X", "confidence": 0.5}', OPTS) is None
    assert ld.parse_decision_text('{"choice": "WAIT", "confidence": 1.5}', OPTS) is None
    assert ld.parse_decision_text('{"choice": "WAIT"}', OPTS) is None
    assert ld.parse_decision_text("no json at all", OPTS) is None
    assert ld.parse_decision_text("", OPTS) is None


def test_unreachable_endpoint_is_unavailable_not_fake():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline fixture")):
        out = ld.decide_local({"x": 1}, OPTS, "http://127.0.0.1:9/v1", timeout=3, strata_entry="")
    assert out["status"] == "UNAVAILABLE"
    assert out.get("status") != "OK" and "choice" not in out  # 不得假裝有選擇


def test_prompt_respects_strata_byte_cap():
    big = {"symbol": "BTCUSDT",
           "six_model_analysis_evidence_only": {
               "votes": {f"seat{i}": {"decision": "long", "confidence": 0.5,
                                      "reason": "x" * 900} for i in range(40)}},
           "market_evidence": {"orderbook": {"a": 1}, "price_action": {}}}
    text = ld.build_prompt(big, OPTS)
    assert len(text.encode("utf-8")) <= 17500
    assert "EXEC_PLAN-0001" in text and "WAIT" in text


def load_tests(loader, tests, pattern):
    """Expose the original standalone assertions to the stdlib test runner."""
    import unittest
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            tests.addTest(unittest.FunctionTestCase(function))
    return tests
