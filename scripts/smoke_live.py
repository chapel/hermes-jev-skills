"""Opt-in three-request synthetic smoke check; never imports a conversation.

Provide a reviewed skills_list JSON snapshot via --catalog. Requires the API key
in the process environment. No key files are opened by this script.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jev_skills.hermes_adapter import DiscoveryPlugin

QUERIES = [
    'Make me a video about my webapp',
    'Extract tables from this scanned PDF into an Excel workbook.',
    'Thanks, that answers my question. Nothing else to do.',
]


class SmokeContext:
    """Ephemeral adapter configuration; not an installed or enabled plugin."""
    class State:
        data_dir = 'synthetic-live-smoke'
    state = State()

    def __init__(self, model):
        self.config = {'allow_remote': True, 'auto_suggest': False, 'record_events': False,
                       'history_messages': 0, 'jev_model': model,
                       'max_calls_per_session': 3, 'max_calls_per_process': 3}

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--catalog', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--model', default='jev-latest')
    args = parser.parse_args()
    if not args.live:
        print('No requests sent. --live sends at most three synthetic queries with the supplied catalog; no retries.')
        return
    key = os.environ.get('TYPESAFE_API_KEY')
    if not key or not args.catalog or not args.output:
        parser.error('--live needs TYPESAFE_API_KEY in the environment, --catalog, and --output')
    data = json.loads(args.catalog.read_text())
    assert data['success'] and len(data['skills']) == data['count']
    skills = sorted([{'name': row['name'], 'description': row['description']}
                     for row in data['skills']], key=lambda row: row['name'])
    plugin = DiscoveryPlugin(SmokeContext(args.model), catalog_loader=lambda: skills,
                             secret_getter=lambda: key)
    records = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for query in QUERIES:
        result = json.loads(plugin.search({'query': query, 'min_probability': 0, 'limit': 100},
                                         session_id='synthetic-live-smoke'))
        # Preserve all scores using the cache; never a fourth live attempt.
        if result.get('has_more'):
            page = json.loads(plugin.search({'query': query, 'min_probability': 0, 'limit': 100,
                                             'offset': result['returned_count']}, session_id='synthetic-live-smoke'))
            result['candidates'].extend(page.get('candidates', []))
            if page.get('has_more'):
                page2 = json.loads(plugin.search({'query': query, 'min_probability': 0, 'limit': 100,
                    'offset': len(result['candidates'])}, session_id='synthetic-live-smoke'))
                result['candidates'].extend(page2.get('candidates', []))
            result['returned_count'] = len(result['candidates'])
            result['has_more'] = result['returned_count'] < result['matching_count']
        records.append({'query': query, 'result': result})
        args.output.write_text(json.dumps({'synthetic_only': True, 'requested_model': args.model,
            'catalog_count': len(skills), 'records': records}, indent=2))
        print(json.dumps({'query': query, 'status': result['status'], 'model': result.get('model'),
            'usage': result.get('usage'), 'latency_ms': result.get('latency_ms'),
            'error': result.get('error'), 'top': [
                {'name': r['name'], 'probability': r['probability']}
                for r in result.get('candidates', [])[:8]]}))
        if result['status'] != 'ok':
            raise SystemExit('Stopped at first error; no retries.')
    assert plugin.total_calls == 3
    assert all(len(r['result']['candidates']) == len(skills) for r in records)


if __name__ == '__main__':
    main()
