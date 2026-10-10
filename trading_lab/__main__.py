"""Run the included synthetic fixture and write a portable JSON audit trail."""

import argparse
import json
from pathlib import Path

from . import replay


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline synthetic trading replay")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="replay the included synthetic fixture")
    demo.add_argument("--output", type=Path, default=Path("artifacts/demo.json"))
    args = parser.parse_args()
    fixture = Path(__file__).with_name("fixtures") / "synthetic_prices.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    result = replay(data["prices"])
    result["fixture"] = {key: value for key, value in data.items() if key != "prices"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}: {result['summary']['bars']} bars, "
          f"{result['summary']['fills']} fills; synthetic offline data")


if __name__ == "__main__":
    main()
