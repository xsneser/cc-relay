# Long CLI History: Partial Cache Reuse

Follow-up: IMAGE_CACHE_PROBE_FINDINGS.md records the completed 16-request small
image/no-image experiment. It did NOT reproduce an image cache barrier; image
warm reuse was 92.89%. The image-boundary hypothesis below remains unproven, and
must not be presented as an established cause or used to justify deleting images.

Read-only investigation on 2026-09-21. No inference, restart, account change,
credential access, or production patch. Existing historical replay results were
reviewed; no new replay was run.

## What Is Established

One CLI history contains 220 successful requests with >=200k input tokens,
2026-09-20 18:46:04 through 20:25:02. Weighted rates use sum(cached)/sum(input),
not averages of percentages. NULL means unreported, not proven physical zero.

| Input size | Requests | Reported weighted reuse, NULL as 0 | Excluding NULL |
| --- | ---: | ---: | ---: |
| 200k to <250k | 32 | 89.21% | 97.58% |
| 250k to <300k | 28 | 93.95% | 93.95% |
| 300k to <400k | 68 | 75.40% | 76.38% |
| 400k to <500k | 53 | 59.90% | 59.90% |
| >=500k | 39 | 45.45% | 50.68% |

The reported reused amount frequently remains around 245k, 249k, 270k, 274k,
299k, 324k, or 332k while total input keeps growing. This explains the arithmetic
of the declining percentage. It does not establish an internal Google cache cap.
Maximum real cached count was 369,688; earlier synthetic many-tool requests
occasionally reused >99%. A hard 250k or 50% ceiling is contradicted by evidence.

217/219 transitions preserve the complete previous LOGGED contents prefix,
including retained object key order. The two exceptions change only the last
content, at 19:20:34 and 19:50:24 (99.96% and 99.98% shared logged bytes).
System, tools, generation, tool config, and safety fingerprints never change.
Only one account/session switch occurs. Before that switch, all 19 requests
above 500k have explicit cache counts and only 53.45% weighted reuse. Thus missing
cache fields, switching, and Desktop compaction cannot explain the entire effect.

## New Lead: Images At The Early Reuse Boundary

The last no-image request is 18:52:51: input 247,583, cached 241,983, 227 contents.
At 18:52:56 the first two image parts appear in content index 228 (zero-based):
input 250,354, cached 246,059. By 18:53:05 there are five images; subsequent
cached values repeatedly fall around 245k, even when input eventually reaches
571,126. The last request contains 41 images and reports cached 274,238 (48.02%).

Several other frequent cached bands are near historical image-addition positions:
images enter after prompt sizes 250,354, 270,789, 273,399, and 299,630; frequent
cache bands are about 249k, 270k, 274k, and 299k. These are approximate historical
prompt-size landmarks, NOT tokenized per-part boundaries or proven cache offsets.

This makes multimodal-prefix behavior a priority hypothesis. Image presence,
history size, and elapsed time grow together, so this is not a controlled causal
test. Earlier no-image synthetic many-tool histories also sometimes gave ~50%
reuse, so images cannot explain every partial-cache observation.

## Critical Logging Blind Spot

All 41 outgoing inlineData image parts are redacted. UpstreamRequestBody first
replaces actual data with `[inline data omitted: N chars]` in proxy/monitor.rs
(set_value and sanitize_upstream_debug_value, lines 175-228). Then simple-mode
payload_audit.rs (simplify_part, lines 157-174) replaces that placeholder with
`[base64 image: 34 bytes]` or similar. The apparent byte count is the length of
the earlier placeholder, NOT necessarily the image's length. Consequently the
old prefix hashes did not prove actual image equality. Prior findings are amended.

The corresponding CLI JSONL contains 41 base64 images, 40 unique content hashes.
No raw image was saved or displayed. Their actual lengths cannot be matched to
the double-redacted outgoing values. Source inspection shows normal valid images
are passed through, with trim/base64 validation and MIME normalization; there is
no per-request resizing/reencoding in this Claude tool-result conversion path:
proxy/mappers/claude/request.rs:1450 and proxy/mappers/common_utils.rs:1303-1389.
This weakens, but does not independently disprove, a Manager image-mutation theory.

## Usage Arithmetic Check

Source paths are under old/lbjlaq-manager/src-tauri/src/.
proxy/pipeline/usage.rs:43 reads Google promptTokenCount as total input and
cachedContentTokenCount as its cached subset. Anthropic conversion subtracts cache
from input; the audit adds it back. Google input is not added to cache twice.
Local estimates are fallback only and do not create positive cache counts.
Thought signatures are not charged by base64 string length in the local estimator.

The 220 retained responses contain normalized `usage`, not original Google
usageMetadata or modality details. Existing direct-Google synthetic results retain
parsed Google prompt/cached counts and reproduce partial reuse without Manager's
response conversion. This argues against a generic Manager denominator bug, but
does not independently validate every production record's original Google fields.

## Conclusion And Next Discriminating Test

The denominator grows while the reported reusable portion stalls; old-history
text churn and account switching do not explain most of it. Images begin near the
earliest recurring plateau, but the present record cannot distinguish multimodal
prefix handling, cache admission/eviction/routing, or unobserved image-byte changes.
The exact Google-internal reason remains unproven. Do not call this a proven fix.

Before another paid replay, capture ONLY full outgoing part SHA-256 hashes
(including image data before redaction) and raw numeric usageMetadata fields,
including promptTokensDetails/cacheTokensDetails if supplied. Do not store images
or credentials. Then use a small isolated paired image/no-image test with identical
text/tool structure, separate controlled prefixes, and multiple warm repetitions.
Removing/replacing images is a diagnostic manipulation, not a production change.
Previous quota exhaustion is a reason not to launch another large replay blindly.

Google's public implicit-cache docs promise no cache saving guarantee and recommend
shared prefixes close in time. They do not specify Antigravity Cloud Code policy:
https://ai.google.dev/gemini-api/docs/generate-content/caching
A first-person report also describes fixed reuse as history grows on another model;
it corroborates the symptom only, not this workload's root cause:
https://discuss.ai.google.dev/t/cant-get-the-implicit-caching-to-work/97205/12

Reproduce: `python -m diagnostics.long_history_audit --output diagnostics/long-history-audit-20260921.json`
The script uses SQLite mode=ro/query_only and emits only counts, hashes, and aliases.
