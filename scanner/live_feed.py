# -*- coding: utf-8 -*-
"""全市場即時饋送：每數秒輪詢全市場價格 + 最佳買賣盤，涵蓋全部 USDT 永續。

期貨 WebSocket 在本機網路連得上但收不到推送，故採 REST 快速輪詢：
- /fapi/v1/ticker/price     全部交易對最新價（輕量）
- /fapi/v1/ticker/bookTicker 全部交易對最佳買/賣價與量（輕量）
兩支合計約 170KB，每 3 秒一輪即可讓 700+ 交易對保持秒級新鮮。
"""
import threading
import time

import requests

BASE = "https://fapi.binance.com"


class LiveFeed:
    def __init__(self, interval=3):
        self.interval = interval
        self.prices = {}
        self.books = {}
        self.updated_at = 0.0
        self.poll_count = 0
        self.last_error = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "orderbook-scanner-live/2.0"})

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="live-feed")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _fetch(self, path):
        r = self._session.get(BASE + path, timeout=8)
        r.raise_for_status()
        return r.json()

    def _run(self):
        while not self._stop.is_set():
            try:
                prices, books = {}, {}
                for row in self._fetch("/fapi/v1/ticker/price"):
                    sym = row.get("symbol", "")
                    if sym.endswith("USDT"):
                        try:
                            prices[sym] = float(row["price"])
                        except (KeyError, TypeError, ValueError):
                            continue
                for row in self._fetch("/fapi/v1/ticker/bookTicker"):
                    sym = row.get("symbol", "")
                    if sym.endswith("USDT"):
                        try:
                            books[sym] = (
                                float(row["bidPrice"]),
                                float(row["bidQty"]),
                                float(row["askPrice"]),
                                float(row["askQty"]),
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
                with self._lock:
                    self.prices, self.books = prices, books
                    self.updated_at = time.time()
                    self.poll_count += 1
                    self.last_error = ""
            except Exception as e:  # 單輪失敗保留上一份資料，下一輪自動恢復
                with self._lock:
                    self.last_error = str(e)[:140]
            self._stop.wait(self.interval)

    def snapshot(self):
        with self._lock:
            return {
                "prices": dict(self.prices),
                "books": dict(self.books),
                "updated_at": self.updated_at,
                "poll_count": self.poll_count,
                "last_error": self.last_error,
                "symbols": len(self.prices),
                "age_seconds": round(time.time() - self.updated_at, 1) if self.updated_at else None,
            }
