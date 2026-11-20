import argparse
import json
import os
import tempfile
from pathlib import Path

from .core import settle
from .engine import BatchError, apply_batch


def _atomic_write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _run_legacy(p, args):
    try:
        result = settle(json.loads(Path(args.input).read_text()))
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
        print(json.dumps({"sessions": len(result["sessions"]),
                          "total_charge_cents": result["total_charge_cents"]}, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, f"charge-settle: {exc}\n")


def _run_batch(p, args):
    try:
        batch = json.loads(Path(args.batch).read_text())
        parent_state = None
        if args.state is not None and Path(args.state).exists():
            parent_state = json.loads(Path(args.state).read_text())

        # All validation happens in-memory first; no file is touched unless
        # the batch is fully accepted.
        next_state, result = apply_batch(parent_state, batch)

        if args.output_state is not None:
            _atomic_write_json(args.output_state, next_state)
        if args.output is not None:
            _atomic_write_json(args.output, result)
        print(json.dumps({
            "batch_id": result["batch_id"],
            "replayed": result["replayed"],
            "current_charge_cents": result["totals"]["current_charge_cents"],
            "withheld_wh": result["totals"]["withheld_wh"],
            "residual_wh": result["totals"]["residual_wh"],
            "adjustment_cents": result["totals"]["adjustment_cents"],
        }, sort_keys=True))
    except BatchError as exc:
        p.exit(2, f"charge-settle: {exc.code}: {exc.message}\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, f"charge-settle: {exc}\n")


def main():
    p = argparse.ArgumentParser(
        prog="charge-settle",
        description="EV charging settlement (legacy static mode and continuous batch mode).")
    p.add_argument("--input", help="legacy static input JSON with tariffs/sessions")
    p.add_argument("--output", help="settlement result JSON path")
    p.add_argument("--batch", help="continuous-mode batch JSON path")
    p.add_argument("--state", help="previous-round state JSON path (optional for the first batch)")
    p.add_argument("--output-state", help="next-round state JSON path")
    args = p.parse_args()

    if args.batch:
        _run_batch(p, args)
        return
    if not args.input or not args.output:
        p.exit(2, "charge-settle: either --batch (continuous mode) or both --input and --output "
                  "(legacy static mode) are required\n")
    _run_legacy(p, args)


if __name__ == "__main__":
    main()
