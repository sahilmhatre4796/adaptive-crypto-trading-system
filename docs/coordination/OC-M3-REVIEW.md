# OpenCode read-only review request: completed M3 and M4 readiness

Task ID: OC-M3-REVIEW. Status: prepared for manual forwarding; NOT dispatched or executed. This is the next requested review; it does not authorize running the older OC-01 through OC-07 queue. Return one report and stop.

Project: C:/Users/sahil/Desktop/adaptive_trading_backtester

Baseline HEAD when prepared: 19d6f21e15243558b29cdf048fb5177d4f489729 on master. M0-M3 changes are uncommitted at preparation time. Codex remains the sole code writer. Use the user's existing hosted MiniMax M2.5 configuration; report the provider/model actually shown. Do not install anything, change models, or launch additional agents.

## Mandatory read-only boundary

Inspect only the files explicitly listed in OC-M3-REVIEW-MANIFEST.json, this request, and that manifest. Use read-only file access, including file hashing if supported. Do not write project files, reports, suggested patches, caches, snapshots, Git state, settings, credentials or dependency files. No tests, backtests, scripts, Python imports, shell execution, subprocesses, installations, account/exchange connections, benchmarks or network probes. Do not invoke any write-capable tool. If the environment cannot enforce read-only access, review supplied excerpts instead and report the limitation. Do not change permissions or work around restrictions.

Return the report in the OpenCode conversation. Suggested fixes must be plain text only. Do not begin another assignment. Do not send messages to external services or people.

## Source identity

The accompanying manifest pins exact working-tree bytes for implementation, tests, contracts and readiness documentation. Compare each allowed file with its SHA-256 before relying on it. A checkpoint commit changes Git metadata, not necessarily file contents: report the user-supplied checkpoint hash separately from the preparation baseline. If any file hash differs, stop and request a refreshed packet; do not silently review different code. If hashes cannot be computed using read-only access, label source identity unverified and request verified excerpts. Do not traverse .git, runs, caches, snapshots or unrelated directories.

The saved M3 test transcript reports 72 passing tests on 2026-11-08. This is historical evidence, not permission to rerun tests or claim independent execution. No historical market evaluation is available.

## Review questions

1. Geometry: audit all six families, twelve directional variants, neutral doji, candle validity, scale invariance and configurable threshold boundaries against docs/M3.md. Identify strict variant assumptions rather than treating a different textbook definition as an implementation defect. Check Hikkake inside bar, false break, first close confirmation, window expiry, cancellation precedence and two-sided ambiguity.
2. Context: separate geometry from prior trend, 4H regime, 1H direction, support/resistance, volatility and volume. Check completed candle alignment, availability, stale/unknown/conflicting evidence and required supplied levels. Verify that strength is a ranking rather than a probability and missing evidence cannot become favorable support.
3. Time and causality: audit open-time input, complete resampling, as-of filtering, delayed receipt, nondecreasing availability, prefix invariance and future context selection. Distinguish detection, confirmation, receipt and expiry. A later revision or appended future candle must not rewrite registered historical evidence.
4. Lifecycle: verify separate 5m confirmation starts after pattern availability, cannot reuse the formation's final sub-candle, rejects wrong asset/configuration and stale confirmation, applies invalidation before favorable confirmation, and enforces chronological observations, stable IDs, duplicates and terminal states. Identify in-memory versus durable guarantees accurately.
5. Exporter: review input/config boundaries, serialization, source/config hashes, effective cutoff, provenance, output overwrite prevention and error behavior. Confirm no order or risk approval path was introduced.
6. Tests: map the existing M3 tests to the above contracts. Give a minimal missing or failing fixture for each material finding. Preserve M0-M2 financial/timing assumptions; do not recommend weakening assertions to accommodate a bug. Do not execute tests.
7. M4 readiness: audit docs/M4-READINESS.md for unsupported licensing/connectivity claims, provider scope, original-source attribution, timestamp/first-seen/revision handling, dedupe, XML/HTTP boundaries, outage/freshness separation and benchmark design. Check that a single provider's two feeds are not described as independent fallback coverage. Verify that research/design is not represented as implementation or measurements. External policy statements remain cited claims unless independently inspected; no network access is authorized by this packet. Flag required independent licensing/connectivity verification explicitly.

## Required report

Use REPORT_TEMPLATE.md. Include actual provider/model, checkpoint hash supplied by the user (or UNKNOWN), reviewed file hashes, files inspected, tools/actions used, and explicit `Files changed: NONE` and `Tests/benchmarks executed: NONE` declarations if true.

For every finding give severity, file:line evidence, violated documented contract, a minimal numerical/timestamp counterexample, observed versus inferred behavior, practical impact, a text-only repair suggestion and an expected regression result. Separate critical/high defects from research assumptions and optional improvements. If no defect is established, say so without claiming proof of correctness or profitability. List coverage gaps and uncertainty.

End with an M3 review disposition: blocking findings, nonblocking findings, or insufficient evidence. Separately list M4 gates that remain open. A favorable code review does not verify provider access, grant content rights, secure a Git checkpoint or authorize M4 implementation. Codex will independently triage findings before changing code.
