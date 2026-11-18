"""The ai-dlc session join key on activity receipts and tracked obligations.

ai-dlc writes the key down in ``docs/design/session-join-key.md`` (KAN-285,
ADR 0002): ``aidlc1:<tracker>:<work>:<session>``, each part ``[A-Za-z0-9._-]{0,100}``.
A session started in an adopted repository has it in ``AI_DLC_SESSION_KEY``, with
the parts in ``AI_DLC_TRACKER_KEY``, ``AI_DLC_WORK_ID`` and ``AI_DLC_SESSION_ID``.

Work Copilot stores three fields, named as ai-dlc's session log names them:
``tracker`` (the ticket key, such as ``KAN-463``), ``work_id`` and ``session_key``.
Flightdeck joins a receipt to its trace by ``session_key``. Nothing here is
verified upstream: the fields record what the session said it was working on.
"""
from __future__ import annotations

import re

PART = re.compile(r'[A-Za-z0-9._-]{1,100}')
KEY = re.compile(r'aidlc1:([A-Za-z0-9._-]{0,100}):([A-Za-z0-9._-]{0,100}):([A-Za-z0-9._-]{0,100})')
# A Jira-style ticket key: only these become a ``belongs_to`` link on export.
TICKET = re.compile(r'[A-Z][A-Z0-9_]*-[0-9]+')
# ai-dlc's reader finds a key on any line like this (``read_session_keys``). A line
# starts after any break ``str.splitlines`` knows, since an excerpt is rebuilt from those.
TRAILER_LINE = re.compile(r'(\A|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029])(\s*)(Work-Session:)')
FIELDS = ('tracker', 'work_id', 'session_key')
ENV = {'session_key': 'AI_DLC_SESSION_KEY', 'tracker': 'AI_DLC_TRACKER_KEY', 'work_id': 'AI_DLC_WORK_ID'}
ENV_SESSION_ID = 'AI_DLC_SESSION_ID'


def neutralise(text):
    """``text`` with every ``Work-Session:`` line quoted, so a reader takes no key from it.

    Free text (a summary, a claim, a note) must never carry a join key: only the
    line Work Copilot writes from the validated fields does.
    """
    return TRAILER_LINE.sub(r'\1\2> \3', text) if isinstance(text, str) else text


def _env_text(env, name):
    value = env.get(name, '')
    return value if isinstance(value, str) else ''


def _from_env(env):
    """The fields a session's environment gives; never raises.

    A key that parses wins whole: its tracker and work parts are taken from it and
    the component variables are ignored, so they can never disagree with it.
    Without one, valid components build a key. A malformed value is skipped, as
    ai-dlc's readers do.
    """
    key = KEY.fullmatch(_env_text(env, ENV['session_key']))
    if key:
        tracker, work, _session = key.groups()
        return {field: value for field, value in
                (('tracker', tracker), ('work_id', work), ('session_key', key.group(0))) if value}
    found = {field: _env_text(env, ENV[field]) for field in ('tracker', 'work_id')
             if PART.fullmatch(_env_text(env, ENV[field]))}
    if not found:
        return {}
    session = _env_text(env, ENV_SESSION_ID)
    found['session_key'] = 'aidlc1:{}:{}:{}'.format(found.get('tracker', ''), found.get('work_id', ''),
                                                    session if PART.fullmatch(session) else '')
    return found


def join_fields(values, env=None, session_id=None):
    """Validated ``tracker``, ``work_id`` and ``session_key`` from ``values``.

    An empty string counts as absent. When ``values`` gives none of them and
    ``env`` is given, all come from ``env`` (values are never mixed with a
    session's environment). A missing ``session_key`` is built from the other two;
    its session part is ``session_id`` (an activity event's own) when that is a
    valid part, else the session part of ``env``'s key when that key has the same
    tracker and work parts, else empty. Missing parts are read from the key. Only
    fields with a value are returned.

    Raises ``ValueError`` for a malformed value in ``values`` or parts that
    disagree with its key; ``env`` never raises.
    """
    given = {}
    for field in FIELDS:
        value = values.get(field)
        if value is None or value == '':
            continue
        pattern = KEY if field == 'session_key' else PART
        if not isinstance(value, str) or not pattern.fullmatch(value):
            what = 'aidlc1:<tracker>:<work>:<session>' if field == 'session_key' else 'letters, digits, dot, dash or underscore (up to 100)'
            raise ValueError(f'{field} must be {what}')
        given[field] = value
    if not given:
        return _from_env(env) if env is not None else {}
    if 'session_key' in given:
        tracker, work, _session = KEY.fullmatch(given['session_key']).groups()
        for field, part in (('tracker', tracker), ('work_id', work)):
            if given.setdefault(field, part) != part:
                raise ValueError(f'{field} does not match the session key')
    else:
        tracker, work = given.get('tracker', ''), given.get('work_id', '')
        session = session_id if isinstance(session_id, str) and PART.fullmatch(session_id) else ''
        if not session and env is not None:
            key = KEY.fullmatch(_env_text(env, ENV['session_key']))
            if key and key.group(1) == tracker and key.group(2) == work:
                session = key.group(3)
        given['session_key'] = f'aidlc1:{tracker}:{work}:{session}'
    return {field: given[field] for field in FIELDS if given.get(field)}
