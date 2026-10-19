# -*- coding: utf-8 -*-
"""幣安 USDT-M 永續公開行情（免金鑰，只讀）。"""
import time

import requests

BASE = "https://fapi.binance.com"


class BinancePublicClient:
    def __init__(self, timeout=10, retries=3):
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.trust_env = False  # Public endpoints never use ambient netrc/proxy credentials.
        self.session.headers.update({"User-Agent": "orderbook-scanner/1.0"})

    def _get(self, path, params=None):
        last_err = None
        for attempt in range(self.retries):
            try:
                r = self.session.get(BASE + path, params=params, timeout=self.timeout)
                r.raise_for_status()
                return r.json()
            except Exception as e:  # 網路抖動時退避重試
                last_err = e
                time.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"Binance GET {path} failed: {last_err}")

    def tickers_24h(self):
        return self._get("/fapi/v1/ticker/24hr")

    def depth(self, symbol, limit=50):
        return self._get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    def klines(self, symbol, interval="15m", limit=120):
        return self._get(
            "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )

    def universe_all(self):
        """全部 USDT 永續 24h 統計（不過濾、不截斷），依成交額排序。"""
        rows = []
        for t in self.tickers_24h():
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            try:
                qv = float(t.get("quoteVolume", 0))
                last = float(t.get("lastPrice", 0))
                chg = float(t.get("priceChangePercent", 0))
            except (TypeError, ValueError):
                continue
            if last <= 0:
                continue
            rows.append(
                {"symbol": sym, "last_price": last, "quote_volume": qv, "change_pct_24h": chg}
            )
        rows.sort(key=lambda x: x["quote_volume"], reverse=True)
        return rows

    def universe(self, min_quote_volume=20_000_000, top_n=30):
        """回傳成交額達標的 USDT 永續清單，依 24h 成交額排序。"""
        rows = []
        for t in self.tickers_24h():
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            try:
                qv = float(t.get("quoteVolume", 0))
                last = float(t.get("lastPrice", 0))
                chg = float(t.get("priceChangePercent", 0))
            except (TypeError, ValueError):
                continue
            if qv < min_quote_volume or last <= 0:
                continue
            rows.append(
                {"symbol": sym, "last_price": last, "quote_volume": qv, "change_pct_24h": chg}
            )
        rows.sort(key=lambda x: x["quote_volume"], reverse=True)
        return rows[:top_n]
