"""Polling cache regressions: all records live in a temporary JSONL file."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cc_relay


def record(idx, model="gemini-3.8-flash-high"):
    return {
        "idx": idx,
        "route": "antigravity",
        "orig_model": model,
        "sent_model": model,
        "resp_status": 200,
        "body": {"messages": [{"role": "user", "content": "not cached"}], "tools": [], "stream": True},
        "headers": {"X-Claude-Code-Agent-Id": "agent-1234567890"},
        "resp_body": "",
        "cache": {"cache_total_tokens": 10, "cache_read_tokens": 2, "cache_creation_tokens": 0},
    }


class StatusPollingCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.records = os.path.join(self.directory.name, "records.jsonl")
        self.original_records = cc_relay.RECORDS
        cc_relay.RECORDS = self.records
        with cc_relay._STATS_LOCK:
            cc_relay._STATS_CACHE.update({"fingerprint": None, "rows": None, "total": 0})
        with cc_relay._CALLS_LOCK:
            cc_relay._CALLS_CACHE.clear()
        self.addCleanup(self._restore_records)

    def _restore_records(self):
        cc_relay.RECORDS = self.original_records
        self.directory.cleanup()

    def _append(self, value):
        with open(self.records, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(value) + "\n")
        os.utime(self.records, None)

    def _status_dependencies(self):
        config = {"router": {"route": "hybrid", "model": "first"}, "upstreams": {}}
        models = {"ds": ["deepseek-a"], "cx": ["gpt-a"], "gm": ["gemini-a"]}
        return (
            mock.patch.object(cc_relay, "load_conf", return_value=config),
            mock.patch.object(cc_relay, "live_models", return_value=models),
            mock.patch.object(cc_relay, "codex_up", return_value=False),
            mock.patch.object(cc_relay, "antigravity_up", return_value=False),
        )

    def test_status_cache_uses_file_fingerprint_but_keeps_config_and_models_live(self):
        self._append(record(1))
        config_patch, model_patch, codex_patch, agy_patch = self._status_dependencies()
        with config_patch as load_conf, model_patch as live_models, \
                codex_patch as codex_up, agy_patch as antigravity_up, \
                mock.patch.object(cc_relay, "_iter_records_tail", wraps=cc_relay._iter_records_tail) as tail:
            first = cc_relay.stats_snapshot()
            # Same file avoids parsing again, yet the response still obtains live values.
            load_conf.return_value["router"]["model"] = "changed-now"
            live_models.return_value = {"ds": [], "cx": [], "gm": ["gemini-now"]}
            second = cc_relay.stats_snapshot()
            self.assertEqual(tail.call_count, 1)
            self.assertEqual(load_conf.call_count, 2)
            self.assertEqual(live_models.call_count, 2)
            self.assertEqual(codex_up.call_count, 2)
            self.assertEqual(antigravity_up.call_count, 2)
            self.assertEqual(second["model"], "changed-now")
            self.assertEqual(second["models_gemini"], ["gemini-now"])
            self.assertEqual(first["rows"], second["rows"])
            self._append(record(2))
            third = cc_relay.stats_snapshot()
            self.assertEqual(tail.call_count, 2)
            self.assertEqual(third["total"], 2)
            self.assertEqual(codex_up.call_count, 3)
            self.assertEqual(antigravity_up.call_count, 3)

    def test_append_during_status_read_invalidates_the_next_snapshot(self):
        self._append(record(1))
        dependencies = self._status_dependencies()
        original_tail = cc_relay._iter_records_tail
        appended = False

        def append_after_read(*args, **kwargs):
            nonlocal appended
            records = original_tail(*args, **kwargs)
            if not appended:
                appended = True
                self._append(record(2))
            return records

        with dependencies[0], dependencies[1], dependencies[2], dependencies[3], \
                mock.patch.object(cc_relay, "_iter_records_tail", side_effect=append_after_read) as tail:
            first = cc_relay.stats_snapshot()
            second = cc_relay.stats_snapshot()
        self.assertEqual(tail.call_count, 2)
        self.assertEqual(first["total"], 1)
        self.assertEqual(second["total"], 2)

    def test_status_singleflight_parses_one_file_version_once(self):
        self._append(record(1))
        dependencies = self._status_dependencies()
        original_tail = cc_relay._iter_records_tail
        entered = threading.Event()

        def slow_tail(*args, **kwargs):
            entered.set()
            time.sleep(0.1)
            return original_tail(*args, **kwargs)

        with dependencies[0], dependencies[1], dependencies[2], dependencies[3], \
                mock.patch.object(cc_relay, "_iter_records_tail", side_effect=slow_tail) as tail:
            threads = [threading.Thread(target=cc_relay.stats_snapshot) for _ in range(2)]
            for thread in threads:
                thread.start()
            self.assertTrue(entered.wait(1))
            for thread in threads:
                thread.join(2)
            self.assertEqual(tail.call_count, 1)

    def test_call_summaries_cache_by_fingerprint_and_requested_range(self):
        self._append(record(1))
        self._append(record(2))
        with mock.patch.object(cc_relay, "_iter_records_tail", wraps=cc_relay._iter_records_tail) as tail:
            first = cc_relay._calls_snapshot(1)
            second = cc_relay._calls_snapshot(1)
            wider = cc_relay._calls_snapshot(2)
            self.assertEqual(tail.call_count, 2)
            self.assertEqual(first, second)
            self.assertEqual(first["total"], 1)
            self.assertEqual(wider["total"], 2)
            self.assertNotIn("body", first["calls"][0])
            self._append(record(3))
            changed = cc_relay._calls_snapshot(1)
            self.assertEqual(tail.call_count, 3)
            self.assertEqual(changed["calls"][0]["idx"], 3)

    def test_append_during_calls_read_invalidates_the_next_snapshot(self):
        self._append(record(1))
        original_tail = cc_relay._iter_records_tail
        appended = False

        def append_after_read(*args, **kwargs):
            nonlocal appended
            records = original_tail(*args, **kwargs)
            if not appended:
                appended = True
                self._append(record(2))
            return records

        with mock.patch.object(cc_relay, "_iter_records_tail", side_effect=append_after_read) as tail:
            first = cc_relay._calls_snapshot(1)
            second = cc_relay._calls_snapshot(1)
        self.assertEqual(tail.call_count, 2)
        self.assertEqual(first["calls"][0]["idx"], 1)
        self.assertEqual(second["calls"][0]["idx"], 2)

    def test_exclusive_server_disables_reuse_and_sets_windows_option(self):
        self.assertFalse(cc_relay.ExclusiveThreadingHTTPServer.allow_reuse_address)
        server = object.__new__(cc_relay.ExclusiveThreadingHTTPServer)
        server.socket = mock.Mock()
        with mock.patch.object(cc_relay.os, "name", "nt"), \
                mock.patch.object(cc_relay.socket, "SO_EXCLUSIVEADDRUSE", 4, create=True), \
                mock.patch.object(cc_relay.ThreadingHTTPServer, "server_bind") as parent_bind:
            cc_relay.ExclusiveThreadingHTTPServer.server_bind(server)
        server.socket.setsockopt.assert_called_once_with(cc_relay.socket.SOL_SOCKET, 4, 1)
        parent_bind.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
