# -*- coding: utf-8 -*-
"""Closed-candle, analysis-only multi-timeframe research utilities.

The module is deliberately independent of trading decisions and account state.
Importing it and constructing its service perform no I/O.
"""

from __future__ import annotations

import copy
import json
import math
import queue
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence

try:
    from .price_action import price_action_features
except ModuleNotFoundError as exc:  # Support the public helper's alternate module name.
    if not exc.name or not exc.name.endswith("price_action"):
        raise
    from .price_action_features import price_action_features


MAX_SAFE_INTEGER = 9_007_199_254_740_991
MIN_REQUIRED_BARS = 60
ANALYSIS_WINDOW = 120
MAX_SERVICE_SYMBOLS = 4
TTL_SECONDS = 60.0
FAILURE_COOLDOWN_SECONDS = 60.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

INTERVAL_MS = OrderedDict(
    (
        ("5m", 5 * 60_000),
        ("15m", 15 * 60_000),
        ("30m", 30 * 60_000),
        ("45m", 45 * 60_000),
        ("1h", 60 * 60_000),
        ("2h", 2 * 60 * 60_000),
        ("4h", 4 * 60 * 60_000),
        ("8h", 8 * 60 * 60_000),
        ("16h", 16 * 60 * 60_000),
        ("1d", 24 * 60 * 60_000),
    )
)
CANONICAL_INTERVALS = tuple(INTERVAL_MS)
NATIVE_INTERVALS = ("5m", "15m", "30m", "1h", "2h", "4h", "8h", "1d")
INTERVAL_ALIASES = {"60m": "1h", "24h": "1d"}
INTERVAL_LABELS = {
    "5m": "5分鐘",
    "15m": "15分鐘",
    "30m": "30分鐘",
    "45m": "45分鐘",
    "1h": "60分鐘／1小時",
    "2h": "2小時",
    "4h": "4小時",
    "8h": "8小時",
    "16h": "16小時",
    "1d": "24小時",
}
SOURCE_FOR_TARGET = {name: name for name in CANONICAL_INTERVALS}
SOURCE_FOR_TARGET.update({"45m": "15m", "16h": "8h"})
SOURCE_LIMITS = {name: 121 for name in NATIVE_INTERVALS}
SOURCE_LIMITS.update({"15m": 366, "8h": 244})

TIME_PATH = "/fapi/v1/time"
KLINES_PATH = "/fapi/v1/klines"
BINANCE_ORIGIN = "https://fapi.binance.com"
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,24}USDT$")
DEMO_SYMBOL_RE = re.compile(r"^DEMO(?:[1-9]|1[0-2])USDT$")
DECIMAL_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


class UnknownReason(str, Enum):
    BAD_SHAPE = "bad_shape"
    INVALID_TIMESTAMP = "invalid_timestamp"
    INVALID_NUMBER = "invalid_number"
    INVALID_PRICE = "invalid_price"
    MISALIGNED = "misaligned"
    BAD_CLOSE_TIME = "bad_close_time"
    DUPLICATE = "duplicate"
    REVERSED = "reversed"
    GAP = "gap"
    UNEXPECTED_OPEN = "unexpected_open"
    MISSING_SOURCE = "missing_source"
    UNSUPPORTED_INTERVAL = "unsupported_interval"
    INSUFFICIENT_DATA = "insufficient_data"
    MISSING_LATEST_CLOSED = "missing_latest_closed"
    FETCH_FAILED = "fetch_failed"
    INVALID_SYMBOL = "invalid_symbol"
    FORBIDDEN = "forbidden"
    DISABLED = "disabled"
    CAPACITY = "capacity"
    CLOSED = "closed"


class TimeframeDataError(ValueError):
    """A provider-neutral validation failure."""

    def __init__(self, reason: UnknownReason | str):
        self.reason = reason.value if isinstance(reason, UnknownReason) else str(reason)
        super().__init__(self.reason)


class TransportError(RuntimeError):
    """A provider-neutral public transport failure."""

    def __init__(self, reason: str = UnknownReason.FETCH_FAILED.value):
        self.reason = reason
        super().__init__(reason)


def normalize_interval(interval: str) -> str:
    if not isinstance(interval, str):
        raise TimeframeDataError(UnknownReason.UNSUPPORTED_INTERVAL)
    normalized = INTERVAL_ALIASES.get(interval, interval)
    if normalized not in INTERVAL_MS:
        raise TimeframeDataError(UnknownReason.UNSUPPORTED_INTERVAL)
    return normalized


def _strict_timestamp(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TimeframeDataError(UnknownReason.INVALID_TIMESTAMP)
    if value < 0 or value > MAX_SAFE_INTEGER:
        raise TimeframeDataError(UnknownReason.INVALID_TIMESTAMP)
    return value


def _strict_number(value: Any, *, positive: bool) -> float:
    if isinstance(value, bool) or value is None:
        raise TimeframeDataError(UnknownReason.INVALID_NUMBER)
    try:
        if isinstance(value, str):
            if not DECIMAL_RE.fullmatch(value):
                raise TimeframeDataError(UnknownReason.INVALID_NUMBER)
            parsed = Decimal(value)
            if not parsed.is_finite():
                raise TimeframeDataError(UnknownReason.INVALID_NUMBER)
            number = float(parsed)
        elif isinstance(value, (int, float, Decimal)):
            number = float(value)
        else:
            raise TimeframeDataError(UnknownReason.INVALID_NUMBER)
    except (InvalidOperation, OverflowError, ValueError):
        raise TimeframeDataError(UnknownReason.INVALID_NUMBER) from None
    if not math.isfinite(number):
        raise TimeframeDataError(UnknownReason.INVALID_NUMBER)
    if (positive and number <= 0.0) or (not positive and number < 0.0):
        raise TimeframeDataError(UnknownReason.INVALID_PRICE)
    return number


def _validated_bar(row: Sequence[Any], span: int) -> dict[str, Any]:
    if isinstance(row, (str, bytes, bytearray)) or not isinstance(row, (list, tuple)) or len(row) < 7:
        raise TimeframeDataError(UnknownReason.BAD_SHAPE)
    opened = _strict_timestamp(row[0])
    closed = _strict_timestamp(row[6])
    o = _strict_number(row[1], positive=True)
    h = _strict_number(row[2], positive=True)
    low = _strict_number(row[3], positive=True)
    c = _strict_number(row[4], positive=True)
    volume = _strict_number(row[5], positive=False)
    if opened % span:
        raise TimeframeDataError(UnknownReason.MISALIGNED)
    if closed != opened + span - 1:
        raise TimeframeDataError(UnknownReason.BAD_CLOSE_TIME)
    if low > h or h < max(o, c) or low > min(o, c):
        raise TimeframeDataError(UnknownReason.INVALID_PRICE)
    return {"t": opened, "o": o, "h": h, "l": low, "c": c, "v": volume, "close_time": closed}


def parse_closed_bars(raw: Sequence[Sequence[Any]], interval: str, now_ms: int) -> list[dict[str, Any]]:
    """Strictly validate Binance klines and return only closed, contiguous bars.

    Exactly one aligned current trailing bar may be open.  A candle is closed only
    when ``now_ms`` is strictly greater than its close time.
    """

    canonical = normalize_interval(interval)
    now = _strict_timestamp(now_ms)
    if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, (list, tuple)):
        raise TimeframeDataError(UnknownReason.BAD_SHAPE)
    span = INTERVAL_MS[canonical]
    bars = [_validated_bar(row, span) for row in raw]
    for previous, current in zip(bars, bars[1:]):
        delta = current["t"] - previous["t"]
        if delta == 0:
            raise TimeframeDataError(UnknownReason.DUPLICATE)
        if delta < 0:
            raise TimeframeDataError(UnknownReason.REVERSED)
        if delta != span:
            raise TimeframeDataError(UnknownReason.GAP)

    first_open = next((index for index, bar in enumerate(bars) if now <= bar["close_time"]), len(bars))
    if first_open < len(bars):
        trailing = bars[first_open:]
        expected_current = (now // span) * span
        if len(trailing) != 1 or first_open != len(bars) - 1 or trailing[0]["t"] != expected_current:
            raise TimeframeDataError(UnknownReason.UNEXPECTED_OPEN)
    return bars[:first_open]


def _validate_bar_mapping(bar: Mapping[str, Any], span: int) -> dict[str, Any]:
    if not isinstance(bar, Mapping):
        raise TimeframeDataError(UnknownReason.BAD_SHAPE)
    try:
        row = [bar["t"], bar["o"], bar["h"], bar["l"], bar["c"], bar["v"], bar["close_time"]]
    except KeyError:
        raise TimeframeDataError(UnknownReason.BAD_SHAPE) from None
    return _validated_bar(row, span)


def aggregate_bars(
    bars: Sequence[Mapping[str, Any]], source_interval: str, target_interval: str
) -> list[dict[str, Any]]:
    """Aggregate complete epoch-aligned groups; edge partial groups are dropped."""

    source = normalize_interval(source_interval)
    target = normalize_interval(target_interval)
    if source != target and SOURCE_FOR_TARGET.get(target) != source:
        raise TimeframeDataError(UnknownReason.UNSUPPORTED_INTERVAL)
    source_span, target_span = INTERVAL_MS[source], INTERVAL_MS[target]
    if target_span % source_span:
        raise TimeframeDataError(UnknownReason.UNSUPPORTED_INTERVAL)
    if isinstance(bars, (str, bytes, bytearray)) or not isinstance(bars, (list, tuple)):
        raise TimeframeDataError(UnknownReason.BAD_SHAPE)
    checked = [_validate_bar_mapping(bar, source_span) for bar in bars]
    for previous, current in zip(checked, checked[1:]):
        delta = current["t"] - previous["t"]
        if delta == 0:
            raise TimeframeDataError(UnknownReason.DUPLICATE)
        if delta < 0:
            raise TimeframeDataError(UnknownReason.REVERSED)
        if delta != source_span:
            raise TimeframeDataError(UnknownReason.GAP)
    if source == target:
        return checked
    if not checked:
        return []

    groups: OrderedDict[int, list[dict[str, Any]]] = OrderedDict()
    for bar in checked:
        bucket = (bar["t"] // target_span) * target_span
        groups.setdefault(bucket, []).append(bar)

    ratio = target_span // source_span
    aggregated: list[dict[str, Any]] = []
    items = list(groups.items())
    for index, (bucket, group) in enumerate(items):
        expected = [bucket + step * source_span for step in range(ratio)]
        actual = [bar["t"] for bar in group]
        if actual != expected:
            is_edge = index == 0 or index == len(items) - 1
            edge_shape = (
                (len(items) == 1 and all(value in expected for value in actual))
                or (index == 0 and actual == expected[-len(actual) :])
                or (index == len(items) - 1 and actual == expected[: len(actual)])
            )
            if is_edge and edge_shape:
                continue
            raise TimeframeDataError(UnknownReason.GAP)
        try:
            total_volume = math.fsum(bar["v"] for bar in group)
        except OverflowError:
            raise TimeframeDataError(UnknownReason.INVALID_NUMBER) from None
        if not math.isfinite(total_volume):
            raise TimeframeDataError(UnknownReason.INVALID_NUMBER)
        aggregated.append(
            {
                "t": bucket,
                "o": group[0]["o"],
                "h": max(bar["h"] for bar in group),
                "l": min(bar["l"] for bar in group),
                "c": group[-1]["c"],
                "v": total_volume,
                "close_time": bucket + target_span - 1,
            }
        )
    return aggregated


def _latest_closed_pattern(bars: Sequence[Mapping[str, Any]]) -> str:
    if len(bars) < 2:
        return "none"
    previous, current = bars[-2], bars[-1]
    body = current["c"] - current["o"]
    candle_range = max(current["h"] - current["l"], 1e-12)
    upper = current["h"] - max(current["o"], current["c"])
    lower = min(current["o"], current["c"]) - current["l"]
    if (
        previous["c"] < previous["o"]
        and current["c"] > current["o"]
        and current["c"] >= previous["o"]
        and current["o"] <= previous["c"]
    ):
        return "bullish_engulfing"
    if (
        previous["c"] > previous["o"]
        and current["c"] < current["o"]
        and current["c"] <= previous["o"]
        and current["o"] >= previous["c"]
    ):
        return "bearish_engulfing"
    if lower > 2.0 * abs(body) and upper < 0.3 * candle_range:
        return "hammer"
    if upper > 2.0 * abs(body) and lower < 0.3 * candle_range:
        return "shooting_star"
    return "none"


def _finite_json(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value if -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER else None
    if isinstance(value, (float, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, Mapping):
        return {str(key): _finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(item) for item in value]
    return None


def _input_count(raw: Any) -> int:
    return len(raw) if isinstance(raw, (list, tuple)) else 0


def _metadata(
    source: str,
    input_count: int,
    source_closed_count: int,
    target_bars: Sequence[Mapping[str, Any]],
    reason: Optional[str],
    expected_asof: Optional[int],
) -> dict[str, Any]:
    selected = list(target_bars[-ANALYSIS_WINDOW:])
    first = selected[0]["t"] if selected else None
    last = selected[-1]["close_time"] if selected else None
    actual_asof = target_bars[-1]["close_time"] if target_bars else None
    return {
        "source_interval": source,
        "input_count": input_count,
        "source_closed_count": source_closed_count,
        "closed_count": len(target_bars),
        "coverage": {
            "first_open_at": first,
            "last_closed_at": last,
            "selected_bars": len(selected),
            "contiguous": bool(selected),
        },
        "last_closed_at": actual_asof,
        "actual_asof": actual_asof,
        "expected_asof": expected_asof,
        "stale": expected_asof is None or actual_asof != expected_asof,
        "unknown_reason": reason,
    }


def _expected_closed_at(now_ms: int, interval: str) -> Optional[int]:
    expected = (now_ms // INTERVAL_MS[interval]) * INTERVAL_MS[interval] - 1
    return expected if 0 <= expected <= MAX_SAFE_INTEGER else None


def _unknown_frame(interval: str, source: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "interval": interval,
        "label": INTERVAL_LABELS[interval],
        "status": "unknown",
        "metadata": _finite_json(metadata),
        "features": None,
        "analysis_only": True,
        "execution_authority": False,
    }


def analyze_frames(
    sources: Mapping[str, Sequence[Sequence[Any]]],
    now_ms: int,
    symbol: str = "DEMO1USDT",
    intervals: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Analyze canonical frames from strict native raw-kline sources."""

    now = _strict_timestamp(now_ms)
    if not isinstance(sources, Mapping):
        raise TimeframeDataError(UnknownReason.BAD_SHAPE)
    requested: list[str] = []
    for item in intervals or CANONICAL_INTERVALS:
        canonical = normalize_interval(item)
        if canonical not in requested:
            requested.append(canonical)

    normalized_sources: dict[str, Any] = {}
    for key, value in sources.items():
        try:
            canonical = normalize_interval(key)
        except TimeframeDataError:
            continue
        if canonical not in NATIVE_INTERVALS:
            continue
        if canonical not in normalized_sources or key == canonical:
            normalized_sources[canonical] = value

    parsed: dict[str, list[dict[str, Any]]] = {}
    parse_failures: dict[str, str] = {}
    for source in dict.fromkeys(SOURCE_FOR_TARGET[target] for target in requested):
        raw = normalized_sources.get(source)
        if raw is None:
            parse_failures[source] = UnknownReason.MISSING_SOURCE.value
            continue
        try:
            parsed[source] = parse_closed_bars(raw, source, now)
        except TimeframeDataError as exc:
            parse_failures[source] = exc.reason

    frames: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for target in requested:
        source = SOURCE_FOR_TARGET[target]
        expected_asof = _expected_closed_at(now, target)
        raw = normalized_sources.get(source)
        source_bars = parsed.get(source, [])
        if source in parse_failures:
            metadata = _metadata(source, _input_count(raw), 0, [], parse_failures[source], expected_asof)
            frames[target] = _unknown_frame(target, source, metadata)
            continue
        try:
            target_bars = aggregate_bars(source_bars, source, target)
        except TimeframeDataError as exc:
            metadata = _metadata(source, _input_count(raw), len(source_bars), [], exc.reason, expected_asof)
            frames[target] = _unknown_frame(target, source, metadata)
            continue
        metadata = _metadata(source, _input_count(raw), len(source_bars), target_bars, None, expected_asof)
        if len(target_bars) < MIN_REQUIRED_BARS:
            metadata["unknown_reason"] = UnknownReason.INSUFFICIENT_DATA.value
            frames[target] = _unknown_frame(target, source, metadata)
            continue
        if expected_asof is None or metadata["actual_asof"] != expected_asof:
            metadata["unknown_reason"] = UnknownReason.MISSING_LATEST_CLOSED.value
            frames[target] = _unknown_frame(target, source, metadata)
            continue
        selected = target_bars[-ANALYSIS_WINDOW:]
        features = price_action_features(selected)
        if not isinstance(features, Mapping):
            metadata["unknown_reason"] = UnknownReason.INSUFFICIENT_DATA.value
            frames[target] = _unknown_frame(target, source, metadata)
            continue
        normalized_features = _finite_json(dict(features))
        normalized_features["pattern"] = _latest_closed_pattern(selected)
        normalized_features["pattern_as_of"] = selected[-1]["close_time"]
        frames[target] = {
            "interval": target,
            "label": INTERVAL_LABELS[target],
            "status": "ready",
            "metadata": _finite_json(metadata),
            "features": normalized_features,
            "analysis_only": True,
            "execution_authority": False,
        }

    overall = "ready" if frames and all(frame["status"] == "ready" for frame in frames.values()) else "unknown"
    data_stale = any(frame["metadata"].get("stale") is True for frame in frames.values())
    return {
        "symbol": symbol,
        "status": overall,
        "server_time": now,
        "frames": frames,
        "data_stale": data_stale,
        "stale": data_stale,
        "analysis_only": True,
        "execution_authority": False,
    }


def _synthetic_row(opened: int, span: int, index: int, seed: int) -> list[Any]:
    base = 80.0 + seed / 100.0 + index * 0.07
    direction = 0.16 if (index + seed) % 5 else -0.11
    opened_price = base
    closed_price = base + direction
    high = max(opened_price, closed_price) + 0.09
    low = min(opened_price, closed_price) - 0.08
    return [opened, f"{opened_price:.8f}", f"{high:.8f}", f"{low:.8f}", f"{closed_price:.8f}", f"{10 + index % 17:.8f}", opened + span - 1]


def synthetic_sources(now_ms: int, symbol: str = "DEMO1USDT") -> dict[str, list[list[Any]]]:
    """Return deterministic, closed-only native sources without external I/O."""

    now = _strict_timestamp(now_ms)
    if not DEMO_SYMBOL_RE.fullmatch(symbol):
        raise TimeframeDataError(UnknownReason.INVALID_SYMBOL)
    seed = sum((index + 1) * ord(char) for index, char in enumerate(symbol)) % 997
    result: dict[str, list[list[Any]]] = {}
    for interval in NATIVE_INTERVALS:
        span = INTERVAL_MS[interval]
        count = SOURCE_LIMITS[interval]
        last_open = (now // span) * span - span
        first_open = last_open - (count - 1) * span
        result[interval] = [
            _synthetic_row(first_open + index * span, span, index, seed) for index in range(count)
        ]
    return result


def _reject_constant(_: str) -> None:
    raise TransportError("invalid_json")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TransportError("invalid_json")
        result[key] = value
    return result


def _strict_json_loads(payload: bytes) -> Any:
    try:
        value = json.loads(
            payload.decode("utf-8", "strict"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except TransportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise TransportError("invalid_json") from None

    def verify(item: Any) -> None:
        if isinstance(item, bool) or item is None or isinstance(item, str):
            return
        if isinstance(item, int):
            if abs(item) > MAX_SAFE_INTEGER:
                raise TransportError("invalid_json")
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise TransportError("invalid_json")
            return
        if isinstance(item, list):
            for child in item:
                verify(child)
            return
        if isinstance(item, dict):
            for child in item.values():
                verify(child)
            return
        raise TransportError("invalid_json")

    verify(value)
    return value


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise TransportError("redirect_rejected")


class SafeBinanceFetcher:
    """Strict public-only transport for two fixed Binance Futures endpoints."""

    def __init__(self, timeout: float = 3.0, opener: Any = None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)):
            raise ValueError("invalid timeout")
        self.timeout = min(3.0, max(0.1, float(timeout)))
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirectHandler(), urllib.request.HTTPSHandler()
        )

    def __call__(self, path: str, params: Mapping[str, Any]) -> Any:
        if path == TIME_PATH:
            if params:
                raise TransportError("invalid_request")
            url = BINANCE_ORIGIN + TIME_PATH
        elif path == KLINES_PATH:
            if not isinstance(params, Mapping) or set(params) != {"symbol", "interval", "limit"}:
                raise TransportError("invalid_request")
            symbol, interval, limit = params["symbol"], params["interval"], params["limit"]
            if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
                raise TransportError("invalid_request")
            if interval not in NATIVE_INTERVALS:
                raise TransportError("invalid_request")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
                raise TransportError("invalid_request")
            query = urllib.parse.urlencode((('symbol', symbol), ('interval', interval), ('limit', str(limit))))
            url = BINANCE_ORIGIN + KLINES_PATH + "?" + query
        else:
            raise TransportError("invalid_request")

        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.getcode()
                content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                content_length = response.headers.get("Content-Length")
                if status != 200 or content_type != "application/json":
                    raise TransportError()
                if content_length is not None:
                    try:
                        if int(content_length) > MAX_RESPONSE_BYTES:
                            raise TransportError("response_too_large")
                    except ValueError:
                        raise TransportError("invalid_response") from None
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except TransportError:
            raise
        except Exception:
            raise TransportError() from None
        if len(payload) > MAX_RESPONSE_BYTES:
            raise TransportError("response_too_large")
        value = _strict_json_loads(payload)
        if path == TIME_PATH and not isinstance(value, dict):
            raise TransportError("invalid_response")
        if path == KLINES_PATH and not isinstance(value, list):
            raise TransportError("invalid_response")
        return value

    def fetch_server_time(self) -> int:
        return _extract_server_time(self(TIME_PATH, {}))

    def fetch_klines(self, symbol: str, interval: str, limit: int) -> Any:
        return self(KLINES_PATH, {"symbol": symbol, "interval": interval, "limit": limit})


def _extract_server_time(payload: Any) -> int:
    if not isinstance(payload, Mapping) or "serverTime" not in payload:
        raise TransportError("invalid_response")
    try:
        return _strict_timestamp(payload["serverTime"])
    except TimeframeDataError:
        raise TransportError("invalid_response") from None


def _call_fetcher(fetcher: Any, path: str, params: Mapping[str, Any]) -> Any:
    if callable(fetcher):
        return fetcher(path, params)
    if path == TIME_PATH and hasattr(fetcher, "fetch_server_time"):
        return {"serverTime": fetcher.fetch_server_time()}
    if path == KLINES_PATH and hasattr(fetcher, "fetch_klines"):
        return fetcher.fetch_klines(params["symbol"], params["interval"], params["limit"])
    raise TransportError()


def _safe_copy(value: Any) -> Any:
    return copy.deepcopy(_finite_json(value))


class TimeframeService:
    """Bounded selection/cache service; reads never start work."""

    def __init__(
        self,
        demo: bool = True,
        enabled: bool = False,
        fetcher: Any = None,
        clock: Optional[Callable[[], float]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        workers: int = 2,
        worker: Any = None,
    ):
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 2:
            raise ValueError("workers must be 1 or 2")
        self.demo = bool(demo)
        self.enabled = bool(enabled)
        self._fetcher = fetcher
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._worker_count = workers
        self._external_worker = worker
        self._owned_workers: list[threading.Thread] = []
        self._jobs: queue.Queue[Optional[str]] = queue.Queue(maxsize=MAX_SERVICE_SYMBOLS)
        self._run_slots = threading.BoundedSemaphore(workers)
        self._lock = threading.RLock()
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._scheduled: set[str] = set()
        self._closed = False

    def _wall_ms(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise RuntimeError("invalid clock")
        return max(0, min(MAX_SAFE_INTEGER, int(float(value) * 1000.0)))

    def _base_status(
        self,
        symbol: Optional[str],
        status: str,
        reason: Optional[str] = None,
        *,
        stale: bool = False,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "status": status,
            "reason": reason,
            "fetched_at": None,
            "server_time": None,
            "stale": stale,
            "analysis_only": True,
            "execution_authority": False,
        }

    def _validate_symbol(self, symbol: Any) -> bool:
        if not isinstance(symbol, str):
            return False
        return bool((DEMO_SYMBOL_RE if self.demo else SYMBOL_RE).fullmatch(symbol))

    def _entry_fresh(self, entry: Mapping[str, Any], now: float) -> bool:
        result = entry["result"]
        lifetime = FAILURE_COOLDOWN_SECONDS if result.get("status") == "failure" else TTL_SECONDS
        return now - entry["stored_mono"] < lifetime

    def _store(self, symbol: str, result: Mapping[str, Any]) -> None:
        with self._lock:
            self._scheduled.discard(symbol)
            self._cache.pop(symbol, None)
            self._cache[symbol] = {"stored_mono": self._monotonic(), "result": _safe_copy(result)}
            while len(self._cache) > MAX_SERVICE_SYMBOLS:
                self._cache.popitem(last=False)

    def _failure(self, symbol: str, reason: str = UnknownReason.FETCH_FAILED.value) -> dict[str, Any]:
        result = self._base_status(symbol, "failure", reason)
        try:
            result["fetched_at"] = self._wall_ms()
        except RuntimeError:
            result["fetched_at"] = None
        return result

    def _run_job(self, symbol: str) -> dict[str, Any]:
        fetched_at: Optional[int] = None
        server_time: Optional[int] = None
        try:
            if self.demo:
                server_time = self._wall_ms()
                analysis = analyze_frames(synthetic_sources(server_time, symbol), server_time, symbol)
            else:
                fetcher = self._fetcher or SafeBinanceFetcher()
                server_time = _extract_server_time(_call_fetcher(fetcher, TIME_PATH, {}))
                sources: dict[str, Any] = {}
                for interval in NATIVE_INTERVALS:
                    sources[interval] = _call_fetcher(
                        fetcher,
                        KLINES_PATH,
                        {"symbol": symbol, "interval": interval, "limit": SOURCE_LIMITS[interval]},
                    )
                analysis = analyze_frames(sources, server_time, symbol)
            fetched_at = self._wall_ms()
            analysis["fetched_at"] = fetched_at
            analysis["server_time"] = server_time
            data_stale = bool(analysis.get("data_stale", analysis.get("stale", False)))
            analysis["data_stale"] = data_stale
            analysis["cache_stale"] = False
            analysis["stale"] = data_stale
            analysis["reason"] = None if analysis["status"] == "ready" else UnknownReason.INSUFFICIENT_DATA.value
            return _safe_copy(analysis)
        except Exception:
            result = self._base_status(symbol, "failure", UnknownReason.FETCH_FAILED.value)
            result["fetched_at"] = fetched_at
            result["server_time"] = server_time
            return result

    def _complete_job(self, symbol: str) -> None:
        with self._run_slots:
            with self._lock:
                if self._closed:
                    self._scheduled.discard(symbol)
                    return
            self._store(symbol, self._run_job(symbol))

    def _worker_loop(self) -> None:
        while True:
            symbol = self._jobs.get()
            if symbol is None:
                return
            self._complete_job(symbol)

    def _ensure_workers(self) -> None:
        if self._external_worker is not None or self._owned_workers:
            return
        for index in range(self._worker_count):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"timeframe-analysis-{index + 1}",
                daemon=True,
            )
            self._owned_workers.append(thread)
            thread.start()

    def _submit(self, symbol: str) -> None:
        try:
            if self._external_worker is not None:
                self._external_worker.submit(self._complete_job, symbol)
            else:
                with self._lock:
                    self._ensure_workers()
                self._jobs.put_nowait(symbol)
        except Exception:
            self._store(symbol, self._failure(symbol))

    def select(self, symbol: str, *, owner: bool = False, csrf_valid: bool = False) -> dict[str, Any]:
        """Select a symbol without waiting for real network work."""

        if owner is not True or csrf_valid is not True:
            return self._base_status(symbol if isinstance(symbol, str) else None, "failure", UnknownReason.FORBIDDEN.value)
        if not self._validate_symbol(symbol):
            return self._base_status(symbol if isinstance(symbol, str) else None, "failure", UnknownReason.INVALID_SYMBOL.value)
        if not self.demo and not self.enabled:
            return self._base_status(symbol, "disabled", UnknownReason.DISABLED.value)

        now = self._monotonic()
        with self._lock:
            if self._closed:
                return self._base_status(symbol, "failure", UnknownReason.CLOSED.value)
            entry = self._cache.get(symbol)
            if entry and self._entry_fresh(entry, now):
                self._cache.move_to_end(symbol)
                return _safe_copy(entry["result"])
            if symbol in self._scheduled:
                return self._base_status(symbol, "pending", stale=entry is not None)

            occupied = set(self._cache) | self._scheduled
            if symbol not in occupied:
                while len(occupied) >= MAX_SERVICE_SYMBOLS:
                    evictable = next((key for key in self._cache if key not in self._scheduled), None)
                    if evictable is None:
                        return self._base_status(symbol, "failure", UnknownReason.CAPACITY.value)
                    self._cache.pop(evictable, None)
                    occupied.discard(evictable)
            self._scheduled.add(symbol)

        if self.demo:
            self._store(symbol, self._run_job(symbol))
        else:
            self._submit(symbol)
        with self._lock:
            completed = self._cache.get(symbol)
            if completed and symbol not in self._scheduled:
                return _safe_copy(completed["result"])
        return self._base_status(symbol, "pending", stale=entry is not None)

    def snapshot(self, symbol: Optional[str] = None) -> dict[str, Any]:
        """Return a JSON-safe copy.  This method never schedules work."""

        with self._lock:
            now = self._monotonic()
            if symbol is not None:
                if symbol in self._scheduled:
                    return self._base_status(symbol, "pending", stale=symbol in self._cache)
                entry = self._cache.get(symbol)
                if not entry:
                    return self._base_status(symbol, "unknown", UnknownReason.MISSING_SOURCE.value)
                result = _safe_copy(entry["result"])
                cache_stale = not self._entry_fresh(entry, now)
                data_stale = bool(result.get("data_stale", result.get("stale", False)))
                result["data_stale"] = data_stale
                result["cache_stale"] = cache_stale
                result["stale"] = data_stale or cache_stale
                return result
            symbols = {}
            for key, entry in self._cache.items():
                result = _safe_copy(entry["result"])
                cache_stale = not self._entry_fresh(entry, now)
                data_stale = bool(result.get("data_stale", result.get("stale", False)))
                result["data_stale"] = data_stale
                result["cache_stale"] = cache_stale
                result["stale"] = data_stale or cache_stale
                symbols[key] = result
            return {
                "symbols": symbols,
                "pending": sorted(self._scheduled),
                "analysis_only": True,
                "execution_authority": False,
            }

    def close(self) -> None:
        """Stop owned daemon workers without waiting on a blocking injected fetcher."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            owned_count = len(self._owned_workers)
        if self._external_worker is None:
            for _ in range(owned_count):
                try:
                    self._jobs.put_nowait(None)
                except queue.Full:
                    break


FEATURE_KEYS = (
    "price",
    "ema20",
    "ema50",
    "macd",
    "macd_signal",
    "macd_hist",
    "macd_cross",
    "trend",
    "rsi14",
    "atr",
    "atr_pct",
    "ret_4bars_pct",
    "ret_16bars_pct",
    "breakout",
    "range_high_20",
    "range_low_20",
    "vol_ratio",
    "vwap",
    "vwap_dev_pct",
    "pattern",
    "pattern_as_of",
)
METADATA_KEYS = (
    "source_interval",
    "input_count",
    "source_closed_count",
    "closed_count",
    "coverage",
    "last_closed_at",
    "actual_asof",
    "expected_asof",
    "stale",
    "unknown_reason",
)
CAPABILITY_METADATA = {
    "connector_enabled": False,
    "capability_verified": False,
    "deny_all_verified": False,
    "spawn_allowed": False,
    "tools_allowed": False,
    "tools_isolated": False,
    "mcp_allowed": False,
    "mcp_isolated": False,
    "plugins_allowed": False,
    "plugins_isolated": False,
    "model": "UNVERIFIED",
    "provider_status": None,
}

_CATEGORICAL_FEATURES = {
    "macd_cross": {"none", "golden", "death"},
    "trend": {"range", "up", "down"},
    "breakout": {"none", "up", "down"},
    "pattern": {"none", "bullish_engulfing", "bearish_engulfing", "hammer", "shooting_star"},
}
_INTEGER_FEATURES = {"pattern_as_of"}
_KNOWN_REASONS = {reason.value for reason in UnknownReason}


def _public_scalar(value: Any, *, integer: bool = False) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if integer:
        return value if isinstance(value, int) and 0 <= value <= MAX_SAFE_INTEGER else None
    return number


def _public_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    coverage = metadata.get("coverage") if isinstance(metadata.get("coverage"), Mapping) else {}
    source = metadata.get("source_interval")
    reason = metadata.get("unknown_reason")
    return {
        "source_interval": source if source in NATIVE_INTERVALS else None,
        "input_count": _public_scalar(metadata.get("input_count"), integer=True),
        "source_closed_count": _public_scalar(metadata.get("source_closed_count"), integer=True),
        "closed_count": _public_scalar(metadata.get("closed_count"), integer=True),
        "coverage": {
            "first_open_at": _public_scalar(coverage.get("first_open_at"), integer=True),
            "last_closed_at": _public_scalar(coverage.get("last_closed_at"), integer=True),
            "selected_bars": _public_scalar(coverage.get("selected_bars"), integer=True),
            "contiguous": coverage.get("contiguous") if isinstance(coverage.get("contiguous"), bool) else False,
        },
        "last_closed_at": _public_scalar(metadata.get("last_closed_at"), integer=True),
        "unknown_reason": reason if isinstance(reason, str) and reason in _KNOWN_REASONS else None,
    }


def _public_features(features: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key in FEATURE_KEYS:
        value = features.get(key)
        if key in _CATEGORICAL_FEATURES:
            output[key] = value if isinstance(value, str) and value in _CATEGORICAL_FEATURES[key] else None
        elif key in _INTEGER_FEATURES:
            output[key] = _public_scalar(value, integer=True)
        else:
            output[key] = _public_scalar(value)
    return output


def public_research_context(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Build the fixed research-only whitelist; raw sources and private state cannot pass."""

    if not isinstance(analysis, Mapping):
        raise TimeframeDataError(UnknownReason.BAD_SHAPE)
    symbol = analysis.get("symbol")
    if not isinstance(symbol, str) or not (SYMBOL_RE.fullmatch(symbol) or DEMO_SYMBOL_RE.fullmatch(symbol)):
        raise TimeframeDataError(UnknownReason.INVALID_SYMBOL)
    supplied_frames = analysis.get("frames") if isinstance(analysis.get("frames"), Mapping) else {}
    frames: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for interval in CANONICAL_INTERVALS:
        supplied = supplied_frames.get(interval) if isinstance(supplied_frames, Mapping) else None
        supplied = supplied if isinstance(supplied, Mapping) else {}
        metadata = supplied.get("metadata") if isinstance(supplied.get("metadata"), Mapping) else {}
        features = supplied.get("features") if isinstance(supplied.get("features"), Mapping) else None
        frames[interval] = {
            "interval": interval,
            "label": INTERVAL_LABELS[interval],
            "status": supplied.get("status") if supplied.get("status") in {"ready", "unknown"} else "unknown",
            "metadata": _public_metadata(metadata),
            "research": _public_features(features) if features is not None else None,
            "analysis_only": True,
            "execution_authority": False,
        }
    return {
        "symbol": symbol,
        "frames": frames,
        "analysis_only": True,
        "execution_authority": False,
        "capability": dict(CAPABILITY_METADATA),
    }


def antigravity_route(_: Mapping[str, Any]) -> dict[str, Any]:
    """Reject before process creation until deny-all capability isolation is proven."""

    return {
        "status": "capability_unverified",
        "spawned": False,
        "analysis_only": True,
        "execution_authority": False,
        "capability": dict(CAPABILITY_METADATA),
    }


__all__ = [
    "CANONICAL_INTERVALS",
    "NATIVE_INTERVALS",
    "INTERVAL_MS",
    "INTERVAL_LABELS",
    "INTERVAL_ALIASES",
    "SOURCE_LIMITS",
    "UnknownReason",
    "TimeframeDataError",
    "TransportError",
    "SafeBinanceFetcher",
    "TimeframeService",
    "normalize_interval",
    "parse_closed_bars",
    "aggregate_bars",
    "analyze_frames",
    "synthetic_sources",
    "public_research_context",
    "antigravity_route",
    "CAPABILITY_METADATA",
]
