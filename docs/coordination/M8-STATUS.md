# M8 implementation handoff

Status: offline implementation and independent OpenCode audit complete: PASS WITH FINDINGS, safe to commit. The user authorized the seven-file M8 commit after M8-F01 documentation corrections and fresh verification. Codex remains the sole writer; OpenCode's audit was read-only. M7 received independent PASS and is already committed.

Pre-M8 commit baseline: master at `0e7c8b84ef540ad1c4774746ed84849893d8ddd4`. All 90 existing M0–M7 files are tracked, including eight committed M7 files. Preservation inventory: ignored `runs/m8-preservation.json` (90 files); M8-F01 pre-edit snapshot: ignored `runs/m8-f01-preservation.json`. Final checks compare the whole inventory, not only Git's tracked diff.

## Exact scope

| Added file | Responsibility |
| --- | --- |
| trading/portfolio.py | Decimal book, quotes, linear specs, reservations, synthetic position accounting, P&L/FX, equity limits' reference metrics, immutable journal. |
| trading/risk.py | Explicit risk/cost policy, isolated committed-M7 adapter, sizing/limits, consumption, deterministic decisions and terminal receipts; offline reservations and booking revalidation. |
| tests/test_portfolio.py | 13 accounting, causality, crossed-stop, cost, funding and immutable-replay tests. |
| tests/test_risk.py | 33 sizing, constraints, circuit-breaker, duplicate, replay and current-M7 boundary tests. |
| docs/M8.md | Input/output contracts, equations, policy assumptions, usage boundaries, causality, verification and limitations. |
| docs/coordination/M8-TESTS.txt | Fresh staged/targeted/full transcripts and final preservation/scope checks. |
| docs/coordination/M8-STATUS.md | This handoff, explicit review gates and exact scope. |

No M0–M7 implementation, test, document, configuration or dependency changed. M7 has no pending files. The authorized M8 commit contains exactly these seven additions. Before M8 staging, those seven additions are the only untracked paths; the index and tracked diff are empty. Commit hash and post-commit clean-state verification are reported after Git creates the commit.

## Verification

Historical implementation results: **46 tests passed in 1.396 seconds** and **273 full-suite tests passed in 28.596 seconds**, including the 227 pre-M8 tests. These precede M8-F01. Fresh targeted/full commit checks are appended separately to `M8-TESTS.txt`. Four Python AST/whitespace checks and complete source review passed; all 90 baseline file hashes match. No lint/type tooling is configured; none was installed.

Fresh M8-F01 commit checks: **46 targeted tests passed in 0.614 seconds; 273 complete-suite tests passed in 28.821 seconds**, with no failures or errors. All 90 tracked M0–M7 files remain byte-identical; portfolio source and both M8 test files match the audited bytes, and the risk AST is unchanged except for its updated docstring.

The last code review added a same-portfolio actor ownership guard, exact subsecond freshness handling and conservative crossed-stop valuation; the final suite was rerun after those changes. Tests reconcile modeled total stop loss with offline cash accounting in both directions and prevent already-paid funding debits from being counted twice.

## Policy and review disposition

- Default account INR 2,000; default per-trade planned-loss fraction 0.10 of current equity, initially INR 200. This is the user's aggressive experiment, not a recommendation or proven safe level. Other limits must be explicitly supplied.
- No exchange specs, fee schedules or FX rates are invented. Explicit point-in-time assumptions and FX observations are required. Correlation buckets are caller-defined conservative groups, not measured relationships.
- Committed M7 returns `SignalRecord`, not `SignalDecision`. Only M8's adapter knows the concrete M7 record/event types. M7 independent PASS and commit are confirmed; the complete causal terminal-event stream must still be supplied by the caller.
- Rejected and blocked formations are consumed; no implicit retry. Expired reservations require explicit cancellation or a terminal event. This is documented conservative offline policy.
- Single-threaded in-memory replay only. No durable restart store, partial exits, automatic fills/stops, venue liquidation/maintenance-margin emulator, settlement-rounding guarantee or empirical historical validation.
- No M4, M9/Freqtrade/Telegram, orders, accounts, external APIs, network, paid services, secrets, installations or paper-trading claims.

M8-F01 is documentation-only: update the stale M7-review docstring, M7 acceptance/commit references and pre-commit file counts. No functional corrections were requested or made. The independent M8 audit found no functional defects or blockers and cleared M8 for commit. Proceed with only the seven authorized files after fresh verification; do not start M9 or modify another milestone.
