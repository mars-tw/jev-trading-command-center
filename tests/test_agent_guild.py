"""Independent actual-module acceptance tests. No CLI, engine or source HTTP calls."""
import collections
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from scanner import agent_cli as cli
from scanner import agent_guild as g


class Handle:
    cleanup_warning = ""
    cleanup_method = "fake-no-files"
    next_pid = 10000

    def __init__(self):
        Handle.next_pid += 1
        self.pid, self.result, self.live = Handle.next_pid, None, True
        self.terminated = False

    def poll(self):
        if self.result is not None:
            self.live = False
        return self.result

    def alive(self):
        return self.live

    def terminate(self):
        self.terminated, self.live = True, False

    def close(self):
        self.live = False


class Runner:
    timeout_seconds = 150

    def __init__(self):
        self.started = []

    def discover(self, refresh=False):
        return {a: {"available": True, "model": "fake-unverified", "reason": ""}
                for a in ("CODEX", "CLAUDE", "GROK", "AGY")}

    def start(self, alias, job_id, prompt):
        handle = Handle()
        self.started.append((alias, job_id, prompt, handle))
        return handle


def snapshot():
    at = g.now_ms()
    return g.PublicSnapshot(True, symbols=[{"symbol": "BTCUSDT", "features": {"price": 100.0, "rule_score": 7.0}}],
                            snapshot_hash="clean-snapshot", scanned_at_ms=at, age_ms=0)


def good():
    return cli.Outcome("completed", verdict="support", finding="Public price is 100", evidence=["price=100"], exit_code=0)


class SchedulerAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_root = Path(tempfile.mkdtemp(prefix="workguild-test-fixtures-"))

    @classmethod
    def tearDownClass(cls):
        print("Retained owned SQLite fixtures for recycle-only cleanup: %s" % cls.fixture_root, file=sys.stderr)

    def setUp(self):
        # Retained owned fixtures; recycle the exact fixture_root reported after this suite.
        self.folder = Path(tempfile.mkdtemp(prefix="audit-", dir=str(self.fixture_root)))
        self.path = self.folder / "guild.sqlite3"
        self.ledger = g.GuildLedger(str(self.path))
        self.runner = Runner()
        self.scheduler = g.GuildScheduler(self.ledger, self.runner, fetcher=snapshot)

    def tearDown(self):
        self.scheduler.shutdown()
        self.ledger.close()

    def job(self, suffix="one", lane="market"):
        return {"id": "audit-" + suffix, "lane_id": lane, "alias": "CODEX", "symbol": "BTCUSDT",
                "snapshot_hash": "safe", "dedup_key": "dedup-" + suffix, "queued_at_ms": g.now_ms(),
                "features": {"price": 100.0}, "scanned_at_ms": g.now_ms(), "cycle": 1}

    def test_initial_eight_real_owned_handles_and_lane_fairness(self):
        self.scheduler.tick(force_cycle=True)
        state = self.scheduler.status()
        live = [w for w in state["workers"] if w["status"] == "running"]
        self.assertEqual(len(live), 8)
        self.assertTrue(all(w["process_alive"] and w["pid"] > 0 for w in live))
        self.assertGreaterEqual(len({w["lane_id"] for w in live}), 3)
        self.assertTrue(all(n <= 3 for n in collections.Counter(w["alias"] for w in live).values()))
        self.assertEqual(len(self.runner.started), 8)

    def test_exactly_twelve_fixed_worker_ids(self):
        workers = self.scheduler.status()["workers"]
        expected = {"%s-%d" % (a, n) for a in ("CODEX", "CLAUDE", "GROK", "AGY") for n in (1, 2, 3)}
        self.assertEqual({w["worker_id"] for w in workers}, expected)

    def test_capacity_four_twelve_and_lowering_preserves_owned_children(self):
        self.scheduler.set_capacity(4)
        self.scheduler.tick(force_cycle=True)
        self.assertEqual(self.scheduler.status()["scheduler"]["active"], 4)
        self.scheduler.set_capacity(12)
        self.scheduler.tick()
        state = self.scheduler.status()
        self.assertEqual(state["scheduler"]["active"], 12)
        self.assertTrue(all(n <= 3 for n in collections.Counter(w["alias"] for w in state["workers"] if w["status"] == "running").values()))
        self.scheduler.set_capacity(4)
        self.scheduler.tick()
        self.assertEqual(self.scheduler.status()["scheduler"]["active"], 12)
        self.assertTrue(all(not h.terminated for _, _, _, h in self.runner.started))

    def test_pause_drains_owned_work_without_termination_or_new_launch(self):
        self.scheduler.tick(force_cycle=True)
        initial = len(self.runner.started)
        self.scheduler.pause()
        self.scheduler.tick()
        self.assertEqual(len(self.runner.started), initial)
        self.assertTrue(all(h.alive() and not h.terminated for _, _, _, h in self.runner.started))
        self.runner.started[0][3].result = good()
        self.scheduler.tick()
        self.assertEqual(self.ledger.lifetime()["completed"], 1)
        self.assertEqual(len(self.runner.started), initial)
        self.assertEqual(self.scheduler.status()["scheduler"]["active"], 7)

    def test_valid_json_only_xp_and_provider_specific_quota_backoff(self):
        self.scheduler.tick(force_cycle=True)
        codex = [(a, jid, prompt, h) for a, jid, prompt, h in self.runner.started if a == "CODEX"]
        codex[0][3].result = cli.Outcome("failed", failure_kind="quota", failure_reason="quota")
        self.scheduler.tick()
        self.assertGreaterEqual(self.scheduler.cooldown_left_ms("CODEX"), 599000)
        self.assertEqual(self.scheduler.cooldown_left_ms("CLAUDE"), 0)
        self.assertEqual(self.ledger.lifetime()["completed"], 0)

    def test_successful_sibling_must_not_cancel_existing_provider_quota_backoff(self):
        self.scheduler.tick(force_cycle=True)
        workers = [w for w in self.scheduler.workers if w.alias == "CODEX" and w.busy()]
        self.assertGreaterEqual(len(workers), 2)
        self.scheduler._finish_worker(workers[0], cli.Outcome("failed", failure_kind="quota", failure_reason="quota"))
        self.assertGreater(self.scheduler.cooldown_left_ms("CODEX"), 599000)
        self.scheduler._finish_worker(workers[1], good())
        self.assertGreater(self.scheduler.cooldown_left_ms("CODEX"), 599000)

    def test_ledger_finish_is_idempotent(self):
        job = self.job()
        self.ledger.enqueue(job)
        self.ledger.mark_started(job["id"], "CODEX-1", 12345, g.now_ms(), g.now_ms() + 150000)
        self.ledger.finish(job["id"], "completed", good())
        self.ledger.finish(job["id"], "completed", good())
        self.assertEqual(self.ledger.lifetime()["completed"], 1)

    def test_ledger_rejects_invalid_completed_outcome(self):
        job = self.job()
        self.ledger.enqueue(job)
        self.ledger.mark_started(job["id"], "CODEX-1", 12345, g.now_ms(), g.now_ms() + 150000)
        self.ledger.finish(job["id"], "completed", cli.Outcome("failed", failure_kind="invalid_json"))
        self.assertEqual(self.ledger.lifetime()["completed"], 0)

    def test_completed_state_and_lifetime_xp_have_one_commit_boundary(self):
        job = self.job()
        self.ledger.enqueue(job)
        self.ledger.mark_started(job["id"], "CODEX-1", 12345, g.now_ms(), g.now_ms() + 150000)
        original = self.ledger.counter_add
        def interrupted_counter(*args, **kwargs):
            raise RuntimeError("simulated stop at transaction boundary")
        self.ledger.counter_add = interrupted_counter
        try:
            try:
                self.ledger.finish(job["id"], "completed", good())
            except RuntimeError:
                pass
        finally:
            self.ledger.counter_add = original
        readback = sqlite3.connect(str(self.path))
        try:
            state = readback.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
            xp = int(readback.execute("SELECT value FROM meta WHERE key='lifetime_completed'").fetchone()[0])
        finally:
            readback.close()
        self.assertEqual(int(state == "completed"), xp)

    def test_poll_exception_never_forgets_a_still_live_owned_child(self):
        self.scheduler.tick(force_cycle=True)
        worker = next(w for w in self.scheduler.workers if w.busy())
        owned = worker.handle
        def broken_poll():
            raise RuntimeError("simulated transient polling error")
        owned.poll = broken_poll
        self.scheduler.tick()
        self.assertTrue(not owned.alive() or any(w.handle is owned for w in self.scheduler.workers))

    def test_recovery_state_and_interrupted_counter_are_one_transaction(self):
        job = self.job()
        self.ledger.enqueue(job)
        self.ledger.mark_started(job["id"], "CODEX-1", 12345, g.now_ms(), g.now_ms() + 150000)
        original = self.ledger.counter_add
        def interrupted_counter(*args, **kwargs):
            raise RuntimeError("simulated stop at transaction boundary")
        self.ledger.counter_add = interrupted_counter
        try:
            try:
                self.ledger.recover()
            except RuntimeError:
                pass
        finally:
            self.ledger.counter_add = original
        self.assertEqual(self.ledger.count_status("interrupted"), self.ledger.lifetime()["interrupted"])

    def test_actual_sqlite_recovery_resumes_fresh_queue_and_preserves_terminal(self):
        self.ledger.enqueue(self.job("queued"))
        self.ledger.enqueue(self.job("running", "risk"))
        self.ledger.enqueue(self.job("completed", "summary"))
        self.ledger.mark_started("audit-running", "CODEX-1", 12345, g.now_ms(), g.now_ms() + 150000)
        self.ledger.mark_started("audit-completed", "CODEX-2", 12346, g.now_ms(), g.now_ms() + 150000)
        self.ledger.finish("audit-completed", "completed", good())
        self.ledger.close()
        self.ledger = g.GuildLedger(str(self.path))
        self.scheduler = g.GuildScheduler(self.ledger, self.runner, fetcher=lambda: g.PublicSnapshot(False, "unavailable"))
        self.assertEqual(self.scheduler.recovered["interrupted"], 1)
        self.assertEqual(self.ledger.lifetime(), {"completed": 1, "failed": 0, "interrupted": 1})
        self.scheduler.tick()
        self.assertEqual(len(self.runner.started), 1)
        self.assertEqual(self.ledger.count_status("running"), 1)
        self.assertEqual(self.ledger.count_status("completed"), 1)

    def test_queue_dedup_and_backpressure_bound(self):
        for index in range(110):
            self.ledger.enqueue(self.job(str(index)))
        self.assertLessEqual(self.ledger.count_status("queued"), 96)

    def test_status_get_never_starts_work(self):
        self.ledger.enqueue(self.job())
        for _ in range(3):
            self.scheduler.status()
        self.assertEqual(self.runner.started, [])

    def test_full_queue_status_keeps_every_active_worker_job_visible(self):
        self.scheduler.tick(force_cycle=True)
        for index in range(110):
            job = self.job("backpressure-" + str(index))
            job["queued_at_ms"] = g.now_ms() + 1000
            self.ledger.enqueue(job)
        self.assertEqual(self.ledger.count_status("queued"), 96)
        state = self.scheduler.status()
        active_ids = {worker["job_id"] for worker in state["workers"] if worker["status"] == "running"}
        exposed_ids = {job["id"] for job in state["jobs"] if job["status"] == "running"}
        self.assertEqual(len(active_ids), 8)
        self.assertTrue(active_ids.issubset(exposed_ids))

    def test_live_queue_ttl_is_enforced_before_dispatch(self):
        job = self.job()
        job["queued_at_ms"] = g.now_ms() - (g.QUEUE_TTL_SECONDS + 1) * 1000
        self.ledger.enqueue(job)
        self.scheduler._next_cycle_at = time.time() + 600
        self.scheduler.tick()
        self.assertEqual(self.runner.started, [])
        self.assertEqual(self.ledger.count_status("queued"), 0)

    def test_dedup_fairness_covers_all_provider_lane_combinations(self):
        # One symbol has 6 lanes x 4 providers = 24 distinct dedup combinations.
        snap = snapshot()
        self.scheduler._plan(snap, ["CODEX", "CLAUDE", "GROK", "AGY"], 96)
        queued = self.ledger.queued()
        self.assertEqual(len(queued), 24)
        self.assertEqual(len({(j["lane_id"], j["alias"]) for j in queued}), 24)


class NativeProcessAudit(unittest.TestCase):
    def test_eight_genuine_python_children_overlap_and_close_owned_handles(self):
        fixture = Path(tempfile.mkdtemp(prefix="workguild-native-tests-"))
        handles = []
        # The gate holds early children until all eight genuine PIDs exist;
        # each then performs the same bounded 0.8-second task.
        child = ("import os,time\n"
                 "deadline=time.monotonic()+3\n"
                 "while not os.path.exists('release.flag') and time.monotonic()<deadline:\n"
                 " time.sleep(0.01)\n"
                 "time.sleep(0.8)\n")
        try:
            for _ in range(8):
                handle = cli.OwnedProcess([sys.executable, "-B", "-c", child],
                    cwd=str(fixture), timeout_seconds=5, max_output_bytes=4096).start()
                handles.append(handle)
                self.assertFalse(handle.spawn_error)
            self.assertEqual(len({handle.pid for handle in handles}), 8)
            self.assertTrue(all(type(handle.pid) is int and handle.pid > 0 and handle.alive()
                                for handle in handles))
            (fixture / "release.flag").write_text("release", encoding="ascii")
            results = {}
            deadline = time.monotonic() + 5
            while len(results) < 8 and time.monotonic() < deadline:
                for handle in handles:
                    result = handle.poll()
                    if result is not None:
                        results[handle.pid] = result
                time.sleep(0.02)
            self.assertEqual(len(results), 8)
            self.assertTrue(all(result.exit_code == 0 and not result.timed_out and not result.overflow
                                for result in results.values()))
        finally:
            for handle in handles:
                handle.close()
            deadline = time.monotonic() + 2
            while any(handle.alive() for handle in handles) and time.monotonic() < deadline:
                time.sleep(0.02)
            for handle in handles:
                handle.poll()
            print("Retained owned native-test fixture for recycle-only cleanup: %s" % fixture, file=sys.stderr)
        self.assertTrue(all(not handle.alive() for handle in handles))


if __name__ == "__main__":
    unittest.main(verbosity=2)
