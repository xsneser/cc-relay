# Cache Drop Audit

Read-only audit on 2026-09-21. No inference requests, production settings changes,
account mutations, or restarts were performed. User confirmed plain Opus/Sonnet
requests originate in Claude Desktop; the separate long history is CLI traffic.

## Scope

The newest successful generation with positive input usage in the Manager log
is still 2026-09-20 21:23:43 local time. These are historical findings, not a new
post-fix workload. The latest 160 successful generations exclude count_tokens.
Missing cached_tokens is retained as null, counted as zero only in the reported
reuse metric; it does not prove a physically empty upstream cache.

| Input size | Requests | Weighted reported reuse |
| --- | ---: | ---: |
| Below 20k | 26 | 9.40% |
| 20k to below 200k | 109 | 93.42% |
| At least 200k | 25 | 41.13% |
| Combined | 160 | 65.67% |

This window differs from the dashboard's 208-record window and earlier reports.

## Distinct Causes And Limits

1. Desktop history replacement: the 20:22:32 request reports 19.17% after client
   history falls from 167 messages to 5. Subsequent requests report 93.94% and
   98.31%. At 20:23:29, history falls from 8 messages to 5 and reuse is 25.37%,
   then recovers to 96.78% and 95.79%. Client bodies already contain continuation
   summary markers before Manager conversion. These are client-side history
   replacements, not evidence of Manager's automatic compression. Manager's
   compression_level is disabled.
2. Account change: Manager logs classify the 20:12:30 failure as QuotaExhausted
   (429). The next long request at 20:12:42 uses another account and another
   upstream session and has cached_tokens=null. Failover is a plausible contributor
   to this cold transition, not proof that every missing cache count is caused by
   account changes. Keep legitimate quota handling and load balancing intact.
3. Stable logged long history: all 24 comparable transitions among the 25 long requests
   preserve the full previous simplified logged contents prefix. System, tool declarations,
   and generation fingerprints are unchanged. Shared serialized-content fractions
   are at least 99.07% (bytes, NOT tokens). Only one transition changes accounts.
   Reuse still varies, including missing values and roughly 44-58% on the same
   account. This excludes visible TEXT prefix churn as an explanation for these
   specific transitions, but does not identify Google's internal cause. IMPORTANT:
   the follow-up audit found 41 redacted image parts. The audit sanitizer and
   simple-mode formatter replace image data twice, so the recorded length may
   even be the length of an earlier placeholder. These hashes cannot establish
   equality of actual outgoing images. See LONG_HISTORY_FINDINGS.md. Outgoing
   requestId and other transport/server state are not claimed to be constant.
4. Mixed traffic: high-reuse Desktop calls and lower-reuse CLI calls appear next
   to one another in the dashboard. Adjacent rows are not necessarily successive
   turns of one conversation. Comparison separates CLI auxiliary requests by
   the first client message fingerprint, not only the shared client session ID.

## Optimization Boundary

CacheFirst with max_wait_seconds=60 is already enabled. Do not fix one account,
ignore quota errors, strip thought signatures, flatten tool history, or change
HTTP headers based on these findings. Earlier controlled tests did not establish
a reliable benefit from those header/session changes; restored old/new native
long-history runs reported 66.08% versus 66.15% weighted warm reuse.

The actionable Desktop direction is to verify its actual /context window and
automatic-compaction threshold, then align them with the actual selected backend
capacity if a supported Desktop setting exists. Global CLI aliases already use
[1m], but Desktop requests observed before relay mapping use plain claude-opus-5.
Changing a model name AFTER the client compacts cannot restore discarded history.
Do not disable compaction or falsify usage. Reducing oversized tool outputs at
the source can reduce repeated compaction without rewriting retained history.

A similar Desktop/CLI window discrepancy is reported in Anthropic issue #81039,
on different versions and OS; it is corroborating context, not local proof.
Current documented window overrides also have model-ID-specific restrictions.

Sources:
- https://code.claude.com/docs/en/model-config#correct-the-window-for-a-gateway-or-custom-model-id
- https://code.claude.com/docs/en/desktop#cli-flag-equivalents
- https://github.com/anthropics/claude-code/issues/81039
- https://ai.google.dev/gemini-api/docs/generate-content/caching

## Reproduction

`python diagnostics/cache_drop_audit.py --output diagnostics/cache-drop-audit-20260921.json`

The script opens SQLite mode=ro plus query_only, emits only fingerprints, counts,
and local ordinal account/session labels, and never loads account credentials.
Synthetic regression coverage checks no DB modification, no prompt/token/identity
leak in output, auxiliary-history separation, account changes, count_tokens
exclusion, and missing usage distinction. Full local suite: 33 tests passed.
