"""Business Management CLI interface.

Provides command-line actions for OpenClaw and local automation to manage
projects, tasks, Critical Path Method (CPM) schedules, milestones, and departments.
Backed by BusinessRepository.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.storage.business import BusinessRepository
from app.projects.cpm import calculate_cpm


DEFAULT_USER_ID = int(os.getenv("NOTIFICATION_CHAT_ID") or os.getenv("OWNER_USER_ID") or "0")
DEFAULT_DB_PATH = str(Path(os.getenv("BUSINESS_DB_PATH", REPO_ROOT / "telegram_business.db")).resolve())


async def get_repo(db_path: str = DEFAULT_DB_PATH, user_id: int = DEFAULT_USER_ID) -> BusinessRepository:
    repo = BusinessRepository(db_path=db_path)
    await repo.init()
    # Ensure default user exists and is approved
    await repo.upsert_user(user_id=user_id, display_name="Admin", role="owner", approved=True)
    return repo


# ---------------- Project Commands ----------------

async def cmd_project_create(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    project_id = await repo.create_project(
        user_id=args.user_id,
        name=args.name,
        description=args.description or "",
        department_id=args.department_id,
        start_date=args.start_date,
        target_date=args.target_date,
    )
    project = await repo.get_project(args.user_id, project_id)
    if args.json:
        print(json.dumps(project, default=str))
    else:
        print("Project created successfully!")
        print(f"ID: {project_id}")
        print(f"Key: {project.get('project_key') if project else 'N/A'}")
        print(f"Name: {args.name}")
        if args.description:
            print(f"Description: {args.description}")


async def cmd_project_list(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    projects = await repo.list_projects(user_id=args.user_id, limit=args.limit)
    if args.json:
        print(json.dumps(projects, default=str))
    else:
        if not projects:
            print("No projects found.")
            return
        print(f"Projects ({len(projects)}):")
        print(f"{'ID':<6} | {'Key':<10} | {'Status':<10} | {'Name'}")
        print("-" * 50)
        for p in projects:
            print(f"{p['id']:<6} | {p.get('project_key', ''):<10} | {p.get('status', 'active'):<10} | {p['name']}")


async def cmd_project_plan(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    plan_data = await repo.project_plan_data(user_id=args.user_id, project_id=args.project_id)
    if not plan_data or not plan_data.get("project"):
        print(f"Error: Project {args.project_id} not found.", file=sys.stderr)
        sys.exit(1)

    project = plan_data["project"]
    tasks = plan_data.get("tasks", [])
    dependencies = plan_data.get("dependencies", [])
    milestones = plan_data.get("milestones", [])

    # Map predecessors for CPM
    dep_map = defaultdict(list)
    for dep in dependencies:
        dep_map[dep["task_id"]].append(dep["predecessor_id"])

    for t in tasks:
        t["predecessors"] = dep_map.get(t["id"], [])

    cpm_result = {}
    cpm_error = None
    if tasks:
        try:
            cpm_result = calculate_cpm(tasks)
        except Exception as exc:
            cpm_error = str(exc)

    output = {
        "project": project,
        "tasks": tasks,
        "dependencies": dependencies,
        "milestones": milestones,
        "cpm": cpm_result,
        "cpm_error": cpm_error,
    }

    if args.json:
        print(json.dumps(output, default=str))
    else:
        print(f"=== Project Plan: {project['name']} (Key: {project.get('project_key')}) ===")
        print(f"Status: {project.get('status')} | Target Date: {project.get('target_date') or 'None'}")
        print(f"Total Tasks: {len(tasks)} | Milestones: {len(milestones)}")
        if cpm_error:
            print(f"Warning (CPM): {cpm_error}")
        elif cpm_result:
            print(f"Estimated Duration (Critical Path): {cpm_result.get('duration_days', 0)} working days")
            critical_ids = set(str(cid) for cid in cpm_result.get("critical_task_ids", []))
            print("\nTasks & Critical Path:")
            print(f"{'ID':<6} | {'Key':<10} | {'Status':<12} | {'Dur(d)':<6} | {'Crit':<6} | {'Title'}")
            print("-" * 65)
            for t in cpm_result.get("tasks", tasks):
                is_crit = "YES *" if str(t["id"]) in critical_ids else "no"
                print(f"{t['id']:<6} | {t.get('display_key') or '-':<10} | {t.get('status'):<12} | {t.get('duration_days', 1):<6} | {is_crit:<6} | {t['title']}")

        if milestones:
            print("\nMilestones:")
            for m in milestones:
                print(f"- [ID: {m['id']}] {m['name']} (Target: {m['target_date']}) - Status: {m.get('status', 'open')}")


# ---------------- Task Commands ----------------

async def cmd_task_create(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    task_id = await repo.create_task(
        user_id=args.user_id,
        title=args.title,
        details=args.details or "",
        due_at=args.due_at,
        project_id=args.project_id,
        assignee_id=args.assignee_id,
        priority=args.priority or "P2",
        duration_days=args.duration or 1,
        item_type=args.type or "task",
        parent_id=args.parent_id,
    )
    task = await repo.get_work_item(args.user_id, task_id)
    if args.json:
        print(json.dumps(task, default=str))
    else:
        print("Task created successfully!")
        print(f"ID: {task_id}")
        if task and task.get("display_key"):
            print(f"Key: {task['display_key']}")
        print(f"Title: {args.title}")
        print(f"Priority: {args.priority or 'P2'} | Duration: {args.duration or 1} days")
        if args.project_id:
            print(f"Project ID: {args.project_id}")


async def cmd_task_list(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    tasks = await repo.list_tasks(
        user_id=args.user_id,
        status=args.status or "open",
        limit=args.limit,
        project_id=args.project_id,
        assignee_id=args.assignee_id,
    )
    if args.json:
        print(json.dumps(tasks, default=str))
    else:
        if not tasks:
            print("No tasks found matching criteria.")
            return
        print(f"Tasks ({len(tasks)}):")
        print(f"{'ID':<6} | {'Key':<10} | {'Status':<12} | {'Priority':<8} | {'Title'}")
        print("-" * 65)
        for t in tasks:
            key = t.get("display_key") or "-"
            print(f"{t['id']:<6} | {key:<10} | {t.get('status'):<12} | {t.get('priority', 'normal'):<8} | {t['title']}")


async def cmd_task_update(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    updated = await repo.update_task(
        user_id=args.user_id,
        task_id=args.task_id,
        status=args.status,
        assignee_id=args.assignee_id,
        priority=args.priority,
        due_at=args.due_at,
    )
    if not updated:
        print(f"Error: Failed to update task {args.task_id} (not found or invalid state transition).", file=sys.stderr)
        sys.exit(1)
    task = await repo.get_work_item(args.user_id, args.task_id)
    if args.json:
        print(json.dumps(task, default=str))
    else:
        print(f"Task {args.task_id} updated successfully!")
        if args.status:
            print(f"Status: {args.status}")
        if args.priority:
            print(f"Priority: {args.priority}")


async def cmd_dependency_add(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    await repo.add_dependency(task_id=args.task_id, predecessor_id=args.depends_on)
    if args.json:
        print(json.dumps({"success": True, "task_id": args.task_id, "predecessor_id": args.depends_on}))
    else:
        print(f"Added dependency: Task {args.task_id} now depends on Task {args.depends_on}.")


# ---------------- Department Commands ----------------

async def cmd_dept_create(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    dept_id = await repo.create_department(name=args.name, description=args.description or "")
    if args.json:
        print(json.dumps({"id": dept_id, "name": args.name, "description": args.description or ""}))
    else:
        print(f"Department '{args.name}' registered (ID: {dept_id}).")


async def cmd_dept_list(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    depts = await repo.list_departments()
    if args.json:
        print(json.dumps(depts, default=str))
    else:
        if not depts:
            print("No departments configured.")
            return
        print(f"Departments ({len(depts)}):")
        for d in depts:
            print(f"- [ID: {d['id']}] {d['name']}: {d.get('description') or 'No description'}")


# ---------------- Milestone & Comment Commands ----------------

async def cmd_milestone_create(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    mid = await repo.add_milestone(project_id=args.project_id, name=args.name, target_date=args.target_date)
    if args.json:
        print(json.dumps({"id": mid, "project_id": args.project_id, "name": args.name, "target_date": args.target_date}))
    else:
        print(f"Milestone '{args.name}' created for Project {args.project_id} (ID: {mid}, Target: {args.target_date}).")


async def cmd_comment_add(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    cid = await repo.add_task_comment(task_id=args.task_id, user_id=args.user_id, comment=args.comment)
    if args.json:
        print(json.dumps({"id": cid, "task_id": args.task_id, "comment": args.comment}))
    else:
        print(f"Comment added to Task {args.task_id} (Comment ID: {cid}).")


# ---------------- Focus & User Approval Commands ----------------

async def cmd_focus_list(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    scopes = await repo.list_focus_scopes(enabled_only=not args.all)
    if args.json:
        print(json.dumps(scopes, default=str))
    else:
        if not scopes:
            print("No monitored chats or contacts configured.")
            return
        print(f"Monitored Chats & Contacts ({len(scopes)}):")
        print(f"{'Chat ID':<16} | {'Title / Name':<25} | {'Mode':<10} | {'Status'}")
        print("-" * 65)
        for s in scopes:
            status = "🟢 Active" if s.get("enabled", 1) else "⚪ Disabled"
            print(f"{s['chat_id']:<16} | {s.get('chat_title', 'Unknown')[:25]:<25} | {s.get('mode', 'monitor'):<10} | {status}")


async def cmd_focus_add(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    await repo.upsert_focus_scope(
        assistant_id="business",
        chat_id=args.chat_id,
        chat_title=args.title,
        chat_type=args.type or "private",
        mode=args.mode or "monitor",
        enabled=True,
    )
    if args.json:
        print(json.dumps({"success": True, "chat_id": args.chat_id, "title": args.title, "mode": args.mode or "monitor"}))
    else:
        print(f"✅ Approved monitoring for '{args.title}' (ID: {args.chat_id}, Mode: {args.mode or 'monitor'}).")


async def cmd_focus_remove(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    await repo.disable_focus_scope(assistant_id="business", chat_id=args.chat_id)
    if args.json:
        print(json.dumps({"success": True, "chat_id": args.chat_id, "disabled": True}))
    else:
        print(f"Disabled monitoring for chat ID {args.chat_id}.")


async def cmd_user_list(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    users = await repo.list_users(limit=args.limit)
    if args.json:
        print(json.dumps(users, default=str))
    else:
        if not users:
            print("No registered users found.")
            return
        print(f"Registered Users ({len(users)}):")
        print(f"{'User ID':<16} | {'Name':<20} | {'Role':<10} | {'Approved'}")
        print("-" * 55)
        for u in users:
            appr = "✅ Yes" if u.get("approved") else "❌ No"
            name = u.get("display_name") or "User"
            print(f"{u['user_id']:<16} | {name[:20]:<20} | {u.get('role', 'user'):<10} | {appr}")


async def cmd_user_approve(args: argparse.Namespace) -> None:
    repo = await get_repo(args.db, args.user_id)
    await repo.set_user_approval(user_id=args.target_user_id, approved=True)
    if args.name:
        await repo.upsert_user(user_id=args.target_user_id, display_name=args.name, role=args.role or "user", approved=True)
USERBOT_API_URL = os.getenv("USERBOT_API_URL", "http://127.0.0.1:18790")


async def cmd_contact_list(args: argparse.Namespace) -> None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{USERBOT_API_URL}/contacts", params={"q": args.query or "", "limit": args.limit})
            data = resp.json()
    except Exception as exc:
        print(f"Error: Could not reach Userbot listener ({exc}). Make sure 'python scripts/userbot_listener.py' is running.", file=sys.stderr)
        sys.exit(1)

    contacts = data.get("contacts", [])
    if args.json:
        print(json.dumps(contacts, default=str))
    else:
        if not contacts:
            print(f"No contacts found matching '{args.query or ''}'.")
            return
        print(f"Telegram Contacts ({len(contacts)}):")
        print(f"{'Chat ID':<16} | {'Name / Title':<25} | {'Username':<15} | {'Monitored'}")
        print("-" * 72)
        for c in contacts:
            uname = f"@{c['username']}" if c.get("username") else "-"
            mon = "🟢 " + c.get("permission", "always") if c.get("monitored") else "⚪ None"
            print(f"{c['id']:<16} | {c['title'][:25]:<25} | {uname:<15} | {mon}")


async def cmd_contact_send(args: argparse.Namespace) -> None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{USERBOT_API_URL}/send", json={"target": args.target, "message": args.message})
            data = resp.json()
    except Exception as exc:
        print(f"Error: Could not reach Userbot listener ({exc}). Make sure 'python scripts/userbot_listener.py' is running.", file=sys.stderr)
        sys.exit(1)

    if not resp.is_success or "error" in data:
        print(f"Error sending message: {data.get('error')}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(data, default=str))
    else:
        print(f"✅ Message sent successfully to '{args.target}' (Message ID: {data.get('message_id')})!")


async def cmd_contact_permit(args: argparse.Namespace) -> None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{USERBOT_API_URL}/permission",
                json={"target": args.target, "title": args.title or "Contact", "scope": args.scope},
            )
            data = resp.json()
    except Exception as exc:
        print(f"Error: Could not reach Userbot listener ({exc}). Make sure 'python scripts/userbot_listener.py' is running.", file=sys.stderr)
        sys.exit(1)

    if not resp.is_success or "error" in data:
        print(f"Error setting permission: {data.get('error')}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(data, default=str))
    else:
        scope_str = "Only this time (temporary)" if args.scope == "once" else "Always monitor (persistent)"
        print(f"✅ Granted permission for '{args.title or args.target}': {scope_str}")


async def cmd_contact_read(args: argparse.Namespace) -> None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{USERBOT_API_URL}/messages", params={"target": args.target, "limit": args.limit})
            data = resp.json()
    except Exception as exc:
        print(f"Error: Could not reach Userbot listener ({exc}). Make sure 'python scripts/userbot_listener.py' is running.", file=sys.stderr)
        sys.exit(1)

    msgs = data.get("messages", [])
    if args.json:
        print(json.dumps(msgs, default=str))
    else:
        if not msgs:
            print(f"No recent messages found with '{args.target}'.")
            return
        print(f"Recent Messages with {args.target} ({len(msgs)}):")
        for m in reversed(msgs):
            print(f"- [{m.get('date', '')[:16]}] {m.get('text', '').strip()[:100]}")


# ---------------- CLI Parser ----------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite database path (default: {DEFAULT_DB_PATH})")
    common.add_argument("--user-id", type=int, default=DEFAULT_USER_ID, help=f"User ID (default: {DEFAULT_USER_ID})")
    common.add_argument("--json", action="store_true", help="Output results as JSON")

    parser = argparse.ArgumentParser(description="Business Management CLI", parents=[common])
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Project subcommands
    p_proj = subparsers.add_parser("project", help="Manage projects", parents=[common])
    p_proj_sub = p_proj.add_subparsers(dest="subcommand", required=True)

    p_p_create = p_proj_sub.add_parser("create", help="Create a project", parents=[common])
    p_p_create.add_argument("--name", required=True, help="Project name")
    p_p_create.add_argument("--description", "--desc", help="Project description")
    p_p_create.add_argument("--department-id", type=int, help="Department ID")
    p_p_create.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    p_p_create.add_argument("--target-date", help="Target completion date (YYYY-MM-DD)")
    p_p_create.set_defaults(func=cmd_project_create)

    p_p_list = p_proj_sub.add_parser("list", help="List projects", parents=[common])
    p_p_list.add_argument("--limit", type=int, default=50, help="Max projects to return")
    p_p_list.set_defaults(func=cmd_project_list)

    p_p_plan = p_proj_sub.add_parser("plan", help="Get project plan & CPM schedule", parents=[common])
    p_p_plan.add_argument("project_id", type=int, help="Project ID")
    p_p_plan.set_defaults(func=cmd_project_plan)

    # Task subcommands
    p_task = subparsers.add_parser("task", help="Manage tasks", parents=[common])
    p_task_sub = p_task.add_subparsers(dest="subcommand", required=True)

    p_t_create = p_task_sub.add_parser("create", help="Create a task", parents=[common])
    p_t_create.add_argument("--title", required=True, help="Task title")
    p_t_create.add_argument("--project-id", type=int, help="Associated Project ID")
    p_t_create.add_argument("--details", help="Detailed description")
    p_t_create.add_argument("--duration", type=int, default=1, help="Duration in working days (for CPM)")
    p_t_create.add_argument("--priority", choices=["P0", "P1", "P2", "P3", "urgent", "high", "normal", "low"], default="P2", help="Priority")
    p_t_create.add_argument("--assignee-id", type=int, help="Assignee Telegram User ID")
    p_t_create.add_argument("--due-at", help="Due date/time (ISO 8601 or YYYY-MM-DD)")
    p_t_create.add_argument("--type", choices=["task", "bug", "story", "epic"], default="task", help="Work item type")
    p_t_create.add_argument("--parent-id", type=int, help="Parent task ID (for subtasks)")
    p_t_create.set_defaults(func=cmd_task_create)

    p_t_list = p_task_sub.add_parser("list", help="List tasks", parents=[common])
    p_t_list.add_argument("--project-id", type=int, help="Filter by Project ID")
    p_t_list.add_argument("--status", choices=["open", "all", "todo", "in_progress", "review", "waiting", "blocked", "done", "backlog"], default="open", help="Status filter")
    p_t_list.add_argument("--assignee-id", type=int, help="Filter by assignee user ID")
    p_t_list.add_argument("--limit", type=int, default=50, help="Max tasks to return")
    p_t_list.set_defaults(func=cmd_task_list)

    p_t_update = p_task_sub.add_parser("update", help="Update task status or attributes", parents=[common])
    p_t_update.add_argument("task_id", type=int, help="Task ID to update")
    p_t_update.add_argument("--status", choices=["todo", "in_progress", "review", "waiting", "blocked", "done", "backlog"], help="New status")
    p_t_update.add_argument("--priority", choices=["P0", "P1", "P2", "P3", "urgent", "high", "normal", "low"], help="New priority")
    p_t_update.add_argument("--assignee-id", type=int, help="New assignee user ID")
    p_t_update.add_argument("--due-at", help="New due date")
    p_t_update.set_defaults(func=cmd_task_update)

    p_t_dep = p_task_sub.add_parser("dep", help="Add dependency between tasks", parents=[common])
    p_t_dep.add_argument("--task-id", type=int, required=True, help="Task that depends on predecessor")
    p_t_dep.add_argument("--depends-on", type=int, required=True, help="Predecessor task ID that must complete first")
    p_t_dep.set_defaults(func=cmd_dependency_add)

    # Department subcommands
    p_dept = subparsers.add_parser("dept", help="Manage departments", parents=[common])
    p_dept_sub = p_dept.add_subparsers(dest="subcommand", required=True)

    p_d_create = p_dept_sub.add_parser("create", help="Create department", parents=[common])
    p_d_create.add_argument("--name", required=True, help="Department name")
    p_d_create.add_argument("--description", "--desc", help="Department description")
    p_d_create.set_defaults(func=cmd_dept_create)

    p_d_list = p_dept_sub.add_parser("list", help="List departments", parents=[common])
    p_d_list.set_defaults(func=cmd_dept_list)

    # Milestone subcommand
    p_mile = subparsers.add_parser("milestone", help="Manage milestones", parents=[common])
    p_mile_sub = p_mile.add_subparsers(dest="subcommand", required=True)

    p_m_create = p_mile_sub.add_parser("create", help="Create milestone", parents=[common])
    p_m_create.add_argument("--project-id", type=int, required=True, help="Project ID")
    p_m_create.add_argument("--name", required=True, help="Milestone title")
    p_m_create.add_argument("--target-date", required=True, help="Target date (YYYY-MM-DD)")
    p_m_create.set_defaults(func=cmd_milestone_create)

    # Comment subcommand
    p_comm = subparsers.add_parser("comment", help="Manage task comments", parents=[common])
    p_comm_sub = p_comm.add_subparsers(dest="subcommand", required=True)

    p_c_add = p_comm_sub.add_parser("add", help="Add comment to task", parents=[common])
    p_c_add.add_argument("--task-id", type=int, required=True, help="Task ID")
    p_c_add.add_argument("--comment", required=True, help="Comment text")
    p_c_add.set_defaults(func=cmd_comment_add)

    # Focus / Monitored Chats subcommand
    p_focus = subparsers.add_parser("focus", help="Manage monitored contacts and focus scopes", parents=[common])
    p_focus_sub = p_focus.add_subparsers(dest="subcommand", required=True)

    p_f_list = p_focus_sub.add_parser("list", help="List monitored contacts and chats", parents=[common])
    p_f_list.add_argument("--all", action="store_true", help="Include disabled/removed scopes")
    p_f_list.set_defaults(func=cmd_focus_list)

    p_f_add = p_focus_sub.add_parser("add", help="Approve and monitor a contact or chat", parents=[common])
    p_f_add.add_argument("--chat-id", type=int, required=True, help="Telegram chat/user ID")
    p_f_add.add_argument("--title", required=True, help="Contact/Chat name (e.g. 'Daivai')")
    p_f_add.add_argument("--type", choices=["private", "group", "channel"], default="private", help="Chat type")
    p_f_add.add_argument("--mode", choices=["monitor", "mention", "orders"], default="monitor", help="Focus mode")
    p_f_add.set_defaults(func=cmd_focus_add)

    p_f_remove = p_focus_sub.add_parser("remove", help="Remove or disable monitoring for a chat", parents=[common])
    p_f_remove.add_argument("--chat-id", type=int, required=True, help="Telegram chat/user ID")
    p_f_remove.set_defaults(func=cmd_focus_remove)

    # User Approval subcommand
    p_user = subparsers.add_parser("user", help="Manage bot user approvals", parents=[common])
    p_user_sub = p_user.add_subparsers(dest="subcommand", required=True)

    p_u_list = p_user_sub.add_parser("list", help="List registered users", parents=[common])
    p_u_list.add_argument("--limit", type=int, default=50, help="Max users to list")
    p_u_list.set_defaults(func=cmd_user_list)

    p_u_approve = p_user_sub.add_parser("approve", help="Approve a user", parents=[common])
    p_u_approve.add_argument("--target-user-id", type=int, required=True, help="User ID to approve")
    p_u_approve.add_argument("--name", help="Display name")
    p_u_approve.add_argument("--role", choices=["user", "staff", "manager", "admin"], default="staff", help="User role")
    p_u_approve.set_defaults(func=cmd_user_approve)

    # Contact management via Userbot
    p_contact = subparsers.add_parser("contact", help="Manage Telegram contacts via Userbot", parents=[common])
    p_contact_sub = p_contact.add_subparsers(dest="subcommand", required=True)

    p_c_list = p_contact_sub.add_parser("list", help="List Telegram contacts", parents=[common])
    p_c_list.add_argument("--query", "-q", help="Search by name or username (e.g. 'Daivai')")
    p_c_list.add_argument("--limit", type=int, default=50, help="Max contacts to return")
    p_c_list.set_defaults(func=cmd_contact_list)

    p_c_send = p_contact_sub.add_parser("send", help="Send message directly to a Telegram contact", parents=[common])
    p_c_send.add_argument("--target", required=True, help="Contact name, username, or chat ID (e.g. 'Daivai')")
    p_c_send.add_argument("--message", required=True, help="Message text to send")
    p_c_send.set_defaults(func=cmd_contact_send)

    p_c_permit = p_contact_sub.add_parser("permit", help="Grant permission ('once' or 'always')", parents=[common])
    p_c_permit.add_argument("--target", type=int, required=True, help="Telegram user ID")
    p_c_permit.add_argument("--title", help="Contact name (e.g. 'Daivai')")
    p_c_permit.add_argument("--scope", choices=["once", "always"], default="once", help="Permission scope ('once' for only this time, 'always' for persistent)")
    p_c_permit.set_defaults(func=cmd_contact_permit)

    p_c_read = p_contact_sub.add_parser("read", help="Read recent messages with a contact", parents=[common])
    p_c_read.add_argument("--target", required=True, help="Contact name, username, or ID")
    p_c_read.add_argument("--limit", type=int, default=5, help="Number of messages to fetch")
    p_c_read.set_defaults(func=cmd_contact_read)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if hasattr(args, "func"):
        asyncio.run(args.func(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
