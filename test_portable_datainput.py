from __future__ import annotations

import io
from decimal import Decimal
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import portable_datainput as feeder


class RecordingOutput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


class FakeTime:
    def __init__(self, initial: float = 10.0, first_sleep_ratio: float = 1.0) -> None:
        self.now = initial
        self.first_sleep_ratio = first_sleep_ratio
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        ratio = self.first_sleep_ratio if len(self.sleeps) == 1 else 1.0
        self.now += seconds * ratio


class PortableDatainputUnitTests(unittest.TestCase):
    def test_parse_preserves_payload_and_accepts_crlf(self) -> None:
        requests = feeder.parse_timed_lines(
            [
                "[0.0]1-WEI-50-FROM-F1-TO-F2\r\n",
                "[0.2]UPDATE-1\n",
                "[0.2]RECYCLE-7",
            ],
            source="case.in",
        )
        self.assertEqual(
            [item.timestamp for item in requests],
            [Decimal("0.0"), Decimal("0.2"), Decimal("0.2")],
        )
        self.assertEqual(
            [item.payload for item in requests],
            ["1-WEI-50-FROM-F1-TO-F2", "UPDATE-1", "RECYCLE-7"],
        )

    def test_parser_rejects_bad_or_ambiguous_input_before_feeding(self) -> None:
        invalid_cases = (
            [],
            ["\n"],
            ["[0]UPDATE-1\n"],
            ["[0.00]UPDATE-1\n"],
            ["[0.0]\n"],
            [" [0.0]UPDATE-1\n"],
            ["[0.2]UPDATE-1\n", "[0.1]RECYCLE-7\n"],
            ["[0.0]UPDATE-1\rRECYCLE-7\n"],
        )
        for raw_lines in invalid_cases:
            with self.subTest(raw_lines=raw_lines):
                with self.assertRaises(feeder.FeederFormatError):
                    feeder.parse_timed_lines(raw_lines)

    def test_parser_enforces_request_limit(self) -> None:
        lines = [f"[0.0]UPDATE-{index}\n" for index in range(feeder.MAX_REQUESTS + 1)]
        with self.assertRaisesRegex(feeder.FeederFormatError, "more than"):
            feeder.parse_timed_lines(lines)

    def test_feeding_uses_absolute_deadlines_and_flushes_every_request(self) -> None:
        requests = feeder.parse_timed_lines(
            ["[0.0]FIRST\n", "[0.2]SECOND\n", "[0.2]THIRD\n", "[0.5]LAST\n"]
        )
        fake_time = FakeTime()
        output = RecordingOutput()
        feeder.feed_requests(
            requests,
            output,
            start_time=10.0,
            monotonic=fake_time.monotonic,
            sleep=fake_time.sleep,
        )
        self.assertEqual(output.getvalue(), "FIRST\nSECOND\nTHIRD\nLAST\n")
        self.assertEqual(output.flush_count, 4)
        self.assertEqual(len(fake_time.sleeps), 2)
        self.assertAlmostEqual(fake_time.sleeps[0], 0.2)
        self.assertAlmostEqual(fake_time.sleeps[1], 0.3)
        self.assertAlmostEqual(fake_time.now, 10.5)

    def test_feeding_rechecks_deadline_after_early_sleep(self) -> None:
        requests = feeder.parse_timed_lines(["[0.1]ONLY\n"])
        fake_time = FakeTime(first_sleep_ratio=0.5)
        output = RecordingOutput()
        feeder.feed_requests(
            requests,
            output,
            start_time=10.0,
            monotonic=fake_time.monotonic,
            sleep=fake_time.sleep,
        )
        self.assertEqual(output.getvalue(), "ONLY\n")
        self.assertGreater(len(fake_time.sleeps), 1)
        self.assertAlmostEqual(fake_time.now, 10.1)

    def test_huge_timestamp_wait_is_chunked_without_float_overflow(self) -> None:
        requests = feeder.parse_timed_lines([f"[{'9' * 400}.0]ONLY\n"])
        output = RecordingOutput()

        def stop_after_first_chunk(seconds: float) -> None:
            self.assertEqual(seconds, 60.0)
            raise RuntimeError("stop test")

        with self.assertRaisesRegex(RuntimeError, "stop test"):
            feeder.feed_requests(
                requests,
                output,
                start_time=10.0,
                monotonic=lambda: 10.0,
                sleep=stop_after_first_chunk,
            )
        self.assertEqual(output.getvalue(), "")

    def test_load_rejects_invalid_utf8_and_oversized_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            invalid = root / "invalid.in"
            invalid.write_bytes(b"\xff")
            with self.assertRaisesRegex(feeder.FeederFormatError, "UTF-8"):
                feeder.load_timed_requests(invalid)

            oversized = root / "oversized.in"
            oversized.write_bytes(b"x" * (feeder.MAX_INPUT_BYTES + 1))
            with self.assertRaisesRegex(feeder.FeederFormatError, "exceeds"):
                feeder.load_timed_requests(oversized)


class PortableDatainputProcessTests(unittest.TestCase):
    def test_cli_feeds_exact_payload_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            (root / "stdin.txt").write_bytes(
                b"[0.0]1-WEI-50-FROM-F1-TO-F2\r\n"
                b"[0.0]MAINT-1-2-F3\r\n"
                b"[0.0]UPDATE-2\r\n"
                b"[0.0]RECYCLE-8\r\n"
            )
            completed = subprocess.run(
                [sys.executable, str(Path(feeder.__file__).resolve())],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        self.assertEqual(
            completed.stdout,
            b"1-WEI-50-FROM-F1-TO-F2\n"
            b"MAINT-1-2-F3\n"
            b"UPDATE-2\n"
            b"RECYCLE-8\n",
        )
        self.assertEqual(completed.stderr, b"")

    @unittest.skipUnless(
        shutil.which("java") and shutil.which("javac")
        and (Path(__file__).parent / "dependency" / "elevator3-2026.jar").is_file(),
        "requires a JDK and the official elevator3 library",
    )
    def test_output_is_accepted_by_official_elevator_input(self) -> None:
        library = Path(__file__).parent / "dependency" / "elevator3-2026.jar"
        source = """
import com.oocourse.elevator3.ElevatorInput;
import com.oocourse.elevator3.Request;
import com.oocourse.elevator3.TimableOutput;

public class PortableFeederConsumer {
    public static void main(String[] args) throws Exception {
        TimableOutput.initStartTimestamp();
        ElevatorInput input = new ElevatorInput(System.in);
        Request request;
        while ((request = input.nextRequest()) != null) {
            System.err.println("SEEN:" + request.getClass().getSimpleName());
        }
        input.close();
    }
}
""".strip()
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            source_path = root / "PortableFeederConsumer.java"
            source_path.write_text(source, encoding="utf-8")
            (root / "stdin.txt").write_text(
                "[0.0]1-WEI-50-FROM-F1-TO-F2\n"
                "[0.0]MAINT-1-2-F3\n"
                "[0.0]UPDATE-2\n"
                "[0.0]RECYCLE-8\n",
                encoding="utf-8",
            )
            compiled = subprocess.run(
                ["javac", "-encoding", "UTF-8", "-cp", str(library), str(source_path)],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr.decode(errors="replace"))

            feeder_process = subprocess.Popen(
                [sys.executable, str(Path(feeder.__file__).resolve())],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            classpath = os.pathsep.join((str(root), str(library)))
            java_process = subprocess.Popen(
                ["java", "-cp", classpath, "PortableFeederConsumer"],
                cwd=root,
                stdin=feeder_process.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert feeder_process.stdout is not None
            feeder_process.stdout.close()
            java_stdout, java_stderr = java_process.communicate(timeout=20)
            feeder_process.wait(timeout=10)
            assert feeder_process.stderr is not None
            feeder_stderr = feeder_process.stderr.read()
            feeder_process.stderr.close()

        self.assertEqual(feeder_process.returncode, 0, feeder_stderr.decode(errors="replace"))
        self.assertEqual(java_process.returncode, 0, java_stderr.decode(errors="replace"))
        observed = java_stderr.decode("utf-8", errors="replace")
        for request_type in (
            "PersonRequest",
            "MaintRequest",
            "UpdateRequest",
            "RecycleRequest",
        ):
            self.assertIn(f"SEEN:{request_type}", observed)
        self.assertNotIn("invalid", observed.lower())
        # Special ACCEPT messages are automatically emitted by ElevatorInput.
        official_output = java_stdout.decode("utf-8", errors="replace")
        self.assertIn("MAINT-ACCEPT-1-2-F3", official_output)
        self.assertIn("UPDATE-ACCEPT-2", official_output)
        self.assertIn("RECYCLE-ACCEPT-8", official_output)


if __name__ == "__main__":
    unittest.main()
