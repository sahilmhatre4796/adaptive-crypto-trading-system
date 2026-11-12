# M6 audit corrections

Baseline: `c87f7609c47e95174658666648ed647ff9d7ff02` on master. OpenCode's supplied audit disposition is PASS WITH FINDINGS. No new independent audit is claimed.

The M5 review corrections were already pending approval: `docs/M5.md`, `tests/test_structure_review.py`, `docs/coordination/M5-REVIEW-TRIAGE.md` and `docs/coordination/M5-REVIEW-TESTS.txt`. Those bytes remain untouched by this task. Before edits, all 79 tracked/pending-review file hashes were recorded in ignored `runs/m6-review-preservation.json`.

## Decisions

| Finding | Correction / disposition |
| --- | --- |
| M6-01 | Implement `M6Bundle.from_dict()` for complete M6-v1 JSON-shaped exports. Require explicit supported schema, exact fields, correct scalar/array types, finite numeric values, nested dataclass construction/validation, output domains and causal bounds. Restore timestamp/tuple/configuration types, preserve numeric JSON representation, and retain existing bundle scope/mode checks. Reject future data and current-only records in BACKTEST payloads. Tests cover round trips including previous observations, events, CURRENT/BACKTEST, empty bundles, missing/stale/partial/transition states, malformed nested data, unsupported versions, future events and historical-eligibility violations. |
| M6-02 | Add a receipt-freshness regression without changing the heatmap engine. The fixture snapshot is fresh at candle open (age 600s) and normal close (age 900s), but stale at delayed receipt (age 1200s). The timely control produces ENTERING/SWEEP_REVERSAL; the delayed bar produces no price-interaction events. |
| M6-03 | Add BTC/ETH timeframe smoke checks using actual engine outputs. Regime accepts the five documented candle intervals including 4h/1d, with completion/cutoff checks, and rejects snapshot. Derivatives accepts those intervals plus snapshot and preserves observation scope/funding semantics; unsupported 1m is rejected. No provider-supported interval is fabricated. |
| M6-04 | Assess validation at the external wire boundary, retaining direct result dataclasses as trusted internal engine containers. Add necessary imported-data type/domain/causality checks under M6-01, without changing analysis-result constructors or recomputing/restricting valid engine outputs. Document that validation cannot authenticate provenance, historical flags, original input IDs or analytical correctness. The exact M6-04 text was not supplied; clarification was requested, so this is a trust-boundary assessment rather than a claim to reproduce an unspecified counterexample. |
| M6-05 / M6-07 | Deferred as instructed. No performance rewrite, provider contact, paid service, dependency installation, or live validation. |

## Scope

Only two original M6 files change: `trading/m6.py` for the required wire loader and `docs/M6.md` for its contract/trust boundary. Added `tests/test_m6_review.py`, this disposition and `M6-REVIEW-TESTS.txt`. Existing analysis engines, historical selection/freshness rules, M3-v2, M0–M5 implementations and tests remain unchanged. Schema stays M6-v1; no evidence semantics, probability claims, orders or M7 integration are added.

## Verification

Fresh targeted command: `python -B -m unittest discover -s tests -p 'test_m6*.py' -q`: **65 tests passed in 1.964 seconds** (55 original M6 tests plus 10 review tests).

Fresh complete command: `python -B -m unittest discover -s tests -v`, with `PYTHONDONTWRITEBYTECODE=1`: **172 tests passed in 14.772 seconds**. This includes all 157 committed tests, five pending M5-review regressions and ten new M6-review tests. Full dated output: `M6-REVIEW-TESTS.txt`. Earlier M6 implementation transcripts remain historical and unchanged.

Syntax/trailing-whitespace checks passed for both touched Python files; `git diff --check` passed. The complete correction diff was reviewed. Post-test byte verification passed for all **77 other pre-existing tracked/pending files**, including all four pending M5 files and all **12 unchanged original M6 files**. No M0–M5 implementation or original test changed. HEAD remains c87f760 and the index is empty. Working tree includes this task's two modifications/three additions alongside the four previously pending M5 files; it is intentionally not clean and no commit was made.

No commit is made by this task; the earlier M5 corrections remain pending approval separately. M6-04's trust boundary remains explicit, and provider/historical market validation is not claimed.
