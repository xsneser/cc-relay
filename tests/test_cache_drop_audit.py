import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from diagnostics.cache_drop_audit import run


class CacheDropAuditTests(unittest.TestCase):
    def test_read_only_audit_separates_auxiliary_histories_and_missing_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.db"
            connection = sqlite3.connect(database)
            connection.execute("""CREATE TABLE request_logs (
                timestamp INTEGER, status INTEGER, url TEXT, model TEXT,
                mapped_model TEXT, input_tokens INTEGER, cached_tokens INTEGER,
                account_email TEXT, session_id TEXT, request_body TEXT,
                upstream_request_body TEXT, request_headers TEXT)""")

            def add(ts, messages, contents, cached, account="private@example.invalid",
                    url="/v1/messages"):
                body = {"messages": messages, "system": "private prompt", "tools": []}
                upstream = {"contents": contents, "sessionId": "private-session",
                            "systemInstruction": {"parts": [{"text": "private prompt"}]}}
                headers = {"X-Claude-Code-Session-Id": "private-client",
                           "Authorization": "private-token"}
                connection.execute("INSERT INTO request_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (ts, 200, url, "test-model", "test-model", 100, cached,
                                    account, "private-db-session", json.dumps(body),
                                    json.dumps(upstream), json.dumps(headers)))

            first = {"role": "user", "content": "private conversation"}
            extra = {"role": "assistant", "content": "private continuation"}
            add(1000, [first], [first], 50)
            add(2000, [{"role": "user", "content": "auxiliary"}], [], 99)
            add(3000, [first, extra], [first, extra], None, account="second@example.invalid")
            add(4000, [first], [first], 0, url="/v1/messages/count_tokens")
            connection.commit()
            connection.close()
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            result = run(database)
            self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())
            self.assertEqual(result["totals"]["requests"], 3)
            self.assertEqual(result["totals"]["missing_cache_field"], 1)
            last = result["requests"][-1]
            self.assertIsNone(last["cached"])
            self.assertTrue(last["comparison"]["account_changed"])
            self.assertTrue(last["comparison"]["upstream_prefix"]["previous_fully_preserved"])
            self.assertEqual(last["comparison"]["previous_hit"], 0.5)
            self.assertEqual(last["previous_client_request"]["hit"], 0.99)
            serialized = json.dumps(result)
            for private in ("private prompt", "private conversation", "private-token",
                            "private-client", "private-session", "@example.invalid"):
                self.assertNotIn(private, serialized)


if __name__ == "__main__":
    unittest.main()
