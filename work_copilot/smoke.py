"""Synthetic, temporary-only installation check; never connects work systems."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from .store import Store
from .vault import capture_vault, export_projects, write_inbox_note
from .hooks import enqueue_activity, drain_activity


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


async def _check_mcp(data: Path, vault: Path, source_id: str):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=['-m', 'work_copilot.mcp_server', '--data-dir', str(data)],
        # Never inherit a user-selected vault into this synthetic child process.
        env={'WORK_COPILOT_VAULT': str(vault), 'WORK_COPILOT_TIMEZONE': 'UTC',
             'WORK_COPILOT_HOOK_ACTIVE': '0', 'HOME': str(data),
             'USERPROFILE': str(data), 'XDG_DATA_HOME': str(data)},
    )
    with anyio.fail_after(20):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                _require(initialized.serverInfo.name == 'Work Copilot', 'Unexpected MCP server identity')
                listed = await session.list_tools()
                _require({'get_attention', 'get_source', 'track_obligation'} <= {t.name for t in listed.tools}, 'Required MCP tools missing')
                response = await session.call_tool('get_attention', {})
                _require(not response.isError, 'MCP attention tool returned an error')
                attention = json.loads(response.content[0].text)
                _require(any(p['id'] == 'smoke-atlas' for p in attention['projects']), 'MCP did not read the shared database')
                response = await session.call_tool('get_source', {'source_id': source_id})
                _require(not response.isError and 'staging' in json.loads(response.content[0].text)['body'], 'MCP passage retrieval failed')


def _activity_child(data):
    """Isolate the hook recursion guard from the caller's environment."""
    data = Path(data)
    store = Store(data/'copilot.db', timezone_name='UTC')
    try:
        queued = enqueue_activity(data, {'session_id':'smoke-session','turn_id':'smoke-turn','event_id':'smoke-event','project_id':'smoke-atlas','summary':'Fictional local activity receipt','outcomes':['Patch prepared; not deployment evidence'],'evidence_urls':['https://example.invalid/smoke']})
        return {'queued': queued, 'drained': drain_activity(store, data)}
    finally:
        store.close()


def _check_activity(data):
    environment = {key: os.environ[key] for key in ('PATH','SYSTEMROOT','WINDIR') if key in os.environ}
    environment.update({'WORK_COPILOT_HOOK_ACTIVE':'0', 'WORK_COPILOT_TIMEZONE':'UTC',
                        'HOME':str(data), 'USERPROFILE':str(data), 'XDG_DATA_HOME':str(data)})
    completed = subprocess.run(
        [sys.executable, '-c', 'import json,sys; from work_copilot.smoke import _activity_child; print(json.dumps(_activity_child(sys.argv[1])))', str(data)],
        env=environment, capture_output=True, text=True, timeout=20, check=True,
    )
    return json.loads(completed.stdout)


def run_smoke() -> dict:
    """Return phase results; all state/files live in a deleted TemporaryDirectory.

    No caller paths/configuration are accepted. No HTTP listener, credentials,
    external model or network operation is used. The MCP subprocess is local.
    """
    result = {'success': False, 'synthetic_only': True, 'checks': []}
    phase = 'fresh_store'
    store = None

    def passed(detail):
        result['checks'].append({'name': phase, 'success': True, 'detail': detail})

    try:
        with TemporaryDirectory(prefix='work-copilot-smoke-') as temporary:
            root = Path(temporary)
            data, vault = root/'data', root/'vault'
            data.mkdir(); vault.mkdir()
            try:
                store = Store(data/'copilot.db', timezone_name='UTC')
                _require(not store.list_sources(), 'Fresh temporary store is not empty')
                passed('Created an empty temporary SQLite store.')

                phase = 'projects_and_rooms'
                shared = {'id':'smoke-shared-room', 'name':'Fictional shared room'}
                store.create_project('smoke-atlas', 'Smoke Atlas', aliases=['Smoke A'], rooms=[shared, {'id':'smoke-engineering','name':'Fictional engineering room'}])
                store.create_project('smoke-beacon', 'Smoke Beacon', rooms=[shared])
                _require(any(r['id'] == shared['id'] for r in store.project('smoke-atlas')['rooms']), 'First project lost shared room')
                _require(any(r['id'] == shared['id'] for r in store.project('smoke-beacon')['rooms']), 'Second project lost shared room')
                passed('Two projects share a room; one project has multiple rooms.')

                phase = 'capture_and_preservation'
                handwritten = vault/'Handwritten.md'
                original = '# Fictional daily note\nUnassigned awareness: no owner has been agreed.\n'
                handwritten.write_text(original, encoding='utf-8')
                capture_vault(store, vault)
                _require(store.snapshot()['review'], 'Unassigned note did not create review candidate')
                receipt = write_inbox_note(store, vault, 'Fictional transcript', 'Decision: validate staging first. Risk: deployment evidence is missing.', project_ids=['smoke-atlas','smoke-beacon'], workstream='engineering')
                source_id = receipt['source_ids'][0]
                _require(handwritten.read_text(encoding='utf-8') == original, 'Capture changed a handwritten note')
                _require(any(s['id'] == source_id for s in store.project('smoke-beacon')['sources']), 'Shared capture is not linked to both projects')
                passed('Captured context with explicit projects; preserved original and unresolved ownership.')

                phase = 'waiting_followup_search_draft'
                tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
                obligation = store.track_obligation({'intent_key':'smoke-staging-question','title':'Await fictional staging evidence','project_id':'smoke-atlas','status':'waiting','owner':'me','waiting_on':'Fictional Alex','follow_up_at':tomorrow,'evidence_ids':[source_id]})
                _require(obligation['follow_up_at'] == tomorrow and not obligation.get('due_at'), 'Follow-up became a promised deadline')
                _require(store.search('staging', project_id='smoke-atlas', provider='vault', workstream='engineering'), 'Filtered evidence search returned no result')
                draft = store.draft(obligation['id'])
                _require(draft['mode'] == 'offline_template' and source_id in draft['source_revisions'], 'Draft lacks offline evidence grounding')
                _require(not draft['stale'], 'Fresh fictional evidence produced a stale draft')
                passed('Tracked waiting/follow-up separately from deadline; searched evidence and prepared an offline draft.')

                phase = 'evidence_edit_invalidates_draft'
                captured = Path(receipt['path'])
                captured.write_text(captured.read_text(encoding='utf-8') + '\nUpdated decision: staging evidence still awaited.\n', encoding='utf-8')
                capture_vault(store, vault)
                _require(next(d for d in store.snapshot()['drafts'] if d['id'] == draft['id'])['stale'], 'Edited evidence did not stale the draft')
                passed('Source edit kept identity and invalidated the old draft.')

                phase = 'activity_queue'
                activity = _check_activity(data)
                _require(activity['queued'].get('queued'), 'Activity enqueue was skipped')
                drained = activity['drained']
                _require(drained['processed'] == 1 and not drained['errors'], 'Activity queue did not drain')
                _require(store.snapshot()['refresh_needed']['needed'], 'Local refresh suggestion is missing')
                _require(store.list_sources(provider='local-activity')[0]['verification'] == 'claimed', 'Activity was promoted to verified state')
                passed('Drained a durable local receipt as an unverified claim; no network refresh ran.')

                phase = 'wiki_export'
                exported = export_projects(store, vault)
                projection = Path(exported['directory'])
                _require((projection/'README.md').is_file() and (projection/'Review.md').is_file(), 'Wiki index or review bucket missing')
                pages = [p for p in projection.glob('Project-*.md')]
                _require(any('staging' in p.read_text(encoding='utf-8') for p in pages), 'Project wiki omitted source context')
                _require(handwritten.read_text(encoding='utf-8') == original, 'Export modified handwritten original')
                passed('Exported separate project wiki and review pages without modifying originals.')

                phase = 'restart_and_dedupe'
                count = len(store.list_sources())
                store.close(); store = None
                store = Store(data/'copilot.db', timezone_name='UTC')
                capture_vault(store, vault)
                _require(len(store.list_sources()) == count, 'Restart/re-capture duplicated sources or ingested generated files')
                restored = next(o for o in store.snapshot()['obligations'] if o['id'] == obligation['id'])
                _require(restored['waiting_on'] == 'Fictional Alex' and restored['follow_up_at'] == tomorrow, 'Waiting record did not persist')
                _require(handwritten.read_text(encoding='utf-8') == original, 'Restart altered original')
                passed('Reopened SQLite and replayed capture without duplicate sources or lost follow-up.')

                phase = 'mcp_stdio'
                store.close(); store = None
                asyncio.run(_check_mcp(data, vault, source_id))
                passed('Official SDK initialized a local stdio server and read the shared evidence packet.')
            finally:
                if store is not None:
                    store.close()
                    store = None
        result['success'] = True
    except Exception as exc:
        result['checks'].append({'name': phase, 'success': False, 'detail': f'{type(exc).__name__}: {exc}'})
    return result
