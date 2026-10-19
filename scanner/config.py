# -*- coding: utf-8 -*-
"""設定載入：JSON 檔 + 安全預設值。金鑰永遠只走環境變數。"""
import json
import os

DEFAULTS = {
    "market": "binance_usdt_futures",
    "timeframe": "15m",
    "use_risk_control": True,
    "simple_position_usd": 5000.0,
    "kelly_fraction": 0.5,
    "kelly_cap_pct": 20.0,
    "kelly_min_samples": 10,
    "kelly_probe_factor": 0.5,
    "kelly_negative_factor": 0.25,
    "risk_max_margin_pct": 60.0,
    "risk_max_drawdown_pct": 5.0,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "filter_enabled": True,
    "filter_trend_align": True,
    "filter_min_vol_ratio": 0.8,
    "filter_max_spread_bps": 12.0,
    "filter_rsi_long_max": 75.0,
    "filter_rsi_short_min": 25.0,
    "filter_book_against": 0.35,
    "filter_min_atr_pct": 0.35,
    "max_hold_hours": 24,
    "scan_top_n": 30,
    "jev_top_k": 8,
    "scan_interval_seconds": 5,
    "fast_interval_seconds": 5,
    "feed_interval_seconds": 3,
    "deep_interval_seconds": 20,
    "deep_top_n": 24,
    "deep_min_quote_volume_usd": 3_000_000,
    "jev_per_cycle": 4,
    "jev_cooldown_seconds": 180,
    "jev_min_rule_strength": 0.22,
    "min_quote_volume_usd": 0,
    "max_spread_bps": 12.0,
    "initial_capital": 100_000.0,
    "risk_per_trade_pct": 0.5,
    "max_concurrent_positions": 50,
    "max_position_notional_pct": 20.0,
    "atr_stop_mult": 1.5,
    "atr_tp_mult": 3.0,
    "fee_taker_pct": 0.05,
    "slippage_pct": 0.02,
    "jev_model": "jev-latest",
    "plan_ttl_seconds": 900,
    "jev_receipt_ttl_seconds": 120,
    "jev_decision_timeout_seconds": 60,
    "external_analysis_enabled": False,
    "analysis_carousel": False,
    "carousel_symbol_cooldown_seconds": 600,
    "consensus_trigger_min_seats": 3,
    "decision_cooldown_seconds": 900,
    "fresh_analysis_max_age_seconds": 600,
    "local_decider_enabled": False,
    "local_decider_base_url": "",
    "local_decider_model": "",
    "local_decider_timeout_seconds": 240,
    "jev_min_entry_quality": 2,
    "jev_min_valid_setup": 0.60,
    "jev_max_trap": 0.40,
    "jev_min_confidence": 0.55,
}


def load_config(path=None):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if not k.startswith("_"):
                cfg[k] = v
    return cfg


def typesafe_api_key():
    """只從環境變數讀 Jev 金鑰；缺值回傳空字串（上層會走規則備援）。"""
    return (os.environ.get("TYPESAFE_API_KEY") or "").strip()
