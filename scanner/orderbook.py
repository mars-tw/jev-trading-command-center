# -*- coding: utf-8 -*-
"""訂單簿特徵：不平衡、價差、microprice、掛單牆、近檔深度。"""


def _levels(raw):
    out = []
    for px, qty in raw or []:
        try:
            out.append((float(px), float(qty)))
        except (TypeError, ValueError):
            continue
    return out


def orderbook_features(depth, mid_levels=20):
    bids = _levels(depth.get("bids"))
    asks = _levels(depth.get("asks"))
    if not bids or not asks:
        return None
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return None
    spread_bps = (best_ask - best_bid) / mid * 10000.0

    def vol_usd(levels, n):
        return sum(px * q for px, q in levels[:n])

    def qty(levels, n):
        return sum(q for _, q in levels[:n])

    feats = {"best_bid": best_bid, "best_ask": best_ask, "mid": mid, "spread_bps": spread_bps}
    for n in (5, 10, 20):
        bq, aq = qty(bids, n), qty(asks, n)
        tot = bq + aq
        feats[f"imbalance_{n}"] = ((bq - aq) / tot) if tot > 0 else 0.0
        feats[f"bid_usd_{n}"] = vol_usd(bids, n)
        feats[f"ask_usd_{n}"] = vol_usd(asks, n)

    # microprice：以對手量加權，偏離 mid 代表短線買賣壓方向
    bq1, aq1 = bids[0][1], asks[0][1]
    denom = bq1 + aq1
    micro = (best_ask * bq1 + best_bid * aq1) / denom if denom > 0 else mid
    feats["microprice"] = micro
    feats["micro_dev_bps"] = (micro - mid) / mid * 10000.0

    # 掛單牆：單一最大檔佔前 20 檔總量比例
    for side, levels in (("bid", bids), ("ask", asks)):
        tot = qty(levels, mid_levels)
        biggest = max((q for _, q in levels[:mid_levels]), default=0.0)
        feats[f"wall_{side}"] = (biggest / tot) if tot > 0 else 0.0

    # mid 上下 1% 內的名目深度（流動性）
    lo, hi = mid * 0.99, mid * 1.01
    feats["depth_1pct_usd"] = sum(px * q for px, q in bids if px >= lo) + sum(
        px * q for px, q in asks if px <= hi
    )
    return feats
