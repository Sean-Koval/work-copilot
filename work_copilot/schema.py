"""Versioned SQLite schema: ordered migrations, pre-migration backups and JSONL export/import.

``PRAGMA user_version`` records the schema version. A database at version 0 is either new
(no tables) or was created before versioning existed; migration 1 brings both to the
baseline. Each migration runs in its own transaction, so a failed step leaves the previous
version intact, and a database that holds data is copied to ``backups/`` before any step.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

EXPORT_KIND = 'work-copilot-export'

# Tables holding recorded state. source_projects and source_search are projections rebuilt
# from these, so an export leaves them out.
STATE_TABLES = ('sources', 'source_revisions', 'source_mappings', 'projects', 'project_config', 'obligations',
                'source_intents', 'manual_evidence', 'evidence', 'drafts', 'connectors', 'local_metadata', 'plans')
PROJECTION_TABLES = ('source_projects', 'source_search')

_BASELINE = (
    'CREATE VIRTUAL TABLE IF NOT EXISTS source_search USING fts5(source_id UNINDEXED,title,body,project_terms)',
    'CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, revision TEXT NOT NULL, payload TEXT NOT NULL, connector TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS source_revisions(id TEXT NOT NULL,revision TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(id,revision))',
    'CREATE TABLE IF NOT EXISTS source_mappings(source_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,projects TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS project_config(project_id TEXT PRIMARY KEY,aliases TEXT NOT NULL,rooms TEXT NOT NULL)',
    "CREATE TABLE IF NOT EXISTS source_projects(source_id TEXT NOT NULL,project_id TEXT NOT NULL,workstream TEXT NOT NULL DEFAULT '',PRIMARY KEY(source_id,project_id,workstream))",
    'CREATE TABLE IF NOT EXISTS obligations(id TEXT PRIMARY KEY, canonical TEXT UNIQUE NOT NULL,payload TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS source_intents(source_id TEXT NOT NULL,candidate_key TEXT NOT NULL,obligation_id TEXT NOT NULL,PRIMARY KEY(source_id,candidate_key))',
    'CREATE TABLE IF NOT EXISTS manual_evidence(obligation_id TEXT NOT NULL,source_id TEXT NOT NULL,PRIMARY KEY(obligation_id,source_id))',
    'CREATE TABLE IF NOT EXISTS evidence(obligation_id TEXT NOT NULL,source_id TEXT NOT NULL,PRIMARY KEY(obligation_id,source_id))',
    'CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY,payload TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS connectors(name TEXT PRIMARY KEY,payload TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS local_metadata(key TEXT PRIMARY KEY,payload TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS plans(day TEXT PRIMARY KEY,payload TEXT NOT NULL)',
)


def _migrate_1(db):
    """Baseline. Also upgrades unversioned databases, which may lack project_config.revision."""
    for statement in _BASELINE:
        db.execute(statement)
    if 'revision' not in {row[1] for row in db.execute('PRAGMA table_info(project_config)')}:
        db.execute('ALTER TABLE project_config ADD COLUMN revision INTEGER NOT NULL DEFAULT 1')


# Append new steps here; never edit or reorder a released one.
MIGRATIONS = (_migrate_1,)
SCHEMA_VERSION = len(MIGRATIONS)


def version(db) -> int:
    return db.execute('PRAGMA user_version').fetchone()[0]


def _has_state(db) -> bool:
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return any(db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in STATE_TABLES if table in present)


KEEP_BACKUPS = 5


def backup(db, database_path, label) -> Path:
    """Copy the open database, WAL included, to ``backups/`` beside it, private to the user.

    Keeps the newest ``KEEP_BACKUPS`` copies: each holds captured work text.
    """
    folder = Path(database_path).parent / 'backups'
    folder.mkdir(mode=0o700, exist_ok=True)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    stem = Path(database_path).stem
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = folder / f'{stem}-{label}-{stamp}.db'
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    copy = sqlite3.connect(str(target))
    try:
        db.backup(copy)
    finally:
        copy.close()
    for old in sorted(folder.glob(f'{stem}-schema-v*-*.db'), key=lambda path: path.name.rsplit('-', 1)[-1])[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def migrate(db, database_path=None, target=None) -> dict:
    """Run ordered migrations up to ``target``; back up first when the database holds data.

    The caller holds the cross-process init lock. Returns what happened, for doctor and tests.
    """
    target = SCHEMA_VERSION if target is None else target
    current = version(db)
    if current > SCHEMA_VERSION:
        raise ValueError(f'Database schema version {current} is newer than this Work Copilot supports '
                         f'({SCHEMA_VERSION}); upgrade Work Copilot before opening it')
    result = {'from': current, 'to': max(current, target), 'backup': None}
    if current >= target:
        return result
    if database_path is not None and _has_state(db):
        result['backup'] = str(backup(db, database_path, f'schema-v{current}'))
    for step in range(current, target):
        db.execute('BEGIN IMMEDIATE')
        try:
            MIGRATIONS[step](db)
            # Projections are derived from the old schema: mark them for a rebuild in the same
            # transaction, so a crash before the rebuild cannot leave them stale (KAN-388).
            db.execute(STALE_PROJECTIONS)
            db.execute(f'PRAGMA user_version={step + 1}')
        except BaseException:
            db.rollback()
            raise
        db.commit()
    return result


def _columns(db, table):
    return [row[1] for row in db.execute(f'PRAGMA table_info({table})')]


def export_jsonl(db, destination, exported_at) -> dict:
    """Write every recorded row as JSONL: a header line, then ``{"table", "row"}`` lines.

    The caller holds a read transaction so the file is one consistent snapshot. Written to a
    private temporary file first and moved into place, so a failure leaves no partial export.
    """
    destination = Path(destination)
    counts = row_counts(db)
    temporary = destination.with_name(destination.name + '.partial')
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_BINARY', 0)
    handle = os.fdopen(os.open(temporary, flags, 0o600), 'w', encoding='utf-8', newline='\n')
    try:
        with handle:
            header = {'kind': EXPORT_KIND, 'schema_version': version(db), 'exported_at': exported_at, 'tables': counts}
            handle.write(json.dumps(header, ensure_ascii=False, sort_keys=True) + '\n')
            for table in STATE_TABLES:
                columns = _columns(db, table)
                for row in db.execute(f'SELECT {",".join(columns)} FROM {table} ORDER BY rowid'):
                    handle.write(json.dumps({'table': table, 'row': dict(zip(columns, row))}, ensure_ascii=False, sort_keys=True) + '\n')
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return {'path': str(destination), 'schema_version': header['schema_version'], 'rows': counts}


# Marks search and project links for a rebuild on the next open (KAN-388). An update, not a
# delete, so the row keeps its place and a re-export lists rows in the same order.
STALE_PROJECTIONS = """UPDATE local_metadata SET payload='{"version": null}' WHERE key='projections'"""

SIDECARS = ('-wal', '-shm', '-journal')


def _sidecars(path):
    return [path.with_name(path.name + suffix) for suffix in SIDECARS]


def _reject_constant(name):
    raise ValueError(f'{name} is not a value an export holds')


def import_jsonl(source, database_path) -> dict:
    """Write an export's rows into a new database file, then migrate it to the current schema.

    The caller opens and checks the result before moving it into place. Rows are inserted
    at the export's own schema version, so older exports upgrade through the same
    migrations an old database would.
    """
    database_path = Path(database_path)
    for path in (database_path, *_sidecars(database_path)):
        if path.exists():
            raise ValueError(f'{path} already exists; import writes a new database only')
    with Path(source).open('r', encoding='utf-8-sig') as handle:
        try:
            header = json.loads(handle.readline(), parse_constant=_reject_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError('Not a Work Copilot JSONL export: the first line is not a JSON header') from exc
        if not isinstance(header, dict) or header.get('kind') != EXPORT_KIND:
            raise ValueError('Not a Work Copilot JSONL export: missing export header')
        exported = header.get('schema_version')
        if type(exported) is not int or not 1 <= exported <= SCHEMA_VERSION:
            raise ValueError(f'Export schema version {exported!r} is not supported by this Work Copilot (1..{SCHEMA_VERSION})')
        expected = header.get('tables')
        if not isinstance(expected, dict) or set(expected) != set(STATE_TABLES):
            raise ValueError('Export header does not list its row counts; the file may be incomplete')
        db = sqlite3.connect(str(database_path))
        try:
            migrate(db, None, exported)
            columns = {table: set(_columns(db, table)) for table in STATE_TABLES}
            counts = dict.fromkeys(STATE_TABLES, 0)
            db.execute('BEGIN IMMEDIATE')
            for number, line in enumerate(handle, 2):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line, parse_constant=_reject_constant)
                    table, row = (item.get('table'), item.get('row')) if isinstance(item, dict) else (None, None)
                    if table not in columns or not isinstance(row, dict) or not row or set(row) - columns[table]:
                        raise ValueError('not a row of a recorded table')
                    if any(not (value is None or isinstance(value, (str, int, float))) or isinstance(value, bool) for value in row.values()):
                        raise ValueError('holds a value SQLite cannot store as exported')
                    names = sorted(row)
                    db.execute(f'INSERT INTO {table}({",".join(names)}) VALUES({",".join("?" * len(names))})', [row[name] for name in names])
                except (ValueError, OverflowError, sqlite3.Error) as exc:
                    raise ValueError(f'Line {number}: {exc}') from exc
                counts[table] += 1
            if expected != counts:
                raise ValueError('Export is incomplete: row counts differ from its header')
            # Projections are not exported, so the first open must build them.
            db.execute(STALE_PROJECTIONS)
            db.commit()
            migrated = migrate(db, None)
        finally:
            db.close()
    return {'path': str(database_path), 'from_schema_version': exported, 'schema_version': migrated['to'], 'rows': counts}


def row_counts(db) -> dict:
    return {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in STATE_TABLES}
