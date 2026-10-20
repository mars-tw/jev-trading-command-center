# -*- coding: utf-8 -*-
"""Focused regression tests for the hash-only remote authentication core."""
import hashlib
import os
import tempfile
import unittest
from unittest import mock

from scanner import remote_auth

try:
    from scanner import remote_acl
except ImportError:  # pragma: no cover - non-Windows packaging fallback
    remote_acl = None


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class RemoteAuthTests(unittest.TestCase):
    """Every store is synthetic and lives under this test's retained temp path."""

    def setUp(self):
        self.temp_root = tempfile.mkdtemp(prefix="jev-auth-regression-")
        self.kdf_calls = []

    def fake_kdf(self, password, salt, params):
        self.kdf_calls.append((password, bytes(salt), dict(params)))
        raw = password.encode("utf-8")
        return hashlib.sha256(bytes(salt) + raw).digest()

    def store(self, leaf="private", real_kdf=False):
        path = os.path.join(self.temp_root, leaf, "owner.json")
        return remote_auth.PasswordStore(path, kdf=None if real_kdf else self.fake_kdf)

    def assert_store_error(self, code, callback):
        with self.assertRaises(remote_auth.StoreError) as raised:
            callback()
        self.assertEqual(code, raised.exception.code)

    def test_password_policy_boundaries_with_fake_kdf_stores(self):
        cases = (
            ("1" * 7, "password_length"),
            ("1" * 8, None),
            ("1" * 128, None),
            ("1" * 129, "password_length"),
        )
        for password, expected in cases:
            with self.subTest(length=len(password)):
                self.assertEqual(
                    expected, remote_auth.password_policy_error(password))

        for length in (8, 128):
            password = "1" * length
            store = self.store(leaf="boundary-accept-%d" % length)
            store.create("owner", password)
            self.assertTrue(store.verify("owner", password))

        for length in (7, 129):
            store = self.store(leaf="boundary-reject-%d" % length)
            self.assert_store_error(
                "password_length",
                lambda store=store, length=length:
                    store.create("owner", "1" * length),
            )
            self.assertFalse(store.exists())

    def test_dummy_password_cannot_bypass_input_validity(self):
        store = self.store()
        password = remote_auth.PasswordStore._DUMMY_PASSWORD
        record = store.create("owner", password)
        self.assertEqual(remote_auth.default_kdf_params(), record["kdf"])
        self.assertTrue(store.verify("owner", password))
        for invalid in ("", "short", None, "\ud800" * 15):
            with self.subTest(invalid=repr(invalid)):
                self.assertFalse(store.verify("owner", invalid))

    def test_unknown_and_invalid_username_use_the_kdf_and_are_denied(self):
        store = self.store()
        password = "synthetic-owner-password-2026"
        store.create("owner", password)
        self.kdf_calls.clear()
        self.assertFalse(store.verify("another.owner", password))
        unknown_call = self.kdf_calls[-1]
        self.assertFalse(store.verify("!", password))
        invalid_call = self.kdf_calls[-1]
        self.assertEqual(2, len(self.kdf_calls))
        self.assertEqual(unknown_call[0], invalid_call[0])
        self.assertEqual(unknown_call[1:], invalid_call[1:])

    def test_malformed_unicode_and_non_ascii_csrf_fail_closed(self):
        malformed = "\ud800" * remote_auth.PASSWORD_MIN_CHARS
        self.assertEqual("password_bytes", remote_auth.password_policy_error(malformed))
        self.assertIsInstance(remote_auth.token_key(None), str)
        sessions = remote_auth.SessionStore()
        token, _csrf = sessions.create()
        record = sessions.validate(token)
        self.assertFalse(sessions.csrf_ok(record, "é"))
        self.assertFalse(sessions.csrf_ok(record, "\ud800"))
        self.assertIsNone(sessions.validate("\ud800"))
        preauth = remote_auth.PreauthStore()
        pre_token, _pre_csrf = preauth.issue()
        self.assertFalse(preauth.consume(pre_token, "é"))
        self.assertFalse(preauth.consume(pre_token, _pre_csrf))

    def test_real_cost_eight_digit_owner_roundtrip(self):
        store = self.store(leaf="real-cost", real_kdf=True)
        password = "12345678"
        store.create("owner", password)
        self.assertTrue(store.verify("OWNER", password))

    def test_exclusive_create_never_overwrites(self):
        store = self.store()
        store.create("owner", "synthetic-first-password-2026")
        before = store.read()
        self.assert_store_error(
            "auth_store_exists",
            lambda: store.create("owner", "synthetic-second-password-2026"),
        )
        self.assertEqual(before, store.read())

    def test_short_writes_are_completed_fsynced_and_verified(self):
        store = self.store(leaf="short-write")
        original_write = os.write
        original_fsync = os.fsync

        def short_write(descriptor, data):
            return original_write(descriptor, data[:max(1, min(7, len(data)))])

        with mock.patch.object(remote_auth.os, "write", side_effect=short_write), \
                mock.patch.object(remote_auth.os, "fsync", wraps=original_fsync) as fsync:
            store.create("owner", "synthetic-short-write-password")
        self.assertTrue(fsync.called)
        self.assertTrue(store.verify("owner", "synthetic-short-write-password"))

    def test_zero_write_and_lying_write_never_report_success(self):
        zero_store = self.store(leaf="zero-write")
        with mock.patch.object(remote_auth.os, "write", return_value=0):
            self.assert_store_error(
                "auth_store_unwritable",
                lambda: zero_store.create("owner", "synthetic-zero-write-password"),
            )
        self.assertTrue(zero_store.exists())
        with self.assertRaises(remote_auth.StoreError):
            zero_store.read()

        lying_store = self.store(leaf="lying-write")
        with mock.patch.object(remote_auth.os, "write", side_effect=lambda _fd, data: len(data)):
            self.assert_store_error(
                "auth_store_verify_failed",
                lambda: lying_store.create("owner", "synthetic-lying-write-password"),
            )
        self.assertTrue(lying_store.exists())

    def test_fsync_failure_is_not_success(self):
        store = self.store(leaf="fsync-failure")
        with mock.patch.object(remote_auth.os, "fsync", side_effect=OSError("synthetic")):
            self.assert_store_error(
                "auth_store_unwritable",
                lambda: store.create("owner", "synthetic-fsync-failure-password"),
            )
        self.assertTrue(store.exists())

    @unittest.skipUnless(os.name == "nt" and remote_acl is not None,
                         "Windows DACL regression")
    def test_windows_new_parent_and_store_are_private(self):
        store = self.store(leaf="windows-private")
        store.create("owner", "synthetic-windows-private-password")
        self.assertTrue(remote_acl.is_private(os.path.dirname(store.path)))
        self.assertTrue(remote_acl.is_private(store.path))
        self.assertEqual(store.read()["version"], remote_auth.STORE_VERSION)

    @unittest.skipUnless(os.name == "nt" and remote_acl is not None,
                         "Windows DACL regression")
    def test_windows_unsafe_existing_parent_is_rejected_unchanged(self):
        parent = os.path.join(self.temp_root, "windows-everyone")
        os.mkdir(parent)
        trustees = remote_acl._trustees((remote_acl.SID_EVERYONE,))
        self.assertTrue(remote_acl._apply_dacl(parent, trustees, inherit=True,
                                                protected=True, set_owner=True))
        self.assertFalse(remote_acl.is_private(parent))
        store = remote_auth.PasswordStore(os.path.join(parent, "owner.json"),
                                          kdf=self.fake_kdf)
        self.assert_store_error(
            "auth_store_parent_permissions",
            lambda: store.create("owner", "synthetic-unsafe-parent-password"),
        )
        self.assertFalse(remote_acl.is_private(parent))
        self.assertFalse(store.exists())

    def test_session_ttl_absolute_expiry_revocation_and_cap(self):
        clock = Clock()
        sessions = remote_auth.SessionStore(clock=clock, idle_seconds=5,
                                            absolute_seconds=12, max_sessions=2)
        first, _ = sessions.create()
        second, _ = sessions.create()
        third, _ = sessions.create()
        self.assertIsNone(sessions.validate(first))
        self.assertIsNotNone(sessions.validate(second))
        self.assertTrue(sessions.revoke(second))
        self.assertFalse(sessions.revoke(second))
        clock.now = 5
        self.assertIsNone(sessions.validate(third))

        absolute, _ = sessions.create()
        clock.now = 9
        self.assertIsNotNone(sessions.validate(absolute))
        clock.now = 13
        self.assertIsNotNone(sessions.validate(absolute))
        clock.now = 17
        self.assertIsNone(sessions.validate(absolute))

    def test_preauth_ttl_one_use_and_growth_caps(self):
        clock = Clock()
        preauth = remote_auth.PreauthStore(clock=clock, ttl_seconds=5, max_entries=2)
        oldest, oldest_csrf = preauth.issue()
        middle, middle_csrf = preauth.issue()
        newest, newest_csrf = preauth.issue()
        self.assertFalse(preauth.consume(oldest, oldest_csrf))
        self.assertTrue(preauth.consume(middle, middle_csrf))
        self.assertFalse(preauth.consume(middle, middle_csrf))
        clock.now = 5
        self.assertFalse(preauth.consume(newest, newest_csrf))
        self.assertEqual(0, preauth.pending())

        failures = remote_auth.FailureBuckets(clock=clock, max_scopes=2,
                                               global_limit=99, scope_limit=99)
        failures.record_failure(("one",))
        failures.record_failure(("two",))
        failures.record_failure(("three",))
        self.assertEqual(2, failures.tracked())


if __name__ == "__main__":
    unittest.main()
