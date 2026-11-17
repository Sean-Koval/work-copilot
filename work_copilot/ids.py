"""Stable ids for sources and the asks they carry.

A source id is ``provider:site:object``:

* ``provider`` names the system, lower case (``jira``, ``confluence``, ``symphony``,
  ``github``, ``vault``, ``local-user``). It equals the source's ``provider`` field.
* ``site`` names the instance, so two Jira sites can't collide
  (``acme.atlassian.net``, a Symphony tenant, a vault id, or ``local``).
* ``object`` is the provider's own id for the item (``ATLAS-12``, a page id, a note
  path). It may contain ``:`` and ``/``, but no control characters or edge spaces.

An intent key is ``<source id>:<ask kind>``: the source the ask came from (which need
not be ingested here) and a lower-case kind such as ``reply``, ``review`` or
``release-readiness``. The intent key alone is an obligation's identity, so the same
ask recorded twice, from any source or project mapping, is one obligation.

Ingest refuses anything else. Records stored before these rules keep their ids.
"""
from __future__ import annotations

import re

_PROVIDER = r'[a-z][a-z0-9-]{0,39}'
_SITE = r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}'
_OBJECT = r'[^\x00-\x1f\x7f\s](?:[^\x00-\x1f\x7f]{0,1022}[^\x00-\x1f\x7f\s])?'
_KIND = r'[a-z][a-z0-9-]{0,63}'
SOURCE_ID = re.compile(rf'(?P<provider>{_PROVIDER}):(?P<site>{_SITE}):(?P<object>{_OBJECT})')
ASK_KIND = re.compile(_KIND)

SOURCE_ID_FORMAT = 'provider:site:object (for example jira:acme.atlassian.net:ATLAS-12)'
INTENT_KEY_FORMAT = '<source id>:<ask kind> (for example jira:acme.atlassian.net:ATLAS-12:reply)'


def parse_source_id(value):
    """``(provider, site, object)`` of a valid source id, else ``ValueError``."""
    match = SOURCE_ID.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError('Source id must be ' + SOURCE_ID_FORMAT)
    return match['provider'], match['site'], match['object']


def validate_source_id(value, provider=None):
    """Check ``value``; when ``provider`` is given, the id's provider must equal it."""
    found, _site, _object = parse_source_id(value)
    if provider is not None and found != provider:
        raise ValueError(f'Source id provider {found!r} does not match the source provider {provider!r}')
    return value


def parse_intent_key(value):
    """``(source id, ask kind)`` of a valid intent key, else ``ValueError``."""
    if isinstance(value, str) and ':' in value:
        source, _, kind = value.rpartition(':')
        if ASK_KIND.fullmatch(kind) and SOURCE_ID.fullmatch(source):
            return source, kind
    raise ValueError('Intent key must be ' + INTENT_KEY_FORMAT)


def is_intent_key(value):
    try:
        parse_intent_key(value)
    except ValueError:
        return False
    return True


def intent_key(source_id, kind):
    """Build an intent key, validating both parts."""
    parse_source_id(source_id)
    if not isinstance(kind, str) or not ASK_KIND.fullmatch(kind):
        raise ValueError('Ask kind must be lower case letters, digits and dashes')
    return f'{source_id}:{kind}'


def ask_kind(value):
    """The ask kind of an intent key; a key stored before these rules is its own kind."""
    try:
        return parse_intent_key(value)[1]
    except ValueError:
        return value
