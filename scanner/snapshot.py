# -*- coding: utf-8 -*-
"""共同資料快照：每輪深層判斷建立不可修改的 snapshot，含內容雜湊。

同一輪所有模型/複核都引用同一 snapshot_id 與雜湊；任何一方私自更新行情
都不得算同一輪（工單 §7.3）。快照只摘要必要欄位，不複製整包 K 線。
"""
import hashlib
import json


def content_hash(payload):
    """對 canonical JSON 計算 sha256（鍵排序、無空白），供綁定版本核對。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_snapshot(seq, created_at, timeframe, feed_age_seconds, candidates, account,
                   strategy_version, feature_version="PA-OB-v1", model_route_version="review-v2"):
    """candidates: 已摘要的候選 list（不含 bars/depth）。回 snapshot dict（含雜湊）。"""
    body = {
        "schema_version": "snapshot/1",
        "seq": seq,
        "created_at": created_at,
        "market": "BINANCE_USDT_M_FUTURES",
        "mode": "PAPER",
        "timeframe": timeframe,
        "feed_age_seconds": feed_age_seconds,
        "candidates": [
            {
                "symbol": c.get("symbol"),
                "price": c.get("price"),
                "rule_direction": c.get("rule_direction"),
                "rule_score": c.get("rule_score"),
                "macd_cross": (c.get("price_action") or {}).get("macd_cross"),
                "trend": (c.get("price_action") or {}).get("trend"),
            }
            for c in candidates
        ],
        "account": {
            "equity": account.get("equity"),
            "open_positions": account.get("open_positions"),
            "closed_trades": account.get("closed_trades"),
        },
        "strategy_version": strategy_version,
        "feature_version": feature_version,
        "model_route_version": model_route_version,
    }
    snap = {"snapshot_id": f"SNAP-{seq:06d}", **body}
    snap["content_hash"] = content_hash(body)
    return snap
