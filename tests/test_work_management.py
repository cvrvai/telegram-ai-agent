import asyncio
import os
import tempfile
import unittest

from app.projects.workflow import can_transition, validate_priority
from app.storage.business import BusinessRepository
from app.priority.classifier import AIClassifier
from app.core.models import IncomingMessage


class WorkManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.repo = BusinessRepository(self.path)
        await self.repo.init()

    async def asyncTearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    async def test_project_keys_and_sequence_are_human_readable(self):
        project_id = await self.repo.create_project(1, "Red Craw Sales")
        first = await self.repo.create_task(1, "Customer order", project_id=project_id, priority="P0")
        second = await self.repo.create_task(1, "Packaging", project_id=project_id, parent_id=first, priority="P3")
        rows = await self.repo.list_work_items(1, "all", 10)
        self.assertEqual({row["display_key"] for row in rows}, {"REDCRAWSAL-1", "REDCRAWSAL-2"})
        self.assertEqual(validate_priority("normal"), "P2")

    async def test_status_transitions_and_hierarchy_are_enforced(self):
        project_id = await self.repo.create_project(1, "Operations")
        item = await self.repo.create_task(1, "Prepare shipment", project_id=project_id)
        self.assertTrue(can_transition("todo", "in_progress"))
        self.assertTrue(await self.repo.update_task(1, item, status="in_progress"))
        with self.assertRaises(ValueError):
            await self.repo.update_task(1, item, status="done")
        with self.assertRaises(ValueError):
            await self.repo.create_task(1, "Wrong parent", project_id=project_id, parent_id=99999)

    def test_priority_and_type_are_independent_in_fallback(self):
        classifier = AIClassifier()
        message = IncomingMessage(message_id=1, chat_id=2, chat_title="Ops", chat_type="private", sender_name="owner", text="FYI production deployment is complete.")
        result = classifier._fallback_classification(message)
        self.assertEqual(result.priority, "P3")
        self.assertEqual(result.message_type, "chatter")

        blocker = IncomingMessage(message_id=2, chat_id=2, chat_title="Ops", chat_type="private", sender_name="owner", text="Production down, this is blocking orders!")
        result = classifier._fallback_classification(blocker)
        self.assertEqual(result.priority, "P0")
        self.assertEqual(result.message_type, "blocker")


if __name__ == "__main__":
    unittest.main()


