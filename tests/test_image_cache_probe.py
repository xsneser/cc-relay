import copy
import json
import unittest

from diagnostics.image_cache_probe import make_pair, outgoing_metadata, safe_usage, TappedResponse, aggregate, final_usage


class ImageCacheProbeTests(unittest.TestCase):
    def test_later_cache_only_frame_is_not_reported_as_missing(self):
        with self.assertRaises(ValueError):
            final_usage([{"promptTokenCount": 100}, {"cachedContentTokenCount": 90}])

    def test_final_usage_does_not_mix_frames(self):
        self.assertEqual(final_usage([{"promptTokenCount": 100, "cachedContentTokenCount": 90},
                                      {"promptTokenCount": 110}]), (110, None))
        with self.assertRaises(ValueError):
            final_usage([{"promptTokenCount": 100, "cachedContentTokenCount": 101}])

    def test_pair_only_image_and_isolation_fields_differ(self):
        arms = make_pair({"generationConfig": {}}, "private-project", 1234)
        try:
            a = copy.deepcopy(arms["no_image"]["body"]["request"])
            b = copy.deepcopy(arms["image_midpoint"]["body"]["request"])
            for item in (a, b):
                item.pop("systemInstruction")
                item.pop("sessionId")
            removed = b["contents"][10]["parts"].pop()
            self.assertIn("inlineData", removed)
            self.assertEqual(a, b)
            meta = outgoing_metadata(arms["image_midpoint"]["body"]["request"])
            self.assertEqual(len(meta["images"]), 1)
            self.assertNotIn(removed["inlineData"]["data"], json.dumps(meta))
            self.assertNotIn("private-project", json.dumps(meta))
        finally:
            for arm in arms.values():
                arm["session"].close()

    def test_numeric_usage_allowlist(self):
        self.assertEqual(safe_usage({"promptTokenCount": 123, "secret": "token",
                         "cacheTokensDetails": [{"modality": "IMAGE", "tokenCount": 30, "secret": "token"}]}),
                         {"promptTokenCount": 123, "cacheTokensDetails": [{"modality": "IMAGE", "tokenCount": 30}]})

    def test_raw_frames_preserve_missing_cache_vs_zero(self):
        class Response:
            def iter_lines(self):
                yield b'data: {"response":{"usageMetadata":{"promptTokenCount":123}}}'
                yield b'data: {"response":{"usageMetadata":{"cachedContentTokenCount":0}}}'
                yield b'data: [DONE]'
        frames = []
        self.assertEqual(len(list(TappedResponse(Response(), frames).iter_lines())), 3)
        self.assertNotIn("cachedContentTokenCount", frames[0])
        self.assertEqual(frames[1]["cachedContentTokenCount"], 0)

    def test_aggregate_excludes_cold_and_error(self):
        rows = [{"arm": "no_image", "turn": 1, "input_tokens": 100, "cached_tokens": 0},
                {"arm": "no_image", "turn": 2, "input_tokens": 100, "cached_tokens": None},
                {"arm": "no_image", "turn": 3, "input_tokens": 200, "cached_tokens": 180},
                {"arm": "no_image", "turn": 4, "error": "failed"}]
        result = aggregate(rows)["no_image"]
        self.assertEqual(result["weighted_reported_hit_pct"], 60)
        self.assertEqual(result["missing_cache_field"], 1)
        self.assertEqual(result["warm_requests"], 2)


if __name__ == "__main__":
    unittest.main()
