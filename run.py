from __future__ import annotations

import atexit
import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
INPUT_DIR = SCRIPT_DIR / "in"
OUTPUT_DIR = SCRIPT_DIR / "out"
JUDGE_DIR = SCRIPT_DIR / "judge"
DATA_GENERATOR = SCRIPT_DIR / "data_generator.py"
JUDGER = SCRIPT_DIR / "judger.py"
JUDGER_CASE_TEMP_GLOB = ".judge_case_*_tmp"
JUDGER_BUILD_TEMP_NAME = ".judge_build_tmp"
RUNNER_CLEANUP_GUARDS_INSTALLED = False


@dataclass(slots=True)
class RunArgs:
    once: bool
    mutual: bool
    sleep_seconds: float
    generator_args: list[str]
    judger_args: list[str]


@dataclass(slots=True)
class RuntimePaths:
    generator_output_dir: Path
    judger_input_dir: Path
    judger_output_dir: Path
    judger_log_dir: Path


def split_passthrough_args(raw_args: list[str]) -> tuple[list[str], list[str], list[str]]:
    run_args: list[str] = []
    generator_args: list[str] = []
    judger_args: list[str] = []
    current_target = run_args

    for arg in raw_args:
        if arg == "--generator-args":
            current_target = generator_args
            continue
        if arg == "--judger-args":
            current_target = judger_args
            continue
        current_target.append(arg)

    return run_args, generator_args, judger_args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Loop generator + judger.py until interrupted.",
        epilog=(
            "run.py options should appear before passthrough sections.\n"
            "Arguments after --generator-args are forwarded to data_generator.py.\n"
            "Arguments after --judger-args are forwarded to judger.py.\n\n"
            "Example:\n"
            "  python run.py --once --generator-args --count 5 "
            "--judger-args --rebuild --cases 1 2 3"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run only one round, useful for verification",
    )
    parser.add_argument(
        "--mutual",
        action="store_true",
        help="forward --mutual to both data_generator.py and judger.py",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.0,
        help="sleep between rounds",
    )
    return parser


def parse_args(raw_args: list[str] | None = None) -> RunArgs:
    parser = build_parser()
    cli_args = sys.argv[1:] if raw_args is None else raw_args
    run_args, generator_args, judger_args = split_passthrough_args(cli_args)
    namespace = parser.parse_args(run_args)
    return RunArgs(
        once=namespace.once,
        mutual=namespace.mutual,
        sleep_seconds=namespace.sleep_seconds,
        generator_args=generator_args,
        judger_args=judger_args,
    )


def resolve_command_path(path: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    return (REPO_ROOT / path).resolve()


def resolve_runtime_paths(generator_args: list[str], judger_args: list[str]) -> RuntimePaths:
    generator_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    generator_parser.add_argument("--output-dir", type=Path, default=INPUT_DIR)
    generator_namespace, _ = generator_parser.parse_known_args(generator_args)

    judger_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    judger_parser.add_argument("--input-dir", type=Path, default=INPUT_DIR)
    judger_parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    judger_parser.add_argument("--log-dir", type=Path, default=JUDGE_DIR)
    judger_namespace, _ = judger_parser.parse_known_args(judger_args)

    return RuntimePaths(
        generator_output_dir=resolve_command_path(generator_namespace.output_dir),
        judger_input_dir=resolve_command_path(judger_namespace.input_dir),
        judger_output_dir=resolve_command_path(judger_namespace.output_dir),
        judger_log_dir=resolve_command_path(judger_namespace.log_dir),
    )


def discover_judger_temp_dirs(base_dir: Path = SCRIPT_DIR) -> set[Path]:
    discovered = {path.resolve() for path in base_dir.glob(JUDGER_CASE_TEMP_GLOB) if path.is_dir()}
    build_dir = base_dir / JUDGER_BUILD_TEMP_NAME
    if build_dir.is_dir():
        discovered.add(build_dir.resolve())
    return discovered


def cleanup_judger_temp_dirs(base_dir: Path = SCRIPT_DIR) -> list[Path]:
    removed: list[Path] = []
    for temp_dir in sorted(discover_judger_temp_dirs(base_dir), key=lambda path: str(path)):
        try:
            shutil.rmtree(temp_dir)
        except OSError:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if not temp_dir.exists():
            removed.append(temp_dir)
    return removed


def format_cleaned_dirs(paths: list[Path]) -> str:
    return ", ".join(path.name for path in paths)


def on_exit_signal(signum: int, _frame: object) -> None:
    cleanup_judger_temp_dirs()
    if signum == getattr(signal, "SIGINT", None):
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)


def install_cleanup_guards() -> None:
    global RUNNER_CLEANUP_GUARDS_INSTALLED
    if RUNNER_CLEANUP_GUARDS_INSTALLED:
        return
    atexit.register(cleanup_judger_temp_dirs)
    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        target_signal = getattr(signal, signal_name, None)
        if target_signal is None:
            continue
        try:
            signal.signal(target_signal, on_exit_signal)
        except (ValueError, OSError):
            continue
    RUNNER_CLEANUP_GUARDS_INSTALLED = True


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_flag_once(arguments: list[str], flag: str, enabled: bool) -> list[str]:
    if not enabled or flag in arguments:
        return list(arguments)
    return [*arguments, flag]


def run_command(command: list[str], name: str) -> int:
    print(f"[{now_text()}] start {name}: {subprocess.list2cmdline(command)}", flush=True)
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


def move_if_exists(source_path: Path, target_dir: Path) -> None:
    if not source_path.exists() or not source_path.is_file():
        return
    target_path = next_available_path(target_dir, source_path.name)
    shutil.move(str(source_path), str(target_path))


def archive_logs(input_dir: Path, output_dir: Path, log_dir: Path) -> Path | None:
    log_files = sorted(path for path in log_dir.glob("*.log") if path.is_file())
    if not log_files:
        return None

    archive_dir = log_dir / datetime.now().strftime("%Y-%m-%d-%H-%M")
    archive_dir.mkdir(parents=True, exist_ok=True)

    for log_file in log_files:
        stem = log_file.stem
        move_if_exists(log_file, archive_dir)
        move_if_exists(input_dir / f"{stem}.in", archive_dir)
        move_if_exists(output_dir / f"{stem}.out", archive_dir)
        move_if_exists(output_dir / f"{stem}.err.out", archive_dir)

    return archive_dir


def main() -> None:
    install_cleanup_guards()
    startup_cleaned = cleanup_judger_temp_dirs()
    if startup_cleaned:
        print(
            f"[{now_text()}] cleaned stale judge temp dirs: {format_cleaned_dirs(startup_cleaned)}",
            flush=True,
        )
    args = parse_args()
    generator_script = DATA_GENERATOR
    generator_args = append_flag_once(args.generator_args, "--mutual", args.mutual)
    judger_args = append_flag_once(args.judger_args, "--mutual", args.mutual)
    runtime_paths = resolve_runtime_paths(generator_args, judger_args)
    round_index = 1
    python = sys.executable

    if not generator_script.exists():
        raise SystemExit(f"data generator does not exist: {generator_script}")
    if not JUDGER.exists():
        raise SystemExit(f"judger does not exist: {JUDGER}")
    if runtime_paths.generator_output_dir != runtime_paths.judger_input_dir:
        print(
            (
                f"[{now_text()}] warning: data_generator writes to "
                f"{runtime_paths.generator_output_dir}, but judger reads from "
                f"{runtime_paths.judger_input_dir}"
            ),
            flush=True,
        )

    try:
        while True:
            print(f"[{now_text()}] ===== round {round_index} =====", flush=True)

            pre_archive_dir = archive_logs(
                input_dir=runtime_paths.judger_input_dir,
                output_dir=runtime_paths.judger_output_dir,
                log_dir=runtime_paths.judger_log_dir,
            )
            if pre_archive_dir is not None:
                print(
                    f"[{now_text()}] archived leftover judge logs to {pre_archive_dir}",
                    flush=True,
                )

            generator_code = run_command(
                [python, str(generator_script), *generator_args],
                generator_script.stem,
            )
            judger_code: int | None = None
            if generator_code == 0:
                judger_code = run_command(
                    [python, str(JUDGER), *judger_args],
                    "judger",
                )
            else:
                print(
                    f"[{now_text()}] skip judger because generator failed",
                    flush=True,
                )

            archive_dir = archive_logs(
                input_dir=runtime_paths.judger_input_dir,
                output_dir=runtime_paths.judger_output_dir,
                log_dir=runtime_paths.judger_log_dir,
            )
            if archive_dir is not None:
                print(f"[{now_text()}] archived judge logs to {archive_dir}", flush=True)
            else:
                print(f"[{now_text()}] no judge logs to archive", flush=True)

            round_cleaned = cleanup_judger_temp_dirs()
            if round_cleaned:
                print(
                    f"[{now_text()}] cleaned judge temp dirs: {format_cleaned_dirs(round_cleaned)}",
                    flush=True,
                )

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
    finally:
        final_cleaned = cleanup_judger_temp_dirs()
        if final_cleaned:
            print(
                f"[{now_text()}] cleaned judge temp dirs on exit: {format_cleaned_dirs(final_cleaned)}",
                flush=True,
            )


if __name__ == "__main__":
    main()
