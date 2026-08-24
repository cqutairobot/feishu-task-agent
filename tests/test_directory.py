"""Tests for Feishu chat/member directory resolution and caching."""

from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock
from zoneinfo import ZoneInfo

from app.feishu.directory import (
    DirectoryMember,
    DirectoryLookupError,
    DirectoryService,
    DirectorySnapshot,
    FeishuDirectoryProvider,
)
from app.feishu.messages import normalize_message_event
from tests.test_messages import TEXT_EVENT


class FakeDirectoryProvider:
    def __init__(self, snapshot: DirectorySnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def fetch(self, chat_id: str) -> DirectorySnapshot:
        self.calls += 1
        if chat_id != self.snapshot.chat_id:
            raise AssertionError("unexpected chat id")
        return self.snapshot

    def fetch_chat_name(self, chat_id: str) -> str:
        self.calls += 1
        if chat_id != self.snapshot.chat_id:
            raise AssertionError("unexpected chat id")
        return self.snapshot.chat_name


class DirectoryServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = DirectorySnapshot(
            chat_id="oc_test",
            chat_name="实验群",
            chat_tenant_key="tenant_test",
            owner_open_id="ou_test",
            members={
                "ou_test": DirectoryMember(
                    open_id="ou_test",
                    name="张三",
                    tenant_key="tenant_test",
                )
            },
        )
        self.provider = FakeDirectoryProvider(self.snapshot)
        self.repository = Mock()
        self.clock = [100.0]
        self.service = DirectoryService(
            self.provider,
            self.repository,
            ttl_seconds=60,
            monotonic=lambda: self.clock[0],
        )
        self.message = normalize_message_event(
            TEXT_EVENT,
            received_at=datetime(
                2026, 8, 22, 18, 30, tzinfo=ZoneInfo("Asia/Shanghai")
            ),
        )

    def test_enriches_sender_and_reuses_cached_snapshot(self) -> None:
        first = self.service.enrich(self.message)
        second = self.service.enrich(self.message)

        self.assertEqual(first.sender_name, "张三")
        self.assertEqual(second.sender_name, "张三")
        self.assertEqual(self.provider.calls, 1)
        self.repository.apply_directory_snapshot.assert_called_once()

    def test_refreshes_after_cache_expires(self) -> None:
        self.service.enrich(self.message)
        self.clock[0] = 161.0

        self.service.enrich(self.message)

        self.assertEqual(self.provider.calls, 2)

    def test_lookup_failure_keeps_message_usable(self) -> None:
        provider = Mock()
        provider.fetch.side_effect = RuntimeError("temporary API failure")
        service = DirectoryService(provider, self.repository)

        with self.assertLogs("app.feishu.directory", level="WARNING"):
            enriched = service.enrich(self.message)

        self.assertIsNone(enriched.sender_name)

    def test_chat_name_refresh_is_lightweight_and_updates_database(self) -> None:
        self.snapshot = DirectorySnapshot(
            chat_id="oc_test",
            chat_name="改名后的实验群",
            chat_tenant_key="tenant_test",
            owner_open_id="ou_test",
            members=self.snapshot.members,
        )
        self.provider.snapshot = self.snapshot

        name = self.service.refresh_chat_name("oc_test")

        self.assertEqual(name, "改名后的实验群")
        self.repository.apply_directory_snapshot.assert_called_once_with(
            "oc_test",
            "改名后的实验群",
            {},
            updated_at=unittest.mock.ANY,
        )

    def test_chat_name_refresh_failure_does_not_return_stale_name(self) -> None:
        self.service.enrich(self.message)
        self.provider.fetch_chat_name = Mock(
            side_effect=RuntimeError("temporary API failure")
        )

        with self.assertLogs("app.feishu.directory", level="WARNING"):
            name = self.service.refresh_chat_name("oc_test")

        self.assertIsNone(name)


class FeishuDirectoryProviderTest(unittest.TestCase):
    def test_fetches_all_member_pages(self) -> None:
        chat_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(
                name="实验群",
                tenant_key="tenant_test",
                owner_id="ou_one",
            ),
        )
        page_one = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(
                items=[
                    SimpleNamespace(
                        member_id="ou_one",
                        name="张三",
                        tenant_key="tenant_test",
                    )
                ],
                has_more=True,
                page_token="next-page",
            ),
        )
        page_two = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(
                items=[
                    SimpleNamespace(
                        member_id="ou_two",
                        name="李四",
                        tenant_key="tenant_test",
                    )
                ],
                has_more=False,
                page_token=None,
            ),
        )
        client = MagicMock()
        client.im.v1.chat.get.return_value = chat_response
        client.im.v1.chat_members.get.side_effect = [page_one, page_two]
        provider = FeishuDirectoryProvider.__new__(FeishuDirectoryProvider)
        provider._client = client

        snapshot = provider.fetch("oc_test")

        self.assertEqual(snapshot.chat_name, "实验群")
        self.assertEqual(snapshot.chat_tenant_key, "tenant_test")
        self.assertEqual(snapshot.owner_open_id, "ou_one")
        self.assertEqual(
            {member.name for member in snapshot.members.values()},
            {"张三", "李四"},
        )
        self.assertEqual(client.im.v1.chat_members.get.call_count, 2)

    def test_rejects_owner_missing_from_member_pages(self) -> None:
        chat_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(
                name="实验群",
                tenant_key="tenant_test",
                owner_id="ou_missing",
            ),
        )
        members_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(
                items=[
                    SimpleNamespace(
                        member_id="ou_one",
                        name="张三",
                        tenant_key="tenant_test",
                    )
                ],
                has_more=False,
                page_token=None,
            ),
        )
        client = MagicMock()
        client.im.v1.chat.get.return_value = chat_response
        client.im.v1.chat_members.get.return_value = members_response
        provider = FeishuDirectoryProvider.__new__(FeishuDirectoryProvider)
        provider._client = client

        with self.assertRaisesRegex(
            DirectoryLookupError, "owner is not present"
        ):
            provider.fetch("oc_test")


if __name__ == "__main__":
    unittest.main()
