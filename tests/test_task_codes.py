"""Stable human-facing task code tests."""

import unittest

from app.tasks.codes import (
    TaskCodeError,
    find_task_code_mentions,
    format_task_code,
    parse_task_code,
)


class TaskCodeTest(unittest.TestCase):
    def test_first_task_has_friendly_expected_code(self) -> None:
        self.assertEqual(format_task_code(1), "T-1A")

    def test_round_trips_representative_ids(self) -> None:
        for task_id in (1, 2, 35, 36, 1_234, 999_999_999):
            with self.subTest(task_id=task_id):
                self.assertEqual(
                    parse_task_code(format_task_code(task_id)), task_id
                )

    def test_accepts_full_compact_shorthand_and_full_width_forms(self) -> None:
        for value in ("T-1A", "t1a", "1a", " Ｔ－１Ａ "):
            with self.subTest(value=value):
                self.assertEqual(parse_task_code(value), 1)

    def test_rejects_wrong_checksum_and_invalid_inputs(self) -> None:
        for value in ("T-1B", "1", "T-", "", "T-0A"):
            with self.subTest(value=value):
                with self.assertRaises(TaskCodeError):
                    parse_task_code(value)
        for task_id in (True, 0, -1, 1.5, "1"):
            with self.subTest(task_id=task_id):
                with self.assertRaises(TaskCodeError):
                    format_task_code(task_id)  # type: ignore[arg-type]

    def test_codes_are_unique_across_many_task_ids(self) -> None:
        codes = {format_task_code(task_id) for task_id in range(1, 10_001)}
        self.assertEqual(len(codes), 10_000)

    def test_finds_codes_without_confusing_task_title_fragments(self) -> None:
        self.assertEqual(
            find_task_code_mentions(
                "1a 已完成，标题仍然是 Phase 4B；重复写 T-1A"
            ),
            ("T-1A",),
        )
        self.assertEqual(
            find_task_code_mentions("请延期 T-2T，并取消 1A"),
            ("T-2T", "T-1A"),
        )

    def test_explicit_bad_mention_is_rejected(self) -> None:
        with self.assertRaisesRegex(TaskCodeError, "checksum"):
            find_task_code_mentions("请完成 T-1B")


if __name__ == "__main__":
    unittest.main()
