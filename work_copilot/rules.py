"""Team rules the store applies when it drafts a reply or plans the day.

The store holds no domain words: which intents need a canned reply, which words mark
a release question, which provider counts as engineering evidence and which owner
names mean the user all live here, as data. A team changes them in ``rules.json`` next
to the database, not in the store. Rules shape offline draft text and ordering only;
they never send anything. The store reads them when it opens, so restart the app or
the MCP server after editing ``rules.json``.

``rules.json`` may set any top-level key of ``DEFAULT_RULES``. ``intents`` merges by
``name``: an entry with a default's name replaces it, ``{"name": ..., "disabled": true}``
removes it, and a new name is added after the defaults. Every other key replaces its
default. Unknown keys are refused so a typo is not silently ignored.

The user is always stored as the owner ``SELF`` (``'me'``), which the UI and the MCP
tools expect; ``self_aliases`` lists other owner names that a source may use for them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

RULES_FILE = 'rules.json'
# The stored owner value for the person using this copilot.
SELF = 'me'

DEFAULT_RULES = {
    # Other owner names meaning the person using this copilot; stored as SELF.
    'self_aliases': [],
    # Gaps every draft lists, whatever its intent.
    'standing_gaps': [
        'Deployment is not verified unless explicitly evidenced; a merged PR alone is not deployment.',
    ],
    # A line added when a clarification for the intent is already sent or answered.
    'question_state_notes': {
        'sent': 'A clarification is already recorded as sent; check for a response rather than ask again.',
        'answered': 'This intent is already recorded as answered; check whether another reply is needed.',
    },
    # Intents matched by ask kind (exactly, ignoring case) or by words in it. The first match applies.
    # replies: the whole draft text by question_state ('pending' is the fallback), replacing quoted evidence.
    # lead_provider: a word in a provider name whose source is quoted second, right after the direct evidence.
    # notes: lines added after the quoted evidence.
    'intents': [
        {
            'name': 'missing-transcript',
            'kinds': ['request-missing-transcript'],
            'replies': {
                'sent': 'The transcript request is already recorded as sent. Check for a response before sending another request.',
                'answered': 'The transcript request is recorded as answered. Review the received evidence; do not request it again automatically.',
                'pending': 'Could you share the meeting transcript or notes when available? I do not have the transcript, so I cannot yet summarize the discussion or confirm its decisions.',
            },
        },
        {
            'name': 'release',
            'words': ['release', 'deployment'],
            'lead_provider': 'github',
            'notes': [
                'Deployment is not verified by this evidence packet; merged code is not a deployment receipt. No delivery date is established here.',
            ],
        },
    ],
}

_QUESTION_STATES = ('pending', 'sent', 'answered')


def _strings(value, what):
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f'{what} must be a list of non-empty strings')
    return tuple(value)


@dataclass(frozen=True)
class IntentRule:
    name: str
    kinds: tuple[str, ...] = ()
    words: tuple[str, ...] = ()
    replies: dict = field(default_factory=dict)
    lead_provider: str | None = None
    notes: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value):
        _check_name(value)
        unknown = set(value) - {'name', 'kinds', 'words', 'replies', 'lead_provider', 'notes'}
        if unknown:
            raise ValueError('Unknown intent rule keys: ' + ', '.join(sorted(unknown)))
        kinds = tuple(kind.lower() for kind in _strings(value.get('kinds', []), 'kinds'))
        words = tuple(word.lower() for word in _strings(value.get('words', []), 'words'))
        if not kinds and not words:
            raise ValueError(f"Intent rule {value['name']} needs kinds or words")
        replies = value.get('replies', {})
        if not isinstance(replies, dict) or set(replies) - set(_QUESTION_STATES) or not all(isinstance(v, str) and v for v in replies.values()):
            raise ValueError('replies maps pending, sent or answered to text')
        lead = value.get('lead_provider')
        if lead is not None and (not isinstance(lead, str) or not lead):
            raise ValueError('lead_provider must be a non-empty string')
        return cls(name=value['name'], kinds=kinds, words=words, replies=dict(replies),
                   lead_provider=lead.lower() if lead else None, notes=_strings(value.get('notes', []), 'notes'))

    def matches(self, kind):
        kind = kind.lower()
        return kind in self.kinds or any(word in kind for word in self.words)


def _check_name(value):
    if not isinstance(value, dict) or not isinstance(value.get('name'), str) or not value['name']:
        raise ValueError('Each intent rule needs a name')


def _merge_intents(defaults, overrides):
    """Defaults merged with the file's intents by name, as the module docstring says."""
    if not isinstance(overrides, list):
        raise ValueError('intents must be a list')
    merged = {item['name']: item for item in defaults}
    for item in overrides:
        _check_name(item)
        if item.get('disabled') is True and set(item) == {'name', 'disabled'}:
            merged.pop(item['name'], None)
        elif 'disabled' in item:
            raise ValueError('disabled takes true and only a name beside it')
        else:
            merged[item['name']] = item
    return list(merged.values())


@dataclass(frozen=True)
class Rules:
    self_aliases: tuple[str, ...]
    standing_gaps: tuple[str, ...]
    question_state_notes: dict
    intents: tuple[IntentRule, ...]

    @classmethod
    def from_dict(cls, overrides=None):
        if overrides is None:
            overrides = {}
        if not isinstance(overrides, dict):
            raise ValueError('Rules must be a JSON object')
        unknown = set(overrides) - set(DEFAULT_RULES)
        if unknown:
            raise ValueError('Unknown rules keys: ' + ', '.join(sorted(unknown)))
        value = {**DEFAULT_RULES, **overrides}
        if 'intents' in overrides:
            value['intents'] = _merge_intents(DEFAULT_RULES['intents'], overrides['intents'])
        aliases = _strings(value['self_aliases'], 'self_aliases')
        notes = value['question_state_notes']
        if not isinstance(notes, dict) or set(notes) - set(_QUESTION_STATES) or not all(isinstance(v, str) and v for v in notes.values()):
            raise ValueError('question_state_notes maps pending, sent or answered to text')
        return cls(self_aliases=tuple(alias.strip().casefold() for alias in aliases), standing_gaps=_strings(value['standing_gaps'], 'standing_gaps'),
                   question_state_notes=dict(notes), intents=tuple(IntentRule.from_dict(item) for item in value['intents']))

    @classmethod
    def load(cls, directory):
        """Defaults, overridden by ``rules.json`` in ``directory`` when that file exists."""
        path = Path(directory) / RULES_FILE
        if not path.is_file():
            return cls.from_dict()
        try:
            # utf-8-sig: Windows Notepad saves UTF-8 with a byte order mark.
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError, RecursionError) as exc:
            raise ValueError(f'{path} is unreadable or not JSON') from exc
        if not isinstance(data, dict):
            raise ValueError(f'{path}: rules must be a JSON object')
        try:
            return cls.from_dict(data)
        except ValueError as exc:
            raise ValueError(f'{path}: {exc}') from exc

    # --- ownership -----------------------------------------------------------------

    @property
    def self_owner(self):
        return SELF

    def owner(self, value):
        """The stored owner for ``value``: an alias of the user becomes ``SELF``."""
        if isinstance(value, str) and value.strip().casefold() in self.self_aliases:
            return SELF
        return value

    def is_mine(self, obligation):
        return obligation.get('owner') == SELF

    def initial_status(self, owner):
        """A new obligation is open when it is mine, otherwise a candidate to review."""
        return 'open' if owner == SELF else 'candidate'

    # --- drafts --------------------------------------------------------------------

    @staticmethod
    def ask_kind(intent_key):
        return intent_key

    def intent_rule(self, intent_key):
        kind = self.ask_kind(intent_key)
        return next((rule for rule in self.intents if rule.matches(kind)), None)

    def order_evidence(self, obligation, evidence):
        """Move the first source from the rule's lead provider to second place."""
        rule = self.intent_rule(obligation['intent_key'])
        evidence = list(evidence)
        if rule and rule.lead_provider and len(evidence) > 1:
            lead = next((source for source in evidence[1:] if rule.lead_provider in source['provider'].lower()), None)
            if lead:
                evidence.remove(lead)
                evidence.insert(1, lead)
        return evidence

    def reply(self, obligation):
        """``(rule name, canned draft lines)`` for this intent and question state, or None to quote evidence."""
        rule = self.intent_rule(obligation['intent_key'])
        if not rule or not rule.replies:
            return None
        state = obligation.get('question_state') or 'pending'
        text = rule.replies.get(state, rule.replies.get('pending'))
        return (rule.name, [text]) if text else None

    def notes(self, obligation):
        """Lines added after the quoted evidence."""
        rule = self.intent_rule(obligation['intent_key'])
        lines = list(rule.notes) if rule else []
        note = self.question_state_notes.get(obligation.get('question_state'))
        if note:
            lines.append(note)
        return lines
