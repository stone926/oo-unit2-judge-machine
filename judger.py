from __future__ import annotations

import atexit
import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING
import hashlib
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from judge_common import (
    CAPACITY,
    CAR_COUNT,
    DOOR_TIME,
    ELEVATOR_COUNT,
    INITIAL_FLOOR,
    INPUT_CORPUS_LOCK_NAME,
    MAINT_COMPLETE_LIMIT,
    MAX_INPUT_BYTES,
    MOVE_TIME,
    OUTPUT_LINE_RE,
    RECYCLE_COMPLETE_LIMIT,
    SPECIAL_WAIT_TIME,
    TEST_MOVE_TIME,
    TIMESTAMP_EPS,
    TRANSFER_FLOOR,
    UPDATE_COMPLETE_LIMIT,
    UPDATE_FLOOR,
    InputRequest,
    MaintRequest,
    PersonRequest,
    RecycleRequest,
    UpdateRequest,
    clean_matching_files,
    ensure_directory,
    exclusive_file_lock,
    floor_to_index,
    load_case,
)
from windows_process_job import WindowsJob

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
IS_WINDOWS = os.name == "nt"
DEFAULT_INPUT_DIR = SCRIPT_DIR / "in"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "out"
DEFAULT_LOG_DIR = SCRIPT_DIR / "judge"
DEFAULT_PROJECT_JAR = SCRIPT_DIR / "project.jar"
DEFAULT_SOURCE_DIR = REPO_ROOT / "src"
DEFAULT_LIB_JAR = SCRIPT_DIR / "dependency" / "elevator3-2026.jar"
DEFAULT_NATIVE_DATAINPUT = SCRIPT_DIR / "dependency" / "datainput"
DEFAULT_PORTABLE_DATAINPUT = SCRIPT_DIR / "portable_datainput.py"


def default_datainput_path(is_windows: bool) -> Path:
    return DEFAULT_NATIVE_DATAINPUT if is_windows else DEFAULT_PORTABLE_DATAINPUT


DEFAULT_DATAINPUT_EXE = default_datainput_path(IS_WINDOWS)
JUDGER_LOCK_PATH = SCRIPT_DIR / ".hw7-judger.lock"
DEFAULT_TIMEOUT = 120
MUTUAL_TIMEOUT = 180
POST_INPUT_GRACE_SECONDS = 40
BUILD_TIMEOUT = 120
MAX_PROCESS_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_LINE_CHARS = 4096
MUTUAL_FIRST_REQUEST_TIME = Decimal("1.0")
MUTUAL_LAST_REQUEST_TIME = Decimal("50.0")
MUTUAL_MAX_REQUESTS = 70
WINDOWS_PROCESS_FLAGS = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if IS_WINDOWS else 0
ACTIVE_TEMP_DIRS: set[Path] = set()
ACTIVE_PROCESSES: set[subprocess.Popen] = set()
CLEANUP_GUARDS_INSTALLED = False
CLEANUP_IN_PROGRESS = False
COMMUNICATE_POLL_INTERVAL = 0.2
FEEDER_WAIT_SECONDS = 5.0
BUILD_FINGERPRINT_VERSION = b"hw7-judger-build-v1\0"

ID12 = r"([1-9]|1[0-2])"
RECEIVE_RE = re.compile(rf"^RECEIVE-(\d+)-{ID12}$")
ARRIVE_RE = re.compile(rf"^ARRIVE-(B[1-4]|F[1-7])-{ID12}$")
OPEN_RE = re.compile(rf"^OPEN-(B[1-4]|F[1-7])-{ID12}$")
CLOSE_RE = re.compile(rf"^CLOSE-(B[1-4]|F[1-7])-{ID12}$")
IN_RE = re.compile(rf"^IN-(\d+)-(B[1-4]|F[1-7])-{ID12}$")
OUT_RE = re.compile(rf"^OUT-([SF])-(\d+)-(B[1-4]|F[1-7])-{ID12}$")
MAINT_ACCEPT_RE = re.compile(r"^MAINT-ACCEPT-([1-6])-(\d+)-(B[12]|F[23])$")
MAINT1_BEGIN_RE = re.compile(r"^MAINT1-BEGIN-([1-6])$")
MAINT2_BEGIN_RE = re.compile(r"^MAINT2-BEGIN-([1-6])$")
MAINT_END_RE = re.compile(r"^MAINT-END-([1-6])$")
UPDATE_ACCEPT_RE = re.compile(r"^UPDATE-ACCEPT-([1-6])$")
UPDATE_BEGIN_RE = re.compile(r"^UPDATE-BEGIN-([1-6])$")
UPDATE_END_RE = re.compile(r"^UPDATE-END-([1-6])$")
RECYCLE_ACCEPT_RE = re.compile(r"^RECYCLE-ACCEPT-([7-9]|1[0-2])$")
RECYCLE_BEGIN_RE = re.compile(r"^RECYCLE-BEGIN-([7-9]|1[0-2])$")
RECYCLE_END_RE = re.compile(r"^RECYCLE-END-([7-9]|1[0-2])$")

MODE_NORMAL = "NORMAL"
MODE_REP_ACCEPT = "REP_ACCEPT"
MODE_REPAIR = "REPAIR"
MODE_TEST = "TEST"
MODE_UP_ACCEPT = "UP_ACCEPT"
MODE_UPDATE = "UPDATE"
MODE_DOUBLE = "DOUBLE"
MODE_REC_ACCEPT = "REC_ACCEPT"
MODE_RECYCLE = "RECYCLE"


class JudgeFailure(Exception):
    def __init__(self, message: str, line_number: int | None = None, line_text: str | None = None):
        super().__init__(message)
        self.message = message
        self.line_number = line_number
        self.line_text = line_text


class InfrastructureFailure(RuntimeError):
    pass


@dataclass(slots=True)
class PassengerState:
    person_id: int
    request_time: Decimal
    from_floor: str
    to_floor: str
    weight: int
    current_floor: str
    onboard: bool = False
    current_elevator: int | None = None
    active_receive_elevator: int | None = None
    completed: bool = False


@dataclass(slots=True)
class MaintContext:
    request: MaintRequest
    accepted_time: Decimal
    begin_time: Decimal | None = None
    worker_onboard: bool = False
    worker_exited: bool = False
    test_phase: str = "to_target"


@dataclass(slots=True)
class UpdateContext:
    accepted_time: Decimal
    begin_time: Decimal | None = None


@dataclass(slots=True)
class RecycleContext:
    accepted_time: Decimal
    begin_time: Decimal | None = None


@dataclass(slots=True)
class CarState:
    elevator_id: int
    current_floor: str = INITIAL_FLOOR
    door_open: bool = False
    last_open_timestamp: Decimal | None = None
    current_weight: int = 0
    onboard_passengers: set[int] = field(default_factory=set)
    active_receives: set[int] = field(default_factory=set)
    next_arrive_not_before: Decimal | None = None
    next_arrive_not_after: Decimal | None = None
    next_arrive_required_floor: str | None = None


@dataclass(slots=True)
class ShaftState:
    shaft_id: int
    mode: str = MODE_NORMAL
    maint: MaintContext | None = None
    update: UpdateContext | None = None
    recycle: RecycleContext | None = None


@dataclass(slots=True)
class CaseResult:
    case_name: str
    passed: bool
    message: str


def positive_int(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def validate_timeout_after_last_input(timeout: int, last_input_time: Decimal) -> None:
    if Decimal(timeout) <= last_input_time:
        raise RuntimeError(
            f"timeout {timeout}s does not outlast the final input request at "
            f"{last_input_time}s; increase --timeout"
        )


def resolve_judge_timeout(
    explicit_timeout: int | None,
    mutual: bool,
    last_input_time: Decimal,
) -> int:
    if explicit_timeout is not None:
        timeout = explicit_timeout
    elif mutual:
        timeout = MUTUAL_TIMEOUT
    else:
        last_input_ceiling = int(
            last_input_time.to_integral_value(rounding=ROUND_CEILING)
        )
        timeout = max(
            DEFAULT_TIMEOUT,
            last_input_ceiling + POST_INPUT_GRACE_SECONDS,
        )
    validate_timeout_after_last_input(timeout, last_input_time)
    return timeout


def next_poll_sleep(
    started_at: float,
    timeout: int,
    now: float,
    poll_interval: float,
) -> float | None:
    elapsed = max(0.0, now - started_at)
    if elapsed >= timeout:
        return None
    if timeout < elapsed + poll_interval:
        return max(0.0, timeout - elapsed)
    return poll_interval


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run and judge elevator hw7 test cases.")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR, help="directory containing input case files (*.in)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="directory for judged program outputs")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="directory for failure logs")
    parser.add_argument("--project-jar", type=Path, default=DEFAULT_PROJECT_JAR, help="path to the project jar to execute")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR, help="source directory used when rebuilding the project jar")
    parser.add_argument("--lib-jar", type=Path, default=DEFAULT_LIB_JAR, help="path to the official elevator library jar")
    parser.add_argument(
        "--datainput",
        type=Path,
        default=DEFAULT_DATAINPUT_EXE,
        help=(
            "path to the datainput feeder; defaults to the official binary on "
            "Windows and the bundled portable Python feeder on macOS/Linux"
        ),
    )
    parser.add_argument("--main-class", default="oo.Main", help="main class name used when rebuilding")
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=None,
        help=(
            "timeout seconds per case; default is at least 120 and keeps 40s "
            "after the final input (180 with --mutual)"
        ),
    )
    parser.add_argument("--cases", nargs="+", default=None, help="optional case stems to run, such as: 1 2 3")
    parser.add_argument("--mutual", action="store_true", help="enable mutual-test input constraints")
    parser.add_argument("--rebuild", action="store_true", help="force rebuilding project jar before judging")
    return parser.parse_args()


def validate_mutual_input_case(requests: list[InputRequest]) -> None:
    if not requests:
        raise JudgeFailure("mutual mode requires at least one input request")
    if requests[0].timestamp < MUTUAL_FIRST_REQUEST_TIME:
        raise JudgeFailure("mutual mode requires the first input request timestamp >= 1.0s")
    if requests[-1].timestamp > MUTUAL_LAST_REQUEST_TIME:
        raise JudgeFailure("mutual mode requires the last input request timestamp <= 50.0s")
    if len(requests) > MUTUAL_MAX_REQUESTS:
        raise JudgeFailure(f"mutual mode requires total input requests <= {MUTUAL_MAX_REQUESTS}")

    maint_count_by_elevator = {elevator_id: 0 for elevator_id in range(1, ELEVATOR_COUNT + 1)}
    for request in requests:
        if isinstance(request, MaintRequest):
            maint_count_by_elevator[request.elevator_id] += 1
            if maint_count_by_elevator[request.elevator_id] > 1:
                raise JudgeFailure(
                    f"mutual mode requires each elevator to have at most one MAINT request (elevator {request.elevator_id})"
                )


def run_command(command: list[str], cwd: Path, timeout: int = BUILD_TIMEOUT) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"command timed out after {timeout}s:\n"
            f"cwd: {cwd}\ncmd: {subprocess.list2cmdline(command)}"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            "command failed:\n"
            f"cwd: {cwd}\ncmd: {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def build_fingerprint_path(project_jar: Path) -> Path:
    return project_jar.with_name(f"{project_jar.name}.build.sha256")


def compute_build_fingerprint(source_dir: Path, lib_jar: Path, main_class: str) -> str:
    digest = hashlib.sha256(BUILD_FINGERPRINT_VERSION)
    digest.update(main_class.encode("utf-8"))
    digest.update(b"\0")
    java_paths = sorted(source_dir.rglob("*.java"), key=lambda path: path.as_posix())
    if not java_paths:
        raise RuntimeError(f"no Java files found under {source_dir}")
    for path in java_paths:
        try:
            relative = path.relative_to(source_dir).as_posix()
            content = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"cannot read Java source {path}: {exc}") from exc
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    try:
        digest.update(lib_jar.read_bytes())
    except OSError as exc:
        raise RuntimeError(f"cannot read library jar {lib_jar}: {exc}") from exc
    return digest.hexdigest()


def managed_build_is_current(
    project_jar: Path,
    source_dir: Path,
    lib_jar: Path,
    main_class: str,
) -> bool:
    if not project_jar.is_file():
        return False
    fingerprint_path = build_fingerprint_path(project_jar)
    try:
        expected = compute_build_fingerprint(source_dir, lib_jar, main_class)
        actual = fingerprint_path.read_text(encoding="ascii").strip()
    except (OSError, RuntimeError, UnicodeError):
        return False
    return actual == expected


def build_project_jar(project_jar: Path, source_dir: Path, lib_jar: Path, main_class: str) -> None:
    java_paths = sorted(source_dir.rglob("*.java"), key=lambda path: path.as_posix())
    java_files = [str(path) for path in java_paths]
    if not java_files:
        raise RuntimeError(f"no Java files found under {source_dir}")
    if not lib_jar.is_file():
        raise RuntimeError(f"official library jar does not exist: {lib_jar}")
    ensure_directory(project_jar.parent)
    fingerprint_before = compute_build_fingerprint(source_dir, lib_jar, main_class)
    temp_dir = register_temp_dir(
        Path(tempfile.mkdtemp(prefix=".judge_build_", dir=project_jar.parent))
    )
    classes_dir = temp_dir / "classes"
    classes_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = temp_dir / "MANIFEST.MF"
    staged_jar = temp_dir / "project.jar"
    manifest_path.write_text(
        f"Main-Class: {main_class}\nClass-Path: {lib_jar.name}\n",
        encoding="utf-8",
    )
    try:
        run_command(
            ["javac", "-encoding", "UTF-8", "-cp", str(lib_jar), "-d", str(classes_dir), *java_files],
            REPO_ROOT,
        )
        try:
            run_command(
                ["jar", "--create", "--file", str(staged_jar), "--manifest", str(manifest_path), "-C", str(classes_dir), "."],
                REPO_ROOT,
            )
        except RuntimeError:
            run_command(["jar", "cfm", str(staged_jar), str(manifest_path), "-C", str(classes_dir), "."], REPO_ROOT)
        fingerprint_after = compute_build_fingerprint(source_dir, lib_jar, main_class)
        if fingerprint_after != fingerprint_before:
            raise RuntimeError("Java sources or official library changed during the build; retry")
        if not staged_jar.is_file() or staged_jar.stat().st_size == 0:
            raise RuntimeError("jar command did not produce a non-empty project jar")
        staged_jar.replace(project_jar)
        fingerprint_temp = temp_dir / "build.sha256"
        fingerprint_temp.write_text(fingerprint_after + "\n", encoding="ascii")
        fingerprint_temp.replace(build_fingerprint_path(project_jar))
    finally:
        if cleanup_temp_dir(temp_dir):
            unregister_temp_dir(temp_dir)


def less_than(left: Decimal, right: Decimal) -> bool:
    return left + TIMESTAMP_EPS < right


def shaft_id_of(car_id: int) -> int:
    return car_id if car_id <= ELEVATOR_COUNT else car_id - ELEVATOR_COUNT


def is_main_car(car_id: int) -> bool:
    return car_id <= ELEVATOR_COUNT


def is_car_active(shaft: ShaftState, car_id: int) -> bool:
    return is_main_car(car_id) or shaft.mode in {MODE_DOUBLE, MODE_REC_ACCEPT, MODE_RECYCLE}


def main_full_range(shaft: ShaftState) -> bool:
    return shaft.mode in {MODE_NORMAL, MODE_REP_ACCEPT, MODE_REPAIR, MODE_TEST, MODE_UP_ACCEPT, MODE_UPDATE}


def floor_reachable(shaft: ShaftState, car_id: int, floor: str) -> bool:
    if not is_car_active(shaft, car_id):
        return False
    if is_main_car(car_id):
        return True if main_full_range(shaft) else floor_to_index(floor) >= floor_to_index(TRANSFER_FLOOR)
    return floor_to_index(floor) <= floor_to_index(TRANSFER_FLOOR)


def planned_target_floor(shaft: ShaftState, car_id: int, passenger: PassengerState) -> str | None:
    if not floor_reachable(shaft, car_id, passenger.current_floor):
        return None
    if is_main_car(car_id) and main_full_range(shaft):
        return passenger.to_floor
    if floor_reachable(shaft, car_id, passenger.to_floor):
        return passenger.to_floor
    if passenger.current_floor == TRANSFER_FLOOR:
        return None
    return TRANSFER_FLOOR


def can_receive_now(shaft: ShaftState, car_id: int) -> bool:
    if is_main_car(car_id):
        return shaft.mode not in {MODE_REPAIR, MODE_TEST, MODE_UPDATE}
    return shaft.mode in {MODE_DOUBLE, MODE_REC_ACCEPT}


def refresh_next_arrive_window(car: CarState, shaft: ShaftState, timestamp: Decimal) -> None:
    car.next_arrive_not_after = None
    car.next_arrive_required_floor = None
    if car.door_open or not is_car_active(shaft, car.elevator_id):
        car.next_arrive_not_before = None
        return
    if is_main_car(car.elevator_id) and shaft.mode == MODE_TEST and shaft.maint is not None:
        if shaft.maint.test_phase == "to_target":
            destination = shaft.maint.request.target_floor
        elif shaft.maint.test_phase == "to_f1":
            destination = INITIAL_FLOOR
        else:
            destination = None
        car.next_arrive_not_before = None if destination in {None, car.current_floor} else timestamp + TEST_MOVE_TIME
        return
    if car.active_receives:
        car.next_arrive_not_before = timestamp + MOVE_TIME
        return
    if is_main_car(car.elevator_id) and shaft.mode == MODE_REP_ACCEPT and car.current_floor != INITIAL_FLOOR:
        car.next_arrive_not_before = timestamp + MOVE_TIME
        return
    if is_main_car(car.elevator_id) and shaft.mode == MODE_UP_ACCEPT and car.current_floor != UPDATE_FLOOR:
        car.next_arrive_not_before = timestamp + MOVE_TIME
        return
    if (not is_main_car(car.elevator_id)) and shaft.mode == MODE_REC_ACCEPT and car.current_floor != INITIAL_FLOOR:
        car.next_arrive_not_before = timestamp + MOVE_TIME
        return
    if shaft.mode in {MODE_DOUBLE, MODE_REC_ACCEPT, MODE_RECYCLE} and car.current_floor == TRANSFER_FLOOR:
        car.next_arrive_not_before = timestamp + MOVE_TIME
        return
    car.next_arrive_not_before = None


def clear_active_receives(passengers: dict[int, PassengerState], car: CarState) -> None:
    for person_id in list(car.active_receives):
        passengers[person_id].active_receive_elevator = None
    car.active_receives.clear()


def validate_double_layout(shaft: ShaftState, cars: dict[int, CarState], line_number: int, line_text: str) -> None:
    if shaft.mode not in {MODE_DOUBLE, MODE_REC_ACCEPT, MODE_RECYCLE}:
        return
    main_car = cars[shaft.shaft_id]
    sub_car = cars[shaft.shaft_id + ELEVATOR_COUNT]
    if floor_to_index(sub_car.current_floor) >= floor_to_index(main_car.current_floor):
        raise JudgeFailure(
            f"shaft {shaft.shaft_id} violates double-cabin order: main at {main_car.current_floor}, sub at {sub_car.current_floor}",
            line_number,
            line_text,
        )


def is_step_toward(current_floor: str, next_floor: str, destination: str) -> bool:
    current_index = floor_to_index(current_floor)
    destination_index = floor_to_index(destination)
    if current_index == destination_index:
        return False
    expected_step = 1 if destination_index > current_index else -1
    return floor_to_index(next_floor) == current_index + expected_step


def validate_output(
    case_path: Path,
    output_path: Path,
    requests: list[InputRequest] | None = None,
) -> None:
    if requests is None:
        requests = load_case(case_path)
    try:
        output_bytes = output_path.stat().st_size
    except OSError as exc:
        raise JudgeFailure(f"cannot stat program output: {exc}") from exc
    if output_bytes > MAX_PROCESS_OUTPUT_BYTES:
        raise JudgeFailure(
            f"program output exceeds {MAX_PROCESS_OUTPUT_BYTES} bytes"
        )
    passengers = {
        request.person_id: PassengerState(
            person_id=request.person_id,
            request_time=request.timestamp,
            from_floor=request.from_floor,
            to_floor=request.to_floor,
            weight=request.weight,
            current_floor=request.from_floor,
        )
        for request in requests
        if isinstance(request, PersonRequest)
    }
    pending_maint = {i: [] for i in range(1, ELEVATOR_COUNT + 1)}
    pending_update = {i: [] for i in range(1, ELEVATOR_COUNT + 1)}
    pending_recycle = {i + ELEVATOR_COUNT: [] for i in range(1, ELEVATOR_COUNT + 1)}
    completed_passengers: set[int] = set()
    for request in requests:
        if isinstance(request, MaintRequest):
            pending_maint[request.elevator_id].append(request)
        elif isinstance(request, UpdateRequest):
            pending_update[request.elevator_id].append(request)
        elif isinstance(request, RecycleRequest):
            pending_recycle[request.elevator_id].append(request)

    shafts = {i: ShaftState(shaft_id=i) for i in range(1, ELEVATOR_COUNT + 1)}
    cars = {i: CarState(elevator_id=i) for i in range(1, CAR_COUNT + 1)}
    last_timestamp: Decimal | None = None

    with output_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if len(raw_line) > MAX_OUTPUT_LINE_CHARS:
                raise JudgeFailure(
                    f"output line exceeds {MAX_OUTPUT_LINE_CHARS} characters",
                    line_number,
                )
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

            for regex, action in (
                (RECEIVE_RE, "receive"),
                (ARRIVE_RE, "arrive"),
                (OPEN_RE, "open"),
                (CLOSE_RE, "close"),
                (IN_RE, "in"),
                (OUT_RE, "out"),
                (MAINT_ACCEPT_RE, "maint_accept"),
                (MAINT1_BEGIN_RE, "maint1"),
                (MAINT2_BEGIN_RE, "maint2"),
                (MAINT_END_RE, "maint_end"),
                (UPDATE_ACCEPT_RE, "update_accept"),
                (UPDATE_BEGIN_RE, "update_begin"),
                (UPDATE_END_RE, "update_end"),
                (RECYCLE_ACCEPT_RE, "recycle_accept"),
                (RECYCLE_BEGIN_RE, "recycle_begin"),
                (RECYCLE_END_RE, "recycle_end"),
            ):
                event = regex.fullmatch(payload)
                if event is None:
                    continue
                if action in {"receive", "arrive", "open", "close", "in", "out"}:
                    last_index = event.lastindex
                    if last_index is None:
                        raise JudgeFailure("internal regex match error", line_number, line)
                    car_id = int(event.group(last_index))
                    shaft = shafts[shaft_id_of(car_id)]
                    car = cars[car_id]
                    if not is_car_active(shaft, car_id):
                        raise JudgeFailure(f"dark elevator {car_id} outputs actions", line_number, line)
                    if action == "receive":
                        person_id = int(event.group(1))
                        passenger = passengers.get(person_id)
                        if passenger is None or passenger.completed or passenger.onboard:
                            raise JudgeFailure(f"invalid RECEIVE for passenger {person_id}", line_number, line)
                        if less_than(timestamp, passenger.request_time):
                            raise JudgeFailure(
                                f"RECEIVE for passenger {person_id} precedes its input request",
                                line_number,
                                line,
                            )
                        if passenger.active_receive_elevator is not None:
                            raise JudgeFailure(f"passenger {person_id} still has an unfinished RECEIVE", line_number, line)
                        if not can_receive_now(shaft, car_id):
                            raise JudgeFailure(f"elevator {car_id} cannot RECEIVE in state {shaft.mode}", line_number, line)
                        if planned_target_floor(shaft, car_id, passenger) is None:
                            raise JudgeFailure(f"elevator {car_id} cannot serve passenger {person_id}", line_number, line)
                        passenger.active_receive_elevator = car_id
                        car.active_receives.add(person_id)
                        # A new RECEIVE independently authorizes future movement,
                        # so it supersedes a one-shot in-flight transfer exception.
                        car.next_arrive_not_after = None
                        car.next_arrive_required_floor = None
                        # A RECEIVE does not interrupt a movement that could already
                        # have started.  Only create a movement window for a resting car.
                        if car.next_arrive_not_before is None:
                            refresh_next_arrive_window(car, shaft, timestamp)
                    elif action == "arrive":
                        floor_name = event.group(1)
                        if (
                            is_main_car(car_id)
                            and shaft.mode == MODE_REP_ACCEPT
                            and shaft.maint is not None
                            and shaft.maint.worker_onboard
                        ):
                            raise JudgeFailure(
                                f"elevator {car_id} cannot move after its maintenance worker enters",
                                line_number,
                                line,
                            )
                        if (
                            car.next_arrive_not_after is not None
                            and less_than(car.next_arrive_not_after, timestamp)
                        ):
                            raise JudgeFailure(
                                f"elevator {car_id} movement permission expired",
                                line_number,
                                line,
                            )
                        if (
                            car.next_arrive_required_floor is not None
                            and floor_name != car.next_arrive_required_floor
                        ):
                            raise JudgeFailure(
                                f"elevator {car_id} must finish its in-flight move at "
                                f"{car.next_arrive_required_floor}",
                                line_number,
                                line,
                            )
                        if car.door_open:
                            raise JudgeFailure(f"elevator {car_id} cannot ARRIVE with open door", line_number, line)
                        if car.next_arrive_not_before is None:
                            raise JudgeFailure(
                                f"elevator {car_id} cannot ARRIVE without movement permission",
                                line_number,
                                line,
                            )
                        if car.next_arrive_not_before is not None and less_than(timestamp, car.next_arrive_not_before):
                            raise JudgeFailure(f"elevator {car_id} moves too fast", line_number, line)
                        if abs(floor_to_index(floor_name) - floor_to_index(car.current_floor)) != 1:
                            raise JudgeFailure(f"elevator {car_id} must move exactly one floor", line_number, line)
                        if not floor_reachable(shaft, car_id, floor_name):
                            raise JudgeFailure(f"elevator {car_id} reaches forbidden floor {floor_name}", line_number, line)
                        special_destination: str | None = None
                        if not car.active_receives:
                            if is_main_car(car_id) and shaft.mode == MODE_REP_ACCEPT:
                                special_destination = INITIAL_FLOOR
                            elif is_main_car(car_id) and shaft.mode == MODE_UP_ACCEPT:
                                special_destination = UPDATE_FLOOR
                            elif (not is_main_car(car_id)) and shaft.mode == MODE_REC_ACCEPT:
                                special_destination = INITIAL_FLOOR
                        if special_destination is not None and not is_step_toward(
                            car.current_floor, floor_name, special_destination
                        ):
                            raise JudgeFailure(
                                f"elevator {car_id} moves away from its special-operation floor",
                                line_number,
                                line,
                            )
                        if shaft.mode == MODE_TEST and is_main_car(car_id) and shaft.maint is not None:
                            if shaft.maint.test_phase == "to_target":
                                destination = shaft.maint.request.target_floor
                            elif shaft.maint.test_phase == "to_f1":
                                destination = INITIAL_FLOOR
                            else:
                                raise JudgeFailure(
                                    f"elevator {car_id} cannot move in maintenance test phase "
                                    f"{shaft.maint.test_phase}",
                                    line_number,
                                    line,
                                )
                            if not is_step_toward(car.current_floor, floor_name, destination):
                                raise JudgeFailure(
                                    f"elevator {car_id} deviates from its maintenance test route",
                                    line_number,
                                    line,
                                )
                        car.current_floor = floor_name
                        if shaft.mode == MODE_TEST and is_main_car(car_id) and shaft.maint is not None:
                            if shaft.maint.test_phase == "to_target" and floor_name == shaft.maint.request.target_floor:
                                shaft.maint.test_phase = "to_f1"
                            elif shaft.maint.test_phase == "to_f1" and floor_name == INITIAL_FLOOR:
                                shaft.maint.test_phase = "ready_open"
                        refresh_next_arrive_window(car, shaft, timestamp)
                        validate_double_layout(shaft, cars, line_number, line)
                    elif action == "open":
                        floor_name = event.group(1)
                        if car.door_open or floor_name != car.current_floor:
                            raise JudgeFailure(f"invalid OPEN for elevator {car_id}", line_number, line)
                        if (
                            is_main_car(car_id)
                            and shaft.mode == MODE_REP_ACCEPT
                            and shaft.maint is not None
                            and shaft.maint.worker_onboard
                        ):
                            raise JudgeFailure(
                                f"elevator {car_id} cannot OPEN after its maintenance worker enters",
                                line_number,
                                line,
                            )
                        if shaft.mode in {MODE_REPAIR, MODE_UPDATE} or ((not is_main_car(car_id)) and shaft.mode == MODE_RECYCLE):
                            raise JudgeFailure(f"elevator {car_id} cannot OPEN in state {shaft.mode}", line_number, line)
                        if shaft.mode == MODE_TEST:
                            if (
                                (not is_main_car(car_id))
                                or shaft.maint is None
                                or floor_name != INITIAL_FLOOR
                                or shaft.maint.test_phase != "ready_open"
                                or (not shaft.maint.worker_onboard)
                                or shaft.maint.worker_exited
                            ):
                                raise JudgeFailure(
                                    f"elevator {car_id} cannot OPEN in maintenance test phase",
                                    line_number,
                                    line,
                                )
                            shaft.maint.test_phase = "door_open"
                        car.door_open = True
                        car.last_open_timestamp = timestamp
                        refresh_next_arrive_window(car, shaft, timestamp)
                    elif action == "close":
                        floor_name = event.group(1)
                        if (not car.door_open) or floor_name != car.current_floor:
                            raise JudgeFailure(f"invalid CLOSE for elevator {car_id}", line_number, line)
                        if car.last_open_timestamp is None or less_than(timestamp, car.last_open_timestamp + DOOR_TIME):
                            raise JudgeFailure(f"elevator {car_id} closes too early", line_number, line)
                        if car.current_weight > CAPACITY:
                            raise JudgeFailure(f"elevator {car_id} exceeds capacity at CLOSE", line_number, line)
                        if shaft.mode == MODE_TEST:
                            if shaft.maint is None or shaft.maint.test_phase != "worker_exited":
                                raise JudgeFailure(
                                    f"elevator {car_id} must let the maintenance worker exit before CLOSE",
                                    line_number,
                                    line,
                                )
                            shaft.maint.test_phase = "ready_end"
                        car.door_open = False
                        refresh_next_arrive_window(car, shaft, timestamp)
                    elif action == "in":
                        actor_id = int(event.group(1))
                        floor_name = event.group(2)
                        if (not car.door_open) or floor_name != car.current_floor:
                            raise JudgeFailure(f"invalid IN for elevator {car_id}", line_number, line)
                        if (
                            is_main_car(car_id)
                            and shaft.mode == MODE_REP_ACCEPT
                            and shaft.maint is not None
                            and shaft.maint.worker_onboard
                        ):
                            raise JudgeFailure(
                                f"elevator {car_id} cannot board actors after its maintenance worker enters",
                                line_number,
                                line,
                            )
                        if actor_id in passengers:
                            passenger = passengers[actor_id]
                            if passenger.active_receive_elevator != car_id or passenger.onboard or passenger.current_floor != floor_name:
                                raise JudgeFailure(f"invalid passenger IN {actor_id}", line_number, line)
                            passenger.onboard = True
                            passenger.current_elevator = car_id
                            car.onboard_passengers.add(actor_id)
                            car.current_weight += passenger.weight
                        elif is_main_car(car_id) and shaft.mode == MODE_REP_ACCEPT and shaft.maint is not None:
                            if (
                                actor_id != shaft.maint.request.worker_id
                                or floor_name != INITIAL_FLOOR
                                or car.onboard_passengers
                                or shaft.maint.worker_onboard
                                or shaft.maint.worker_exited
                            ):
                                raise JudgeFailure(f"invalid maintenance worker IN {actor_id}", line_number, line)
                            shaft.maint.worker_onboard = True
                        else:
                            raise JudgeFailure(f"unknown actor {actor_id}", line_number, line)
                    else:
                        out_type = event.group(1)
                        actor_id = int(event.group(2))
                        floor_name = event.group(3)
                        if (not car.door_open) or floor_name != car.current_floor:
                            raise JudgeFailure(f"invalid OUT for elevator {car_id}", line_number, line)
                        if (
                            is_main_car(car_id)
                            and shaft.mode == MODE_REP_ACCEPT
                            and shaft.maint is not None
                            and shaft.maint.worker_onboard
                        ):
                            raise JudgeFailure(
                                f"elevator {car_id} cannot unload actors after its maintenance worker enters",
                                line_number,
                                line,
                            )
                        if actor_id in passengers:
                            passenger = passengers[actor_id]
                            if (not passenger.onboard) or passenger.current_elevator != car_id or passenger.active_receive_elevator != car_id:
                                raise JudgeFailure(f"invalid passenger OUT {actor_id}", line_number, line)
                            if out_type == "S" and floor_name != passenger.to_floor:
                                raise JudgeFailure(f"passenger {actor_id} uses OUT-S at non-target floor", line_number, line)
                            if out_type == "F" and floor_name == passenger.to_floor:
                                raise JudgeFailure(f"passenger {actor_id} uses OUT-F at target floor", line_number, line)
                            passenger.onboard = False
                            passenger.current_elevator = None
                            passenger.current_floor = floor_name
                            passenger.active_receive_elevator = None
                            passenger.completed = out_type == "S"
                            if out_type == "S":
                                completed_passengers.add(actor_id)
                            car.onboard_passengers.remove(actor_id)
                            car.active_receives.remove(actor_id)
                            car.current_weight -= passenger.weight
                        elif is_main_car(car_id) and shaft.mode == MODE_TEST and shaft.maint is not None:
                            if (
                                actor_id != shaft.maint.request.worker_id
                                or out_type != "S"
                                or floor_name != INITIAL_FLOOR
                                or (not shaft.maint.worker_onboard)
                                or shaft.maint.worker_exited
                                or shaft.maint.test_phase != "door_open"
                            ):
                                raise JudgeFailure(f"invalid maintenance worker OUT {actor_id}", line_number, line)
                            shaft.maint.worker_onboard = False
                            shaft.maint.worker_exited = True
                            shaft.maint.test_phase = "worker_exited"
                        else:
                            raise JudgeFailure(f"unknown actor {actor_id}", line_number, line)
                    break
                if action == "maint_accept":
                    elevator_id = int(event.group(1))
                    shaft = shafts[elevator_id]
                    if shaft.mode != MODE_NORMAL or not pending_maint[elevator_id]:
                        raise JudgeFailure(f"unexpected MAINT-ACCEPT for elevator {elevator_id}", line_number, line)
                    request = pending_maint[elevator_id][0]
                    if less_than(timestamp, request.timestamp):
                        raise JudgeFailure(
                            f"MAINT-ACCEPT for elevator {elevator_id} precedes its input request",
                            line_number,
                            line,
                        )
                    if request.worker_id != int(event.group(2)) or request.target_floor != event.group(3):
                        raise JudgeFailure(f"maintenance accept mismatch for elevator {elevator_id}", line_number, line)
                    pending_maint[elevator_id].pop(0)
                    shaft.mode = MODE_REP_ACCEPT
                    shaft.maint = MaintContext(request=request, accepted_time=timestamp)
                    cars[elevator_id].next_arrive_not_after = None
                    cars[elevator_id].next_arrive_required_floor = None
                    if cars[elevator_id].next_arrive_not_before is None:
                        refresh_next_arrive_window(cars[elevator_id], shaft, timestamp)
                    break
                if action == "maint1":
                    elevator_id = int(event.group(1))
                    shaft = shafts[elevator_id]
                    car = cars[elevator_id]
                    if shaft.mode != MODE_REP_ACCEPT or shaft.maint is None or car.current_floor != INITIAL_FLOOR or car.door_open:
                        raise JudgeFailure(f"invalid MAINT1-BEGIN for elevator {elevator_id}", line_number, line)
                    if not shaft.maint.worker_onboard or car.onboard_passengers:
                        raise JudgeFailure(f"elevator {elevator_id} is not ready for MAINT1-BEGIN", line_number, line)
                    clear_active_receives(passengers, car)
                    shaft.mode = MODE_REPAIR
                    shaft.maint.begin_time = timestamp
                    refresh_next_arrive_window(car, shaft, timestamp)
                    break
                if action == "maint2":
                    elevator_id = int(event.group(1))
                    shaft = shafts[elevator_id]
                    car = cars[elevator_id]
                    if shaft.mode != MODE_REPAIR or shaft.maint is None or shaft.maint.begin_time is None:
                        raise JudgeFailure(f"invalid MAINT2-BEGIN for elevator {elevator_id}", line_number, line)
                    if less_than(timestamp, shaft.maint.begin_time + SPECIAL_WAIT_TIME):
                        raise JudgeFailure(f"elevator {elevator_id} does not wait long enough in REPAIR", line_number, line)
                    if car.current_floor != INITIAL_FLOOR or car.door_open or not shaft.maint.worker_onboard:
                        raise JudgeFailure(f"elevator {elevator_id} is not ready for MAINT2-BEGIN", line_number, line)
                    shaft.mode = MODE_TEST
                    shaft.maint.test_phase = "to_target"
                    refresh_next_arrive_window(car, shaft, timestamp)
                    break
                if action == "maint_end":
                    elevator_id = int(event.group(1))
                    shaft = shafts[elevator_id]
                    car = cars[elevator_id]
                    if shaft.mode != MODE_TEST or shaft.maint is None:
                        raise JudgeFailure(f"invalid MAINT-END for elevator {elevator_id}", line_number, line)
                    if car.current_floor != INITIAL_FLOOR or car.door_open or not shaft.maint.worker_exited or shaft.maint.worker_onboard:
                        raise JudgeFailure(f"elevator {elevator_id} is not ready for MAINT-END", line_number, line)
                    if shaft.maint.test_phase != "ready_end" or less_than(shaft.maint.accepted_time + MAINT_COMPLETE_LIMIT, timestamp):
                        raise JudgeFailure(f"elevator {elevator_id} violates maintenance completion rules", line_number, line)
                    shaft.mode = MODE_NORMAL
                    shaft.maint = None
                    refresh_next_arrive_window(car, shaft, timestamp)
                    break
                if action == "update_accept":
                    elevator_id = int(event.group(1))
                    shaft = shafts[elevator_id]
                    if shaft.mode != MODE_NORMAL or not pending_update[elevator_id]:
                        raise JudgeFailure(f"unexpected UPDATE-ACCEPT for shaft {elevator_id}", line_number, line)
                    request = pending_update[elevator_id][0]
                    if less_than(timestamp, request.timestamp):
                        raise JudgeFailure(
                            f"UPDATE-ACCEPT for shaft {elevator_id} precedes its input request",
                            line_number,
                            line,
                        )
                    pending_update[elevator_id].pop(0)
                    shaft.mode = MODE_UP_ACCEPT
                    shaft.update = UpdateContext(accepted_time=timestamp)
                    cars[elevator_id].next_arrive_not_after = None
                    cars[elevator_id].next_arrive_required_floor = None
                    if cars[elevator_id].next_arrive_not_before is None:
                        refresh_next_arrive_window(cars[elevator_id], shaft, timestamp)
                    break
                if action == "update_begin":
                    elevator_id = int(event.group(1))
                    shaft = shafts[elevator_id]
                    car = cars[elevator_id]
                    if shaft.mode != MODE_UP_ACCEPT or shaft.update is None or car.current_floor != UPDATE_FLOOR or car.door_open or car.onboard_passengers:
                        raise JudgeFailure(f"invalid UPDATE-BEGIN for shaft {elevator_id}", line_number, line)
                    clear_active_receives(passengers, car)
                    shaft.mode = MODE_UPDATE
                    shaft.update.begin_time = timestamp
                    refresh_next_arrive_window(car, shaft, timestamp)
                    break
                if action == "update_end":
                    elevator_id = int(event.group(1))
                    shaft = shafts[elevator_id]
                    sub_car = cars[elevator_id + ELEVATOR_COUNT]
                    if shaft.mode != MODE_UPDATE or shaft.update is None or shaft.update.begin_time is None:
                        raise JudgeFailure(f"invalid UPDATE-END for shaft {elevator_id}", line_number, line)
                    if less_than(timestamp, shaft.update.begin_time + SPECIAL_WAIT_TIME) or less_than(shaft.update.accepted_time + UPDATE_COMPLETE_LIMIT, timestamp):
                        raise JudgeFailure(f"shaft {elevator_id} violates update completion rules", line_number, line)
                    if sub_car.current_floor != INITIAL_FLOOR or sub_car.door_open or sub_car.onboard_passengers or sub_car.active_receives:
                        raise JudgeFailure(f"sub elevator {sub_car.elevator_id} is not reset at UPDATE-END", line_number, line)
                    shaft.mode = MODE_DOUBLE
                    shaft.update = None
                    refresh_next_arrive_window(cars[elevator_id], shaft, timestamp)
                    refresh_next_arrive_window(sub_car, shaft, timestamp)
                    validate_double_layout(shaft, cars, line_number, line)
                    break
                if action == "recycle_accept":
                    elevator_id = int(event.group(1))
                    shaft = shafts[shaft_id_of(elevator_id)]
                    if shaft.mode != MODE_DOUBLE or not pending_recycle[elevator_id]:
                        raise JudgeFailure(f"unexpected RECYCLE-ACCEPT for elevator {elevator_id}", line_number, line)
                    request = pending_recycle[elevator_id][0]
                    if less_than(timestamp, request.timestamp):
                        raise JudgeFailure(
                            f"RECYCLE-ACCEPT for elevator {elevator_id} precedes its input request",
                            line_number,
                            line,
                        )
                    pending_recycle[elevator_id].pop(0)
                    shaft.mode = MODE_REC_ACCEPT
                    shaft.recycle = RecycleContext(accepted_time=timestamp)
                    if cars[elevator_id].next_arrive_not_before is None:
                        refresh_next_arrive_window(cars[elevator_id], shaft, timestamp)
                    if cars[shaft.shaft_id].next_arrive_not_before is None:
                        refresh_next_arrive_window(cars[shaft.shaft_id], shaft, timestamp)
                    break
                if action == "recycle_begin":
                    elevator_id = int(event.group(1))
                    shaft = shafts[shaft_id_of(elevator_id)]
                    car = cars[elevator_id]
                    if shaft.mode != MODE_REC_ACCEPT or shaft.recycle is None or car.current_floor != INITIAL_FLOOR or car.door_open or car.onboard_passengers:
                        raise JudgeFailure(f"invalid RECYCLE-BEGIN for elevator {elevator_id}", line_number, line)
                    clear_active_receives(passengers, car)
                    shaft.mode = MODE_RECYCLE
                    shaft.recycle.begin_time = timestamp
                    refresh_next_arrive_window(car, shaft, timestamp)
                    break
                if action == "recycle_end":
                    elevator_id = int(event.group(1))
                    shaft = shafts[shaft_id_of(elevator_id)]
                    car = cars[elevator_id]
                    if shaft.mode != MODE_RECYCLE or shaft.recycle is None or shaft.recycle.begin_time is None:
                        raise JudgeFailure(f"invalid RECYCLE-END for elevator {elevator_id}", line_number, line)
                    if less_than(timestamp, shaft.recycle.begin_time + SPECIAL_WAIT_TIME) or less_than(shaft.recycle.accepted_time + RECYCLE_COMPLETE_LIMIT, timestamp):
                        raise JudgeFailure(f"shaft {shaft.shaft_id} violates recycle completion rules", line_number, line)
                    if car.current_floor != INITIAL_FLOOR or car.door_open or car.onboard_passengers or car.active_receives:
                        raise JudgeFailure(f"sub elevator {elevator_id} is not reset at RECYCLE-END", line_number, line)
                    shaft.mode = MODE_NORMAL
                    shaft.recycle = None
                    main_car = cars[shaft.shaft_id]
                    if main_car.active_receives:
                        main_car.next_arrive_not_after = None
                        main_car.next_arrive_required_floor = None
                        if main_car.next_arrive_not_before is None:
                            refresh_next_arrive_window(main_car, shaft, timestamp)
                    elif (
                        main_car.current_floor == TRANSFER_FLOOR
                        and not main_car.door_open
                        and main_car.next_arrive_not_before is not None
                    ):
                        # The car may already have left F2 under the double-cabin
                        # no-RECEIVE exception.  Preserve exactly that one upward
                        # step, which must finish within one normal move interval.
                        main_car.next_arrive_not_after = timestamp + MOVE_TIME
                        main_car.next_arrive_required_floor = UPDATE_FLOOR
                    else:
                        refresh_next_arrive_window(main_car, shaft, timestamp)
                    break
            else:
                raise JudgeFailure("unknown output action", line_number, line)

    pending_specials = [
        *(f"MAINT-{elevator_id}" for elevator_id, queue in pending_maint.items() if queue),
        *(f"UPDATE-{elevator_id}" for elevator_id, queue in pending_update.items() if queue),
        *(f"RECYCLE-{elevator_id}" for elevator_id, queue in pending_recycle.items() if queue),
    ]
    if pending_specials:
        raise JudgeFailure(
            f"unaccepted special requests at program end: {', '.join(pending_specials)}"
        )

    for car_id, car in cars.items():
        if car.door_open:
            raise JudgeFailure(f"elevator {car_id} door is still open at program end")
        if car.current_weight > CAPACITY:
            raise JudgeFailure(f"elevator {car_id} exceeds capacity at program end")
        if car.onboard_passengers:
            raise JudgeFailure(f"elevator {car_id} still carries passengers at program end: {sorted(car.onboard_passengers)}")
    for shaft_id, shaft in shafts.items():
        if shaft.mode != MODE_NORMAL or shaft.maint is not None or shaft.update is not None or shaft.recycle is not None:
            raise JudgeFailure(f"shaft {shaft_id} has unfinished state at program end")
    unfinished = [person_id for person_id in passengers if person_id not in completed_passengers]
    if unfinished:
        raise JudgeFailure(f"unfinished passengers at program end: {unfinished}")


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
            # On Windows, force-kill the whole process tree to avoid locked temp files.
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
            # Each POSIX child is launched with start_new_session=True, so its
            # pid is also its process-group id.  Kill the group even when the
            # group leader has already exited; otherwise surviving descendants
            # would be orphaned on macOS/Linux.
            killpg = getattr(os, "killpg", None)
            sigkill = getattr(signal, "SIGKILL", None)
            if callable(killpg) and sigkill is not None:
                try:
                    killpg(process.pid, sigkill)
                except ProcessLookupError:
                    pass
                except OSError:
                    pass
    if not root_running:
        return True
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return process.poll() is not None


def register_process(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    ACTIVE_PROCESSES.add(process)


def unregister_process(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    ACTIVE_PROCESSES.discard(process)


def close_terminated_process_handle(process: subprocess.Popen | None) -> None:
    """Release CPython's Windows process handle after all Popen operations."""
    if not IS_WINDOWS or process is None or process.returncode is None:
        return
    handle = getattr(process, "_handle", None)
    close = getattr(handle, "Close", None)
    if callable(close):
        close()


def terminate_active_processes() -> None:
    for process in list(ACTIVE_PROCESSES):
        if terminate_process(process):
            ACTIVE_PROCESSES.discard(process)


def cleanup_temp_dir(path: Path, retry_seconds: float = 0.0) -> bool:
    if not path.exists():
        return True
    deadline = time.monotonic() + max(0.0, retry_seconds)
    while True:
        try:
            shutil.rmtree(path)
        except OSError:
            pass
        if not path.exists():
            return True
        if time.monotonic() >= deadline:
            return False
        # Windows can retain executable/file handles briefly after taskkill.
        time.sleep(0.05)


def register_temp_dir(path: Path) -> Path:
    resolved = path.resolve()
    ACTIVE_TEMP_DIRS.add(resolved)
    return resolved


def unregister_temp_dir(path: Path) -> None:
    ACTIVE_TEMP_DIRS.discard(path.resolve())


def cleanup_all_temp_dirs() -> None:
    global CLEANUP_IN_PROGRESS
    if CLEANUP_IN_PROGRESS:
        return
    CLEANUP_IN_PROGRESS = True
    try:
        # Only remove directories created by this process. Global glob cleanup
        # races with other concurrently running judger instances.
        for target in sorted(set(ACTIVE_TEMP_DIRS), key=lambda item: len(str(item)), reverse=True):
            if cleanup_temp_dir(target, retry_seconds=10.0):
                ACTIVE_TEMP_DIRS.discard(target)
    finally:
        CLEANUP_IN_PROGRESS = False


def on_exit_signal(signum: int, _frame: object) -> None:
    terminate_active_processes()
    cleanup_all_temp_dirs()
    if signum == getattr(signal, "SIGINT", None):
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)


def install_cleanup_guards() -> None:
    global CLEANUP_GUARDS_INSTALLED
    if CLEANUP_GUARDS_INSTALLED:
        return
    atexit.register(cleanup_all_temp_dirs)
    # atexit callbacks run in reverse order: terminate writers before deleting
    # their working directories.
    atexit.register(terminate_active_processes)
    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        target_signal = getattr(signal, signal_name, None)
        if target_signal is None:
            continue
        try:
            signal.signal(target_signal, on_exit_signal)
        except (ValueError, OSError):
            continue
    CLEANUP_GUARDS_INSTALLED = True


def output_size(paths: tuple[Path, ...]) -> int:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def read_process_text(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_PROCESS_OUTPUT_BYTES + 1)
    except OSError:
        return ""
    if len(content) > MAX_PROCESS_OUTPUT_BYTES:
        content = content[:MAX_PROCESS_OUTPUT_BYTES]
    return content.decode("utf-8", errors="replace")


def read_jar_manifest(project_jar: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(project_jar) as archive:
            raw_manifest = archive.read("META-INF/MANIFEST.MF").decode(
                "utf-8", errors="replace"
            )
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"cannot read project jar manifest from {project_jar}: {exc}") from exc
    unfolded: list[str] = []
    for raw_line in raw_manifest.replace("\r\n", "\n").split("\n"):
        if raw_line.startswith(" ") and unfolded:
            unfolded[-1] += raw_line[1:]
        else:
            unfolded.append(raw_line)
    attributes: dict[str, str] = {}
    for line in unfolded:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        attributes[key.strip().lower()] = value.strip()
    return attributes


def java_launch_command(project_jar: Path, lib_jar: Path) -> list[str]:
    manifest = read_jar_manifest(project_jar)
    main_class = manifest.get("main-class")
    if not main_class:
        raise RuntimeError(f"project jar manifest has no Main-Class: {project_jar}")
    classpath = [project_jar, lib_jar]
    for entry in manifest.get("class-path", "").split():
        dependency = (project_jar.parent / entry).resolve()
        if dependency not in classpath and dependency.is_file():
            classpath.append(dependency)
    return ["java", "-cp", os.pathsep.join(str(path) for path in classpath), main_class]


def datainput_launch_command(datainput_path: Path) -> list[str]:
    """Build a platform-safe feeder command without requiring script execute bits."""
    if datainput_path.suffix.casefold() == ".py":
        return [sys.executable, str(datainput_path)]
    return [str(datainput_path)]


def truncate_to_output_limit(path: Path) -> None:
    try:
        if path.stat().st_size > MAX_PROCESS_OUTPUT_BYTES:
            with path.open("r+b") as handle:
                handle.truncate(MAX_PROCESS_OUTPUT_BYTES)
    except OSError:
        pass


def run_case(case_path: Path, out_path: Path, err_path: Path, project_jar: Path, lib_jar: Path, datainput_exe: Path, timeout: int) -> tuple[str, str]:
    ensure_directory(out_path.parent)
    ensure_directory(err_path.parent)
    if out_path.exists():
        out_path.unlink()
    if err_path.exists():
        err_path.unlink()
    temp_dir = register_temp_dir(
        Path(tempfile.mkdtemp(prefix="hw7_judge_case_"))
    )
    feeder: subprocess.Popen | None = None
    java: subprocess.Popen | None = None
    windows_job: WindowsJob | None = None
    stdout_handle = None
    java_stderr_handle = None
    feeder_stderr_handle = None
    java_stderr_path = temp_dir / "java.stderr"
    feeder_stderr_path = temp_dir / "datainput.stderr"
    feeder_wait_timeout = False
    timed_out = False
    output_limited = False
    java_returncode: int | None = None
    feeder_returncode: int | None = None
    termination_failures: list[str] = []
    stderr_text = ""
    feeder_stderr = ""
    try:
        shutil.copy2(case_path, temp_dir / "stdin.txt")
        java_command = java_launch_command(project_jar, lib_jar)
        feeder_command = datainput_launch_command(datainput_exe)
        stdout_handle = out_path.open("wb")
        java_stderr_handle = java_stderr_path.open("wb")
        feeder_stderr_handle = feeder_stderr_path.open("wb")
        if IS_WINDOWS:
            windows_job = WindowsJob()
            feeder = windows_job.launch(
                feeder_command,
                cwd=temp_dir,
                stdout=subprocess.PIPE,
                stderr=feeder_stderr_handle,
                creationflags=WINDOWS_PROCESS_FLAGS,
            )
            register_process(feeder)
            java = windows_job.launch(
                java_command,
                cwd=temp_dir,
                stdin=feeder.stdout,
                stdout=stdout_handle,
                stderr=java_stderr_handle,
                creationflags=WINDOWS_PROCESS_FLAGS,
            )
            register_process(java)
        else:
            feeder = subprocess.Popen(
                feeder_command,
                cwd=temp_dir,
                stdout=subprocess.PIPE,
                stderr=feeder_stderr_handle,
                start_new_session=True,
            )
            register_process(feeder)
            java = subprocess.Popen(
                java_command,
                cwd=temp_dir,
                stdin=feeder.stdout,
                stdout=stdout_handle,
                stderr=java_stderr_handle,
                start_new_session=True,
            )
            register_process(java)
        if feeder.stdout is not None:
            feeder.stdout.close()
        started_at = time.monotonic()
        monitored_paths = (out_path, java_stderr_path, feeder_stderr_path)
        while java.poll() is None:
            if output_size(monitored_paths) > MAX_PROCESS_OUTPUT_BYTES:
                output_limited = True
                break
            sleep_seconds = next_poll_sleep(
                started_at,
                timeout,
                time.monotonic(),
                COMMUNICATE_POLL_INTERVAL,
            )
            if sleep_seconds is None:
                timed_out = True
                break
            time.sleep(sleep_seconds)
        if timed_out or output_limited:
            if windows_job is not None:
                windows_job.terminate()
            terminate_process(java)
            terminate_process(feeder)
        else:
            feeder_wait_deadline = time.monotonic() + FEEDER_WAIT_SECONDS
            while feeder.poll() is None:
                if output_size(monitored_paths) > MAX_PROCESS_OUTPUT_BYTES:
                    output_limited = True
                    break
                remaining = feeder_wait_deadline - time.monotonic()
                if remaining <= 0:
                    feeder_wait_timeout = True
                    break
                try:
                    feeder.wait(timeout=min(COMMUNICATE_POLL_INTERVAL, remaining))
                except subprocess.TimeoutExpired:
                    pass
            if feeder_wait_timeout or output_limited:
                if windows_job is not None:
                    windows_job.terminate()
                terminate_process(feeder)
        if output_size(monitored_paths) > MAX_PROCESS_OUTPUT_BYTES:
            output_limited = True
            if windows_job is not None:
                windows_job.terminate()
            terminate_process(java)
            terminate_process(feeder)
    finally:
        job_terminated = True
        if windows_job is not None:
            job_terminated = windows_job.terminate()
            windows_job.close()
        java_stopped = terminate_process(java)
        feeder_stopped = terminate_process(feeder)
        java_returncode = None if java is None else java.returncode
        feeder_returncode = None if feeder is None else feeder.returncode
        if java_stopped:
            unregister_process(java)
        elif java is not None:
            termination_failures.append(f"java pid {java.pid}")
        if feeder_stopped:
            unregister_process(feeder)
        elif feeder is not None:
            termination_failures.append(f"datainput pid {feeder.pid}")
        if not job_terminated:
            termination_failures.append("Windows Job Object")
        if feeder is not None and feeder.stdout is not None:
            try:
                feeder.stdout.close()
            except OSError:
                pass
        for handle in (stdout_handle, java_stderr_handle, feeder_stderr_handle):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        stderr_text = read_process_text(java_stderr_path)
        feeder_stderr = read_process_text(feeder_stderr_path)
        if java_stopped:
            close_terminated_process_handle(java)
        if feeder_stopped:
            close_terminated_process_handle(feeder)
        # On Windows a Popen object itself keeps a kernel process handle alive.
        # Release those references before deleting a directory that was used as
        # the child's working directory.
        if java_stopped:
            java = None
        if feeder_stopped:
            feeder = None
        if java_stopped and feeder_stopped and cleanup_temp_dir(temp_dir, retry_seconds=1.0):
            unregister_temp_dir(temp_dir)

    termination_note = ""
    if termination_failures:
        termination_note = (
            "[Judger] failed to terminate child process(es): "
            + ", ".join(termination_failures)
            + "\n"
        )
        combined_stderr = stderr_text + termination_note
        if feeder_stderr.strip():
            combined_stderr += f"[Datainput stderr]\n{feeder_stderr}"
        err_path.write_text(combined_stderr, encoding="utf-8")
        raise InfrastructureFailure(termination_note.strip())

    if output_limited:
        truncate_to_output_limit(out_path)
        combined_stderr = (
            stderr_text
            + f"[Judger] Output Limit Exceed: combined output exceeded "
            f"{MAX_PROCESS_OUTPUT_BYTES} bytes\n"
            + termination_note
        )
        if feeder_stderr.strip():
            combined_stderr += f"[Datainput stderr]\n{feeder_stderr}"
        err_path.write_text(combined_stderr, encoding="utf-8")
        raise JudgeFailure(
            f"Output Limit Exceed: combined output exceeded {MAX_PROCESS_OUTPUT_BYTES} bytes"
        )

    if timed_out:
        combined_stderr = (
            stderr_text
            + f"[Judger] Time Limit Exceed: did not finish within {timeout} seconds\n"
            + termination_note
        )
        if feeder_stderr.strip():
            combined_stderr += f"[Datainput stderr]\n{feeder_stderr}"
        err_path.write_text(combined_stderr, encoding="utf-8")
        raise JudgeFailure(f"Time Limit Exceed: did not finish within {timeout} seconds")

    combined_stderr = stderr_text
    if feeder_wait_timeout:
        combined_stderr += (
            f"[Judger] datainput did not exit within {FEEDER_WAIT_SECONDS:g} "
            "seconds and was terminated\n"
        )
    combined_stderr += termination_note
    if java_returncode not in {None, 0}:
        combined_stderr += f"[Judger] java exited with code {java_returncode}\n"
    if feeder_returncode not in {None, 0}:
        combined_stderr += f"[Judger] datainput exited with code {feeder_returncode}\n"
    if feeder_stderr.strip():
        combined_stderr += f"[Datainput stderr]\n{feeder_stderr}"
    err_path.write_text(combined_stderr, encoding="utf-8")
    stdout_text = read_process_text(out_path)
    return stdout_text, combined_stderr


def write_failure_log(log_path: Path, case_path: Path, out_path: Path, err_path: Path, message: str) -> None:
    ensure_directory(log_path.parent)
    log_path.write_text(
        "\n".join([f"case: {case_path.name}", f"input: {case_path}", f"stdout: {out_path}", f"stderr: {err_path}", f"message: {message}"]) + "\n",
        encoding="utf-8",
    )


def write_judge_failure_log(log_path: Path, case_path: Path, out_path: Path, err_path: Path, failure: JudgeFailure) -> None:
    ensure_directory(log_path.parent)
    parts = [f"case: {case_path.name}", f"input: {case_path}", f"stdout: {out_path}", f"stderr: {err_path}", f"message: {failure.message}"]
    if failure.line_number is not None:
        parts.append(f"line: {failure.line_number}")
    if failure.line_text is not None:
        parts.append(f"content: {failure.line_text}")
    log_path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def sort_case_paths(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=lambda path: (0, f"{int(path.stem):08d}") if path.stem.isdigit() else (1, path.stem))


def select_cases(input_dir: Path, selected_stems: list[str] | None) -> list[Path]:
    if not input_dir.is_dir():
        raise RuntimeError(f"input directory does not exist: {input_dir}")
    all_cases = sort_case_paths([path for path in input_dir.glob("*.in") if not path.name.endswith(".no.in")])
    if selected_stems is None:
        selected = all_cases
    else:
        if len(selected_stems) != len(set(selected_stems)):
            raise RuntimeError("--cases contains duplicate case stems")
        requested = set(selected_stems)
        available = {path.stem for path in all_cases}
        missing = sorted(requested - available)
        if missing:
            raise RuntimeError(f"requested input cases do not exist: {', '.join(missing)}")
        selected = [path for path in all_cases if path.stem in requested]
    if not selected:
        raise RuntimeError(f"no input cases selected from {input_dir}")
    return selected


@contextmanager
def snapshot_input_cases(case_paths: list[Path]):
    snapshot_dir = register_temp_dir(
        Path(tempfile.mkdtemp(prefix="hw7_input_snapshot_"))
    )
    case_pairs: list[tuple[Path, Path]] = []
    try:
        for source_path in case_paths:
            try:
                with source_path.open("rb") as handle:
                    content = handle.read(MAX_INPUT_BYTES + 1)
            except OSError as exc:
                raise RuntimeError(
                    f"cannot snapshot input case {source_path}: {exc}"
                ) from exc
            if len(content) > MAX_INPUT_BYTES:
                raise RuntimeError(
                    f"input case is too large to snapshot: {source_path}"
                )
            snapshot_path = snapshot_dir / source_path.name
            snapshot_path.write_bytes(content)
            case_pairs.append((source_path, snapshot_path))
        yield case_pairs
    finally:
        if cleanup_temp_dir(snapshot_dir, retry_seconds=1.0):
            unregister_temp_dir(snapshot_dir)


def require_runtime_file(path: Path, description: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"{description} does not exist: {path}")


def preflight_runtime(
    project_jar: Path,
    lib_jar: Path,
    datainput_exe: Path,
    building: bool,
) -> None:
    require_runtime_file(lib_jar, "official library jar")
    require_runtime_file(datainput_exe, "datainput feeder")
    if not building:
        require_runtime_file(project_jar, "project jar")
    if shutil.which("java") is None:
        raise RuntimeError("java executable was not found on PATH")
    if building:
        for executable in ("javac", "jar"):
            if shutil.which(executable) is None:
                raise RuntimeError(f"{executable} executable was not found on PATH")
    if datainput_exe.suffix.casefold() == ".py":
        try:
            with datainput_exe.open("rb") as handle:
                handle.read(1)
        except OSError as exc:
            raise RuntimeError(
                f"cannot read Python datainput feeder {datainput_exe}: {exc}"
            ) from exc
    elif not IS_WINDOWS:
        try:
            with datainput_exe.open("rb") as handle:
                header = handle.read(4)
        except OSError as exc:
            raise RuntimeError(f"cannot read datainput feeder {datainput_exe}: {exc}") from exc
        if header.startswith(b"MZ"):
            raise RuntimeError(
                f"datainput feeder is a Windows PE executable and cannot run on this platform: {datainput_exe}"
            )
        if not os.access(datainput_exe, os.X_OK):
            raise RuntimeError(f"datainput feeder is not executable: {datainput_exe}")


def _execute_snapshotted_cases(
    args: argparse.Namespace,
    case_pairs: list[tuple[Path, Path]],
    output_dir: Path,
    log_dir: Path,
    project_jar: Path,
    source_dir: Path,
    lib_jar: Path,
    datainput_exe: Path,
) -> int:
    requests_by_case: dict[Path, list[InputRequest]] = {}
    for _, case_path in case_pairs:
        requests = load_case(case_path)
        requests_by_case[case_path] = requests
        if args.mutual:
            validate_mutual_input_case(requests)
    latest_input_time = max(
        requests[-1].timestamp for requests in requests_by_case.values()
    )
    judge_timeout = resolve_judge_timeout(
        args.timeout,
        args.mutual,
        latest_input_time,
    )

    managed_default_jar = project_jar == DEFAULT_PROJECT_JAR.resolve()
    build_required = (
        args.rebuild
        or not project_jar.is_file()
        or (
            managed_default_jar
            and not managed_build_is_current(project_jar, source_dir, lib_jar, args.main_class)
        )
    )
    preflight_runtime(project_jar, lib_jar, datainput_exe, build_required)
    if build_required:
        build_project_jar(project_jar, source_dir, lib_jar, args.main_class)
    ensure_directory(output_dir)
    ensure_directory(log_dir)
    clean_matching_files(output_dir, "*.out")
    clean_matching_files(output_dir, "*.err.out")
    clean_matching_files(log_dir, "*.log")
    results: list[CaseResult] = []
    for original_case_path, case_path in case_pairs:
        out_path = output_dir / f"{original_case_path.stem}.out"
        err_path = output_dir / f"{original_case_path.stem}.err.out"
        log_path = log_dir / f"{original_case_path.stem}.log"
        try:
            requests = requests_by_case[case_path]
            if args.mutual:
                validate_mutual_input_case(requests)
            _, combined_stderr = run_case(case_path, out_path, err_path, project_jar, lib_jar, datainput_exe, judge_timeout)
            if combined_stderr.strip():
                message = "stderr is not empty, skipped semantic judging"
                write_failure_log(log_path, original_case_path, out_path, err_path, message)
                results.append(CaseResult(case_name=original_case_path.stem, passed=False, message=message))
                continue
            # Judge against the immutable request snapshot parsed before the
            # child starts, not against a path that could change mid-run.
            validate_output(original_case_path, out_path, requests=requests)
            results.append(CaseResult(case_name=original_case_path.stem, passed=True, message="passed"))
        except JudgeFailure as failure:
            write_judge_failure_log(log_path, original_case_path, out_path, err_path, failure)
            results.append(CaseResult(case_name=original_case_path.stem, passed=False, message=failure.message))
        except InfrastructureFailure as failure:
            message = f"infrastructure failure; aborted remaining cases: {failure}"
            write_failure_log(log_path, original_case_path, out_path, err_path, message)
            results.append(CaseResult(case_name=original_case_path.stem, passed=False, message=message))
            break
        except Exception as exc:  # noqa: BLE001
            write_failure_log(log_path, original_case_path, out_path, err_path, str(exc))
            results.append(CaseResult(case_name=original_case_path.stem, passed=False, message=str(exc)))
    passed_count = sum(1 for result in results if result.passed)
    failed_count = len(results) - passed_count
    for result in results:
        print(f"[{'PASS' if result.passed else 'FAIL'}] {result.case_name}: {result.message}")
    print(f"summary: {passed_count} passed, {failed_count} failed, total {len(results)}")
    return 0 if failed_count == 0 else 1


def execute_judging(
    args: argparse.Namespace,
    input_dir: Path,
    output_dir: Path,
    log_dir: Path,
    project_jar: Path,
    source_dir: Path,
    lib_jar: Path,
    datainput_exe: Path,
) -> int:
    cleanup_all_temp_dirs()
    original_case_paths = select_cases(input_dir, args.cases)
    with snapshot_input_cases(original_case_paths) as case_pairs:
        return _execute_snapshotted_cases(
            args,
            case_pairs,
            output_dir,
            log_dir,
            project_jar,
            source_dir,
            lib_jar,
            datainput_exe,
        )


def main() -> int:
    install_cleanup_guards()
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    log_dir = args.log_dir.resolve()
    project_jar = args.project_jar.resolve()
    source_dir = args.source_dir.resolve()
    lib_jar = args.lib_jar.resolve()
    datainput_exe = args.datainput.resolve()
    if not input_dir.is_dir():
        raise RuntimeError(f"input directory does not exist: {input_dir}")
    with exclusive_file_lock(JUDGER_LOCK_PATH, "judger workspace"):
        with exclusive_file_lock(
            input_dir / INPUT_CORPUS_LOCK_NAME,
            f"input corpus {input_dir}",
        ):
            return execute_judging(
                args,
                input_dir,
                output_dir,
                log_dir,
                project_jar,
                source_dir,
                lib_jar,
                datainput_exe,
            )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        terminate_active_processes()
        cleanup_all_temp_dirs()
        raise SystemExit(130)
