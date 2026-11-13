"""Read-only review queue over a knowledge repository (KAN-468).

Three things wait on the user and become ``review`` obligations in "Needs you":

* open pull requests labelled ``records``, read from the GitHub REST API with GET only:
  all of them in a personal repository (``review='all'``), or in a team repository
  those where the user is a requested reviewer, in a requested team, a code owner or
  the author, and those the batch bot opened (``review='assigned'``). Drafts count
  when they change record files, since ai-dlc ``propose`` opens drafts;
* records under the check roots (``team/``, ``projects/``) that the user owns and
  whose ``stale_after`` (formerly ``review_by``) has passed;
* inbox Source records still ``raw`` and older than 25 days, so they expire in 5.

Each obligation carries ``opened_at`` and an ``age_hours`` so the 48-hour review
outcome (north-star O2) is visible; a PR's ``due_at`` is opened plus 48 hours.
An item that no longer appears on the next collect resolves itself.

Nothing here writes outside Work Copilot's own store: the local clone is only read,
and GitHub is only called with ``GET``. Approving, merging and commenting are out of
scope by design.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

CONNECTOR = 'knowledge-review'
RECORDS_LABEL = 'records'
REVIEW_MODES = ('all', 'assigned')
REVIEW_WINDOW = timedelta(hours=48)
SOURCE_WARNING_AGE = timedelta(days=25)
SOURCE_WINDOW = timedelta(days=30)
DEFAULT_ROOTS = ('team', 'projects')
# Statuses that need no further review, whatever their stale_after says.
_FINISHED = {'superseded', 'rejected', 'retired', 'expired'}
_CODEOWNERS = ('.github/CODEOWNERS', 'CODEOWNERS', 'docs/CODEOWNERS')
_MAX_PAGES = 10

Fetch = Callable[[str], object]
log = logging.getLogger(__name__)


class AccessDenied(LookupError):
    """GitHub answered 403 for a read, for example a team membership the token cannot see."""


# --- GitHub, read-only --------------------------------------------------------


def gh_reader(runner=subprocess.run, executable: str | None = None) -> Fetch:
    """A GET-only reader over ``gh api``; gh holds the credentials, not Work Copilot."""
    binary = executable or shutil.which('gh')
    if not binary:
        raise ValueError('The GitHub CLI (gh) is required to read pull requests; install it and run gh auth login')

    def fetch(path: str):
        if path.startswith(('/', 'http')) or '..' in path:
            raise ValueError('GitHub paths are relative REST paths')
        result = runner([binary, 'api', '--method', 'GET', path], capture_output=True, text=True,
                        encoding='utf-8', timeout=60, check=False)
        if result.returncode != 0:
            if 'HTTP 404' in (result.stderr or ''):
                raise LookupError(path.split('?')[0])
            if 'HTTP 403' in (result.stderr or ''):
                raise AccessDenied(path.split('?')[0])
            raise RuntimeError('gh api GET failed for ' + path.split('?')[0] + '; check gh auth status')
        return json.loads(result.stdout or 'null')

    return fetch


def _pages(fetch: Fetch, path: str) -> list[dict]:
    items: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        batch = fetch(f'{path}{"&" if "?" in path else "?"}per_page=100&page={page}')
        if not isinstance(batch, list):
            raise ValueError('Unexpected GitHub response for ' + path.split('?')[0])
        items.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
    return items


# --- CODEOWNERS ---------------------------------------------------------------


def codeowner_rules(root: Path) -> list[tuple[str, list[str]]]:
    for name in _CODEOWNERS:
        path = root / name
        if path.is_file():
            rules = []
            for line in path.read_text(encoding='utf-8').splitlines():
                text = line.split('#', 1)[0].strip()
                if text:
                    pattern, *owners = text.split()
                    rules.append((pattern, [owner.lstrip('@').lower() for owner in owners]))
            return rules
    return []


def _matches(pattern: str, path: str) -> bool:
    """gitignore-style match: a pattern matches a path or any of its parent directories."""
    directory = pattern.endswith('/')
    core = pattern.strip('/')
    if core in ('', '*', '**'):
        return True
    parts = path.split('/')
    starts = [0] if pattern.startswith('/') or '/' in core else range(len(parts))
    for i in starts:
        for j in range(i + 1, len(parts) + (0 if directory else 1)):
            if fnmatch.fnmatchcase('/'.join(parts[i:j]), core):
                return True
    return False


def owners_of(rules: list[tuple[str, list[str]]], path: str) -> list[str]:
    """Owners of one path: the last matching rule wins, as on GitHub."""
    found: list[str] = []
    for pattern, owners in rules:
        if _matches(pattern, path):
            found = owners
    return found


# --- Frontmatter --------------------------------------------------------------


def frontmatter(text: str) -> dict:
    """Top-level scalar fields of a record header (JSON or plain YAML scalars)."""
    lines = text.lstrip('﻿').splitlines()
    if not lines or lines[0].strip() != '---':
        return {}
    fields: dict = {}
    for line in lines[1:]:
        if line.strip() == '---':
            return fields
        if not line or line[0] in ' \t-#' or ':' not in line:
            continue
        key, _, value = line.partition(':')
        value = value.strip()
        try:
            fields[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            fields[key.strip()] = value.strip('\'"')
    return {}


def _instant(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _revision(*parts) -> str:
    return 'sha256:' + hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:32]


# --- Collectors ---------------------------------------------------------------


def _is_record_path(path: str, roots) -> bool:
    return path.split('/', 1)[0] in (*roots, 'inbox') and path.endswith('.md')


def pull_request_items(fetch: Fetch, repository: str, me: str, rules, *, review: str = 'all',
                       batch_authors=(), record_roots=DEFAULT_ROOTS) -> list[dict]:
    """Open records PRs that wait on ``me``: labelled ``records``, or changing record files.

    ``review='all'`` (the personal repository) lists every open ``records`` PR, because the
    nightly batch opens them under the user's own account. ``review='assigned'`` (a team
    repository) lists PRs where ``me`` is a requested reviewer or a code owner of a changed
    file, plus PRs authored by ``me`` or by one of ``batch_authors``.
    """
    if review not in REVIEW_MODES:
        raise ValueError('review must be one of ' + ', '.join(REVIEW_MODES))
    login = me.lstrip('@').lower()
    batch = {name.lstrip('@').lower() for name in batch_authors if name}
    items = []
    memberships: dict[tuple[str, str], bool] = {}
    denied: list[str] = []

    def in_team(team) -> bool:
        org = str((team.get('organization') or {}).get('login') or repository.split('/')[0])
        slug = str(team.get('slug', ''))
        if not slug:
            return False
        if (org, slug) not in memberships:
            try:
                answer = fetch(f'orgs/{quote(org, safe="")}/teams/{quote(slug, safe="")}/memberships/{quote(login, safe="")}')
            except AccessDenied:  # 403: the token cannot read this team; skip it, keep going
                if not denied:
                    log.warning('Skipping requested teams whose membership GitHub would not show (HTTP 403)')
                denied.append(slug)
                answer = None
            except LookupError:  # 404: not a member, or the team is not visible to you
                answer = None
            memberships[(org, slug)] = isinstance(answer, dict) and answer.get('state') == 'active'
        return memberships[(org, slug)]

    for pull in _pages(fetch, f'repos/{repository}/pulls?state=open'):
        draft = bool(pull.get('draft'))
        labels = {str(label.get('name', '')).lower() for label in pull.get('labels') or [] if isinstance(label, dict)}
        files = None
        if RECORDS_LABEL not in labels or draft:
            # The live repository had no `records` label and the batch PR carried none, so a PR
            # that changes record files counts as a records PR too. ai-dlc `propose` opens drafts:
            # a draft counts when it changes record files, label or not.
            files = _pages(fetch, f'repos/{repository}/pulls/{int(pull["number"])}/files')
            if not any(_is_record_path(str(f.get('filename', '')), record_roots) for f in files):
                continue
        author = str((pull.get('user') or {}).get('login', '')).lower()
        reviewers = {str(r.get('login', '')).lower() for r in pull.get('requested_reviewers') or [] if isinstance(r, dict)}
        if review == 'all':
            why = 'owner of this repository'
        elif login in reviewers:
            why = 'requested reviewer'
        elif author == login:
            why = 'author'
        elif any(in_team(t) for t in pull.get('requested_teams') or [] if isinstance(t, dict)):
            why = 'member of a requested team'
        elif author in batch:
            why = 'reviewer of the batch'
        else:
            why = None
        if why is None and rules:
            if files is None:
                files = _pages(fetch, f'repos/{repository}/pulls/{int(pull["number"])}/files')
            if any(login in owners_of(rules, str(f.get('filename', ''))) for f in files):
                why = 'code owner'
        if why is None:
            continue
        opened = _instant(pull.get('created_at'))
        if opened is None:
            continue
        number = int(pull['number'])
        title = str(pull.get('title') or f'Pull request #{number}')
        items.append({
            'key': f'pr:{repository.lower()}#{number}', 'review_kind': 'pull_request', 'draft': draft,
            'title': f'Review {"draft " if draft else ""}records PR #{number}: {title}', 'url': pull.get('html_url'),
            'opened_at': opened.isoformat(), 'due_at': (opened + REVIEW_WINDOW).isoformat(),
            'body': f'Open {"draft " if draft else ""}records pull request #{number} in {repository}, '
                    f'waiting on you as {why}. The 48-hour clock runs from when it was opened, draft or not. '
                    'Review it on GitHub; Work Copilot never approves or merges.',
            'revision': _revision(number, title, opened.isoformat(), why, draft),
        })
    return items


def _record_roots(root: Path) -> tuple[str, ...]:
    config = root / 'knowledge.toml'
    if config.is_file():
        try:
            roots = tomllib.loads(config.read_text(encoding='utf-8')).get('check', {}).get('roots')
        except tomllib.TOMLDecodeError:
            roots = None
        if isinstance(roots, list) and all(isinstance(r, str) for r in roots):
            return tuple(roots)
    return DEFAULT_ROOTS


def _markdown(directory: Path):
    if directory.is_dir() and not directory.is_symlink():
        for path in sorted(directory.rglob('*.md')):
            if path.is_file() and not path.is_symlink() and path.name not in ('README.md', 'index.md', 'log.md'):
                yield path


def overdue_record_items(root: Path, owner: str, now: datetime) -> list[dict]:
    """Records the user owns whose ``stale_after`` has passed."""
    items = []
    wanted = owner.strip().lower()
    for top in _record_roots(root):
        for path in _markdown(root / top):
            header = frontmatter(path.read_text(encoding='utf-8', errors='replace'))
            due = _instant(header.get('stale_after') or header.get('review_by'))
            if (due is None or due > now or str(header.get('owner', '')).strip().lower() != wanted
                    or str(header.get('status', '')).lower() in _FINISHED):
                continue
            relative = path.relative_to(root).as_posix()
            name = header.get('title') or header.get('id') or relative
            items.append({
                'key': f'record:{relative}', 'review_kind': 'overdue_record', 'path': relative,
                'title': f'Re-review {header.get("type", "record")} {name}: stale since {due.date().isoformat()}',
                'url': None, 'opened_at': due.isoformat(), 'due_at': due.isoformat(),
                'body': f'{relative} is past its stale_after date ({due.isoformat()}). Confirm it or move the date '
                        'in a pull request to the knowledge repository.',
                'revision': _revision(relative, due.isoformat(), header.get('status'), name),
            })
    return items


def expiring_source_items(root: Path, now: datetime) -> list[dict]:
    """Unprocessed inbox Source records older than 25 days (5 days before expiry)."""
    items = []
    for path in sorted((root / 'inbox').glob('*.md')) if (root / 'inbox').is_dir() else []:
        if not path.is_file() or path.is_symlink():
            continue
        header = frontmatter(path.read_text(encoding='utf-8', errors='replace'))
        if header.get('type') != 'source' or str(header.get('status', 'raw')) != 'raw':
            continue
        expires = _instant(header.get('stale_after'))
        captured = expires - SOURCE_WINDOW if expires else _instant(header.get('observed_at') or header.get('date'))
        if captured is None or now - captured < SOURCE_WARNING_AGE:
            continue
        expires = expires or captured + SOURCE_WINDOW
        relative = path.relative_to(root).as_posix()
        name = header.get('title') or header.get('id') or path.stem
        items.append({
            'key': f'source:{relative}', 'review_kind': 'expiring_source', 'path': relative,
            'title': f'Process inbox Source {name} before it expires on {expires.date().isoformat()}',
            'url': None, 'opened_at': captured.isoformat(), 'due_at': expires.isoformat(),
            'body': f'{relative} has waited in the inbox since {captured.date().isoformat()} and is still raw. '
                    'Extract records from it or let it expire.',
            'revision': _revision(relative, captured.isoformat(), expires.isoformat(), name),
        })
    return items


def collect_review_queue(store, repo, *, me: str, owner: str | None = None, github: str | None = None,
                         review: str = 'all', batch_authors=(), fetch: Fetch | None = None,
                         now: datetime | None = None) -> dict:
    """Collect the queue and sync it into ``store``; returns counts, never writes externally."""
    from .knowledge import knowledge_root
    if not isinstance(me, str) or not me.strip():
        raise ValueError('Give your GitHub login (--me) so the queue knows which reviews wait on you')
    root = knowledge_root(repo)
    moment = (now or store.now()).astimezone(timezone.utc)
    items: list[dict] = []
    if github:
        if github.count('/') != 1 or any(not part or part != quote(part, safe='-_.') for part in github.split('/')):
            raise ValueError('--github must be owner/repository')
        items += pull_request_items(fetch or gh_reader(), github, me, codeowner_rules(root), review=review,
                                    batch_authors=batch_authors, record_roots=_record_roots(root))
    items += overdue_record_items(root, owner or me, moment)
    items += expiring_source_items(root, moment)
    result = store.sync_review_queue(items, connector=CONNECTOR, observed_at=moment.isoformat())
    result.update(pull_requests_read=bool(github), external_writes=False)
    return result
