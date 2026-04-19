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
DEFAULT_MAINT_RATIO = 0.6
DEFAULT_UPDATE_RATIO = 0.05
SPECIAL_LOWER_OFFSET_TENTHS = 20
MAINT_LATEST_GUARD_TENTHS = 60
UPDATE_LATEST_GUARD_TENTHS = 120
OVERLAP_UPDATE_LOWER_OFFSET_TENTHS = 110
OVERLAP_MAINT_TO_UPDATE_GAP_TENTHS = 90
RECYCLE_MIN_GAP_TENTHS = 80
RECYCLE_MAX_GAP_DEFAULT_TENTHS = 140
RECYCLE_MAX_GAP_MUTUAL_TENTHS = 110
RECYCLE_FALLBACK_MIN_GAP_TENTHS = 70

STRESS_MODE_NONE = "none"
STRESS_MODE_SPECIAL_BURST = "special-burst"
STRESS_MODE_SHAFT_CHAIN = "shaft-chain"
STRESS_MODE_MAINT_WAVE = "maint-wave"
STRESS_MODE_TRANSFER_FLOOD = "transfer-flood"
STRESS_MODE_ORDER = (
    STRESS_MODE_SPECIAL_BURST,
    STRESS_MODE_SHAFT_CHAIN,
    STRESS_MODE_MAINT_WAVE,
    STRESS_MODE_TRANSFER_FLOOD,
)

LOW_ZONE_FLOORS = ("B4", "B3", "B2", "B1", "F1")
UP_ZONE_FLOORS = ("F3", "F4", "F5", "F6", "F7")

PRESSURE_MIN_UPDATE_RECYCLE_GAP_TENTHS = 70
PRESSURE_BASE_UPDATE_RECYCLE_GAP_TENTHS = 90
PRESSURE_MAINT_TO_UPDATE_GAP_TENTHS = 90
PRESSURE_NEXT_CYCLE_GAP_TENTHS = 70
PRESSURE_WAVE_MAINT_GAP_TENTHS = 82

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


@dataclass(frozen=True, slots=True)
class SpecialEventSpec:
    kind: str
    tenths: int
    elevator_id: int


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


def resolve_stress_mode(case_index: int, stress_mode: str) -> str:
    if stress_mode == AUTO_MODE:
        return STRESS_MODE_ORDER[(case_index - 1) % len(STRESS_MODE_ORDER)]
    return stress_mode


def choose_different_floor(rng: random.Random, floor_choices: tuple[str, ...], current_floor: str) -> str:
    candidates = [floor for floor in floor_choices if floor != current_floor]
    return rng.choice(candidates)


def add_maint_unit(
    units: list[list[SpecialEventSpec]],
    tenths: int,
    elevator_id: int,
    lower_tenths: int,
    upper_tenths: int,
) -> bool:
    if tenths < lower_tenths or tenths > upper_tenths:
        return False
    units.append([SpecialEventSpec(kind="maint", tenths=tenths, elevator_id=elevator_id)])
    return True


def add_cycle_unit(
    units: list[list[SpecialEventSpec]],
    shaft_id: int,
    update_tenths: int,
    recycle_tenths: int,
    lower_tenths: int,
    upper_tenths: int,
) -> bool:
    if update_tenths < lower_tenths or update_tenths > upper_tenths:
        return False
    if recycle_tenths < lower_tenths or recycle_tenths > upper_tenths:
        return False
    if recycle_tenths - update_tenths < PRESSURE_MIN_UPDATE_RECYCLE_GAP_TENTHS:
        return False
    units.append(
        [
            SpecialEventSpec(kind="update", tenths=update_tenths, elevator_id=shaft_id),
            SpecialEventSpec(kind="recycle", tenths=recycle_tenths, elevator_id=shaft_id + ELEVATOR_COUNT),
        ]
    )
    return True


def build_special_burst_units(
    rng: random.Random,
    lower_tenths: int,
    upper_tenths: int,
    mutual: bool,
) -> list[list[SpecialEventSpec]]:
    units: list[list[SpecialEventSpec]] = []
    shafts = list(range(1, ELEVATOR_COUNT + 1))
    rng.shuffle(shafts)

    recycle_gap_upper = PRESSURE_BASE_UPDATE_RECYCLE_GAP_TENTHS + (6 if mutual else 24)
    window = upper_tenths - lower_tenths
    min_anchor = lower_tenths + PRESSURE_MAINT_TO_UPDATE_GAP_TENTHS + 5
    max_anchor = upper_tenths - (PRESSURE_BASE_UPDATE_RECYCLE_GAP_TENTHS + 5)
    if min_anchor > max_anchor:
        anchor = lower_tenths + window // 2
    else:
        anchor = rng.randint(min_anchor, max_anchor)

    update_times_by_shaft: dict[int, int] = {}
    for idx, shaft_id in enumerate(shafts):
        update_tenths = anchor + idx * 2 + rng.randint(0, 2)
        recycle_gap = rng.randint(PRESSURE_BASE_UPDATE_RECYCLE_GAP_TENTHS - 10, recycle_gap_upper)
        recycle_tenths = update_tenths + recycle_gap
        if add_cycle_unit(units, shaft_id, update_tenths, recycle_tenths, lower_tenths, upper_tenths):
            update_times_by_shaft[shaft_id] = update_tenths

    for shaft_id, update_tenths in update_times_by_shaft.items():
        maint_tenths = update_tenths - rng.randint(
            PRESSURE_MAINT_TO_UPDATE_GAP_TENTHS,
            PRESSURE_MAINT_TO_UPDATE_GAP_TENTHS + 15,
        )
        add_maint_unit(units, maint_tenths, shaft_id, lower_tenths, upper_tenths)
    return units


def build_shaft_chain_units(
    rng: random.Random,
    lower_tenths: int,
    upper_tenths: int,
    mutual: bool,
) -> list[list[SpecialEventSpec]]:
    units: list[list[SpecialEventSpec]] = []
    focus_shaft = rng.randint(1, ELEVATOR_COUNT)
    recycle_gap_upper = PRESSURE_BASE_UPDATE_RECYCLE_GAP_TENTHS + (8 if mutual else 20)
    cursor = lower_tenths + 20
    maint_count = 0

    for _ in range(12):
        if cursor > upper_tenths - (PRESSURE_MIN_UPDATE_RECYCLE_GAP_TENTHS + 10):
            break
        add_maint = (not mutual and rng.random() < 0.75) or (mutual and maint_count == 0)
        if add_maint:
            if add_maint_unit(units, cursor, focus_shaft, lower_tenths, upper_tenths):
                maint_count += 1
            update_tenths = cursor + rng.randint(
                PRESSURE_MAINT_TO_UPDATE_GAP_TENTHS - 8,
                PRESSURE_MAINT_TO_UPDATE_GAP_TENTHS + 6,
            )
        else:
            update_tenths = cursor + rng.randint(12, 28)

        recycle_tenths = update_tenths + rng.randint(PRESSURE_MIN_UPDATE_RECYCLE_GAP_TENTHS, recycle_gap_upper)
        if not add_cycle_unit(units, focus_shaft, update_tenths, recycle_tenths, lower_tenths, upper_tenths):
            break
        cursor = recycle_tenths + rng.randint(PRESSURE_NEXT_CYCLE_GAP_TENTHS, PRESSURE_NEXT_CYCLE_GAP_TENTHS + 16)

    has_cycle = any(event.kind == "update" for unit in units for event in unit)
    if not has_cycle:
        center = lower_tenths + (upper_tenths - lower_tenths) // 2
        add_cycle_unit(
            units,
            focus_shaft,
            center,
            center + PRESSURE_BASE_UPDATE_RECYCLE_GAP_TENTHS,
            lower_tenths,
            upper_tenths,
        )
    return units


def build_maint_wave_units(
    rng: random.Random,
    lower_tenths: int,
    upper_tenths: int,
    mutual: bool,
) -> list[list[SpecialEventSpec]]:
    units: list[list[SpecialEventSpec]] = []
    shafts = list(range(1, ELEVATOR_COUNT + 1))
    first_wave = lower_tenths + max(15, (upper_tenths - lower_tenths) // 4)
    wave_times = [first_wave]
    second_wave = first_wave + PRESSURE_WAVE_MAINT_GAP_TENTHS
    if (not mutual) and second_wave <= upper_tenths - 10:
        wave_times.append(second_wave)

    for wave_tenths in wave_times:
        wave_shafts = shafts.copy()
        rng.shuffle(wave_shafts)
        for idx, shaft_id in enumerate(wave_shafts):
            add_maint_unit(units, wave_tenths + idx % 3, shaft_id, lower_tenths, upper_tenths)

    cycle_count = 3 if mutual else 4
    cycle_shafts = rng.sample(shafts, k=cycle_count)
    recycle_gap_upper = PRESSURE_BASE_UPDATE_RECYCLE_GAP_TENTHS + (8 if mutual else 18)
    cycle_anchor = first_wave + 36
    for idx, shaft_id in enumerate(cycle_shafts):
        update_tenths = cycle_anchor + idx * 5 + rng.randint(0, 2)
        recycle_tenths = update_tenths + rng.randint(PRESSURE_MIN_UPDATE_RECYCLE_GAP_TENTHS + 5, recycle_gap_upper)
        add_cycle_unit(units, shaft_id, update_tenths, recycle_tenths, lower_tenths, upper_tenths)
    return units


def build_transfer_flood_units(
    rng: random.Random,
    lower_tenths: int,
    upper_tenths: int,
    mutual: bool,
) -> list[list[SpecialEventSpec]]:
    units: list[list[SpecialEventSpec]] = []
    shafts = list(range(1, ELEVATOR_COUNT + 1))
    cycle_count = 3 if mutual else 4
    cycle_shafts = rng.sample(shafts, k=cycle_count)
    recycle_gap_upper = PRESSURE_BASE_UPDATE_RECYCLE_GAP_TENTHS + (6 if mutual else 14)
    anchor = lower_tenths + max(25, (upper_tenths - lower_tenths) // 3)

    for idx, shaft_id in enumerate(cycle_shafts):
        update_tenths = anchor + idx * 6 + rng.randint(0, 2)
        recycle_tenths = update_tenths + rng.randint(PRESSURE_MIN_UPDATE_RECYCLE_GAP_TENTHS, recycle_gap_upper)
        add_cycle_unit(units, shaft_id, update_tenths, recycle_tenths, lower_tenths, upper_tenths)

    maint_anchor = anchor - PRESSURE_MAINT_TO_UPDATE_GAP_TENTHS + 10
    maint_shafts = [shaft_id for shaft_id in shafts if shaft_id not in cycle_shafts]
    rng.shuffle(maint_shafts)
    maint_quota = 2 if mutual else 3
    for idx, shaft_id in enumerate(maint_shafts[:maint_quota]):
        add_maint_unit(units, maint_anchor + idx * 2, shaft_id, lower_tenths, upper_tenths)
    return units


def build_stress_special_units(
    stress_mode: str,
    rng: random.Random,
    lower_tenths: int,
    upper_tenths: int,
    mutual: bool,
) -> list[list[SpecialEventSpec]]:
    if stress_mode == STRESS_MODE_SPECIAL_BURST:
        return build_special_burst_units(rng, lower_tenths, upper_tenths, mutual)
    if stress_mode == STRESS_MODE_SHAFT_CHAIN:
        return build_shaft_chain_units(rng, lower_tenths, upper_tenths, mutual)
    if stress_mode == STRESS_MODE_MAINT_WAVE:
        return build_maint_wave_units(rng, lower_tenths, upper_tenths, mutual)
    if stress_mode == STRESS_MODE_TRANSFER_FLOOD:
        return build_transfer_flood_units(rng, lower_tenths, upper_tenths, mutual)
    return []


def select_special_units(
    units: list[list[SpecialEventSpec]],
    max_special: int,
    mutual: bool,
) -> list[SpecialEventSpec]:
    selected: list[SpecialEventSpec] = []
    maint_count_by_elevator = {elevator_id: 0 for elevator_id in range(1, ELEVATOR_COUNT + 1)}

    for unit in units:
        if len(selected) + len(unit) > max_special:
            continue
        if mutual:
            conflict = False
            for event in unit:
                if event.kind == "maint" and maint_count_by_elevator[event.elevator_id] >= 1:
                    conflict = True
                    break
            if conflict:
                continue
        selected.extend(unit)
        for event in unit:
            if event.kind == "maint":
                maint_count_by_elevator[event.elevator_id] += 1
    return selected


def materialize_special_events(
    rng: random.Random,
    events: list[SpecialEventSpec],
    next_request_id: int,
) -> tuple[list[InputRequest], int]:
    requests: list[InputRequest] = []
    for event in events:
        if event.kind == "maint":
            requests.append(
                build_maint(
                    next_request_id,
                    event.tenths,
                    event.elevator_id,
                    rng.choice(MAINT_TARGET_FLOORS),
                )
            )
            next_request_id += 1
        elif event.kind == "update":
            requests.append(build_update(event.tenths, event.elevator_id))
        else:
            requests.append(build_recycle(event.tenths, event.elevator_id))
    return requests, next_request_id


def generate_stress_special_requests(
    stress_mode: str,
    rng: random.Random,
    next_request_id: int,
    lower_tenths: int,
    upper_tenths: int,
    request_count: int,
    mutual: bool,
) -> tuple[list[InputRequest], int, list[SpecialEventSpec]]:
    max_special = max(0, request_count - 1)
    units = build_stress_special_units(stress_mode, rng, lower_tenths, upper_tenths, mutual)
    selected_events = select_special_units(units, max_special, mutual)

    if (not selected_events) and max_special >= 2:
        center = lower_tenths + (upper_tenths - lower_tenths) // 2
        fallback_update = clamp_tenths(center, lower_tenths, upper_tenths)
        fallback_recycle = fallback_update + PRESSURE_BASE_UPDATE_RECYCLE_GAP_TENTHS
        if fallback_recycle <= upper_tenths:
            selected_events = [
                SpecialEventSpec(kind="update", tenths=fallback_update, elevator_id=1),
                SpecialEventSpec(kind="recycle", tenths=fallback_recycle, elevator_id=1 + ELEVATOR_COUNT),
            ]

    requests, next_request_id = materialize_special_events(rng, selected_events, next_request_id)
    return requests, next_request_id, selected_events


def generate_stress_floor_pairs(
    stress_mode: str,
    person_count: int,
    pickup_mode: str,
    dropoff_mode: str,
    rng: random.Random,
) -> list[tuple[str, str]]:
    if person_count == 0:
        return []

    floor_pairs: list[tuple[str, str]] = []
    for _ in range(person_count):
        if stress_mode == STRESS_MODE_TRANSFER_FLOOD:
            roll = rng.random()
            if roll < 0.72:
                if rng.random() < 0.5:
                    from_floor = rng.choice(LOW_ZONE_FLOORS)
                    to_floor = rng.choice(UP_ZONE_FLOORS)
                else:
                    from_floor = rng.choice(UP_ZONE_FLOORS)
                    to_floor = rng.choice(LOW_ZONE_FLOORS)
            elif roll < 0.88:
                from_floor = "F2"
                to_floor = rng.choice(LOW_ZONE_FLOORS if rng.random() < 0.5 else UP_ZONE_FLOORS)
            else:
                from_floor = rng.choice(ALL_FLOORS)
                to_floor = choose_different_floor(rng, ALL_FLOORS, from_floor)
        elif stress_mode == STRESS_MODE_MAINT_WAVE:
            roll = rng.random()
            if roll < 0.65:
                if rng.random() < 0.5:
                    from_floor = "F1"
                    to_floor = choose_different_floor(rng, ALL_FLOORS, "F1")
                else:
                    from_floor = choose_different_floor(rng, ALL_FLOORS, "F1")
                    to_floor = "F1"
            elif roll < 0.90:
                if rng.random() < 0.5:
                    from_floor = rng.choice(LOW_ZONE_FLOORS)
                    to_floor = rng.choice(UP_ZONE_FLOORS)
                else:
                    from_floor = rng.choice(UP_ZONE_FLOORS)
                    to_floor = rng.choice(LOW_ZONE_FLOORS)
            else:
                from_floor = rng.choice(ALL_FLOORS)
                to_floor = choose_different_floor(rng, ALL_FLOORS, from_floor)
        elif stress_mode == STRESS_MODE_SHAFT_CHAIN:
            roll = rng.random()
            if roll < 0.55:
                from_floor = "F3" if rng.random() < 0.5 else "F1"
                to_floor = rng.choice(LOW_ZONE_FLOORS if from_floor == "F3" else UP_ZONE_FLOORS)
            elif roll < 0.85:
                from_floor = "F2"
                to_floor = rng.choice(UP_ZONE_FLOORS if rng.random() < 0.5 else LOW_ZONE_FLOORS)
            else:
                from_floor = rng.choice(ALL_FLOORS)
                to_floor = choose_different_floor(rng, ALL_FLOORS, from_floor)
        elif stress_mode == STRESS_MODE_SPECIAL_BURST:
            from_floor = rng.choice(("F1", "F2", "F3") if rng.random() < 0.6 else ALL_FLOORS)
            to_floor = choose_different_floor(rng, ALL_FLOORS, from_floor)
        else:
            from_floor, to_floor = generate_floor_pairs(1, pickup_mode, dropoff_mode, rng)[0]
        floor_pairs.append((from_floor, to_floor))
    return floor_pairs


def generate_stress_person_timestamps(
    stress_mode: str,
    rng: random.Random,
    person_count: int,
    lower_tenths: int,
    upper_tenths: int,
    special_events: list[SpecialEventSpec],
) -> list[int]:
    if person_count == 0:
        return []

    anchors = [event.tenths for event in special_events]
    if not anchors:
        return generate_person_timestamps(
            TIME_MODE_BURST,
            rng,
            person_count,
            lower_tenths,
            upper_tenths,
        )

    values: list[int] = []
    if stress_mode == STRESS_MODE_TRANSFER_FLOOD:
        window = upper_tenths - lower_tenths
        wave_anchors = [
            clamp_tenths(lower_tenths + max(5, window // 3), lower_tenths, upper_tenths),
            clamp_tenths(lower_tenths + max(8, (window * 2) // 3), lower_tenths, upper_tenths),
        ]
        merged_anchors = anchors + wave_anchors
        for _ in range(person_count):
            center = rng.choice(merged_anchors)
            values.append(clamp_tenths(center + rng.randint(-3, 3), lower_tenths, upper_tenths))
    else:
        jitter = 2 if stress_mode == STRESS_MODE_SPECIAL_BURST else 3
        for _ in range(person_count):
            center = rng.choice(anchors)
            values.append(clamp_tenths(center + rng.randint(-jitter, jitter), lower_tenths, upper_tenths))
    values.sort()
    return values


def choose_person_weight(stress_mode: str, rng: random.Random) -> int:
    if stress_mode == STRESS_MODE_TRANSFER_FLOOD:
        if rng.random() < 0.70:
            return rng.randint(88, 100)
        return rng.randint(50, 70)
    if stress_mode in {STRESS_MODE_SPECIAL_BURST, STRESS_MODE_MAINT_WAVE} and rng.random() < 0.55:
        return rng.randint(85, 100)
    if stress_mode == STRESS_MODE_SHAFT_CHAIN and rng.random() < 0.45:
        return rng.randint(80, 100)
    return rng.randint(50, 100)


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
    # Keep maintenance and update/recycle quotas independent: each can reach the full shaft count.
    max_maint = min(ELEVATOR_COUNT, max(0, (request_count - 3) // 8))
    maint_count = min(max_maint, int(round(request_count * maint_ratio)))
    return maint_count, cycle_count


def reduce_special_counts_to_budget(maint_count: int, cycle_count: int, request_count: int) -> tuple[int, int]:
    # Reserve at least one person request; trim special requests instead of failing hard.
    allowed_special = max(0, request_count - 1)
    special_count = maint_count + 2 * cycle_count
    if special_count <= allowed_special:
        return maint_count, cycle_count

    overflow = special_count - allowed_special
    cycle_trim = min(cycle_count, (overflow + 1) // 2)
    cycle_count -= cycle_trim
    overflow = maint_count + 2 * cycle_count - allowed_special
    if overflow > 0:
        maint_count = max(0, maint_count - overflow)
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
    maint_shafts = sorted(rng.sample(shafts, k=maint_count)) if maint_count > 0 else []
    cycle_shafts = sorted(rng.sample(shafts, k=cycle_count)) if cycle_count > 0 else []
    overlap_shafts = set(maint_shafts).intersection(cycle_shafts)

    update_times_by_shaft: dict[int, int] = {}
    for shaft_id in cycle_shafts:
        update_lower = lower_tenths + SPECIAL_LOWER_OFFSET_TENTHS
        if shaft_id in overlap_shafts:
            # When a shaft has both MAINT and UPDATE, schedule UPDATE later to keep MAINT in NORMAL mode.
            update_lower = max(update_lower, lower_tenths + OVERLAP_UPDATE_LOWER_OFFSET_TENTHS)
        update_upper = max(update_lower, upper_tenths - UPDATE_LATEST_GUARD_TENTHS)
        update_tenths = rng.randint(update_lower, update_upper)
        update_times_by_shaft[shaft_id] = update_tenths

        recycle_gap = rng.randint(
            RECYCLE_MIN_GAP_TENTHS,
            RECYCLE_MAX_GAP_MUTUAL_TENTHS if mutual else RECYCLE_MAX_GAP_DEFAULT_TENTHS,
        )
        recycle_tenths = min(upper_tenths, update_tenths + recycle_gap)
        if recycle_tenths <= update_tenths + 60:
            recycle_tenths = update_tenths + RECYCLE_FALLBACK_MIN_GAP_TENTHS
        requests.append(build_update(update_tenths, shaft_id))
        requests.append(build_recycle(recycle_tenths, shaft_id + 6))

    for elevator_id in maint_shafts:
        maint_lower = lower_tenths + SPECIAL_LOWER_OFFSET_TENTHS
        if elevator_id in overlap_shafts:
            # Ensure MAINT is always before UPDATE on the same shaft.
            update_tenths = update_times_by_shaft[elevator_id]
            maint_upper = min(
                upper_tenths - MAINT_LATEST_GUARD_TENTHS,
                update_tenths - OVERLAP_MAINT_TO_UPDATE_GAP_TENTHS,
            )
        else:
            maint_upper = upper_tenths - MAINT_LATEST_GUARD_TENTHS
        tenths = rng.randint(maint_lower, max(maint_lower, maint_upper))
        requests.append(build_maint(next_request_id, tenths, elevator_id, rng.choice(MAINT_TARGET_FLOORS)))
        next_request_id += 1
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
    parser.add_argument(
        "--stress-mode",
        choices=[STRESS_MODE_NONE, AUTO_MODE, *STRESS_MODE_ORDER],
        default=AUTO_MODE,
        help="pressure profile: none keeps ratio mode; auto rotates all pressure profiles",
    )
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
    active_stress_modes: set[str] = set()
    for case_index in range(1, args.count + 1):
        request_count = rng.randint(min_requests, max_requests)
        pattern = resolve_case_pattern(case_index, args.time_mode, args.pickup_mode, args.dropoff_mode)
        stress_mode = resolve_stress_mode(case_index, args.stress_mode)
        if stress_mode != STRESS_MODE_NONE:
            active_stress_modes.add(stress_mode)
        if args.mutual:
            lower_tenths = MUTUAL_FIRST_TENTHS
            upper_tenths = MUTUAL_LAST_TENTHS
        else:
            lower_tenths = rng.randint(0, min(20, max(0, last_limit_tenths - 200)))
            upper_tenths = last_limit_tenths

        special_events: list[SpecialEventSpec] = []
        if stress_mode == STRESS_MODE_NONE:
            maint_count, cycle_count = choose_special_counts(request_count, args.maint_ratio, args.update_ratio)
            maint_count, cycle_count = reduce_special_counts_to_budget(maint_count, cycle_count, request_count)
            special_requests, next_request_id = generate_special_requests(
                rng,
                next_request_id,
                lower_tenths,
                upper_tenths,
                maint_count,
                cycle_count,
                args.mutual,
            )
        else:
            special_requests, next_request_id, special_events = generate_stress_special_requests(
                stress_mode,
                rng,
                next_request_id,
                lower_tenths,
                upper_tenths,
                request_count,
                args.mutual,
            )

        person_count = request_count - len(special_requests)
        if stress_mode == STRESS_MODE_NONE:
            person_timestamps = generate_person_timestamps(
                pattern.time_mode,
                rng,
                person_count,
                lower_tenths,
                upper_tenths,
            )
            floor_pairs = generate_floor_pairs(person_count, pattern.pickup_mode, pattern.dropoff_mode, rng)
        else:
            person_timestamps = generate_stress_person_timestamps(
                stress_mode,
                rng,
                person_count,
                lower_tenths,
                upper_tenths,
                special_events,
            )
            floor_pairs = generate_stress_floor_pairs(
                stress_mode,
                person_count,
                pattern.pickup_mode,
                pattern.dropoff_mode,
                rng,
            )

        requests: list[InputRequest] = []
        for tenths, (from_floor, to_floor) in zip(person_timestamps, floor_pairs):
            requests.append(
                build_person(
                    next_request_id,
                    tenths,
                    from_floor,
                    to_floor,
                    choose_person_weight(stress_mode, rng),
                )
            )
            next_request_id += 1

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
    print(f"stress_mode = {args.stress_mode}")
    if active_stress_modes:
        print(f"stress_profiles = {','.join(sorted(active_stress_modes))}")
    print(f"seed = {seed}")


if __name__ == "__main__":
    main()
