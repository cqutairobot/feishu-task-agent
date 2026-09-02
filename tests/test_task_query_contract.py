"""Strict contract tests for natural-language task query intent."""

import json
import unittest

from app.tasks.query_contracts import (
    TaskQueryOutputError,
    TaskQueryScope,
    parse_task_query_json,
    task_query_json_schema,
)
from app.tasks.query_prompt import build_task_query_input


class TaskQueryContractTest(unittest.TestCase):
    def test_schema_is_strict_and_has_explicit_scopes(self) -> None:
        schema = task_query_json_schema()

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["scope"]["enum"],
            [scope.value for scope in TaskQueryScope],
        )
        self.assertEqual(
            set(schema["required"]),
            {"is_task_query", "scope", "target_name", "status", "confidence"},
        )

    def test_parses_self_query(self) -> None:
        intent = parse_task_query_json(
            json.dumps(
                {
                    "is_task_query": True,
                    "scope": "self",
                    "target_name": None,
                    "status": "open",
                    "confidence": 0.95,
                }
            )
        )

        self.assertTrue(intent.is_query)
        self.assertIs(intent.scope, TaskQueryScope.SELF)
        self.assertIsNone(intent.target_name)

    def test_parses_person_query_with_target_name(self) -> None:
        intent = parse_task_query_json(
            json.dumps(
                {
                    "is_task_query": True,
                    "scope": "person",
                    "target_name": " 王政 ",
                    "status": "open",
                    "confidence": 0.93,
                }
            )
        )

        self.assertIs(intent.scope, TaskQueryScope.PERSON)
        self.assertEqual(intent.target_name, "王政")

    def test_rejects_inconsistent_scope_and_target(self) -> None:
        with self.assertRaisesRegex(TaskQueryOutputError, "target_name"):
            parse_task_query_json(
                json.dumps(
                    {
                        "is_task_query": True,
                        "scope": "self",
                        "target_name": "王政",
                        "status": "open",
                        "confidence": 0.95,
                    }
                )
            )

    def test_prompt_keeps_user_text_as_data(self) -> None:
        payload = build_task_query_input(
            "还有哪些任务没完成？",
            chat_type="p2p",
            sender_name="王政",
        )

        self.assertEqual(payload["message"]["text"], "还有哪些任务没完成？")
        self.assertIn("scope=self", payload["instructions"])


if __name__ == "__main__":
    unittest.main()
