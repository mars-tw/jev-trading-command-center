# -*- coding: utf-8 -*-
"""有期限的條件交易計畫 + 執行前重新驗證（工單 §10）。

計畫是資料（受限制的規則格式），不是程式碼：trigger 只存型態/參數，
由引擎以白名單邏輯比對，不執行模型回傳的任意程式/SQL/shell。
複核結果綁定 plan 的 content_hash；改版即新雜湊，舊複核不得沿用。
"""
import time

from .snapshot import content_hash

DEFAULT_TTL_SECONDS = 180


def make_plan(plan_seq, symbol, side, trigger, snapshot_id, reference_price,
              invalidation, ttl_seconds=DEFAULT_TTL_SECONDS, exit_policy="macd_reverse_or_tpsl",
              now_epoch=None):
    now_epoch = time.time() if now_epoch is None else now_epoch
    body = {
        "symbol": symbol,
        "side": side,
        "trigger": trigger,  # 例 {"type": "review_consensus", "consensus": "long"}
        "snapshot_id": snapshot_id,
        "reference_price": reference_price,
        "invalidation": list(invalidation or []),
        "exit_policy": exit_policy,
        "created_epoch": round(now_epoch, 3),
        "expires_epoch": round(now_epoch + float(ttl_seconds), 3),
        "order_ttl_seconds": 120,
        "version": 1,
    }
    return {"plan_id": f"PLAN-{plan_seq:04d}", **body, "content_hash": content_hash(body)}


def revalidate(plan, ctx):
    """執行前重驗（工單 §10.2）。ctx 為最新狀態摘要。回 (ok, reasons)。

    任一不過即不得送單，且不得臨場改價/改期限讓舊計畫看似成立。
    """
    reasons = []
    now = ctx.get("now_epoch", time.time())
    if ctx.get("plan_superseded"):
        reasons.append("計畫已被新版本取代")
    if now > float(plan.get("expires_epoch", 0)):
        reasons.append("計畫已過期")
    if ctx.get("paused_new"):
        reasons.append("已暫停新增交易")
    if ctx.get("brake"):
        reasons.append("風險剎車中")
    price = ctx.get("price")
    ref = plan.get("reference_price")
    max_dev = float(ctx.get("max_price_dev_pct", 0.5))
    if price is None or price <= 0:
        reasons.append("最新價格缺失")
    elif ref and abs(price / ref - 1.0) * 100.0 > max_dev:
        reasons.append(f"價格偏離計畫參考價 {abs(price / ref - 1.0) * 100.0:.2f}% > {max_dev}%")
    spread = ctx.get("spread_bps")
    if spread is not None and spread > float(ctx.get("max_spread_bps", 12.0)):
        reasons.append(f"價差過大（{spread:.1f}bps）")
    if ctx.get("has_position"):
        reasons.append("已有同商品持倉")
    if ctx.get("has_open_order"):
        reasons.append("已有同商品未完成委託（含結果未知）")
    if ctx.get("data_stale"):
        reasons.append("行情資料過期")
    return (not reasons), reasons
