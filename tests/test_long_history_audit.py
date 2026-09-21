import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from diagnostics.long_history_audit import geometry, image_metadata, ordered_digest, run, summarize


class LongHistoryAuditTests(unittest.TestCase):
    def test_image_redaction_is_an_evidence_gap(self):
        result = image_metadata([{"parts": [{"text": "secret"}]},
                                 {"parts": [{"inlineData": {"data": "[base64 image: 34 bytes]"}},
                                            {"inlineData": {"data": "private-image-data"}}]}])
        self.assertEqual(result, {"image_count": 2, "first_image_content_index": 1,
                                  "redacted_image_count": 1})
        self.assertNotIn("private-image-data", json.dumps(result))

    def test_geometry_does_not_emit_contents_or_signatures(self):
        result = geometry([{"parts": [{"text": "secret text", "thought": True,
                                       "thoughtSignature": "secret signature"},
                                      {"functionResponse": {"name": "private-tool", "response": {}}}]}])
        self.assertEqual(result["part_counts"]["thought"], 1)
        self.assertEqual(result["part_counts"]["signed_parts"], 1)
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("private-tool", json.dumps(result))

    def test_missing_and_explicit_zero_are_distinct(self):
        result = summarize([{"input": 100, "cached": None}, {"input": 100, "cached": 0},
                            {"input": 100, "cached": 90}])
        self.assertEqual(result["weighted_reported_hit"], .3)
        self.assertEqual(result["known_field_weighted_hit"], .45)
        self.assertEqual(result["missing_cache_field"], 1)

    def test_ordered_digest_detects_object_order_change(self):
        self.assertNotEqual(ordered_digest({"a": 1, "b": 2}), ordered_digest({"b": 2, "a": 1}))

    def test_read_only_cli_filter_and_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.db"
            db = sqlite3.connect(path)
            db.execute("""CREATE TABLE request_logs (
                timestamp INTEGER, status INTEGER, url TEXT, model TEXT,
                mapped_model TEXT, input_tokens INTEGER, cached_tokens INTEGER,
                account_email TEXT, session_id TEXT, request_body TEXT,
                upstream_request_body TEXT, request_headers TEXT)""")
            first = {"role": "user", "parts": [{"text": "private-prompt"}]}
            for index, ua in enumerate(("external, cli", "external, cli", "external, local-agent")):
                contents = [first] + ([{"role": "model", "parts": [{"text": "private-response"}]}] if index else [])
                db.execute("INSERT INTO request_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (index * 1000, 200, "/v1/messages", "model", "model", 300000,
                            None if index == 0 else 250000, "private-email", "private-session",
                            json.dumps({"messages": [first]}),
                            json.dumps({"contents": contents, "sessionId": "private-session"}),
                            json.dumps({"user-agent": ua, "authorization": "private-token"})))
            db.commit()
            db.close()
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            result = run(path)
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
            group = result["history1"]
            self.assertEqual(group["summary"]["requests"], 2)
            self.assertEqual(group["full_ordered_prefix_preserved"], 1)
            self.assertEqual(group["control_changes"], 0)
            for secret in ("private-prompt", "private-response", "private-email", "private-session", "private-token"):
                self.assertNotIn(secret, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
