"""Phase 6A lifecycle output contract and grounding tests."""

from datetime import datetime
import json
import unittest
from zoneinfo import ZoneInfo

from app.agent.context import (
    ContextMessage,
    ContextParticipant,
    TaskDetectionContext,
)
from app.lifecycle.context import (
    LifecycleDetectionContext,
    LifecycleTaskReference,
)
from app.lifecycle.contracts import (
    LifecycleAction,
    LifecycleOutputError,
    lifecycle_detection_json_schema,
    parse_lifecycle_detection_json,
)


class LifecycleContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(
            2026, 8, 23, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        conversation = TaskDetectionContext(
            chat_id="oc_lab",
            trigger_message_id="om_done",
            timezone="Asia/Shanghai",
            reference_time=self.now,
            participants=(ContextParticipant("ou_wang", "王政"),),
            messages=(
                ContextMessage(
                    "om_done",
                    "ou_wang",
                    "王政",
                    "Phase 4B 验收记录已经完成了。",
                    self.now,
                ),
            ),
            focus_message_ids=("om_done",),
        )
        self.context = LifecycleDetectionContext(
            conversation=conversation,
            tasks=(
                LifecycleTaskReference(
                    task_id=1,
                    owner_open_id="ou_wang",
                    owner_name="王政",
                    title="完成 Phase 4B 自动物化验收记录",
                    description="完成 Phase 4B 验收记录",
                    deadline=datetime(
                        2026,
                        8,
                        30,
                        18,
                        0,
                        tzinfo=ZoneInfo("Asia/Shanghai"),
                    ),
                    status="todo",
                ),
            ),
            eligible_owners=(
                ContextParticipant("ou_wang", "王政"),
                ContextParticipant("ou_ha", "王哈"),
            ),
        )

    def test_accepts_grounded_completion(self) -> None:
        result = self._parse(self._completion())

        self.assertEqual(len(result.updates), 1)
        self.assertEqual(
            result.updates[0].action, LifecycleAction.COMPLETE
        )
        self.assertEqual(result.updates[0].task_id, 1)
        self.assertIsNone(result.updates[0].new_deadline)

    def test_allows_empty_read_only_result(self) -> None:
        result = self._parse({"updates": []})

        self.assertEqual(result.updates, ())

    def test_administrator_confirm_is_not_available_to_the_model(self) -> None:
        schema = lifecycle_detection_json_schema()
        actions = schema["properties"]["updates"]["items"]["properties"][
            "action"
        ]["enum"]
        self.assertNotIn("confirm", actions)

        payload = self._completion()
        payload["updates"][0]["action"] = "confirm"
        with self.assertRaisesRegex(LifecycleOutputError, "not supported"):
            self._parse(payload)

    def test_high_risk_review_actions_are_rejected_by_legacy_contract(self) -> None:
        schema = lifecycle_detection_json_schema()
        actions = schema["properties"]["updates"]["items"]["properties"][
            "action"
        ]["enum"]
        for action in ("accept", "reopen"):
            with self.subTest(action=action):
                self.assertNotIn(action, actions)
                payload = self._completion()
                payload["updates"][0]["action"] = action
                with self.assertRaisesRegex(
                    LifecycleOutputError, "not supported"
                ):
                    self._parse(payload)

    def test_rejects_task_from_another_chat(self) -> None:
        payload = self._completion()
        payload["updates"][0]["task_id"] = 99

        with self.assertRaisesRegex(
            LifecycleOutputError, "not an open task in this chat"
        ):
            self._parse(payload)

    def test_rejects_evidence_outside_context_or_focus(self) -> None:
        payload = self._completion()
        payload["updates"][0]["evidence_message_ids"] = ["om_other"]
        with self.assertRaisesRegex(LifecycleOutputError, "outside"):
            self._parse(payload)

        second_message = ContextMessage(
            "om_old",
            "ou_wang",
            "王政",
            "旧消息",
            self.now,
        )
        context = LifecycleDetectionContext(
            conversation=TaskDetectionContext(
                chat_id=self.context.chat_id,
                trigger_message_id="om_done",
                timezone="Asia/Shanghai",
                reference_time=self.now,
                participants=self.context.conversation.participants,
                messages=(
                    second_message,
                    self.context.conversation.messages[0],
                ),
                focus_message_ids=("om_done",),
            ),
            tasks=self.context.tasks,
        )
        payload["updates"][0]["evidence_message_ids"] = ["om_old"]
        with self.assertRaisesRegex(LifecycleOutputError, "focus"):
            parse_lifecycle_detection_json(
                json.dumps(payload, ensure_ascii=False), context
            )

    def test_reschedule_requires_changed_future_deadline(self) -> None:
        payload = self._completion()
        update = payload["updates"][0]
        update["action"] = "reschedule"
        update["new_deadline"] = None
        with self.assertRaisesRegex(LifecycleOutputError, "non-null"):
            self._parse(payload)

        update["new_deadline"] = "2026-08-23T10:00:00+08:00"
        with self.assertRaisesRegex(LifecycleOutputError, "reference_time"):
            self._parse(payload)

        update["new_deadline"] = "2026-09-02T18:00:00+08:00"
        result = self._parse(payload)
        self.assertEqual(
            result.updates[0].action, LifecycleAction.RESCHEDULE
        )

    def test_complete_and_cancel_require_null_deadline(self) -> None:
        for action in ("complete", "cancel"):
            payload = self._completion()
            payload["updates"][0]["action"] = action
            payload["updates"][0]["new_deadline"] = (
                "2026-09-02T18:00:00+08:00"
            )
            with self.assertRaisesRegex(LifecycleOutputError, "null"):
                self._parse(payload)

    def test_accepts_grounded_title_and_assignee_corrections(self) -> None:
        rename = self._completion()
        rename["updates"][0].update(
            action="rename",
            new_title="提交联合回归报告",
        )
        rename_result = self._parse(rename)
        self.assertEqual(rename_result.updates[0].action, LifecycleAction.RENAME)
        self.assertEqual(
            rename_result.updates[0].new_title,
            "提交联合回归报告",
        )

        reassign = self._completion()
        reassign["updates"][0].update(
            action="reassign",
            new_owners=[{"name": "王哈", "open_id": "ou_ha"}],
        )
        reassign_result = self._parse(reassign)
        self.assertEqual(
            reassign_result.updates[0].action,
            LifecycleAction.REASSIGN,
        )
        self.assertEqual(reassign_result.updates[0].new_owners[0].name, "王哈")

    def test_reassignment_rejects_ungrounded_or_unchanged_members(self) -> None:
        payload = self._completion()
        payload["updates"][0].update(
            action="reassign",
            new_owners=[{"name": "李四", "open_id": "ou_unknown"}],
        )
        with self.assertRaisesRegex(LifecycleOutputError, "not grounded"):
            self._parse(payload)

        payload["updates"][0]["new_owners"] = [
            {"name": "王政", "open_id": "ou_wang"}
        ]
        with self.assertRaisesRegex(LifecycleOutputError, "must change"):
            self._parse(payload)

    def test_correction_payloads_are_action_specific(self) -> None:
        payload = self._completion()
        payload["updates"][0]["new_title"] = "不应出现"
        with self.assertRaisesRegex(LifecycleOutputError, "requires new_title"):
            self._parse(payload)

        payload = self._completion()
        payload["updates"][0].update(action="invalidate")
        result = self._parse(payload)
        self.assertEqual(result.updates[0].action, LifecycleAction.INVALIDATE)

    def test_rejects_two_updates_for_same_task(self) -> None:
        payload = self._completion()
        payload["updates"].append(dict(payload["updates"][0]))

        with self.assertRaisesRegex(LifecycleOutputError, "duplicate"):
            self._parse(payload)

    def test_rejects_extra_fields_and_non_finite_confidence(self) -> None:
        payload = self._completion()
        payload["updates"][0]["reasoning"] = "hidden"
        with self.assertRaisesRegex(LifecycleOutputError, "extra"):
            self._parse(payload)

        raw = (
            '{"updates":[{"action":"complete","confidence":NaN,'
            '"task_id":1,"new_deadline":null,'
            '"new_title":null,"new_owners":[],'
            '"evidence_message_ids":["om_done"]}]}'
        )
        with self.assertRaisesRegex(LifecycleOutputError, "non-finite"):
            parse_lifecycle_detection_json(raw, self.context)

    def _parse(self, payload: dict):
        return parse_lifecycle_detection_json(
            json.dumps(payload, ensure_ascii=False), self.context
        )

    @staticmethod
    def _completion() -> dict:
        return {
            "updates": [
                {
                    "action": "complete",
                    "confidence": 0.97,
                    "task_id": 1,
                    "new_deadline": None,
                    "new_title": None,
                    "new_owners": [],
                    "evidence_message_ids": ["om_done"],
                }
            ]
        }


if __name__ == "__main__":
    unittest.main()
