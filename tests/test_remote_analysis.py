# -*- coding: utf-8 -*-
"""Focused integration tests for the closed-timeframe HTTP surfaces.

The fixtures bind only to 127.0.0.1 on port 0.  Fetchers, credentials and
analysis workers are inert in-memory doubles; no provider or native process is
contacted.
"""
import copy
import http.client
import json
import os
import re
import tempfile
import threading
import unittest

from scanner import demo_server
from scanner import remote_gateway as rg
from scanner.timeframe_analysis import (CANONICAL_INTERVALS, INTERVAL_LABELS,
                                        KLINES_PATH, SOURCE_LIMITS, TIME_PATH,
                                        TimeframeService, synthetic_sources)


PUBLIC_ORIGIN = "https://public.example.invalid"
PUBLIC_HOST = "public.example.invalid"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBROOT = os.path.join(PROJECT_ROOT, "web")
AUTH_STORE = os.path.join(tempfile.gettempdir(), "jev-analysis-owner-fixture.json")
FIXTURE_USERNAME = "analysis-owner"
FIXTURE_PASSWORD = "fixture-password-not-a-credential"


class FakePasswordStore:
    def exists(self):
        return True

    def verify(self, username, password):
        return username == FIXTURE_USERNAME and password == FIXTURE_PASSWORD


def _frame_result(symbol):
    frames = {}
    for interval in CANONICAL_INTERVALS:
        frames[interval] = {
            "interval": interval,
            "label": INTERVAL_LABELS[interval],
            "status": "unknown",
            "metadata": {"unknown_reason": "insufficient_data"},
            "features": None,
            "analysis_only": True,
            "execution_authority": False,
        }
    return {
        "symbol": symbol,
        "status": "unknown",
        "reason": "insufficient_data",
        "server_time": 1_800_000_000_000,
        "fetched_at": 1_800_000_000_100,
        "stale": False,
        "frames": frames,
        "analysis_only": True,
        "execution_authority": False,
    }


class RecordingAnalysisService:
    def __init__(self, demo=True, disabled=False):
        self.demo = demo
        self.disabled = disabled
        self.select_calls = []
        self.snapshot_calls = []
        self.close_calls = 0
        self.selected = None

    def select(self, symbol, *, owner=False, csrf_valid=False):
        self.select_calls.append((symbol, owner, csrf_valid))
        pattern = r"DEMO(?:[1-9]|1[0-2])USDT" if self.demo else r"[A-Z0-9]{2,24}USDT"
        if not isinstance(symbol, str) or re.fullmatch(pattern, symbol) is None:
            return {
                "symbol": symbol if isinstance(symbol, str) else None,
                "status": "failure",
                "reason": "invalid_symbol",
                "analysis_only": True,
                "execution_authority": False,
            }
        self.selected = symbol
        if self.disabled:
            return {
                "symbol": symbol,
                "status": "disabled",
                "reason": "disabled",
                "fetched_at": None,
                "server_time": None,
                "stale": False,
                "analysis_only": True,
                "execution_authority": False,
            }
        return _frame_result(symbol)

    def snapshot(self, symbol=None):
        self.snapshot_calls.append(symbol)
        return _frame_result(symbol) if symbol else {
            "symbol": None,
            "status": "unknown",
            "reason": "missing_source",
            "analysis_only": True,
            "execution_authority": False,
        }

    def close(self):
        self.close_calls += 1


def _header_values(headers, name):
    wanted = name.lower()
    return [value for key, value in headers if key.lower() == wanted]


def _request(port, method, target, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        conn.request(method, target, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        conn.close()


def _raw_request(port, method, target, headers, body=b""):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        for name, value in headers:
            conn.putheader(name, value)
        conn.endheaders(body)
        response = conn.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        conn.close()


def _csrf_from_html(body):
    for tag in re.findall(br"<meta\b[^>]*>", body, flags=re.I):
        if b"csrf" in tag.lower():
            match = re.search(br"\bcontent=[\"']([^\"']+)[\"']", tag, flags=re.I)
            if match:
                return match.group(1).decode("ascii")
    raise AssertionError("CSRF meta element missing")


def _cookie_pair(headers, cookie_name):
    prefix = cookie_name + "="
    for value in _header_values(headers, "Set-Cookie"):
        if value.startswith(prefix):
            return value.split(";", 1)[0]
    raise AssertionError("cookie missing: " + cookie_name)


def _login(port):
    status, headers, body = _request(port, "GET", "/login", {"Host": PUBLIC_HOST})
    if status != 200:
        raise AssertionError("login page status: %d" % status)
    preauth = _cookie_pair(headers, rg.PREAUTH_COOKIE)
    payload = json.dumps({
        "username": FIXTURE_USERNAME,
        "password": FIXTURE_PASSWORD,
    }).encode("utf-8")
    status, headers, body = _request(port, "POST", "/api/auth/login", {
        "Host": PUBLIC_HOST,
        "Origin": PUBLIC_ORIGIN,
        "Content-Type": "application/json",
        rg.CSRF_HEADER: _csrf_from_html(body),
        "Cookie": preauth,
    }, payload)
    if status != 200:
        raise AssertionError("login status: %d, body=%r" % (status, body))
    response = json.loads(body.decode("utf-8"))
    return {"session": _cookie_pair(headers, rg.SESSION_COOKIE),
            "csrf": response["csrf"]}


def _start_remote(config, service):
    httpd, gateway = rg.build_server(
        config, webroot=WEBROOT, password_store=FakePasswordStore(),
        analysis_service=service)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, gateway, thread


def _stop_remote(httpd, gateway, thread):
    if thread.is_alive():
        httpd.shutdown()
    gateway.close()
    httpd.server_close()
    thread.join(timeout=4)


def _owner_headers(login):
    return {
        "Host": PUBLIC_HOST,
        "Origin": PUBLIC_ORIGIN,
        "Content-Type": "application/json",
        rg.CSRF_HEADER: login["csrf"],
        "Cookie": login["session"],
    }


class RemoteAnalysisHTTPTests(unittest.TestCase):
    def setUp(self):
        self.service = RecordingAnalysisService(demo=True)
        config = rg.GatewayConfig(
            PUBLIC_ORIGIN, AUTH_STORE, port=0, enable_antigravity=True)
        self.httpd, self.gateway, self.thread = _start_remote(config, self.service)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        _stop_remote(self.httpd, self.gateway, self.thread)

    def test_authenticated_select_then_get_returns_only_selected_snapshot(self):
        login = _login(self.port)
        body = json.dumps({"symbol": "DEMO1USDT"}).encode("utf-8")
        status, _headers, raw = _request(
            self.port, "POST", "/api/analysis/select", _owner_headers(login), body)
        self.assertEqual(status, 200)
        selected = json.loads(raw.decode("utf-8"))
        self.assertEqual(selected["symbol"], "DEMO1USDT")
        self.assertEqual(selected["source"], "DEMO")
        self.assertEqual(len(selected["frames"]), 10)
        self.assertTrue(selected["analysis_only"])
        self.assertFalse(selected["execution_authority"])
        connector = selected["antigravity"]
        self.assertEqual(connector["protocol_id"], "AGY")
        self.assertEqual(connector["display_name"], "Antigravity")
        self.assertEqual(connector["status"], "capability_unverified")
        self.assertFalse(connector["spawned"])
        self.assertFalse(connector["capability"]["deny_all_verified"])
        self.assertFalse(connector["capability"]["tools_allowed"])
        self.assertFalse(connector["capability"]["mcp_allowed"])
        self.assertFalse(connector["capability"]["plugins_allowed"])
        self.assertTrue(connector["context_ready"])
        self.assertEqual(connector["frame_count"], 10)

        before = list(self.service.select_calls)
        status, _headers, raw = _request(self.port, "GET", "/api/analysis/timeframes", {
            "Host": PUBLIC_HOST, "Cookie": login["session"]})
        self.assertEqual(status, 200)
        snapshot = json.loads(raw.decode("utf-8"))
        self.assertEqual(snapshot["symbol"], "DEMO1USDT")
        self.assertNotIn("symbols", snapshot)
        self.assertEqual(self.service.select_calls, before)
        self.assertEqual(self.service.snapshot_calls, ["DEMO1USDT"])
        self.assertNotIn(b"NaN", raw)
        self.assertNotIn(b"Infinity", raw)

    def test_auth_origin_csrf_shape_and_duplicate_headers_precede_side_effects(self):
        payload = json.dumps({"symbol": "DEMO1USDT"}).encode("utf-8")
        status, _headers, _body = _request(self.port, "GET", "/api/analysis/timeframes", {
            "Host": PUBLIC_HOST})
        self.assertEqual(status, 401)
        status, _headers, _body = _request(self.port, "POST", "/api/analysis/select", {
            "Host": PUBLIC_HOST,
            "Origin": PUBLIC_ORIGIN,
            "Content-Type": "application/json",
        }, payload)
        self.assertEqual(status, 401)

        login = _login(self.port)
        headers = _owner_headers(login)
        wrong_origin = dict(headers, Origin="https://wrong.example.invalid")
        self.assertEqual(_request(
            self.port, "POST", "/api/analysis/select", wrong_origin, payload)[0], 403)
        wrong_csrf = dict(headers)
        wrong_csrf[rg.CSRF_HEADER] = "not-the-session-token"
        self.assertEqual(_request(
            self.port, "POST", "/api/analysis/select", wrong_csrf, payload)[0], 403)
        extra = json.dumps({"symbol": "DEMO1USDT", "cutoff": 1}).encode("utf-8")
        self.assertEqual(_request(
            self.port, "POST", "/api/analysis/select", headers, extra)[0], 400)
        bad_type = json.dumps({"symbol": ["DEMO1USDT"]}).encode("utf-8")
        self.assertEqual(_request(
            self.port, "POST", "/api/analysis/select", headers, bad_type)[0], 400)

        duplicate = [
            ("Host", PUBLIC_HOST),
            ("Origin", PUBLIC_ORIGIN),
            ("Origin", PUBLIC_ORIGIN),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(payload))),
            (rg.CSRF_HEADER, login["csrf"]),
            ("Cookie", login["session"]),
        ]
        self.assertEqual(_raw_request(
            self.port, "POST", "/api/analysis/select", duplicate, payload)[0], 400)
        self.assertEqual(self.service.select_calls, [])

    def test_query_routes_and_financial_routes_are_not_repurposed(self):
        login = _login(self.port)
        self.assertEqual(_request(
            self.port, "GET", "/api/analysis/timeframes?symbol=DEMO1USDT", {
                "Host": PUBLIC_HOST, "Cookie": login["session"]})[0], 404)
        self.assertEqual(_request(
            self.port, "POST", "/api/scan", _owner_headers(login), b"{}")[0], 200)
        self.assertEqual(self.service.select_calls, [])


class RemoteRealModeTests(unittest.TestCase):
    def test_market_analysis_requires_read_only_real_mode(self):
        with self.assertRaises(rg.ConfigError) as caught:
            rg.GatewayConfig(
                PUBLIC_ORIGIN, AUTH_STORE, port=0, market_analysis=True)
        self.assertEqual(caught.exception.code, "market_analysis_requires_real_mode")

    def test_read_only_real_allows_research_but_keeps_financial_post_blocked(self):
        service = RecordingAnalysisService(demo=False)
        config = rg.GatewayConfig(
            PUBLIC_ORIGIN, AUTH_STORE, port=0,
            upstream=rg.ALLOWED_UPSTREAM, companion=rg.ALLOWED_COMPANION,
            market_analysis=True)
        httpd, gateway, thread = _start_remote(config, service)
        try:
            port = httpd.server_address[1]
            login = _login(port)
            headers = _owner_headers(login)
            payload = json.dumps({"symbol": "BTCUSDT"}).encode("utf-8")
            status, _headers, raw = _request(
                port, "POST", "/api/analysis/select", headers, payload)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw.decode("utf-8"))["source"], "PUBLIC_BINANCE")
            self.assertEqual(_request(
                port, "POST", "/api/scan", headers, b"{}")[0], 403)
            self.assertEqual(service.select_calls, [("BTCUSDT", True, True)])
        finally:
            _stop_remote(httpd, gateway, thread)

    def test_real_mode_without_opt_in_is_disabled_and_does_not_fetch(self):
        calls = []

        def forbidden_fetch(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("disabled analysis attempted I/O")

        service = TimeframeService(demo=False, enabled=False, fetcher=forbidden_fetch)
        config = rg.GatewayConfig(
            PUBLIC_ORIGIN, AUTH_STORE, port=0,
            upstream=rg.ALLOWED_UPSTREAM, companion=rg.ALLOWED_COMPANION)
        httpd, gateway, thread = _start_remote(config, service)
        try:
            port = httpd.server_address[1]
            login = _login(port)
            payload = json.dumps({"symbol": "BTCUSDT"}).encode("utf-8")
            status, _headers, raw = _request(
                port, "POST", "/api/analysis/select", _owner_headers(login), payload)
            self.assertEqual(status, 200)
            response = json.loads(raw.decode("utf-8"))
            self.assertEqual(response["status"], "disabled")
            self.assertEqual(response["reason"], "disabled")
            status, _headers, raw = _request(port, "GET", "/api/analysis/timeframes", {
                "Host": PUBLIC_HOST, "Cookie": login["session"]})
            self.assertEqual(status, 200)
            snapshot = json.loads(raw.decode("utf-8"))
            self.assertEqual(snapshot["status"], "disabled")
            self.assertEqual(snapshot["reason"], "disabled")
            self.assertEqual(calls, [])
        finally:
            _stop_remote(httpd, gateway, thread)


class HoldingWorker:
    def __init__(self):
        self.jobs = []

    def submit(self, function, *args):
        self.jobs.append((function, args))

    def run_one(self):
        function, args = self.jobs.pop(0)
        function(*args)


class PublicFetcher:
    def __init__(self, server_time):
        self.server_time = server_time
        self.sources = synthetic_sources(server_time, "DEMO1USDT")
        self.calls = []

    def __call__(self, path, params):
        self.calls.append((path, copy.deepcopy(dict(params))))
        if path == TIME_PATH:
            return {"serverTime": self.server_time}
        if path == KLINES_PATH:
            return copy.deepcopy(self.sources[params["interval"]])
        raise AssertionError("unexpected public path")


class TimeframeServiceBoundaryTests(unittest.TestCase):
    def test_cold_public_selection_fetches_time_plus_eight_sources_once(self):
        server_time = 1_800_000_000_000
        worker = HoldingWorker()
        fetcher = PublicFetcher(server_time)
        service = TimeframeService(
            demo=False, enabled=True, fetcher=fetcher, worker=worker,
            clock=lambda: server_time / 1000.0, monotonic=lambda: 100.0)
        try:
            first = service.select("BTCUSDT", owner=True, csrf_valid=True)
            second = service.select("BTCUSDT", owner=True, csrf_valid=True)
            self.assertEqual(first["status"], "pending")
            self.assertEqual(second["status"], "pending")
            self.assertEqual(len(worker.jobs), 1)
            self.assertEqual(fetcher.calls, [])
            worker.run_one()

            snapshot = service.snapshot("BTCUSDT")
            self.assertEqual(snapshot["symbol"], "BTCUSDT")
            self.assertEqual(len(snapshot["frames"]), 10)
            self.assertEqual(len(fetcher.calls), 1 + len(SOURCE_LIMITS))
            self.assertEqual(fetcher.calls[0], (TIME_PATH, {}))
            self.assertEqual(
                [params["interval"] for path, params in fetcher.calls if path == KLINES_PATH],
                list(SOURCE_LIMITS))
            cached = service.select("BTCUSDT", owner=True, csrf_valid=True)
            self.assertEqual(cached["symbol"], "BTCUSDT")
            self.assertEqual(len(fetcher.calls), 1 + len(SOURCE_LIMITS))
        finally:
            service.close()

    def test_demo_true_remains_zero_io_even_if_enabled(self):
        calls = []

        def forbidden_fetch(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("DEMO attempted provider I/O")

        service = TimeframeService(
            demo=True, enabled=True, fetcher=forbidden_fetch,
            clock=lambda: 1_800_000_000.0, monotonic=lambda: 100.0)
        try:
            result = service.select("DEMO1USDT", owner=True, csrf_valid=True)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(len(result["frames"]), 10)
            self.assertEqual(calls, [])
        finally:
            service.close()


class LocalDemoAnalysisHTTPTests(unittest.TestCase):
    def test_local_same_origin_select_and_get_are_synthetic_and_closed(self):
        calls = []

        def forbidden_fetch(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("local DEMO attempted provider I/O")

        service = TimeframeService(
            demo=True, enabled=True, fetcher=forbidden_fetch,
            clock=lambda: 1_800_000_000.0, monotonic=lambda: 100.0)
        httpd, _engine = demo_server.build(
            PROJECT_ROOT, 0, analysis_service=service)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        host = "127.0.0.1:%d" % port
        origin = "http://" + host
        try:
            payload = json.dumps({"symbol": "DEMO1USDT"}).encode("utf-8")
            status, _headers, raw = _request(port, "POST", "/api/analysis/select", {
                "Host": host,
                "Origin": origin,
                "Content-Type": "application/json",
            }, payload)
            self.assertEqual(status, 200)
            selected = json.loads(raw.decode("utf-8"))
            self.assertEqual(selected["source"], "DEMO")
            self.assertEqual(selected["status"], "ready")
            self.assertEqual(tuple(selected["frames"]), CANONICAL_INTERVALS)
            self.assertTrue(all(
                frame["metadata"]["last_closed_at"] <= selected["server_time"]
                for frame in selected["frames"].values()))

            status, _headers, raw = _request(port, "GET", "/api/analysis/timeframes", {
                "Host": host})
            self.assertEqual(status, 200)
            snapshot = json.loads(raw.decode("utf-8"))
            self.assertEqual(snapshot["symbol"], "DEMO1USDT")
            self.assertEqual(len(snapshot["frames"]), 10)
            self.assertEqual(calls, [])
            self.assertNotIn("127.0.0.1", raw.decode("utf-8"))
        finally:
            if thread.is_alive():
                httpd.shutdown()
            httpd.server_close()
            service.close()
            thread.join(timeout=4)

    def test_local_wrong_origin_and_extra_keys_have_no_selection_side_effect(self):
        service = RecordingAnalysisService(demo=True)
        httpd, _engine = demo_server.build(
            PROJECT_ROOT, 0, analysis_service=service)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        host = "127.0.0.1:%d" % port
        try:
            payload = json.dumps({"symbol": "DEMO1USDT"}).encode("utf-8")
            status = _request(port, "POST", "/api/analysis/select", {
                "Host": host,
                "Origin": "http://wrong.example.invalid",
                "Content-Type": "application/json",
            }, payload)[0]
            self.assertEqual(status, 403)
            extra = json.dumps({"symbol": "DEMO1USDT", "as_of": 1}).encode("utf-8")
            status = _request(port, "POST", "/api/analysis/select", {
                "Host": host,
                "Origin": "http://" + host,
                "Content-Type": "application/json",
            }, extra)[0]
            self.assertEqual(status, 400)
            self.assertEqual(service.select_calls, [])
        finally:
            if thread.is_alive():
                httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=4)


if __name__ == "__main__":
    unittest.main()
