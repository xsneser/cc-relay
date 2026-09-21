# Small Image Cache Probe

Completed 2026-09-21. Result: image-cache-probe-20260921-113435.json.
No production configuration, account selection policy, or services were changed.
No real conversation or screenshot was sent. No model tool was executed.

## Design

- Same reference account, model gemini-3.8-flash-high, Google Sandbox endpoint,
  curl Chrome123 transport, tools, and generation settings within the experiment.
- Two independent synthetic fixtures. Each pair has the same 600 archive rows
  across 10 completed tool-call/result pairs. One arm inserts a fixed 768x512 PNG
  after the fifth result; the other has no image. There is substantial text both
  before and after the image. Per request input is about 34k/35k, NOT 480k.
- Separate equal-character-length system nonces and sessions isolate the arms;
  their differing nonce tokenization is a small confound. Requests are interleaved,
  with initial arm order reversed in repetition two. Each arm has one nominal cold
  request and three warm repeats. No retries, account switching, or endpoint fallback.
- The entire inner Google request is identical within each arm, verified using
  SHA-256 immediately before send. Transport requestId changes each time outside
  that inner object. Synthetic model replies are not appended to later test input.
- Every outgoing part hash, base64 image hash, and decoded-image hash is recorded
  before redaction. Image bytes and credentials are never written to the report.
- Each original Google SSE usageMetadata frame is retained through a numeric and
  modality allowlist. Statistics use the LAST frame containing promptTokenCount,
  without merging fields from different frames; validate 0 <= cached <= prompt.

## Results

| Repetition | Arm | Cold | Warm 1 | Warm 2 | Warm 3 |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | No image | unreported | unreported | 83.89% | 83.89% |
| 1 | Image midpoint | unreported | 92.89% | 92.89% | 92.89% |
| 2 | No image | unreported | 83.89% | 83.89% | unreported |
| 2 | Image midpoint | unreported | 92.90% | 92.90% | 92.90% |

All 16 requests returned HTTP 200 and the expected inert cache_probe call.
Excluding each arm's first request, token-weighted reported reuse:

- No image: 114,304 / 204,384 = 55.93%. Two of six warm requests omit the cache
  field; those count as zero ONLY in this reported-reuse metric. The four warm
  requests with the field all have cached=28,576, input=34,064, or 83.89%.
- Image: 195,900 / 210,885 = 92.89%. All six warm requests report cached=32,650;
  input is 35,148 in repetition one and 35,147 in repetition two.

Total reported input for the full experiment: 553,692 tokens, of which 310,204
are reported cached. Candidate output: 256 tokens; reported thoughts: 1,707 tokens.
This is API usage, not a monetary bill or independently verified physical cache.
The configured aggregate input guard was 700,000 tokens.

## What This Does And Does Not Establish

The small fixed-image fixture does NOT reproduce an image boundary that blocks
reuse of the second half. Reuse exceeds 92%, well beyond the image's midpoint.
Images therefore are not a universal cache barrier in this endpoint/model setup.

Conversely, no-image requests can omit cache usage even after a successful hit,
with a verified identical inner payload and no Manager/cc-relay conversion in
the response path. Thus payload churn and Manager parsing are not necessary for
the observed cache-reporting fluctuation. A missing field still does not prove
there was no physical server-side reuse.

Do NOT infer that adding images improves caching. This is one synthetic image,
two independent fixture pairs, and six warm requests per arm. Cache block alignment,
backend placement/admission/eviction, and nonce differences remain uncontrolled.
There is no server-side cache-offset or eviction-reason telemetry.

This is a direct-Google test, not a test of Manager's actual image conversion.
It does not rule out a size-dependent issue in the real growing 250k-570k history,
41 real screenshots, or accumulated genuine thought signatures. It also does not
reproduce the growing-history workload: input is deliberately fixed to eliminate
history mutation as a variable. Random archive seeds were not retained in this
run, so hashes verify within-run identity but do not allow exact byte reconstruction.

## Useful Raw-Frame Observation

For image requests Google sends an initial usage frame without cache information,
then a final usage frame with corrected prompt usage and (on warm requests) cache:

    initial: promptTokenCount=34068, cachedContentTokenCount absent
    final:   promptTokenCount=35148, cachedContentTokenCount=32650

The final prompt count adds 1,080 tokens versus that initial frame. No
promptTokensDetails or cacheTokensDetails were supplied in the retained frames;
we cannot independently allocate exact token counts to individual image parts.
This confirms why reading only the first usage frame would be misleading. It
does NOT show that the existing cc-relay parser does so; prior SSE checks passed.

## Next Decision

Do not remove pictures, change headers, or roll back Manager based on the earlier
image-boundary correlation. That correlation has not become a causal result.
The next useful evidence is pre-redaction hashes plus raw usage on the actual
long workload, or a separately budgeted size-matched controlled replay. Neither
has been run or installed by this probe. No further live calls were made after
the 16-request plan completed.

Local verification: 44 tests passed, including six new probe tests covering
paired fixture equivalence, privacy, missing/zero cache distinction, final-frame
selection, rejection of a later cache-only frame, and weighted aggregation.
Offline revalidation of all 16 captured frame sequences passed the stricter
parser; no later cache-only frame occurred. No production request path was edited.
