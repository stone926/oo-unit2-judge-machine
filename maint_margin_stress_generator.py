from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import random

from judge_common import (
    ELEVATOR_COUNT,
    InputRequest,
    MAINT_TARGET_FLOORS,
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
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "in_maint_margin_stress"

MUTUAL_FIRST_TENTHS = 10
MUTUAL_LAST_TENTHS = 500
MUTUAL_MAX_REQUESTS = 70
DEFAULT_LAST_TENTHS = 900

MAINT_MIN_GAP_TENTHS = 80

PROFILE_SYNC_WAVE = "sync-wave"
PROFILE_STAGGERED_WAVE = "staggered-wave"
PROFILE_OPPOSITE_FLOW = "opposite-flow"
PROFILE_DOOR_THRASH = "door-thrash"
PROFILE_ORDER = (
    PROFILE_SYNC_WAVE,
    PROFILE_STAGGERED_WAVE,
    PROFILE_OPPOSITE_FLOW,
    PROFILE_DOOR_THRASH,
)

UPPER_FLOORS = ("F4", "F5", "F6", "F7")
LOWER_FLOORS = ("B4", "B3", "B2", "B1")
NEAR_F1_FLOORS = ("B2", "B1", "F1", "F2", "F3")
EXTREME_FLOORS = ("B4", "B3", "F6", "F7")


@dataclass(frozen=True, slots=True)
class CasePlan:
    profile: str
    maint_base_tenths: int
    preload_count: int
    hot_count: int
    post_count: int


class RequestBuilder:
    def __init__(self, start_request_id: int) -> None:
        self.next_request_id = start_request_id
        self.requests: list[InputRequest] = []

    def alloc_request_id(self) -> int:
        request_id = self.next_request_id
        self.next_request_id += 1
        return request_id

    def add_person(self, tenths: int, from_floor: str, to_floor: str, weight: int) -> None:
        if from_floor == to_floor:
            raise RuntimeError("person from/to floor must be different")
        person_id = self.alloc_request_id()
        self.requests.append(
            PersonRequest(
                timestamp=Decimal(format_input_timestamp(tenths)),
                person_id=person_id,
                weight=weight,
                from_floor=from_floor,
                to_floor=to_floor,
            )
        )

    def add_maint(self, tenths: int, elevator_id: int, target_floor: str) -> None:
        worker_id = self.alloc_request_id()
        self.requests.append(
            MaintRequest(
                timestamp=Decimal(format_input_timestamp(tenths)),
                elevator_id=elevator_id,
                worker_id=worker_id,
                target_floor=target_floor,
            )
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate stress cases specifically for MAINT safety-margin validation. "
            "The generated workloads focus on high contention around MAINT-ACCEPT windows."
        )
    )
    parser.add_argument("--count", type=int, default=24, help="number of cases to generate")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory used to write *.in files",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="random seed; omitted means using a random 64-bit seed",
    )
    parser.add_argument(
        "--mutual",
        action="store_true",
        help="enforce mutual-test constraints (<=70 requests, <=50.0s, one maint per elevator)",
    )
    parser.add_argument(
        "--double-wave",
        action="store_true",
        help=(
            "inject a second MAINT wave on a subset of elevators; "
            "ignored in mutual mode"
        ),
    )
    return parser.parse_args()


def choose_different_floor(rng: random.Random, candidates: tuple[str, ...], avoid: str) -> str:
    if len(candidates) == 1 and candidates[0] == avoid:
        raise RuntimeError("no different floor available")
    for _ in range(8):
        selected = rng.choice(candidates)
        if selected != avoid:
            return selected
    for selected in candidates:
        if selected != avoid:
            return selected
    raise RuntimeError("failed to choose a different floor")


def choose_route(rng: random.Random, route_mode: str) -> tuple[str, str]:
    if route_mode == "away":
        from_floor = rng.choice(("F1", "B1", "F2"))
        to_floor = choose_different_floor(rng, EXTREME_FLOORS + ("F5", "B2"), from_floor)
        return from_floor, to_floor

    if route_mode == "toward":
        from_floor = rng.choice(EXTREME_FLOORS + ("F5", "B2"))
        to_floor = choose_different_floor(rng, ("F1", "B1", "F2", "F3"), from_floor)
        return from_floor, to_floor

    if route_mode == "cross":
        if rng.random() < 0.5:
            from_floor = rng.choice(UPPER_FLOORS)
            to_floor = rng.choice(LOWER_FLOORS)
        else:
            from_floor = rng.choice(LOWER_FLOORS)
            to_floor = rng.choice(UPPER_FLOORS)
        return from_floor, to_floor

    if route_mode == "door":
        from_floor = rng.choice(NEAR_F1_FLOORS)
        to_floor = choose_different_floor(rng, NEAR_F1_FLOORS, from_floor)
        return from_floor, to_floor

    # mixed
    branch = rng.random()
    if branch < 0.30:
        return choose_route(rng, "away")
    if branch < 0.55:
        return choose_route(rng, "toward")
    if branch < 0.80:
        return choose_route(rng, "cross")
    return choose_route(rng, "door")


def add_person_burst(
    builder: RequestBuilder,
    rng: random.Random,
    count: int,
    start_tenths: int,
    end_tenths: int,
    route_mode: str,
    heavy_weight: bool,
) -> None:
    start = min(start_tenths, end_tenths)
    end = max(start_tenths, end_tenths)
    for _ in range(count):
        tenths = rng.randint(start, end)
        from_floor, to_floor = choose_route(rng, route_mode)
        weight = rng.randint(90, 100) if heavy_weight else rng.randint(50, 100)
        builder.add_person(tenths, from_floor, to_floor, weight)


def resolve_offsets(profile: str) -> list[int]:
    if profile == PROFILE_SYNC_WAVE:
        return [0, 0, 1, 1, 2, 2]
    if profile == PROFILE_STAGGERED_WAVE:
        return [0, 2, 4, 6, 8, 10]
    if profile == PROFILE_OPPOSITE_FLOW:
        return [0, 1, 2, 4, 6, 8]
    if profile == PROFILE_DOOR_THRASH:
        return [0, 1, 1, 2, 2, 3]
    raise RuntimeError(f"unknown profile: {profile}")


def choose_maint_target(rng: random.Random) -> str:
    # B2/F3 has the longest TEST route and gives higher timing pressure.
    if rng.random() < 0.75:
        return rng.choice(("B2", "F3"))
    return rng.choice(MAINT_TARGET_FLOORS)


def add_maint_wave(
    builder: RequestBuilder,
    rng: random.Random,
    base_tenths: int,
    profile: str,
    elevator_ids: list[int],
) -> dict[int, int]:
    offsets = resolve_offsets(profile)
    order = elevator_ids[:]
    rng.shuffle(order)
    first_wave_by_elevator: dict[int, int] = {}
    for index, elevator_id in enumerate(order):
        tenths = base_tenths + offsets[index]
        builder.add_maint(tenths, elevator_id, choose_maint_target(rng))
        first_wave_by_elevator[elevator_id] = tenths
    return first_wave_by_elevator


def add_optional_second_wave(
    builder: RequestBuilder,
    rng: random.Random,
    first_wave_by_elevator: dict[int, int],
    max_tenths: int,
) -> None:
    chosen = list(first_wave_by_elevator.keys())
    rng.shuffle(chosen)
    chosen = chosen[:3]
    if not chosen:
        return

    second_base = min(max_tenths - 2, max(first_wave_by_elevator.values()) + 90 + rng.randint(0, 20))
    if second_base <= 0:
        return

    for index, elevator_id in enumerate(chosen):
        earliest = first_wave_by_elevator[elevator_id] + MAINT_MIN_GAP_TENTHS
        tenths = max(second_base + index * 2, earliest)
        if tenths > max_tenths:
            continue
        builder.add_maint(tenths, elevator_id, choose_maint_target(rng))


def sort_requests(requests: list[InputRequest]) -> list[InputRequest]:
    def request_id(request: InputRequest) -> int:
        if isinstance(request, PersonRequest):
            return request.person_id
        return request.worker_id

    return sorted(requests, key=lambda request: (request.timestamp, request_id(request)))


def validate_mutual_limits(requests: list[InputRequest]) -> None:
    if len(requests) > MUTUAL_MAX_REQUESTS:
        raise RuntimeError(f"mutual mode request count must be <= {MUTUAL_MAX_REQUESTS}")
    if not requests:
        raise RuntimeError("mutual mode requires at least one request")
    if requests[0].timestamp < Decimal("1.0"):
        raise RuntimeError("mutual mode requires first request timestamp >= 1.0")
    if requests[-1].timestamp > Decimal("50.0"):
        raise RuntimeError("mutual mode requires last request timestamp <= 50.0")
    maint_count_by_elevator = {elevator_id: 0 for elevator_id in range(1, ELEVATOR_COUNT + 1)}
    for request in requests:
        if isinstance(request, MaintRequest):
            maint_count_by_elevator[request.elevator_id] += 1
            if maint_count_by_elevator[request.elevator_id] > 1:
                raise RuntimeError(
                    f"mutual mode allows at most one MAINT request on elevator {request.elevator_id}"
                )


def build_case_plan(case_index: int, rng: random.Random, mutual: bool) -> CasePlan:
    profile = PROFILE_ORDER[(case_index - 1) % len(PROFILE_ORDER)]
    if mutual:
        base_low, base_high = 70, 120
        preload_count, hot_count, post_count = 14, 18, 20
    else:
        base_low, base_high = 60, 180
        preload_count, hot_count, post_count = 24, 24, 28

    if profile == PROFILE_DOOR_THRASH:
        hot_count += 6
        preload_count = max(10, preload_count - 4)
        post_count = max(12, post_count - 2)
    elif profile == PROFILE_OPPOSITE_FLOW:
        preload_count += 4

    maint_base_tenths = rng.randint(base_low, base_high)
    return CasePlan(
        profile=profile,
        maint_base_tenths=maint_base_tenths,
        preload_count=preload_count,
        hot_count=hot_count,
        post_count=post_count,
    )


def generate_case(
    case_index: int,
    rng: random.Random,
    start_request_id: int,
    mutual: bool,
    double_wave: bool,
) -> tuple[list[InputRequest], int, str]:
    max_tenths = MUTUAL_LAST_TENTHS if mutual else DEFAULT_LAST_TENTHS
    plan = build_case_plan(case_index, rng, mutual)
    builder = RequestBuilder(start_request_id=start_request_id)

    preload_start = MUTUAL_FIRST_TENTHS if mutual else 10
    preload_end = max(preload_start + 8, plan.maint_base_tenths - 20)
    hot_start = max(preload_start, plan.maint_base_tenths - 12)
    hot_end = min(max_tenths, plan.maint_base_tenths + 6)
    post_start = min(max_tenths, plan.maint_base_tenths + 4)
    post_end = min(max_tenths, plan.maint_base_tenths + (90 if mutual else 120))

    if plan.profile == PROFILE_SYNC_WAVE:
        add_person_burst(builder, rng, plan.preload_count, preload_start, preload_end, "mixed", True)
        add_person_burst(builder, rng, plan.hot_count, hot_start, hot_end, "away", True)
        add_person_burst(builder, rng, plan.post_count, post_start, post_end, "mixed", False)
    elif plan.profile == PROFILE_STAGGERED_WAVE:
        add_person_burst(builder, rng, plan.preload_count, preload_start, preload_end, "away", True)
        add_person_burst(builder, rng, plan.hot_count, hot_start, hot_end, "cross", True)
        add_person_burst(builder, rng, plan.post_count, post_start, post_end, "toward", False)
    elif plan.profile == PROFILE_OPPOSITE_FLOW:
        add_person_burst(builder, rng, plan.preload_count, preload_start, preload_end, "away", True)
        add_person_burst(builder, rng, plan.hot_count, hot_start, hot_end, "away", True)
        add_person_burst(builder, rng, plan.post_count, post_start, post_end, "toward", True)
    elif plan.profile == PROFILE_DOOR_THRASH:
        add_person_burst(builder, rng, plan.preload_count, preload_start, preload_end, "door", False)
        add_person_burst(builder, rng, plan.hot_count, hot_start, hot_end, "door", True)
        add_person_burst(builder, rng, plan.post_count, post_start, post_end, "mixed", False)
    else:
        raise RuntimeError(f"unknown profile: {plan.profile}")

    first_wave = add_maint_wave(
        builder=builder,
        rng=rng,
        base_tenths=plan.maint_base_tenths,
        profile=plan.profile,
        elevator_ids=list(range(1, ELEVATOR_COUNT + 1)),
    )
    if double_wave and not mutual:
        add_optional_second_wave(builder, rng, first_wave, max_tenths=max_tenths)

    requests = sort_requests(builder.requests)

    if mutual:
        validate_mutual_limits(requests)
    if len(requests) > 100:
        raise RuntimeError(
            "generated case exceeds 100 requests; lower --count or disable --double-wave"
        )

    return requests, builder.next_request_id, plan.profile


def main() -> None:
    args = parse_args()
    if args.count <= 0:
        raise SystemExit("--count must be positive")

    seed = random.SystemRandom().getrandbits(64) if args.seed is None else args.seed
    rng = random.Random(seed)

    output_dir = args.output_dir.resolve()
    ensure_directory(output_dir)
    clean_matching_files(output_dir, "*.in")

    next_request_id = 1
    profile_counter = {profile: 0 for profile in PROFILE_ORDER}

    for case_index in range(1, args.count + 1):
        requests, next_request_id, profile = generate_case(
            case_index=case_index,
            rng=rng,
            start_request_id=next_request_id,
            mutual=args.mutual,
            double_wave=args.double_wave,
        )

        profile_counter[profile] += 1

        case_path = output_dir / f"{case_index}.in"
        no_timestamp_case_path = output_dir / f"{case_index}.no.in"
        write_case(case_path, requests)
        write_case_without_timestamp(no_timestamp_case_path, requests)
        load_case(case_path)

    print(f"generated {args.count} MAINT-margin stress case(s) in {output_dir}")
    print(f"mutual_mode = {args.mutual}")
    print(f"double_wave = {args.double_wave and not args.mutual}")
    print(f"seed = {seed}")
    print("profile_distribution:")
    for profile in PROFILE_ORDER:
        print(f"  {profile}: {profile_counter[profile]}")


if __name__ == "__main__":
    main()
