# -*- coding: utf-8 -*-
"""Focused tests for the loopback-only, one-shot owner setup server."""
import http.client
import json
import os
import re
import tempfile
import threading
import unittest

from scanner import remote_gateway as rg
from scanner import remote_setup as rs


AUTH_STORE = os.path.join(tempfile.gettempdir(), "jev-remote-setup-fixture-owner.json")
FIXTURE_USERNAME = "fixture-owner"
FIXTURE_PASSWORD = "fixture-password-not-a-real-credential"


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeOwnerStore:
    def __init__(self):
        self.create_calls = 0
        self.last_username = None

    def exists(self):
        return False

    def create(self, username, _password):
        self.create_calls += 1
        self.last_username = username
        if self.create_calls > 1:
            raise AssertionError("owner store written more than once")


def _request(port, method, target, headers=None, body=None):
    conn = http.client.HTTPConnection(rg.BIND_HOST, port, timeout=4)
    try:
        conn.request(method, target, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        return response.status, response.getheaders(), data
    finally:
        conn.close()


def _header(headers, name):
    wanted = name.lower()
    values = [value for key, value in headers if key.lower() == wanted]
    return values[-1] if values else None


def _csrf_from_page(body):
    match = re.search(
        br'<meta\s+name="jev-setup-csrf"\s+content="([^"]+)">', body)
    if not match:
        raise AssertionError("setup CSRF meta element missing")
    return match.group(1).decode("ascii")


class OwnerSetupCSRFTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.store = FakeOwnerStore()
        self.setup = rs.OwnerSetup(
            AUTH_STORE, port=0, store=self.store, clock=self.clock)

    def test_nonascii_surrogate_bytes_and_malformed_tokens_are_safe_false(self):
        token = self.setup.issue_csrf()
        malformed = (
            "é" * rs.SETUP_CSRF_TOKEN_CHARS,
            "\ud800" * rs.SETUP_CSRF_TOKEN_CHARS,
            "!" * rs.SETUP_CSRF_TOKEN_CHARS,
            "a",
            b"a" * rs.SETUP_CSRF_TOKEN_CHARS,
            None,
        )
        for presented in malformed:
            self.assertFalse(self.setup.consume_csrf(presented))
        self.assertTrue(self.setup.consume_csrf(token))
        self.assertFalse(self.setup.consume_csrf(token))

    def test_token_is_one_use_bounded_and_expires(self):
        first = self.setup.issue_csrf()
        self.clock.value += rs.SETUP_CSRF_TTL_SECONDS
        self.assertFalse(self.setup.consume_csrf(first))

        tokens = [self.setup.issue_csrf()
                  for _index in range(rs.MAX_PENDING_CSRF + 1)]
        self.assertFalse(self.setup.consume_csrf(tokens[0]))
        self.assertTrue(self.setup.consume_csrf(tokens[-1]))
        self.assertFalse(self.setup.consume_csrf(tokens[-1]))

    def test_page_uses_eight_character_accessible_local_validation(self):
        body, hashes = self.setup.page()
        page = body.decode("utf-8")

        self.assertIn('<form id="f" autocomplete="off" novalidate>', page)
        self.assertIn('autocomplete="username"', page)
        self.assertEqual(2, page.count(
            'autocomplete="new-password"'))
        self.assertEqual(2, page.count(
            'minlength="8" maxlength="128"'))
        self.assertIn("input{width:100%;min-height:44px", page)
        self.assertIn("button{margin-top:18px;width:100%;min-height:44px", page)
        self.assertGreaterEqual(page.count("font-size:16px"), 2)
        self.assertIn("密碼可用 8 碼以上、好記短句或密碼管理員，不必用難記亂碼", page)

        self.assertIn("return Array.from(value).length;", page)
        self.assertIn('aria-describedby="password-help password-count"', page)
        self.assertIn('<div id="password-count" aria-live="polite"></div>', page)
        self.assertNotIn("console.", page)
        self.assertNotIn("localStorage", page)

        username_check = page.index("if(form.u.value.length===0)")
        length_check = page.index("if(length<minimum||length>maximum)")
        confirmation_check = page.index("if(form.p.value!==form.c.value)")
        request = page.index('fetch("/api/setup/owner"')
        self.assertLess(username_check, request)
        self.assertLess(length_check, request)
        self.assertLess(confirmation_check, request)
        self.assertIn('form.p.value=""', page)
        self.assertIn('form.c.value=""', page)
        self.assertIn('<a id="retry" href="/" hidden>', page)

        self.assertIn("Object.freeze({", page)
        self.assertIn("safeServerMessage(pair[1])", page)
        self.assertNotIn('pair[1].reason||', page)
        self.assertNotIn('"建立失敗："+', page)
        self.assertNotIn("Google", page)
        self.assertEqual(hashes, rg.script_hashes(page))


class OwnerSetupHTTPTests(unittest.TestCase):
    def test_loopback_host_exact_origin_csrf_store_once_and_shutdown(self):
        store = FakeOwnerStore()
        clock = FakeClock()
        httpd, setup = rs.build_server(
            AUTH_STORE, port=0, store=store, clock=clock)
        port = setup.bound_port
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host = "127.0.0.1:%d" % port
        origin = "http://127.0.0.1:%d" % port
        payload = json.dumps({
            "username": FIXTURE_USERNAME,
            "password": FIXTURE_PASSWORD,
            "confirm": FIXTURE_PASSWORD,
        }).encode("utf-8")
        try:
            status, _headers, _body = _request(
                port, "GET", "/", headers={"Host": "public.example.invalid"})
            self.assertEqual(403, status)

            status, headers, body = _request(
                port, "GET", "/", headers={"Host": host})
            self.assertEqual(200, status)
            self.assertEqual("no-store", _header(headers, "Cache-Control"))
            csp = _header(headers, "Content-Security-Policy")
            script_rule = next(item.strip() for item in csp.split(";")
                               if item.strip().startswith("script-src "))
            self.assertIn("'self'", script_rule)
            self.assertIn("'sha256-", script_rule)
            self.assertNotIn("'unsafe-inline'", script_rule)
            token = _csrf_from_page(body)

            common = {
                "Host": host,
                "Content-Type": "application/json",
                "X-JEV-CSRF": token,
            }
            wrong_origin = dict(common)
            wrong_origin["Origin"] = origin.upper()
            status, _headers, _body = _request(
                port, "POST", rs.SETUP_POST_ROUTE,
                headers=wrong_origin, body=payload)
            self.assertEqual(403, status)
            self.assertEqual(0, store.create_calls)

            nonascii_csrf = dict(common)
            nonascii_csrf["Origin"] = origin
            nonascii_csrf["X-JEV-CSRF"] = "é" * rs.SETUP_CSRF_TOKEN_CHARS
            status, _headers, _body = _request(
                port, "POST", rs.SETUP_POST_ROUTE,
                headers=nonascii_csrf, body=payload)
            self.assertEqual(403, status)
            self.assertEqual(0, store.create_calls)

            valid = dict(common)
            valid["Origin"] = origin
            status, headers, body = _request(
                port, "POST", rs.SETUP_POST_ROUTE,
                headers=valid, body=payload)
            self.assertEqual(200, status)
            self.assertEqual("no-store", _header(headers, "Cache-Control"))
            self.assertEqual({"ok": True, "created": True,
                              "note": "owner record stored as hashes only"},
                             json.loads(body.decode("utf-8")))
            self.assertEqual(1, store.create_calls)
            self.assertEqual(FIXTURE_USERNAME, store.last_username)
            self.assertTrue(setup.completed)

            thread.join(timeout=4)
            self.assertFalse(thread.is_alive(), "setup server did not shut down")
            self.assertEqual((False, "already_completed"),
                             setup.create(FIXTURE_USERNAME,
                                          FIXTURE_PASSWORD,
                                          FIXTURE_PASSWORD))
            self.assertEqual(1, store.create_calls)
        finally:
            if thread.is_alive():
                httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=4)


if __name__ == "__main__":
    unittest.main()
