"""Phase 6A prompt safety rules."""

import unittest

from app.lifecycle.prompt import LIFECYCLE_DETECTION_INSTRUCTIONS


class LifecyclePromptTest(unittest.TestCase):
    def test_questions_and_vague_updates_do_not_change_tasks(self) -> None:
        self.assertIn("完成了吗", LIFECYCLE_DETECTION_INSTRUCTIONS)
        self.assertIn("快完成了", LIFECYCLE_DETECTION_INSTRUCTIONS)
        self.assertIn("先别急", LIFECYCLE_DETECTION_INSTRUCTIONS)
        self.assertIn("updates 输出空数组", LIFECYCLE_DETECTION_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
