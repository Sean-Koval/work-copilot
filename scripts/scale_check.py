"""Measure reopen and get_attention time on a synthetic store (KAN-388).

    python scripts/scale_check.py [--sources 5000] [--strict]

Builds a throwaway store with N synthetic sources of about 1 KB each (one obligation per
ten sources, spread over 20 projects), then times a cold reopen and get_attention. With
--strict it exits 1 when reopen takes 0.5 s or more, or get_attention 200 ms or more.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from work_copilot.mcp_server import build_server  # noqa: E402
from work_copilot.store import Store  # noqa: E402

REOPEN_BUDGET = 0.5
ATTENTION_BUDGET = 0.2


def synthetic(count):
    for number in range(count):
        project = f'p{number % 20}'
        record = {'id': f'scale/{number}', 'revision': '1', 'provider': 'scale', 'title': f'Thread {number}',
                  'body': f'Synthetic message {number}. ' + 'lorem ipsum dolor sit amet ' * 36,
                  'room_id': f'room{number % 40}', 'room_name': f'Room {number % 40}',
                  'observed_at': f'2026-11-{1 + number % 16:02d}T12:00:00+00:00',
                  'projects': [{'id': project, 'name': f'Project {number % 20}', 'workstream': 'Engineering'}],
                  'obligations': []}
        if number % 10 == 0:
            record['obligations'] = [{'intent_key': f'follow-up-{number}', 'title': f'Follow up on thread {number}',
                                      'project_id': project, 'owner': 'me'}]
        yield record


def measure(count, folder):
    path = Path(folder) / 'copilot.db'
    store = Store(path)
    records = list(synthetic(count))
    for start in range(0, len(records), 500):
        store.ingest(records[start:start + 500], connector='scale')
    store.close()

    started = time.perf_counter()
    Store(path).close()
    reopen = time.perf_counter() - started

    server = build_server(Path(folder))
    try:
        tool = server._tool_manager.get_tool('get_attention')
        started = time.perf_counter()
        result = json.loads(tool.fn())
        attention = time.perf_counter() - started
    finally:
        # Windows cannot delete the temporary folder while the database is open.
        server.work_copilot_store.close()
    return {'sources': count, 'obligations': result['page']['total'], 'reopen_seconds': round(reopen, 3),
            'get_attention_seconds': round(attention, 3)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--sources', type=int, default=5000)
    parser.add_argument('--strict', action='store_true', help='exit 1 when a budget is missed')
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as folder:
        numbers = measure(args.sources, folder)
    numbers['within_budget'] = numbers['reopen_seconds'] < REOPEN_BUDGET and numbers['get_attention_seconds'] < ATTENTION_BUDGET
    print(json.dumps(numbers, indent=2))
    return 1 if args.strict and not numbers['within_budget'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
