# OC-M3-REVIEW — Codex disposition

Source: completed review supplied by the user in this conversation on 2026-11-12 (pasted twice; one report). Reported reviewer: MiMo-V2.6-Flash Free, provider `opencode`, model `mimo-v2.6-flash-free`, rather than the requested MiniMax configuration. This is reviewer-reported metadata, not a new independently executed external review. The reviewer ran no tests or network checks.

## Source identity and checkpoint correction

Before changes, Codex freshly verified all **15/15** hashes in `OC-M3-REVIEW-MANIFEST.json` against the actual checkout. The report therefore applies to the M3 bytes committed in **9442bc4357af28e503973361314d0adacda5decd**, even though its packet retained the original baseline/UNKNOWN-checkpoint wording. M5 is already implemented separately in **cc607f2eb1d9ea04cfd9f452d9223b41d7a5b9cd**. Neither commit is rewritten. The pasted report's `trading/init.py` spelling refers to the manifest's actual `trading/__init__.py`.

The report's pending-checkpoint statements are historical and superseded. Review receipt and triage are now complete; this does not clear M4's connectivity, licensing/polling-policy or benchmark gates, nor authorize M4.

## Findings and reproduced behavior

| Finding | Decision | Evidence and implemented/retained behavior |
| --- | --- | --- |
| F-1 | Accepted and fixed | Fresh reproduction with neutral wick geometry, neutral prior direction and BULLISH/RANGE context returned REJECTED with `higher_direction_conflict`, while granting regime support. The documented neutral geometry lacks a direction to compare. Neutral patterns now skip ordinary higher-direction/regime comparisons and bonuses, yielding INSUFFICIENT with missing preceding direction/location. Fresh ABNORMAL regime and independent volume/volatility rejection remain effective. Neither neutral geometry nor doji can confirm entry. |
| F-2 | Accepted and fixed | Fresh reproduction of the supplied stale 1h/4h fixture returned INSUFFICIENT but still listed both groups as supporting: score **94.80000000000001**, versus **79.80000000000001** with both groups UNKNOWN. Each stale group now takes an exclusive missing branch: zero bonus and zero conflict penalty. An independently fresh group is still evaluated. Existing strict `age > interval` freshness boundary is retained; the exact boundary and one nanosecond beyond it are tested. |
| F-3 | Retain behavior; document intent | For a directional pattern, fresh known NEUTRAL 1h bias and RANGE 4h regime remain permitted, non-conflicting context and retain +10/+5 ranking bonuses. The contract requires known/non-conflicting context rather than mandatory aligned direction. Changing this would introduce a new strategy preference without market evidence. Bonuses describe compatibility, not directional certainty; UNKNOWN/UNCERTAIN/stale groups earn none. Tests pin VALIDATED status and equality with aligned context scores in both directions. This rule never makes a neutral pattern directional. |
| F-4 | Retain per-event chronology; clarify scope | The tracker has separate histories keyed by event ID and no shared portfolio state. Its clock is intentionally per event: reject decreasing decision times for the same event before state changes; allow another event to be replayed at an earlier decision time. No global clock is added. Documentation/docstring now state this explicitly. Regression checks cover same-event rejection, cross-event acceptance, duplicate behavior and unchanged state after the error. |

F-1/F-2 change context labels/rankings, without changing geometry, entry price tests, expiry, risk or order behavior. `PatternEngine` now identifies this scoring algorithm as **M3-v2**, so new exports cannot silently reuse M3-v1 configuration/event IDs with changed scores. Old manifests and transcripts are preserved as evidence of the reviewed version. IDs remain deterministic within a version; this does not provide cross-version order deduplication or persistence.

## Coverage gaps

1. Chronological violation: closed by the F-4 regression, including cross-event policy and error atomicity.
2. Neutral REJECTED versus INSUFFICIENT: closed for both wick families, both missing/neutral prior states, and BULLISH/BEARISH/NEUTRAL bias with directional/RANGE regimes. Independent abnormal/weak-volume rejection is also pinned.
3. Stale score/support: closed independently for stale 1h, stale 4h and both, including formerly matching and conflicting values and stale ABNORMAL regime. Scores/support/conflicts match an otherwise identical UNKNOWN-group baseline. Exact freshness boundaries are covered.
4. Exporter configuration errors: closed with subprocess checks for invalid top-level type/key (ValueError) and invalid nested geometry/context key (TypeError), nonzero exit and absent output directory. The existing exception types are retained: the contract requires dataclass-compatible configs, not a polished CLI error format. No exporter source change is needed.

Eight new regression tests are in `tests/test_m3_review.py`; existing M3, M0–M2 and M5 assertions remain intact. Full-suite execution and preservation results are recorded below after the run. No independent review of unlisted modules or new external-provider claim is inferred from OpenCode's static inspection.

## Verification and scope

Fresh verification on 2026-11-12:

- Focused `python -B -m unittest discover -s tests -p test_m3_review.py -v`: **8 tests passed in 4.399 seconds**.
- One consolidated `python -B -m unittest discover -s tests -v`, with `PYTHONDONTWRITEBYTECODE=1` inherited by subprocesses: **102 tests passed in 11.474 seconds**. This includes all existing 94 tests plus the eight new review regressions. Full fresh transcript: `M3-REVIEW-FIX-TESTS.txt`. No historical pass count is presented as a fresh result.
- `git diff --check` passed; LF/CRLF conversion notices are informational.
- All 11 Phase A M0–M2 source/document hashes remain exact. All five M5 files, both snapshot trees, original M3 tests, geometry source and exporter match the pre-fix checkpoint. Only `trading/pattern_context.py`, `trading/pattern_engine.py` and `docs/M3.md` changed among existing tracked files.
- Added `tests/test_m3_review.py`, this triage record and the fresh test transcript. No existing assertion was weakened or removed. The separate review-fix commit and observed final Git status are reported in the final response; 9442bc4 and cc607f2 remain ancestors.

No M4 work, provider probes, installations, additional agents, model changes, accounts, live trading or paper trading were started. Historical market performance, profitability and threshold calibration remain unverified; this task verifies deterministic offline contracts only.
