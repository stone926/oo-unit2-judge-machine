from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import re

ALL_FLOORS = ("B4", "B3", "B2", "B1", "F1", "F2", "F3", "F4", "F5", "F6", "F7")
FLOOR_TO_INDEX = {name: index for index, name in enumerate(ALL_FLOORS)}
INITIAL_FLOOR = "F1"
ELEVATOR_COUNT = 6
CAPACITY = 400
MOVE_TIME = Decimal("0.4")
TEST_MOVE_TIME = Decimal("0.2")
DOOR_TIME = Decimal("0.4")
REPAIR_TIME = Decimal("1.0")
MAINT_COMPLETE_LIMIT = Decimal("7.0")
TIMESTAMP_EPS = Decimal("0.000001")
MAINT_TARGET_FLOORS = ("B2", "B1", "F2", "F3")

PERSON_INPUT_LINE_RE = re.compile(
    r"^\[(\d+\.\d)\](\d+)-WEI-(\d+)-FROM-(B[1-4]|F[1-7])-TO-(B[1-4]|F[1-7])$"
)
MAINT_INPUT_LINE_RE = re.compile(
    r"^\[(\d+\.\d)\]MAINT-([1-6])-(\d+)-(B[12]|F[23])$"
)
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


InputRequest = PersonRequest | MaintRequest


def floor_to_index(name: str) -> int:
    index = FLOOR_TO_INDEX.get(name)
    if index is None:
        raise CaseFormatError(f"unknown floor: {name}")
    return index


def request_unique_id(request: InputRequest) -> int:
    if isinstance(request, PersonRequest):
        return request.person_id
    return request.worker_id


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

    raise CaseFormatError(
        f"{path}:{line_number}: invalid input line format: {raw_line.rstrip()}"
    )


def load_case(path: Path) -> list[InputRequest]:
    requests: list[InputRequest] = []
    seen_ids: set[int] = set()
    maint_timestamps_by_elevator = {elevator_id: [] for elevator_id in range(1, ELEVATOR_COUNT + 1)}

    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\r\n")
            if line == "":
                raise CaseFormatError(
                    f"{path}:{line_number}: blank lines are not allowed in input"
                )
            request = parse_input_line(line, path, line_number)
            unique_id = request_unique_id(request)
            if unique_id in seen_ids:
                raise CaseFormatError(
                    f"{path}:{line_number}: duplicated request id {unique_id}"
                )
            if requests and request.timestamp < requests[-1].timestamp:
                raise CaseFormatError(
                    f"{path}:{line_number}: input timestamps must be nondecreasing"
                )
            if isinstance(request, MaintRequest):
                last_timestamps = maint_timestamps_by_elevator[request.elevator_id]
                if last_timestamps and request.timestamp - last_timestamps[-1] < Decimal("8.0"):
                    raise CaseFormatError(
                        f"{path}:{line_number}: maintenance requests for elevator "
                        f"{request.elevator_id} must be at least 8.0s apart"
                    )
                last_timestamps.append(request.timestamp)
            seen_ids.add(unique_id)
            requests.append(request)

    if not 1 <= len(requests) <= 100:
        raise CaseFormatError(f"{path}: request count must be in [1, 100]")
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
    else:
        payload = (
            f"MAINT-{request.elevator_id}-{request.worker_id}-"
            f"{request.target_floor}"
        )
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
