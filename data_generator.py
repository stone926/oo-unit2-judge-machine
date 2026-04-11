from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
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
DEFAULT_LAST_REQUEST_LIMIT_SECONDS = Decimal("80.0")
MUTUAL_FIRST_TENTHS = 10
MUTUAL_LAST_TENTHS = 500
MUTUAL_MAX_REQUESTS = 70
MAINT_GAP_TENTHS = 80
DEFAULT_MAINT_RATIO = 0.30
DEFAULT_FIRST_TENTHS_MIN = 20
DEFAULT_FIRST_TENTHS_MAX = 260
DEFAULT_MIN_WINDOW_TENTHS = 180
MAINT_CLUSTER_JITTER_TENTHS = 12

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
    if value < 0 or value > 0.95:
        raise argparse.ArgumentTypeError("ratio must be in [0, 0.95]")
    return value


def seconds_to_tenths(seconds: Decimal) -> int:
    return int(seconds * Decimal("10"))


def clamp_tenths(tenths: int, lower_tenths: int, upper_tenths: int) -> int:
    return max(lower_tenths, min(upper_tenths, tenths))


def choose_clustered_floor(
    rng: random.Random,
    hotspots: tuple[str, ...],
) -> str:
    if not hotspots:
        return rng.choice(ALL_FLOORS)
    if len(hotspots) == 1:
        if rng.random() < 0.85:
            return hotspots[0]
        return rng.choice(ALL_FLOORS)

    roll = rng.random()
    if roll < 0.70:
        return hotspots[0]
    if roll < 0.90:
        return hotspots[1]
    return rng.choice(ALL_FLOORS)


def build_hotspots(rng: random.Random) -> tuple[str, ...]:
    hotspot_count = 2 if rng.random() < 0.85 else 1
    return tuple(rng.sample(list(ALL_FLOORS), k=hotspot_count))


def choose_pickup_floor(
    pickup_mode: str,
    pickup_hotspots: tuple[str, ...],
    rng: random.Random,
) -> str:
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
        to_floor = choose_dropoff_floor(
            from_floor=from_floor,
            dropoff_mode=dropoff_mode,
            dropoff_hotspots=dropoff_hotspots,
            rng=rng,
        )
        floor_pairs.append((from_floor, to_floor))
    return floor_pairs


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


def generate_uniform_person_timestamps(
    rng: random.Random,
    person_count: int,
    lower_tenths: int,
    upper_tenths: int,
) -> list[int]:
    if person_count == 0:
        return []
    if lower_tenths > upper_tenths:
        raise RuntimeError("invalid timestamp window")
    if person_count == 1:
        return [rng.randint(lower_tenths, upper_tenths)]

    span = upper_tenths - lower_tenths
    if span == 0:
        return [lower_tenths for _ in range(person_count)]

    timestamps: list[int] = []
    step = span / max(1, person_count - 1)
    for offset in range(person_count):
        base = lower_tenths + round(offset * step)
        jitter_limit = max(1, int(step // 4))
        tentative = base + rng.randint(-jitter_limit, jitter_limit)
        timestamps.append(clamp_tenths(tentative, lower_tenths, upper_tenths))
    timestamps.sort()
    return timestamps


def generate_burst_person_timestamps(
    rng: random.Random,
    person_count: int,
    lower_tenths: int,
    upper_tenths: int,
) -> list[int]:
    if person_count == 0:
        return []
    if lower_tenths > upper_tenths:
        raise RuntimeError("invalid timestamp window")
    if person_count == 1:
        return [rng.randint(lower_tenths, upper_tenths)]

    span = upper_tenths - lower_tenths
    if span == 0:
        return [lower_tenths for _ in range(person_count)]

    burst_count = min(6, max(2, person_count // 10))
    segment_width = max(1, span // burst_count)

    centers: list[int] = []
    for index in range(burst_count):
        segment_start = lower_tenths + index * segment_width
        segment_end = upper_tenths if index == burst_count - 1 else min(upper_tenths, segment_start + segment_width)
        centers.append(rng.randint(segment_start, segment_end))

    burst_weights = [rng.randint(3, 10) for _ in centers]
    timestamps: list[int] = []
    for _ in range(person_count):
        center = rng.choices(centers, weights=burst_weights, k=1)[0]
        if rng.random() < 0.35:
            tentative = center
        else:
            tentative = center + rng.randint(-3, 3)
        timestamps.append(clamp_tenths(tentative, lower_tenths, upper_tenths))
    timestamps.sort()
    return timestamps


def generate_person_timestamps(
    time_mode: str,
    rng: random.Random,
    person_count: int,
    lower_tenths: int,
    upper_tenths: int,
) -> list[int]:
    if time_mode == TIME_MODE_UNIFORM:
        return generate_uniform_person_timestamps(rng, person_count, lower_tenths, upper_tenths)
    return generate_burst_person_timestamps(rng, person_count, lower_tenths, upper_tenths)


def resolve_case_pattern(
    case_index: int,
    time_mode: str,
    pickup_mode: str,
    dropoff_mode: str,
) -> CasePattern:
    time_candidates = [time_mode] if time_mode != AUTO_MODE else list(TIME_MODE_ORDER)
    pickup_candidates = [pickup_mode] if pickup_mode != AUTO_MODE else list(PICKUP_MODE_ORDER)
    dropoff_candidates = [dropoff_mode] if dropoff_mode != AUTO_MODE else list(DROPOFF_MODE_ORDER)

    combinations = [
        CasePattern(candidate_time, candidate_pickup, candidate_dropoff)
        for candidate_time in time_candidates
        for candidate_pickup in pickup_candidates
        for candidate_dropoff in dropoff_candidates
    ]
    if not combinations:
        raise RuntimeError("no available case pattern")
    return combinations[(case_index - 1) % len(combinations)]


def resolve_default_time_window(
    rng: random.Random,
    upper_tenths: int,
) -> tuple[int, int]:
    if upper_tenths <= 0:
        raise RuntimeError("default mode last request limit must be positive")

    latest_start = min(DEFAULT_FIRST_TENTHS_MAX, upper_tenths - DEFAULT_MIN_WINDOW_TENTHS)
    if latest_start >= DEFAULT_FIRST_TENTHS_MIN:
        lower_tenths = rng.randint(DEFAULT_FIRST_TENTHS_MIN, latest_start)
    else:
        lower_tenths = max(0, upper_tenths // 5)

    if lower_tenths >= upper_tenths:
        lower_tenths = max(0, upper_tenths - 1)
    return lower_tenths, upper_tenths


def build_maint_slot_template(
    lower_tenths: int,
    upper_tenths: int,
) -> list[int]:
    return list(range(lower_tenths, upper_tenths + 1, MAINT_GAP_TENTHS))


def resolve_maint_count(
    request_count: int,
    maint_ratio: float,
    mutual: bool,
    slot_count_per_elevator: int,
) -> int:
    if request_count <= 1 or slot_count_per_elevator <= 0:
        return 0
    if maint_ratio <= 0:
        return 0

    target_count = int(round(request_count * maint_ratio))
    target_count = max(1, target_count)

    max_by_passenger = request_count - 1
    if mutual:
        max_by_slots = ELEVATOR_COUNT
        density_floor = 2 if request_count >= 40 else 1
    else:
        max_by_slots = slot_count_per_elevator * ELEVATOR_COUNT
        density_floor = 3 if request_count >= 60 else 2 if request_count >= 30 else 1

    max_count = min(max_by_passenger, max_by_slots)
    if max_count <= 0:
        return 0
    min_count = min(max_count, density_floor)
    return max(min_count, min(target_count, max_count))


def build_dense_maint_elevator_plan(
    rng: random.Random,
    maint_count: int,
    mutual: bool,
    slot_count_per_elevator: int,
) -> list[int]:
    if maint_count == 0:
        return []

    elevator_ids = list(range(1, ELEVATOR_COUNT + 1))
    if mutual:
        if maint_count > ELEVATOR_COUNT:
            raise RuntimeError("mutual mode cannot assign maintenance to more than 6 elevators")
        return rng.sample(elevator_ids, k=maint_count)

    remaining_slots = {elevator_id: slot_count_per_elevator for elevator_id in elevator_ids}
    hotspot_count = 1 if maint_count < 4 else 2 if maint_count < 10 else 3
    hotspots = set(rng.sample(elevator_ids, k=min(hotspot_count, ELEVATOR_COUNT)))

    plan: list[int] = []
    for _ in range(maint_count):
        candidates = [elevator_id for elevator_id in elevator_ids if remaining_slots[elevator_id] > 0]
        if not candidates:
            raise RuntimeError("unable to allocate enough maintenance slots")
        weights = [
            (8 if elevator_id in hotspots else 3) + remaining_slots[elevator_id]
            for elevator_id in candidates
        ]
        selected = rng.choices(candidates, weights=weights, k=1)[0]
        remaining_slots[selected] -= 1
        plan.append(selected)
    return plan


def build_maint_cluster_centers(
    rng: random.Random,
    maint_count: int,
    lower_tenths: int,
    upper_tenths: int,
) -> list[int]:
    if maint_count <= 0:
        return []
    anchor = rng.randint(lower_tenths, upper_tenths)
    cluster_count = min(3, max(1, maint_count // 6 + 1))
    centers = [
        clamp_tenths(anchor + rng.randint(-120, 120), lower_tenths, upper_tenths)
        for _ in range(cluster_count)
    ]
    centers.sort()
    return centers


def choose_preferred_maint_tenths(
    rng: random.Random,
    cluster_centers: list[int],
    lower_tenths: int,
    upper_tenths: int,
) -> int:
    center = rng.choice(cluster_centers)
    return clamp_tenths(
        center + rng.randint(-MAINT_CLUSTER_JITTER_TENTHS, MAINT_CLUSTER_JITTER_TENTHS),
        lower_tenths,
        upper_tenths,
    )


def nearest_maint_slot(slots: list[int], preferred_tenths: int) -> int:
    return min(slots, key=lambda tenths: (abs(tenths - preferred_tenths), tenths))


def generate_maint_requests(
    rng: random.Random,
    maint_count: int,
    start_request_id: int,
    lower_tenths: int,
    upper_tenths: int,
    mutual: bool,
) -> list[MaintRequest]:
    if maint_count == 0:
        return []

    slot_template = build_maint_slot_template(lower_tenths, upper_tenths)
    if not slot_template:
        raise RuntimeError("maintenance time window is too narrow")
    elevator_plan = build_dense_maint_elevator_plan(
        rng=rng,
        maint_count=maint_count,
        mutual=mutual,
        slot_count_per_elevator=len(slot_template),
    )
    cluster_centers = build_maint_cluster_centers(rng, maint_count, lower_tenths, upper_tenths)

    available_slots = {
        elevator_id: list(slot_template)
        for elevator_id in range(1, ELEVATOR_COUNT + 1)
    }

    requests: list[MaintRequest] = []
    for offset, planned_elevator_id in enumerate(elevator_plan):
        preferred_tenths = choose_preferred_maint_tenths(
            rng,
            cluster_centers,
            lower_tenths,
            upper_tenths,
        )
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
    last_request_limit_tenths: int,
    maint_ratio: float,
    time_mode: str,
    pickup_mode: str,
    dropoff_mode: str,
) -> tuple[list[InputRequest], int]:
    case_pattern = resolve_case_pattern(
        case_index=case_index,
        time_mode=time_mode,
        pickup_mode=pickup_mode,
        dropoff_mode=dropoff_mode,
    )

    if mutual:
        lower_tenths = MUTUAL_FIRST_TENTHS
        upper_tenths = MUTUAL_LAST_TENTHS
    else:
        lower_tenths, upper_tenths = resolve_default_time_window(
            rng=rng,
            upper_tenths=last_request_limit_tenths,
        )

    slot_template = build_maint_slot_template(lower_tenths, upper_tenths)
    maint_count = resolve_maint_count(
        request_count=request_count,
        maint_ratio=maint_ratio,
        mutual=mutual,
        slot_count_per_elevator=len(slot_template),
    )
    person_count = max(1, request_count - maint_count)
    maint_count = request_count - person_count

    person_timestamps = generate_person_timestamps(
        time_mode=case_pattern.time_mode,
        rng=rng,
        person_count=person_count,
        lower_tenths=lower_tenths,
        upper_tenths=upper_tenths,
    )
    floor_pairs = generate_floor_pairs(
        person_count=person_count,
        pickup_mode=case_pattern.pickup_mode,
        dropoff_mode=case_pattern.dropoff_mode,
        rng=rng,
    )

    requests: list[InputRequest] = []
    next_request_id = start_request_id
    for tenths, (from_floor, to_floor) in zip(person_timestamps, floor_pairs):
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
    parser.add_argument(
        "--last-request-limit",
        type=parse_decimal_seconds,
        default=DEFAULT_LAST_REQUEST_LIMIT_SECONDS,
        help="default-mode upper bound for the last request timestamp, in seconds",
    )
    parser.add_argument(
        "--maint-ratio",
        type=parse_ratio,
        default=DEFAULT_MAINT_RATIO,
        help="target ratio of maintenance requests in each case, in [0, 0.95]",
    )
    parser.add_argument(
        "--time-mode",
        choices=[AUTO_MODE, *TIME_MODE_ORDER],
        default=AUTO_MODE,
        help="time distribution mode for person requests",
    )
    parser.add_argument(
        "--pickup-mode",
        choices=[AUTO_MODE, *PICKUP_MODE_ORDER],
        default=AUTO_MODE,
        help="pickup-floor spatial mode",
    )
    parser.add_argument(
        "--dropoff-mode",
        choices=[AUTO_MODE, *DROPOFF_MODE_ORDER],
        default=AUTO_MODE,
        help="dropoff-floor spatial mode",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    min_requests, max_requests = resolve_request_bounds(args.mutual, args.min_requests, args.max_requests)
    default_last_limit_tenths = seconds_to_tenths(args.last_request_limit)
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
    if not args.mutual and default_last_limit_tenths < 10:
        raise SystemExit("--last-request-limit must be at least 1.0 in default mode")

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
            last_request_limit_tenths=default_last_limit_tenths,
            maint_ratio=args.maint_ratio,
            time_mode=args.time_mode,
            pickup_mode=args.pickup_mode,
            dropoff_mode=args.dropoff_mode,
        )
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
    print(f"time_mode = {args.time_mode}")
    print(f"pickup_mode = {args.pickup_mode}")
    print(f"dropoff_mode = {args.dropoff_mode}")
    print(f"seed = {seed}")


if __name__ == "__main__":
    main()
