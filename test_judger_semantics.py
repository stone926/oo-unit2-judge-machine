from __future__ import annotations

from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import judger
from judge_common import MaintRequest, PersonRequest, RecycleRequest, UpdateRequest


def d(value: str) -> Decimal:
    return Decimal(value)


class JudgerSemanticTests(unittest.TestCase):
    def validate(self, requests: list[object], output: str) -> None:
        with (
            patch.object(
                Path,
                "stat",
                return_value=SimpleNamespace(st_size=len(output.encode("utf-8"))),
            ),
            patch.object(Path, "open", return_value=StringIO(output)),
        ):
            judger.validate_output(
                Path("case.in"),
                Path("case.out"),
                requests=requests,
            )

    def assert_rejected(self, requests: list[object], output: str, message: str) -> None:
        with self.assertRaises(judger.JudgeFailure) as caught:
            self.validate(requests, output)
        self.assertIn(message, caught.exception.message)

    def test_unaccepted_special_request_is_rejected(self) -> None:
        requests = [MaintRequest(d("0.0"), 1, 99, "B2")]
        self.assert_rejected(requests, "", "unaccepted special requests")

    def test_receive_before_person_request_is_rejected(self) -> None:
        requests = [PersonRequest(d("10.0"), 1, 50, "F1", "F2")]
        self.assert_rejected(
            requests,
            "[0.0]RECEIVE-1-1\n",
            "precedes its input request",
        )

    def test_special_accepts_before_requests_are_rejected(self) -> None:
        with self.subTest(kind="maint"):
            self.assert_rejected(
                [MaintRequest(d("10.0"), 1, 99, "B2")],
                "[0.0]MAINT-ACCEPT-1-99-B2\n",
                "precedes its input request",
            )

        with self.subTest(kind="update"):
            self.assert_rejected(
                [UpdateRequest(d("10.0"), 1)],
                "[0.0]UPDATE-ACCEPT-1\n",
                "precedes its input request",
            )

        recycle_requests = [
            UpdateRequest(d("0.0"), 1),
            RecycleRequest(d("10.0"), 7),
        ]
        recycle_output = """[0.0]UPDATE-ACCEPT-1
[0.4]ARRIVE-F2-1
[0.8]ARRIVE-F3-1
[0.8]UPDATE-BEGIN-1
[1.8]UPDATE-END-1
[2.0]RECYCLE-ACCEPT-7
"""
        with self.subTest(kind="recycle"):
            self.assert_rejected(
                recycle_requests,
                recycle_output,
                "precedes its input request",
            )

    def test_receive_during_movement_does_not_reset_arrival_time(self) -> None:
        requests = [
            PersonRequest(d("0.0"), 1, 50, "F2", "F3"),
            PersonRequest(d("0.1"), 2, 50, "F3", "F4"),
        ]
        output = """[0.0]RECEIVE-1-1
[0.2]RECEIVE-2-1
[0.4]ARRIVE-F2-1
[0.4]OPEN-F2-1
[0.4]IN-1-F2-1
[0.8]CLOSE-F2-1
[1.2]ARRIVE-F3-1
[1.2]OPEN-F3-1
[1.2]OUT-S-1-F3-1
[1.2]IN-2-F3-1
[1.6]CLOSE-F3-1
[2.0]ARRIVE-F4-1
[2.0]OPEN-F4-1
[2.0]OUT-S-2-F4-1
[2.4]CLOSE-F4-1
"""
        self.validate(requests, output)

    def test_special_approach_without_receive_must_move_toward_target(self) -> None:
        rep_requests = [
            PersonRequest(d("0.0"), 1, 50, "F1", "F2"),
            MaintRequest(d("1.0"), 1, 99, "B2"),
        ]
        rep_output = """[0.0]RECEIVE-1-1
[0.0]OPEN-F1-1
[0.0]IN-1-F1-1
[0.4]CLOSE-F1-1
[0.8]ARRIVE-F2-1
[0.8]OPEN-F2-1
[0.8]OUT-S-1-F2-1
[1.2]CLOSE-F2-1
[1.2]MAINT-ACCEPT-1-99-B2
[1.6]ARRIVE-F3-1
"""
        with self.subTest(mode="rep-accept"):
            self.assert_rejected(
                rep_requests,
                rep_output,
                "moves away from its special-operation floor",
            )

        with self.subTest(mode="up-accept"):
            self.assert_rejected(
                [UpdateRequest(d("0.0"), 1)],
                "[0.0]UPDATE-ACCEPT-1\n[0.4]ARRIVE-B1-1\n",
                "moves away from its special-operation floor",
            )

        recycle_requests = [
            UpdateRequest(d("0.0"), 1),
            PersonRequest(d("2.0"), 1, 50, "F1", "B1"),
            RecycleRequest(d("8.0"), 7),
        ]
        recycle_output = """[0.0]UPDATE-ACCEPT-1
[0.4]ARRIVE-F2-1
[0.8]ARRIVE-F3-1
[0.8]UPDATE-BEGIN-1
[1.8]UPDATE-END-1
[2.0]RECEIVE-1-7
[2.0]OPEN-F1-7
[2.0]IN-1-F1-7
[2.4]CLOSE-F1-7
[2.8]ARRIVE-B1-7
[2.8]OPEN-B1-7
[2.8]OUT-S-1-B1-7
[3.2]CLOSE-B1-7
[8.0]RECYCLE-ACCEPT-7
[8.4]ARRIVE-B2-7
"""
        with self.subTest(mode="rec-accept"):
            self.assert_rejected(
                recycle_requests,
                recycle_output,
                "moves away from its special-operation floor",
            )

    def test_valid_maintenance_test_route_passes(self) -> None:
        requests = [MaintRequest(d("0.0"), 1, 99, "F2")]
        output = """[0.0]MAINT-ACCEPT-1-99-F2
[0.0]OPEN-F1-1
[0.0]IN-99-F1-1
[0.4]CLOSE-F1-1
[0.4]MAINT1-BEGIN-1
[1.4]MAINT2-BEGIN-1
[1.6]ARRIVE-F2-1
[1.8]ARRIVE-F1-1
[1.8]OPEN-F1-1
[1.8]OUT-S-99-F1-1
[2.2]CLOSE-F1-1
[2.2]MAINT-END-1
"""
        self.validate(requests, output)

    def test_maintenance_test_cannot_skip_or_detour(self) -> None:
        requests = [MaintRequest(d("0.0"), 1, 99, "F2")]
        prefix = """[0.0]MAINT-ACCEPT-1-99-F2
[0.0]OPEN-F1-1
[0.0]IN-99-F1-1
[0.4]CLOSE-F1-1
[0.4]MAINT1-BEGIN-1
[1.4]MAINT2-BEGIN-1
"""
        with self.subTest(kind="skip"):
            self.assert_rejected(
                requests,
                prefix + "[1.4]OPEN-F1-1\n",
                "maintenance test phase",
            )
        with self.subTest(kind="detour"):
            self.assert_rejected(
                requests,
                prefix + "[1.6]ARRIVE-B1-1\n",
                "deviates from its maintenance test route",
            )

    def test_maintenance_test_allows_exactly_one_door_cycle(self) -> None:
        requests = [MaintRequest(d("0.0"), 1, 99, "F2")]
        output = """[0.0]MAINT-ACCEPT-1-99-F2
[0.0]OPEN-F1-1
[0.0]IN-99-F1-1
[0.4]CLOSE-F1-1
[0.4]MAINT1-BEGIN-1
[1.4]MAINT2-BEGIN-1
[1.6]ARRIVE-F2-1
[1.8]ARRIVE-F1-1
[1.8]OPEN-F1-1
[1.8]OUT-S-99-F1-1
[2.2]CLOSE-F1-1
[2.2]OPEN-F1-1
"""
        self.assert_rejected(requests, output, "maintenance test phase")

    def test_maintenance_worker_cannot_repeat_or_move_after_boarding(self) -> None:
        requests = [
            PersonRequest(d("0.0"), 1, 50, "F2", "F3"),
            MaintRequest(d("0.0"), 1, 99, "B2"),
        ]
        duplicate_in = """[0.0]MAINT-ACCEPT-1-99-B2
[0.0]OPEN-F1-1
[0.0]IN-99-F1-1
[0.0]IN-99-F1-1
"""
        with self.subTest(kind="duplicate-in"):
            self.assert_rejected(requests, duplicate_in, "cannot board actors")

        movement = """[0.0]RECEIVE-1-1
[0.0]MAINT-ACCEPT-1-99-B2
[0.0]OPEN-F1-1
[0.0]IN-99-F1-1
[0.4]CLOSE-F1-1
[0.8]ARRIVE-F2-1
"""
        with self.subTest(kind="movement"):
            self.assert_rejected(requests, movement, "cannot move after")

    def test_maintenance_worker_must_board_at_f1_and_exit_once(self) -> None:
        requests = [
            PersonRequest(d("0.0"), 1, 50, "F2", "F3"),
            MaintRequest(d("0.0"), 1, 99, "F2"),
        ]
        wrong_floor = """[0.0]RECEIVE-1-1
[0.4]ARRIVE-F2-1
[0.4]MAINT-ACCEPT-1-99-F2
[0.4]OPEN-F2-1
[0.4]IN-99-F2-1
"""
        with self.subTest(kind="wrong-floor"):
            self.assert_rejected(requests, wrong_floor, "invalid maintenance worker IN")

        duplicate_out_requests = [MaintRequest(d("0.0"), 1, 99, "F2")]
        duplicate_out = """[0.0]MAINT-ACCEPT-1-99-F2
[0.0]OPEN-F1-1
[0.0]IN-99-F1-1
[0.4]CLOSE-F1-1
[0.4]MAINT1-BEGIN-1
[1.4]MAINT2-BEGIN-1
[1.6]ARRIVE-F2-1
[1.8]ARRIVE-F1-1
[1.8]OPEN-F1-1
[1.8]OUT-S-99-F1-1
[1.8]OUT-S-99-F1-1
"""
        with self.subTest(kind="duplicate-out"):
            self.assert_rejected(
                duplicate_out_requests,
                duplicate_out,
                "invalid maintenance worker OUT",
            )

    def test_recycle_end_preserves_transfer_floor_departure(self) -> None:
        requests = [
            UpdateRequest(d("0.0"), 1),
            PersonRequest(d("2.0"), 1, 50, "F2", "F3"),
            RecycleRequest(d("8.0"), 7),
        ]
        output = """[0.0]UPDATE-ACCEPT-1
[0.4]ARRIVE-F2-1
[0.8]ARRIVE-F3-1
[0.8]UPDATE-BEGIN-1
[1.8]UPDATE-END-1
[2.0]RECEIVE-1-1
[2.4]ARRIVE-F2-1
[2.4]OPEN-F2-1
[2.4]IN-1-F2-1
[2.4]OUT-F-1-F2-1
[2.8]CLOSE-F2-1
[8.0]RECYCLE-ACCEPT-7
[8.0]RECYCLE-BEGIN-7
[9.0]RECYCLE-END-7
[9.2]ARRIVE-F3-1
[9.2]RECEIVE-1-1
[9.6]ARRIVE-F2-1
[9.6]OPEN-F2-1
[9.6]IN-1-F2-1
[10.0]CLOSE-F2-1
[10.4]ARRIVE-F3-1
[10.4]OPEN-F3-1
[10.4]OUT-S-1-F3-1
[10.8]CLOSE-F3-1
"""
        self.validate(requests, output)

    def test_recycle_end_transfer_departure_is_one_shot_and_time_bounded(self) -> None:
        requests = [
            UpdateRequest(d("0.0"), 1),
            PersonRequest(d("2.0"), 1, 50, "F2", "F3"),
            RecycleRequest(d("8.0"), 7),
        ]
        prefix = """[0.0]UPDATE-ACCEPT-1
[0.4]ARRIVE-F2-1
[0.8]ARRIVE-F3-1
[0.8]UPDATE-BEGIN-1
[1.8]UPDATE-END-1
[2.0]RECEIVE-1-1
[2.4]ARRIVE-F2-1
[2.4]OPEN-F2-1
[2.4]IN-1-F2-1
[2.4]OUT-F-1-F2-1
[2.8]CLOSE-F2-1
[8.0]RECYCLE-ACCEPT-7
[8.0]RECYCLE-BEGIN-7
[9.0]RECYCLE-END-7
"""
        with self.subTest(kind="expired"):
            self.assert_rejected(
                requests,
                prefix + "[20.0]ARRIVE-F3-1\n",
                "movement permission expired",
            )
        with self.subTest(kind="wrong-direction"):
            self.assert_rejected(
                requests,
                prefix + "[9.2]ARRIVE-F1-1\n",
                "must finish its in-flight move at F3",
            )


if __name__ == "__main__":
    unittest.main()
