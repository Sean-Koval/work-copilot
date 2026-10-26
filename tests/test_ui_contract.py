"""Static contract checks; browser integration is exercised separately."""
from pathlib import Path

ROOT = Path(__file__).parents[1] / 'work_copilot' / 'static'


def test_dashboard_has_accessible_local_shell():
    html = (ROOT / 'index.html').read_text()
    assert 'aria-live="polite"' in html
    assert 'id="main"' in html
    assert '/static/app.js' in html
    assert 'Content-Security' not in html  # response headers own policy


def test_untrusted_data_never_uses_html_sinks():
    js = (ROOT / 'app.js').read_text()
    for unsafe in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'eval('):
        assert unsafe not in js
    assert 'textContent' in js
    assert "['http:', 'https:']" in js
    assert 'X-Copilot-Token' in js
    assert 'response.status === 409' in js


def test_ui_separates_review_from_send_and_has_freshness():
    js = (ROOT / 'app.js').read_text()
    for phrase in ('Nothing is sent', 'This week', 'New urgency', 'Evidence', 'Offline template', 'Copy reply', 'suggestions', 'question_state'):
        assert phrase in js
    assert '/send' not in js


def test_search_and_manual_followup_are_explicit():
    js = (ROOT / 'app.js').read_text()
    for text in ('/api/search?', 'Ranked lexical', 'follow_up_at', 'Track waiting / action', '/api/obligations'):
        assert text in js
    assert 'name="search-query"' not in js  # DOM construction, not HTML templates


def test_empty_workspace_has_project_onboarding():
    js = (ROOT / 'app.js').read_text()
    assert 'Create project' in js
    assert "request('/api/projects','POST'" in js
    assert 'First-run checklist' in js
    assert 'aliases:' in js


def test_attention_pinning_and_week_overdue_contract():
    js = (ROOT / 'app.js').read_text()
    assert 'NEEDS YOUR ATTENTION' in js
    assert 'NEEDS YOUR RESPONSE' not in js
    assert "request('/api/priorities','POST'" in js
    assert 'const revision = state.today.revision' in js
    assert '{ids,revision}' in js
    assert 'w.overdue' in js
    assert "item.owner === 'me' && item.status === 'open'" in js


def test_track_distinguishes_reminder_from_promise():
    js = (ROOT / 'app.js').read_text()
    for phrase in ('waiting_on:', 'due_at:', 'commitment:commitment.input.checked', 'Promised due date', 'Reminder, not a promise'):
        assert phrase in js


def test_focus_eligibility_executes_for_mixed_statuses():
    import json
    import re
    import shutil
    import subprocess
    import pytest
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is optional; run node --check and UI behavior checks in frontend QA')
    js = (ROOT / 'app.js').read_text()
    helper = re.search(r'function focusEligible\(items\) \{[^\n]+\}', js).group()
    items = [{'id': status, 'owner': 'me', 'status': status} for status in ['open','waiting','resolved','dismissed','candidate']]
    items += [{'id':'other','owner':'person:alex','status':'open'}]
    result = subprocess.run([node, '-e', helper + ';process.stdout.write(JSON.stringify(focusEligible(' + json.dumps(items) + ')));'], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [{'id':'open','owner':'me','status':'open'}]


def test_project_edit_preserves_config_revision_and_explicit_room_bindings():
    js = (ROOT / 'app.js').read_text()
    for text in ('Edit project', 'revision:project.config_revision', 'configured_rooms', "'PATCH'", 'Exact room IDs route incoming sources'):
        assert text in js
