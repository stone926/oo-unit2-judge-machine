from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path
import random

from judge_common import (
    ALL_FLOORS,
    ELEVATOR_COUNT,
    InputRequest,
    MAINT_TARGET_FLOORS,
    MaintRequest,
    PersonRequest,
    RecycleRequest,
    UpdateRequest,
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
DEFAULT_LAST_REQUEST_LIMIT_SECONDS = Decimal("80.0")
MUTUAL_FIRST_TENTHS = 10
MUTUAL_LAST_TENTHS = 500
MUTUAL_MAX_REQUESTS = 70
DEFAULT_MAINT_RATIO = 0.16
DEFAULT_UPDATE_RATIO = 0.18

AUTO_MODE = "auto"
TIME_MODE_UNIFORM = "uniform"
TIME_MODE_BURST = "burst"
PICKUP_MODE_CLUSTERED = "clustered"
PICKUP_MODE_UNIFORM = "uniform"
DROPOFF_MODE_CLUSTERED = "clustered"
DROPOFF_MODE_UNIFORM = "uniform"
TIME_MODE_ORDER = (TIME_MODE_UNIFORM, TIME_MODE_BURST)
PICKUP_MODE_ORDER = (PICKUP_MODE_CLUSTERED, PICKUP_MODE_UNIFORM)
DROPOFF_MODE_ORDER = (DROPOFF_MODE_CLUSTERED, DROPOFF_MODE_UNIFORM)


@dataclass(frozen=True, slots=True)
class CasePattern:
    time_mode: str
    pickup_mode: str
    dropoff_mode: str


def parse_decimal_seconds(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal seconds: {raw}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    scaled = value * Decimal("10")
    if scaled != scaled.to_integral_value():
        raise argparse.ArgumentTypeError("value must use at most one decimal place")
    return value


def parse_ratio(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ratio: {raw}") from exc
    if value < 0 or value > 0.6:
        raise argparse.ArgumentTypeError("ratio must be in [0, 0.6]")
    return value


def seconds_to_tenths(seconds: Decimal) -> int:
    return int(seconds * Decimal("10"))


def clamp_tenths(tenths: int, lower_tenths: int, upper_tenths: int) -> int:
    return max(lower_tenths, min(upper_tenths, tenths))


def choose_clustered_floor(rng: random.Random, hotspots: tuple[str, ...]) -> str:
    if not hotspots:
        return rng.choice(ALL_FLOORS)
    if len(hotspots) == 1 or rng.random() < 0.72:
        return hotspots[0]
    if len(hotspots) >= 2 and rng.random() < 0.90:
        return hotspots[1]
    return rng.choice(ALL_FLOORS)


def build_hotspots(rng: random.Random) -> tuple[str, ...]:
    hotspot_count = 2 if rng.random() < 0.8 else 1
    return tuple(rng.sample(list(ALL_FLOORS), k=hotspot_count))


def choose_pickup_floor(pickup_mode: str, pickup_hotspots: tuple[str, ...], rng: random.Random) -> str:
    if pickup_mode == PICKUP_MODE_UNIFORM:
        return rng.choice(ALL_FLOORS)
    return choose_clustered_floor(rng, pickup_hotspots)


def choose_dropoff_floor(
    from_floor: str,
    dropoff_mode: str,
    dropoff_hotspots: tuple[str, ...],
    rng: random.Random,
) -> str:
    candidates = [floor for floor in ALL_FLOORS if floor != from_floor]
    if dropoff_mode == DROPOFF_MODE_CLUSTERED:
        for _ in range(4):
            selected = choose_clustered_floor(rng, dropoff_hotspots)
            if selected != from_floor:
                return selected
    return rng.choice(candidates)


def generate_floor_pairs(
    person_count: int,
    pickup_mode: str,
    dropoff_mode: str,
    rng: random.Random,
) -> list[tuple[str, str]]:
    pickup_hotspots = build_hotspots(rng) if pickup_mode == PICKUP_MODE_CLUSTERED else tuple()
    dropoff_hotspots = build_hotspots(rng) if dropoff_mode == DROPOFF_MODE_CLUSTERED else tuple()
    floor_pairs: list[tuple[str, str]] = []
    for _ in range(person_count):
        from_floor = choose_pickup_floor(pickup_mode, pickup_hotspots, rng)
        to_floor = choose_dropoff_floor(from_floor, dropoff_mode, dropoff_hotspots, rng)
        floor_pairs.append((from_floor, to_floor))
    return floor_pairs


def generate_person_timestamps(
    time_mode: str,
    rng: random.Random,
    person_count: int,
    lower_tenths: int,
    upper_tenths: int,
) -> list[int]:
    if person_count == 0:
        return []
    if time_mode == TIME_MODE_UNIFORM:
        if person_count == 1:
            return [rng.randint(lower_tenths, upper_tenths)]
        step = max(1, (upper_tenths - lower_tenths) // max(1, person_count - 1))
        values = [
            clamp_tenths(lower_tenths + i * step + rng.randint(-step // 3, step // 3), lower_tenths, upper_tenths)
            for i in range(person_count)
        ]
    else:
        center_count = min(6, max(2, person_count // 10))
        centers = [rng.randint(lower_tenths, upper_tenths) for _ in range(center_count)]
        values = []
        for _ in range(person_count):
            center = rng.choice(centers)
            values.append(clamp_tenths(center + rng.randint(-3, 3), lower_tenths, upper_tenths))
    values.sort()
    return values


def resolve_case_pattern(case_index: int, time_mode: str, pickup_mode: str, dropoff_mode: str) -> CasePattern:
    time_candidates = [time_mode] if time_mode != AUTO_MODE else list(TIME_MODE_ORDER)
    pickup_candidates = [pickup_mode] if pickup_mode != AUTO_MODE else list(PICKUP_MODE_ORDER)
    dropoff_candidates = [dropoff_mode] if dropoff_mode != AUTO_MODE else list(DROPOFF_MODE_ORDER)
    combinations = [
        CasePattern(candidate_time, candidate_pickup, candidate_dropoff)
        for candidate_time in time_candidates
        for candidate_pickup in pickup_candidates
        for candidate_dropoff in dropoff_candidates
    ]
    return combinations[(case_index - 1) % len(combinations)]


def build_person(request_id: int, tenths: int, from_floor: str, to_floor: str, weight: int) -> PersonRequest:
    return PersonRequest(
        timestamp=Decimal(format_input_timestamp(tenths)),
        person_id=request_id,
        weight=weight,
        from_floor=from_floor,
        to_floor=to_floor,
    )


def build_maint(request_id: int, tenths: int, elevator_id: int, target_floor: str) -> MaintRequest:
    return MaintRequest(
        timestamp=Decimal(format_input_timestamp(tenths)),
        elevator_id=elevator_id,
        worker_id=request_id,
        target_floor=target_floor,
    )


def build_update(tenths: int, elevator_id: int) -> UpdateRequest:
    return UpdateRequest(
        timestamp=Decimal(format_input_timestamp(tenths)),
        elevator_id=elevator_id,
    )


def build_recycle(tenths: int, elevator_id: int) -> RecycleRequest:
    return RecycleRequest(
        timestamp=Decimal(format_input_timestamp(tenths)),
        elevator_id=elevator_id,
    )


def choose_special_counts(request_count: int, maint_ratio: float, update_ratio: float) -> tuple[int, int]:
    max_cycle = min(ELEVATOR_COUNT, max(0, (request_count - 6) // 10))
    cycle_count = min(max_cycle, int(round(request_count * update_ratio / 2)))
    max_maint = min(ELEVATOR_COUNT - cycle_count, max(0, (request_count - 2 * cycle_count - 3) // 8))
    maint_count = min(max_maint, int(round(request_count * maint_ratio)))
    return maint_count, cycle_count


def generate_special_requests(
    rng: random.Random,
    next_request_id: int,
    lower_tenths: int,
    upper_tenths: int,
    maint_count: int,
    cycle_count: int,
    mutual: bool,
) -> tuple[list[InputRequest], int]:
    requests: list[InputRequest] = []
    shafts = list(range(1, ELEVATOR_COUNT + 1))
    rng.shuffle(shafts)
    cycle_shafts = sorted(shafts[:cycle_count])
    maint_shafts = sorted(shafts[cycle_count:cycle_count + maint_count])

    for elevator_id in maint_shafts:
        tenths = rng.randint(lower_tenths + 20, max(lower_tenths + 20, upper_tenths - 60))
        requests.append(build_maint(next_request_id, tenths, elevator_id, rng.choice(MAINT_TARGET_FLOORS)))
        next_request_id += 1

    for shaft_id in cycle_shafts:
        update_tenths = rng.randint(lower_tenths + 20, max(lower_tenths + 20, upper_tenths - 120))
        recycle_gap = rng.randint(80, 140 if not mutual else 110)
        recycle_tenths = min(upper_tenths, update_tenths + recycle_gap)
        if recycle_tenths <= update_tenths + 60:
            recycle_tenths = update_tenths + 70
        requests.append(build_update(update_tenths, shaft_id))
        requests.append(build_recycle(recycle_tenths, shaft_id + 6))
    return requests, next_request_id


def sort_requests(requests: list[InputRequest]) -> list[InputRequest]:
    def sort_key(request: InputRequest) -> tuple[Decimal, int, int]:
        if isinstance(request, PersonRequest):
            return request.timestamp, 0, request.person_id
        if isinstance(request, MaintRequest):
            return request.timestamp, 1, request.worker_id
        if isinstance(request, UpdateRequest):
            return request.timestamp, 2, request.elevator_id
        return request.timestamp, 3, request.elevator_id

    return sorted(requests, key=sort_key)


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
                    f"mutual mode requires each elevator to have at most one maintenance request"
                )


def resolve_request_bounds(mutual: bool, min_requests: int | None, max_requests: int | None) -> tuple[int, int]:
    resolved_min = DEFAULT_MIN_REQUESTS if min_requests is None else min_requests
    resolved_max = MUTUAL_MAX_REQUESTS if mutual else DEFAULT_MAX_REQUESTS
    if max_requests is not None:
        resolved_max = max_requests
    return resolved_min, resolved_max


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate elevator hw7 test cases.")
    parser.add_argument("--count", type=int, default=20, help="number of cases to generate")
    parser.add_argument("--mutual", action="store_true", help="generate mutual-test-friendly cases")
    parser.add_argument("--min-requests", type=int, default=None, help="minimum total requests in each case")
    parser.add_argument("--max-requests", type=int, default=None, help="maximum total requests in each case")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="directory used to write *.in files")
    parser.add_argument(
        "--last-request-limit",
        type=parse_decimal_seconds,
        default=DEFAULT_LAST_REQUEST_LIMIT_SECONDS,
        help="default-mode upper bound for the last request timestamp, in seconds",
    )
    parser.add_argument("--maint-ratio", type=parse_ratio, default=DEFAULT_MAINT_RATIO)
    parser.add_argument("--update-ratio", type=parse_ratio, default=DEFAULT_UPDATE_RATIO)
    parser.add_argument("--time-mode", choices=[AUTO_MODE, *TIME_MODE_ORDER], default=AUTO_MODE)
    parser.add_argument("--pickup-mode", choices=[AUTO_MODE, *PICKUP_MODE_ORDER], default=AUTO_MODE)
    parser.add_argument("--dropoff-mode", choices=[AUTO_MODE, *DROPOFF_MODE_ORDER], default=AUTO_MODE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    min_requests, max_requests = resolve_request_bounds(args.mutual, args.min_requests, args.max_requests)
    last_limit_tenths = seconds_to_tenths(args.last_request_limit)
    if args.count <= 0:
        raise SystemExit("--count must be positive")
    if min_requests <= 0 or max_requests <= 0 or min_requests > max_requests:
        raise SystemExit("invalid request count bounds")
    if args.mutual and max_requests > MUTUAL_MAX_REQUESTS:
        raise SystemExit(f"--max-requests cannot exceed {MUTUAL_MAX_REQUESTS} in mutual mode")
    if not args.mutual and last_limit_tenths < 10:
        raise SystemExit("--last-request-limit must be at least 1.0 in default mode")

    seed = random.SystemRandom().getrandbits(64)
    rng = random.Random(seed)
    output_dir = args.output_dir.resolve()
    ensure_directory(output_dir)
    clean_matching_files(output_dir, "*.in")

    next_request_id = 1
    for case_index in range(1, args.count + 1):
        request_count = rng.randint(min_requests, max_requests)
        pattern = resolve_case_pattern(case_index, args.time_mode, args.pickup_mode, args.dropoff_mode)
        if args.mutual:
            lower_tenths = MUTUAL_FIRST_TENTHS
            upper_tenths = MUTUAL_LAST_TENTHS
        else:
            lower_tenths = rng.randint(0, min(20, max(0, last_limit_tenths - 200)))
            upper_tenths = last_limit_tenths

        maint_count, cycle_count = choose_special_counts(request_count, args.maint_ratio, args.update_ratio)
        special_count = maint_count + 2 * cycle_count
        person_count = max(1, request_count - special_count)
        while person_count + special_count > request_count and (maint_count > 0 or cycle_count > 0):
            if cycle_count > 0:
                cycle_count -= 1
            elif maint_count > 0:
                maint_count -= 1
            special_count = maint_count + 2 * cycle_count
            person_count = max(1, request_count - special_count)

        person_timestamps = generate_person_timestamps(
            pattern.time_mode,
            rng,
            person_count,
            lower_tenths,
            upper_tenths,
        )
        floor_pairs = generate_floor_pairs(person_count, pattern.pickup_mode, pattern.dropoff_mode, rng)

        requests: list[InputRequest] = []
        for tenths, (from_floor, to_floor) in zip(person_timestamps, floor_pairs):
            requests.append(
                build_person(
                    next_request_id,
                    tenths,
                    from_floor,
                    to_floor,
                    rng.randint(50, 100),
                )
            )
            next_request_id += 1

        special_requests, next_request_id = generate_special_requests(
            rng,
            next_request_id,
            lower_tenths,
            upper_tenths,
            maint_count,
            cycle_count,
            args.mutual,
        )
        requests.extend(special_requests)
        requests = sort_requests(requests)
        if args.mutual:
            validate_mutual_case(requests)

        case_path = output_dir / f"{case_index}.in"
        no_timestamp_case_path = output_dir / f"{case_index}.no.in"
        write_case(case_path, requests)
        write_case_without_timestamp(no_timestamp_case_path, requests)
        load_case(case_path)

    print(f"generated {args.count} case(s) in {output_dir}")
    print(f"mode = {MUTUAL_MODE if args.mutual else DEFAULT_MODE}")
    if not args.mutual:
        print(f"last_request_limit = {args.last_request_limit}")
    print(f"maint_ratio = {args.maint_ratio:.2f}")
    print(f"update_ratio = {args.update_ratio:.2f}")
    print(f"time_mode = {args.time_mode}")
    print(f"pickup_mode = {args.pickup_mode}")
    print(f"dropoff_mode = {args.dropoff_mode}")
    print(f"seed = {seed}")


if __name__ == "__main__":
    main()
