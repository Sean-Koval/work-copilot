"""Team rules the store applies when it drafts a reply or plans the day.

The store holds no domain words: which intents need a canned reply, which words mark
a release question, which provider counts as engineering evidence and who "me" is all
live here, as data. A team changes them in ``rules.json`` next to the database, not in
the store. Rules shape offline draft text and ordering only; they never send anything.

``rules.json`` may set any top-level key of ``DEFAULT_RULES``; a key it sets replaces
the default for that key. Unknown keys are refused so a typo is not silently ignored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .ids import ask_kind

RULES_FILE = 'rules.json'

DEFAULT_RULES = {
    # The owner value meaning the person using this copilot.
    'self_owner': 'me',
    # Gaps every draft lists, whatever its intent.
    'standing_gaps': [
        'Deployment is not verified unless explicitly evidenced; a merged PR alone is not deployment.',
    ],
    # A line added when a clarification for the intent is already sent or answered.
    'question_state_notes': {
        'sent': 'A clarification is already recorded as sent; check for a response rather than ask again.',
        'answered': 'This intent is already recorded as answered; check whether another reply is needed.',
    },
    # Intents matched by ask kind or by words in it. The first match applies.
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
        if not isinstance(value, dict) or not isinstance(value.get('name'), str) or not value['name']:
            raise ValueError('Each intent rule needs a name')
        unknown = set(value) - {'name', 'kinds', 'words', 'replies', 'lead_provider', 'notes'}
        if unknown:
            raise ValueError('Unknown intent rule keys: ' + ', '.join(sorted(unknown)))
        kinds = _strings(value.get('kinds', []), 'kinds')
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
        return kind in self.kinds or any(word in kind.lower() for word in self.words)


@dataclass(frozen=True)
class Rules:
    self_owner: str
    standing_gaps: tuple[str, ...]
    question_state_notes: dict
    intents: tuple[IntentRule, ...]

    @classmethod
    def from_dict(cls, overrides=None):
        overrides = overrides or {}
        if not isinstance(overrides, dict):
            raise ValueError('Rules must be a JSON object')
        unknown = set(overrides) - set(DEFAULT_RULES)
        if unknown:
            raise ValueError('Unknown rules keys: ' + ', '.join(sorted(unknown)))
        value = {**DEFAULT_RULES, **overrides}
        if not isinstance(value['self_owner'], str) or not value['self_owner']:
            raise ValueError('self_owner must be a non-empty string')
        notes = value['question_state_notes']
        if not isinstance(notes, dict) or set(notes) - set(_QUESTION_STATES) or not all(isinstance(v, str) and v for v in notes.values()):
            raise ValueError('question_state_notes maps pending, sent or answered to text')
        if not isinstance(value['intents'], list):
            raise ValueError('intents must be a list')
        return cls(self_owner=value['self_owner'], standing_gaps=_strings(value['standing_gaps'], 'standing_gaps'),
                   question_state_notes=dict(notes), intents=tuple(IntentRule.from_dict(item) for item in value['intents']))

    @classmethod
    def load(cls, directory):
        """Defaults, overridden by ``rules.json`` in ``directory`` when that file exists."""
        path = Path(directory) / RULES_FILE
        if not path.is_file():
            return cls.from_dict()
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError(f'{RULES_FILE} is unreadable or not JSON') from exc
        return cls.from_dict(data)

    # --- ownership -----------------------------------------------------------------

    def is_mine(self, obligation):
        return obligation.get('owner') == self.self_owner

    def initial_status(self, owner):
        """A new obligation is open when it is mine, otherwise a candidate to review."""
        return 'open' if owner == self.self_owner else 'candidate'

    # --- drafts --------------------------------------------------------------------

    @staticmethod
    def ask_kind(intent_key):
        return ask_kind(intent_key)

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
        """Canned draft text for this intent and question state, or None to quote evidence."""
        rule = self.intent_rule(obligation['intent_key'])
        if not rule or not rule.replies:
            return None
        state = obligation.get('question_state') or 'pending'
        text = rule.replies.get(state, rule.replies.get('pending'))
        return [text] if text else None

    def notes(self, obligation):
        """Lines added after the quoted evidence."""
        rule = self.intent_rule(obligation['intent_key'])
        lines = list(rule.notes) if rule else []
        note = self.question_state_notes.get(obligation.get('question_state'))
        if note:
            lines.append(note)
        return lines
