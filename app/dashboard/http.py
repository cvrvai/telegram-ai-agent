"""Small authenticated dashboard server with safe management endpoints."""

from __future__ import annotations

import asyncio
import html
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from app.dashboard.service import DashboardService
from app.storage.business import BusinessRepository
from app.usage.budget import UsageBudget


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, repository: BusinessRepository, budget: UsageBudget, host: str = "127.0.0.1", port: int = 3141, token: Optional[str] = None, schedules: list[str] | None = None, default_user_id: int | None = None, google_account: Optional[object] = None, loop: Optional[object] = None):
        self.dashboard_service = DashboardService(repository, budget, schedules, default_user_id)
        self.dashboard_token = token
        self.google_account = google_account
        # Motor/Telethon are bound to the bot's loop. This server answers on
        # its own threads, so coroutines must be scheduled back onto that loop
        # rather than run on a fresh one ("attached to a different loop").
        self.main_loop = loop
        super().__init__((host, port), self._handler())

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _authorized(self, query: dict[str, list[str]]) -> bool:
                if not server.dashboard_token:
                    return False
                supplied = self.headers.get("Authorization", "")
                bearer = supplied.removeprefix("Bearer ").strip() if supplied.startswith("Bearer ") else ""
                return bearer == server.dashboard_token or query.get("token", [""])[0] == server.dashboard_token

            def _send(self, status: int, content_type: str, body: str) -> None:
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _await(self, coro):
                """Run a coroutine on the bot's loop when there is one; fall back to a
                private loop for the standalone `main.py dashboard` command."""
                if server.main_loop is not None and not server.main_loop.is_closed():
                    return asyncio.run_coroutine_threadsafe(coro, server.main_loop).result(timeout=60)
                return asyncio.run(coro)

            def _json_body(self) -> dict:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 1_000_000:
                    raise ValueError("request too large")
                return json.loads(self.rfile.read(length) or b"{}")

            def _google_oauth_callback(self, query: dict[str, list[str]]) -> None:
                page = "<!doctype html><html><head><meta charset='utf-8'><title>Google connection</title></head><body style='font:16px system-ui;max-width:480px;margin:60px auto;text-align:center'>{}</body></html>"
                if server.google_account is None:
                    self._send(503, "text/html; charset=utf-8", page.format("<h1>Google integration is not configured.</h1>"))
                    return
                error = query.get("error", [""])[0]
                if error:
                    self._send(400, "text/html; charset=utf-8", page.format(f"<h1>Google connection cancelled</h1><p>{html.escape(error)}</p>"))
                    return
                code = query.get("code", [""])[0]
                state = query.get("state", [""])[0]
                try:
                    self._await(server.google_account.handle_callback(code, state))
                    self._send(200, "text/html; charset=utf-8", page.format("<h1>&#9989; Google account connected</h1><p>You can close this tab and return to Telegram.</p>"))
                except Exception as exc:
                    self._send(400, "text/html; charset=utf-8", page.format(f"<h1>Could not connect Google</h1><p>{html.escape(str(exc))}</p>"))

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path == "/healthz":
                    self._send(200, "application/json", '{"ok":true}')
                    return
                if parsed.path == "/oauth/google/callback":
                    # Google's redirect carries no dashboard bearer token; the
                    # one-time, single-use `state` value is the authorization
                    # here instead (see GoogleAccount.handle_callback).
                    self._google_oauth_callback(query)
                    return
                if not self._authorized(query):
                    self._send(401, "application/json", '{"error":"dashboard authentication required"}')
                    return
                period = datetime.now(timezone.utc).strftime("%Y-%m")
                snapshot = self._await(server.dashboard_service.snapshot(period))
                if parsed.path == "/api/status":
                    self._send(200, "application/json", json.dumps(snapshot))
                    return
                if parsed.path == "/api/users":
                    self._send(200, "application/json", json.dumps(self._await(server.dashboard_service.users())))
                    return
                if parsed.path == "/api/assistants":
                    self._send(200, "application/json", json.dumps(self._await(server.dashboard_service.assistants())))
                    return
                if parsed.path == "/api/groups":
                    self._send(200, "application/json", json.dumps(self._await(server.dashboard_service.groups())))
                    return
                if parsed.path == "/api/schedules":
                    self._send(200, "application/json", json.dumps({"schedules": server.dashboard_service.schedule_list()}))
                    return
                if parsed.path == "/api/permissions":
                    users = self._await(server.dashboard_service.users())
                    self._send(200, "application/json", json.dumps({"users": [{"user_id": u.get("user_id"), "approved": bool(u.get("approved")), "role": u.get("role", "user")} for u in users]}))
                    return
                if parsed.path == "/api/actions":
                    self._send(200, "application/json", json.dumps(self._await(server.dashboard_service.actions())))
                    return
                if parsed.path == "/api/tasks":
                    user_id = int(query["user_id"][0]) if query.get("user_id") else server.dashboard_service.default_user_id
                    self._send(200, "application/json", json.dumps(self._await(server.dashboard_service.tasks(user_id))))
                    return
                if parsed.path == "/api/departments":
                    self._send(200, "application/json", json.dumps(self._await(server.dashboard_service.departments())))
                    return
                if parsed.path == "/api/projects":
                    user_id = int(query["user_id"][0]) if query.get("user_id") else server.dashboard_service.default_user_id
                    if user_id is None:
                        self._send(400, "application/json", '{"error":"user_id is required"}')
                    else:
                        self._send(200, "application/json", json.dumps(self._await(server.dashboard_service.projects(user_id))))
                    return
                if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/plan"):
                    project_id = parsed.path.split("/")[3]
                    user_id = int(query["user_id"][0]) if query.get("user_id") else server.dashboard_service.default_user_id
                    if user_id is None:
                        self._send(400, "application/json", '{"error":"user_id is required"}')
                    else:
                        self._send(200, "application/json", json.dumps(self._await(server.dashboard_service.project_plan(user_id, project_id))))
                    return
                if parsed.path == "/webapp":
                    token = html.escape(query.get("token", [""])[0], quote=True)
                    body = """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Business Workspace</title>
                    <style>body{font:15px system-ui;margin:0;background:#0e1726;color:#eef4ff}main{max-width:760px;margin:auto;padding:18px}header{display:flex;justify-content:space-between;align-items:center}section{background:#18283a;border-radius:14px;padding:16px;margin:12px 0}h1{font-size:22px}h2{font-size:16px;margin-top:0}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.metric{background:#22364b;border-radius:10px;padding:12px}.metric b{display:block;font-size:21px}button{background:#2d77d0;color:white;border:0;border-radius:8px;padding:9px 12px;margin:3px}input,select{box-sizing:border-box;width:100%;margin:4px 0;padding:9px;border-radius:8px;border:1px solid #48617a;background:#102033;color:#eef4ff}ul{padding-left:20px}small{color:#a9bed5}.critical{color:#ffbd69;font-weight:600}</style><script src='https://telegram.org/js/telegram-web-app.js'></script></head><body><main><header><h1>📊 Business Workspace</h1><button onclick='loadAll()'>Refresh</button></header><section><h2>AI usage</h2><div id='status' class='grid'></div></section><section><h2>Create project</h2><input id='projectName' placeholder='Project name'><input id='projectDescription' placeholder='Description'><button onclick='createProject()'>Create project</button></section><section><h2>Create project task</h2><input id='taskProject' placeholder='Project ID'><input id='taskTitle' placeholder='Task title'><input id='taskDuration' type='number' min='1' value='1' placeholder='Duration in days'><input id='taskAssignee' placeholder='Assignee Telegram user ID (optional)'><select id='taskPriority'><option>normal</option><option>high</option><option>urgent</option><option>low</option></select><button onclick='createTask()'>Create task</button></section><section><h2>Projects and CPM plan</h2><ul id='projects'><li>Loading…</li></ul></section><section><h2>Open tasks</h2><ul id='tasks'><li>Loading…</li></ul></section><section><h2>People and assistants</h2><div id='people'><small>Loading…</small></div></section><script>
                    const token='""" + token + """'; if(window.Telegram?.WebApp){Telegram.WebApp.ready();Telegram.WebApp.expand();} const auth=path=>fetch(path+(path.includes('?')?'&':'?')+'token='+encodeURIComponent(token)).then(r=>r.json());
                    async function post(path,body){return fetch(path+'?token='+encodeURIComponent(token),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());} async function createProject(){const r=await post('/api/projects',{name:document.querySelector('#projectName').value,description:document.querySelector('#projectDescription').value}); if(!r.ok){alert(r.error||'Could not create project');return;} document.querySelector('#projectName').value='';loadAll();} async function createTask(){const r=await post('/api/tasks',{project_id:document.querySelector('#taskProject').value,title:document.querySelector('#taskTitle').value,duration_days:Number(document.querySelector('#taskDuration').value),assignee_id:document.querySelector('#taskAssignee').value?Number(document.querySelector('#taskAssignee').value):null,priority:document.querySelector('#taskPriority').value}); if(!r.ok){alert(r.error||'Could not create task');return;} document.querySelector('#taskTitle').value='';loadAll();} async function loadAll(){try{const s=await auth('/api/status');document.querySelector('#status').innerHTML=`<div class='metric'><small>Budget</small><b>$${s.budget_usd.toFixed(2)}</b></div><div class='metric'><small>Spent</small><b>$${s.spent_usd.toFixed(2)}</b></div><div class='metric'><small>Remaining</small><b>$${s.remaining_usd.toFixed(2)}</b></div><div class='metric'><small>Status</small><b>${s.budget_paused?'Paused':'Active'}</b></div>`;const [t,p]=await Promise.all([auth('/api/tasks'),auth('/api/projects')]);document.querySelector('#tasks').innerHTML=t.length?t.map(x=>`<li><b>#${x.id}</b> ${x.title} · ${x.status}${x.priority?' · '+x.priority:''}${x.due_at?' · '+x.due_at:''}</li>`).join(''):'<li>No tasks for this user yet.</li>';document.querySelector('#projects').innerHTML=p.length?p.map(x=>`<li><b>#${x.id}</b> ${x.name} · ${x.status} <button onclick='showPlan(${JSON.stringify(x.id)})'>CPM plan</button></li>`).join(''):'<li>No projects yet. Create one with /project Name.</li>';const [u,a]=await Promise.all([auth('/api/users'),auth('/api/assistants')]);document.querySelector('#people').innerHTML=`<p>👥 ${u.length} users · 🤖 ${a.length} assistants</p>`; }catch(e){document.querySelector('#status').innerHTML='<p>Could not load workspace data.</p>';}} async function showPlan(id){const p=await auth('/api/projects/'+encodeURIComponent(id)+'/plan');alert('Duration: '+p.duration_days+' days\nCompletion: '+p.completion_percent+'%\nCritical tasks: '+p.critical_task_ids.join(', '));} loadAll();</script></main></body></html>"""
                    self._send(200, "text/html; charset=utf-8", body)
                    return
                body = """<!doctype html><html><head><meta charset='utf-8'><title>Business Assistant</title>
                <style>body{font:16px system-ui;max-width:760px;margin:40px auto;padding:0 20px;background:#f6f7f9}main{background:#fff;padding:24px;border-radius:12px}dt{color:#555;margin-top:12px}dd{font-size:1.4rem;margin:2px 0}</style></head>
                <body><main><h1>Business Assistant</h1><p>Current AI usage and connection snapshot</p>
                <dl><dt>Budget</dt><dd>${budget:.2f}</dd><dt>Spent</dt><dd>${spent:.2f}</dd><dt>Remaining</dt><dd>${remaining:.2f}</dd><dt>Status</dt><dd>{status}</dd></dl>
                <p>Usage is shared across all assistants and features.</p></main></body></html>""".format(
                    budget=float(snapshot["budget_usd"]),
                    spent=float(snapshot["spent_usd"]),
                    remaining=float(snapshot["remaining_usd"]),
                    status="Paused" if snapshot["budget_paused"] else "Active",
                )
                self._send(200, "text/html; charset=utf-8", body)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if not self._authorized(parse_qs(parsed.query)):
                    self._send(401, "application/json", '{"error":"dashboard authentication required"}')
                    return
                try:
                    data = self._json_body()
                    if parsed.path.startswith("/api/users/") and parsed.path.endswith("/approval"):
                        user_id = int(parsed.path.split("/")[3])
                        self._await(server.dashboard_service.approve_user(user_id, bool(data.get("approved"))))
                        self._send(200, "application/json", '{"ok":true}')
                        return
                    if parsed.path == "/api/assistants":
                        required = {"assistant_id", "name"}
                        if not required.issubset(data):
                            raise ValueError("assistant_id and name are required")
                        self._await(server.dashboard_service.save_assistant(str(data["assistant_id"]), str(data["name"]), str(data.get("instructions", "")), bool(data.get("enabled", True))))
                        self._send(200, "application/json", '{"ok":true}')
                        return
                    owner_id = server.dashboard_service.default_user_id
                    if owner_id is None:
                        raise ValueError("dashboard owner is not configured")
                    if parsed.path == "/api/projects":
                        project_id = self._await(server.dashboard_service.create_project(owner_id, data))
                        self._send(200, "application/json", json.dumps({"ok": True, "id": project_id}))
                        return
                    if parsed.path == "/api/tasks":
                        task_id = self._await(server.dashboard_service.create_task(owner_id, data))
                        self._send(200, "application/json", json.dumps({"ok": True, "id": task_id}))
                        return
                    if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/dependency"):
                        task_id = parsed.path.split("/")[3]
                        self._await(server.dashboard_service.add_dependency(task_id, data["predecessor_id"]))
                        self._send(200, "application/json", '{"ok":true}')
                        return
                    if parsed.path.startswith("/api/tasks/"):
                        task_id = parsed.path.split("/")[3]
                        updated = self._await(server.dashboard_service.update_task(owner_id, task_id, data))
                        self._send(200, "application/json", json.dumps({"ok": updated}))
                        return
                    self._send(404, "application/json", '{"error":"unknown endpoint"}')
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send(400, "application/json", json.dumps({"error": str(exc)}))

        return Handler
