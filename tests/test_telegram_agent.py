"""Behavior regressions for source isolation, Telegram consent and cancellation."""
import asyncio
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx

from app.agent.context import AgentContext
from app.agent.runtime import ProviderDecisionPlanner
from app.agent.state import now
from app.agent.tools import build_registry
from app.ai.provider import OpenAICompatibleProvider, StubProvider
from app.security.access import AccessController
from app.telegram.conversation import TelegramConversation, chunks
from app.telegram.sources import InteractionRequired, SourceInput, TelegramSources, window


class State:
    """In-memory contract fake. New coordinators reuse it to simulate recovery."""
    def __init__(self):
        self.rows, self.sessions, self.grants = {}, {}, set()

    async def create(self, actor, chat, kind, text, event_key):
        if any(r['event_key'] == event_key for r in self.rows.values()):
            return None
        key = str(len(self.rows) + 1)
        row = dict(_id=key, actor_id=actor, chat_id=chat, chat_type=kind, request=text,
                   event_key=event_key, state='queued', expires_at=now()+timedelta(minutes=30))
        self.rows[key] = row
        return copy.deepcopy(row)

    async def get(self, key):
        return copy.deepcopy(self.rows.get(key))

    async def transition(self, record, expected, state, **fields):
        row = self.rows[record['_id']]
        if row['state'] not in expected:
            return None
        row.update(state=state, **copy.deepcopy(fields))
        return copy.deepcopy(row)

    async def pending(self, actor, chat):
        return next((copy.deepcopy(row) for row in reversed(list(self.rows.values())) if row['actor_id']==actor and row['chat_id']==chat and row['state'] in {'selecting','awaiting_approval'} and row['expires_at']>now()),None)

    async def cancel(self, actor, chat):
        count=0
        for row in self.rows.values():
            if row['actor_id']==actor and row['chat_id']==chat and row['state'] in {'queued','running','selecting','awaiting_approval'}:
                row['state']='cancelled'
                count+=1
        return count

    async def load_session(self, actor, chat):
        return copy.deepcopy(self.sessions.get((actor,chat),{}))

    async def save_session(self, actor, chat, context):
        self.sessions[actor,chat]={k:v for k,v in context.items() if not k.startswith('_')}

    async def has_grant(self, actor, source):
        return (actor,source['account_id'],source['id']) in self.grants

    async def grant(self, actor, source):
        self.grants.add((actor,source['account_id'],source['id']))

    async def list_grants(self, actor):
        return []


class Client:
    def __init__(self):
        self.account=42
        self.dialogs=[]
        self.messages=[]
        self.reads=[]
        self.after_message=None

    async def get_me(self):
        return NS(id=self.account)

    async def iter_dialogs(self, limit=None):
        for row in self.dialogs:
            yield row

    async def iter_messages(self, peer, **kwargs):
        self.reads.append((peer,kwargs))
        for message in self.messages[:kwargs['limit']]:
            yield message
            if self.after_message:
                self.after_message()


class Event:
    def __init__(self, text='', actor=42, chat=42, data=None, event_id=1):
        self.raw_text,self.sender_id,self.chat_id,self.data,self.id=text,actor,chat,data,event_id
        self.is_private=chat>0
        self.sent=[]

    async def respond(self,text,**kwargs):
        self.sent.append(text)
        return self

    async def edit(self,text,**kwargs):
        self.sent.append(text)

    async def answer(self,text='',**kwargs):
        self.sent.append(text)


class TelegramAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store=State()
        self.client=Client()
        self.selected=set()
        self.access=AccessController(owner_id=42, approved_users={7}, approved_groups={-100123})
        self.sources=TelegramSources(lambda:self.client,42,self.store,lambda:self.selected,self.access)

    async def context(self):
        row=await self.store.create(42,42,'private','summarize saved messages','test')
        row=await self.store.transition(row,['queued'],'running')
        return AgentContext(42,42,'private',session={'_run_id':row['_id']}),row

    async def test_tool_result_reaches_planner_without_losing_fields(self):
        class Service:
            async def list_projects(self,*args):
                return [{'id':'p1','name':'Actual project'}]
        result=await build_registry().execute('list_projects', {'limit':10},NS(service=Service(),actor_id=42,chat_id=42,chat_type='private'))
        self.assertEqual(result.data['projects'][0]['name'],'Actual project')

    def test_followup_context_and_tool_specs(self):
        context=AgentContext(42,42,'private',recent_turns=[{'role':'user','content':'Daivai this month'}])
        self.assertIn('Daivai this month','\n'.join(context.prompt_lines()))
        specs=build_registry(telegram=True).specifications()
        tool=next(t for t in specs if t['function']['name']=='read_telegram_chat')
        self.assertIn('period',tool['function']['parameters']['properties'])
        self.assertNotIn('change_priority',build_registry(telegram=True).names())

    async def test_invalid_decision_does_not_leak_json(self):
        planner=ProviderDecisionPlanner(StubProvider('{"tool_name":"oops", "arguments": {}}'))
        result=await planner.decide('hi',AgentContext(42,42,'private'),(),[])
        self.assertNotIn('tool_name',result.message)

    async def test_directory_includes_saved_and_more_than_100_and_duplicates(self):
        self.client.dialogs=[NS(id=-i,name='AIC',is_user=False,is_group=True,is_channel=True,entity=NS(username=None)) for i in range(1,125)]
        rows=await self.sources.directory()
        self.assertEqual(len(rows),125)
        self.assertEqual(rows[0]['title'],'Saved Messages')
        self.assertEqual(len(self.sources.matches(rows,'AIC','group')),124)
        self.assertEqual(self.sources.matches(rows,'save')[0]['id'],42)

    async def test_no_read_before_consent_and_member_cannot_read_owner_sources(self):
        context,row=await self.context()
        with self.assertRaises(InteractionRequired) as caught:
            await self.sources.request(SourceInput(hint='Saved Messages'),context)
        self.assertEqual(caught.exception.state,'awaiting_approval')
        self.assertEqual(self.client.reads,[])
        context.actor_id=7
        with self.assertRaises(PermissionError):
            await self.sources.request(SourceInput(hint='Saved Messages'),context)
        context.actor_id=42
        context.chat_type='group'
        with self.assertRaises(PermissionError):
            await self.sources.request(SourceInput(hint='Saved Messages'),context)
        self.assertEqual(self.client.reads,[])

    async def test_preapproved_source_and_monthly_two_way_history(self):
        context,row=await self.context()
        self.selected.add(42)
        start=datetime(2026,9,1,tzinfo=timezone(timedelta(hours=7)))
        self.client.messages=[NS(id=i,date=start+timedelta(hours=i),message=f'message {i}',sender_id=42 if i%2 else 10,out=bool(i%2)) for i in range(80,0,-1)]
        self.client.messages.append(NS(id=0,date=start-timedelta(seconds=1),message='outside',out=False))
        source=(await self.sources.directory())[0]
        data=await self.sources.read(source,{'start':start.isoformat(),'end':(start+timedelta(days=4)).isoformat(),'timezone':'Asia/Phnom_Penh'},context,row)
        self.assertEqual(data['messages_analyzed'],80)
        self.assertFalse(data['partial'])
        self.assertEqual({r['direction'] for r in data['messages']},{'incoming','outgoing'})
        self.assertNotIn('outside',str(data))
        self.assertEqual(self.client.reads[0][0],42)

    async def test_revocation_interrupts_history(self):
        context,row=await self.context()
        source=(await self.sources.directory())[0]
        await self.store.grant(42,source)
        self.client.messages=[NS(id=i,date=now()-timedelta(minutes=i),message='text',out=False) for i in range(1,5)]
        self.client.after_message=lambda:self.store.grants.clear()
        with self.assertRaises(PermissionError):
            await self.sources.read(source,window(SourceInput()),context,row)

    async def test_changed_account_cannot_use_previous_grant(self):
        context,row=await self.context()
        source=(await self.sources.directory())[0]
        self.selected.add(42)
        self.client.account=99
        with self.assertRaises(PermissionError):
            await self.sources.read(source,window(SourceInput()),context,row)
        self.assertFalse(self.client.reads)

    def test_month_window_uses_local_timezone(self):
        dates=window(SourceInput(period='this_month'),datetime(2026,9,1,0,0,tzinfo=timezone.utc))
        self.assertEqual(dates['start'],'2026-09-01T00:00:00+07:00')
        dates=window(SourceInput(period='last_month'),datetime(2026,9,1,tzinfo=timezone.utc))
        self.assertEqual(dates['start'],'2026-08-01T00:00:00+07:00')
        with self.assertRaises(ValueError):
            window(SourceInput(period='custom',start_date='2026-09-05',end_date='2026-09-01'))

    async def test_picker_resumes_original_request_after_restart_and_approval(self):
        source=(await self.sources.directory())[0]
        row=await self.store.create(42,42,'private','Summarize Saved Messages this month','event')
        payload={'args':SourceInput(hint='Saved Messages',period='this_month').model_dump(),'window':window(SourceInput(period='this_month')),'directory':[source],'candidates':[source]}
        row=await self.store.transition(row,['queued'],'selecting',payload=payload)
        calls=[]
        async def handle(*args,**kwargs):
            calls.append((args,kwargs))
            return 'Summary from selected chat'
        runtime=NS(service=NS(access=self.access),handle_turn=handle)
        gateway=TelegramConversation(runtime,self.sources,self.store)
        event=Event(data=f"agent:{row['_id']}:pick:42".encode())
        await gateway.callback(event)
        self.assertEqual((await self.store.get(row['_id']))['state'],'awaiting_approval')
        self.assertFalse(calls)
        self.assertFalse(self.client.reads)
        # Construct a fresh coordinator: approval survives process-local state loss.
        gateway=TelegramConversation(runtime,self.sources,self.store)
        approval=Event(data=f"agent:{row['_id']}:once".encode())
        await gateway.callback(approval)
        self.assertEqual(calls[0][0][3],'Summarize Saved Messages this month')
        self.assertEqual(calls[0][1]['initial_results'][0]['result']['data']['source']['id'],42)
        self.assertFalse(self.store.grants)
        await gateway.callback(approval)
        self.assertEqual(len(calls),1)

    async def test_stale_and_foreign_buttons_never_approve_new_request(self):
        old=await self.store.create(42,42,'private','old','old')
        await self.store.transition(old,['queued'],'cancelled')
        new=await self.store.create(42,42,'private','new','new')
        runtime=NS(service=NS(access=self.access))
        gateway=TelegramConversation(runtime,self.sources,self.store)
        await gateway.callback(Event(data=f"agent:{old['_id']}:once".encode()))
        await gateway.callback(Event(actor=7,data=f"agent:{new['_id']}:once".encode()))
        self.assertEqual((await self.store.get(new['_id']))['state'],'queued')
        self.assertFalse(self.client.reads)

    async def test_cancel_in_flight_prevents_late_reply(self):
        entered=asyncio.Event()
        async def handle(*args,**kwargs):
            entered.set()
            await asyncio.Event().wait()
            return 'LATE RESULT'
        gateway=TelegramConversation(NS(service=NS(access=self.access),handle_turn=handle),self.sources,self.store)
        event=Event('summarize')
        task=asyncio.create_task(gateway.handle(event,'summarize'))
        await entered.wait()
        self.assertTrue(await gateway.controls(Event('stop',event_id=2)))
        await task
        self.assertNotIn('LATE RESULT',event.sent)
        self.assertEqual(self.store.rows['1']['state'],'cancelled')
        self.assertFalse(gateway.tasks[42,42])

    def test_telegram_chunks_respect_utf16_limits(self):
        parts=list(chunks('😀'*4000))
        self.assertEqual(''.join(parts),'😀'*4000)
        self.assertTrue(all(len(part.encode('utf-16-le'))//2 <=3500 for part in parts))

    async def test_native_tool_contract_and_evidence_transcript(self):
        bodies=[]
        def respond(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'message':{'tool_calls':[{'function':{'name':'read_telegram_chat','arguments':'{"hint":"Saved Messages"}'}}]}}]})
        original=httpx.AsyncClient
        def client(**kwargs):
            return original(transport=httpx.MockTransport(respond),**kwargs)
        with patch('app.ai.provider.httpx.AsyncClient',side_effect=client):
            response=await OpenAICompatibleProvider('https://example.invalid/v1','test','test').decide('Summarize',[],build_registry(telegram=True).specifications(),[{'tool':'list_projects','result':{'data':{'projects':[{'name':'Actual data'}]}}}])
        self.assertEqual(json.loads(response.text)['tool_name'],'read_telegram_chat')
        self.assertTrue(bodies[0]['tools'])
        self.assertIn('Actual data',bodies[0]['messages'][-1]['content'])
        self.assertEqual(bodies[0]['messages'][-1]['role'],'tool')


if __name__=='__main__':
    unittest.main()
