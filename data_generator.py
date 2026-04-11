from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
import random

from judge_common import (
    ELEVATOR_COUNT,
    ALL_FLOORS,
    MAINT_TARGET_FLOORS,
    InputRequest,
    MaintRequest,
    PersonRequest,
    clean_matching_files,
    ensure_directory,
    format_input_timestamp,
    load_case,
    write_case,
    write_case_without_timestamp,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "in"
DEFAULT_MODE = "default"
MUTUAL_MODE = "mutual"
DEFAULT_MIN_REQUESTS = 50
DEFAULT_MAX_REQUESTS = 100
MUTUAL_FIRST_TENTHS = 10
MUTUAL_LAST_TENTHS = 500
MUTUAL_MAX_REQUESTS = 70
MAINT_GAP_TENTHS = 80
DEFAULT_FIRST_TENTHS_MIN = 20
DEFAULT_FIRST_TENTHS_MAX = 260
DEFAULT_MAINT_EXTRA_TENTHS = 120
DEFAULT_SPAN_PER_PERSON_MIN = 2
DEFAULT_SPAN_PER_PERSON_MAX = 5
DEFAULT_SPAN_BY_CASE_TYPE = (
    (520, 900),
    (360, 760),
    (700, 1250),
    (850, 1500),
)


def random_floor_pair(rng: random.Random) -> tuple[str, str]:
    from_floor = rng.choice(ALL_FLOORS)
    to_floor = rng.choice(ALL_FLOORS)
    while to_floor == from_floor:
        to_floor = rng.choice(ALL_FLOORS)
    return from_floor, to_floor


def build_person(
    request_id: int,
    tenths: int,
    from_floor: str,
    to_floor: str,
    weight: int,
) -> PersonRequest:
    return PersonRequest(
        timestamp=Decimal(format_input_timestamp(tenths)),
        person_id=request_id,
        weight=weight,
        from_floor=from_floor,
        to_floor=to_floor,
    )


def build_maint(
    request_id: int,
    tenths: int,
    elevator_id: int,
    target_floor: str,
) -> MaintRequest:
    return MaintRequest(
        timestamp=Decimal(format_input_timestamp(tenths)),
        elevator_id=elevator_id,
        worker_id=request_id,
        target_floor=target_floor,
    )


def generate_default_person_timestamps(
    case_type: int,
    rng: random.Random,
    person_count: int,
) -> list[int]:
    if person_count == 0:
        return []
    timestamps: list[int] = []
    current_tenths = rng.randint(0, 5)
    for offset in range(person_count):
        if case_type == 0:
            current_tenths += rng.randint(0, 4)
        elif case_type == 1:
            if offset > 0 and offset % rng.randint(3, 6) == 0:
                current_tenths += rng.randint(2, 5)
            else:
                current_tenths += rng.randint(0, 1)
        elif case_type == 2:
            if offset > 0 and offset % rng.randint(2, 5) == 0:
                current_tenths += rng.randint(5, 10)
            else:
                current_tenths += rng.randint(0, 2)
        else:
            current_tenths += rng.randint(0, 2)
        timestamps.append(current_tenths)
    return timestamps


def fit_timestamps_to_window(
    timestamps: list[int],
    lower_tenths: int,
    upper_tenths: int,
    rng: random.Random,
) -> list[int]:
    if not timestamps:
        return []
    if lower_tenths > upper_tenths:
        raise RuntimeError("invalid timestamp window")
    if len(timestamps) == 1:
        return [rng.randint(lower_tenths, upper_tenths)]

    source_min = timestamps[0]
    source_max = timestamps[-1]
    if source_max == source_min:
        return sorted(
            rng.randint(lower_tenths, upper_tenths)
            for _ in timestamps
        )

    source_span = source_max - source_min
    target_span = upper_tenths - lower_tenths
    projected = [
        lower_tenths + round((tenths - source_min) * target_span / source_span)
        for tenths in timestamps
    ]

    jittered = [
        max(lower_tenths, min(upper_tenths, tenths + rng.randint(-2, 2)))
        for tenths in projected
    ]
    jittered.sort()
    jittered[0] = max(jittered[0], lower_tenths)
    jittered[-1] = min(jittered[-1], upper_tenths)
    return jittered


def generate_mutual_person_timestamps(
    case_type: int,
    rng: random.Random,
    person_count: int,
) -> list[int]:
    baseline = generate_default_person_timestamps(case_type, rng, person_count)
    return fit_timestamps_to_window(
        timestamps=baseline,
        lower_tenths=MUTUAL_FIRST_TENTHS,
        upper_tenths=MUTUAL_LAST_TENTHS,
        rng=rng,
    )


def resolve_default_time_window(
    case_type: int,
    rng: random.Random,
    person_count: int,
) -> tuple[int, int]:
    first_tenths = rng.randint(DEFAULT_FIRST_TENTHS_MIN, DEFAULT_FIRST_TENTHS_MAX)
    span_min, span_max = DEFAULT_SPAN_BY_CASE_TYPE[case_type % len(DEFAULT_SPAN_BY_CASE_TYPE)]
    span = rng.randint(span_min, span_max)
    span += person_count * rng.randint(DEFAULT_SPAN_PER_PERSON_MIN, DEFAULT_SPAN_PER_PERSON_MAX)
    return first_tenths, first_tenths + span


def choose_maint_count(
    case_type: int,
    rng: random.Random,
    request_count: int,
    mutual: bool,
) -> int:
    if request_count <= 1:
        return 0

    # Mutual mode allows at most one MAINT per elevator.
    hard_cap = ELEVATOR_COUNT if mutual else 12
    limit = min(hard_cap, max(1, request_count // 4))

    if case_type == 0:
        return rng.randint(1, min(3, limit))
    if case_type == 1:
        lower = 2 if limit >= 2 else 1
        return rng.randint(lower, min(4, limit))
    if case_type == 2:
        lower = 2 if limit >= 2 else 1
        return rng.randint(lower, min(5, limit))

    lower = max(2, limit // 2)
    return rng.randint(min(lower, limit), limit)


def build_maint_elevator_plan(
    case_type: int,
    rng: random.Random,
    maint_count: int,
    mutual: bool,
) -> list[int]:
    if maint_count == 0:
        return []

    base_plan: list[int]
    if case_type == 1:
        base_plan = [rng.randint(1, ELEVATOR_COUNT) for _ in range(maint_count)]
    elif case_type == 2:
        base_plan = rng.sample(list(range(1, ELEVATOR_COUNT + 1)), k=min(maint_count, ELEVATOR_COUNT))
        while len(base_plan) < maint_count:
            base_plan.append(rng.randint(1, ELEVATOR_COUNT))
    else:
        repeated = rng.randint(1, ELEVATOR_COUNT)
        base_plan = [repeated]
        while len(base_plan) < maint_count:
            if rng.random() < 0.6:
                base_plan.append(repeated)
            else:
                base_plan.append(rng.randint(1, ELEVATOR_COUNT))

    if not mutual:
        return base_plan

    assigned: list[int] = []
    used: set[int] = set()
    for preferred in base_plan:
        if preferred not in used:
            selected = preferred
        else:
            candidates = [elevator_id for elevator_id in range(1, ELEVATOR_COUNT + 1) if elevator_id not in used]
            if not candidates:
                raise RuntimeError("mutual mode cannot assign maintenance to more than 6 elevators")
            selected = min(
                candidates,
                key=lambda elevator_id: (abs(elevator_id - preferred), elevator_id),
            )
        used.add(selected)
        assigned.append(selected)
    return assigned


def nearest_maint_slot(slots: list[int], preferred_tenths: int) -> int:
    return min(slots, key=lambda tenths: (abs(tenths - preferred_tenths), tenths))


def generate_maint_requests(
    case_type: int,
    rng: random.Random,
    maint_count: int,
    start_request_id: int,
    lower_tenths: int,
    upper_tenths: int,
    mutual: bool,
) -> list[MaintRequest]:
    if maint_count == 0:
        return []

    elevator_plan = build_maint_elevator_plan(case_type, rng, maint_count, mutual)
    base_timestamps = sorted(
        rng.randint(lower_tenths, upper_tenths)
        for _ in range(maint_count)
    )

    # Use fixed 8.0s slots to guarantee same-elevator maintenance spacing.
    slot_template = list(range(lower_tenths, upper_tenths + 1, MAINT_GAP_TENTHS))
    if not slot_template:
        raise RuntimeError("maintenance time window is too narrow")
    available_slots = {
        elevator_id: list(slot_template)
        for elevator_id in range(1, ELEVATOR_COUNT + 1)
    }

    requests: list[MaintRequest] = []
    for offset, (planned_elevator_id, preferred_tenths) in enumerate(zip(elevator_plan, base_timestamps)):
        selected_elevator_id = planned_elevator_id
        if not available_slots[selected_elevator_id]:
            candidates = [
                elevator_id
                for elevator_id, slots in available_slots.items()
                if slots
            ]
            if not candidates:
                raise RuntimeError("unable to allocate enough maintenance slots")
            selected_elevator_id = min(
                candidates,
                key=lambda elevator_id: (
                    abs(nearest_maint_slot(available_slots[elevator_id], preferred_tenths) - preferred_tenths),
                    elevator_id,
                ),
            )

        tenths = nearest_maint_slot(available_slots[selected_elevator_id], preferred_tenths)
        available_slots[selected_elevator_id].remove(tenths)
        requests.append(
            build_maint(
                request_id=start_request_id + offset,
                tenths=tenths,
                elevator_id=selected_elevator_id,
                target_floor=rng.choice(MAINT_TARGET_FLOORS),
            )
        )
    return requests


def validate_mutual_case(requests: list[InputRequest]) -> None:
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
                    "mutual mode requires each elevator to have at most one maintenance request"
                )


def sort_requests(requests: list[InputRequest]) -> list[InputRequest]:
    def sort_key(request: InputRequest) -> tuple[Decimal, int]:
        unique_id = request.person_id if isinstance(request, PersonRequest) else request.worker_id
        return request.timestamp, unique_id

    return sorted(requests, key=sort_key)


def generate_case(
    case_index: int,
    rng: random.Random,
    request_count: int,
    start_request_id: int,
    mutual: bool,
) -> tuple[list[InputRequest], int]:
    case_type = (case_index - 1) % 4
    maint_count = choose_maint_count(case_type, rng, request_count, mutual)
    person_count = max(1, request_count - maint_count)
    maint_count = request_count - person_count

    if mutual:
        person_timestamps = generate_mutual_person_timestamps(case_type, rng, person_count)
        lower_tenths = MUTUAL_FIRST_TENTHS
        upper_tenths = MUTUAL_LAST_TENTHS
    else:
        baseline_person_timestamps = generate_default_person_timestamps(case_type, rng, person_count)
        lower_tenths, person_upper_tenths = resolve_default_time_window(case_type, rng, person_count)
        person_timestamps = fit_timestamps_to_window(
            timestamps=baseline_person_timestamps,
            lower_tenths=lower_tenths,
            upper_tenths=person_upper_tenths,
            rng=rng,
        )
        upper_tenths = person_upper_tenths + DEFAULT_MAINT_EXTRA_TENTHS

    requests: list[InputRequest] = []
    next_request_id = start_request_id
    for tenths in person_timestamps:
        from_floor, to_floor = random_floor_pair(rng)
        requests.append(
            build_person(
                request_id=next_request_id,
                tenths=tenths,
                from_floor=from_floor,
                to_floor=to_floor,
                weight=rng.randint(50, 100),
            )
        )
        next_request_id += 1

    maint_requests = generate_maint_requests(
        case_type=case_type,
        rng=rng,
        maint_count=maint_count,
        start_request_id=next_request_id,
        lower_tenths=lower_tenths,
        upper_tenths=upper_tenths,
        mutual=mutual,
    )
    requests.extend(maint_requests)
    next_request_id += len(maint_requests)

    requests = sort_requests(requests)
    if mutual:
        validate_mutual_case(requests)
    return requests, next_request_id


def resolve_request_bounds(
    mutual: bool,
    min_requests: int | None,
    max_requests: int | None,
) -> tuple[int, int]:
    resolved_min = DEFAULT_MIN_REQUESTS if min_requests is None else min_requests
    resolved_max = MUTUAL_MAX_REQUESTS if mutual else DEFAULT_MAX_REQUESTS
    if max_requests is not None:
        resolved_max = max_requests
    return resolved_min, resolved_max


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate elevator hw6 test cases.")
    parser.add_argument("--count", type=int, default=20, help="number of cases to generate")
    parser.add_argument(
        "--mutual",
        action="store_true",
        help="generate mutual-test-friendly cases",
    )
    parser.add_argument(
        "--min-requests",
        type=int,
        default=None,
        help="minimum total requests in each case",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="maximum total requests in each case",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory used to write *.in files",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    min_requests, max_requests = resolve_request_bounds(args.mutual, args.min_requests, args.max_requests)
    if args.count <= 0:
        raise SystemExit("--count must be positive")
    if min_requests <= 0 or max_requests <= 0:
        raise SystemExit("request count bounds must be positive")
    if min_requests > max_requests:
        raise SystemExit("--min-requests cannot be greater than --max-requests")
    if args.mutual and max_requests > MUTUAL_MAX_REQUESTS:
        raise SystemExit(f"--max-requests cannot exceed {MUTUAL_MAX_REQUESTS} in mutual mode")
    if not args.mutual and max_requests > DEFAULT_MAX_REQUESTS:
        raise SystemExit(f"--max-requests cannot exceed {DEFAULT_MAX_REQUESTS}")

    seed = random.SystemRandom().getrandbits(64)
    rng = random.Random(seed)
    output_dir = args.output_dir.resolve()
    ensure_directory(output_dir)
    clean_matching_files(output_dir, "*.in")

    next_request_id = 1
    for case_index in range(1, args.count + 1):
        request_count = rng.randint(min_requests, max_requests)
        requests, next_request_id = generate_case(
            case_index=case_index,
            rng=rng,
            request_count=request_count,
            start_request_id=next_request_id,
            mutual=args.mutual,
        )
        case_path = output_dir / f"{case_index}.in"
        no_timestamp_case_path = output_dir / f"{case_index}.no.in"
        write_case(case_path, requests)
        write_case_without_timestamp(no_timestamp_case_path, requests)
        load_case(case_path)

    print(f"generated {args.count} case(s) in {output_dir}")
    print(f"mode = {MUTUAL_MODE if args.mutual else DEFAULT_MODE}")
    print(f"seed = {seed}")


if __name__ == "__main__":
    main()
