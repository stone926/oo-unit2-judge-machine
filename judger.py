from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from decimal import Decimal
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess

from judge_common import (
    CAPACITY,
    CAR_COUNT,
    DOOR_TIME,
    ELEVATOR_COUNT,
    INITIAL_FLOOR,
    MAINT_COMPLETE_LIMIT,
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
DEFAULT_LIB_JAR = SCRIPT_DIR / "dependency" / "elevator3-2026.jar"
DEFAULT_DATAINPUT_EXE = SCRIPT_DIR / "dependency" / "datainput"
DEFAULT_TIMEOUT = 120
MUTUAL_TIMEOUT = 180
MUTUAL_FIRST_REQUEST_TIME = Decimal("1.0")
MUTUAL_LAST_REQUEST_TIME = Decimal("50.0")
MUTUAL_MAX_REQUESTS = 70
IS_WINDOWS = os.name == "nt"
WINDOWS_PROCESS_FLAGS = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if IS_WINDOWS else 0

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run and judge elevator hw7 test cases.")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR, help="directory containing input case files (*.in)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="directory for judged program outputs")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="directory for failure logs")
    parser.add_argument("--project-jar", type=Path, default=DEFAULT_PROJECT_JAR, help="path to the project jar to execute")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR, help="source directory used when rebuilding the project jar")
    parser.add_argument("--lib-jar", type=Path, default=DEFAULT_LIB_JAR, help="path to the official elevator library jar")
    parser.add_argument("--datainput", type=Path, default=DEFAULT_DATAINPUT_EXE, help="path to the datainput feeder executable")
    parser.add_argument("--main-class", default="oo.Main", help="main class name used when rebuilding")
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="timeout seconds per case; defaults to 120 (or 180 with --mutual)",
    )
    parser.add_argument("--cases", nargs="*", default=None, help="optional case stems to run, such as: 1 2 3")
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
            f"cwd: {cwd}\ncmd: {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def build_project_jar(project_jar: Path, source_dir: Path, lib_jar: Path, main_class: str) -> None:
    java_files = sorted(str(path) for path in source_dir.rglob("*.java"))
    if not java_files:
        raise RuntimeError(f"no Java files found under {source_dir}")
    ensure_directory(project_jar.parent)
    if project_jar.exists():
        project_jar.unlink()
    temp_dir = project_jar.parent / ".judge_build_tmp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    classes_dir = temp_dir / "classes"
    classes_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = temp_dir / "MANIFEST.MF"
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
                ["jar", "--create", "--file", str(project_jar), "--manifest", str(manifest_path), "-C", str(classes_dir), "."],
                REPO_ROOT,
            )
        except RuntimeError:
            run_command(["jar", "cfm", str(project_jar), str(manifest_path), "-C", str(classes_dir), "."], REPO_ROOT)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


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


def validate_output(case_path: Path, output_path: Path) -> None:
    requests = load_case(case_path)
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
                        if passenger.active_receive_elevator is not None:
                            raise JudgeFailure(f"passenger {person_id} still has an unfinished RECEIVE", line_number, line)
                        if not can_receive_now(shaft, car_id):
                            raise JudgeFailure(f"elevator {car_id} cannot RECEIVE in state {shaft.mode}", line_number, line)
                        if planned_target_floor(shaft, car_id, passenger) is None:
                            raise JudgeFailure(f"elevator {car_id} cannot serve passenger {person_id}", line_number, line)
                        passenger.active_receive_elevator = car_id
                        car.active_receives.add(person_id)
                        refresh_next_arrive_window(car, shaft, timestamp)
                    elif action == "arrive":
                        floor_name = event.group(1)
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
                        if shaft.mode in {MODE_REPAIR, MODE_UPDATE} or ((not is_main_car(car_id)) and shaft.mode == MODE_RECYCLE):
                            raise JudgeFailure(f"elevator {car_id} cannot OPEN in state {shaft.mode}", line_number, line)
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
                        car.door_open = False
                        refresh_next_arrive_window(car, shaft, timestamp)
                    elif action == "in":
                        actor_id = int(event.group(1))
                        floor_name = event.group(2)
                        if (not car.door_open) or floor_name != car.current_floor:
                            raise JudgeFailure(f"invalid IN for elevator {car_id}", line_number, line)
                        if actor_id in passengers:
                            passenger = passengers[actor_id]
                            if passenger.active_receive_elevator != car_id or passenger.onboard or passenger.current_floor != floor_name:
                                raise JudgeFailure(f"invalid passenger IN {actor_id}", line_number, line)
                            passenger.onboard = True
                            passenger.current_elevator = car_id
                            car.onboard_passengers.add(actor_id)
                            car.current_weight += passenger.weight
                        elif is_main_car(car_id) and shaft.mode == MODE_REP_ACCEPT and shaft.maint is not None:
                            if actor_id != shaft.maint.request.worker_id or car.onboard_passengers:
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
                            if actor_id != shaft.maint.request.worker_id or out_type != "S" or floor_name != INITIAL_FLOOR:
                                raise JudgeFailure(f"invalid maintenance worker OUT {actor_id}", line_number, line)
                            shaft.maint.worker_onboard = False
                            shaft.maint.worker_exited = True
                            shaft.maint.test_phase = "ready_end"
                        else:
                            raise JudgeFailure(f"unknown actor {actor_id}", line_number, line)
                    break
                if action == "maint_accept":
                    elevator_id = int(event.group(1))
                    shaft = shafts[elevator_id]
                    if shaft.mode != MODE_NORMAL or not pending_maint[elevator_id]:
                        raise JudgeFailure(f"unexpected MAINT-ACCEPT for elevator {elevator_id}", line_number, line)
                    request = pending_maint[elevator_id].pop(0)
                    if request.worker_id != int(event.group(2)) or request.target_floor != event.group(3):
                        raise JudgeFailure(f"maintenance accept mismatch for elevator {elevator_id}", line_number, line)
                    shaft.mode = MODE_REP_ACCEPT
                    shaft.maint = MaintContext(request=request, accepted_time=timestamp)
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
                    pending_update[elevator_id].pop(0)
                    shaft.mode = MODE_UP_ACCEPT
                    shaft.update = UpdateContext(accepted_time=timestamp)
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
                    # Keep an already running main-car movement window unchanged.
                    # Recompute only when there is no in-progress active-receive movement.
                    main_car = cars[shaft.shaft_id]
                    if not (main_car.active_receives and main_car.next_arrive_not_before is not None):
                        refresh_next_arrive_window(main_car, shaft, timestamp)
                    break
            else:
                raise JudgeFailure("unknown output action", line_number, line)

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


def terminate_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if process.pid > 0:
        if IS_WINDOWS:
            # On Windows, force-kill the whole process tree to avoid locked temp files.
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except OSError:
                pass
        else:
            killpg = getattr(os, "killpg", None)
            sigkill = getattr(signal, "SIGKILL", None)
            if callable(killpg) and sigkill is not None:
                try:
                    killpg(process.pid, sigkill)
                except ProcessLookupError:
                    return
                except OSError:
                    pass
    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def cleanup_temp_dir(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)


def normalize_subprocess_text(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def run_case(case_path: Path, out_path: Path, err_path: Path, project_jar: Path, lib_jar: Path, datainput_exe: Path, timeout: int) -> tuple[str, str]:
    ensure_directory(out_path.parent)
    ensure_directory(err_path.parent)
    if out_path.exists():
        out_path.unlink()
    if err_path.exists():
        err_path.unlink()
    temp_dir = SCRIPT_DIR / f".judge_case_{case_path.stem}_tmp"
    if temp_dir.exists():
        cleanup_temp_dir(temp_dir)
    ensure_directory(temp_dir)
    feeder: subprocess.Popen | None = None
    java: subprocess.Popen | None = None
    stdout_text = ""
    stderr_text = ""
    feeder_stderr = ""
    feeder_wait_timeout = False
    timed_out = False
    try:
        local_datainput = temp_dir / datainput_exe.name
        shutil.copy2(case_path, temp_dir / "stdin.txt")
        shutil.copy2(project_jar, temp_dir / "code.jar")
        shutil.copy2(lib_jar, temp_dir / lib_jar.name)
        shutil.copy2(datainput_exe, local_datainput)
        if IS_WINDOWS:
            feeder = subprocess.Popen(
                [str(local_datainput)],
                cwd=temp_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=WINDOWS_PROCESS_FLAGS,
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
                creationflags=WINDOWS_PROCESS_FLAGS,
            )
        else:
            feeder = subprocess.Popen(
                [str(local_datainput)],
                cwd=temp_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
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
                start_new_session=True,
            )
        if feeder.stdout is not None:
            feeder.stdout.close()
        try:
            stdout_text, stderr_text = java.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout_text = normalize_subprocess_text(exc.stdout)
            stderr_text = normalize_subprocess_text(exc.stderr)
        if timed_out:
            terminate_process(java)
            terminate_process(feeder)
            if java is not None:
                try:
                    extra_stdout, extra_stderr = java.communicate(timeout=3)
                    stdout_text += normalize_subprocess_text(extra_stdout)
                    stderr_text += normalize_subprocess_text(extra_stderr)
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    pass
            if feeder is not None and feeder.stderr is not None:
                try:
                    feeder_stderr = feeder.stderr.read().decode("utf-8", errors="replace")
                except OSError:
                    feeder_stderr = ""
            timeout_stderr = stderr_text + f"[Judger] Time Limit Exceed: did not finish within {timeout} seconds\n"
            if feeder_stderr.strip():
                timeout_stderr += f"[Datainput stderr]\n{feeder_stderr}"
            out_path.write_text(stdout_text, encoding="utf-8")
            err_path.write_text(timeout_stderr, encoding="utf-8")
            raise JudgeFailure(f"Time Limit Exceed: did not finish within {timeout} seconds")

        if feeder.stderr is not None:
            feeder_stderr = feeder.stderr.read().decode("utf-8", errors="replace")
        try:
            feeder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            feeder_wait_timeout = True
    finally:
        terminate_process(java)
        terminate_process(feeder)
        cleanup_temp_dir(temp_dir)

    combined_stderr = stderr_text
    if feeder_wait_timeout:
        combined_stderr += "[Judger] datainput did not exit within 5 seconds and was terminated\n"
    if java is not None and java.returncode != 0:
        combined_stderr += f"[Judger] java exited with code {java.returncode}\n"
    if feeder is not None and feeder.returncode != 0:
        combined_stderr += f"[Judger] datainput exited with code {feeder.returncode}\n"
    if feeder_stderr.strip():
        combined_stderr += f"[Datainput stderr]\n{feeder_stderr}"
    out_path.write_text(stdout_text, encoding="utf-8")
    err_path.write_text(combined_stderr, encoding="utf-8")
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
    all_cases = sort_case_paths([path for path in input_dir.glob("*.in") if not path.name.endswith(".no.in")])
    return all_cases if selected_stems is None else [path for path in all_cases if path.stem in set(selected_stems)]


def main() -> None:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    log_dir = args.log_dir.resolve()
    project_jar = args.project_jar.resolve()
    source_dir = args.source_dir.resolve()
    lib_jar = args.lib_jar.resolve()
    datainput_exe = args.datainput.resolve()
    ensure_directory(output_dir)
    ensure_directory(log_dir)
    clean_matching_files(output_dir, "*.out")
    clean_matching_files(output_dir, "*.err.out")
    clean_matching_files(log_dir, "*.log")
    if args.rebuild or not project_jar.exists():
        build_project_jar(project_jar, source_dir, lib_jar, args.main_class)
    judge_timeout = args.timeout if args.timeout is not None else (MUTUAL_TIMEOUT if args.mutual else DEFAULT_TIMEOUT)
    results: list[CaseResult] = []
    for case_path in select_cases(input_dir, args.cases):
        out_path = output_dir / f"{case_path.stem}.out"
        err_path = output_dir / f"{case_path.stem}.err.out"
        log_path = log_dir / f"{case_path.stem}.log"
        try:
            if args.mutual:
                validate_mutual_input_case(load_case(case_path))
            _, combined_stderr = run_case(case_path, out_path, err_path, project_jar, lib_jar, datainput_exe, judge_timeout)
            if combined_stderr.strip():
                message = "stderr is not empty, skipped semantic judging"
                write_failure_log(log_path, case_path, out_path, err_path, message)
                results.append(CaseResult(case_name=case_path.stem, passed=False, message=message))
                continue
            validate_output(case_path, out_path)
            results.append(CaseResult(case_name=case_path.stem, passed=True, message="passed"))
        except JudgeFailure as failure:
            write_judge_failure_log(log_path, case_path, out_path, err_path, failure)
            results.append(CaseResult(case_name=case_path.stem, passed=False, message=failure.message))
        except Exception as exc:  # noqa: BLE001
            write_failure_log(log_path, case_path, out_path, err_path, str(exc))
            results.append(CaseResult(case_name=case_path.stem, passed=False, message=str(exc)))
    passed_count = sum(1 for result in results if result.passed)
    failed_count = len(results) - passed_count
    for result in results:
        print(f"[{'PASS' if result.passed else 'FAIL'}] {result.case_name}: {result.message}")
    print(f"summary: {passed_count} passed, {failed_count} failed, total {len(results)}")


if __name__ == "__main__":
    main()
