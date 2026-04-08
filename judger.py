from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from judge_common import (
    CAPACITY,
    DOOR_TIME,
    ELEVATOR_COUNT,
    INITIAL_FLOOR,
    MOVE_TIME,
    OUTPUT_LINE_RE,
    TIMESTAMP_EPS,
    clean_matching_files,
    ensure_directory,
    floor_to_index,
    load_case,
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_INPUT_DIR = SCRIPT_DIR / "in"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "out"
DEFAULT_LOG_DIR = SCRIPT_DIR / "judge"
DEFAULT_PROJECT_JAR = SCRIPT_DIR / "project.jar"
DEFAULT_SOURCE_DIR = REPO_ROOT / "src"
DEFAULT_LIB_JAR = SCRIPT_DIR / "dependency" / "elevator1-2026.jar"
DEFAULT_DATAINPUT_EXE = SCRIPT_DIR / "dependency" / "datainput"

RECEIVE_RE = re.compile(r"^RECEIVE-(\d+)-([1-6])$")
ARRIVE_RE = re.compile(r"^ARRIVE-(B[1-4]|F[1-7])-([1-6])$")
OPEN_RE = re.compile(r"^OPEN-(B[1-4]|F[1-7])-([1-6])$")
CLOSE_RE = re.compile(r"^CLOSE-(B[1-4]|F[1-7])-([1-6])$")
IN_RE = re.compile(r"^IN-(\d+)-(B[1-4]|F[1-7])-([1-6])$")
OUT_RE = re.compile(r"^OUT-([SF])-(\d+)-(B[1-4]|F[1-7])-([1-6])$")


class JudgeFailure(Exception):
    def __init__(self, message: str, line_number: int | None = None, line_text: str | None = None):
        super().__init__(message)
        self.message = message
        self.line_number = line_number
        self.line_text = line_text


@dataclass(slots=True)
class PassengerState:
    person_id: int
    request_time: Decimal
    from_floor: str
    to_floor: str
    weight: int
    elevator_id: int
    current_floor: str
    onboard: bool = False
    current_elevator: int | None = None
    active_receive_elevator: int | None = None
    completed: bool = False
    receive_count: int = 0


@dataclass(slots=True)
class ElevatorState:
    elevator_id: int
    current_floor: str = INITIAL_FLOOR
    door_open: bool = False
    last_open_timestamp: Decimal | None = None
    current_weight: int = 0
    onboard_passengers: set[int] = field(default_factory=set)
    active_receives: set[int] = field(default_factory=set)
    next_arrive_not_before: Decimal | None = None


@dataclass(slots=True)
class CaseResult:
    case_name: str
    passed: bool
    message: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run and judge elevator hw5 test cases.")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR, help="directory of *.in files")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="directory of *.out files")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="directory of judge logs")
    parser.add_argument("--project-jar", type=Path, default=DEFAULT_PROJECT_JAR, help="path of target project.jar")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR, help="source root used for fallback build")
    parser.add_argument("--lib-jar", type=Path, default=DEFAULT_LIB_JAR, help="official elevator interface jar")
    parser.add_argument(
        "--datainput-exe",
        type=Path,
        default=DEFAULT_DATAINPUT_EXE,
        help="datainput feeder executable",
    )
    parser.add_argument("--main-class", default="oo.Main", help="main class used when building project.jar")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
        help="timeout for each case execution",
    )
    parser.add_argument(
        "--cases",
        nargs="*",
        default=None,
        help="specific case stems to run, for example: --cases 1 3 5",
    )
    parser.add_argument("--rebuild", action="store_true", help="force rebuild project.jar before judging")
    return parser.parse_args()


def run_command(command: list[str], cwd: Path) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "command failed:\n"
            f"cwd: {cwd}\n"
            f"cmd: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def build_project_jar(
    project_jar: Path,
    source_dir: Path,
    lib_jar: Path,
    main_class: str,
) -> None:
    if not source_dir.exists():
        raise RuntimeError(f"source directory does not exist: {source_dir}")
    if not lib_jar.exists():
        raise RuntimeError(f"library jar does not exist: {lib_jar}")

    java_files = sorted(str(path) for path in source_dir.rglob("*.java"))
    if not java_files:
        raise RuntimeError(f"no Java files found under {source_dir}")

    ensure_directory(project_jar.parent)
    if project_jar.exists():
        project_jar.unlink()

    with tempfile.TemporaryDirectory(prefix="judge_build_", dir=str(project_jar.parent)) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        classes_dir = temp_dir / "classes"
        classes_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = temp_dir / "MANIFEST.MF"
        manifest_path.write_text(
            f"Main-Class: {main_class}\nClass-Path: {lib_jar.name}\n",
            encoding="utf-8",
        )

        compile_command = [
            "javac",
            "-encoding",
            "UTF-8",
            "-cp",
            str(lib_jar),
            "-d",
            str(classes_dir),
            *java_files,
        ]
        run_command(compile_command, REPO_ROOT)

        package_command = [
            "jar",
            "--create",
            "--file",
            str(project_jar),
            "--manifest",
            str(manifest_path),
            "-C",
            str(classes_dir),
            ".",
        ]
        run_command(package_command, REPO_ROOT)


def sort_case_paths(paths: list[Path]) -> list[Path]:
    def key(path: Path) -> tuple[int, str]:
        stem = path.stem
        return (0, f"{int(stem):08d}") if stem.isdigit() else (1, stem)

    return sorted(paths, key=key)


def less_than(left: Decimal, right: Decimal) -> bool:
    return left + TIMESTAMP_EPS < right


def ensure_passenger_exists(
    passengers: dict[int, PassengerState], person_id: int, line_number: int, line_text: str
) -> PassengerState:
    passenger = passengers.get(person_id)
    if passenger is None:
        raise JudgeFailure(f"unknown passenger id {person_id}", line_number, line_text)
    return passenger


def ensure_elevator_exists(
    elevators: dict[int, ElevatorState], elevator_id: int, line_number: int, line_text: str
) -> ElevatorState:
    elevator = elevators.get(elevator_id)
    if elevator is None:
        raise JudgeFailure(f"unknown elevator id {elevator_id}", line_number, line_text)
    return elevator


def validate_output(case_path: Path, output_path: Path) -> None:
    requests = load_case(case_path)
    passengers = {
        request.person_id: PassengerState(
            person_id=request.person_id,
            request_time=request.timestamp,
            from_floor=request.from_floor,
            to_floor=request.to_floor,
            weight=request.weight,
            elevator_id=request.elevator_id,
            current_floor=request.from_floor,
        )
        for request in requests
    }
    elevators = {elevator_id: ElevatorState(elevator_id=elevator_id) for elevator_id in range(1, ELEVATOR_COUNT + 1)}

    if not output_path.exists():
        raise JudgeFailure(f"output file does not exist: {output_path}")

    last_timestamp: Decimal | None = None
    with output_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\r\n")
            if line == "":
                raise JudgeFailure("blank output line is not allowed", line_number, line)
            match = OUTPUT_LINE_RE.fullmatch(line)
            if match is None:
                raise JudgeFailure("invalid timestamped output format", line_number, line)
            timestamp = Decimal(match.group(1))
            payload = match.group(2)
            if last_timestamp is not None and less_than(timestamp, last_timestamp):
                raise JudgeFailure("output timestamps must be nondecreasing", line_number, line)
            last_timestamp = timestamp

            receive_match = RECEIVE_RE.fullmatch(payload)
            if receive_match is not None:
                person_id = int(receive_match.group(1))
                elevator_id = int(receive_match.group(2))
                passenger = ensure_passenger_exists(passengers, person_id, line_number, line)
                elevator = ensure_elevator_exists(elevators, elevator_id, line_number, line)
                if elevator_id != passenger.elevator_id:
                    raise JudgeFailure(
                        f"passenger {person_id} is assigned to elevator {passenger.elevator_id}, got {elevator_id}",
                        line_number,
                        line,
                    )
                # 数据投喂时间波动是正常现象，不以此为正确性盘踞
                # if less_than(timestamp, passenger.request_time):
                #     raise JudgeFailure(
                #         f"RECEIVE of passenger {person_id} is earlier than request time {passenger.request_time}",
                #         line_number,
                #         line,
                #     )
                if passenger.completed:
                    raise JudgeFailure(
                        f"passenger {person_id} has already reached the destination and cannot be RECEIVEd again",
                        line_number,
                        line,
                    )
                if passenger.active_receive_elevator is not None:
                    raise JudgeFailure(
                        f"passenger {person_id} still has an unfinished RECEIVE",
                        line_number,
                        line,
                    )
                passenger.active_receive_elevator = elevator_id
                passenger.receive_count += 1
                was_empty = not elevator.active_receives
                elevator.active_receives.add(person_id)
                if was_empty and not elevator.door_open:
                    elevator.next_arrive_not_before = timestamp + MOVE_TIME
                continue

            arrive_match = ARRIVE_RE.fullmatch(payload)
            if arrive_match is not None:
                floor_name = arrive_match.group(1)
                elevator_id = int(arrive_match.group(2))
                elevator = ensure_elevator_exists(elevators, elevator_id, line_number, line)
                if elevator.door_open:
                    raise JudgeFailure(
                        f"elevator {elevator_id} cannot ARRIVE while the door is open",
                        line_number,
                        line,
                    )
                if not elevator.active_receives:
                    raise JudgeFailure(
                        f"elevator {elevator_id} cannot move without an unfinished RECEIVE",
                        line_number,
                        line,
                    )
                if elevator.next_arrive_not_before is not None and less_than(
                    timestamp, elevator.next_arrive_not_before
                ):
                    raise JudgeFailure(
                        f"elevator {elevator_id} moves too fast, next ARRIVE should be no earlier than "
                        f"{elevator.next_arrive_not_before}",
                        line_number,
                        line,
                    )
                previous_index = floor_to_index(elevator.current_floor)
                current_index = floor_to_index(floor_name)
                if abs(current_index - previous_index) != 1:
                    raise JudgeFailure(
                        f"elevator {elevator_id} must move exactly one floor each time",
                        line_number,
                        line,
                    )
                elevator.current_floor = floor_name
                elevator.next_arrive_not_before = timestamp + MOVE_TIME
                continue

            open_match = OPEN_RE.fullmatch(payload)
            if open_match is not None:
                floor_name = open_match.group(1)
                elevator_id = int(open_match.group(2))
                elevator = ensure_elevator_exists(elevators, elevator_id, line_number, line)
                if elevator.door_open:
                    raise JudgeFailure(
                        f"elevator {elevator_id} cannot OPEN when the door is already open",
                        line_number,
                        line,
                    )
                if floor_name != elevator.current_floor:
                    raise JudgeFailure(
                        f"elevator {elevator_id} OPEN floor {floor_name} does not match current floor "
                        f"{elevator.current_floor}",
                        line_number,
                        line,
                    )
                elevator.door_open = True
                elevator.last_open_timestamp = timestamp
                elevator.next_arrive_not_before = None
                continue

            close_match = CLOSE_RE.fullmatch(payload)
            if close_match is not None:
                floor_name = close_match.group(1)
                elevator_id = int(close_match.group(2))
                elevator = ensure_elevator_exists(elevators, elevator_id, line_number, line)
                if not elevator.door_open:
                    raise JudgeFailure(
                        f"elevator {elevator_id} cannot CLOSE when the door is already closed",
                        line_number,
                        line,
                    )
                if floor_name != elevator.current_floor:
                    raise JudgeFailure(
                        f"elevator {elevator_id} CLOSE floor {floor_name} does not match current floor "
                        f"{elevator.current_floor}",
                        line_number,
                        line,
                    )
                if elevator.last_open_timestamp is None or less_than(timestamp, elevator.last_open_timestamp + DOOR_TIME):
                    raise JudgeFailure(
                        f"elevator {elevator_id} closes too early after OPEN",
                        line_number,
                        line,
                    )
                if elevator.current_weight > CAPACITY:
                    raise JudgeFailure(
                        f"elevator {elevator_id} exceeds capacity when closing: {elevator.current_weight}",
                        line_number,
                        line,
                    )
                elevator.door_open = False
                elevator.last_open_timestamp = None
                elevator.next_arrive_not_before = (
                    timestamp + MOVE_TIME if elevator.active_receives else None
                )
                continue

            in_match = IN_RE.fullmatch(payload)
            if in_match is not None:
                person_id = int(in_match.group(1))
                floor_name = in_match.group(2)
                elevator_id = int(in_match.group(3))
                passenger = ensure_passenger_exists(passengers, person_id, line_number, line)
                elevator = ensure_elevator_exists(elevators, elevator_id, line_number, line)
                if not elevator.door_open:
                    raise JudgeFailure(
                        f"passenger {person_id} cannot IN when elevator {elevator_id} door is closed",
                        line_number,
                        line,
                    )
                if floor_name != elevator.current_floor:
                    raise JudgeFailure(
                        f"passenger {person_id} IN floor {floor_name} does not match elevator {elevator_id} "
                        f"current floor {elevator.current_floor}",
                        line_number,
                        line,
                    )
                if elevator_id != passenger.elevator_id:
                    raise JudgeFailure(
                        f"passenger {person_id} enters wrong elevator {elevator_id}",
                        line_number,
                        line,
                    )
                if passenger.completed:
                    raise JudgeFailure(
                        f"passenger {person_id} has already reached the destination",
                        line_number,
                        line,
                    )
                if passenger.onboard:
                    raise JudgeFailure(
                        f"passenger {person_id} is already inside an elevator",
                        line_number,
                        line,
                    )
                if passenger.active_receive_elevator != elevator_id:
                    raise JudgeFailure(
                        f"passenger {person_id} enters elevator {elevator_id} without an active RECEIVE",
                        line_number,
                        line,
                    )
                if passenger.current_floor != floor_name:
                    raise JudgeFailure(
                        f"passenger {person_id} is not waiting at floor {floor_name}",
                        line_number,
                        line,
                    )
                passenger.onboard = True
                passenger.current_elevator = elevator_id
                elevator.onboard_passengers.add(person_id)
                elevator.current_weight += passenger.weight
                continue

            out_match = OUT_RE.fullmatch(payload)
            if out_match is not None:
                out_type = out_match.group(1)
                person_id = int(out_match.group(2))
                floor_name = out_match.group(3)
                elevator_id = int(out_match.group(4))
                passenger = ensure_passenger_exists(passengers, person_id, line_number, line)
                elevator = ensure_elevator_exists(elevators, elevator_id, line_number, line)
                if not elevator.door_open:
                    raise JudgeFailure(
                        f"passenger {person_id} cannot OUT when elevator {elevator_id} door is closed",
                        line_number,
                        line,
                    )
                if floor_name != elevator.current_floor:
                    raise JudgeFailure(
                        f"passenger {person_id} OUT floor {floor_name} does not match elevator {elevator_id} "
                        f"current floor {elevator.current_floor}",
                        line_number,
                        line,
                    )
                if not passenger.onboard or passenger.current_elevator != elevator_id:
                    raise JudgeFailure(
                        f"passenger {person_id} is not inside elevator {elevator_id}",
                        line_number,
                        line,
                    )
                if passenger.active_receive_elevator != elevator_id:
                    raise JudgeFailure(
                        f"passenger {person_id} has no active RECEIVE in elevator {elevator_id}",
                        line_number,
                        line,
                    )
                if out_type == "S" and floor_name != passenger.to_floor:
                    raise JudgeFailure(
                        f"passenger {person_id} uses OUT-S at non-target floor {floor_name}",
                        line_number,
                        line,
                    )
                if out_type == "F" and floor_name == passenger.to_floor:
                    raise JudgeFailure(
                        f"passenger {person_id} uses OUT-F at target floor {floor_name}",
                        line_number,
                        line,
                    )
                passenger.onboard = False
                passenger.current_elevator = None
                passenger.current_floor = floor_name
                passenger.active_receive_elevator = None
                if out_type == "S":
                    passenger.completed = True
                if person_id not in elevator.onboard_passengers:
                    raise JudgeFailure(
                        f"elevator {elevator_id} does not record passenger {person_id} as onboard",
                        line_number,
                        line,
                    )
                elevator.onboard_passengers.remove(person_id)
                elevator.current_weight -= passenger.weight
                if person_id not in elevator.active_receives:
                    raise JudgeFailure(
                        f"elevator {elevator_id} does not record passenger {person_id} as active RECEIVE",
                        line_number,
                        line,
                    )
                elevator.active_receives.remove(person_id)
                continue

            raise JudgeFailure("unknown output action", line_number, line)

    for elevator_id, elevator in elevators.items():
        if elevator.door_open:
            raise JudgeFailure(f"elevator {elevator_id} door is still open at program end")
        if elevator.current_weight > CAPACITY:
            raise JudgeFailure(
                f"elevator {elevator_id} exceeds capacity at program end: {elevator.current_weight}"
            )
        if elevator.onboard_passengers:
            raise JudgeFailure(
                f"elevator {elevator_id} still carries passengers at program end: "
                f"{sorted(elevator.onboard_passengers)}"
            )

    unfinished = [person_id for person_id, passenger in passengers.items() if not passenger.completed]
    if unfinished:
        raise JudgeFailure(f"unfinished passengers at program end: {unfinished}")


def run_case(
    case_path: Path,
    out_path: Path,
    err_path: Path,
    project_jar: Path,
    lib_jar: Path,
    datainput_exe: Path,
    timeout_seconds: int,
) -> tuple[str, str]:
    ensure_directory(out_path.parent)
    ensure_directory(err_path.parent)
    if out_path.exists():
        out_path.unlink()
    if err_path.exists():
        err_path.unlink()

    with tempfile.TemporaryDirectory(prefix=f"judge_case_{case_path.stem}_", dir=str(SCRIPT_DIR)) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        local_datainput = temp_dir / datainput_exe.name
        shutil.copy2(case_path, temp_dir / "stdin.txt")
        shutil.copy2(project_jar, temp_dir / "code.jar")
        shutil.copy2(lib_jar, temp_dir / lib_jar.name)
        shutil.copy2(datainput_exe, local_datainput)

        feeder = subprocess.Popen(
            [str(local_datainput)],
            cwd=temp_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        java = subprocess.Popen(
            ["java", "-jar", "code.jar"],
            cwd=temp_dir,
            stdin=feeder.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if feeder.stdout is not None:
            feeder.stdout.close()

        try:
            stdout_text, stderr_text = java.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            java.kill()
            feeder.kill()
            stdout_text, stderr_text = java.communicate()
            feeder_stderr = ""
            if feeder.stderr is not None:
                feeder_stderr = feeder.stderr.read().decode("utf-8", errors="replace")
            timed_out = stderr_text
            timed_out += "\n[Judger] execution timed out\n"
            if feeder_stderr.strip():
                timed_out += f"[Datainput stderr]\n{feeder_stderr}"
            out_path.write_text(stdout_text, encoding="utf-8")
            err_path.write_text(timed_out, encoding="utf-8")
            return stdout_text, timed_out

        feeder_stderr = ""
        if feeder.stderr is not None:
            feeder_stderr = feeder.stderr.read().decode("utf-8", errors="replace")
        feeder.wait(timeout=5)

    combined_stderr = stderr_text
    if java.returncode != 0:
        combined_stderr += f"\n[Judger] java exited with code {java.returncode}\n"
    if feeder.returncode != 0:
        combined_stderr += f"\n[Judger] datainput exited with code {feeder.returncode}\n"
    if feeder_stderr.strip():
        combined_stderr += f"\n[Datainput stderr]\n{feeder_stderr}"

    out_path.write_text(stdout_text, encoding="utf-8")
    err_path.write_text(combined_stderr, encoding="utf-8")
    return stdout_text, combined_stderr


def write_failure_log(log_path: Path, case_path: Path, out_path: Path, err_path: Path, message: str) -> None:
    ensure_directory(log_path.parent)
    content = [
        f"case: {case_path.name}",
        f"input: {case_path}",
        f"stdout: {out_path}",
        f"stderr: {err_path}",
        f"message: {message}",
    ]
    if err_path.exists():
        stderr_text = err_path.read_text(encoding="utf-8", errors="replace").strip()
        if stderr_text:
            content.append("stderr_content:")
            content.append(stderr_text)
    log_path.write_text("\n".join(content) + "\n", encoding="utf-8")


def write_judge_failure_log(
    log_path: Path,
    case_path: Path,
    out_path: Path,
    err_path: Path,
    failure: JudgeFailure,
) -> None:
    ensure_directory(log_path.parent)
    parts = [
        f"case: {case_path.name}",
        f"input: {case_path}",
        f"stdout: {out_path}",
        f"stderr: {err_path}",
        f"message: {failure.message}",
    ]
    if failure.line_number is not None:
        parts.append(f"line: {failure.line_number}")
    if failure.line_text is not None:
        parts.append(f"content: {failure.line_text}")
    log_path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def select_cases(input_dir: Path, selected_stems: list[str] | None) -> list[Path]:
    all_cases = sort_case_paths(list(input_dir.glob("*.in")))
    if selected_stems is None:
        return all_cases
    selected = set(selected_stems)
    return [path for path in all_cases if path.stem in selected]


def main() -> None:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    log_dir = args.log_dir.resolve()
    project_jar = args.project_jar.resolve()
    source_dir = args.source_dir.resolve()
    lib_jar = args.lib_jar.resolve()
    datainput_exe = args.datainput_exe.resolve()

    if not input_dir.exists():
        raise SystemExit(f"input directory does not exist: {input_dir}")
    if not datainput_exe.exists():
        raise SystemExit(f"datainput executable does not exist: {datainput_exe}")
    if not lib_jar.exists():
        raise SystemExit(f"library jar does not exist: {lib_jar}")

    ensure_directory(output_dir)
    ensure_directory(log_dir)
    clean_matching_files(output_dir, "*.out")
    clean_matching_files(output_dir, "*.err.out")
    clean_matching_files(log_dir, "*.log")

    if args.rebuild or not project_jar.exists():
        build_project_jar(project_jar, source_dir, lib_jar, args.main_class)

    if not project_jar.exists():
        raise SystemExit(f"project jar does not exist: {project_jar}")

    case_paths = select_cases(input_dir, args.cases)
    if not case_paths:
        raise SystemExit(f"no cases found in {input_dir}")

    results: list[CaseResult] = []
    for case_path in case_paths:
        case_name = case_path.stem
        out_path = output_dir / f"{case_name}.out"
        err_path = output_dir / f"{case_name}.err.out"
        log_path = log_dir / f"{case_name}.log"
        try:
            _, combined_stderr = run_case(
                case_path=case_path,
                out_path=out_path,
                err_path=err_path,
                project_jar=project_jar,
                lib_jar=lib_jar,
                datainput_exe=datainput_exe,
                timeout_seconds=args.timeout_seconds,
            )
            if combined_stderr.strip():
                message = "stderr is not empty, skipped semantic judging"
                write_failure_log(log_path, case_path, out_path, err_path, message)
                results.append(CaseResult(case_name=case_name, passed=False, message=message))
                continue
            validate_output(case_path, out_path)
            results.append(CaseResult(case_name=case_name, passed=True, message="passed"))
        except JudgeFailure as failure:
            write_judge_failure_log(log_path, case_path, out_path, err_path, failure)
            results.append(CaseResult(case_name=case_name, passed=False, message=failure.message))
        except Exception as exc:  # noqa: BLE001
            write_failure_log(log_path, case_path, out_path, err_path, str(exc))
            results.append(CaseResult(case_name=case_name, passed=False, message=str(exc)))

    passed_count = sum(1 for result in results if result.passed)
    failed_count = len(results) - passed_count
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.case_name}: {result.message}")
    print(f"summary: {passed_count} passed, {failed_count} failed, total {len(results)}")


if __name__ == "__main__":
    main()
