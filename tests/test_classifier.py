"""Comprehensive simulation test suite for AI Classification and Digest formatting."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config import config
from app.core.models import IncomingMessage
from app.storage.sqlite_messages import Database
from app.priority.prefilter import evaluate_prefilter
from app.priority.classifier import AIClassifier
from app.telegram.notifier import Notifier
from app.priority.digest import DigestEngine

console = Console(force_terminal=True, legacy_windows=False)



# Realistic test cases representing different priority tiers and contexts
SAMPLE_MESSAGES = [
    IncomingMessage(
        message_id=101,
        chat_id=1001,
        chat_title="University CS Dept - Year 4",
        chat_type="supergroup",
        sender_id=501,
        sender_name="Prof. Somnang",
        sender_username="prof_somnang",
        text="Dear students, please note that Assignment 3 submission deadline has been moved to this Friday 5:00 PM due to server maintenance. Please verify your submissions.",
        date=datetime.now(),
        message_link="https://t.me/c/1001/101",
    ),
    IncomingMessage(
        message_id=102,
        chat_id=1002,
        chat_title="CloudKH Core Dev",
        chat_type="group",
        sender_id=502,
        sender_name="Dara (Tech Lead)",
        sender_username="dara_dev",
        text="Hey @cheon, we finished the new payment gateway UI wireframe. We need your sign-off and approval before we can push the release to staging.",
        date=datetime.now(),
        message_link="https://t.me/c/1002/102",
    ),
    IncomingMessage(
        message_id=103,
        chat_id=1003,
        chat_title="Server Hosting Supplier",
        chat_type="private",
        sender_id=503,
        sender_name="Cloud Host Billing",
        sender_username="host_billing",
        text="URGENT: Dedicated server invoice #9823 is due today. Please send payment confirmation to avoid service disconnection at 6 PM.",
        date=datetime.now(),
        message_link="https://t.me/c/1003/103",
    ),
    IncomingMessage(
        message_id=104,
        chat_id=1002,
        chat_title="CloudKH Core Dev",
        chat_type="group",
        sender_id=504,
        sender_name="Sophea (Backend)",
        sender_username="sophea_py",
        text="FYI team: Redis caching layer upgrade is completed and merged into dev branch. All benchmarks look green.",
        date=datetime.now(),
        message_link="https://t.me/c/1002/104",
    ),
    IncomingMessage(
        message_id=105,
        chat_id=1004,
        chat_title="Football & Weekend Chill",
        chat_type="group",
        sender_id=505,
        sender_name="Vireak",
        sender_username="vireak99",
        text="Barcelona vs Real Madrid match moved to 3 PM tonight! Anyone going to the cafe to watch together?",
        date=datetime.now(),
        message_link="https://t.me/c/1004/105",
    ),
    IncomingMessage(
        message_id=106,
        chat_id=1004,
        chat_title="Football & Weekend Chill",
        chat_type="group",
        sender_id=506,
        sender_name="Bona",
        sender_username="bona_k",
        text="hahaha lol sure 👍🔥",
        date=datetime.now(),
        message_link="https://t.me/c/1004/106",
    ),
    IncomingMessage(
        message_id=107,
        chat_id=1005,
        chat_title="Friend Chat",
        chat_type="private",
        sender_id=507,
        sender_name="Koson",
        sender_username="koson_m",
        text="",
        media_type="sticker",
        date=datetime.now(),
        message_link="https://t.me/c/1005/107",
    ),
]


async def run_tests():
    console.print(Panel.fit("[bold cyan]🚀 Starting Telegram AI Priority Filter Test Suite[/bold cyan]"))

    test_db = Database("test_telegram.db")
    await test_db.init_db()

    classifier = AIClassifier(config)
    notifier = Notifier(config)
    digest_engine = DigestEngine(test_db)

    results_table = Table(title="Message Classification Results")
    results_table.add_column("ID", style="dim")
    results_table.add_column("Chat / Sender", style="bold")
    results_table.add_column("Priority", style="bold")
    results_table.add_column("Score", justify="right")
    results_table.add_column("Action Required", style="yellow")
    results_table.add_column("Prefiltered?", style="magenta")
    results_table.add_column("Summary", style="green")

    alerts_generated = []

    for msg in SAMPLE_MESSAGES:
        # Step 1: Pre-filter check
        pre_cls = evaluate_prefilter(msg, config.profile.vip_senders)

        if pre_cls:
            cls = pre_cls
            is_prefiltered = True
        else:
            cls = await classifier.classify(msg)
            is_prefiltered = False

        should_alert = cls.score >= config.urgent_score_threshold or cls.priority in config.alert_priority_list

        await test_db.save_message(msg, cls, is_prefiltered=is_prefiltered, alert_sent=should_alert)

        priority_style = {
            "P0": "[bold red]P0 URGENT[/bold red]",
            "P1": "[bold yellow]P1 IMPORTANT[/bold yellow]",
            "P2": "[cyan]P2 USEFUL[/cyan]",
            "P3": "[dim]P3 NOISE[/dim]",
        }.get(cls.priority, cls.priority)

        action_display = cls.action if (cls.needs_action and cls.action) else "None"
        prefilter_display = "⚡ Yes (0 tokens)" if is_prefiltered else "🤖 AI Evaluated"

        results_table.add_row(
            str(msg.message_id),
            f"{msg.chat_title}\n[dim]{msg.sender_name}[/dim]",
            priority_style,
            str(cls.score),
            action_display,
            prefilter_display,
            cls.summary,
        )

        if should_alert:
            alert_text = notifier.format_instant_alert(msg, cls)
            alerts_generated.append((msg, cls, alert_text))

    console.print(results_table)

    # Display Immediate Alerts
    console.print("\n[bold red]⚡ Instant Push Notifications (Triggered on Score >= 90 or P0/P1):[/bold red]")
    for msg, cls, alert_html in alerts_generated:
        console.print(Panel(alert_html, title=f"🚨 Immediate Alert: {msg.chat_title}"))

    # Generate Batch Digest
    console.print("\n[bold yellow]📋 Generating Periodic 3-Tier Digest:[/bold yellow]")
    digest_text, stats = await digest_engine.generate_digest()
    if digest_text:
        console.print(Panel(digest_text, title=f"Periodic Digest Output (Total: {stats.total_messages} messages)"))

    console.print("[bold green]✓ Test suite completed successfully![/bold green]")


if __name__ == "__main__":
    asyncio.run(run_tests())


