from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import judger


class JudgerPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        judger.ACTIVE_PROCESSES.clear()
        judger.ACTIVE_TEMP_DIRS.clear()

    def tearDown(self) -> None:
        judger.terminate_active_processes()
        judger.cleanup_all_temp_dirs()

    def test_select_cases_never_succeeds_with_zero_or_missing_cases(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            with self.assertRaisesRegex(RuntimeError, "no input cases"):
                judger.select_cases(root, None)
            (root / "1.in").write_text(
                "[0.0]1-WEI-50-FROM-F1-TO-F2", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "do not exist"):
                judger.select_cases(root, ["1", "2"])
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                judger.select_cases(root, ["1", "1"])

    def test_macos_defaults_to_portable_python_datainput(self) -> None:
        self.assertEqual(
            judger.DEFAULT_PORTABLE_DATAINPUT,
            judger.default_datainput_path(False),
        )
        self.assertEqual(
            [sys.executable, str(judger.DEFAULT_PORTABLE_DATAINPUT)],
            judger.datainput_launch_command(judger.DEFAULT_PORTABLE_DATAINPUT),
        )

    def test_python_datainput_does_not_require_posix_execute_bit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            project = root / "project.jar"
            library = root / "library.jar"
            feeder = root / "portable_feeder.py"
            for path in (project, library, feeder):
                path.write_bytes(b"content")

            with (
                mock.patch.object(judger, "IS_WINDOWS", False),
                mock.patch.object(judger.shutil, "which", return_value="available"),
                mock.patch.object(
                    judger.os,
                    "access",
                    side_effect=AssertionError("X_OK must not be checked for Python"),
                ),
            ):
                judger.preflight_runtime(project, library, feeder, False)

    def test_non_windows_preflight_rejects_windows_pe_datainput(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            project = root / "project.jar"
            library = root / "library.jar"
            feeder = root / "datainput"
            project.write_bytes(b"project")
            library.write_bytes(b"library")
            feeder.write_bytes(b"MZ\x90\x00")

            with (
                mock.patch.object(judger, "IS_WINDOWS", False),
                mock.patch.object(judger.shutil, "which", return_value="available"),
                self.assertRaisesRegex(RuntimeError, "Windows PE executable"),
            ):
                judger.preflight_runtime(project, library, feeder, False)

    def test_timeout_must_outlast_the_final_input(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "final input request"):
            judger.validate_timeout_after_last_input(120, Decimal("120.0"))
        judger.validate_timeout_after_last_input(121, Decimal("120.0"))

    def test_implicit_timeout_tracks_actual_last_input_with_grace(self) -> None:
        self.assertEqual(120, judger.resolve_judge_timeout(None, False, Decimal("80.0")))
        self.assertEqual(121, judger.resolve_judge_timeout(None, False, Decimal("80.1")))
        self.assertEqual(240, judger.resolve_judge_timeout(None, False, Decimal("200.0")))
        self.assertEqual(180, judger.resolve_judge_timeout(None, True, Decimal("50.0")))
        self.assertEqual(250, judger.resolve_judge_timeout(250, False, Decimal("200.0")))

    def test_poll_timing_does_not_overflow_for_huge_timeout(self) -> None:
        self.assertEqual(
            judger.COMMUNICATE_POLL_INTERVAL,
            judger.next_poll_sleep(
                100.0,
                10**400,
                101.0,
                judger.COMMUNICATE_POLL_INTERVAL,
            ),
        )

    def test_posix_cleanup_kills_group_after_leader_has_exited(self) -> None:
        process = mock.Mock(spec=subprocess.Popen)
        process.pid = 43210
        process.poll.return_value = 0
        with (
            mock.patch.object(judger, "IS_WINDOWS", False),
            mock.patch.object(judger.os, "killpg", create=True) as killpg,
            mock.patch.object(judger.signal, "SIGKILL", 9, create=True),
        ):
            self.assertTrue(judger.terminate_process(process))

        killpg.assert_called_once_with(process.pid, 9)
        process.kill.assert_not_called()

    def test_failed_build_preserves_previous_project_jar(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            source = root / "src"
            source.mkdir()
            (source / "Main.java").write_text("class Main {}", encoding="utf-8")
            library = root / "library.jar"
            library.write_bytes(b"library")
            project = root / "project.jar"
            project.write_bytes(b"known-good")

            with mock.patch.object(
                judger, "run_command", side_effect=RuntimeError("compile failed")
            ):
                with self.assertRaisesRegex(RuntimeError, "compile failed"):
                    judger.build_project_jar(project, source, library, "Main")

            self.assertEqual(b"known-good", project.read_bytes())

    def test_successful_build_is_atomic_and_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            source = root / "src"
            source.mkdir()
            java_file = source / "Main.java"
            java_file.write_text("class Main {}", encoding="utf-8")
            library = root / "library.jar"
            library.write_bytes(b"library")
            project = root / "project.jar"
            project.write_bytes(b"old")

            def fake_run(command: list[str], _cwd: Path, timeout: int = 0) -> None:
                del timeout
                if command[0] != "jar":
                    return
                if "--file" in command:
                    target = Path(command[command.index("--file") + 1])
                else:
                    target = Path(command[2])
                target.write_bytes(b"new-jar")

            with mock.patch.object(judger, "run_command", side_effect=fake_run):
                judger.build_project_jar(project, source, library, "Main")

            self.assertEqual(b"new-jar", project.read_bytes())
            self.assertTrue(
                judger.managed_build_is_current(project, source, library, "Main")
            )
            java_file.write_text("class Main { int changed; }", encoding="utf-8")
            self.assertFalse(
                judger.managed_build_is_current(project, source, library, "Main")
            )

    def test_main_returns_nonzero_when_any_case_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            case = root / "1.in"
            case.write_text("[0.0]1-WEI-50-FROM-F1-TO-F2", encoding="utf-8")
            project = root / "custom.jar"
            library = root / "library.jar"
            feeder = root / "datainput"
            for path in (project, library, feeder):
                path.write_bytes(b"placeholder")
            args = argparse.Namespace(
                input_dir=root,
                output_dir=root / "out",
                log_dir=root / "logs",
                project_jar=project,
                source_dir=root / "src",
                lib_jar=library,
                datainput=feeder,
                main_class="Main",
                timeout=1,
                cases=None,
                mutual=False,
                rebuild=False,
            )
            with (
                mock.patch.object(judger, "install_cleanup_guards"),
                mock.patch.object(judger, "JUDGER_LOCK_PATH", root / ".judger.lock"),
                mock.patch.object(judger, "parse_args", return_value=args),
                mock.patch.object(judger, "preflight_runtime"),
                mock.patch.object(judger, "run_case", return_value=("", "")),
                mock.patch.object(
                    judger,
                    "validate_output",
                    side_effect=judger.JudgeFailure("invalid output"),
                ),
                mock.patch("builtins.print"),
            ):
                exit_code = judger.main()

            self.assertEqual(1, exit_code)
            self.assertTrue((root / "logs" / "1.log").is_file())

    def test_infrastructure_failure_aborts_remaining_cases(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            for stem in ("1", "2"):
                (root / f"{stem}.in").write_text(
                    f"[0.0]{stem}-WEI-50-FROM-F1-TO-F2",
                    encoding="utf-8",
                )
            project = root / "custom.jar"
            library = root / "library.jar"
            feeder = root / "datainput"
            for path in (project, library, feeder):
                path.write_bytes(b"placeholder")
            args = argparse.Namespace(
                cases=None,
                mutual=False,
                rebuild=False,
                main_class="Main",
                timeout=10,
            )
            with (
                mock.patch.object(judger, "preflight_runtime"),
                mock.patch.object(
                    judger,
                    "run_case",
                    side_effect=judger.InfrastructureFailure("cannot stop child"),
                ) as run_case,
                mock.patch("builtins.print"),
            ):
                exit_code = judger.execute_judging(
                    args,
                    root,
                    root / "out",
                    root / "logs",
                    project,
                    root / "src",
                    library,
                    feeder,
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(1, run_case.call_count)
            self.assertIn(
                "aborted remaining cases",
                (root / "logs" / "1.log").read_text(encoding="utf-8"),
            )

    def test_execution_feeds_and_judges_the_same_byte_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            case = root / "1.in"
            case.write_text("[0.0]1-WEI-50-FROM-F1-TO-F2", encoding="utf-8")
            project = root / "custom.jar"
            library = root / "library.jar"
            feeder = root / "datainput"
            for path in (project, library, feeder):
                path.write_bytes(b"placeholder")
            args = argparse.Namespace(
                cases=None,
                mutual=False,
                rebuild=False,
                main_class="Main",
                timeout=10,
            )
            fed_content: list[str] = []
            judged_ids: list[int] = []

            def mutate_original(*_args: object) -> None:
                case.write_text("[0.0]2-WEI-50-FROM-F1-TO-F2", encoding="utf-8")

            def fake_run_case(snapshot_path: Path, *_args: object) -> tuple[str, str]:
                fed_content.append(snapshot_path.read_text(encoding="utf-8"))
                return "", ""

            def fake_validate(
                _case_path: Path,
                _out_path: Path,
                requests: list[object] | None = None,
            ) -> None:
                assert requests is not None
                judged_ids.append(requests[0].person_id)  # type: ignore[attr-defined]

            with (
                mock.patch.object(judger, "preflight_runtime", side_effect=mutate_original),
                mock.patch.object(judger, "run_case", side_effect=fake_run_case),
                mock.patch.object(judger, "validate_output", side_effect=fake_validate),
                mock.patch("builtins.print"),
            ):
                exit_code = judger.execute_judging(
                    args,
                    root,
                    root / "out",
                    root / "logs",
                    project,
                    root / "src",
                    library,
                    feeder,
                )

            self.assertEqual(0, exit_code)
            self.assertEqual(["[0.0]1-WEI-50-FROM-F1-TO-F2"], fed_content)
            self.assertEqual([1], judged_ids)


@unittest.skipUnless(
    judger.IS_WINDOWS
    and shutil.which("java") is not None
    and shutil.which("javac") is not None
    and shutil.which("jar") is not None
    and (judger.SCRIPT_DIR / "dependency" / "datainput").is_file(),
    "requires the bundled Windows feeder and a JDK",
)
class RunCaseIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        judger.terminate_active_processes()
        judger.cleanup_all_temp_dirs()

    def build_jar(self, root: Path, source_body: str) -> Path:
        source = root / "Main.java"
        source.write_text(source_body, encoding="utf-8")
        manifest = root / "MANIFEST.MF"
        manifest.write_text("Main-Class: Main\n\n", encoding="ascii")
        subprocess.run(
            ["javac", str(source)], check=True, cwd=root, capture_output=True
        )
        project = root / "program.jar"
        subprocess.run(
            ["jar", "cfm", str(project), str(manifest), "Main.class"],
            check=True,
            cwd=root,
            capture_output=True,
        )
        return project

    def test_early_java_exit_cannot_block_forever_on_feeder_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            project = self.build_jar(
                root, "public class Main { public static void main(String[] a) {} }"
            )
            case = root / "late.in"
            case.write_text("[10.0]1-WEI-50-FROM-F1-TO-F2", encoding="utf-8")
            started = time.monotonic()
            with mock.patch.object(judger, "FEEDER_WAIT_SECONDS", 0.2):
                _, stderr = judger.run_case(
                    case,
                    root / "late.out",
                    root / "late.err",
                    project,
                    judger.SCRIPT_DIR / "dependency" / "elevator3-2026.jar",
                    judger.SCRIPT_DIR / "dependency" / "datainput",
                    timeout=2,
                )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 2.0)
            self.assertIn("datainput did not exit", stderr)
            self.assertFalse(judger.ACTIVE_TEMP_DIRS)

    def test_output_flood_is_killed_and_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            project = self.build_jar(
                root,
                "public class Main { public static void main(String[] a) throws Exception { "
                "byte[] b = new byte[4096]; while (true) { System.out.write(b); } } }",
            )
            case = root / "flood.in"
            case.write_text("[0.0]1-WEI-50-FROM-F1-TO-F2", encoding="utf-8")
            out_path = root / "flood.out"
            with (
                mock.patch.object(judger, "MAX_PROCESS_OUTPUT_BYTES", 8192),
                mock.patch.object(judger, "COMMUNICATE_POLL_INTERVAL", 0.02),
            ):
                with self.assertRaisesRegex(judger.JudgeFailure, "Output Limit"):
                    judger.run_case(
                        case,
                        out_path,
                        root / "flood.err",
                        project,
                        judger.SCRIPT_DIR / "dependency" / "elevator3-2026.jar",
                        judger.SCRIPT_DIR / "dependency" / "datainput",
                        timeout=3,
                    )

            self.assertLessEqual(out_path.stat().st_size, 8192)
            self.assertFalse(judger.ACTIVE_TEMP_DIRS)


if __name__ == "__main__":
    unittest.main()
