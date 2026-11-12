# M4 news pipeline readiness

Resumed: 2026-11-12. Status: research/design only; M4 implementation and provider benchmark have not started. Read alongside coordination/M3-STATUS.md and PHASE-A-PLAN.md. This document does not supersede either source manifest.

## Recovered state

All 11 Phase A and six M3 source SHA-256 values match their saved manifests. `git diff --check` passes, with existing LF/CRLF warnings. HEAD remains 19d6f21e15243558b29cdf048fb5177d4f489729; the M0-M3 checkpoint is still absent from this checkout. No OpenCode review report is recorded. The existing 72-test result is from November 8, not a new run. No implementation changed during this readiness work.

## First provider candidate

Use the Board of Governors' monetary-policy RSS as the first *candidate* for a macro-only vertical slice. It is an original source and requires no account credentials. It supplies neither comprehensive crypto news nor a BTC/ETH directional signal.

| Evidence | Verified on 2026-11-12 | Limit |
| --- | --- | --- |
| [Official feed directory](https://www.federalreserve.gov/feeds/feeds.htm) | Publishes monetary-policy and all-press-release RSS links; describes reader/aggregator use | Does not establish an uptime SLA, delivery delay or request quota |
| [Board disclaimer](https://www.federalreserve.gov/disclaimer.htm) | Board information is generally public domain unless otherwise indicated; attribution requested | Third-party material needs separate permission; seals/logos are restricted |
| [Monetary-policy RSS](https://www.federalreserve.gov/feeds/press_monetary.xml) | Endpoint linked by the official directory | Browser fetch reports unsupported text/xml; body, live schema and runtime access remain unverified |
| [All press releases](https://www.federalreserve.gov/feeds/press_all.xml) | Endpoint linked by the same directory | Same provider, so this is overlapping coverage, not an independent outage fallback |

Scope retention to original Board feed metadata and permitted text, with explicit attribution and policy URL. Do not scrape linked third-party articles or reuse branding. Do not generalize this policy to regional Reserve Banks or other publishers. Cryptocurrency.cv has not been re-evaluated or adopted; its previously recorded source-license blocker remains unresolved.

## Smallest implementation after readiness gates

One bounded collector, one normalized record format and SQLite storage. Use the standard library and existing project conventions; no LLM, framework, scheduler, background service or strategy integration is needed for the first slice.

- Collector accepts only the configured HTTPS feed. Set timeout and response-byte limits, verify HTTP success, disable cross-host redirects, reject DTD/entity declarations and parse supported RSS fields as plain text. Never fetch item links automatically. HTML in descriptions is untrusted data.
- Store source ID, source feed URL, original item URL/GUID, title, permitted summary, publisher timestamp, receipt timestamp, first-seen timestamp, content hash, attribution/policy reference and parser version. Preserve the actual response status and observation time separately from items.
- Use source plus GUID as identity, falling back to source plus canonical original URL. Reject items lacking both. Keep the original URL and GUID unchanged alongside any normalized key. Avoid title-only merging or cross-source identity claims.
- Append changed content as revisions under the same identity. Preserve first-seen time and every revision's receipt time. Repeated identical content is a duplicate observation, not a new event. Commit a response's accepted records and observation atomically.
- Availability is the local receipt time of that revision, never its older publication time. A historical feed fetched today cannot be inserted into an earlier backtest as if it had been seen then. Missing/invalid/future publication time remains explicit uncertainty.
- Mark monetary-policy feed items as MACRO / MONETARY_POLICY. BTC/ETH entity mentions require explicit text evidence; macro exposure alone is not an entity mention. Default direction is UNKNOWN, not bullish/bearish sentiment. A revision is not automatically a contradiction.
- Store poll health separately from item freshness. A successful empty or unchanged feed is not an outage; an HTTP failure cannot refresh old evidence. Missing, stale and uncertain evidence contributes no favorable signal. Require explicit freshness configuration before any later signal consumer is enabled.

## Focused acceptance cases

| Fixture | Required result |
| --- | --- |
| One valid RSS item | Original-source provenance and UTC receipt time retained |
| Same item twice, then changed title/summary | One identity, two revisions, immutable first-seen time |
| Delayed publication; later revision | Each revision unavailable before its own receipt time |
| Missing/malformed/future publication date | Uncertain timestamp; never silently replaced with receipt as publication |
| Empty or unchanged successful response | Healthy poll; no fabricated event or refreshed item age |
| Timeout, 429, 5xx, malformed XML, oversized response | Failed observation; stored evidence retained; no success timestamp |
| DTD/entity payload; external redirect; HTML description | Rejected or inert text; no secondary network requests |
| Different sources with matching headlines | Separate provenance; no title-only dedupe |
| Restart and repeated response | Same identities and revision counts; original first-seen time preserved |
| Future records appended | Historical as-of query returns identical earlier evidence |

These are proposed checks, not executed tests. Contradiction classification and independent crypto-provider coverage remain additional M4 work; this slice cannot satisfy the whole milestone.

## Bounded measurement protocol, not yet executed

First confirm runtime HTTP access with one capped fetch, preserving status, content type, receipt time and parse outcome. That is a connectivity/schema check, not a latency benchmark. Then, after verifying provider polling policy and benchmark authorization, record a fixed observation window and polling cadence before sampling. Do not start with an invented request quota. Use conditional requests only if supported, honor Retry-After, and stop/back off on rate limits.

Measure request duration with a monotonic clock; retain UTC request/receipt times for availability. Report sample count, duration p50/p95, errors by status, bytes, duplicates and revisions. Report publication-to-first-receipt age only where publication time is valid, separating pre-existing items from newly observed releases. Polling and publisher clock precision limit this measurement; it is not true wire delivery latency. No new release in the window means release-delay percentiles are unavailable. Do not claim historical coverage, uptime or independent provider comparison from a single fetch/window.

## Remaining gates

The saved plan requires checkpoint readiness and returned review findings to be triaged before further implementation. The user has been asked whether either happened outside this checkout. No Git write, permission change, installation, benchmark, account connection or trading action was attempted. Next: resolve the checkpoint/review handoff, verify runtime RSS access and polling policy, then implement the isolated collector/storage slice with the focused fixtures above. Paper readiness remains NO.
