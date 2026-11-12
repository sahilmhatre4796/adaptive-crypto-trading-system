# Phase A verification and Phase B execution plan

Status: preparation only. Awaiting user approval before implementation. No trading implementation files, configuration, packages, repository copies, accounts or Windows settings were changed in Phase A. Only this report, coordination assignments, source manifest and test transcript were prepared; the existing test suite also generated its usual small ignored CLI artifacts.

## Verified local state

Project: C:/Users/sahil/Desktop/adaptive_trading_backtester

Baseline commit: 19d6f21e15243558b29cdf048fb5177d4f489729 on master. M0-M2 repairs are UNCOMMITTED. The original snapshot's seven files, including bytecode, match that commit byte for byte. A second pre-review-fix snapshot is preserved separately. The source hashes in coordination/PHASE-A-SOURCE-MANIFEST.json identify the actual reviewed implementation, not just the older HEAD.

Inspected: README.md, docs/M0-M2.md, backtest.py, strategy.py, download_binance.py, all three trading source files, both current test files, original snapshot inventory and byte content, coordination documents, dependency declarations, Git state, root configuration/artifact inventory and synthetic runs.

Existing tree (no hidden application modules were found):

```text
backtest.py                  research CLI and chronological simulator
strategy.py                  existing indicators, features, baseline strategy and Params
download_binance.py          public Binance candle download utility
requirements.txt             pandas, NumPy, PyArrow minimums
trading/{data,ledger}.py      validation, availability, ledger and risk halts
tests/test_core.py           original 15 regression tests
tests/test_review_fixes.py   21 independent-review regression tests
docs/M0-M2.md                preserved execution/data contracts
docs/coordination/           manual OpenCode handoff documents
snapshots/original/          original immutable source and cached bytecode
snapshots/m0-m2-before-review-fixes/
runs/test-*/                 synthetic CLI test inputs and outputs
test_backtester.py           preserved legacy script; not part of current suite
```

No data/ directory or genuine historical BTC/ETH datasets/research performance reports were found in this checkout. runs contains synthetic test reports (zero-trade CLI examples); these are not market validation. No settings files containing a complete application configuration exist: current research configuration is the typed Params dataclass and CLI options. The README's breakout/retest wording exceeds actual logic: there is no dedicated retest or market-structure engine.

Implemented: contiguous 5m validation, UTC timing, completed 15m/1h/4h resampling, next-open simulation, partial exits, costs, explicit funding settlement events, marked equity, conservative stops/gaps, daily/weekly/hard halts, event drawdown and reconciliation IDs. Baseline risk remains 0.25%, fee 4 bps, slippage 2 bps, leverage cap 2. No parameter tuning was done.

Not implemented: six-family candlestick engine, dedicated news/structure/derivatives/regime services, new confluence, portfolio risk/reservations, paper-trading loop, Freqtrade adapter, Telegram or deployment. Existing coarse regime/scoring rules and optional derivative/news input columns are research hooks, not those finished systems.

Known limitations retained for later approved work: RSI has a zero-loss edge case, min_rr is not enforced, missing optional inputs use legacy defaults, historical receipt times are assumed when metadata is absent, and liquidation/exchange precision are not modeled. No new critical accounting defect was established in this preparation; independent OpenCode reviews remain pending.

## Tests and dependencies

Final logged command: `python -B -m unittest discover -s tests -v` with PYTHONDONTWRITEBYTECODE=1 for subprocesses. Result: 36 tests passed in 2.518 seconds. `git diff --check` passed, with only Git's LF/CRLF conversion warnings. Transcript: coordination/PHASE-A-TESTS.txt. An initial verification was included in an overlarge combined inspection output; the final run was recorded separately to make its result inspectable. No further suite repetition was performed during document preparation.

Python 3.13.7. Current interpreter's packages live in C:/Users/sahil/AppData/Local/Programs/Python/Python313/Lib/site-packages:

| Package | Installed version |
| --- | --- |
| pandas | 3.0.5 |
| numpy | 2.5.2 |
| pyarrow | 25.0.1 |
| requests | 2.34.2 |
| SQLAlchemy | 2.0.52 |
| pytest | 9.1.1 |
| freqtrade, ccxt, TA-Lib | NOT INSTALLED |

No dependencies were installed by this preparation. requests is imported by the existing downloader but missing from requirements.txt; declare it in a future approved reproducibility change, without reinstalling it unnecessarily. Tests use standard-library unittest. SQLAlchemy's presence does not mean Freqtrade is installed. M3 requires no new package. A small M4 can use existing HTTP support plus standard-library XML/JSON/hash/SQLite; optional LLM/provider integrations should remain separate dependencies. Freqtrade gets its own reviewed environment later, not a global upgrade of this research environment. Node/npm are on PATH; Docker was not found on PATH and its installation was not otherwise established.

## Four-repository state

Local search scope: the entire project tree and named repository directories up to two descendant levels under Desktop, Downloads, Documents, Projects, repos, source/repos and GitHub where present. This is not a claim about every disk, renamed checkout or separate interpreter. Nothing was cloned/downloaded in Phase A; no duplicate was created.

| Repository | Downloaded/cloned | Exact local directory | Local commit/version | Dependencies installed for repository | Actually integrated | Research only |
| --- | --- | --- | --- | --- | --- | --- |
| freqtrade/freqtrade | NO found | None found in searched scope | N/A; not installed | NO complete installation | NONE | Strategy/callback/data/exchange architecture and license |
| tradermonty/claude-trading-skills | NO found | None found in searched scope | N/A | NO repository environment established | NONE | Workflows, named skills, index and license |
| public-apis/public-apis | YES partial extracted files; NO Git clone | C:/Users/sahil/Desktop/public-apis/public-apis-master | UNKNOWN: no .git, no README, no release metadata | NO matching pinned script environment established | NONE | Local license/scripts requirements and public directory |
| nirholas/cryptocurrency.cv | NO found | None found in searched scope | N/A | NO repository installation | NONE | Repository/API claims, manifest and actual license |

Current remote head SHAs could NOT be reliably pinned. GitHub API access through the browsing tool failed; direct Python requests were refused (WinError 10061); cryptocurrency.cv's commit-history request returned HTTP 429. Cached commit-history pages for the other projects were visibly older than current source metadata, so they are not presented as current heads. OpenCode OC-01 should attempt independent verification from its permitted environment. Pin an immutable revision before any future source reuse.

### Freqtrade

Observed stable source version: 2026.10, not a local installation. Its manifest requires Python >=3.12, NumPy >2,<3 and pandas >=2.2,<4, so our interpreter/core versions meet those declared ranges. This does not establish runtime compatibility: CCXT and TA-Lib are absent, and native wheels/platform behavior remain untested. Use a thin strategy/callback adapter after an isolated installation check. GPLv3 obligations matter if a combined/modified product is redistributed; separating a process is not automatically a licensing exemption. No FreqAI training stack is justified initially.

Evidence: [version](https://raw.githubusercontent.com/freqtrade/freqtrade/stable/freqtrade/__init__.py), [manifest](https://raw.githubusercontent.com/freqtrade/freqtrade/stable/pyproject.toml), [license](https://raw.githubusercontent.com/freqtrade/freqtrade/stable/LICENSE), [interface](https://www.freqtrade.io/en/stable/strategy-customization/), [exchange notes](https://www.freqtrade.io/en/stable/exchanges/).

### Claude Trading Skills

This is a mixed toolkit of instructions/workflows and selected helpers, not an imported Python execution engine. No skill has been installed into this project. The MIT license permits adaptation subject to retaining notices; upstream data/provider terms still apply. Claude-specific tool names/workflows, equity focus and stock sizing assumptions require adaptation. Each helper has its own dependencies and validation needs; there is no verified whole-repository runtime environment here.

Evidence: [repository](https://github.com/tradermonty/claude-trading-skills), [index](https://github.com/tradermonty/claude-trading-skills/blob/main/skills-index.yaml), [license](https://raw.githubusercontent.com/tradermonty/claude-trading-skills/main/LICENSE).

### Public APIs

The existing partial extract contains LICENSE, CONTRIBUTING.md, .gitattributes, .gitignore and two scripts files, but no API catalog README or Git metadata. It is not an installed service or news aggregator. MIT covers the directory, not each provider's API/content. Its script requirements pin requests 2.27.1, urllib3 1.26.8 and other 2021-era versions; the current environment uses newer versions, so do not downgrade the project to run directory-maintenance scripts. Those scripts are unnecessary for provider discovery.

Evidence: named local files and [upstream repository](https://github.com/public-apis/public-apis), [license](https://raw.githubusercontent.com/public-apis/public-apis/master/LICENSE).

### cryptocurrency.cv

Important adoption blocker: the current LICENSE is proprietary and withholds reuse/copy/modification/distribution rights absent written permission. Do not clone, copy or self-host its application based on README open-source claims. Its hosted API terms and third-party article rights need separate verification. Observed package.json version is 1.0.2 (not a verified current release); it declares Node >=20 and a large Next.js stack, which is unnecessary for consuming an approved API. No endpoint latency, coverage, reliability or historical suitability has been benchmarked. REST/RSS/SSE/WebSocket descriptions remain provider claims until verified.

Evidence: [actual license](https://github.com/nirholas/cryptocurrency.cv/blob/main/LICENSE), [raw license](https://raw.githubusercontent.com/nirholas/cryptocurrency.cv/main/LICENSE), [manifest](https://raw.githubusercontent.com/nirholas/cryptocurrency.cv/main/package.json), [repository](https://github.com/nirholas/cryptocurrency.cv).

## Git checkpoint diagnosis and strategy

There is no .git/index.lock and no Git process was visible during inspection. Git history/status/index remain readable. .git and .git/index ACLs contain explicit write-deny entries for sandbox principals, while the declared workspace policy separately grants only read access to .git. The normal account owner has FullControl, which does not remove an applicable deny for this sandbox. This is a permission boundary, not a stale-lock diagnosis. No lock was deleted, ACL changed, Git data overwritten or security workaround attempted.

Required action before new implementation checkpoints: either the user makes the commit from their normal authorized Windows terminal, or the task execution policy is explicitly changed through the environment's supported controls to permit Git writes. Do not manually edit ACLs or relocate Git storage to evade restrictions. If the user's normal terminal is also denied, report the error and obtain a supported permission change.

Suggested reviewed checkpoint commands from an authorized terminal in the project:

```powershell
git status --short
git diff --check
git add -- .gitignore README.md backtest.py strategy.py trading tests docs snapshots/m0-m2-before-review-fixes
git diff --cached --stat
git commit -m "Checkpoint verified M0-M2 and Phase A planning"
git rev-parse HEAD
```

Review staged changes before commit. Use the user's Git author identity. runs and caches remain ignored; the already-tracked original snapshot must have no diff. Until a checkpoint is made, source hashes and verified copies protect the handoff but do not satisfy the requested per-milestone Git commit requirement. Git checkpoints are a readiness gate for Phase C.

## OpenCode workflow

Use coordination/ASSIGNMENTS.md with the existing templates. OC-01 is ready for the user to forward; six more bounded packets are queued. Run one at a time. No direct communication, reports or completed assignments are claimed. Initial priority after repository verification: independent M0-M2 accounting/timing review and M3 definitions. Codex reviews and reproduces findings; MiniMax reports are not accepted as mathematical proof.

## Proposed implementation order after approval

Preserve the public entry points, baseline strategy and M0-M2 contracts. New detectors/evidence modules remain separate; no signal can bypass independent risk. Maintain 4H regime, 1H bias, 15M setup/patterns and 5M confirmation. Critical M0-M2 defects trigger a report/contract decision before changes.

| Milestone | Deliverable / likely module boundaries | Acceptance gate |
| --- | --- | --- |
| Readiness | Git checkpoint, immutable source manifest, returned review findings triaged | No unresolved critical accounting/timing/risk issue |
| M3 | trading/patterns.py: pure OHLC six-family geometry, Hikkake confirmation lifecycle, event IDs/timestamps/expiry/invalidation; separate context validation | LONG/SHORT/neutral fixtures, thresholds, malformed/zero-range input, continuous markets, confirmation timing, duplicates, warmed prefix invariance; all 36 retained |
| M4 | trading/news/: selected lawful feed adapter, original-source provenance, normalization, dedupe, asset/event classification, SQLite records, freshness/contradictions; optional cached schema-validated LLM adapter only if justified | Offline fixtures, outage/late/revised/duplicate/uncertain cases; actual bounded provider measurements before selection; content/API rights verified |
| M5 | trading/structure.py: confirmed swings, structural sequences, zones, breakout/retest/failure/sweep states | Pivot time versus confirmation time, ties, false breaks, no repainting; no duplicate Hikkake detector |
| M6 | trading/regime.py and derivatives.py, reusing existing indicators | Train-only thresholds, persistence, external units/availability/coverage/freshness; unknown is not favorable evidence |
| M7 | trading/signals.py: bounded evidence groups, contradictions, expiry and duplicate prevention | Deterministic LONG/SHORT/NO TRADE explanations; group removal tests; no probability claim |
| M8 | trading/risk.py: portfolio exposure, reserved risk, sizing/cost/margin/precision, plan validation | Independent approval, concurrent BTC/ETH cases, rounding down, duplicate plans, halts and restart state |
| M9 + Telegram | integrations/freqtrade/ thin adapter, verified quantities/stops/fills and dry-run-only launcher; reuse protected notifications/controls | Isolated compatibility pass; callback replay/reconciliation; actual fills; unauthorized Telegram users denied; no forced entries or live mode |
| M10 | research evaluation, benchmark/metric extensions and final end-to-end validation | Each pattern and selected combinations, held-out data, costs/funding, walk-forward, sensitivity, prefix invariance and stability; report all missing coverage |

M3's context interface can consume existing coarse regime/direction and valid volume/volatility observations. Support/resistance is explicitly unavailable until M5 supplies it; it must not be fabricated. Pattern geometry can be complete independently of those later contexts. Pattern strength is a ranking, not success probability. Doji cannot create directional candidates. Historical per-pattern evaluation remains pending real data at M10; unit fixtures do not substitute for it.

Deterministic Python owns data/time, pattern geometry, structure, regime rules, event freshness/source policies, confluence, all risk and execution. Optional hosted LLM processing is confined to cached news entities/classification/summaries/conflicts; its strict-schema output is untrusted evidence. No model call occurs in order callbacks. Current Codex model remains selected; no model or thinking-level upgrade is proposed.

The Freqtrade adapter should call shared deterministic logic, not rely on remote force-entry commands. It must translate quantity/stake/leverage/stop units, freeze approved plans, reconcile actual fills, reserve shared risk and reject stale/duplicate plans. The project launcher must refuse dry_run=false; no live launcher, exchange credentials or public API exposure are authorized. Do not claim global impossibility of live trades by arbitrary separately installed software.

## Realistic 3-4 hour sprint

The implementation clock begins only after plan approval and checkpoint readiness. The entire M3-M10 specification plus empirical testing and sustained reliability validation cannot be responsibly promised in 3-4 hours from this starting point. Missing market/news history, network permissions, Freqtrade installation and provider rights are external blockers, not coding shortcuts.

Proposed bounded 240-minute budget:

| Implementation time | Priority |
| --- | --- |
| 0-15 min | Confirm checkpoint/source freeze, triage returned reviews, lock pattern definitions and timing contracts |
| 15-105 min | Implement all six M3 families and context/event interfaces, with test fixtures prepared before integration |
| 105-135 min | Consolidated M3 suite and milestone report; debug only focused failures |
| 135-210 min | Start a real M4 vertical slice only if lawful provider access is verified: ingestion through stored normalized evidence and outage handling; otherwise research/design and report the block |
| 210-240 min | Consolidated checks for completed work, checkpoint, documented status and exact next tasks |

These are planning estimates, not completion guarantees. The M4 slice would be explicitly PARTIAL until provider benchmarking, full taxonomy, contradiction behavior and all acceptance criteria are satisfied. M5-M10 remain incomplete if the budget ends here. Do not skip milestones or label placeholders as finished to meet the clock. A working news collector is not validated news-based trading; a tested detector is not a profitable strategy.

One comprehensive suite per completed milestone; focused tests only for specific failures. No local models or nested/parallel agent fan-out. One heavy computation across both assistants at a time; OpenCode initially runs none. Current project footprint is about 0.32 MB excluding Git/cache, and the two existing OpenCode processes used about 582 MiB combined during inspection. No new background services were launched.

At sprint end report completed/incomplete features, exact files, repositories/dependencies actually adopted, returned OpenCode findings, executed tests, known defects/assumptions, Git status, paper readiness and next steps. Current paper-trading readiness is NO: no execution framework or adapter is installed, no market history is present and no exchange connection was made.

## Approval requested

Approve this staged execution plan, beginning with M3 after a verified Git checkpoint and triage of relevant review findings. Cryptocurrency.cv source reuse remains blocked absent permission; use an alternative lawful source if its hosted API terms cannot be verified. Networked benchmarking, Freqtrade installation and Telegram connectivity must be reported as actual actions when later authorized. Stop at milestone reports and never proceed with unresolved serious financial, timing, look-ahead or risk defects. Phase A itself authorizes no implementation.
