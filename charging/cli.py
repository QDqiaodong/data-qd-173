import argparse
import json
from pathlib import Path
from .core import settle


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    try:
        result = settle(json.loads(Path(a.input).read_text()))
        target = Path(a.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
        print(json.dumps({"sessions": len(result["sessions"]), "total_charge_cents": result["total_charge_cents"]}, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, f"charge-settle: {exc}\n")


if __name__ == "__main__":
    main()
