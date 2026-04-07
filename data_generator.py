from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
import random

from judge_common import PersonRequest, clean_matching_files, ensure_directory, load_case, write_case

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "in"
ALL_FLOORS = ("B4", "B3", "B2", "B1", "F1", "F2", "F3", "F4", "F5", "F6", "F7")


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate elevator hw5 test cases.")
    parser.add_argument("--count", type=int, default=20, help="number of cases to generate")
    parser.add_argument(
        "--min-requests",
        type=int,
        default=20,
        help="minimum number of requests in each case",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=100,
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
    if args.count <= 0:
        raise SystemExit("--count must be positive")
    if args.min_requests <= 0 or args.max_requests <= 0:
        raise SystemExit("request count bounds must be positive")
    if args.min_requests > args.max_requests:
        raise SystemExit("--min-requests cannot be greater than --max-requests")
    if args.max_requests > 100:
        raise SystemExit("--max-requests cannot exceed 100")

    rng = random.Random(args.seed)
    output_dir = args.output_dir.resolve()
    ensure_directory(output_dir)
    clean_matching_files(output_dir, "*.in")

    next_person_id = 1
    for case_index in range(1, args.count + 1):
        request_count = rng.randint(args.min_requests, args.max_requests)
        requests = generate_case(case_index, rng, request_count, next_person_id)
        next_person_id += request_count
        case_path = output_dir / f"{case_index}.in"
        write_case(case_path, requests)
        load_case(case_path)

    print(f"generated {args.count} case(s) in {output_dir}")
    print(f"seed = {args.seed}")


if __name__ == "__main__":
    main()
