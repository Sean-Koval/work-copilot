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
