# -*- coding: utf-8 -*-
"""Focused HTTP regression tests for the password remote gateway.

All credentials and stores in this module are inert fixtures.  Servers bind only
to 127.0.0.1 on port 0, and no upstream or provider service is contacted.
"""
import http.client
import json
import os
import posixpath
import re
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from scanner import remote_gateway as rg


PUBLIC_ORIGIN = "https://public.example.invalid"
PUBLIC_HOST = "public.example.invalid"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBROOT = os.path.join(PROJECT_ROOT, "web")
# The path is outside the checkout and is never opened: FakePasswordStore owns
# every test.  A stable temp path is intentionally retained for repeated runs.
AUTH_STORE = os.path.join(tempfile.gettempdir(), "jev-remote-gateway-fixture-owner.json")
FIXTURE_USERNAME = "fixture-owner"
FIXTURE_PASSWORD = "fixture-password-not-a-real-credential"


class FakePasswordStore:
    def __init__(self):
        self.verify_calls = 0

    def exists(self):
        return True

    def verify(self, username, password):
        self.verify_calls += 1
        return username == FIXTURE_USERNAME and password == FIXTURE_PASSWORD


def _header_values(headers, name):
    wanted = name.lower()
    return [value for key, value in headers if key.lower() == wanted]


def _header(headers, name):
    values = _header_values(headers, name)
    return values[-1] if values else None


def _request(port, method, target, headers=None, body=None):
    conn = http.client.HTTPConnection(rg.BIND_HOST, port, timeout=4)
    try:
        conn.request(method, target, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        return response.status, response.getheaders(), data
    finally:
        conn.close()


def _raw_request(port, method, target, headers, body=b""):
    """Send the exact request-target/header framing through http.client."""
    conn = http.client.HTTPConnection(rg.BIND_HOST, port, timeout=4)
    try:
        conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        for name, value in headers:
            conn.putheader(name, value)
        conn.endheaders(body)
        response = conn.getresponse()
        data = response.read()
        return response.status, response.getheaders(), data
    finally:
        conn.close()


def _csrf_from_html(body):
    for tag in re.findall(br"<meta\b[^>]*>", body, flags=re.I):
        if b"csrf" not in tag.lower():
            continue
        match = re.search(br"\bcontent=[\"']([^\"']+)[\"']", tag, flags=re.I)
        if match:
            return match.group(1).decode("ascii")
    raise AssertionError("CSRF meta element missing")


def _cookie_pair(set_cookie_headers, cookie_name):
    prefix = cookie_name + "="
    for value in set_cookie_headers:
        if value.startswith(prefix):
            return value.split(";", 1)[0], value
    raise AssertionError("cookie missing: " + cookie_name)


def _login(port):
    status, headers, body = _request(
        port, "GET", "/login", headers={"Host": PUBLIC_HOST})
    if status != 200:
        raise AssertionError("login page status: %d" % status)
    csrf = _csrf_from_html(body)
    preauth_pair, preauth_header = _cookie_pair(
        _header_values(headers, "Set-Cookie"), rg.PREAUTH_COOKIE)
    payload = json.dumps({
        "username": FIXTURE_USERNAME,
        "password": FIXTURE_PASSWORD,
    }).encode("utf-8")
    status, headers, body = _request(port, "POST", "/api/auth/login", headers={
        "Host": PUBLIC_HOST,
        "Origin": PUBLIC_ORIGIN,
        "Content-Type": "application/json",
        "X-JEV-CSRF": csrf,
        "Cookie": preauth_pair,
    }, body=payload)
    if status != 200:
        raise AssertionError("login status: %d, body=%r" % (status, body))
    response = json.loads(body.decode("utf-8"))
    session_pair, session_header = _cookie_pair(
        _header_values(headers, "Set-Cookie"), rg.SESSION_COOKIE)
    return {
        "csrf": response["csrf"],
        "session": session_pair,
        "session_header": session_header,
        "preauth_header": preauth_header,
    }


def _session_status(port, login):
    return _request(port, "GET", "/api/auth/session", headers={
        "Host": PUBLIC_HOST,
        "Cookie": login["session"],
    })


def _shutdown(httpd, thread=None):
    if thread is not None and thread.is_alive():
        httpd.shutdown()
    httpd.server_close()
    if thread is not None:
        thread.join(timeout=4)


class GatewayHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.passwords = FakePasswordStore()
        config = rg.GatewayConfig(PUBLIC_ORIGIN, AUTH_STORE, port=0)

        def no_upstream(_url):
            raise AssertionError("DEMO attempted an upstream request")

        cls.httpd, cls.gateway = rg.build_server(
            config, webroot=WEBROOT, password_store=cls.passwords,
            fetcher=no_upstream)
        cls.port = cls.gateway.bound_port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        _shutdown(cls.httpd, cls.thread)

    def test_valid_login_session_cookie_and_logout(self):
        login = _login(self.port)
        for cookie_header in (login["preauth_header"], login["session_header"]):
            self.assertIn("Path=/", cookie_header)
            self.assertIn("Secure", cookie_header)
            self.assertIn("HttpOnly", cookie_header)
            self.assertIn("SameSite=Strict", cookie_header)
            self.assertNotIn("Domain=", cookie_header)

        status, headers, body = _session_status(self.port, login)
        self.assertEqual(200, status)
        self.assertEqual("no-store", _header(headers, "Cache-Control"))
        self.assertTrue(json.loads(body.decode("utf-8"))["ok"])

        status, headers, body = _request(
            self.port, "POST", "/api/auth/logout", headers={
                "Host": PUBLIC_HOST,
                "Origin": PUBLIC_ORIGIN,
                "Content-Type": "application/json",
                "X-JEV-CSRF": login["csrf"],
                "Cookie": login["session"],
            }, body=b"{}")
        self.assertEqual(200, status)
        self.assertTrue(json.loads(body.decode("utf-8"))["logged_out"])
        cleared = _header_values(headers, "Set-Cookie")
        self.assertTrue(any(item.startswith(rg.SESSION_COOKIE + "=") and
                            "Max-Age=0" in item for item in cleared))

        status, _headers, body = _session_status(self.port, login)
        self.assertEqual(401, status)
        self.assertEqual("auth_required",
                         json.loads(body.decode("utf-8"))["reason"])

    def test_malformed_logout_never_revokes_session(self):
        login = _login(self.port)
        common = [
            ("Host", PUBLIC_HOST),
            ("Origin", PUBLIC_ORIGIN),
            ("X-JEV-CSRF", login["csrf"]),
            ("Cookie", login["session"]),
        ]
        cases = (
            (common + [("Content-Type", "application/json")], b"", 411),
            (common + [("Content-Type", "application/json"),
                       ("Content-Length", "bogus")], b"", 400),
            (common + [("Content-Length", "2")], b"{}", 415),
            (common + [("Content-Type", "application/json"),
                       ("Content-Length", "7")], b'{"x":1}', 400),
            (common + [("Content-Type", "application/json"),
                       ("Content-Length", "2")], b"[]", 400),
        )
        for headers, request_body, expected in cases:
            status, _response_headers, _body = _raw_request(
                self.port, "POST", "/api/auth/logout", headers, request_body)
            self.assertEqual(expected, status)
            session_status, _headers, session_body = _session_status(self.port, login)
            self.assertEqual(200, session_status)
            self.assertTrue(json.loads(session_body.decode("utf-8"))["ok"])

        valid_headers = {
            "Host": PUBLIC_HOST,
            "Origin": PUBLIC_ORIGIN,
            "Content-Type": "application/json",
            "X-JEV-CSRF": login["csrf"],
            "Cookie": login["session"],
        }
        status, _headers, _body = _request(
            self.port, "POST", "/api/auth/logout",
            headers=dict(valid_headers, Origin="https://PUBLIC.EXAMPLE.INVALID"),
            body=b"{}")
        self.assertEqual(403, status)
        self.assertEqual(200, _session_status(self.port, login)[0])

        status, _headers, _body = _request(
            self.port, "POST", "/api/auth/logout",
            headers=dict(valid_headers, **{"X-JEV-CSRF": "invalid"}),
            body=b"{}")
        self.assertEqual(403, status)
        self.assertEqual(200, _session_status(self.port, login)[0])

        self.assertEqual(200, _request(
            self.port, "POST", "/api/auth/logout",
            headers=valid_headers, body=b"{}")[0])

    def test_raw_request_target_ambiguity_is_rejected(self):
        targets = (
            "//evil/api/status",
            "http://public.example.invalid/api/status",
            "/api/status?preview=1",
            "/api/%2e%2e/status",
            "/api/%252e%252e/status",
            "/api%5cstatus",
            "/api\\status",
            "/api/../status",
        )
        for target in targets:
            status, _headers, body = _raw_request(
                self.port, "GET", target, [("Host", PUBLIC_HOST)])
            self.assertEqual(404, status, target)
            result = json.loads(body.decode("utf-8"))
            self.assertEqual({"ok": False, "reason": "not_found"}, result)
            self.assertNotIn(target.encode("utf-8"), body)

    def test_dashboard_static_asset_closure_and_security_headers(self):
        status, headers, _body = _request(
            self.port, "GET", "/", headers={"Host": PUBLIC_HOST})
        self.assertEqual(302, status)
        self.assertEqual("/login", _header(headers, "Location"))

        login = _login(self.port)
        status, headers, stage = _request(self.port, "GET", "/", headers={
            "Host": PUBLIC_HOST,
            "Cookie": login["session"],
        })
        self.assertEqual(200, status)
        self.assertEqual("no-store", _header(headers, "Cache-Control"))
        csp = _header(headers, "Content-Security-Policy")
        script_rule = next(item.strip() for item in csp.split(";")
                           if item.strip().startswith("script-src "))
        self.assertIn("'self'", script_rule)
        self.assertNotIn("'unsafe-inline'", script_rule)
        self.assertIn("'sha256-", script_rule)
        self.assertIn(b'/remote-client.js', stage)
        self.assertNotIn(b'/three-viz.js?v=17', stage)
        self.assertNotIn(b'/line-workers.js?v=4', stage)

        self._import_map = {}
        pending = set(self._asset_references("/", stage, "text/html"))
        pending.update(("/api/bootstrap.js", "/remote-client.js",
                        "/three-viz.js", "/line-workers.js"))
        seen = set()
        while pending:
            route = pending.pop()
            if route in seen:
                continue
            seen.add(route)

            pre_status, _pre_headers, _pre_body = _request(
                self.port, "HEAD", route, headers={"Host": PUBLIC_HOST})
            if route in rg.PRE_AUTH_ROUTES:
                self.assertEqual(200, pre_status, route)
            elif route.startswith("/api/"):
                self.assertEqual(401, pre_status, route)
            else:
                self.assertEqual(302, pre_status, route)

            asset_status, asset_headers, asset_body = _request(
                self.port, "GET", route, headers={
                    "Host": PUBLIC_HOST,
                    "Cookie": login["session"],
                })
            self.assertEqual(200, asset_status, route)
            self.assertEqual("no-store", _header(asset_headers, "Cache-Control"))
            self.assertIn("'self'",
                          _header(asset_headers, "Content-Security-Policy"))
            content_type = (_header(asset_headers, "Content-Type") or "").lower()
            pending.update(self._asset_references(route, asset_body, content_type))

        self.assertTrue({"/api/bootstrap.js", "/remote-client.js",
                         "/three-viz.js", "/line-workers.js"}.issubset(seen))

    def _asset_references(self, parent_route, body, content_type):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return ()
        refs = []
        if "html" in content_type:
            refs.extend(re.findall(
                r"\b(?:src|href)=[\"']([^\"']+)[\"']", text, flags=re.I))
            for source in re.findall(
                    r"<script\b[^>]*type=[\"']importmap[\"'][^>]*>(.*?)"
                    r"</script\s*>", text, flags=re.I | re.S):
                try:
                    mapping = json.loads(source)
                except ValueError as exc:
                    raise AssertionError("invalid served import map") from exc
                imports = mapping.get("imports", {})
                if isinstance(imports, dict):
                    self._import_map.update(
                        (key, value) for key, value in imports.items()
                        if isinstance(key, str) and isinstance(value, str))
                stack = [mapping]
                while stack:
                    value = stack.pop()
                    if isinstance(value, dict):
                        stack.extend(value.values())
                    elif isinstance(value, list):
                        stack.extend(value)
                    elif isinstance(value, str):
                        refs.append(value)
        if "javascript" in content_type:
            patterns = (
                r"\bfrom\s*[\"']([^\"']+)[\"']",
                r"\bimport\s*[\"']([^\"']+)[\"']",
                r"\bimport\s*\(\s*[\"']([^\"']+)[\"']",
                r"\bnew\s+(?:Worker|URL)\s*\(\s*[\"']([^\"']+)[\"']",
            )
            for pattern in patterns:
                refs.extend(re.findall(pattern, text))
        if "css" in content_type:
            refs.extend(re.findall(
                r"url\(\s*[\"']?([^\"')]+)", text, flags=re.I))

        found = set()
        for ref in refs:
            route = self._normalize_asset(parent_route, ref)
            if route is not None:
                found.add(route)
        return found

    def _normalize_asset(self, parent_route, ref):
        ref = ref.strip()
        if not ref or ref.startswith(("data:", "#", "http:", "https:", "//")):
            return None
        if not ref.startswith(("/", "./", "../")):
            mapped = self._import_map.get(ref)
            if mapped is None:
                prefixes = [key for key in self._import_map
                            if key.endswith("/") and ref.startswith(key)]
                if prefixes:
                    prefix = max(prefixes, key=len)
                    mapped = self._import_map[prefix] + ref[len(prefix):]
            if mapped is None:
                if posixpath.splitext(ref)[1].lower() not in rg.STATIC_EXT:
                    return None
                ref = "./" + ref
            else:
                if mapped == ref:
                    return None
                ref = mapped
        self.assertNotIn("?", ref, "version/query references cannot be served")
        self.assertNotIn("#", ref, "fragment references cannot be served")
        if ref.startswith("/"):
            route = posixpath.normpath(ref)
        elif ref.startswith(("./", "../")):
            route = posixpath.normpath(
                posixpath.join(posixpath.dirname(parent_route), ref))
            if not route.startswith("/"):
                route = "/" + route
        else:
            return None
        extension = posixpath.splitext(route)[1].lower()
        if route == "/api/bootstrap.js" or extension in rg.STATIC_EXT:
            return route
        return None


class UpstreamAndDemoTests(unittest.TestCase):
    def test_upstream_json_rejects_nonfinite_and_excessive_nesting(self):
        valid = rg.parse_upstream_json(
            b'{"nested":[1,2.5,true,null,{"value":-3.25}]}')
        self.assertEqual(-3.25, valid["nested"][4]["value"])
        bad_values = (
            b'{"value":NaN}',
            b'{"value":Infinity}',
            b'{"value":-Infinity}',
            b'{"value":1e400}',
            b'{"value":-1e400}',
            b'{"nested":[{"value":1e400}]}',
        )
        for raw in bad_values:
            with self.assertRaises(rg.UpstreamError) as caught:
                rg.parse_upstream_json(raw)
            self.assertEqual("upstream_bad_json", caught.exception.code)

        too_deep = (b'{"value":' + (b"[" * (rg.UPSTREAM_MAX_JSON_DEPTH + 1)) +
                    b"0" + (b"]" * (rg.UPSTREAM_MAX_JSON_DEPTH + 1)) + b"}")
        with self.assertRaises(rg.UpstreamError) as caught:
            rg.parse_upstream_json(too_deep)
        self.assertEqual("upstream_bad_json", caught.exception.code)

    def test_default_demo_build_has_zero_upstream_native_or_key_io(self):
        calls = []

        def no_fetch(url):
            calls.append(url)
            raise AssertionError("unexpected fetch")

        poison = {
            "JEV_API_KEY": "POISON-NOT-A-KEY",
            "OPENAI_API_KEY": "POISON-NOT-A-KEY",
            "ANTHROPIC_API_KEY": "POISON-NOT-A-KEY",
            "BINANCE_API_KEY": "POISON-NOT-A-KEY",
        }
        config = rg.GatewayConfig(PUBLIC_ORIGIN, AUTH_STORE, port=0)
        with mock.patch.dict(os.environ, poison, clear=False), \
                mock.patch.object(rg.urllib.request, "build_opener",
                                  side_effect=AssertionError("HTTP disabled")), \
                mock.patch.object(socket, "create_connection",
                                  side_effect=AssertionError("network disabled")), \
                mock.patch.object(subprocess, "Popen",
                                  side_effect=AssertionError("native disabled")):
            httpd, gateway = rg.build_server(
                config, webroot=WEBROOT, password_store=FakePasswordStore(),
                fetcher=no_fetch)
            try:
                self.assertTrue(config.demo)
                self.assertEqual("DEMO", config.mode)
                self.assertIsNone(config.upstream)
                self.assertIsNone(config.companion)
                self.assertIsInstance(gateway.demo_data("/api/status"), dict)
                self.assertIsInstance(gateway.demo_data("/api/guild/status"), dict)
                self.assertIsInstance(gateway.demo_data("/api/health"), dict)
                bootstrap = gateway.bootstrap_js()
                self.assertIn("network_allowed:false", bootstrap)
                self.assertIn('window.__JEV_NATIVE_ORIGIN__=""', bootstrap)
            finally:
                httpd.server_close()
        self.assertEqual([], calls)

    def test_real_mode_companion_health_mapping_and_post_denial(self):
        fetched = []

        def fetcher(url):
            fetched.append(url)
            return b'{"ok":true,"nested":[false,null,1.5]}'

        config = rg.GatewayConfig(
            PUBLIC_ORIGIN, AUTH_STORE, port=0,
            upstream=rg.ALLOWED_UPSTREAM, companion=rg.ALLOWED_COMPANION)
        httpd, gateway = rg.build_server(
            config, webroot=WEBROOT, password_store=FakePasswordStore(),
            fetcher=fetcher)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            login = _login(gateway.bound_port)
            status, _headers, body = _request(
                gateway.bound_port, "GET", "/api/guild/health", headers={
                    "Host": PUBLIC_HOST,
                    "Cookie": login["session"],
                })
            self.assertEqual(200, status)
            result = json.loads(body.decode("utf-8"))
            self.assertTrue(result["remote_read_only"])
            self.assertEqual("READ_ONLY_REAL", result["remote_mode"])
            self.assertEqual([rg.ALLOWED_COMPANION + "/api/health"], fetched)

            post_headers = {
                "Host": PUBLIC_HOST,
                "Origin": PUBLIC_ORIGIN,
                "Content-Type": "application/json",
                "X-JEV-CSRF": login["csrf"],
                "Cookie": login["session"],
            }
            self.assertEqual(403, _request(
                gateway.bound_port, "POST", "/api/control",
                headers=post_headers, body=b'{"action":"pause_new"}')[0])
            self.assertEqual(404, _request(
                gateway.bound_port, "POST", "/api/order",
                headers=post_headers, body=b"{}")[0])
            self.assertEqual([rg.ALLOWED_COMPANION + "/api/health"], fetched)
        finally:
            _shutdown(httpd, thread)

    def test_upstream_nonfinite_http_error_is_fixed_and_bounded(self):
        config = rg.GatewayConfig(
            PUBLIC_ORIGIN, AUTH_STORE, port=0,
            upstream=rg.ALLOWED_UPSTREAM, companion=rg.ALLOWED_COMPANION)
        httpd, gateway = rg.build_server(
            config, webroot=WEBROOT, password_store=FakePasswordStore(),
            fetcher=lambda _url: b'{"secret_provider_value":1e400}')
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            login = _login(gateway.bound_port)
            status, headers, body = _request(
                gateway.bound_port, "GET", "/api/status", headers={
                    "Host": PUBLIC_HOST,
                    "Cookie": login["session"],
                })
            self.assertEqual(502, status)
            self.assertEqual("no-store", _header(headers, "Cache-Control"))
            result = json.loads(body.decode("utf-8"))
            self.assertEqual("upstream_bad_json", result["reason"])
            self.assertNotIn(b"secret_provider_value", body)
            self.assertNotIn(b"Infinity", body)
        finally:
            _shutdown(httpd, thread)


if __name__ == "__main__":
    unittest.main()
