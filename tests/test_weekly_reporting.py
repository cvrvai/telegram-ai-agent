from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.models import IncomingMessage, PriorityClassification
from app.priority.situations import SituationLinker
from app.priority.situation_tools import WeeklyReportInput, generate_weekly_report
from app.reporting.weekly_deck import WeeklyDeckBuilder
from app.reporting.weekly_pdf import WeeklyPdfBuilder
from app.storage.sqlite_messages import Database
from config import AppConfig


class WeeklyReportingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "messages.db"))
        await self.db.init_db()

        # Seed sample situations
        linker = SituationLinker(AppConfig())
        msg1 = IncomingMessage(
            message_id=1,
            chat_id=1001,
            chat_title="Engineering",
            chat_type="group",
            sender_name="Sophat",
            text="Water pump pressure drop in Building B",
            date=datetime.now(timezone.utc),
            message_link="https://t.me/c/1001/1",
        )
        cls1 = PriorityClassification(
            priority="P0",
            score=95,
            reason="critical facility",
            needs_action=True,
            action="Repair pump",
            category="engineering",
            summary="Water pump failure Building B",
            message_type="task",
            critical_impact=True,
        )
        dec1 = await linker.link(msg1, cls1, open_situations=[])
        await self.db.create_situation(msg1, dec1, cls1)

        msg2 = IncomingMessage(
            message_id=2,
            chat_id=1002,
            chat_title="Housekeeping",
            chat_type="group",
            sender_name="Dara",
            text="Linen restocking delay",
            date=datetime.now(timezone.utc),
            message_link="https://t.me/c/1002/2",
        )
        cls2 = PriorityClassification(
            priority="P2",
            score=50,
            reason="inventory",
            needs_action=True,
            action="Follow up supplier",
            category="housekeeping",
            summary="Linen delay",
            message_type="update",
            critical_impact=False,
        )
        dec2 = await linker.link(msg2, cls2, open_situations=[])
        await self.db.create_situation(msg2, dec2, cls2)

        self.mock_repo = SimpleNamespace(
            list_actions=AsyncMock(return_value=[
                {"id": 1, "action_type": "telegram_message", "status": "pending"}
            ])
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_weekly_deck_builder_generates_pptx(self) -> None:
        builder = WeeklyDeckBuilder(self.db, self.mock_repo)
        out_path = os.path.join(self.temp_dir.name, "report.pptx")
        await builder.build(out_path, title="Tara Angkor Hotel Weekly Deck")

        self.assertTrue(os.path.exists(out_path))
        self.assertGreater(os.path.getsize(out_path), 1000)
        with open(out_path, "rb") as f:
            header = f.read(4)
        self.assertEqual(header, b"PK\x03\x04")  # Valid PPTX (zip archive)

    async def test_weekly_pdf_builder_generates_pdf(self) -> None:
        builder = WeeklyPdfBuilder(self.db, self.mock_repo)
        out_path = os.path.join(self.temp_dir.name, "report.pdf")
        await builder.build(out_path, title="Tara Angkor Hotel Weekly Brief")

        self.assertTrue(os.path.exists(out_path))
        self.assertGreater(os.path.getsize(out_path), 1000)
        with open(out_path, "rb") as f:
            header = f.read(4)
        self.assertEqual(header, b"%PDF")  # Valid PDF

    async def test_generate_weekly_report_tool_pptx_and_pdf(self) -> None:
        session = {}
        context = SimpleNamespace(
            message_database=self.db,
            repository=self.mock_repo,
            allowed_chat_ids={1001, 1002},
            session=session,
        )

        # 1. PPTX test
        res_pptx = await generate_weekly_report(WeeklyReportInput(title="Executive PPTX", format="pptx"), context)
        self.assertTrue(res_pptx["generated"])
        self.assertEqual(res_pptx["format"], "pptx")
        self.assertIn("pending_file", session)
        pptx_file = session["pending_file"]["path"]
        self.assertTrue(os.path.exists(pptx_file))
        self.assertTrue(pptx_file.endswith(".pptx"))

        # 2. PDF test
        res_pdf = await generate_weekly_report(WeeklyReportInput(title="Executive PDF", format="pdf"), context)
        self.assertTrue(res_pdf["generated"])
        self.assertEqual(res_pdf["format"], "pdf")
        self.assertIn("pending_file", session)
        pdf_file = session["pending_file"]["path"]
        self.assertTrue(os.path.exists(pdf_file))
        self.assertTrue(pdf_file.endswith(".pdf"))


if __name__ == "__main__":
    unittest.main()
