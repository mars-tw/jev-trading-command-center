# -*- coding: utf-8 -*-
import json
import math
import threading
import time
import unittest
from unittest import mock

from scanner import timeframe_analysis as ta


BASE = 1_791_936_000_000


def fixed_raw(span, count):
    return [
        [
            BASE + index * span,
            str(100 + index),
            str(104 + index),
            str(98 + index),
            str(102 + index),
            str(index + 1),
            BASE + (index + 1) * span - 1,
        ]
        for index in range(count)
    ]


def raw_row(opened, interval="5m", *, o=100.0, h=101.0, low=99.0, c=100.5, volume=10.0):
    span = ta.INTERVAL_MS[ta.normalize_interval(interval)]
    return [opened, str(o), str(h), str(low), str(c), str(volume), opened + span - 1]


def raw_series(interval, count, start=0):
    span = ta.INTERVAL_MS[ta.normalize_interval(interval)]
    rows = []
    for index in range(count):
        base = 100.0 + index * 0.05
        rows.append(
            raw_row(
                start + index * span,
                interval,
                o=base,
                h=base + 0.8,
                low=base - 0.7,
                c=base + 0.2,
                volume=10 + index % 7,
            )
        )
    return rows


class MutableClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class InlineWorker:
    def submit(self, function, *args):
        function(*args)


class ManualWorker:
    def __init__(self):
        self.tasks = []
        self.shutdown_called = False

    def submit(self, function, *args):
        self.tasks.append((function, args))

    def shutdown(self):
        self.shutdown_called = True


class FakePublicFetcher:
    def __init__(self, now_ms):
        self.now_ms = now_ms
        self.calls = []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if path == ta.TIME_PATH:
            self.assert_time_params(params)
            return {"serverTime": self.now_ms}
        if path != ta.KLINES_PATH:
            raise AssertionError("unexpected path")
        symbol = params["symbol"]
        interval = params["interval"]
        limit = params["limit"]
        if not ta.SYMBOL_RE.fullmatch(symbol):
            raise AssertionError("unvalidated symbol")
        if interval not in ta.NATIVE_INTERVALS or limit != ta.SOURCE_LIMITS[interval]:
            raise AssertionError("unexpected interval or limit")
        span = ta.INTERVAL_MS[interval]
        current = (self.now_ms // span) * span
        return raw_series(interval, limit, current - (limit - 1) * span)

    @staticmethod
    def assert_time_params(params):
        if params:
            raise AssertionError("time request must not have parameters")


class TimeframeParsingTests(unittest.TestCase):
    def test_canonical_aliases_labels_and_alias_dedup(self):
        self.assertEqual("1h", ta.normalize_interval("60m"))
        self.assertEqual("1d", ta.normalize_interval("24h"))
        self.assertEqual("60分鐘／1小時", ta.INTERVAL_LABELS["1h"])
        self.assertEqual("24小時", ta.INTERVAL_LABELS["1d"])
        span = ta.INTERVAL_MS["1h"]
        result = ta.analyze_frames(
            {"60m": raw_series("1h", 60)},
            60 * span + 1,
            intervals=["60m", "1h"],
        )
        self.assertEqual(["1h"], list(result["frames"]))
        self.assertEqual("ready", result["frames"]["1h"]["status"])

    def test_now_boundary_is_strict_and_open_tail_is_excluded(self):
        close_time = ta.INTERVAL_MS["5m"] - 1
        row = raw_row(0)
        self.assertEqual([], ta.parse_closed_bars([row], "5m", close_time))
        self.assertEqual(1, len(ta.parse_closed_bars([row], "5m", close_time + 1)))

        span = ta.INTERVAL_MS["5m"]
        two = [raw_row(0), raw_row(span)]
        parsed = ta.parse_closed_bars(two, "5m", span + 50)
        self.assertEqual([0], [bar["t"] for bar in parsed])

    def test_epoch_45m_and_16h_cross_day_buckets(self):
        minute = 60_000
        source_45 = ta.parse_closed_bars(raw_series("15m", 12), "15m", 12 * 15 * minute)
        bars_45 = ta.aggregate_bars(source_45, "15m", "45m")
        self.assertEqual([0, 45, 30, 15], [(bar["t"] // minute) % 60 for bar in bars_45])

        hour = 60 * minute
        source_16 = ta.parse_closed_bars(raw_series("8h", 8), "8h", 64 * hour)
        bars_16 = ta.aggregate_bars(source_16, "8h", "16h")
        self.assertEqual([0, 16, 8, 0], [(bar["t"] // hour) % 24 for bar in bars_16])

    def test_expected_leading_and_trailing_partial_groups_drop(self):
        source_span = ta.INTERVAL_MS["15m"]
        raw = raw_series("15m", 6, source_span)
        source = ta.parse_closed_bars(raw, "15m", 8 * source_span)
        result = ta.aggregate_bars(source, "15m", "45m")
        self.assertEqual([3 * source_span], [bar["t"] for bar in result])
        self.assertEqual(sum(bar["v"] for bar in source[2:5]), result[0]["v"])

    def test_duplicate_reverse_gap_bad_close_alignment_and_values_reject(self):
        span = ta.INTERVAL_MS["5m"]
        valid = raw_row(0)
        cases = {
            "missing": [[0, "1"]],
            "duplicate": [valid, valid],
            "reverse": [raw_row(span), valid],
            "gap": [valid, raw_row(2 * span)],
            "bad_close": [[0, "100", "101", "99", "100", "1", span]],
            "alignment": [raw_row(1)],
            "bool_time": [[False, "100", "101", "99", "100", "1", span - 1]],
            "bool_price": [[0, True, "101", "99", "100", "1", span - 1]],
            "nan_number": [[0, "NaN", "101", "99", "100", "1", span - 1]],
            "inf_number": [[0, "100", math.inf, "99", "100", "1", span - 1]],
            "zero_price": [[0, "0", "101", "0", "100", "1", span - 1]],
            "negative_volume": [[0, "100", "101", "99", "100", "-1", span - 1]],
            "high_invariant": [[0, "100", "100.1", "99", "101", "1", span - 1]],
            "low_invariant": [[0, "100", "101", "100.1", "100", "1", span - 1]],
            "float_time_string": [["0.0", "100", "101", "99", "100", "1", span - 1]],
            "unsafe_time": [[ta.MAX_SAFE_INTEGER + 1, "100", "101", "99", "100", "1", 1]],
        }
        for name, payload in cases.items():
            with self.subTest(name=name), self.assertRaises(ta.TimeframeDataError):
                ta.parse_closed_bars(payload, "5m", 3 * span)

    def test_open_interior_or_multiple_future_bars_rejects(self):
        span = ta.INTERVAL_MS["5m"]
        with self.assertRaises(ta.TimeframeDataError) as caught:
            ta.parse_closed_bars(raw_series("5m", 3), "5m", span + 1)
        self.assertEqual(ta.UnknownReason.UNEXPECTED_OPEN.value, caught.exception.reason)

    def test_internal_partial_bucket_and_mapping_gap_are_unknown(self):
        source = ta.parse_closed_bars(raw_series("15m", 9), "15m", 10 * ta.INTERVAL_MS["15m"])
        del source[4]
        with self.assertRaises(ta.TimeframeDataError) as caught:
            ta.aggregate_bars(source, "15m", "45m")
        self.assertEqual(ta.UnknownReason.GAP.value, caught.exception.reason)


class AnalysisTests(unittest.TestCase):
    def test_native_frame_with_sixty_old_closed_bars_requires_latest_expected_close(self):
        span = 900_000
        result = ta.analyze_frames(
            {"15m": fixed_raw(span, 60)},
            BASE + 61 * span,
            intervals=["15m"],
        )
        frame = result["frames"]["15m"]
        self.assertEqual("unknown", frame["status"])
        self.assertIsNone(frame["features"])
        self.assertEqual("missing_latest_closed", frame["metadata"]["unknown_reason"])
        self.assertEqual(1_791_989_999_999, frame["metadata"]["actual_asof"])
        self.assertEqual(1_791_990_899_999, frame["metadata"]["expected_asof"])
        self.assertIs(frame["metadata"]["stale"], True)
        self.assertIs(result["data_stale"], True)
        self.assertIs(result["stale"], True)

    def test_derived_16h_frame_rejects_missing_latest_complete_bucket(self):
        source_span = 28_800_000
        target_span = 57_600_000
        result = ta.analyze_frames(
            {"8h": fixed_raw(source_span, 121)},
            BASE + 61 * target_span,
            intervals=["16h"],
        )
        frame = result["frames"]["16h"]
        self.assertEqual("unknown", frame["status"])
        self.assertIsNone(frame["features"])
        self.assertEqual("missing_latest_closed", frame["metadata"]["unknown_reason"])
        self.assertEqual(1_795_391_999_999, frame["metadata"]["actual_asof"])
        self.assertEqual(1_795_449_599_999, frame["metadata"]["expected_asof"])
        self.assertIs(frame["metadata"]["stale"], True)

    def test_derived_16h_latest_bucket_is_valid_ten_hours_after_close(self):
        source_span = 28_800_000
        now_ms = BASE + (60 * 16 + 10) * 3_600_000
        result = ta.analyze_frames(
            {"8h": fixed_raw(source_span, 121)},
            now_ms,
            intervals=["16h"],
        )
        frame = result["frames"]["16h"]
        expected = BASE + 60 * ta.INTERVAL_MS["16h"] - 1
        self.assertEqual("ready", frame["status"])
        self.assertIsNotNone(frame["features"])
        self.assertEqual(expected, frame["metadata"]["actual_asof"])
        self.assertEqual(expected, frame["metadata"]["expected_asof"])
        self.assertIs(frame["metadata"]["stale"], False)
        self.assertIs(result["data_stale"], False)
        self.assertIs(result["stale"], False)

    def test_insufficient_frame_has_no_indicator_values_and_explicit_metadata(self):
        span = ta.INTERVAL_MS["5m"]
        result = ta.analyze_frames({"5m": raw_series("5m", 59)}, 60 * span, intervals=["5m"])
        frame = result["frames"]["5m"]
        self.assertEqual("unknown", frame["status"])
        self.assertIsNone(frame["features"])
        self.assertEqual("insufficient_data", frame["metadata"]["unknown_reason"])
        for key in ("input_count", "source_closed_count", "closed_count", "coverage", "last_closed_at"):
            self.assertIn(key, frame["metadata"])

    def test_latest_two_closed_pattern_replaces_legacy_offset(self):
        span = ta.INTERVAL_MS["5m"]
        raw = raw_series("5m", 60)
        raw[-2] = raw_row(58 * span, o=110, h=111, low=107, c=108)
        raw[-1] = raw_row(59 * span, o=107, h=112, low=106, c=111)
        result = ta.analyze_frames({"5m": raw}, 60 * span, intervals=["5m"])
        features = result["frames"]["5m"]["features"]
        self.assertEqual("bullish_engulfing", features["pattern"])
        self.assertEqual(60 * span - 1, features["pattern_as_of"])

    def test_deterministic_all_ten_demo_frames_ready_and_zero_io(self):
        now_ms = 2_000_000_000_000
        first = ta.synthetic_sources(now_ms, "DEMO1USDT")
        second = ta.synthetic_sources(now_ms, "DEMO1USDT")
        self.assertEqual(first, second)
        result = ta.analyze_frames(first, now_ms, "DEMO1USDT")
        self.assertEqual(list(ta.CANONICAL_INTERVALS), list(result["frames"]))
        self.assertTrue(all(frame["status"] == "ready" for frame in result["frames"].values()))
        self.assertTrue(result["analysis_only"])
        self.assertFalse(result["execution_authority"])

        calls = []

        def forbidden_fetch(*args):
            calls.append(args)
            raise AssertionError("demo mode must not fetch")

        service = ta.TimeframeService(demo=True, fetcher=forbidden_fetch, clock=lambda: now_ms / 1000.0)
        for index in range(1, 11):
            selected = service.select(f"DEMO{index}USDT", owner=True, csrf_valid=True)
            self.assertEqual("ready", selected["status"])
        self.assertEqual([], calls)
        self.assertLessEqual(len(service.snapshot()["symbols"]), 4)
        service.close()

    def test_feature_output_is_finite_or_null(self):
        now_ms = 2_000_000_000_000
        result = ta.analyze_frames(ta.synthetic_sources(now_ms), now_ms)
        for frame in result["frames"].values():
            for value in frame["features"].values():
                if isinstance(value, float):
                    self.assertTrue(math.isfinite(value))


class ServiceTests(unittest.TestCase):
    def test_run_job_and_snapshot_preserve_data_stale_separately_from_cache_age(self):
        now_ms = 2_000_000_000_000
        clock = MutableClock()
        stale_analysis = {
            "symbol": "DEMO1USDT",
            "status": "unknown",
            "server_time": now_ms,
            "frames": {},
            "data_stale": True,
            "stale": True,
            "analysis_only": True,
            "execution_authority": False,
        }
        service = ta.TimeframeService(
            demo=True,
            clock=lambda: now_ms / 1000.0,
            monotonic=clock,
            worker=InlineWorker(),
        )
        with mock.patch.object(ta, "analyze_frames", return_value=stale_analysis):
            selected = service.select("DEMO1USDT", owner=True, csrf_valid=True)
        self.assertIs(selected["data_stale"], True)
        self.assertIs(selected["cache_stale"], False)
        self.assertIs(selected["stale"], True)

        fresh_cache = service.snapshot("DEMO1USDT")
        self.assertIs(fresh_cache["data_stale"], True)
        self.assertIs(fresh_cache["cache_stale"], False)
        self.assertIs(fresh_cache["stale"], True)
        clock.value += ta.TTL_SECONDS
        old_cache = service.snapshot("DEMO1USDT")
        self.assertIs(old_cache["data_stale"], True)
        self.assertIs(old_cache["cache_stale"], True)
        self.assertIs(old_cache["stale"], True)
        service.close()

    def test_snapshot_getters_recompute_stale_without_fetch_or_cache_mutation(self):
        clock = MutableClock()
        fetcher = mock.Mock(side_effect=AssertionError("snapshot must not fetch"))
        service = ta.TimeframeService(
            demo=False,
            enabled=True,
            fetcher=fetcher,
            monotonic=clock,
            worker=InlineWorker(),
        )
        service._store(
            "BTCUSDT",
            {"symbol": "BTCUSDT", "status": "ready", "stale": False},
        )
        cache_before = json.dumps(service._cache, sort_keys=True)

        self.assertFalse(service.snapshot("BTCUSDT")["stale"])
        self.assertFalse(service.snapshot()["symbols"]["BTCUSDT"]["stale"])

        clock.value += ta.TTL_SECONDS
        self.assertTrue(service.snapshot("BTCUSDT")["stale"])
        self.assertTrue(service.snapshot()["symbols"]["BTCUSDT"]["stale"])

        fetcher.assert_not_called()
        self.assertEqual(cache_before, json.dumps(service._cache, sort_keys=True))
        service.close()

    def test_owner_csrf_gate_disabled_mode_and_demo_allowlist(self):
        demo = ta.TimeframeService(clock=lambda: 2_000_000_000.0)
        self.assertEqual("forbidden", demo.select("DEMO1USDT")["reason"])
        self.assertEqual(
            "invalid_symbol",
            demo.select("BTCUSDT", owner=True, csrf_valid=True)["reason"],
        )
        demo.close()
        real = ta.TimeframeService(demo=False, enabled=False)
        self.assertEqual(
            "disabled",
            real.select("BTCUSDT", owner=True, csrf_valid=True)["status"],
        )
        real.close()

    def test_real_job_uses_time_plus_eight_native_calls_reuses_sources_and_ttl(self):
        now_ms = 2_000_000_000_000
        fake = FakePublicFetcher(now_ms)
        monotonic = MutableClock()
        service = ta.TimeframeService(
            demo=False,
            enabled=True,
            fetcher=fake,
            clock=lambda: now_ms / 1000.0,
            monotonic=monotonic,
            worker=InlineWorker(),
        )
        first = service.select("BTCUSDT", owner=True, csrf_valid=True)
        self.assertEqual("ready", first["status"])
        self.assertEqual(9, len(fake.calls))
        self.assertEqual(1, sum(path == ta.TIME_PATH for path, _ in fake.calls))
        requested = [params["interval"] for path, params in fake.calls if path == ta.KLINES_PATH]
        self.assertEqual(set(ta.NATIVE_INTERVALS), set(requested))
        self.assertEqual(8, len(requested))
        self.assertEqual(first["fetched_at"], service.snapshot("BTCUSDT")["fetched_at"])
        self.assertEqual(now_ms, first["server_time"])

        second = service.select("BTCUSDT", owner=True, csrf_valid=True)
        self.assertEqual("ready", second["status"])
        self.assertEqual(9, len(fake.calls))
        monotonic.value += 61
        service.select("BTCUSDT", owner=True, csrf_valid=True)
        self.assertEqual(18, len(fake.calls))
        service.close()

    def test_failure_cooldown_prevents_timer_hammer(self):
        calls = []

        def failing(path, params):
            calls.append((path, params))
            raise RuntimeError("private provider detail")

        clock = MutableClock()
        service = ta.TimeframeService(
            demo=False,
            enabled=True,
            fetcher=failing,
            clock=clock,
            monotonic=clock,
            worker=InlineWorker(),
        )
        first = service.select("BTCUSDT", owner=True, csrf_valid=True)
        second = service.select("BTCUSDT", owner=True, csrf_valid=True)
        self.assertEqual("failure", first["status"])
        self.assertEqual("fetch_failed", first["reason"])
        self.assertNotIn("private", json.dumps(first))
        self.assertEqual(first, second)
        self.assertEqual(1, len(calls))
        clock.value += 61
        service.select("BTCUSDT", owner=True, csrf_valid=True)
        self.assertEqual(2, len(calls))
        service.close()

    def test_dedup_and_global_four_symbol_bound_without_unbounded_queue(self):
        worker = ManualWorker()
        service = ta.TimeframeService(demo=False, enabled=True, worker=worker)
        symbols = ["AAUSDT", "BBUSDT", "CCUSDT", "DDUSDT"]
        for symbol in symbols:
            self.assertEqual("pending", service.select(symbol, owner=True, csrf_valid=True)["status"])
        self.assertEqual(4, len(worker.tasks))
        self.assertEqual("pending", service.select("AAUSDT", owner=True, csrf_valid=True)["status"])
        self.assertEqual(4, len(worker.tasks))
        fifth = service.select("EEUSDT", owner=True, csrf_valid=True)
        self.assertEqual("capacity", fifth["reason"])
        self.assertEqual(4, len(service.snapshot()["pending"]))
        service.close()
        self.assertFalse(worker.shutdown_called)

    def test_owned_workers_never_exceed_two_concurrent_jobs(self):
        release = threading.Event()
        two_started = threading.Event()
        lock = threading.Lock()
        state = {"active": 0, "peak": 0, "started": 0}
        now_ms = 2_000_000_000_000
        fallback = FakePublicFetcher(now_ms)

        def blocking(path, params):
            if path == ta.TIME_PATH:
                with lock:
                    state["active"] += 1
                    state["started"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                    if state["started"] == 2:
                        two_started.set()
                release.wait(2.0)
                with lock:
                    state["active"] -= 1
                return {"serverTime": now_ms}
            return fallback(path, params)

        service = ta.TimeframeService(
            demo=False,
            enabled=True,
            fetcher=blocking,
            clock=lambda: now_ms / 1000.0,
            workers=2,
        )
        for symbol in ("AAUSDT", "BBUSDT", "CCUSDT", "DDUSDT"):
            service.select(symbol, owner=True, csrf_valid=True)
        self.assertTrue(two_started.wait(1.0))
        self.assertEqual(2, state["peak"])
        release.set()
        service.close()

    def test_close_returns_while_user_fake_is_blocking(self):
        started = threading.Event()
        release = threading.Event()

        def blocking(path, params):
            started.set()
            release.wait(2.0)
            raise RuntimeError("stopped")

        service = ta.TimeframeService(demo=False, enabled=True, fetcher=blocking, workers=1)
        service.select("AAUSDT", owner=True, csrf_valid=True)
        self.assertTrue(started.wait(1.0))
        before = time.monotonic()
        service.close()
        elapsed = time.monotonic() - before
        release.set()
        self.assertLess(elapsed, 0.2)


class FakeResponse:
    def __init__(self, payload, content_type="application/json", status=200, content_length=None):
        self.payload = payload
        self.status = status
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def getcode(self):
        return self.status

    def read(self, limit):
        return self.payload[:limit]


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


class TransportTests(unittest.TestCase):
    def test_default_transport_disables_proxies_and_redirects(self):
        captured = []

        def build(*handlers):
            captured.extend(handlers)
            return object()

        with mock.patch.object(ta.urllib.request, "build_opener", side_effect=build):
            ta.SafeBinanceFetcher()
        proxies = [handler for handler in captured if isinstance(handler, ta.urllib.request.ProxyHandler)]
        self.assertEqual(1, len(proxies))
        self.assertEqual({}, proxies[0].proxies)
        self.assertTrue(any(isinstance(handler, ta.NoRedirectHandler) for handler in captured))
        with self.assertRaises(ta.TransportError):
            ta.NoRedirectHandler().redirect_request(None, None, 302, "", {}, "https://example.invalid")

    def test_fixed_paths_validated_params_timeout_and_json_content_type(self):
        response = FakeResponse(b'{"serverTime":123456}')
        opener = FakeOpener(response)
        fetcher = ta.SafeBinanceFetcher(timeout=99, opener=opener)
        self.assertEqual({"serverTime": 123456}, fetcher(ta.TIME_PATH, {}))
        request, timeout = opener.requests[0]
        self.assertEqual("https://fapi.binance.com/fapi/v1/time", request.full_url)
        self.assertLessEqual(timeout, 3.0)
        with self.assertRaises(ta.TransportError):
            fetcher("https://attacker.invalid", {})
        with self.assertRaises(ta.TransportError):
            fetcher(ta.KLINES_PATH, {"symbol": "bad", "interval": "5m", "limit": 1})

        wrong_type = ta.SafeBinanceFetcher(opener=FakeOpener(FakeResponse(b"[]", "text/plain")))
        with self.assertRaises(ta.TransportError):
            wrong_type(ta.KLINES_PATH, {"symbol": "BTCUSDT", "interval": "5m", "limit": 1})

    def test_nonfinite_duplicate_json_and_oversize_reject(self):
        payloads = [
            b'{"serverTime":1e999}',
            b'{"serverTime":1,"serverTime":2}',
            b'{"serverTime":NaN}',
        ]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ta.TransportError):
                ta.SafeBinanceFetcher(opener=FakeOpener(FakeResponse(payload)))(ta.TIME_PATH, {})
        oversized = FakeResponse(b"{}", content_length=ta.MAX_RESPONSE_BYTES + 1)
        with self.assertRaises(ta.TransportError):
            ta.SafeBinanceFetcher(opener=FakeOpener(oversized))(ta.TIME_PATH, {})


class ResearchContextTests(unittest.TestCase):
    def test_public_context_is_ten_frame_whitelist_and_capability_rejects_pre_spawn(self):
        now_ms = 2_000_000_000_000
        analysis = ta.analyze_frames(ta.synthetic_sources(now_ms), now_ms, "DEMO1USDT")
        analysis["account"] = {"equity": 999}
        analysis["frames"]["5m"]["raw"] = [["secret"]]
        analysis["frames"]["5m"]["metadata"]["unknown_reason"] = "secret provider traceback"
        analysis["frames"]["5m"]["metadata"]["coverage"]["raw"] = "secret"
        analysis["frames"]["5m"]["features"]["trend"] = "secret"
        analysis["frames"]["5m"]["features"]["price"] = math.inf
        context = ta.public_research_context(analysis)
        self.assertEqual(list(ta.CANONICAL_INTERVALS), list(context["frames"]))
        self.assertIsNone(context["frames"]["5m"]["research"]["price"])
        self.assertIsNone(context["frames"]["5m"]["research"]["trend"])
        self.assertIsNone(context["frames"]["5m"]["metadata"]["unknown_reason"])
        serialized = json.dumps(context, sort_keys=True).lower()
        for forbidden in ("account", "positions", "equity", "kelly", "trades", "orders", "reviews", "secret", '"raw"'):
            self.assertNotIn(forbidden, serialized)
        capability = context["capability"]
        self.assertEqual("UNVERIFIED", capability["model"])
        self.assertIsNone(capability["provider_status"])
        for key in (
            "connector_enabled",
            "capability_verified",
            "deny_all_verified",
            "spawn_allowed",
            "tools_allowed",
            "tools_isolated",
            "mcp_allowed",
            "mcp_isolated",
            "plugins_allowed",
            "plugins_isolated",
        ):
            self.assertIs(capability[key], False)
        route = ta.antigravity_route(context)
        self.assertEqual("capability_unverified", route["status"])
        self.assertFalse(route["spawned"])


if __name__ == "__main__":
    unittest.main()
