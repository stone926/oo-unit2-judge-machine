from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from judge_common import CaseFormatError, exclusive_file_lock, load_case


class LoadCaseTests(unittest.TestCase):
    def load_text(self, content: str):
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "case.in"
            path.write_text(content, encoding="utf-8")
            return load_case(path)

    def test_rejects_empty_case(self) -> None:
        with self.assertRaisesRegex(CaseFormatError, "at least one"):
            self.load_text("")

    def test_rejects_surrounding_whitespace(self) -> None:
        for content in (
            " [0.0]1-WEI-50-FROM-F1-TO-F2",
            "[0.0]1-WEI-50-FROM-F1-TO-F2 ",
        ):
            with self.subTest(content=content):
                with self.assertRaisesRegex(CaseFormatError, "invalid input line"):
                    self.load_text(content)

    def test_rejects_more_than_one_hundred_requests(self) -> None:
        lines = [
            f"[0.0]{person_id}-WEI-50-FROM-F1-TO-F2"
            for person_id in range(1, 102)
        ]
        with self.assertRaisesRegex(CaseFormatError, "more than 100"):
            self.load_text("\n".join(lines))

    def test_rejects_id_outside_positive_java_int_range(self) -> None:
        with self.assertRaisesRegex(CaseFormatError, "positive Java int"):
            self.load_text("[0.0]2147483648-WEI-50-FROM-F1-TO-F2")

    def test_rejects_cross_kind_special_requests_less_than_eight_seconds_apart(self) -> None:
        content = "\n".join(
            (
                "[1.0]MAINT-1-100-B1",
                "[8.9]UPDATE-1",
                "[16.9]RECYCLE-7",
            )
        )
        with self.assertRaisesRegex(CaseFormatError, "at least 8.0s"):
            self.load_text(content)

    def test_rejects_unmatched_update(self) -> None:
        with self.assertRaisesRegex(CaseFormatError, "without matching RECYCLE"):
            self.load_text("[1.0]UPDATE-1")

    def test_accepts_valid_update_recycle_pair(self) -> None:
        requests = self.load_text("[1.0]UPDATE-1\n[9.0]RECYCLE-7")
        self.assertEqual(2, len(requests))

    def test_exclusive_file_lock_fails_fast_for_a_second_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            lock_path = Path(raw_dir) / ".lock"
            with exclusive_file_lock(lock_path, "test resource"):
                with self.assertRaisesRegex(RuntimeError, "already in use"):
                    with exclusive_file_lock(lock_path, "test resource"):
                        self.fail("a second owner acquired an exclusive lock")


if __name__ == "__main__":
    unittest.main()
