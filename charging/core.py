from datetime import datetime


def _dt(s):
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        raise ValueError("timestamps require an offset")
    return d


def _round_half_up(n, d):
    return (2 * n + d) // (2 * d)


def settle(data):
    tariffs = sorted(data["tariffs"], key=lambda x: _dt(x["start"]))
    parsed = [(_dt(x["start"]), _dt(x["end"]), int(x["cents_per_kwh"])) for x in tariffs]
    if any(a >= b for a, b, _ in parsed) or any(parsed[i][1] > parsed[i + 1][0] for i in range(len(parsed) - 1)):
        raise ValueError("tariff intervals must be valid and non-overlapping")
    rows = []
    ids = set()
    for s in sorted(data["sessions"], key=lambda x: x["id"]):
        if s["id"] in ids:
            raise ValueError("session IDs must be unique")
        ids.add(s["id"])
        start, end = _dt(s["start"]), _dt(s["end"])
        energy = int(s["end_wh"]) - int(s["start_wh"])
        duration = int((end - start).total_seconds())
        if duration <= 0 or energy < 0:
            raise ValueError(f"invalid session {s['id']}")
        cuts = [start] + [a for a, _, _ in parsed if start < a < end] + [end]
        segments, used = [], 0
        for i, (a, b) in enumerate(zip(cuts, cuts[1:])):
            matches = [(x, y, rate) for x, y, rate in parsed if x <= a and b <= y]
            if len(matches) != 1:
                raise ValueError(f"tariff coverage gap for {s['id']} at {a.isoformat()}")
            part = energy - used if i == len(cuts) - 2 else energy * int((b - a).total_seconds()) // duration
            used += part
            rate = matches[0][2]
            cents = _round_half_up(part * rate, 1000)
            segments.append({"start": a.isoformat(), "end": b.isoformat(), "energy_wh": part, "rate_cents_per_kwh": rate, "charge_cents": cents})
        rows.append({"session_id": s["id"], "energy_wh": energy, "charge_cents": sum(x["charge_cents"] for x in segments), "segments": segments})
    return {"sessions": rows, "total_charge_cents": sum(x["charge_cents"] for x in rows)}
