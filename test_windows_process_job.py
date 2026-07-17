from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import windows_process_job as windows_job
from windows_process_job import WindowsJob


@unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
class WindowsJobTests(unittest.TestCase):
    def test_gate_wrapper_exits_if_launcher_dies_before_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            wrapper_pid_path = root / "wrapper.pid"
            target_started = root / "target-started"
            wrapper_script = Path(windows_job.__file__).resolve()
            target_code = (
                "from pathlib import Path; "
                f"Path({str(target_started)!r}).write_text('started')"
            )
            launcher_code = "\n".join(
                [
                    "import ctypes, os, subprocess, sys, time",
                    "from pathlib import Path",
                    "import windows_process_job as w",
                    "sa = w.SECURITY_ATTRIBUTES(",
                    "    nLength=ctypes.sizeof(w.SECURITY_ATTRIBUTES),",
                    "    lpSecurityDescriptor=None,",
                    "    bInheritHandle=True,",
                    ")",
                    "gate = w.KERNEL32.CreateEventW(ctypes.byref(sa), True, False, None)",
                    "parent = w.KERNEL32.OpenProcess(w.SYNCHRONIZE, True, os.getpid())",
                    "if not gate or not parent: raise RuntimeError('cannot create inherited handles')",
                    "wrapper = subprocess.Popen(",
                    "    [",
                    f"        sys.executable, {str(wrapper_script)!r},",
                    "        '--gate-handle', str(int(gate)),",
                    "        '--parent-handle', str(int(parent)), '--',",
                    f"        sys.executable, '-c', {target_code!r},",
                    "    ],",
                    "    close_fds=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,",
                    ")",
                    f"Path({str(wrapper_pid_path)!r}).write_text(str(wrapper.pid), encoding='ascii')",
                    "w.KERNEL32.CloseHandle(gate)",
                    "w.KERNEL32.CloseHandle(parent)",
                    "time.sleep(30)",
                ]
            )

            launcher = subprocess.Popen(
                [sys.executable, "-c", launcher_code],
                cwd=Path(__file__).resolve().parent,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            wrapper_pid: int | None = None
            wrapper_handle = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        wrapper_pid = int(
                            wrapper_pid_path.read_text(encoding="ascii").strip()
                        )
                        break
                    except (FileNotFoundError, OSError, ValueError):
                        pass
                    if launcher.poll() is not None:
                        break
                    time.sleep(0.01)
                if wrapper_pid is None:
                    _, launcher_stderr = launcher.communicate(timeout=3)
                    self.fail(
                        "launcher failed before publishing a complete wrapper pid: "
                        f"{launcher_stderr}"
                    )
                wrapper_handle = windows_job.KERNEL32.OpenProcess(
                    windows_job.SYNCHRONIZE, False, wrapper_pid
                )
                self.assertTrue(wrapper_handle)

                launcher.kill()
                launcher.wait(timeout=3)
                wait_result = windows_job.KERNEL32.WaitForSingleObject(
                    wrapper_handle, 3_000
                )
                self.assertEqual(windows_job.WAIT_OBJECT_0, wait_result)
                self.assertFalse(target_started.exists())
            finally:
                if launcher.poll() is None:
                    launcher.kill()
                    launcher.wait(timeout=3)
                if launcher.stderr is not None:
                    launcher.stderr.close()
                if wrapper_handle is None and wrapper_pid is not None:
                    wrapper_handle = windows_job.KERNEL32.OpenProcess(
                        windows_job.SYNCHRONIZE, False, wrapper_pid
                    )
                if wrapper_handle:
                    if (
                        windows_job.KERNEL32.WaitForSingleObject(wrapper_handle, 0)
                        == windows_job.WAIT_TIMEOUT
                        and wrapper_pid is not None
                    ):
                        subprocess.run(
                            ["taskkill", "/PID", str(wrapper_pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5,
                            check=False,
                        )
                        windows_job.KERNEL32.WaitForSingleObject(wrapper_handle, 3_000)
                    windows_job.KERNEL32.CloseHandle(wrapper_handle)

    def test_job_terminates_wrapped_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            started = root / "started"
            escaped = root / "escaped"
            target_code = (
                "from pathlib import Path; import time; "
                f"Path({str(started)!r}).write_text('started'); "
                "time.sleep(1.5); "
                f"Path({str(escaped)!r}).write_text('escaped')"
            )

            job = WindowsJob()
            try:
                process = job.launch(
                    [sys.executable, "-c", target_code],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
                deadline = time.monotonic() + 2
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(started.is_file())
                self.assertTrue(job.terminate())
                process.communicate(timeout=3)
            finally:
                job.close()

            time.sleep(2.0)
            self.assertFalse(escaped.exists())


if __name__ == "__main__":
    unittest.main()
