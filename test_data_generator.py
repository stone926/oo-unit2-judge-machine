from __future__ import annotations

import argparse
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from decimal import Decimal
import io
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

import data_generator as generator
from judge_common import MaintRequest
from judge_common import load_case
from judge_common import validate_hw7_special_constraints


class DataGeneratorTest(unittest.TestCase):
    def run_main(self, *arguments: str) -> tuple[str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", ["data_generator.py", *arguments]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            generator.main()
        return stdout.getvalue(), stderr.getvalue()

    def test_nonfinite_numeric_arguments_are_rejected(self) -> None:
        for raw in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(decimal=raw):
                with self.assertRaises(argparse.ArgumentTypeError):
                    generator.parse_decimal_seconds(raw)
            with self.subTest(ratio=raw):
                with self.assertRaises(argparse.ArgumentTypeError):
                    generator.parse_ratio(raw)

    def test_seed_reproduces_identical_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first"
            second = root / "second"
            common_arguments = (
                "--seed",
                "0x1234abcd",
                "--count",
                "8",
                "--min-requests",
                "15",
                "--max-requests",
                "20",
                "--stress-mode",
                "auto",
            )
            first_stdout, _ = self.run_main(
                *common_arguments,
                "--output-dir",
                str(first),
            )
            second_stdout, _ = self.run_main(
                *common_arguments,
                "--output-dir",
                str(second),
            )

            self.assertIn(f"seed = {int('1234abcd', 16)}", first_stdout)
            self.assertIn(f"seed = {int('1234abcd', 16)}", second_stdout)
            first_files = {path.name: path.read_bytes() for path in first.glob("*.in")}
            second_files = {path.name: path.read_bytes() for path in second.glob("*.in")}
            self.assertEqual(first_files, second_files)

    def test_basic_request_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(SystemExit, "cannot exceed 100"):
                self.run_main(
                    "--seed",
                    "1",
                    "--count",
                    "1",
                    "--min-requests",
                    "101",
                    "--max-requests",
                    "101",
                    "--output-dir",
                    temporary_directory,
                )

    def test_short_ratio_windows_stay_in_bounds_and_validate(self) -> None:
        configurations = (
            ("1.0", ("--maint-ratio", "0.6", "--update-ratio", "0.05")),
            ("9.0", ("--maint-ratio", "0", "--update-ratio", "0.05")),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for index, (last_limit, ratio_arguments) in enumerate(configurations, start=1):
                with self.subTest(last_limit=last_limit):
                    output_dir = root / str(index)
                    self.run_main(
                        "--seed",
                        "1",
                        "--count",
                        "1",
                        "--min-requests",
                        "50",
                        "--max-requests",
                        "50",
                        "--stress-mode",
                        "none",
                        "--last-request-limit",
                        last_limit,
                        *ratio_arguments,
                        "--output-dir",
                        str(output_dir),
                    )
                    requests = load_case(output_dir / "1.in")
                    self.assertEqual(len(requests), 50)
                    self.assertLessEqual(requests[-1].timestamp, Decimal(last_limit))
                    validate_hw7_special_constraints(requests, mutual=False)

    def test_standard_window_realizes_high_maintenance_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory) / "cases"
            _, stderr = self.run_main(
                "--seed",
                "1",
                "--count",
                "1",
                "--min-requests",
                "100",
                "--max-requests",
                "100",
                "--stress-mode",
                "none",
                "--maint-ratio",
                "0.6",
                "--update-ratio",
                "0",
                "--last-request-limit",
                "80.0",
                "--output-dir",
                str(output_dir),
            )
            requests = load_case(output_dir / "1.in")
            maintenance = [
                request for request in requests if isinstance(request, MaintRequest)
            ]
            self.assertEqual(len(requests), 100)
            self.assertEqual(len(maintenance), 60)
            self.assertNotIn("reduced ratio-mode", stderr)

            times_by_shaft: dict[int, list[Decimal]] = {}
            for request in maintenance:
                times_by_shaft.setdefault(request.elevator_id, []).append(request.timestamp)
            self.assertTrue(any(len(times) > 1 for times in times_by_shaft.values()))
            for times in times_by_shaft.values():
                for previous, current in zip(times, times[1:]):
                    self.assertGreaterEqual(current - previous, Decimal("8.0"))

    def test_short_stress_window_keeps_a_special_request(self) -> None:
        requests, _, events = generator.generate_stress_special_requests(
            generator.STRESS_MODE_SPECIAL_BURST,
            random.Random(1),
            next_request_id=1,
            lower_tenths=0,
            upper_tenths=10,
            request_count=2,
            mutual=False,
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(requests[0], MaintRequest)

    def test_failed_staging_validation_preserves_old_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory) / "cases"
            output_dir.mkdir()
            old_timestamped = output_dir / "1.in"
            old_without_timestamp = output_dir / "1.no.in"
            old_timestamped.write_text(
                "[1.0]1-WEI-50-FROM-F1-TO-F2",
                encoding="utf-8",
            )
            old_without_timestamp.write_text(
                "1-WEI-50-FROM-F1-TO-F2",
                encoding="utf-8",
            )

            with patch.object(
                generator,
                "validate_serialized_case",
                side_effect=RuntimeError("injected staging validation failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected staging"):
                    self.run_main(
                        "--seed",
                        "1",
                        "--count",
                        "1",
                        "--output-dir",
                        str(output_dir),
                    )

            self.assertEqual(
                old_timestamped.read_text(encoding="utf-8"),
                "[1.0]1-WEI-50-FROM-F1-TO-F2",
            )
            self.assertEqual(
                old_without_timestamp.read_text(encoding="utf-8"),
                "1-WEI-50-FROM-F1-TO-F2",
            )

    def test_publish_rollback_continues_after_installed_move_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output_dir = root / "cases"
            staging_dir = root / "staging"
            output_dir.mkdir()
            staging_dir.mkdir()
            (output_dir / "1.in").write_text("old-one", encoding="utf-8")
            (output_dir / "2.in").write_text("old-two", encoding="utf-8")
            (staging_dir / "1.in").write_text("new-one", encoding="utf-8")
            (staging_dir / "2.in").write_text("new-two", encoding="utf-8")

            original_replace = Path.replace

            def flaky_replace(source: Path, target: Path) -> Path:
                source_path = Path(source)
                target_path = Path(target)
                if source_path == staging_dir / "2.in" and target_path == output_dir / "2.in":
                    raise OSError("injected publish failure")
                if source_path == output_dir / "1.in" and target_path == staging_dir / "1.in":
                    raise OSError("injected installed rollback failure")
                return original_replace(source_path, target_path)

            with patch.object(Path, "replace", new=flaky_replace):
                with self.assertRaisesRegex(OSError, "injected publish failure"):
                    generator.publish_generated_cases(staging_dir, output_dir)

            self.assertEqual((output_dir / "1.in").read_text(encoding="utf-8"), "old-one")
            self.assertEqual((output_dir / "2.in").read_text(encoding="utf-8"), "old-two")
            self.assertEqual(list(root.glob(".cases.backup-*")), [])

    def test_incomplete_publish_rollback_retains_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output_dir = root / "cases"
            staging_dir = root / "staging"
            output_dir.mkdir()
            staging_dir.mkdir()
            (output_dir / "1.in").write_text("old-one", encoding="utf-8")
            (staging_dir / "1.in").write_text("new-one", encoding="utf-8")
            (staging_dir / "2.in").write_text("new-two", encoding="utf-8")

            original_replace = Path.replace

            def flaky_replace(source: Path, target: Path) -> Path:
                source_path = Path(source)
                target_path = Path(target)
                if source_path == staging_dir / "2.in":
                    raise OSError("injected publish failure")
                if source_path.parent.name.startswith(".cases.backup-"):
                    raise OSError("injected backup restore failure")
                return original_replace(source_path, target_path)

            with patch.object(Path, "replace", new=flaky_replace):
                with self.assertRaisesRegex(RuntimeError, "rollback was incomplete") as context:
                    generator.publish_generated_cases(staging_dir, output_dir)

            backup_directories = list(root.glob(".cases.backup-*"))
            self.assertEqual(len(backup_directories), 1)
            backup_dir = backup_directories[0]
            self.assertIn(str(backup_dir), str(context.exception))
            self.assertEqual((backup_dir / "1.in").read_text(encoding="utf-8"), "old-one")


if __name__ == "__main__":
    unittest.main()
