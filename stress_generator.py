from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_GENERATOR = SCRIPT_DIR / "data_generator.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate hw7 stress cases biased toward maintenance/update/recycle windows. "
            "This is a thin wrapper over data_generator.py with more aggressive defaults."
        )
    )
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument("--output-dir", type=Path, default=SCRIPT_DIR / "in")
    parser.add_argument("--mutual", action="store_true")
    parser.add_argument("--double-wave", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.count <= 0:
        raise SystemExit("--count must be positive")
    command = [
        sys.executable,
        str(DATA_GENERATOR),
        "--count",
        str(args.count),
        "--output-dir",
        str(args.output_dir),
        "--maint-ratio",
        "0.24" if not args.mutual else "0.18",
        "--update-ratio",
        "0.30" if args.double_wave and not args.mutual else "0.24",
        "--time-mode",
        "burst",
        "--pickup-mode",
        "clustered",
        "--dropoff-mode",
        "clustered",
    ]
    if args.mutual:
        command.append("--mutual")
    subprocess.run(command, check=True)
    print("maint/update/recycle stress profile enabled")
    print(f"double_wave = {args.double_wave and not args.mutual}")


if __name__ == "__main__":
    main()
