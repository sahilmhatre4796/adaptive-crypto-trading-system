# Codex / OpenCode coordination

Status: coordination established; no review task dispatched. Awaiting the user's complete development instructions.

## Connection and ownership

No usable direct OpenCode channel is available in this Codex session. No connector is exposed and plugin discovery returned no OpenCode plugin. A local CLI exists at C:/Users/sahil/AppData/Roaming/npm/opencode.ps1, but even `opencode --help` fails with PermissionDenied while opening C:/Users/sahil/.local/share/opencode/log/opencode.log. That path lies outside the writable project scope. Do not bypass the restriction or redirect application storage to evade it. A future direct CLI handoff would first require an approved execution environment and verified read-only controls; none have been established here.

Two existing OpenCode processes were observed at setup, approximately 601 MiB combined working set. This does not prove two active agent tasks or a messaging connection. Codex launched no agent/model request and has not sent OpenCode an assignment. Do not represent manual handoffs as direct delegation or infer that a report was received.

Codex is the primary developer and technical lead. Codex owns architecture, main code modifications, integration, financial/risk implementation, final tests, checkpoints and delivery. OpenCode with the user's hosted MiniMax M2.5 is a read-only reviewer/research assistant. Model/provider selection in OpenCode is controlled by the user; Codex has not verified or changed it.

The main working directory is C:/Users/sahil/Desktop/adaptive_trading_backtester. The original baseline is 19d6f21e15243558b29cdf048fb5177d4f489729. The working implementation is uncommitted, so HEAD alone does not identify the source being reviewed. File hashes must accompany each assigned scope.

## Manual handoff

1. After receiving the complete task, Codex creates ONE bounded assignment using TASK_TEMPLATE.md, with a task ID, named source files, hashes, questions and acceptance criteria.
2. The user passes that assignment to OpenCode. Only the named files or supplied excerpts are in scope. OpenCode initially produces a text report in its conversation and writes nothing in the main checkout, including report files.
3. The user returns OpenCode's report. Codex may save it under docs/coordination/reports/<task-id>.md; OpenCode does not create that file.
4. Codex checks scope and source hashes, reviews evidence and reproduces material findings using small cases when appropriate. Findings are marked accepted, rejected with reasons, or requiring evidence.
5. Codex implements only changes within the user's current authorization, runs relevant checks, and reports results. An assistant recommendation never expands authorization.
6. Stop at the milestone boundary. Obtain the user's required next-phase approval before proceeding.

This workflow is an instruction and ownership contract, not an OS security boundary. Codex cannot enforce permissions in a separate OpenCode application. Use its own verified read-only tool permissions if available; if these cannot be ensured, pass selected source excerpts instead of granting it unrestricted access to the writable checkout. No Windows permissions/settings are changed by this workflow.

## Preventing conflicts

- Codex is the sole source-code writer. OpenCode must not edit, format, create patches on disk, stage, commit, stash, reset, switch branches, or alter snapshots, configuration, dependencies or generated artifacts.
- OpenCode may suggest a patch in its text response, with evidence; Codex decides whether to implement it.
- Freeze the assigned files during a short review. If Codex needs to modify them, cancel/reissue that assignment with new hashes. Discard stale conclusions until rechecked against the new version.
- Baseline snapshots are immutable. Neither assistant may delete research data, overwrite reports, or bypass workspace/Git restrictions.
- No exchange access, secrets, account connections, hosting, paid subscriptions or new trading phases are authorized by this document.
- Keep source text, credentials, .env files, Git credentials and unrelated user files out of external research requests. Supply only the source context needed for a task; never request secret values.

## Candidate assignments (not yet dispatched)

| Priority | Suggested OpenCode task | Expected evidence |
| --- | --- | --- |
| First, if included in the complete task | Independent M0-M2 accounting and timing review | File/line findings, hand-calculated examples, missing-test suggestions |
| Later M3 | Review proposed OHLC pattern definitions and confirmation timing | Boundary examples, causal timing, ambiguous definitions; no implementation |
| Later | Audit market structure interfaces and look-ahead risks | Confirmation/availability timelines and minimal counterexamples |
| Later M4 | Research news providers, repositories and licenses | Primary URLs, versions/dates, permissions, limits and unresolved claims |
| As needed | Dependency/compatibility review or review of Codex changes | Specific compatibility evidence and reproducible failure descriptions |

Repository/news research is evidence gathering, not authorization to adopt code, install packages, run provider benchmarks, subscribe, or contact accounts. MiniMax reviews do not replace Codex's independent checks of trading mathematics or execution safety.

## Laptop resource policy

- Use the hosted API only. No local model downloads or inference.
- One OpenCode assignment at a time; no nested agent fan-out or extra assistant sessions launched by Codex.
- One heavy test/backtest workload across both assistants at a time. Initially OpenCode runs no tests/backtests; Codex owns execution.
- Prefer small fixtures and scoped reads. Do not recursively read caches, .git objects, runs, snapshots or large market datasets unless explicitly assigned.
- No installs, services, polling loops, background workers, repeated full suites, or bulk downloads for coordination.
- Check process CPU time and working-set memory at meaningful workload transitions when useful, not continuously. Process CPU time is cumulative, not instantaneous CPU utilization. If resource pressure appears, pause new heavy work and report it.
- Reports are concise text with links and minimal reproductions; avoid duplicated datasets and large generated files. Do not terminate unrelated processes automatically.

## Current project boundary

M0-M2 review fixes previously passed 36 tests; this coordination task does not rerun or re-certify them. The Git checkpoint diagnostic task was interrupted and remains outstanding. Earlier .git/index.lock writes were denied; no permission workaround is authorized. M3/M4 requirements are future scope only. Await the complete instructions before starting assignments or implementation.
