"""Product rules embedded in task-detection prompts."""

import unittest

from app.agent.prompt import (
    TASK_BATCH_DETECTION_INSTRUCTIONS,
    TASK_DETECTION_INSTRUCTIONS,
)


class TaskPromptTest(unittest.TestCase):
    def test_explicit_assignment_does_not_require_owner_acknowledgement(self) -> None:
        rule = "不要求负责人回复、确认或承诺"

        self.assertIn(rule, TASK_DETECTION_INSTRUCTIONS)
        self.assertIn(rule, TASK_BATCH_DETECTION_INSTRUCTIONS)
        self.assertIn("纯确认回复", TASK_DETECTION_INSTRUCTIONS)
        self.assertIn("纯确认回复", TASK_BATCH_DETECTION_INSTRUCTIONS)

    def test_batch_prompt_distinguishes_shared_from_individual_work(self) -> None:
        self.assertIn("一项共同产出物", TASK_BATCH_DETECTION_INSTRUCTIONS)
        self.assertIn("assignment_mode 为 shared", TASK_BATCH_DETECTION_INSTRUCTIONS)
        self.assertIn("各自", TASK_BATCH_DETECTION_INSTRUCTIONS)
        self.assertIn("多个 single candidates", TASK_BATCH_DETECTION_INSTRUCTIONS)

    def test_prompt_uses_exact_mentions_for_time_bound_meeting_tasks(self) -> None:
        for instructions in (
            TASK_DETECTION_INSTRUCTIONS,
            TASK_BATCH_DETECTION_INSTRUCTIONS,
        ):
            self.assertIn("messages 中的 mentions", instructions)
            self.assertIn("本群已确认的任务姓名", instructions)
            self.assertIn("不能改用飞书显示名", instructions)
            self.assertIn("组织、主持、召开或参加", instructions)
            self.assertIn("21:00 前开智能体讨论会议", instructions)


if __name__ == "__main__":
    unittest.main()
