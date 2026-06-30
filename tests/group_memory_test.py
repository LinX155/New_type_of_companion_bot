import tempfile
import unittest
from datetime import datetime

from app.core.group_memory import GroupMemoryManager


class GroupMemoryManagerTest(unittest.TestCase):
    def test_mem_self_identity_updates_confirmed_qid_nickname_and_personal_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)

            success, response, payload = manager.apply_mem_command(
                "/mem 我是小夏",
                session_id="qq_group_123456",
                sender_qid="550808201",
                timestamp=datetime(2026, 6, 30, 23, 30),
            )

            self.assertTrue(success)
            self.assertEqual(response, "已把你的群聊昵称记住了。")
            self.assertEqual(payload["status"], "identity_updated")
            self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {"550808201": "小夏"})
            content = manager.read("qq_group_123456")
            self.assertIn("- 550808201: 小夏", content)
            self.assertIn("<550808201><2026-06-30T23:30:00><群聊/mem本人><昵称=小夏>", content)

    def test_mem_rejects_other_person_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)

            success, response, payload = manager.apply_mem_command(
                "/mem 1057552839 是小明",
                session_id="qq_group_123456",
                sender_qid="550808201",
            )

            self.assertFalse(success)
            self.assertEqual(payload["reason"], "other_person")
            self.assertIn("不能替别人写记忆", response)
            self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {})

    def test_mem_common_memory_uses_common_section_not_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)

            success, response, payload = manager.apply_mem_command(
                "/mem 群里最近都在玩赛博猫猫梗",
                session_id="qq_group_123456",
                sender_qid="550808201",
                timestamp=datetime(2026, 6, 30, 23, 31),
            )

            self.assertTrue(success)
            self.assertEqual(response, "已写入群聊共同记忆。")
            self.assertEqual(payload["status"], "common_memory_added")
            context = manager.prompt_context("qq_group_123456", qids=["550808201"])
            self.assertEqual(context["qid_to_nickname"], {})
            self.assertIn("群里最近都在玩赛博猫猫梗", "\n".join(context["common_memory"]))

    def test_forget_only_removes_sender_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)
            manager.apply_mem_command(
                "/mem 我叫小夏",
                session_id="qq_group_123456",
                sender_qid="550808201",
                timestamp=datetime(2026, 6, 30, 23, 30),
            )
            manager.apply_mem_command(
                "/mem 我叫小明",
                session_id="qq_group_123456",
                sender_qid="1057552839",
                timestamp=datetime(2026, 6, 30, 23, 31),
            )

            success, response, payload = manager.apply_forget_command(
                "/forget 我的昵称",
                session_id="qq_group_123456",
                sender_qid="550808201",
            )

            self.assertTrue(success)
            self.assertEqual(payload["status"], "identity_removed")
            self.assertIn("群聊身份记忆", response)
            self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {"1057552839": "小明"})

    def test_prompt_context_filters_personal_memory_to_window_qids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)
            manager.apply_mem_command("/mem 我叫小夏", session_id="qq_group_123456", sender_qid="550808201")
            manager.apply_mem_command("/mem 我叫小明", session_id="qq_group_123456", sender_qid="1057552839")

            context = manager.prompt_context("qq_group_123456", qids=["550808201"])

            self.assertEqual(context["qid_to_nickname"], {"550808201": "小夏"})
            self.assertIn("昵称=小夏", "\n".join(context["personal_memory"]))
            self.assertNotIn("昵称=小明", "\n".join(context["personal_memory"]))

    def test_observe_group_text_records_clear_self_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)

            payload = manager.observe_group_text(
                "我叫小夏",
                session_id="qq_group_123456",
                sender_qid="550808201",
                timestamp=datetime(2026, 6, 30, 23, 32),
            )

            self.assertEqual(payload["status"], "identity_observed")
            self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {"550808201": "小夏"})
            content = manager.read("qq_group_123456")
            self.assertIn("<550808201><2026-06-30T23:32:00><群聊/明确自称><昵称=小夏>", content)

    def test_observe_group_text_rejects_generic_self_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)

            payload = manager.observe_group_text(
                "我是一个路过的人",
                session_id="qq_group_123456",
                sender_qid="550808201",
            )

            self.assertEqual(payload["status"], "skipped")
            self.assertEqual(payload["reason"], "no_self_identity")
            self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {})


if __name__ == "__main__":
    unittest.main()
