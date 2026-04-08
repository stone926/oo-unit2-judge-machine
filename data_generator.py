from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
from pathlib import Path
import random

from judge_common import PersonRequest, clean_matching_files, ensure_directory, load_case, write_case

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "in"
ALL_FLOORS = ("B4", "B3", "B2", "B1", "F1", "F2", "F3", "F4", "F5", "F6", "F7")
DEFAULT_MODE = "default"
MUTUAL_MODE = "mutual"
DEFAULT_MIN_REQUESTS = 20
DEFAULT_MAX_REQUESTS = 100
MUTUAL_FIRST_TENTHS = 10
MUTUAL_LAST_TENTHS = 500
MUTUAL_MAX_REQUESTS = 70
MUTUAL_MAX_REQUESTS_PER_ELEVATOR = 30


def random_floor_pair(rng: random.Random) -> tuple[str, str]:
    from_floor = rng.choice(ALL_FLOORS)
    to_floor = rng.choice(ALL_FLOORS)
    while to_floor == from_floor:
        to_floor = rng.choice(ALL_FLOORS)
    return from_floor, to_floor


def build_request(
    person_id: int,
    tenths: int,
    elevator_id: int,
    from_floor: str,
    to_floor: str,
    weight: int,
) -> PersonRequest:
    return PersonRequest(
        timestamp=Decimal(f"{tenths // 10}.{tenths % 10}"),
        person_id=person_id,
        weight=weight,
        from_floor=from_floor,
        to_floor=to_floor,
        elevator_id=elevator_id,
    )


def generate_balanced_case(
    rng: random.Random, request_count: int, start_person_id: int
) -> list[PersonRequest]:
    requests: list[PersonRequest] = []
    current_tenths = rng.randint(0, 5)
    for offset in range(request_count):
        current_tenths += rng.randint(0, 4)
        from_floor, to_floor = random_floor_pair(rng)
        requests.append(
            build_request(
                person_id=start_person_id + offset,
                tenths=current_tenths,
                elevator_id=rng.randint(1, 6),
                from_floor=from_floor,
                to_floor=to_floor,
                weight=rng.randint(50, 100),
            )
        )
    return requests


def generate_burst_case(
    rng: random.Random, request_count: int, start_person_id: int
) -> list[PersonRequest]:
    requests: list[PersonRequest] = []
    hotspot_elevator = rng.randint(1, 6)
    current_tenths = rng.randint(0, 3)
    group_size = rng.randint(3, 6)
    for offset in range(request_count):
        if offset > 0 and offset % group_size == 0:
            current_tenths += rng.randint(1, 3)
            group_size = rng.randint(3, 6)
        from_floor, to_floor = random_floor_pair(rng)
        requests.append(
            build_request(
                person_id=start_person_id + offset,
                tenths=current_tenths,
                elevator_id=hotspot_elevator if rng.random() < 0.7 else rng.randint(1, 6),
                from_floor=from_floor,
                to_floor=to_floor,
                weight=rng.randint(70, 100),
            )
        )
    return requests


def generate_single_elevator_case(
    rng: random.Random, request_count: int, start_person_id: int
) -> list[PersonRequest]:
    requests: list[PersonRequest] = []
    elevator_id = rng.randint(1, 6)
    current_tenths = rng.randint(0, 4)
    for offset in range(request_count):
        current_tenths += rng.randint(0, 2)
        from_floor, to_floor = random_floor_pair(rng)
        requests.append(
            build_request(
                person_id=start_person_id + offset,
                tenths=current_tenths,
                elevator_id=elevator_id,
                from_floor=from_floor,
                to_floor=to_floor,
                weight=rng.randint(50, 100),
            )
        )
    return requests


def generate_wave_case(
    rng: random.Random, request_count: int, start_person_id: int
) -> list[PersonRequest]:
    requests: list[PersonRequest] = []
    current_tenths = rng.randint(0, 2)
    for offset in range(request_count):
        if offset > 0 and offset % rng.randint(2, 5) == 0:
            current_tenths += rng.randint(6, 10)
        else:
            current_tenths += rng.randint(0, 1)
        from_floor, to_floor = random_floor_pair(rng)
        requests.append(
            build_request(
                person_id=start_person_id + offset,
                tenths=current_tenths,
                elevator_id=rng.randint(1, 6),
                from_floor=from_floor,
                to_floor=to_floor,
                weight=rng.randint(50, 100),
            )
        )
    return requests


def generate_case(
    case_index: int, rng: random.Random, request_count: int, start_person_id: int
) -> list[PersonRequest]:
    case_type = case_index % 4
    if case_type == 1:
        return generate_balanced_case(rng, request_count, start_person_id)
    if case_type == 2:
        return generate_burst_case(rng, request_count, start_person_id)
    if case_type == 3:
        return generate_single_elevator_case(rng, request_count, start_person_id)
    return generate_wave_case(rng, request_count, start_person_id)


def clamp_mutual_tenths(tenths: int) -> int:
    return max(MUTUAL_FIRST_TENTHS, min(MUTUAL_LAST_TENTHS, tenths))


def generate_mutual_timestamps(case_index: int, rng: random.Random, request_count: int) -> list[int]:
    case_type = case_index % 4
    if request_count == 1:
        return [rng.randint(MUTUAL_FIRST_TENTHS, MUTUAL_LAST_TENTHS)]

    if case_type == 1:
        return sorted(rng.randint(MUTUAL_FIRST_TENTHS, MUTUAL_LAST_TENTHS) for _ in range(request_count))

    if case_type == 2:
        center_count = min(request_count, rng.randint(3, 6))
        centers = sorted(rng.randint(MUTUAL_FIRST_TENTHS, MUTUAL_LAST_TENTHS) for _ in range(center_count))
        return sorted(
            clamp_mutual_tenths(centers[offset % center_count] + rng.randint(-8, 8))
            for offset in range(request_count)
        )

    if case_type == 3:
        span = MUTUAL_LAST_TENTHS - MUTUAL_FIRST_TENTHS
        return sorted(
            clamp_mutual_tenths(
                MUTUAL_FIRST_TENTHS + round(span * offset / (request_count - 1)) + rng.randint(-3, 3)
            )
            for offset in range(request_count)
        )

    segment_count = min(request_count, rng.randint(3, 5))
    segment_centers = []
    for segment_index in range(segment_count):
        left = MUTUAL_FIRST_TENTHS + (MUTUAL_LAST_TENTHS - MUTUAL_FIRST_TENTHS) * segment_index // segment_count
        right = MUTUAL_FIRST_TENTHS + (
            (MUTUAL_LAST_TENTHS - MUTUAL_FIRST_TENTHS) * (segment_index + 1) // segment_count
        )
        segment_centers.append(rng.randint(left, max(left, right)))
    return sorted(
        clamp_mutual_tenths(segment_centers[offset % segment_count] + rng.randint(-5, 5))
        for offset in range(request_count)
    )


def choose_mutual_elevator(
    rng: random.Random,
    elevator_counts: dict[int, int],
    weight_map: dict[int, int],
) -> int:
    available_ids = [
        elevator_id
        for elevator_id in range(1, 7)
        if elevator_counts[elevator_id] < MUTUAL_MAX_REQUESTS_PER_ELEVATOR
    ]
    if not available_ids:
        raise RuntimeError("no available elevator left for mutual mode")
    weights = [weight_map.get(elevator_id, 1) for elevator_id in available_ids]
    return rng.choices(available_ids, weights=weights, k=1)[0]


def generate_mutual_elevator_ids(case_index: int, rng: random.Random, request_count: int) -> list[int]:
    case_type = case_index % 4
    elevator_counts = {elevator_id: 0 for elevator_id in range(1, 7)}
    elevator_ids: list[int] = []
    hotspot = rng.randint(1, 6)
    partner = rng.randint(1, 6)
    while partner == hotspot:
        partner = rng.randint(1, 6)
    wave_order = rng.sample(list(range(1, 7)), k=6)

    for offset in range(request_count):
        if case_type == 1:
            weight_map = {
                elevator_id: MUTUAL_MAX_REQUESTS_PER_ELEVATOR - elevator_counts[elevator_id] + 1
                for elevator_id in range(1, 7)
            }
        elif case_type == 2:
            weight_map = {elevator_id: 1 for elevator_id in range(1, 7)}
            weight_map[hotspot] = 10
            weight_map[partner] = 4
        elif case_type == 3:
            weight_map = {elevator_id: 1 for elevator_id in range(1, 7)}
            weight_map[hotspot] = 7
            weight_map[partner] = 7
        else:
            phase_index = (offset * len(wave_order)) // request_count
            preferred = wave_order[phase_index]
            secondary = wave_order[(phase_index + 1) % len(wave_order)]
            weight_map = {elevator_id: 1 for elevator_id in range(1, 7)}
            weight_map[preferred] = 9
            weight_map[secondary] = 4

        elevator_id = choose_mutual_elevator(rng, elevator_counts, weight_map)
        elevator_counts[elevator_id] += 1
        elevator_ids.append(elevator_id)

    return elevator_ids


def validate_mutual_case(requests: list[PersonRequest]) -> None:
    if not requests:
        raise RuntimeError("mutual mode requires at least one request")
    if requests[0].timestamp < Decimal("1.0"):
        raise RuntimeError("mutual mode requires the first request time to be at least 1.0s")
    if requests[-1].timestamp > Decimal("50.0"):
        raise RuntimeError("mutual mode requires the last request time to be at most 50.0s")
    if len(requests) > MUTUAL_MAX_REQUESTS:
        raise RuntimeError(f"mutual mode request count must be at most {MUTUAL_MAX_REQUESTS}")

    elevator_counts = Counter(request.elevator_id for request in requests)
    for elevator_id, count in elevator_counts.items():
        if count > MUTUAL_MAX_REQUESTS_PER_ELEVATOR:
            raise RuntimeError(
                f"mutual mode elevator {elevator_id} has {count} requests, "
                f"exceeding {MUTUAL_MAX_REQUESTS_PER_ELEVATOR}"
            )


def generate_mutual_case(
    case_index: int,
    rng: random.Random,
    request_count: int,
    start_person_id: int,
) -> list[PersonRequest]:
    timestamps = generate_mutual_timestamps(case_index, rng, request_count)
    elevator_ids = generate_mutual_elevator_ids(case_index, rng, request_count)
    requests: list[PersonRequest] = []
    for offset, (tenths, elevator_id) in enumerate(zip(timestamps, elevator_ids)):
        from_floor, to_floor = random_floor_pair(rng)
        requests.append(
            build_request(
                person_id=start_person_id + offset,
                tenths=tenths,
                elevator_id=elevator_id,
                from_floor=from_floor,
                to_floor=to_floor,
                weight=rng.randint(50, 100),
            )
        )

    validate_mutual_case(requests)
    return requests


def resolve_request_bounds(mode: str, min_requests: int | None, max_requests: int | None) -> tuple[int, int]:
    resolved_min = DEFAULT_MIN_REQUESTS if min_requests is None else min_requests
    if mode == MUTUAL_MODE:
        resolved_max = MUTUAL_MAX_REQUESTS if max_requests is None else max_requests
    else:
        resolved_max = DEFAULT_MAX_REQUESTS if max_requests is None else max_requests
    return resolved_min, resolved_max


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate elevator hw5 test cases.")
    parser.add_argument("--count", type=int, default=20, help="number of cases to generate")
    parser.add_argument(
        "--mode",
        choices=(DEFAULT_MODE, MUTUAL_MODE),
        default=DEFAULT_MODE,
        help="generation mode; mutual mode follows inter-test limits",
    )
    parser.add_argument(
        "--min-requests",
        type=int,
        default=None,
        help="minimum number of requests in each case",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="maximum number of requests in each case",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260407,
        help="random seed for reproducible generation",
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
    min_requests, max_requests = resolve_request_bounds(args.mode, args.min_requests, args.max_requests)
    if args.count <= 0:
        raise SystemExit("--count must be positive")
    if min_requests <= 0 or max_requests <= 0:
        raise SystemExit("request count bounds must be positive")
    if min_requests > max_requests:
        raise SystemExit("--min-requests cannot be greater than --max-requests")
    if args.mode == MUTUAL_MODE:
        if min_requests > MUTUAL_MAX_REQUESTS:
            raise SystemExit(f"--min-requests cannot exceed {MUTUAL_MAX_REQUESTS} in mutual mode")
        if max_requests > MUTUAL_MAX_REQUESTS:
            raise SystemExit(f"--max-requests cannot exceed {MUTUAL_MAX_REQUESTS} in mutual mode")
    elif max_requests > DEFAULT_MAX_REQUESTS:
        raise SystemExit(f"--max-requests cannot exceed {DEFAULT_MAX_REQUESTS}")

    rng = random.Random(args.seed)
    output_dir = args.output_dir.resolve()
    ensure_directory(output_dir)
    clean_matching_files(output_dir, "*.in")

    next_person_id = 1
    for case_index in range(1, args.count + 1):
        request_count = rng.randint(min_requests, max_requests)
        if args.mode == MUTUAL_MODE:
            requests = generate_mutual_case(case_index, rng, request_count, next_person_id)
        else:
            requests = generate_case(case_index, rng, request_count, next_person_id)
        next_person_id += request_count
        case_path = output_dir / f"{case_index}.in"
        write_case(case_path, requests)
        load_case(case_path)

    print(f"generated {args.count} case(s) in {output_dir}")
    print(f"mode = {args.mode}")
    print(f"seed = {args.seed}")


if __name__ == "__main__":
    main()
