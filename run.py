from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
JUDGE_DIR = SCRIPT_DIR / "judge"
DATA_GENERATOR = SCRIPT_DIR / "data_generator.py"
JUDGER = SCRIPT_DIR / "judger.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Loop generator + judger until interrupted.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run only one round, useful for verification",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.0,
        help="sleep between rounds",
    )
    return parser.parse_args()


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_command(command: list[str], name: str) -> int:
    print(f"[{now_text()}] start {name}: {' '.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    print(
        f"[{now_text()}] finish {name}: return code = {completed.returncode}",
        flush=True,
    )
    return completed.returncode


def next_available_path(target_dir: Path, file_name: str) -> Path:
    target_path = target_dir / file_name
    if not target_path.exists():
        return target_path

    stem = Path(file_name).stem
    suffix = Path(file_name).suffix
    index = 1
    while True:
        candidate = target_dir / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def archive_logs() -> Path | None:
    log_files = sorted(path for path in JUDGE_DIR.glob("*.log") if path.is_file())
    if not log_files:
        return None

    archive_dir = JUDGE_DIR / datetime.now().strftime("%Y-%m-%d-%H-%M")
    archive_dir.mkdir(parents=True, exist_ok=True)

    for log_file in log_files:
        target_path = next_available_path(archive_dir, log_file.name)
        shutil.move(str(log_file), str(target_path))

    return archive_dir


def main() -> None:
    args = parse_args()
    round_index = 1
    python = sys.executable

    if not DATA_GENERATOR.exists():
        raise SystemExit(f"data generator does not exist: {DATA_GENERATOR}")
    if not JUDGER.exists():
        raise SystemExit(f"judger does not exist: {JUDGER}")

    try:
        while True:
            print(f"[{now_text()}] ===== round {round_index} =====", flush=True)

            pre_archive_dir = archive_logs()
            if pre_archive_dir is not None:
                print(
                    f"[{now_text()}] archived leftover judge logs to {pre_archive_dir}",
                    flush=True,
                )

            generator_code = run_command([python, str(DATA_GENERATOR)], "data_generator")
            judger_code: int | None = None
            if generator_code == 0:
                judger_code = run_command([python, str(JUDGER), "--rebuild"], "judger")
            else:
                print(
                    f"[{now_text()}] skip judger because data_generator failed",
                    flush=True,
                )

            archive_dir = archive_logs()
            if archive_dir is not None:
                print(f"[{now_text()}] archived judge logs to {archive_dir}", flush=True)
            else:
                print(f"[{now_text()}] no judge logs to archive", flush=True)

            if generator_code != 0 or (judger_code is not None and judger_code != 0):
                print(f"[{now_text()}] round {round_index} finished with errors", flush=True)
            else:
                print(f"[{now_text()}] round {round_index} finished", flush=True)

            if args.once:
                break

            round_index += 1
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)
    except KeyboardInterrupt:
        print(f"\n[{now_text()}] loop stopped by user", flush=True)


if __name__ == "__main__":
    main()
