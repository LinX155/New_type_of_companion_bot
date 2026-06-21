import tempfile
import unittest

from app.memory.files import MemoryFileManager


class MemoryFileManagerTest(unittest.TestCase):
    def test_sync_mem_append_disambiguates_user_and_assistant_pronouns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryFileManager(base_dir=tmpdir)

            success, _ = manager.apply_mem_command("/mem 我希望你少用 emoji")

            self.assertTrue(success)
            core = manager.read_memory_core()
            self.assertIn("用户希望我少用 emoji", core)
            self.assertNotIn("我希望你少用 emoji", core)

    def test_sync_mem_append_disambiguates_negative_preference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryFileManager(base_dir=tmpdir)

            success, _ = manager.apply_mem_command("/mem 我不喜欢初音未来了")

            self.assertTrue(success)
            core = manager.read_memory_core()
            self.assertIn("用户不喜欢初音未来了", core)
            self.assertNotIn("我不喜欢初音未来了", core)

    def test_sync_mem_append_disambiguates_direct_assistant_instruction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryFileManager(base_dir=tmpdir)

            success, _ = manager.apply_mem_command("/mem 你应该多主动找我聊天")

            self.assertTrue(success)
            core = manager.read_memory_core()
            self.assertIn("我应该多主动找用户聊天", core)
            self.assertNotIn("你应该多主动找我聊天", core)

    def test_sync_mem_append_disambiguates_future_assistant_instruction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryFileManager(base_dir=tmpdir)

            success, _ = manager.apply_mem_command("/mem 你以后不要老问我问题")

            self.assertTrue(success)
            core = manager.read_memory_core()
            self.assertIn("我以后不要老问用户问题", core)
            self.assertNotIn("你以后不要老问我问题", core)


if __name__ == "__main__":
    unittest.main()
