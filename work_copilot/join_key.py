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
FIELDS = ('tracker', 'work_id', 'session_key')
ENV = {'session_key': 'AI_DLC_SESSION_KEY', 'tracker': 'AI_DLC_TRACKER_KEY', 'work_id': 'AI_DLC_WORK_ID'}
ENV_SESSION_ID = 'AI_DLC_SESSION_ID'


def _from_env(env):
    """The fields a session's environment gives; a malformed value is skipped, as ai-dlc's readers do."""
    found = {}
    key = env.get(ENV['session_key'], '')
    if KEY.fullmatch(key):
        found['session_key'] = key
    for field in ('tracker', 'work_id'):
        if PART.fullmatch(env.get(ENV[field], '')):
            found[field] = env[ENV[field]]
    if 'session_key' not in found and ('tracker' in found or 'work_id' in found):
        session = env.get(ENV_SESSION_ID, '')
        found['session_key'] = 'aidlc1:{}:{}:{}'.format(found.get('tracker', ''), found.get('work_id', ''),
                                                        session if PART.fullmatch(session) else '')
    return found


def join_fields(values, env=None):
    """Validated ``tracker``, ``work_id`` and ``session_key`` from ``values``.

    When ``values`` gives none of them and ``env`` is given, all come from ``env``
    (values are never mixed with a session's environment). A missing
    ``session_key`` is built from the other two (with no session part), and
    missing parts are read from the key. Only fields with a value are returned.
    Raises ``ValueError`` for a malformed value or parts that disagree with the key.
    """
    given = {field: values.get(field) for field in FIELDS if values.get(field) is not None}
    for field, value in given.items():
        pattern = KEY if field == 'session_key' else PART
        if not isinstance(value, str) or not pattern.fullmatch(value):
            what = 'aidlc1:<tracker>:<work>:<session>' if field == 'session_key' else 'letters, digits, dot, dash or underscore (up to 100)'
            raise ValueError(f'{field} must be {what}')
    if env is not None:
        given = {**_from_env(env), **given} if not given else given
    if not given:
        return {}
    if 'session_key' in given:
        tracker, work, _session = KEY.fullmatch(given['session_key']).groups()
        for field, part in (('tracker', tracker), ('work_id', work)):
            if part and given.setdefault(field, part) != part:
                raise ValueError(f'{field} does not match the session key')
    else:
        given['session_key'] = 'aidlc1:{}:{}:'.format(given.get('tracker', ''), given.get('work_id', ''))
    return {field: given[field] for field in FIELDS if given.get(field)}
