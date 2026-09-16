"""Metadata-only catalog adapter; never loads or executes a skill body."""
import json


def load_catalog(ctx):
    payload = json.loads(ctx.dispatch_tool('skills_list', {}))
    if not payload.get('success'):
        raise ValueError('Hermes skill catalog unavailable')
    rows = payload.get('skills', [])
    if not isinstance(rows, list) or payload.get('count', len(rows)) != len(rows):
        raise ValueError('Incomplete Hermes skill catalog')
    skills = []
    seen = set()
    for row in rows:
        name, description = row.get('name'), row.get('description')
        if not isinstance(name, str) or not name or name in seen:
            raise ValueError('Ambiguous Hermes skill catalog')
        if not isinstance(description, str) or not description.strip():
            # No invented meaning for a skill whose catalog entry lacks a description.
            continue
        skills.append({'name': name, 'description': description})
        seen.add(name)
    return sorted(skills, key=lambda row: row['name'])
