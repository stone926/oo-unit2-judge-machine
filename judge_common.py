from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import re

FLOOR_NAMES = ("B4", "B3", "B2", "B1", "F1", "F2", "F3", "F4", "F5", "F6", "F7")
FLOOR_TO_INDEX = {name: index for index, name in enumerate(FLOOR_NAMES)}
INITIAL_FLOOR = "F1"
ELEVATOR_COUNT = 6
CAPACITY = 400
MOVE_TIME = Decimal("0.4")
DOOR_TIME = Decimal("0.4")
TIMESTAMP_EPS = Decimal("0.000001")

INPUT_LINE_RE = re.compile(
    r"^\[(\d+\.\d)\](\d+)-WEI-(\d+)-FROM-(B[1-4]|F[1-7])-TO-(B[1-4]|F[1-7])-BY-([1-6])$"
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
    elevator_id: int


def floor_to_index(name: str) -> int:
    index = FLOOR_TO_INDEX.get(name)
    if index is None:
        raise CaseFormatError(f"unknown floor: {name}")
    return index


def validate_request(request: PersonRequest) -> None:
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
    if not 1 <= request.elevator_id <= ELEVATOR_COUNT:
        raise CaseFormatError(
            f"person {request.person_id}: elevator id {request.elevator_id} is invalid"
        )


def parse_input_line(raw_line: str, path: Path, line_number: int) -> PersonRequest:
    line = raw_line.strip()
    match = INPUT_LINE_RE.fullmatch(line)
    if match is None:
        raise CaseFormatError(
            f"{path}:{line_number}: invalid input line format: {raw_line.rstrip()}"
        )
    request = PersonRequest(
        timestamp=Decimal(match.group(1)),
        person_id=int(match.group(2)),
        weight=int(match.group(3)),
        from_floor=match.group(4),
        to_floor=match.group(5),
        elevator_id=int(match.group(6)),
    )
    validate_request(request)
    return request


def load_case(path: Path) -> list[PersonRequest]:
    requests: list[PersonRequest] = []
    seen_ids: set[int] = set()
    terminated = False
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\r\n")
            if line == "":
                terminated = True
                break
            request = parse_input_line(line, path, line_number)
            if request.person_id in seen_ids:
                raise CaseFormatError(
                    f"{path}:{line_number}: duplicated person id {request.person_id}"
                )
            if requests and request.timestamp < requests[-1].timestamp:
                raise CaseFormatError(
                    f"{path}:{line_number}: input timestamps must be nondecreasing"
                )
            seen_ids.add(request.person_id)
            requests.append(request)
    if not terminated:
        raise CaseFormatError(f"{path}: input must end with an empty line")
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


def write_case(path: Path, requests: list[PersonRequest]) -> None:
    ensure_directory(path.parent)
    lines = [
        "[{}]{}-WEI-{}-FROM-{}-TO-{}-BY-{}".format(
            request.timestamp,
            request.person_id,
            request.weight,
            request.from_floor,
            request.to_floor,
            request.elevator_id,
        )
        for request in requests
    ]
    content = "\n".join(lines) + "\n\n"
    path.write_text(content, encoding="utf-8")
