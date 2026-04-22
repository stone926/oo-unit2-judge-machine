from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import re

ALL_FLOORS = ("B4", "B3", "B2", "B1", "F1", "F2", "F3", "F4", "F5", "F6", "F7")
FLOOR_TO_INDEX = {name: index for index, name in enumerate(ALL_FLOORS)}
INITIAL_FLOOR = "F1"
TRANSFER_FLOOR = "F2"
UPDATE_FLOOR = "F3"
ELEVATOR_COUNT = 6
CAR_COUNT = 12
CAPACITY = 400
MOVE_TIME = Decimal("0.4")
TEST_MOVE_TIME = Decimal("0.2")
DOOR_TIME = Decimal("0.4")
SPECIAL_WAIT_TIME = Decimal("1.0")
MAINT_COMPLETE_LIMIT = Decimal("7.0")
UPDATE_COMPLETE_LIMIT = Decimal("6.0")
RECYCLE_COMPLETE_LIMIT = Decimal("6.0")
TIMESTAMP_EPS = Decimal("0.000001")
MAINT_TARGET_FLOORS = ("B2", "B1", "F2", "F3")
MUTUAL_MAX_REQUESTS = 70

PERSON_INPUT_LINE_RE = re.compile(
    r"^\[(\d+\.\d)\](\d+)-WEI-(\d+)-FROM-(B[1-4]|F[1-7])-TO-(B[1-4]|F[1-7])$"
)
MAINT_INPUT_LINE_RE = re.compile(
    r"^\[(\d+\.\d)\]MAINT-([1-6])-(\d+)-(B[12]|F[23])$"
)
UPDATE_INPUT_LINE_RE = re.compile(r"^\[(\d+\.\d)\]UPDATE-([1-6])$")
RECYCLE_INPUT_LINE_RE = re.compile(r"^\[(\d+\.\d)\]RECYCLE-([7-9]|1[0-2])$")
OUTPUT_LINE_RE = re.compile(r"^\[\s*(\d+(?:\.\d+)?)\](.+)$")


class CaseFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PersonRequest:
    timestamp: Decimal
    person_id: int
    weight: int
    from_floor: str
    to_floor: str


@dataclass(frozen=True, slots=True)
class MaintRequest:
    timestamp: Decimal
    elevator_id: int
    worker_id: int
    target_floor: str


@dataclass(frozen=True, slots=True)
class UpdateRequest:
    timestamp: Decimal
    elevator_id: int


@dataclass(frozen=True, slots=True)
class RecycleRequest:
    timestamp: Decimal
    elevator_id: int


InputRequest = PersonRequest | MaintRequest | UpdateRequest | RecycleRequest


def floor_to_index(name: str) -> int:
    index = FLOOR_TO_INDEX.get(name)
    if index is None:
        raise CaseFormatError(f"unknown floor: {name}")
    return index


def validate_person_request(request: PersonRequest) -> None:
    if request.person_id <= 0:
        raise CaseFormatError("person id must be positive")
    if not 50 <= request.weight <= 100:
        raise CaseFormatError(
            f"person {request.person_id}: weight {request.weight} is out of range [50, 100]"
        )
    if request.from_floor == request.to_floor:
        raise CaseFormatError(
            f"person {request.person_id}: from floor and to floor must be different"
        )


def validate_maint_request(request: MaintRequest) -> None:
    if not 1 <= request.elevator_id <= ELEVATOR_COUNT:
        raise CaseFormatError(
            f"maintenance worker {request.worker_id}: elevator id {request.elevator_id} is invalid"
        )
    if request.worker_id <= 0:
        raise CaseFormatError("maintenance worker id must be positive")
    if request.target_floor not in MAINT_TARGET_FLOORS:
        raise CaseFormatError(
            f"maintenance worker {request.worker_id}: target floor {request.target_floor} is invalid"
        )


def validate_update_request(request: UpdateRequest) -> None:
    if not 1 <= request.elevator_id <= ELEVATOR_COUNT:
        raise CaseFormatError(f"update request elevator id {request.elevator_id} is invalid")


def validate_recycle_request(request: RecycleRequest) -> None:
    if not 7 <= request.elevator_id <= CAR_COUNT:
        raise CaseFormatError(f"recycle request elevator id {request.elevator_id} is invalid")


def parse_input_line(raw_line: str, path: Path, line_number: int) -> InputRequest:
    line = raw_line.strip()
    person_match = PERSON_INPUT_LINE_RE.fullmatch(line)
    if person_match is not None:
        request = PersonRequest(
            timestamp=Decimal(person_match.group(1)),
            person_id=int(person_match.group(2)),
            weight=int(person_match.group(3)),
            from_floor=person_match.group(4),
            to_floor=person_match.group(5),
        )
        validate_person_request(request)
        return request

    maint_match = MAINT_INPUT_LINE_RE.fullmatch(line)
    if maint_match is not None:
        request = MaintRequest(
            timestamp=Decimal(maint_match.group(1)),
            elevator_id=int(maint_match.group(2)),
            worker_id=int(maint_match.group(3)),
            target_floor=maint_match.group(4),
        )
        validate_maint_request(request)
        return request

    update_match = UPDATE_INPUT_LINE_RE.fullmatch(line)
    if update_match is not None:
        request = UpdateRequest(
            timestamp=Decimal(update_match.group(1)),
            elevator_id=int(update_match.group(2)),
        )
        validate_update_request(request)
        return request

    recycle_match = RECYCLE_INPUT_LINE_RE.fullmatch(line)
    if recycle_match is not None:
        request = RecycleRequest(
            timestamp=Decimal(recycle_match.group(1)),
            elevator_id=int(recycle_match.group(2)),
        )
        validate_recycle_request(request)
        return request

    raise CaseFormatError(
        f"{path}:{line_number}: invalid input line format: {raw_line.rstrip()}"
    )


def load_case(path: Path) -> list[InputRequest]:
    requests: list[InputRequest] = []
    seen_person_ids: set[int] = set()
    seen_worker_ids: set[int] = set()
    maint_timestamps_by_elevator = {elevator_id: [] for elevator_id in range(1, ELEVATOR_COUNT + 1)}

    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\r\n")
            if line == "":
                raise CaseFormatError(f"{path}:{line_number}: blank lines are not allowed in input")
            request = parse_input_line(line, path, line_number)
            if requests and request.timestamp < requests[-1].timestamp:
                raise CaseFormatError(f"{path}:{line_number}: input timestamps must be nondecreasing")
            if isinstance(request, PersonRequest):
                if request.person_id in seen_person_ids or request.person_id in seen_worker_ids:
                    raise CaseFormatError(f"{path}:{line_number}: duplicated request id {request.person_id}")
                seen_person_ids.add(request.person_id)
            elif isinstance(request, MaintRequest):
                if request.worker_id in seen_person_ids or request.worker_id in seen_worker_ids:
                    raise CaseFormatError(f"{path}:{line_number}: duplicated request id {request.worker_id}")
                last_timestamps = maint_timestamps_by_elevator[request.elevator_id]
                if last_timestamps and request.timestamp - last_timestamps[-1] < Decimal("8.0"):
                    raise CaseFormatError(
                        f"{path}:{line_number}: maintenance requests for elevator "
                        f"{request.elevator_id} must be at least 8.0s apart"
                    )
                last_timestamps.append(request.timestamp)
                seen_worker_ids.add(request.worker_id)
            requests.append(request)
    return requests


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def clean_matching_files(directory: Path, pattern: str) -> None:
    if not directory.exists():
        return
    for target in directory.glob(pattern):
        if target.is_file():
            target.unlink()


def format_input_timestamp(tenths: int) -> str:
    return f"{tenths // 10}.{tenths % 10}"


def request_to_line(request: InputRequest, with_timestamp: bool) -> str:
    if isinstance(request, PersonRequest):
        payload = (
            f"{request.person_id}-WEI-{request.weight}-FROM-"
            f"{request.from_floor}-TO-{request.to_floor}"
        )
    elif isinstance(request, MaintRequest):
        payload = f"MAINT-{request.elevator_id}-{request.worker_id}-{request.target_floor}"
    elif isinstance(request, UpdateRequest):
        payload = f"UPDATE-{request.elevator_id}"
    else:
        payload = f"RECYCLE-{request.elevator_id}"
    if not with_timestamp:
        return payload
    return f"[{request.timestamp}]{payload}"


def write_case(path: Path, requests: list[InputRequest]) -> None:
    ensure_directory(path.parent)
    content = "\n".join(request_to_line(request, with_timestamp=True) for request in requests)
    path.write_text(content, encoding="utf-8")


def write_case_without_timestamp(path: Path, requests: list[InputRequest]) -> None:
    ensure_directory(path.parent)
    content = "\n".join(request_to_line(request, with_timestamp=False) for request in requests)
    path.write_text(content, encoding="utf-8")


def validate_hw7_special_constraints(requests: list[InputRequest], mutual: bool) -> None:
    """Validate special requests (MAINT, UPDATE, RECYCLE) according to hw7 constraints."""
    in_double = {shaft_id: False for shaft_id in range(1, ELEVATOR_COUNT + 1)}
    last_special_time: dict[int, Decimal | None] = {
        shaft_id: None for shaft_id in range(1, ELEVATOR_COUNT + 1)
    }
    maint_count = {shaft_id: 0 for shaft_id in range(1, ELEVATOR_COUNT + 1)}
    update_count = {shaft_id: 0 for shaft_id in range(1, ELEVATOR_COUNT + 1)}
    recycle_count = {shaft_id: 0 for shaft_id in range(1, ELEVATOR_COUNT + 1)}

    for request in requests:
        shaft_id: int | None = None
        kind: str | None = None
        if isinstance(request, MaintRequest):
            shaft_id = request.elevator_id
            kind = "maint"
        elif isinstance(request, UpdateRequest):
            shaft_id = request.elevator_id
            kind = "update"
        elif isinstance(request, RecycleRequest):
            shaft_id = request.elevator_id - ELEVATOR_COUNT
            kind = "recycle"

        if shaft_id is None or kind is None:
            continue

        last_time = last_special_time[shaft_id]
        if last_time is not None and request.timestamp - last_time < Decimal("8.0"):
            raise RuntimeError(
                f"special requests on shaft {shaft_id} must be at least 8.0s apart"
            )
        last_special_time[shaft_id] = request.timestamp

        if kind == "maint":
            if in_double[shaft_id]:
                raise RuntimeError(f"MAINT on shaft {shaft_id} must be in NORMAL mode")
            maint_count[shaft_id] += 1
            if mutual and maint_count[shaft_id] > 1:
                raise RuntimeError(
                    f"mutual mode requires at most one MAINT per shaft, got {maint_count[shaft_id]} on shaft {shaft_id}"
                )
            continue

        if kind == "update":
            if in_double[shaft_id]:
                raise RuntimeError(f"UPDATE on shaft {shaft_id} must be in NORMAL mode")
            update_count[shaft_id] += 1
            if update_count[shaft_id] > 1:
                raise RuntimeError(
                    f"shaft {shaft_id} can have at most one UPDATE request"
                )
            in_double[shaft_id] = True
            continue

        if not in_double[shaft_id]:
            raise RuntimeError(f"RECYCLE on shaft {shaft_id} must be in DOUBLE mode")
        recycle_count[shaft_id] += 1
        if recycle_count[shaft_id] > 1:
            raise RuntimeError(
                f"shaft {shaft_id} can have at most one RECYCLE request"
            )
        in_double[shaft_id] = False

    for shaft_id in range(1, ELEVATOR_COUNT + 1):
        if in_double[shaft_id]:
            raise RuntimeError(
                f"shaft {shaft_id} ends in DOUBLE mode: generated UPDATE without matching RECYCLE"
            )
        if update_count[shaft_id] != recycle_count[shaft_id]:
            raise RuntimeError(
                f"shaft {shaft_id} has unmatched UPDATE/RECYCLE counts: {update_count[shaft_id]} vs {recycle_count[shaft_id]}"
            )


def validate_mutual_case(requests: list[InputRequest]) -> None:
    """Validate a case for mutual test mode compatibility."""
    if not requests:
        raise RuntimeError("mutual mode requires at least one request")
    if requests[0].timestamp < Decimal("1.0"):
        raise RuntimeError("mutual mode requires the first request time to be at least 1.0s")
    if requests[-1].timestamp > Decimal("50.0"):
        raise RuntimeError("mutual mode requires the last request time to be at most 50.0s")
    if len(requests) > MUTUAL_MAX_REQUESTS:
        raise RuntimeError(f"mutual mode request count must be at most {MUTUAL_MAX_REQUESTS}")
    maint_count_by_elevator = {elevator_id: 0 for elevator_id in range(1, ELEVATOR_COUNT + 1)}
    for request in requests:
        if isinstance(request, MaintRequest):
            maint_count_by_elevator[request.elevator_id] += 1
            if maint_count_by_elevator[request.elevator_id] > 1:
                raise RuntimeError(
                    f"mutual mode requires each elevator to have at most one maintenance request"
                )
