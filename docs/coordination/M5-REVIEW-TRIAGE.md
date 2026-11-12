# M5 OpenCode review — Codex disposition

Audit disposition supplied by the user: PASS WITH FINDINGS. This record addresses the five supplied findings; it does not claim a new independent audit or additional OpenCode execution.

## Repository state and scope

Inspection began on clean `master` at **c87f7609c47e95174658666648ed647ff9d7ff02**. Prior commits remain intact: M0–M3 checkpoint 9442bc4, M5 cc607f2, M3 review corrections abcfbc9 and M6 c87f760. All 76 tracked files were byte-hashed before edits in the ignored local verification record `runs/m5-review-preservation.json`.

The M5 audit's observation of untracked M6 files predates c87f760 and is historical, not a current defect. This review changes tests/documentation only; no M0–M2, M3, M5 or M6 analysis implementation, scoring version, dependency or existing test assertion is changed. The user requested a proposed commit summary for approval; no commit or history rewrite is made.

## Finding-by-finding decisions

| Finding | Decision and evidence |
| --- | --- |
| M5-01 — pre-current-candle trend | Retain existing causal ordering. M5 processes zone events before confirming the current candle's newly knowable pivot and updating `trend`. Document this explicitly. Two new mirrored tests establish (a) a break against an existing BULLISH/BEARISH trend stays CHOCH even when the same close confirms a pivot changing the sequence to RANGE; (b) a break starting from UNKNOWN stays UNCLASSIFIED even when that same close establishes BULLISH/BEARISH. STRUCTURE_CHANGE follows zone events; earlier as-of output remains unchanged. |
| M5-02 — M3 handoff coverage | Accepted coverage gap. A new test passes actual `StructureResult.level_distances()` dictionaries directly into `ContextSnapshot`, then into M3 `assess_context`. Both directions cover ACTIVE, BROKEN, RETESTED, FAILED and EXPIRED, including zero distance inside an active zone. Fresh independent higher-timeframe context plus an eligible level gives VALIDATED; missing support/resistance gives INSUFFICIENT with no location bonus. Location-only snapshots remain INSUFFICIENT. No M3-v2 rule is weakened. |
| M5-03 — private imports | Retain `_bars` and `_number`; document the dependency and refactoring risk. `_bars` supplies M3's shared completed-candle/time validation; `_number` supplies finite/positive numeric validation and also has M3/M6 callers. There is no current behavior defect. Renaming/extracting or adding public aliases now would expand scope without changing behavior. `docs/M5.md` records exact import paths, signature expectations and the need to audit callers/run all M3/M5/M6 tests before future changes. |
| M5-04 — equal wick boundary | Accepted coverage gap. A mirrored test isolates `high == upper` / `low == lower` with a close already beyond the opposite zone edge, proving equality creates no SWEEP and leaves `swept=False`. A later strictly crossing wick emits exactly one sweep at that later candle, so the no-sweep result is attributable to equality rather than an unsatisfied close condition. |
| M5-05 — zero-range strict pivot | Accepted coverage gap, not an input error. A zero-range candle can be a unique high if neighboring candles are lower, or a unique low if they are higher. It produces a zero-width zone under the existing width formula. A mirrored regression verifies point-zone distance zero, deterministic IDs/replay, unchanged earlier as-of results, and valid BREAKOUT → RETEST → FAILED_BREAK transitions without division by zone width or a crash. Document the supported boundary. |

## Changed files

- `tests/test_structure_review.py`: five regression tests for the supplied findings; retains the original M5 fixtures/tests unchanged.
- `docs/M5.md`: clarifies trend ordering, exact wick equality, zero-width zones and private-helper dependencies; corrects historical review/M6 status references.
- `docs/coordination/M5-REVIEW-TRIAGE.md`: decisions, scope, verification and proposed commit summary.
- `docs/coordination/M5-REVIEW-TESTS.txt`: fresh complete-suite transcript, generated during this task.

## Verification

Fresh targeted command: `python -B -m unittest discover -s tests -p 'test_structure*.py' -v`, with child-process bytecode disabled. **27 tests passed in 0.522 seconds**: all original 22 M5 tests plus five new tests, including the M3 integration handoff.

Fresh complete command: `python -B -m unittest discover -s tests -v`, with `PYTHONDONTWRITEBYTECODE=1`. **162 tests passed in 12.955 seconds**. This includes all 157 pre-existing tests plus the five new review regressions; the transcript explicitly contains all **55 M6 tests**, **8 M3 review-fix tests**, and original **22 M5 tests** passing. Full fresh output: `M5-REVIEW-TESTS.txt`. No historical result is represented as fresh verification.

Post-test byte verification passed for **all 75 other pre-existing tracked files**, including every implementation, original test, snapshot and all 14 M6 files. Only `docs/M5.md` changed among previously tracked files. HEAD remains c87f760; the index is unchanged/empty. The complete documentation/test diff was reviewed, new test syntax/whitespace checked, and `git diff --check` passed (only informational LF/CRLF notices). No configured lint/type runner or dependency was added. Final working tree intentionally contains the four listed uncommitted files for approval: one tracked modification and three additions.

## Proposed commit, awaiting user approval

`test: close M5 review coverage gaps and clarify structure contracts`

Add M3 ContextSnapshot handoff, same-close trend ordering, equal-wick and zero-width-zone regressions. Document private M3 helper dependencies and established M5 boundaries. Preserve all analysis implementations, M3-v2 behavior and committed M6 files.

No commit has been made. No paid API, network/provider call, dependency change, deployment, live trading or paper trading was performed. Market performance and profitability remain untested by these deterministic fixtures.
