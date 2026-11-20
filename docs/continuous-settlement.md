# Continuous correction-safe EV settlement

This document defines the continuous batch mode added on top of the original
static `charge-settle` behavior. The legacy mode (`--input`/`--output` with
`tariffs` + `sessions`) is unchanged; continuous mode is selected with
`--batch` and carries state between rounds.

All timestamps are UTC instants with an explicit offset
(e.g. `2026-01-10T01:00:00+00:00`). All energy amounts are integer Wh and all
money is integer cents. The implementation uses the standard library only.

## 1. Command surface

```sh
# Legacy static mode (unchanged behavior, refuses to overwrite an existing file)
charge-settle --input fixtures/input.json --output run/settlement.json

# Continuous mode: parent state is optional for the first batch
charge-settle --batch fixtures/batches/batch-01-register.json \
              --state run/state.json \
              --output-state run/state.json \
              --output run/result.json
```

* `--batch PATH` – this round's batch JSON.
* `--state PATH` – previous-round state JSON; omit (or point at a missing
  file) only for the first batch.
* `--output-state PATH` – next-round state; written atomically and **only when
  the whole batch validates**.
* `--output PATH` – human/JSON-readable settlement result; also atomic.
* Exit code `2` with a stable `code:` prefix on any validation or business
  error. On failure neither output file is created or replaced, so the parent
  state and prior outputs remain exactly as they were.

`charging.engine.apply_batch(parent_state, batch)` is the pure in-process
entry point and returns `(next_state, result)`; it raises `BatchError`
(attributes `.code`, `.message`) without mutating the parent.

## 2. Batch

A batch is one JSON object:

| field | required | meaning |
| --- | --- | --- |
| `batch_id` | yes | Non-empty string. Unique identity of this submission. |
| `tariffs` | no | Whole replacement tariff catalog (same schema/validation as legacy). Omit to keep the current catalog. |
| `stations` | no | Station registrations: `{station_id, import_source_id}`. Re-registering the same binding is a no-op; a conflicting binding is rejected. |
| `sessions` | no | Session registrations: `{session_id, station_id, meter_source_id, start, end}`. A meter source can belong to at most one session; conflicting re-registration is rejected. |
| `records` | no | Immutable meter records and correction records (see §4, §5). |
| `periods` | no | Billing period lifecycle entries (see §6). |

A batch may combine registrations, readings, corrections and period actions.
Everything is validated together: any single failure rejects the entire batch
and publishes no state.

### `batch_id` semantics and replay

* The engine stores a deterministic full-content summary of every processed
  `batch_id` in `state.processed_batches`.
* Re-submitting the **same `batch_id` with identical content** on the same
  parent state is an idempotent replay: no record, line or adjustment is
  published twice, the returned state equals the parent state byte-for-byte,
  and the result carries `"replayed": true`.
* The same `batch_id` with different content fails with
  `batch_replay_conflict`.
* Re-submitting an already-known immutable record (same `record_id` and exact
  content) inside a *new* batch is a no-op merge and cannot create a second
  adjustment. Re-using a `record_id` with different content fails with
  `duplicate_record_id`.

## 3. State

State is only ever produced by a fully validated batch. Top-level fields:

| field | meaning |
| --- | --- |
| `version` | State schema version (currently `1`). |
| `records` | Append-only log of every accepted immutable meter record, in acceptance order. Never rewritten or deleted. |
| `chains` | One entry per `(source_id, meter_instant)` slot: the effective head, its cumulative Wh, and the head-first chain of record ids. |
| `tariffs` | Currently effective tariff catalog. |
| `stations` / `sessions` / `source_sessions` | Registries and the meter-source → session index. |
| `periods` | Billing periods with UTC bounds and `open`/`closed` status. |
| `closed_lines` | Frozen statement lines keyed by `line_id` (immutable snapshots). |
| `adjustments` | Every adjustment ever published, each traceable to a frozen line and to correction record ids. |
| `processed_batches` | `batch_id` → full-content summary, powering replay idempotency. |

The state is plain JSON-serializable data, so a round is resumable by writing
the previous state to disk and passing it back via `--state`.

## 4. Meter records and `record_id`

```json
{"record_id": "rec-001", "source_id": "M-ST-ALPHA",
 "meter_instant": "2026-01-10T01:00:00+00:00", "cumulative_wh": 1000}
```

* `record_id` – globally unique, immutable identifier of a reading. Unique
  across **all** sources and slots, not just within one batch.
* `source_id` – meter identity. Station import meters and session meters live
  in the same record universe; `stations` binds a station to its import meter
  and `sessions` binds a session to its meter.
* `meter_instant` – UTC instant at which the meter showed the cumulative
  value. The pair `(source_id, meter_instant)` defines a *slot*.
* `cumulative_wh` – integer cumulative register value. Effective per-meter
  sequences must be monotonic; a backwards effective curve fails with
  `negative_reading_delta`.

Effective readings are recomputed from the record log on every batch. Between
two consecutive readings a meter holds its earlier cumulative value, so
session energy over `[start, end]` is `reading(end) - reading(start)`.

## 5. Correction chain

A correction record adds `supersedes_record_id`:

```json
{"record_id": "rec-002", "source_id": "M-ST-ALPHA",
 "meter_instant": "2026-01-10T01:00:00+00:00", "cumulative_wh": 1200,
 "supersedes_record_id": "rec-001"}
```

Rules enforced per slot `(source_id, meter_instant)`:

1. A correction may only supersede a record with the **same `source_id` and
   `meter_instant`**; a foreign or unknown predecessor is
   `unknown_predecessor` and is rejected, never staged.
2. Links form exactly one linear, non-branching, acyclic chain. Two heads are
   `conflicting_head`; forks are `branching_chain`; loops are
   `cyclic_chain`; duplicated ids are `duplicate_record_id`.
3. A predecessor may live in the parent state **or earlier in the same
   batch**. Therefore records can arrive out of order within a batch and the
   effective head is identical to an in-order submission. Across batches, a
   batch whose predecessor does not yet exist is rejected outright (no
   half-accepted corrections).
4. Because effective readings are derived rather than patched, correcting one
   cumulative value changes the interval differences on **both sides** of
   that reading point – the interval ending at the point and the interval
   starting at it.
5. Final effective readings depend only on the record set, never on batch
   boundaries or submission order.

## 6. Billing periods

```json
{"period_id": "BP-2026-01",
 "start": "2026-01-01T00:00:00+00:00", "end": "2026-02-01T00:00:00+00:00",
 "action": "open"}
```

* Each period has explicit UTC `start` (inclusive) / `end` (exclusive) bounds
  and an `open` or `closed` status. Periods must not overlap
  (`period_overlap`). Closing a period that was never opened is
  `unknown_period`.
* A session belongs to the period containing its **end** instant.
* `action: "close"` freezes the period's statement lines (see §8). Closing is
  idempotent on replay; an already-closed period stays closed.

## 7. Station evidence: residuals and billability

Station import meters cover only the EV equipment in the input. For every
pair of consecutive effective import readings of a station, an interval is
built and:

```
residual_wh = station_import_wh − Σ session_meter_wh   (all session meters)
```

The session-meter contribution over an interval uses the same
hold-last-value rule as session energy. A session is **billable only when
every station interval it touches has `residual_wh == 0`** (and its energy is
positive). Otherwise its full energy is withheld and remains visible:

* `station_residuals[]` – every station interval with import Wh, summed
  session Wh and the signed residual. Unexplained energy is retained as
  residual; it is never silently passed to a customer.
* `withheld_energy[]` – every session with its energy, withheld Wh, billable
  flag, reason (`station_residual` or `zero_energy`) and the signed residual
  touching it.
* `session_charges[]` – only currently billable sessions, with the legacy
  tariff segment breakdown.

The three views are deliberately separate so residual Wh, withheld Wh and
billable charges can never be confused.

## 8. Statements, statement lines and close-time freeze

While a period is open, `session_charges` is the live *projection* computed
from current effective readings and is recomputed every batch.

On close, one immutable statement line is frozen per session ending in that
period:

```json
{"line_id": "BP-2026-01:S-1001", "period_id": "BP-2026-01",
 "session_id": "S-1001", "billable": true, "energy_wh": 1000,
 "charge_cents": 10, "segments": [ ...legacy segment shape... ],
 "frozen_after_batch_id": "2026-02-d01-close"}
```

Frozen lines are never mutated in place. `closed_statements[]` renders the
immutable snapshot of every closed period with its line total.

## 9. Adjustments to closed periods

After a close, a correction can change what a frozen line *would* be worth
under the new evidence (e.g. the session becomes unbillable, so its current
value is `0`). The original line is preserved and a signed adjustment is
posted into the next open period:

```json
{"adjustment_id": "BP-2026-01:S-1001:2026-02-d02-correct-station",
 "period_id": "BP-2026-02", "source_period_id": "BP-2026-01",
 "line_id": "BP-2026-01:S-1001", "session_id": "S-1001",
 "amount_cents": -10, "frozen_charge_cents": 10,
 "current_charge_cents": 0,
 "correction_record_ids": ["rec-st-alpha-0010-c1"],
 "batch_id": "2026-02-d02-correct-station"}
```

* `amount_cents` is the exact signed difference between the current value and
  the value already reflected by prior adjustments; a later rebalancing
  correction posts the opposite difference (e.g. `-10` then `+10`).
* The target is the **earliest open period starting at or after the source
  period**; if none exists the batch fails with `no_open_period` instead of
  silently overwriting the closed result.
* Adjustments are keyed by `(line_id, batch_id)` and carry the originating
  `correction_record_ids`, so each one is traceable to its frozen line and
  correction identity. Replays and identical correction histories can never
  publish the same difference twice.
* A correction that does not change any frozen amount (e.g. re-asserting the
  same effective value) posts no adjustment and is accepted even without an
  open successor.
* Corrections accepted **before** the first close are simply part of the
  evidence at freeze time; they appear in the frozen snapshot, not as
  adjustments. Adjustments exist exclusively for post-close revisions.

## 10. Tariff and charging rules (unchanged)

Tariff intervals remain non-overlapping UTC ranges with coverage validation.
Sessions crossing a tariff boundary keep allocating integer Wh by elapsed
seconds, the final segment receives the exact remainder, and each segment is
rounded half-up to whole cents using `(2*n + d) // (2*d)`. Corrections,
period changes and replugs never alter these rules; they only change the
effective Wh inputs and billability. Session output is ordered by
`session_id` for stable diffs. Legacy static inputs produce byte-compatible
output with the original implementation.

## 11. Error code reference

| code | trigger |
| --- | --- |
| `invalid_batch` / `invalid_state` | missing `batch_id`, unsupported state version, wrong JSON types |
| `invalid_record` | missing record fields, non-integer Wh, unparseable/naive timestamp |
| `duplicate_record_id` | repeated `record_id` in a batch or conflicting reuse across batches |
| `unknown_predecessor` | supersede link points at an unknown or foreign-slot record |
| `branching_chain` / `conflicting_head` / `cyclic_chain` | malformed correction graph |
| `negative_reading_delta` / `missing_reading` | backwards effective curve or a meter without covering readings |
| `invalid_station` / `station_conflict` / `unknown_station` / `source_conflict` | registry violations |
| `invalid_session` / `session_conflict` | bad session interval or conflicting re-registration |
| `invalid_period` / `unknown_period` / `period_overlap` / `period_conflict` | period lifecycle violations (bad action/order, overlapping UTC bounds, re-registering a period with different bounds) |
| `invalid_tariffs` / `tariff_coverage` / `missing_tariffs` | catalog or coverage violations |
| `batch_replay_conflict` | same `batch_id`, different content |
| `no_open_period` | a correction changes a frozen amount but no open successor period exists |

## 12. Capability boundaries

* Station import meters are assumed to cover only the EV equipment modeled in
  the same input; residual energy is reported, never allocated to customers.
* A session must be fully covered by station import intervals and by its own
  meter readings; sparse upstream data is rejected rather than estimated.
* Tariff catalogs are replaced whole; there is no per-interval price patching.
* All arithmetic is integer Wh / integer cents; no currency conversion or tax
  layer exists.
* State is a local JSON file; locking, multi-writer coordination and
  retention/archival of old states are the caller's responsibility.
