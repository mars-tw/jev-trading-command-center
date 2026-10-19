"""Offline release gates. Fixtures never contact a provider or start a model CLI."""
import copy
import contextlib
import importlib
import io
import json
import math
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
import http.client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scanner import jev_client as jc, jev_decision as jd, multi_review as mr
from scanner.demo_engine import DemoEngine
from scanner import demo_server
from scanner.config import DEFAULTS
from scanner.plans import make_plan
import run_app
import run_workguild


class TypedDecisionTests(unittest.TestCase):
    def setUp(self):
        self.plan = make_plan(1, 'DEMO1USDT', 'long', {'type': 'fixture'}, 'SNAP-TEST', 1000, [])
        self.options = jd.build_options(self.plan)
        self.action = self.options[0]['id']
        self.response = {'ok': True, 'choice': self.action,
                         'probabilities': {self.action: .8, 'WAIT': .2}, 'confidence': .75}

    def body(self, response):
        return {'answers': {'final_decision': response}, 'model': 'fixture'}

    def test_valid_action_wait_and_single_use(self):
        response = jc.parse_choice_body(self.body(self.response), self.options)
        response['ok'] = True
        receipt = jd.evaluate_response(response, self.options, self.plan, 100)
        self.assertTrue(receipt['can_execute'])
        self.assertTrue(jd.consume_receipt(receipt, 'fixture-intent', 101)[0])
        self.assertFalse(jd.consume_receipt(receipt, 'again', 102)[0])
        response.update(choice='WAIT', probabilities={self.action: .2, 'WAIT': .8})
        receipt = jd.evaluate_response(response, self.options, self.plan, 100)
        self.assertEqual(receipt['status'], 'JEV_WAIT')
        self.assertFalse(receipt['can_execute'])

    def test_malformed_numeric_probabilities_never_authorize(self):
        for bad in (True, False, math.nan, math.inf, -math.inf, -.1, 1.1, '0.8', None, {}, []):
            with self.subTest(value_type=type(bad).__name__):
                value = copy.deepcopy(self.response)
                value['probabilities'][self.action] = bad
                self.assertIsNone(jc.parse_choice_body(self.body(value), self.options))
                self.assertFalse(jd.validate_choice(value['choice'], value['probabilities'], self.options)[0])
                receipt = jd.evaluate_response(value, self.options, self.plan, 100)
                self.assertEqual(receipt['status'], 'NO_JEV_DECISION')
                self.assertFalse(receipt['can_execute'])

    def test_malformed_confidence_and_nonobjects_fail_closed(self):
        for bad in (True, False, math.nan, math.inf, -.1, 1.1, '0.8', None, {}, []):
            value = {**self.response, 'confidence': bad}
            self.assertIsNone(jc.parse_choice_body(self.body(value), self.options))
            self.assertFalse(jd.evaluate_response(value, self.options, self.plan, 100)['can_execute'])
        for bad in (None, True, [], 'text', 42):
            self.assertIsNone(jc.parse_choice_body(bad, self.options))
            self.assertFalse(jd.evaluate_response(bad, self.options, self.plan, 100)['can_execute'])

    def test_keyset_sum_unknown_choice_and_argmax(self):
        for choice, probs in (('OTHER', {self.action: .8, 'WAIT': .2}),
                              (self.action, {self.action: .8}),
                              (self.action, {self.action: .8, 'WAIT': .5}),
                              (self.action, {self.action: .2, 'WAIT': .8})):
            value = {**self.response, 'choice': choice, 'probabilities': probs}
            self.assertIsNone(jc.parse_choice_body(self.body(value), self.options))
            self.assertFalse(jd.evaluate_response(value, self.options, self.plan, 100)['can_execute'])

    def test_missing_key_never_opens_transport(self):
        with mock.patch.object(jc, '_open_request', side_effect=AssertionError('network')):
            out = jc.decide_choice('', 'fixture', {}, self.options)
        self.assertFalse(out['ok'])

    def test_fixed_https_no_proxy_and_no_redirect(self):
        request = urllib.request.Request(jc.API_URL)
        opener = mock.Mock()
        with mock.patch.object(jc.urllib.request, 'build_opener', return_value=opener) as build:
            jc._open_request(request, 3)
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], jc.RejectRedirect)
        with self.assertRaises(urllib.error.HTTPError):
            handlers[1].redirect_request(request, None, 302, 'redirect', {}, 'https://example.invalid/')
        with self.assertRaises(urllib.error.URLError):
            jc._open_request(urllib.request.Request('https://example.invalid/'), 3)


class DefaultBoundaryTests(unittest.TestCase):
    def test_all_external_integrations_default_off(self):
        for key in ('external_analysis_enabled', 'analysis_carousel', 'local_decider_enabled'):
            self.assertIs(DEFAULTS[key], False)
        self.assertTrue(DEFAULTS['use_risk_control'])
        with mock.patch.object(mr.agent_cli, 'NativeCliRunner', side_effect=AssertionError('native')):
            self.assertEqual(mr.review({})['source'], 'DISABLED')

    def test_native_runner_requires_flag_before_any_probe(self):
        with mock.patch.object(run_workguild, 'probe_existing', side_effect=AssertionError('probe')), \
             mock.patch.object(run_workguild, 'port_in_use', side_effect=AssertionError('bind')), \
             mock.patch.object(run_workguild.agent_cli, 'NativeCliRunner', side_effect=AssertionError('native')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_workguild.main([]), 2)

    def test_default_and_compat_entries_are_demo_without_outbound_or_subprocess(self):
        import run_scanner
        with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network')), \
             mock.patch.object(urllib.request, 'urlopen', side_effect=AssertionError('network')), \
             mock.patch('subprocess.Popen', side_effect=AssertionError('child')), \
             mock.patch.dict(os.environ, {'TYPESAFE_API_KEY': 'DO_NOT_READ_FIXTURE_ONLY'}), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run_app.main(['--check']), 0)
            self.assertEqual(run_scanner.main(['--once']), 0)
        self.assertIn('native_cli_count=0', out.getvalue())
        self.assertIn('network_allowed=False', out.getvalue())

    def test_real_paper_missing_env_fails_before_engine_import(self):
        original_import = __import__
        def guarded_import(name, *args, **kwargs):
            if name in ('scanner.engine', 'requests', 'scanner.binance_client'):
                raise AssertionError('real-mode dependency imported')
            return original_import(name, *args, **kwargs)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch('builtins.__import__', side_effect=guarded_import), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_app.main(['--real-paper']), 2)

    def test_only_public_numeric_features_reach_optional_cli(self):
        packet = {'symbol': 'DEMO1USDT', 'rule': {'direction': 'long', 'score': .5},
                  'orderbook': {'spread_bps': 2, 'note': 'PRIVATE_FIXTURE'},
                  'account': {'equity': 'PRIVATE_FIXTURE'},
                  'jev_first_opinion': {'raw': 'PRIVATE_FIXTURE'},
                  'kelly': {'state': 'PRIVATE_FIXTURE'}, 'secret': 'PRIVATE_FIXTURE'}
        public = mr._public_packet(packet)
        self.assertEqual(set(public), {'symbol', 'candidate_direction', 'features'})
        self.assertNotIn('PRIVATE_FIXTURE', json.dumps(public))

    def test_public_http_disables_ambient_credentials(self):
        try:
            from scanner.binance_client import BinancePublicClient
        except ImportError:
            self.skipTest('optional requests dependency is not installed')
        client = BinancePublicClient()
        try:
            self.assertFalse(client.session.trust_env)
        finally:
            client.session.close()

    def test_engine_decision_path_respects_external_opt_in(self):
        try:
            from scanner.engine import ScanEngine
        except ImportError:
            self.skipTest('optional requests dependency is not installed')
        engine = ScanEngine.__new__(ScanEngine)
        engine.cfg = {'external_analysis_enabled': False}
        with mock.patch.object(mr, 'review', side_effect=AssertionError('native')), \
             mock.patch.object(engine, '_fresh_analysis', side_effect=AssertionError('cache')):
            self.assertEqual(engine._analysis_evidence('DEMO1USDT', {})['source'], 'DISABLED')


class DemoTests(unittest.TestCase):
    def test_seed_and_all_initial_scenarios_are_deterministic(self):
        clock = lambda: 1000
        for scenario, status in (('success', 'JEV_ACTION_SELECTED'), ('wait', 'JEV_WAIT'),
                                 ('risk_reject', 'RISK_REJECTED')):
            a = DemoEngine(clock=clock, seed=1, scenario=scenario)
            b = DemoEngine(clock=clock, seed=1, scenario=scenario)
            self.assertEqual(a.status(), b.status())
            self.assertEqual(a.status()['decisions'][0]['status'], status)
            self.assertEqual(a.health()['real_cli_agents'], 0)

    def test_demo_payload_never_claims_real_processes(self):
        engine = DemoEngine(clock=lambda: 1000)
        health, guild = engine.health(), engine.guild()
        self.assertEqual(guild['source'], 'DEMO')
        self.assertTrue(guild['simulated'])
        self.assertEqual(health['native_cli_count'], 0)
        self.assertFalse(health['key_env_read'])
        self.assertFalse(health['jev_api_called'])
        self.assertTrue(all(w['pid'] is None and w['process_alive'] is False for w in guild['workers']))


class DemoHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.engine = demo_server.build(str(ROOT), 0, DemoEngine(scenario='wait'))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        hdr = dict(headers or {})
        if method == 'POST':
            hdr.setdefault('Origin', 'http://127.0.0.1:%d' % self.port)
            hdr.setdefault('Content-Type', 'application/json')
        conn.request(method, path, body=body, headers=hdr)
        response = conn.getresponse()
        raw = response.read()
        value = (response.status, dict(response.getheaders()), raw)
        conn.close()
        return value

    def test_cold_http_full_ui_vendor_and_self_only_csp(self):
        for path in ('/', '/stage.html', '/api/status', '/api/guild/status', '/api/health',
                     '/api/bootstrap.js', '/vendor/three/build/three.module.js'):
            code, headers, data = self.request('GET', path)
            self.assertEqual(code, 200, path)
            self.assertIn("connect-src 'self';", headers['Content-Security-Policy'])
            self.assertIn("script-src 'self' 'sha256-", headers['Content-Security-Policy'])
            self.assertNotIn("script-src 'self' 'unsafe-inline'", headers['Content-Security-Policy'])
        self.assertIn(b'__JEV_DEMO__=true', self.request('GET', '/api/bootstrap.js')[2])

    def test_host_origin_type_size_shape_and_route_guards(self):
        cases = [('/api/control', '{"action":"pause_new"}', {'Origin': 'https://example.invalid'}, 403),
                 ('/api/control', '{}', {'Content-Type': 'text/plain'}, 415),
                 ('/api/control', '[]', {}, 400),
                 ('/api/control', '{"action":true}', {}, 400),
                 ('/api/control', '{"action":"pause_new","extra":1}', {}, 400),
                 ('/api/control', '{"action":NaN}', {}, 400),
                 ('/api/control', 'x' * 4097, {}, 413),
                 ('/api/control-unrecognized', '{}', {}, 404)]
        for path, body, headers, expected in cases:
            self.assertEqual(self.request('POST', path, body, headers)[0], expected)
        self.assertEqual(self.request('GET', '/api/status', headers={'Host': 'example.invalid'})[0], 403)
        for path in ('/../README.md', '/%2e%2e/README.md', '/.env.example', '/api/status-unknown'):
            self.assertEqual(self.request('GET', path)[0], 404)

    def test_valid_controls_change_only_explicit_synthetic_state(self):
        for action, paused in (('pause_new', True), ('resume', False)):
            code, _, body = self.request('POST', '/api/control', json.dumps({'action': action}))
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)['source'], 'DEMO')
            self.assertEqual(self.engine.status()['mode']['paused_new'], paused)
        for action, paused in (('pause', True), ('resume', False)):
            self.assertEqual(self.request('POST', '/api/guild/control', json.dumps({'action': action}))[0], 200)
            self.assertEqual(self.engine.guild_paused, paused)
        self.assertFalse(self.engine.health()['jev_api_called'])


if __name__ == '__main__':
    unittest.main()
