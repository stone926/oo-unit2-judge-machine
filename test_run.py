from __future__ import annotations

import contextlib
import io
from pathlib import Path
import signal
import subprocess
import unittest
from unittest import mock

import run


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        run.ACTIVE_PROCESSES.clear()

    def tearDown(self) -> None:
        run.ACTIVE_PROCESSES.clear()

    def test_sleep_seconds_rejects_negative_and_nonfinite_values(self) -> None:
        for raw_value in ("-0.1", "nan", "inf", "-inf"):
            with self.subTest(raw_value=raw_value):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        run.parse_args(["--sleep-seconds", raw_value])
                self.assertEqual(raised.exception.code, 2)

    def test_sleep_seconds_accepts_zero_and_positive_values(self) -> None:
        self.assertEqual(run.parse_args(["--sleep-seconds", "0"]).sleep_seconds, 0.0)
        self.assertEqual(run.parse_args(["--sleep-seconds", "1.5"]).sleep_seconds, 1.5)

    def test_mismatched_generator_and_judger_directories_fail_before_running(self) -> None:
        args = run.RunArgs(
            once=True,
            mutual=False,
            sleep_seconds=0.0,
            generator_args=[],
            judger_args=[],
        )
        runtime_paths = run.RuntimePaths(
            generator_output_dir=Path("generator-input"),
            judger_input_dir=Path("judger-input"),
            judger_output_dir=Path("output"),
            judger_log_dir=Path("judge"),
        )
        with (
            mock.patch.object(run, "install_cleanup_guards"),
            mock.patch.object(run, "parse_args", return_value=args),
            mock.patch.object(run, "resolve_runtime_paths", return_value=runtime_paths),
            mock.patch.object(run, "archive_logs") as archive_logs,
            mock.patch.object(run, "run_command") as run_command,
        ):
            with self.assertRaises(SystemExit) as raised:
                run.main()

        self.assertIn("must match", str(raised.exception.code))
        archive_logs.assert_not_called()
        run_command.assert_not_called()

    def test_once_propagates_generator_failure_and_skips_judger(self) -> None:
        run_command = self._run_once_with_codes([7])
        self.assertEqual(run_command.call_count, 1)

    def test_once_propagates_judger_failure(self) -> None:
        run_command = self._run_once_with_codes([0, 9])
        self.assertEqual(run_command.call_count, 2)

    def test_once_returns_normally_when_both_commands_succeed(self) -> None:
        run_command = self._run_once_with_codes([0, 0], expected_exit_code=None)
        self.assertEqual(run_command.call_count, 2)

    def test_once_keeps_judge_artifacts_in_place(self) -> None:
        args = run.RunArgs(
            once=True,
            mutual=False,
            sleep_seconds=0.0,
            generator_args=[],
            judger_args=[],
        )
        shared_input = Path("shared-input")
        runtime_paths = run.RuntimePaths(
            generator_output_dir=shared_input,
            judger_input_dir=shared_input,
            judger_output_dir=Path("output"),
            judger_log_dir=Path("judge"),
        )
        with (
            mock.patch.object(run, "install_cleanup_guards"),
            mock.patch.object(run, "parse_args", return_value=args),
            mock.patch.object(run, "resolve_runtime_paths", return_value=runtime_paths),
            mock.patch.object(run, "archive_logs") as archive_logs,
            mock.patch.object(run, "run_command", side_effect=[0, 0]),
            mock.patch.object(run, "terminate_active_processes"),
            mock.patch("builtins.print"),
        ):
            run.main()

        archive_logs.assert_not_called()

    def test_run_command_tracks_process_and_uses_a_separate_process_group(self) -> None:
        process = mock.Mock(spec=subprocess.Popen)
        process.wait.return_value = 4
        process.poll.return_value = 4
        if run.IS_WINDOWS:
            job = mock.Mock()
            job.launch.return_value = process
            job.terminate.return_value = True
            with mock.patch.object(run, "WindowsJob", return_value=job):
                return_code = run.run_command(["example-command"], "example")
            launch_kwargs = job.launch.call_args.kwargs
        else:
            with mock.patch.object(run.subprocess, "Popen", return_value=process) as popen:
                return_code = run.run_command(["example-command"], "example")
            launch_kwargs = popen.call_args.kwargs

        self.assertEqual(return_code, 4)
        self.assertNotIn(process, run.ACTIVE_PROCESSES)
        if run.IS_WINDOWS:
            job.terminate.assert_called()
            job.close.assert_called_once_with()
            self.assertEqual(
                launch_kwargs["creationflags"],
                run.WINDOWS_PROCESS_FLAGS,
            )
        else:
            self.assertTrue(launch_kwargs["start_new_session"])

    def test_run_command_terminates_process_when_wait_is_interrupted(self) -> None:
        process = mock.Mock(spec=subprocess.Popen)
        process.wait.side_effect = KeyboardInterrupt
        process.poll.return_value = None
        with mock.patch.object(
            run, "terminate_process", return_value=True
        ) as terminate_process:
            if run.IS_WINDOWS:
                job = mock.Mock()
                job.launch.return_value = process
                job.terminate.return_value = True
                launcher = mock.patch.object(run, "WindowsJob", return_value=job)
            else:
                launcher = mock.patch.object(
                    run.subprocess, "Popen", return_value=process
                )
            with launcher:
                with self.assertRaises(KeyboardInterrupt):
                    run.run_command(["example-command"], "example")

        terminate_process.assert_called_once_with(process)
        self.assertNotIn(process, run.ACTIVE_PROCESSES)

    def test_posix_cleanup_kills_group_after_leader_has_exited(self) -> None:
        process = mock.Mock(spec=subprocess.Popen)
        process.pid = 43210
        process.poll.return_value = 0
        with (
            mock.patch.object(run, "IS_WINDOWS", False),
            mock.patch.object(run.os, "killpg", create=True) as killpg,
            mock.patch.object(run.signal, "SIGKILL", 9, create=True),
        ):
            self.assertTrue(run.terminate_process(process))

        killpg.assert_called_once_with(process.pid, 9)
        process.kill.assert_not_called()

    def test_posix_cleanup_allows_child_runner_to_clean_its_children(self) -> None:
        process = mock.Mock(spec=subprocess.Popen)
        process.pid = 43210
        process.poll.side_effect = [None, 0]
        process.wait.return_value = 0
        with (
            mock.patch.object(run, "IS_WINDOWS", False),
            mock.patch.object(run.os, "killpg", create=True) as killpg,
            mock.patch.object(run.signal, "SIGTERM", 15, create=True),
            mock.patch.object(run.signal, "SIGKILL", 9, create=True),
        ):
            self.assertTrue(run.terminate_process(process))

        self.assertEqual(
            [mock.call(process.pid, 15), mock.call(process.pid, 9)],
            killpg.call_args_list,
        )
        process.wait.assert_called_once_with(timeout=3)
        process.kill.assert_not_called()

    def test_exit_signal_terminates_registered_processes(self) -> None:
        process = mock.Mock(spec=subprocess.Popen)
        run.ACTIVE_PROCESSES.add(process)
        with mock.patch.object(run, "terminate_process", return_value=True) as terminate_process:
            with self.assertRaises(SystemExit) as raised:
                run.on_exit_signal(signal.SIGTERM, None)

        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        terminate_process.assert_called_once_with(process)
        self.assertFalse(run.ACTIVE_PROCESSES)

    def test_cleanup_guard_registers_process_cleanup_not_global_directory_cleanup(self) -> None:
        original_installed = run.RUNNER_CLEANUP_GUARDS_INSTALLED
        run.RUNNER_CLEANUP_GUARDS_INSTALLED = False
        try:
            with (
                mock.patch.object(run.atexit, "register") as register,
                mock.patch.object(run.signal, "signal"),
            ):
                run.install_cleanup_guards()
        finally:
            run.RUNNER_CLEANUP_GUARDS_INSTALLED = original_installed

        register.assert_called_once_with(run.terminate_active_processes)

    def _run_once_with_codes(
        self,
        command_codes: list[int],
        expected_exit_code: int | None = -1,
    ) -> mock.Mock:
        args = run.RunArgs(
            once=True,
            mutual=False,
            sleep_seconds=0.0,
            generator_args=[],
            judger_args=[],
        )
        shared_input = Path("shared-input")
        runtime_paths = run.RuntimePaths(
            generator_output_dir=shared_input,
            judger_input_dir=shared_input,
            judger_output_dir=Path("output"),
            judger_log_dir=Path("judge"),
        )
        run_command = mock.Mock(side_effect=command_codes)
        patches = (
            mock.patch.object(run, "install_cleanup_guards"),
            mock.patch.object(run, "parse_args", return_value=args),
            mock.patch.object(run, "resolve_runtime_paths", return_value=runtime_paths),
            mock.patch.object(run, "archive_logs", return_value=None),
            mock.patch.object(run, "run_command", run_command),
            mock.patch.object(run, "terminate_active_processes"),
            mock.patch("builtins.print"),
        )
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            if expected_exit_code is None:
                run.main()
            else:
                actual_exit_code = command_codes[-1]
                with self.assertRaises(SystemExit) as raised:
                    run.main()
                self.assertEqual(raised.exception.code, actual_exit_code)
        return run_command


if __name__ == "__main__":
    unittest.main()
