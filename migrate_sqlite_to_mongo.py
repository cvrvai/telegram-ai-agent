"""One-time migration for legacy SQLite data into MongoDB.

The operation is idempotent by numeric record id and leaves the SQLite file
untouched. Stop the bot before running it so no new rows arrive mid-migration.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import aiosqlite


async def rows(db: aiosqlite.Connection, table: str) -> list[dict[str, Any]]:
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(f"SELECT * FROM {table}")
    except aiosqlite.OperationalError:
        return []
    return [dict(row) for row in await cursor.fetchall()]


async def migrate(sqlite_path: str, mongo_uri: str, mongo_database: str) -> None:
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError as exc:
        raise RuntimeError("Mongo migration requires the motor package; install the project dependencies first") from exc
    client = AsyncIOMotorClient(mongo_uri, serverSelectionTimeoutMS=5_000)
    await client.admin.command("ping")
    mongo = client[mongo_database]
    max_ids: list[int] = []
    async with aiosqlite.connect(sqlite_path) as source:
        for table in ("messages", "digests"):
            documents = await rows(source, table)
            target = mongo[table]
            for document in documents:
                identifier = int(document["id"])
                await target.replace_one({"id": identifier}, document, upsert=True)
                max_ids.append(identifier)

        for table, key in (
            ("assistant_users", "user_id"),
            ("assistant_profiles", "assistant_id"),
            ("assistant_groups", "chat_id"),
            ("assistant_pairings", "code"),
        ):
            for document in await rows(source, table):
                await mongo[table].replace_one({key: document[key]}, document, upsert=True)

        for document in await rows(source, "assistant_memberships"):
            identifier = f"{document['assistant_id']}:{document['user_id']}"
            await mongo["assistant_memberships"].replace_one({"_id": identifier}, {**document, "_id": identifier}, upsert=True)

        for document in await rows(source, "assistant_conversations"):
            identifier = str(document["id"])
            await mongo["assistant_conversations"].replace_one(
                {"_id": identifier},
                {**document, "_id": identifier},
                upsert=True,
            )

        for document in await rows(source, "assistant_turns"):
            identifier = str(document["id"])
            await mongo["assistant_turns"].replace_one(
                {"_id": identifier},
                {**document, "_id": identifier, "conversation_id": str(document["conversation_id"])},
                upsert=True,
            )

        for document in await rows(source, "ai_usage"):
            identifier = str(document["id"])
            await mongo["ai_usage"].replace_one({"_id": identifier}, {**document, "_id": identifier}, upsert=True)

        # Preserve business-tool records and their pending approval state.
        for table in ("assistant_tasks", "assistant_reminders", "assistant_actions", "assistant_departments", "assistant_projects", "assistant_project_members", "assistant_task_dependencies", "assistant_milestones", "assistant_task_comments"):
            for document in await rows(source, table):
                identifier = str(document["id"])
                await mongo[table].replace_one({"_id": identifier}, {**document, "_id": identifier}, upsert=True)

    if max_ids:
        await mongo["counters"].update_one({"_id": "messages"}, {"$max": {"value": max(max_ids)}}, upsert=True)
    await client.close()
    print(f"Migration complete: {len(max_ids)} legacy message/digest records copied.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Telegram bot SQLite data to MongoDB")
    parser.add_argument("--sqlite", default="telegram_bot.db")
    parser.add_argument("--mongo-uri", required=True)
    parser.add_argument("--mongo-database", default="telegram_business")
    args = parser.parse_args()
    asyncio.run(migrate(args.sqlite, args.mongo_uri, args.mongo_database))


if __name__ == "__main__":
    main()
