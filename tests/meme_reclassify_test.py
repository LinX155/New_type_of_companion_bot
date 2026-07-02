import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.memes.catalog import MemeCatalog
from app.memes.reclassify import MemeReclassifier, MemeReclassifyQueue, MemeReclassifyResult
from app.memes.steal import MemeStealAnalyzer, MemeStealSaver


class MemeReclassifyTest(unittest.TestCase):
    def test_reclassify_moves_file_and_keeps_dhash_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "memes"
            assets = base / "assets"
            (assets / "miscellaneous").mkdir(parents=True)
            (assets / "amused").mkdir(parents=True)
            (base / "memes_data.json").write_text(json.dumps({
                "miscellaneous": {"name": "misc", "description": "misc"},
                "amused": {"name": "amused", "description": "funny"},
            }), encoding="utf-8")
            source = assets / "miscellaneous" / "miscellaneous_old_wrong.png"
            Image.new("RGB", (32, 32), color=(255, 30, 90)).save(source)

            catalog = MemeCatalog(str(base))
            analyzer = MemeStealAnalyzer(catalog, tmp)
            saver = MemeStealSaver(catalog, tmp)
            reclassifier = MemeReclassifier(catalog=catalog, analyzer=analyzer, saver=saver)
            before_hash = catalog._compute_dhash(str(source))
            llm = FakeLLM({
                "save_name": "amused_cat_laughing_face",
                "keywords": ["cat", "laughing", "face"],
                "reason": "更像开心调侃表情",
                "safety": "ok",
            })

            result = asyncio.run(reclassifier.reclassify(
                from_category="miscellaneous",
                file_stem="miscellaneous_old_wrong",
                target_category="amused",
                llm_client=llm,
                job_id="job_test",
            ))

            self.assertEqual(result.status, "moved")
            self.assertEqual(result.target_category, "amused")
            self.assertEqual(result.hash_before, before_hash)
            self.assertEqual(result.hash_after, before_hash)
            self.assertFalse(source.exists())
            self.assertTrue(Path(result.file_path).exists())
            self.assertTrue(result.file_stem.startswith("amused_cat_laughing_face"))
            self.assertIn("target_category: amused", json.dumps(llm.messages, ensure_ascii=False))

    def test_reclassify_queue_processes_jobs_serially(self):
        async def scenario():
            calls = []
            queue = MemeReclassifyQueue(
                reclassifier=FakeReclassifier(calls),
                llm_client_factory=lambda: FakeApiKeyClient(),
            )
            first = await queue.submit(
                from_category="miscellaneous",
                file_stem="one",
                target_category="amused",
            )
            second = await queue.submit(
                from_category="miscellaneous",
                file_stem="two",
                target_category="amused",
            )
            for _ in range(100):
                first_job = queue.get(first["job_id"])
                second_job = queue.get(second["job_id"])
                if first_job["status"] == "completed" and second_job["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(calls, ["one", "two"])
            self.assertEqual(queue.get(first["job_id"])["status"], "completed")
            self.assertEqual(queue.get(second["job_id"])["status"], "completed")

        asyncio.run(scenario())


class FakeLLM:
    api_key = "test"

    def __init__(self, payload: dict):
        self.payload = payload
        self.messages = None

    async def chat_completion(self, *, messages, temperature):
        self.messages = messages
        return json.dumps(self.payload, ensure_ascii=False)


class FakeApiKeyClient:
    api_key = "test"


class FakeReclassifier:
    def __init__(self, calls: list[str]):
        self.calls = calls

    async def reclassify(self, *, from_category, file_stem, target_category, llm_client, job_id=None):
        self.calls.append(file_stem)
        await asyncio.sleep(0.02)
        return MemeReclassifyResult(
            status="moved",
            job_id=job_id,
            from_category=from_category,
            target_category=target_category,
            original_file_stem=file_stem,
            file_stem=f"{target_category}_{file_stem}_renamed",
        )


if __name__ == "__main__":
    unittest.main()
