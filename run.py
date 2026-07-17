from __future__ import annotations

import atexit
import argparse
from dataclasses import dataclass
from datetime import datetime
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

from windows_process_job import WindowsJob

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
INPUT_DIR = SCRIPT_DIR / "in"
OUTPUT_DIR = SCRIPT_DIR / "out"
JUDGE_DIR = SCRIPT_DIR / "judge"
DATA_GENERATOR = SCRIPT_DIR / "data_generator.py"
JUDGER = SCRIPT_DIR / "judger.py"
IS_WINDOWS = os.name == "nt"
WINDOWS_PROCESS_FLAGS = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if IS_WINDOWS else 0
)
ACTIVE_PROCESSES: set[subprocess.Popen] = set()
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


def parse_nonnegative_float(raw_value: str) -> float:
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return value


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
        type=parse_nonnegative_float,
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


def terminate_process(process: subprocess.Popen | None) -> bool:
    if process is None:
        return True
    root_running = process.poll() is None
    if IS_WINDOWS and not root_running:
        return True
    if process.pid > 0:
        if IS_WINDOWS:
            ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
            if ctrl_break is not None:
                try:
                    process.send_signal(ctrl_break)
                    process.wait(timeout=0.5)
                    return True
                except (OSError, subprocess.TimeoutExpired):
                    pass
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            # POSIX commands run in a new session.  The group can still have
            # live descendants after its original leader exits, so always
            # signal the process group before considering cleanup complete.
            killpg = getattr(os, "killpg", None)
            sigterm = getattr(signal, "SIGTERM", None)
            sigkill = getattr(signal, "SIGKILL", None)
            if callable(killpg) and root_running and sigterm is not None:
                try:
                    # Give judger.py a chance to run its own signal cleanup;
                    # its Java/feeder children deliberately live in separate
                    # process groups.
                    killpg(process.pid, sigterm)
                    process.wait(timeout=3)
                except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                    pass
                root_running = process.poll() is None
            if callable(killpg) and sigkill is not None:
                try:
                    killpg(process.pid, sigkill)
                except (ProcessLookupError, OSError):
                    pass
    if not root_running:
        return True
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return process.poll() is not None


def register_process(process: subprocess.Popen | None) -> None:
    if process is not None:
        ACTIVE_PROCESSES.add(process)


def unregister_process(process: subprocess.Popen | None) -> None:
    if process is not None:
        ACTIVE_PROCESSES.discard(process)


def terminate_active_processes() -> None:
    for process in list(ACTIVE_PROCESSES):
        if terminate_process(process):
            ACTIVE_PROCESSES.discard(process)


def on_exit_signal(signum: int, _frame: object) -> None:
    terminate_active_processes()
    if signum == getattr(signal, "SIGINT", None):
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)


def install_cleanup_guards() -> None:
    global RUNNER_CLEANUP_GUARDS_INSTALLED
    if RUNNER_CLEANUP_GUARDS_INSTALLED:
        return
    atexit.register(terminate_active_processes)
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
    process: subprocess.Popen | None = None
    windows_job: WindowsJob | None = None
    try:
        if IS_WINDOWS:
            windows_job = WindowsJob()
            process = windows_job.launch(
                command,
                cwd=REPO_ROOT,
                creationflags=WINDOWS_PROCESS_FLAGS,
            )
        else:
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                start_new_session=True,
            )
        register_process(process)
        return_code = process.wait()
    finally:
        if windows_job is not None:
            windows_job.terminate()
            windows_job.close()
        stopped = terminate_process(process)
        if stopped:
            unregister_process(process)
    print(
        f"[{now_text()}] finish {name}: return code = {return_code}",
        flush=True,
    )
    return return_code


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
        raise SystemExit(
            "data_generator output directory must match judger input directory: "
            f"{runtime_paths.generator_output_dir} != {runtime_paths.judger_input_dir}"
        )

    try:
        while True:
            print(f"[{now_text()}] ===== round {round_index} =====", flush=True)

            pre_archive_dir = None
            if not args.once:
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

            archive_dir = None
            if not args.once:
                archive_dir = archive_logs(
                    input_dir=runtime_paths.judger_input_dir,
                    output_dir=runtime_paths.judger_output_dir,
                    log_dir=runtime_paths.judger_log_dir,
                )
            if args.once:
                print(f"[{now_text()}] judge artifacts kept in place", flush=True)
            elif archive_dir is not None:
                print(f"[{now_text()}] archived judge logs to {archive_dir}", flush=True)
            else:
                print(f"[{now_text()}] no judge logs to archive", flush=True)

            round_exit_code = generator_code
            if round_exit_code == 0 and judger_code is not None:
                round_exit_code = judger_code

            if round_exit_code != 0:
                print(f"[{now_text()}] round {round_index} finished with errors", flush=True)
            else:
                print(f"[{now_text()}] round {round_index} finished", flush=True)

            if args.once:
                if round_exit_code != 0:
                    raise SystemExit(round_exit_code)
                break

            round_index += 1
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)
    except KeyboardInterrupt:
        print(f"\n[{now_text()}] loop stopped by user", flush=True)
        raise SystemExit(130)
    finally:
        terminate_active_processes()


if __name__ == "__main__":
    main()
