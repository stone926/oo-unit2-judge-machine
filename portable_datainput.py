#!/usr/bin/env python3
"""Cross-platform replacement for the bundled ``datainput`` executable.

The feeder reads ``stdin.txt`` from its working directory.  Each physical line
must have the form ``[seconds.tenths]payload``.  At the corresponding absolute
offset from feeder startup it writes ``payload`` followed by a newline to
standard output and flushes it immediately.

The module deliberately has no project imports so that it can also be copied
or invoked from an isolated per-case working directory.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable, Iterable, Sequence, TextIO


DEFAULT_INPUT_FILE = Path("stdin.txt")
MAX_INPUT_BYTES = 1024 * 1024
MAX_REQUESTS = 100
MAX_SLEEP_CHUNK = Decimal("60")
DEADLINE_EPSILON = Decimal("0.000000001")
_TIMED_LINE_RE = re.compile(r"^\[(\d+\.\d)\](.+)$")


class FeederFormatError(ValueError):
    """Raised when ``stdin.txt`` does not follow the timed-input protocol."""


@dataclass(frozen=True, slots=True)
class TimedRequest:
    """One validated line of timed input."""

    timestamp: Decimal
    payload: str
    line_number: int


def _remove_line_ending(raw_line: str) -> str:
    if raw_line.endswith("\n"):
        raw_line = raw_line[:-1]
        if raw_line.endswith("\r"):
            raw_line = raw_line[:-1]
    if "\n" in raw_line or "\r" in raw_line:
        raise FeederFormatError("embedded line ending is not allowed")
    return raw_line


def parse_timed_lines(
    raw_lines: Iterable[str],
    *,
    source: str = "stdin.txt",
) -> list[TimedRequest]:
    """Parse and validate timed feeder lines without emitting partial input."""

    requests: list[TimedRequest] = []
    previous_timestamp: Decimal | None = None
    for line_number, raw_line in enumerate(raw_lines, start=1):
        line = _remove_line_ending(raw_line)
        if not line:
            raise FeederFormatError(f"{source}:{line_number}: blank lines are not allowed")

        match = _TIMED_LINE_RE.fullmatch(line)
        if match is None:
            raise FeederFormatError(
                f"{source}:{line_number}: expected [seconds.tenths]payload"
            )

        timestamp = Decimal(match.group(1))
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise FeederFormatError(
                f"{source}:{line_number}: timestamps must be nondecreasing"
            )
        requests.append(
            TimedRequest(
                timestamp=timestamp,
                payload=match.group(2),
                line_number=line_number,
            )
        )
        previous_timestamp = timestamp
        if len(requests) > MAX_REQUESTS:
            raise FeederFormatError(
                f"{source}: contains more than {MAX_REQUESTS} requests"
            )

    if not requests:
        raise FeederFormatError(f"{source}: input must contain at least one request")
    return requests


def load_timed_requests(path: Path = DEFAULT_INPUT_FILE) -> list[TimedRequest]:
    """Read one bounded UTF-8 input file and validate all lines before output."""

    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise FeederFormatError(f"cannot read {path}: {exc}") from exc
    if len(content) > MAX_INPUT_BYTES:
        raise FeederFormatError(
            f"{path}: input exceeds the {MAX_INPUT_BYTES}-byte limit"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FeederFormatError(f"{path}: input is not valid UTF-8: {exc}") from exc
    return parse_timed_lines(text.splitlines(keepends=True), source=str(path))


def feed_requests(
    requests: Iterable[TimedRequest],
    output: TextIO,
    *,
    start_time: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Emit requests at absolute deadlines, tolerating interrupted early sleeps."""

    for request in requests:
        while True:
            elapsed = max(0.0, monotonic() - start_time)
            remaining = request.timestamp - Decimal(str(elapsed))
            if remaining <= DEADLINE_EPSILON:
                break
            # Avoid float overflow for syntactically valid, very large Decimal
            # timestamps and remain responsive to termination signals.
            sleep(float(min(remaining, MAX_SLEEP_CHUNK)))
        output.write(request.payload)
        output.write("\n")
        output.flush()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="feed [timestamp]request lines from stdin.txt in real time"
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_INPUT_FILE,
        help="timed input file (default: ./stdin.txt)",
    )
    return parser


def _silence_stdout_after_broken_pipe() -> None:
    """Prevent CPython's shutdown flush from turning a handled EPIPE into rc=120."""

    try:
        stdout_fd = sys.stdout.fileno()
        null_fd = os.open(os.devnull, os.O_WRONLY)
    except (AttributeError, OSError, ValueError):
        return
    try:
        os.dup2(null_fd, stdout_fd)
    finally:
        os.close(null_fd)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    # The consumer is line-oriented, but fixing LF also makes captured feeder
    # bytes identical on macOS, Linux, and Windows-based protocol tests.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", newline="\n")
    # Count file parsing and validation against the same origin as the official
    # feeder.  For valid, bounded hw7 cases this overhead is only a few ms.
    started_at = time.monotonic()
    try:
        requests = load_timed_requests(args.file)
        feed_requests(requests, sys.stdout, start_time=started_at)
    except FeederFormatError as exc:
        print(f"portable_datainput: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        _silence_stdout_after_broken_pipe()
        print("portable_datainput: output pipe was closed before EOF", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
