# M12 Phase 3 Unit 3 — durable offline execution replay

`DurablePaperExecution.create(path, factory)` creates a local SQLite journal;
`recover(path, factory)` opens it with a new writer epoch and **fresh** M8/M9.1
actors. The factory must reproduce the same authoritative M8 decisions,
reservations, M9.1 admissions, and other pre-execution facts on each call.
The baseline and relevant source-code bytes are hashed. A changed baseline
or implementation fails recovery. Existing Unit 1 and Unit 2 files are
consumed through their public interfaces and remain unchanged.

Each top-level Unit 2 call (`acknowledge`, `fill`, or `cancel`) has one immutable
command row. Its full typed input is canonically encoded using the existing
allowlisted M9.3 codec. The journal commits that input first (PENDING). The
adapter then invokes M9.1/M8. On success, an outcome row with the record ID
and complete actor-state hash commits atomically (APPLIED). Nested bridge and
portfolio calls are not replayed as separate commands. Command and outcome
hash chains, sequence numbers, metadata heads, SQLite integrity checks, code
identity, and single-writer epochs are verified before advancing.
Recovery verifies SQLite structure and the pinned baseline, then atomically
increments the writer epoch **before** replaying actor calls. This fences the
old writer while replay may invoke a PENDING callback. If semantic replay
later fails, the new owner stops and the old owner remains fenced; moving the
epoch bump after replay would permit two graphs to advance during recovery.

After a crash before APPLIED, recovery discards the old actor graph, rebuilds
fresh actors from the pinned baseline, and replays the pending command once.
This remains safe even if the discarded graph booked a fill before the crash.
After APPLIED commits but before acknowledgement, recovery replays the
committed prefix into fresh actors, checks every record ID and state hash, and
returns the original receipt for a duplicate command ID. It does not book a
second fill or release a reservation twice. A caught callback error writes a
QUARANTINED outcome; recovery refuses to guess whether that event should be
reapplied. Corrupt, incomplete, reordered, revised, or mismatched journals
fail closed. There is no automatic repair or reset.

The SQLite database may have active WAL sidecar files. Back up a closed
journal or use SQLite's backup API; copying only the main database while its
writer is active is not a valid preservation procedure.

`reconcile(as_of=...)` runs Unit 1's read-only check over the recovered M8/M9.1
state. It does not alter the portfolio. Acknowledgement and full simulated
fills, rejection, and cancellation remain Unit 2/M9.1 operations. M8 retains
sole ownership of reservations and cash. The selected M8 risk fraction stays
`Decimal('.10')`.

**Integration boundary:** this journal starts from a caller-supplied,
reproducible M8/M9.1 baseline. It does not yet journal new M7 signals, M8 risk
evaluations, new M9.1 admissions, Phase 2 exit/funding controller calls, or
market-data frames arriving during the paper session. Wiring those upstream
facts to a durable, causally ordered producer is still required for a complete
Phase 3 runtime. A factory that returns already-mutated callback state fails
the baseline check instead of silently adopting it.

SQLite guarantees replay of committed **local** facts only. It does not prove
exactly-once external delivery. This module has no network, exchange, real
Freqtrade, or Telegram transport. It is offline simulation and does not
establish paper-trading readiness.
