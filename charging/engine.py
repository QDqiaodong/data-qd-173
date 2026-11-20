"""Continuous, correction-safe EV settlement engine.

The engine is a pure state machine: :func:`apply_batch` takes the previous
state (or ``None``) plus a batch and returns the next state together with a
readable settlement result.  Validation failures raise :class:`BatchError`
without mutating the parent state, so no half-published state can exist.

Design notes (see docs/continuous-settlement.md for the full contract):

* Every meter reading is an immutable record.  Correction records supersede a
  prior record with the same ``(source_id, meter_instant)``; the supersede
  links form a single non-branching chain per slot.
* Effective readings are recomputed from scratch every batch, so a corrected
  cumulative value updates the interval differences on *both* sides of the
  reading point and batch ordering cannot change the result.
* Station import meters only cover EV equipment.  Per station interval the
  residual is station import Wh minus the sum of session-meter Wh; only
  sessions whose touched station intervals all have zero residual are
  billable.
* Closing a billing period freezes its statement lines.  Later corrections
  that change a frozen amount keep the original line and emit a signed
  adjustment into the next open period.
"""

from copy import deepcopy

from .core import _dt, _parse_tariffs, compute_session_charge

STATE_VERSION = 1


def _parse_tariffs_checked(tariffs):
    try:
        return _parse_tariffs(tariffs)
    except (ValueError, KeyError, TypeError) as exc:
        raise BatchError("invalid_tariffs", str(exc)) from exc


class BatchError(ValueError):
    """A batch failed validation or hit a documented business boundary.

    ``code`` is a stable machine-readable identifier used by the CLI and the
    tests; the message is human readable.
    """

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _require(cond, code, message):
    if not cond:
        raise BatchError(code, message)


def _reading_at(readings, source_id, instant):
    """Effective cumulative Wh of ``source_id`` at ``instant``.

    Readings are the effective per-source sorted ``(instant, cumulative_wh)``
    heads.  The meter curve is extended backwards from the first reading and
    forwards from the last reading (a meter holds its last cumulative value
    until a new reading arrives).
    """
    seq = readings.get(source_id)
    if not seq:
        return None
    if instant <= seq[0][0]:
        return seq[0][1]
    if instant >= seq[-1][0]:
        return seq[-1][1]
    for t, value in seq:
        if t == instant:
            return value
    # falls between two readings: hold the earlier cumulative value
    lo = seq[0][1]
    for t, value in seq:
        if t <= instant:
            lo = value
        else:
            break
    return lo


def _chain_heads(records, record_index):
    """Group record ids by (source_id, meter_instant) and validate chains.

    Returns ``{slot: head_record_id}``.  Every chain must be linear,
    acyclic, have one head, and every supersede link must point at a record
    belonging to the same slot.
    """
    slots = {}
    for rec in records:
        slots.setdefault((rec["source_id"], rec["meter_instant"]), []).append(rec["record_id"])

    heads = {}
    for slot, ids in slots.items():
        id_set = set(ids)
        superseded_by = {}
        for rid in ids:
            prev = record_index[rid].get("supersedes_record_id")
            if prev is not None:
                _require(prev in id_set, "unknown_predecessor",
                         f"record {rid!r} supersedes unknown or foreign record {prev!r}")
                _require(prev not in superseded_by, "branching_chain",
                         f"record {prev!r} is superseded by more than one record in slot {slot}")
                superseded_by[prev] = rid
        head_ids = [rid for rid in ids if rid not in superseded_by]
        # A pure cycle has no head; detect it explicitly so callers see the
        # cyclic_chain error code rather than a generic head conflict.
        if not head_ids:
            cur = ids[0]
            seen = set()
            while cur not in seen:
                seen.add(cur)
                cur = record_index[cur].get("supersedes_record_id")
            raise BatchError("cyclic_chain",
                             f"correction cycle detected at record {cur!r} in slot {slot}")
        _require(len(head_ids) == 1, "conflicting_head",
                 f"slot {slot} must have exactly one effective head, found {len(head_ids)}")
        # walk the chain to prove it is a single acyclic path
        seen = set()
        cur = head_ids[0]
        while cur is not None:
            _require(cur not in seen, "cyclic_chain", f"correction cycle detected at record {cur!r}")
            seen.add(cur)
            cur = record_index[cur].get("supersedes_record_id")
        _require(len(seen) == len(ids), "branching_chain",
                 f"correction records for slot {slot} do not form one chain")
        heads[slot] = head_ids[0]
    return heads


# ---------------------------------------------------------------------------
# state construction
# ---------------------------------------------------------------------------

def _initial_state():
    return {
        "version": STATE_VERSION,
        "records": [],            # every immutable meter record, replay order
        "chains": [],             # one entry per (source_id, meter_instant) slot
        "tariffs": [],            # currently effective tariff catalog
        "stations": {},           # station_id -> {import_source_id}
        "sessions": {},           # session_id -> registry payload
        "source_sessions": {},    # session meter source_id -> session_id
        "periods": [],            # billing periods, registration order
        "closed_lines": {},       # line_id -> frozen statement line
        "adjustments": [],        # every adjustment ever published
        "processed_batches": {},  # batch_id -> identity summary
    }


def _validate_meter_record(rec):
    for field in ("record_id", "source_id", "meter_instant", "cumulative_wh"):
        _require(field in rec and rec[field] is not None, "invalid_record",
                 f"meter record is missing field {field!r}")
    _require(isinstance(rec["record_id"], str) and rec["record_id"], "invalid_record",
             "record_id must be a non-empty string")
    _require(isinstance(rec["source_id"], str) and rec["source_id"], "invalid_record",
             "source_id must be a non-empty string")
    try:
        _dt(rec["meter_instant"])
    except (ValueError, TypeError) as exc:
        raise BatchError("invalid_record", f"record {rec['record_id']!r} has bad meter_instant: {exc}")
    if isinstance(rec["cumulative_wh"], bool) or not isinstance(rec["cumulative_wh"], int):
        raise BatchError("invalid_record",
                         f"record {rec['record_id']!r} cumulative_wh must be an integer")
    prev = rec.get("supersedes_record_id")
    _require(prev is None or isinstance(prev, str), "invalid_record",
             f"record {rec['record_id']!r} supersedes_record_id must be a string or null")


def _batch_identity(batch):
    """Deterministic full-content summary used for replay conflict checks."""
    return {
        "records": deepcopy(batch.get("records", [])),
        "sessions": deepcopy(sorted(batch.get("sessions", []), key=lambda s: s["session_id"])),
        "stations": deepcopy(sorted(batch.get("stations", []), key=lambda s: s["station_id"])),
        "tariffs": deepcopy(batch.get("tariffs")),
        "periods": deepcopy(batch.get("periods", [])),
    }


def _merge_records(state, batch):
    """Validate and merge batch records into the state record log.

    Returns the new record list and record index.  Raises BatchError on any
    structural problem; the caller discards the working copy on failure.
    """
    records = list(state["records"])
    index = {r["record_id"]: r for r in records}
    batch_index = {r["record_id"]: r for r in batch.get("records", [])}
    _require(len(batch_index) == len(batch.get("records", [])), "duplicate_record_id",
             "batch contains duplicate record_id values")

    for rec in batch.get("records", []):
        _validate_meter_record(rec)
        if rec["record_id"] in index:
            # identical replay of the same immutable record is a no-op
            _require(rec == index[rec["record_id"]], "duplicate_record_id",
                     f"record_id {rec['record_id']!r} already exists with different content")
            continue
        prev = rec.get("supersedes_record_id")
        if prev is not None:
            _require(prev in index or prev in batch_index, "unknown_predecessor",
                     f"record {rec['record_id']!r} supersedes unknown record {prev!r}")
        records.append(rec)
        index[rec["record_id"]] = rec

    # validate every slot chain over the merged record universe
    heads = _chain_heads(records, index)
    return records, index, heads


def _merge_registries(state, batch):
    for st in batch.get("stations", []):
        _require("station_id" in st and "import_source_id" in st, "invalid_station",
                 "station entry requires station_id and import_source_id")
        existing = state["stations"].get(st["station_id"])
        if existing is not None:
            _require(existing["import_source_id"] == st["import_source_id"], "station_conflict",
                     f"station {st['station_id']!r} is already bound to another import meter")
        else:
            state["stations"][st["station_id"]] = {"import_source_id": st["import_source_id"]}

    for sess in batch.get("sessions", []):
        for field in ("session_id", "station_id", "meter_source_id", "start", "end"):
            _require(field in sess and sess[field] is not None, "invalid_session",
                     f"session entry is missing field {field!r}")
        sid = sess["session_id"]
        start, end = _dt(sess["start"]), _dt(sess["end"])
        _require(start < end, "invalid_session", f"session {sid!r} must start before it ends")
        _require(sess["station_id"] in state["stations"], "unknown_station",
                 f"session {sid!r} references unregistered station {sess['station_id']!r}")
        payload = {
            "session_id": sid,
            "station_id": sess["station_id"],
            "meter_source_id": sess["meter_source_id"],
            "start": sess["start"],
            "end": sess["end"],
        }
        existing = state["sessions"].get(sid)
        if existing is not None:
            _require(existing == payload, "session_conflict",
                     f"session {sid!r} re-registered with different attributes")
        else:
            _require(sess["meter_source_id"] not in state["source_sessions"], "source_conflict",
                     f"meter source {sess['meter_source_id']!r} already belongs to another session")
            state["sessions"][sid] = payload
            state["source_sessions"][sess["meter_source_id"]] = sid


def _merge_periods(state, batch):
    existing = {p["period_id"]: p for p in state["periods"]}
    for per in batch.get("periods", []):
        for field in ("period_id", "start", "end", "action"):
            _require(field in per and per[field] is not None, "invalid_period",
                     f"period entry is missing field {field!r}")
        pid = per["period_id"]
        start, end = _dt(per["start"]), _dt(per["end"])
        _require(start < end, "invalid_period", f"period {pid!r} must start before it ends")
        _require(per["action"] in ("open", "close"), "invalid_period",
                 f"period {pid!r} action must be 'open' or 'close'")
        if pid in existing:
            cur = existing[pid]
            _require(cur["start"] == per["start"] and cur["end"] == per["end"], "period_conflict",
                     f"period {pid!r} re-registered with different UTC bounds")
            if per["action"] == "close":
                _require(cur["status"] in ("open", "closed"), "invalid_period",
                         f"period {pid!r} in unexpected state {cur['status']!r}")
                cur["status"] = "closed"
        else:
            _require(per["action"] == "open", "unknown_period",
                     f"cannot close period {pid!r} that was never opened")
            # non-overlapping UTC intervals across all known periods
            for other in state["periods"]:
                o_start, o_end = _dt(other["start"]), _dt(other["end"])
                _require(end <= o_start or start >= o_end, "period_overlap",
                         f"period {pid!r} overlaps period {other['period_id']!r}")
            state["periods"].append(
                {"period_id": pid, "start": per["start"], "end": per["end"],
                 "status": "closed" if per["action"] == "close" else "open"})
            existing[pid] = state["periods"][-1]


# ---------------------------------------------------------------------------
# effective readings and station evidence
# ---------------------------------------------------------------------------

def _effective_readings(state, index, heads):
    """Per source sorted effective (instant datetime, cumulative_wh)."""
    readings = {}
    for (source_id, instant_str), head_id in heads.items():
        rec = index[head_id]
        readings.setdefault(source_id, []).append((_dt(instant_str), int(rec["cumulative_wh"])))
    for seq in readings.values():
        seq.sort(key=lambda x: x[0])
    return readings


def _station_intervals(state, readings):
    """Build station intervals from consecutive effective import readings."""
    intervals = []
    for station_id in sorted(state["stations"]):
        import_source = state["stations"][station_id]["import_source_id"]
        seq = readings.get(import_source, [])
        for (a, va), (b, vb) in zip(seq, seq[1:]):
            intervals.append({
                "station_id": station_id,
                "start": a,
                "end": b,
                "import_wh": vb - va,
            })
    return intervals


def _session_energy_between(readings, source_id, start, end):
    va = _reading_at(readings, source_id, start)
    vb = _reading_at(readings, source_id, end)
    if va is None or vb is None:
        return None
    return vb - va


def _evaluate_evidence(state, readings):
    """Compute residuals per station interval and per-session billability."""
    intervals = _station_intervals(state, readings)
    # session meters are scoped to their station; a meter never contributes
    # energy to another station's residual
    station_sources = {
        station_id: {sess["meter_source_id"]
                     for sess in state["sessions"].values()
                     if sess["station_id"] == station_id}
        for station_id in state["stations"]}

    for iv in intervals:
        session_wh = 0
        for source_id in sorted(station_sources[iv["station_id"]]):
            delta = _session_energy_between(readings, source_id, iv["start"], iv["end"])
            if delta is None:
                raise BatchError("missing_reading",
                                 f"session meter {source_id!r} has no effective reading covering "
                                 f"{iv['start'].isoformat()}")
            _require(delta >= 0, "negative_reading_delta",
                     f"session meter {source_id!r} goes backwards between "
                     f"{iv['start'].isoformat()} and {iv['end'].isoformat()}")
            session_wh += delta
        iv["session_wh"] = session_wh
        iv["residual_wh"] = iv["import_wh"] - session_wh

    # sessions: billable only when every touched station interval balances
    session_view = {}
    for sid in sorted(state["sessions"]):
        sess = state["sessions"][sid]
        s_start, s_end = _dt(sess["start"]), _dt(sess["end"])
        source_id = sess["meter_source_id"]
        energy = _session_energy_between(readings, source_id, s_start, s_end)
        _require(energy is not None, "missing_reading",
                 f"session {sid!r} meter {source_id!r} has no effective reading")
        _require(energy >= 0, "negative_reading_delta", f"session {sid!r} energy is negative")
        touched = [iv for iv in intervals
                   if iv["station_id"] == sess["station_id"] and iv["start"] < s_end and iv["end"] > s_start]
        _require(touched, "missing_reading",
                 f"session {sid!r} is not covered by any station import interval")
        residual_total = sum(iv["residual_wh"] for iv in touched)
        if energy == 0:
            billable, reason = False, "zero_energy"
        elif all(iv["residual_wh"] == 0 for iv in touched):
            billable, reason = True, "none"
        else:
            billable, reason = False, "station_residual"
        session_view[sid] = {
            "session": sess,
            "energy_wh": energy,
            "billable": billable,
            "reason": reason,
            "withheld_wh": 0 if billable else energy,
            "station_residual_wh": residual_total,
            "interval_ids": [f"{iv['station_id']}|{iv['start'].isoformat()}|{iv['end'].isoformat()}"
                             for iv in touched],
        }

    # validate effective station curves never decrease
    for source_id, seq in readings.items():
        for (_, va), (_, vb) in zip(seq, seq[1:]):
            _require(vb >= va, "negative_reading_delta",
                     f"meter {source_id!r} effective cumulative readings go backwards")
    return intervals, session_view


# ---------------------------------------------------------------------------
# billing: projections, closes, adjustments
# ---------------------------------------------------------------------------

def _period_for_end(state, when):
    for per in state["periods"]:
        if _dt(per["start"]) <= when < _dt(per["end"]):
            return per
    return None


def _line_id(period_id, session_id):
    return f"{period_id}:{session_id}"


def _charge_sessions(state, session_view, tariffs):
    parsed = _parse_tariffs_checked(tariffs)
    charges = {}
    for sid, view in session_view.items():
        if not view["billable"]:
            continue
        sess = view["session"]
        try:
            charges[sid] = compute_session_charge(
                sid, _dt(sess["start"]), _dt(sess["end"]), view["energy_wh"], parsed)
        except ValueError as exc:
            # normalize legacy tariff/interval errors into batch errors
            raise BatchError("tariff_coverage", str(exc)) from exc
    return charges


def _next_open_period(state, after_period_id):
    """Earliest open period starting at/after the given period's start."""
    anchor = None
    for per in state["periods"]:
        if per["period_id"] == after_period_id:
            anchor = _dt(per["start"])
            break
    candidates = [p for p in state["periods"]
                  if p["status"] == "open" and (anchor is None or _dt(p["start"]) >= anchor)]
    candidates.sort(key=lambda p: _dt(p["start"]))
    return candidates[0] if candidates else None


def _settle_and_post(state, readings, batch):
    intervals, session_view = _evaluate_evidence(state, readings)
    tariffs = state["tariffs"]
    _require(tariffs, "missing_tariffs", "no tariff catalog is available")
    charges = _charge_sessions(state, session_view, tariffs)

    # ---- close handling: freeze lines for periods this batch closes ----
    close_ids = [p["period_id"] for p in batch.get("periods", []) if p["action"] == "close"]
    for pid in close_ids:
        per = next(p for p in state["periods"] if p["period_id"] == pid)
        p_start, p_end = _dt(per["start"]), _dt(per["end"])
        for sid in sorted(state["sessions"]):
            sess = state["sessions"][sid]
            end = _dt(sess["end"])
            if not (p_start <= end < p_end):
                continue
            lid = _line_id(pid, sid)
            if lid in state["closed_lines"]:
                continue  # idempotent replay of the close
            view = session_view[sid]
            charge = charges.get(sid)
            line = {
                "line_id": lid,
                "period_id": pid,
                "session_id": sid,
                "billable": view["billable"],
                "energy_wh": view["energy_wh"],
                "charge_cents": charge["charge_cents"] if charge else 0,
                "segments": charge["segments"] if charge else [],
                "frozen_after_batch_id": batch["batch_id"],
            }
            state["closed_lines"][lid] = line

    # ---- adjustments: frozen lines re-valued under current evidence ----
    existing_adj = {a["adjustment_id"] for a in state["adjustments"]}
    corrections_in_batch = [r for r in batch.get("records", [])
                            if r.get("supersedes_record_id") is not None]
    for lid in sorted(state["closed_lines"]):
        line = state["closed_lines"][lid]
        sid = line["session_id"]
        view = session_view.get(sid)
        if view is None:
            continue
        current_cents = charges[sid]["charge_cents"] if view["billable"] and sid in charges else 0
        reflected = line.get("reflected_charge_cents", line["charge_cents"])
        new_delta = current_cents - reflected
        if new_delta == 0:
            continue
        target = _next_open_period(state, line["period_id"])
        if target is None:
            raise BatchError(
                "no_open_period",
                f"correction changes closed line {lid!r} by {new_delta} cents but no open "
                "successor billing period exists; open the next period before submitting "
                "this batch")
        adj_id = f"{lid}:{batch['batch_id']}"
        if adj_id in existing_adj:
            continue  # identical replay, adjustment already published
        # traceability: only corrections touching this session's own meter or
        # its station's import meter can have driven the line's value
        sess = state["sessions"][sid]
        relevant_sources = {sess["meter_source_id"],
                            state["stations"][sess["station_id"]]["import_source_id"]}
        correction_ids = sorted(r["record_id"] for r in corrections_in_batch
                                if r["source_id"] in relevant_sources)
        state["adjustments"].append({
            "adjustment_id": adj_id,
            "period_id": target["period_id"],
            "source_period_id": line["period_id"],
            "line_id": lid,
            "session_id": sid,
            "amount_cents": new_delta,
            "frozen_charge_cents": line["charge_cents"],
            "current_charge_cents": current_cents,
            "correction_record_ids": correction_ids,
            "batch_id": batch["batch_id"],
        })
        line["reflected_charge_cents"] = current_cents
        existing_adj.add(adj_id)

    return intervals, session_view, charges


# ---------------------------------------------------------------------------
# output rendering
# ---------------------------------------------------------------------------

def _render_result(state, intervals, session_view, charges, batch_id, replayed):
    session_charges, withheld = [], []
    for sid in sorted(session_view):
        view = session_view[sid]
        sess = view["session"]
        if view["billable"] and sid in charges:
            row = dict(charges[sid])
            row["period_id"] = (_period_for_end(state, _dt(sess["end"])) or {}).get("period_id")
            session_charges.append(row)
        withheld.append({
            "session_id": sid,
            "station_id": sess["station_id"],
            "energy_wh": view["energy_wh"],
            "withheld_wh": view["withheld_wh"],
            "billable": view["billable"],
            "reason": view["reason"],
            "station_residual_wh": view["station_residual_wh"],
        })

    residuals = [{
        "station_id": iv["station_id"],
        "start": iv["start"].isoformat(),
        "end": iv["end"].isoformat(),
        "station_import_wh": iv["import_wh"],
        "session_meter_wh": iv["session_wh"],
        "residual_wh": iv["residual_wh"],
    } for iv in intervals]

    closed_statements = []
    for pid in sorted({p["period_id"] for p in state["periods"] if p["status"] == "closed"}):
        lines = [dict(state["closed_lines"][lid])
                 for lid in sorted(state["closed_lines"])
                 if state["closed_lines"][lid]["period_id"] == pid]
        closed_statements.append({
            "period_id": pid,
            "start": next(p["start"] for p in state["periods"] if p["period_id"] == pid),
            "end": next(p["end"] for p in state["periods"] if p["period_id"] == pid),
            "status": "closed",
            "lines": lines,
            "total_charge_cents": sum(x["charge_cents"] for x in lines),
        })

    adjustments = [dict(a) for a in sorted(state["adjustments"], key=lambda a: a["adjustment_id"])]
    periods = [dict(p) for p in state["periods"]]

    return {
        "batch_id": batch_id,
        "replayed": replayed,
        "periods": periods,
        "session_charges": session_charges,
        "withheld_energy": withheld,
        "station_residuals": residuals,
        "closed_statements": closed_statements,
        "next_period_adjustments": adjustments,
        "totals": {
            "current_charge_cents": sum(x["charge_cents"] for x in session_charges),
            "withheld_wh": sum(x["withheld_wh"] for x in withheld),
            "residual_wh": sum(x["residual_wh"] for x in residuals),
            "adjustment_cents": sum(x["amount_cents"] for x in adjustments),
        },
    }


def _render_chains(state, heads, index):
    chains = []
    for (source_id, instant) in sorted(heads):
        head_id = heads[(source_id, instant)]
        chain = []
        cur = head_id
        while cur is not None:
            rec = index[cur]
            chain.append({
                "record_id": cur,
                "supersedes_record_id": rec.get("supersedes_record_id"),
                "cumulative_wh": int(rec["cumulative_wh"]),
            })
            cur = rec.get("supersedes_record_id")
        chains.append({
            "source_id": source_id,
            "meter_instant": instant,
            "head_record_id": head_id,
            "effective_cumulative_wh": int(index[head_id]["cumulative_wh"]),
            "chain": chain,  # head first, oldest record last
        })
    return chains


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def apply_batch(parent_state, batch):
    """Apply ``batch`` on top of ``parent_state`` (``None`` for the first run).

    Returns ``(next_state, result)``.  Raises :class:`BatchError` on any
    validation failure; the parent state is left untouched.
    """
    _require(isinstance(batch, dict), "invalid_batch", "batch must be a JSON object")
    _require(isinstance(batch.get("batch_id"), str) and batch["batch_id"], "invalid_batch",
             "batch.batch_id must be a non-empty string")
    bid = batch["batch_id"]

    state = deepcopy(parent_state) if parent_state is not None else _initial_state()
    _require(state.get("version") == STATE_VERSION, "invalid_state", "unsupported state version")

    # idempotent replay: an already-processed batch_id must be the exact same
    # batch and yields the same outputs without publishing anything twice.
    if bid in state["processed_batches"]:
        identity = _batch_identity(batch)
        _require(identity == state["processed_batches"][bid], "batch_replay_conflict",
                 f"batch_id {bid!r} was already processed with different content")
        heads = _chain_heads(state["records"], {r["record_id"]: r for r in state["records"]})
        readings = _effective_readings(state, {r["record_id"]: r for r in state["records"]}, heads)
        intervals, session_view, charges = _settle_and_post(state, readings, batch)
        result = _render_result(state, intervals, session_view, charges, bid, replayed=True)
        return deepcopy(parent_state), result

    # tariff catalog: batches may replace it; the catalog is versioned whole
    if batch.get("tariffs") is not None:
        _parse_tariffs_checked(batch["tariffs"])  # validation only
        state["tariffs"] = deepcopy(batch["tariffs"])

    _merge_registries(state, batch)
    records, index, heads = _merge_records(state, batch)
    state["records"] = records
    _merge_periods(state, batch)

    readings = _effective_readings(state, index, heads)
    intervals, session_view, charges = _settle_and_post(state, readings, batch)
    state["chains"] = _render_chains(state, heads, index)
    state["processed_batches"][bid] = _batch_identity(batch)

    result = _render_result(state, intervals, session_view, charges, bid, replayed=False)
    return state, result
