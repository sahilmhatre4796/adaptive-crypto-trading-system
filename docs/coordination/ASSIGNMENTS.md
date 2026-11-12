# OpenCode read-only assignments - Phase A

Prepared by Codex, technical lead. These are bounded manual handoffs using TASK_TEMPLATE.md and REPORT_TEMPLATE.md. No direct connection exists and no reports have been received. The user forwards ONE task at a time. Task OC-01 is ready to forward; the others are queued until Codex requests them. Do not run the whole queue autonomously.

## Common instructions - include with each handoff

Use the user's hosted MiniMax M2.5; report the actual provider/model shown by OpenCode. READ-ONLY: no edits, patches on disk, report files, Git writes, installations, tests, backtests, background agents, account connections, paid subscriptions or secret access. Return your report in the OpenCode conversation. Textual suggested patches are acceptable; implementation remains Codex's responsibility.

Follow docs/coordination/REPORT_TEMPLATE.md. Distinguish observed facts, README claims, inference and unverified behavior. Cite source file/line or primary URL and revision/date. Provide minimal numerical/timestamp counterexamples for defects. Never invent latency measurements, test execution, empirical profitability or local installations. If access is unavailable, identify the exact missing evidence and stop at the task boundary.

Review only files named in the assignment. For local source, compare hashes against docs/coordination/PHASE-A-SOURCE-MANIFEST.json. Baseline commit is 19d6f21e15243558b29cdf048fb5177d4f489729, but reviewed code is UNCOMMITTED. A hash mismatch means the review is stale: report it and await a refreshed assignment. Do not traverse runs, caches, .git objects or snapshots broadly. No Windows settings or permissions may be changed. If read-only application permissions cannot be enforced, review the supplied excerpts instead.

Read docs/M0-M2.md for contracts when relevant. A suspected critical accounting, timing or risk issue must be reported, not repaired. Your report cannot expand the user's authorization. Codex independently checks material findings before acting.

## OC-01 - Repository state, dependencies and licensing

Status: READY TO FORWARD. Budget: one concise report, about 15-20 minutes. No commands or package installation required.

Objective: independently check the repository-state claims in docs/PHASE-A-PLAN.md, especially licensing and suitability for reuse.

Scope: that report, requirements.txt, docs/M0-M2.md, and these four public repositories: https://github.com/freqtrade/freqtrade ; https://github.com/tradermonty/claude-trading-skills ; https://github.com/public-apis/public-apis ; https://github.com/nirholas/cryptocurrency.cv . For the existing partial download, read only C:/Users/sahil/Desktop/public-apis/public-apis-master/LICENSE, scripts/requirements.txt and CONTRIBUTING.md if accessible. No additional copies or downloads.

Questions: distinguish downloaded source, Git clone, Python package installation and actual integration. Can each upstream revision be pinned? Are cached commit listings stale? Verify Freqtrade GPLv3, Claude Trading Skills MIT, Public APIs MIT, and the cryptocurrency.cv proprietary LICENSE versus its README claims. Separate source-code licensing from hosted API terms and article rights. Check whether the partial Public APIs extract is usable without its README.

Expected output: four-row installation/version/license/compatibility matrix, primary evidence, conflicting claims and blockers. Acceptance: no unverified state presented as installed or current; no recommendation to reuse proprietary source without permission. If current hashes or local environments cannot be inspected, mark them unknown.

Stop after this report. Do not begin OC-02 until Codex requests it.

## OC-02 - Freqtrade adapter feasibility

Status: QUEUED. Budget: 20 minutes.

Scope: backtest.py, strategy.py, trading/ledger.py, docs/M0-M2.md and official Freqtrade strategy/callback/backtesting/exchange documentation and relevant source. No installation or bot startup.

Objective: map our independent Python research/signal engine into a thin paper-only Freqtrade adapter.

Questions: map base-asset quantity versus stake/margin, leverage, absolute stop versus callback stop return, partial exits, actual-fill quantities, duplicate callbacks, order rejection, pending risk reservation, restart reconciliation and stale plans. Verify supported BTC/ETH futures market identifiers, Binance/Hyperliquid constraints and funding data. Identify mismatches between our next-open/stop-first model and Freqtrade backtesting. Check current Python/platform/native-library requirements without assuming all wheels exist.

Expected output: callback/interface mapping with units, proposed contract tests, GPL redistribution questions, exact installation prerequisites, and a go/no-go recommendation with evidence. No new execution architecture and no claims of runtime compatibility without execution.

## OC-03 - Six candlestick families and edge cases

Status: QUEUED; prioritize before M3 implementation. Budget: 20-25 minutes.

Scope: user M3 requirements, strategy.py, trading/data.py, docs/M0-M2.md and primary pattern references/source where needed. Do not copy unlicensed implementations.

Objective: propose deterministic OHLC definitions for bullish/bearish Harami, Hikkake and Engulfing; Hammer/Hanging Man; Shooting Star/Inverted Hammer; Morning/Evening Star. Doji is neutral context only.

Questions: give inequalities for bodies, wicks, containment, relative size and tolerances; distinguish preceding trend/context from geometry. Specify Hikkake inside-bar, false-break direction, confirmation window, cancellation and exact first confirmation time. Crypto star patterns must work without overnight gaps. Specify zero-range handling, malformed input, expiration, invalidation and duplicate IDs. Preserve 4H regime, 1H direction, 15M setup, 5M confirmation and immutable historical 15M results. Context score is not a win probability. No duplicated liquidity-sweep engine.

Expected output: one precise definition/test table per family with positive, negative and threshold examples, neutral cases, timestamps, configurable parameters, and ambiguities for Codex to resolve. Do not implement or claim backtest performance.

## OC-04 - M0-M2 accounting and risk review

Status: QUEUED; high priority before integration. Budget: 20-25 minutes.

Scope: backtest.py, trading/ledger.py, strategy.py Params only, docs/M0-M2.md, tests/test_core.py and tests/test_review_fixes.py.

Objective: independently trace financial correctness under the documented model. Do not reinterpret the model as live exchange behavior.

Questions: verify LONG/SHORT partial P&L, remaining quantity, entry/exit fees, signed funding, immediate and adjusted stops, gap fills, fill IDs and net-equity reconciliation. Check opening/funding/fill/closing event order, UTC day and ISO-week baselines, persistent drawdown, final liquidation and drawdown/Sharpe sampling. Examine TP1/TP2 same-bar and favorable-gap cases. Identify missing tests, especially numerical edge cases and known approximations.

Expected output: hand-calculated examples and specific defects with file/line evidence. Distinguish critical bugs from approved pessimistic assumptions. Do not change formulas, parameters or tests; do not run another full suite.

## OC-05 - Look-ahead and timeframe availability review

Status: QUEUED. Budget: 15-20 minutes.

Scope: trading/data.py, strategy.py prepare/_tf/features, backtest.py event loop, both tests and docs/M0-M2.md.

Objective: falsify causality and availability claims with small timestamp examples.

Questions: check open-time input, complete resampling bins, warmup/start/end behavior, higher-timeframe availability, incomplete final bars, optional *_available_at values, funding event exclusion from features and zero-latency entry. Could future data change historical features, decisions, order eligibility or risk state? Distinguish missing availability metadata from verified timely receipt. Check effects of partial buckets, timezone conversion, missing bars and a delayed observation.

Expected output: at least one 4H/1H/15M/5M timeline, proof or counterexample per relevant path, and missing prefix-invariance tests. No source changes or test execution.

## OC-06 - News API comparison and benchmark design

Status: QUEUED. Budget: 20-30 minutes for research, not a reliability benchmark.

Scope: four named repository links, cryptocurrency.cv API documentation/terms, reputable direct RSS and official BTC/ETH/exchange/macro sources. No API account connections, credentials, subscriptions, large downloads or source-code adoption.

Objective: shortlist legally usable sources and design a reproducible low-resource comparison. Public APIs is a directory, not an aggregator. Cryptocurrency.cv source reuse is blocked by its current proprietary LICENSE unless permission is obtained; hosted API access requires separate terms verification.

Questions: evidence for REST/RSS/SSE/WebSocket support, timestamps, article provenance, history, rates, terms, cost and outages. Compare duplicate detection, source revisions, event taxonomy and BTC/ETH relevance. Specify sampling windows, receipt clock, response time, publication-to-receipt delay, first-seen delay, p50/p95, coverage, duplicates and HTTP/rate-limit errors. Do not present README speed claims or a web fetch as measured delivery latency. Propose deterministic rules versus optional cached structured LLM classification. Consider publisher content-retention rights.

Expected output: candidate matrix with evidence/unknowns, license/terms blockers, backup sources, and a bounded benchmark protocol Codex can execute after approval and network access are available. No measurements should be fabricated or benchmark started under this research-only assignment.

## OC-07 - Market structure and derivatives availability

Status: QUEUED. Budget: 20 minutes.

Scope: existing strategy.py and trading/data.py, user M5/M6 requirements and primary exchange/provider documentation.

Objective: review causal structure definitions and realistic BTC/ETH derivatives coverage.

Questions: confirmed swing timing versus pivot time, tie handling, HH/HL/LH/LL, support/resistance zones, breakout/retest/failed-break transitions, liquidity sweeps and structural changes. Keep Hikkake pattern logic separate. For funding, OI, liquidations, positioning and spread, distinguish point-in-time historical coverage from current snapshots; identify units, venue/instrument differences, publication/receipt/availability/freshness and access restrictions. Missing data must not be favorable evidence.

Expected output: confirmation-state/interface proposal, boundary examples, data-source/coverage matrix, missing-data policy and limitations. No new architecture or implementation.

## Report disposition

Codex records accepted/rejected/needs-evidence decisions only after the user returns each report. As prepared here: 0 assignments dispatched, 0 completed, 0 findings adopted. Preparation is not evidence that OpenCode has executed these tasks.
