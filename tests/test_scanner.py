# -*- coding: utf-8 -*-
"""離線單元測試：不連網、不需金鑰。執行：python -B -m unittest discover -s tests -v"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scanner.config import DEFAULTS  # noqa: E402
from scanner.jev_client import gate  # noqa: E402
from scanner.orderbook import orderbook_features  # noqa: E402
from scanner.kelly import compute_kelly, risk_snapshot  # noqa: E402
from scanner.paper_broker import PaperBroker  # noqa: E402
from scanner.price_action import instant_score, macd_features, price_action_features, rule_score  # noqa: E402


def _depth(bid_qty=3.0, ask_qty=1.0):
    return {
        "bids": [["100.0", str(bid_qty)], ["99.9", "2.0"], ["99.8", "2.0"]],
        "asks": [["100.1", str(ask_qty)], ["100.2", "2.0"], ["100.3", "2.0"]],
    }


def _bars(n=80, drift=0.15):
    bars, px = [], 100.0
    for i in range(n):
        px += drift
        bars.append({"t": i, "o": px - 0.2, "h": px + 0.4, "l": px - 0.4, "c": px, "v": 1000.0})
    return bars


def test_orderbook_bid_pressure():
    f = orderbook_features(_depth())
    assert f is not None and f["imbalance_5"] > 0 and f["spread_bps"] > 0


def test_price_action_uptrend_and_rule_long():
    pa = price_action_features(_bars())
    assert pa["trend"] == "up"
    r = rule_score(orderbook_features(_depth()), pa)
    assert r["rule_direction"] == "long" and r["rule_strength"] > 0


def test_jev_gate_blocks_low_quality():
    cfg = dict(DEFAULTS)
    bad = {"direction": "long", "entry_quality": 1.0, "valid_setup": 0.9, "trap_risk": 0.1, "confidence": 0.9, "risk_ok": 0.9}
    ok, reasons = gate(bad, cfg)
    assert not ok and "entry_quality_low" in reasons
    good = {"direction": "long", "entry_quality": 2.5, "valid_setup": 0.9, "trap_risk": 0.1, "confidence": 0.9, "risk_ok": 0.9}
    ok2, reasons2 = gate(good, cfg)
    assert ok2 and reasons2 == []
    assert gate(None, cfg)[0] is False


def test_instant_score_directions():
    s_long, d_long = instant_score(0.8, 0.45, 3.0, 0.9)
    s_short, d_short = instant_score(-0.8, -0.45, -3.0, 0.9)
    _, d_watch = instant_score(0.02, 0.01, 0.1, 0.5)
    assert d_long == "long" and s_long > 0.2
    assert d_short == "short" and s_short < -0.2
    assert d_watch == "watch"


def test_macd_cross_detection():
    # 先跌後強漲應出現金叉；先漲後強跌應出現死叉（合成序列，離線可重現）
    down_then_up = [100 - i * 0.5 for i in range(40)] + [80 + i * 1.2 for i in range(40)]
    up_then_down = [100 + i * 0.5 for i in range(40)] + [120 - i * 1.2 for i in range(40)]
    assert macd_features(down_then_up)["macd_cross"] in ("golden", "none")
    assert macd_features(down_then_up)["macd_hist"] > 0
    assert macd_features(up_then_down)["macd_hist"] < 0


def test_paper_simple_mode_open_and_signal_close():
    cfg = dict(DEFAULTS, use_risk_control=False)
    # v3 策略（MACD15m-TPSL）預設 15m；停損停利由引擎在成交後依 ATR 寫入持倉
    assert cfg["use_risk_control"] is False and cfg["timeframe"] == "15m"
    b = PaperBroker(cfg)
    pos, msg = b.open_position("ETHUSDT", "long", 100.0, None, 1.5, 2.5)
    assert pos is not None and pos["stop"] is None and pos["take_profit"] is None
    # 無停損停利時 mark_and_manage 不得自動平倉
    assert b.mark_and_manage({"ETHUSDT": 50.0}) == []
    rec = b.close_position("ETHUSDT", 110.0, "macd_reverse_cross")
    assert rec is not None and rec["pnl_net"] > 0 and b.positions == []


def test_kelly_positive_and_negative_edge():
    cfg = dict(DEFAULTS)
    good = [{"pnl_net": 120.0}] * 18 + [{"pnl_net": -60.0}] * 12
    k = compute_kelly(good, cfg, 100000.0)
    assert k["state"] == "positive_edge" and k["kelly_full"] > 0 and k["suggested_notional"] > 0
    bad = [{"pnl_net": 50.0}] * 10 + [{"pnl_net": -100.0}] * 20
    k2 = compute_kelly(bad, cfg, 100000.0)
    assert k2["state"] == "no_edge" and k2["suggested_notional"] < float(cfg["simple_position_usd"])
    few = compute_kelly([{"pnl_net": 10.0}] * 3, cfg, 100000.0)
    assert few["state"] == "insufficient_sample"


def test_risk_snapshot_brake():
    cfg = dict(DEFAULTS)
    b = PaperBroker(cfg)
    pos, _ = b.open_position("BTCUSDT", "long", 100.0, 1.0, 1.5, 2.5)
    assert pos is not None
    r = risk_snapshot(b, {"BTCUSDT": 100.0}, cfg, compute_kelly([], cfg, 100000.0))
    assert r["margin_pct"] > 0 and r["brake"] is False


def test_paper_broker_open_and_take_profit():
    b = PaperBroker(dict(DEFAULTS, use_risk_control=True))
    pos, msg = b.open_position("BTCUSDT", "long", 100.0, 1.0, 1.5, 2.5)
    assert pos is not None and msg == "opened"
    assert b.snapshot({"BTCUSDT": 100.0})["equity"] < 100000  # 已扣開倉費，合理
    closed = b.mark_and_manage({"BTCUSDT": 103.0}, {"BTCUSDT": 103.0}, {"BTCUSDT": 102.5})
    assert len(closed) == 1 and closed[0]["exit_reason"] == "take_profit" and closed[0]["pnl_net"] > 0


from scanner import multi_review  # noqa: E402


def test_review_parse_vote_extracts_json():
    v = multi_review._parse_vote('{"decision":"long","confidence":0.7,"reason":"ok"}')
    assert multi_review._parse_vote('noise {"decision":"long","confidence":0.7,"reason":"ok"} tail') is None
    assert v is not None and v["decision"] == "long" and abs(v["confidence"] - 0.7) < 1e-9
    assert multi_review._parse_vote("no json here") is None
    assert multi_review._parse_vote('{"decision":"maybe","confidence":1}') is None


def test_review_marks_unavailable_without_clis():
    orig = multi_review.shutil.which
    multi_review.shutil.which = lambda _x: None
    try:
        packet = multi_review.build_packet(
            "BTCUSDT",
            {"rule_direction": "long", "rule_score": 0.5, "price_action": {}, "orderbook": {}},
            {"direction": "long", "confidence": 0.9},
            {"state": "positive_edge"}, {"brake": False})
        out = multi_review.review(packet, ".")
    finally:
        multi_review.shutil.which = orig
    assert out["votes"]["gpt"]["decision"] == "unavailable"
    assert out["votes"]["claude"]["decision"] == "unavailable"
    assert out["votes"]["grok"]["decision"] == "unavailable"
    assert out["votes"]["synth"]["decision"] == "long"
    # 只有本機彙整一席有效，未達 2 席共識，不得冒充共識可執行
    assert out["consensus"] == "no_trade"


def test_external_support_counts_only_external_seats():
    from scanner import multi_review as mr
    votes = {"gpt": {"decision": "long"}, "claude": {"decision": "long"},
             "gemini": {"decision": "long"}, "grok": {"decision": "short"},
             "synth": {"decision": "short"}, "muse": {"decision": "long"}}
    d, n = mr.external_support(votes)
    assert d == "long" and n == 3  # synth/muse 不計入觸發門檻
    d, n = mr.external_support({"gpt": {"decision": "long"}, "claude": {"decision": "short"}})
    assert d == "no_trade"
    d, n = mr.external_support({})
    assert d == "no_trade" and n == 0


def test_review_includes_gemini_seat_key():
    orig = multi_review.shutil.which
    multi_review.shutil.which = lambda _x: None
    try:
        packet = multi_review.build_packet(
            "ETHUSDT",
            {"rule_direction": "short", "rule_score": -0.4, "price_action": {}, "orderbook": {}},
            {"direction": "short", "confidence": 0.8},
            {"state": "positive_edge"}, {"brake": False})
        out = multi_review.review(packet, ".")
    finally:
        multi_review.shutil.which = orig
    assert out["votes"]["gemini"]["decision"] == "unavailable"
    assert set(out["votes"]) == {"gpt", "claude", "gemini", "grok", "synth"}


from scanner.price_action import entry_filter_reasons  # noqa: E402


def test_entry_filters_block_and_pass():
    cfg = dict(DEFAULTS)
    pa_ok = {"trend": "up", "vol_ratio": 1.5, "rsi14": 55.0}
    ob_ok = {"spread_bps": 3.0, "imbalance_10": 0.2}
    assert entry_filter_reasons(pa_ok, ob_ok, "long", cfg) == []
    # 趨勢反向 + 量能不足 + 盤口反向 -> 至少擋下且含趨勢原因
    pa_bad = {"trend": "down", "vol_ratio": 0.3, "rsi14": 82.0}
    ob_bad = {"spread_bps": 30.0, "imbalance_10": -0.6}
    rs = entry_filter_reasons(pa_bad, ob_bad, "long", cfg)
    assert len(rs) >= 3 and any("趨勢" in r for r in rs)
    # 關閉濾網整組放行
    assert entry_filter_reasons(pa_bad, ob_bad, "long", dict(cfg, filter_enabled=False)) == []
    # 空單 RSI 過冷擋下
    rs2 = entry_filter_reasons({"trend": "down", "vol_ratio": 2.0, "rsi14": 12.0}, ob_ok, "short", cfg)
    assert any("RSI" in r for r in rs2)


from scanner.snapshot import build_snapshot, content_hash  # noqa: E402
from scanner.plans import make_plan, revalidate  # noqa: E402


def test_snapshot_hash_binds_content():
    cands = [{"symbol": "BTCUSDT", "price": 100.0, "rule_direction": "long", "rule_score": 0.3,
              "price_action": {"macd_cross": "golden", "trend": "up"}}]
    acc = {"equity": 100000.0, "open_positions": 0, "closed_trades": 0}
    s1 = build_snapshot(7, "2026-10-19 00:00:00 UTC", "1m", 1.0, cands, acc, "V3")
    s2 = build_snapshot(7, "2026-10-19 00:00:00 UTC", "1m", 1.0, cands, acc, "V3")
    assert s1["snapshot_id"] == "SNAP-000007" and s1["content_hash"] == s2["content_hash"]
    cands2 = [dict(cands[0], price=101.0)]
    s3 = build_snapshot(7, "2026-10-19 00:00:00 UTC", "1m", 1.0, cands2, acc, "V3")
    assert s3["content_hash"] != s1["content_hash"]  # 內容改了雜湊必須跟著變


def test_plan_revalidate_blocks_expired_and_risky():
    p = make_plan(3, "BTCUSDT", "long", {"type": "review_consensus", "consensus": "long"},
                  "SNAP-000007", 100.0, ["macd_reverse"], ttl_seconds=60, now_epoch=1000.0)
    ok, rs = revalidate(p, {"now_epoch": 1030.0, "price": 100.2, "spread_bps": 3.0})
    assert ok and rs == []
    ok2, rs2 = revalidate(p, {"now_epoch": 2000.0, "price": 100.2})
    assert not ok2 and any("過期" in r for r in rs2)
    ok3, rs3 = revalidate(p, {"now_epoch": 1030.0, "price": 103.0, "brake": True, "has_open_order": True})
    assert not ok3 and len(rs3) >= 2  # 價格偏離+剎車+未完委託不得送單
    # 改版（新雜湊）後舊複核綁定失效：雜湊不同視為不同計畫版本
    p2 = make_plan(4, "BTCUSDT", "long", {"type": "review_consensus", "consensus": "long"},
                   "SNAP-000008", 100.0, ["macd_reverse"], ttl_seconds=60, now_epoch=1000.0)
    assert p2["content_hash"] != p["content_hash"] and p2["plan_id"] != p["plan_id"]


def test_entry_filter_atr_gate():
    cfg = dict(DEFAULTS)
    pa = {"trend": "up", "vol_ratio": 1.5, "rsi14": 55.0, "atr_pct": 0.10}
    ob = {"spread_bps": 3.0, "imbalance_10": 0.1}
    rs = entry_filter_reasons(pa, ob, "long", cfg)
    assert any("波動不足" in r for r in rs)  # ATR 0.10% < 0.35% 預設門檻擋下
    pa2 = dict(pa, atr_pct=0.80)
    assert entry_filter_reasons(pa2, ob, "long", cfg) == []


def load_tests(loader, tests, pattern):
    """Expose the original standalone assertions to the stdlib test runner."""
    import unittest
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            tests.addTest(unittest.FunctionTestCase(function))
    return tests
