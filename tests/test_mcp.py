"""An actual SDK subprocess handshake, tool discovery and invocation."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPTests(unittest.TestCase):
    def test_sdk_initialize_list_call(self):
        async def exercise(directory):
            params = StdioServerParameters(command=sys.executable, args=['-m', 'work_copilot.mcp_server', '--data-dir', directory])
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.serverInfo.name, 'Work Copilot')
                    response = await session.list_tools()
                    names = {tool.name for tool in response.tools}
                    self.assertEqual(names, {'get_attention', 'get_project', 'get_reply_context', 'search_context', 'get_source', 'track_obligation', 'review_obligation', 'create_project', 'update_project', 'set_priorities'})
                    for tool in response.tools:
                        self.assertEqual(tool.annotations.readOnlyHint, tool.name not in {'track_obligation', 'review_obligation', 'create_project', 'update_project', 'set_priorities'})
                    result = await session.call_tool('get_attention', {})
                    self.assertFalse(result.isError)
                    data = json.loads(result.content[0].text)
                    self.assertIn('projects', data)
                    self.assertIn('obligations', data)
                    self.assertEqual(data['projects'][0]['id'], 'atlas')
                    context = await session.call_tool('get_reply_context', {'obligation_id': data['obligations'][0]['id']})
                    packet = json.loads(context.content[0].text)
                    self.assertEqual(packet['direct_evidence'][0]['revision'], '1')
                    self.assertIn('No external sends', packet['policy'])
                    unknown = await session.call_tool('get_reply_context', {'obligation_id': 'missing'})
                    self.assertTrue(unknown.isError)
        with TemporaryDirectory() as directory:
            from work_copilot.store import Store
            store = Store(Path(directory) / 'copilot.db')
            store.ingest([{'id':'example:message:1','revision':'1','provider':'example','title':'Fictional question','body':'Is staging ready?', 'projects':[{'id':'atlas','name':'Atlas'}], 'obligations':[{'intent_key':'staging','title':'Confirm staging','project_id':'atlas','owner':None}]}])
            store.close()
            asyncio.run(exercise(directory))

    def test_local_writes_capture_review_search_persist(self):
        async def exercise(directory, vault):
            params = StdioServerParameters(command=sys.executable, args=['-m', 'work_copilot.mcp_server', '--data-dir', directory], env={'WORK_COPILOT_VAULT': str(vault)})
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.assertIn('capture_note', {tool.name for tool in listed.tools})
                    created = await session.call_tool('create_project', {'id':'atlas', 'name':'Atlas', 'aliases':['AT'], 'rooms':[{'id':'room-shared','name':'Shared room'}]})
                    self.assertFalse(created.isError, created.content)
                    self.assertEqual(json.loads(created.content[0].text)['name'], 'Atlas')
                    config=await session.call_tool('get_project',{'project_id':'atlas'})
                    self.assertEqual(json.loads(config.content[0].text)['aliases'],['AT'])
                    capture = await session.call_tool('capture_note', {'title':'Meeting', 'body':'Decision: staging first. Risk: evidence missing.', 'project_id':'atlas', 'workstream':'engineering'})
                    self.assertFalse(capture.isError, capture.content)
                    receipt = json.loads(capture.content[0].text)
                    self.assertTrue(receipt['source_ids'])
                    tracked = await session.call_tool('track_obligation', {'payload': {'intent_key':'atlas-transcript','title':'Get Atlas transcript','project_id':'atlas','status':'waiting','waiting_on':'Alex','follow_up_at':'2026-10-27','evidence_ids':receipt['source_ids']}})
                    self.assertFalse(tracked.isError, tracked.content)
                    obligation = json.loads(tracked.content[0].text)
                    self.assertEqual(obligation['waiting_on'], 'Alex')
                    self.assertEqual(obligation['follow_up_at'], '2026-10-27')
                    self.assertIsNone(obligation.get('due_at'))
                    reviewed = await session.call_tool('review_obligation', {'obligation_id':obligation['id'], 'revision':obligation['revision'], 'changes': {'status':'resolved'}})
                    self.assertFalse(reviewed.isError, reviewed.content)
                    conflict = await session.call_tool('review_obligation', {'obligation_id':obligation['id'], 'revision':obligation['revision'], 'changes': {'status':'open'}})
                    self.assertTrue(conflict.isError)
                    found = await session.call_tool('search_context', {'query':'staging', 'provider':'vault'})
                    self.assertFalse(found.isError, found.content)
                    hits = json.loads(found.content[0].text)
                    self.assertTrue(hits)
                    self.assertIn('revision', hits[0])
                    return obligation['id']
        with TemporaryDirectory() as directory:
            vault = Path(directory) / 'vault'
            vault.mkdir()
            obligation_id = asyncio.run(exercise(directory, vault))
            from work_copilot.store import Store
            store = Store(Path(directory) / 'copilot.db')
            matched = next(o for o in store.snapshot()['obligations'] if o['id'] == obligation_id)
            self.assertEqual(matched['status'], 'resolved')
            store.close()

    def test_compact_paging_and_explicit_source_detail(self):
        async def exercise(directory):
            params=StdioServerParameters(command=sys.executable,args=['-m','work_copilot.mcp_server','--data-dir',directory])
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result=await session.call_tool('get_attention',{'limit':5})
                    packet=json.loads(result.content[0].text)
                    self.assertLess(len(result.content[0].text),12000)
                    self.assertEqual(packet['page']['total'],40)
                    self.assertEqual(packet['page']['next_cursor'],5)
                    self.assertNotIn('body',packet['obligations'][0])
                    source=await session.call_tool('get_source',{'source_id':'fictional:big','max_chars':1000})
                    detail=json.loads(source.content[0].text)
                    self.assertEqual(len(detail['body']),1000)
                    self.assertTrue(detail['truncated'])
                    self.assertEqual(detail['next_offset'],1000)
        with TemporaryDirectory() as directory:
            from work_copilot.store import Store
            store=Store(Path(directory)/'copilot.db')
            store.ingest([{'id':'fictional:big','revision':'1','provider':'fixture','title':'Large fixture','body':'context '*20000,'projects':[{'id':'atlas','name':'Atlas'}],'obligations':[{'intent_key':f'q-{i}','title':f'Question {i}','owner':'me','project_id':'atlas'} for i in range(40)]}])
            store.close()
            asyncio.run(exercise(directory))

    def test_large_urgency_list_is_paged_independently(self):
        async def exercise(directory):
            params=StdioServerParameters(command=sys.executable,args=['-m','work_copilot.mcp_server','--data-dir',directory])
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result=await session.call_tool('get_attention',{'limit':1})
                    self.assertFalse(result.isError,result.content)
                    packet=json.loads(result.content[0].text)
                    self.assertLess(len(result.content[0].text),12000)
                    self.assertGreater(packet['today']['urgency_page']['total'],800)
                    self.assertEqual(len(packet['today']['new_urgency']),1)
                    self.assertEqual(packet['today']['urgency_page']['next_cursor'],1)
        with TemporaryDirectory() as directory:
            from work_copilot.store import Store
            store=Store(Path(directory)/'copilot.db')
            store.ingest([{'id':'fixture:overdue','revision':'1','provider':'fixture','title':'Overdue fixture','body':'Fictional source','projects':[{'id':'atlas','name':'Atlas'}],'obligations':[{'intent_key':f'q-{i}','title':f'Action {i}','owner':'me','project_id':'atlas','due_at':'2020-01-01'} for i in range(900)]}])
            store.close();asyncio.run(exercise(directory))

    def test_source_identity_and_reply_evidence_continuation(self):
        async def exercise(directory, long_id, expected_ids):
            params=StdioServerParameters(command=sys.executable,args=['-m','work_copilot.mcp_server','--data-dir',directory])
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    hits=await session.call_tool('search_context',{'query':'IdentityToken'})
                    hit=json.loads(hits.content[0].text)[0]
                    self.assertEqual(hit['id'],long_id)
                    self.assertEqual(hit['revision'],'revision-'+('y'*350))
                    source=await session.call_tool('get_source',{'source_id':hit['id']})
                    self.assertFalse(source.isError,source.content)
                    attention=json.loads((await session.call_tool('get_attention',{})).content[0].text)
                    oid=attention['obligations'][0]['id']; seen=set(); cursor=0
                    while cursor is not None:
                        packet=json.loads((await session.call_tool('get_reply_context',{'obligation_id':oid,'direct_evidence_cursor':cursor})).content[0].text)
                        seen.update(s['id'] for s in packet['direct_evidence'])
                        cursor=packet['direct_evidence_page']['next_cursor']
                    self.assertTrue(set(expected_ids)<=seen)
        with TemporaryDirectory() as directory:
            from work_copilot.store import Store
            store=Store(Path(directory)/'copilot.db');store.create_project('atlas','Atlas')
            long_id='source:'+('x'*350)
            sources=[{'id':long_id if i==0 else f'source:{i}','revision':'revision-'+('y'*350),'provider':'fixture','title':str(i),'body':'IdentityToken' if i==0 else 'Evidence','projects':[],'obligations':[]} for i in range(12)]
            store.ingest(sources);store.track_obligation({'intent_key':'question','title':'Question','project_id':'atlas','evidence_ids':[s['id'] for s in sources]});store.close()
            asyncio.run(exercise(directory,long_id,[s['id'] for s in sources]))
