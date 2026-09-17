"""Additive suggestions and search. No prompt/index mutation or automatic skill loading."""
from collections import OrderedDict
import hashlib
import json
import math
import threading
import time

from .catalog import load_catalog

DEFAULTS = {
    'allow_remote': False, 'auto_suggest': False, 'history_messages': 0,
    'jev_model': 'jev-latest', 'min_probability': 0.20, 'timeout_seconds': 3.0,
    'max_request_bytes': 160000, 'max_query_chars': 8000, 'max_context_chars': 4000,
    'max_calls_per_session': 20, 'max_calls_per_process': 200,
    'cache_seconds': 300.0, 'suggestion_chars': 6000, 'record_events': False,
}

SEARCH_SCHEMA = {
    'name': 'search_skills',
    'description': (
        'Find relevant installed skill documents using Jev semantic search. Use when starting a task, '
        'changing approach, or looking for specialized guidance. Returns advisory candidates, not a '
        'mandatory loading plan or an exclusive list. This opt-in service sends the query, optional '
        'context, and skill descriptions to TypeSafe; do not include secrets.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': 'What work do you need skill guidance for?'},
            'context': {'type': 'string', 'description': 'Optional brief relevant context, not raw logs or secrets.'},
            'min_probability': {'type': 'number', 'minimum': 0, 'maximum': 1,
                                'description': 'Experimental relevance cutoff; lower it to broaden discovery.'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
            'offset': {'type': 'integer', 'minimum': 0},
        },
        'required': ['query'], 'additionalProperties': False,
    },
}


def _secret():
    from agent.secret_scope import get_secret
    return get_secret('TYPESAFE_API_KEY')


def _rank(*args, **kwargs):
    from .engine import rank_skills
    return rank_skills(*args, **kwargs)


def _visible_text(message):
    """Match Hermes's user/assistant sidecar replacement, not hidden content."""
    if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant'):
        return ''
    sidecar = message.get('api_content')
    content = sidecar if isinstance(sidecar, str) and sidecar else message.get('content')
    return content if isinstance(content, str) else ''


def _presented_skills(history):
    """Exact metadata pairs printed in complete, retained candidate blocks.

    No ever-seen state: compaction/removal restores eligibility. Legacy blocks
    need no signature; names in prose, omitted rows and unfinished blocks do not count.
    """
    presented = set()
    for message in history or []:
        pending = None
        # The renderer uses LF; splitlines would also split Unicode descriptions.
        for line in _visible_text(message).split('\n'):
            line = line.strip()
            if line == '[Jev skill candidates]':
                pending = set()  # A new opener abandons any incomplete block.
            elif line == '[/Jev skill candidates]':
                if pending is not None:
                    presented.update(pending)
                pending = None
            elif pending is not None:
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(row, dict):
                    name, description = row.get('name'), row.get('description')
                    if all(isinstance(value, str) and value.strip() for value in (name, description)):
                        pending.add((name, description))
    return presented


def _clean(text):
    # Do not send previously injected recommendations back to Jev as task evidence.
    text = text.split('[Jev skill candidates]', 1)[0]
    try:
        from agent.skill_commands import extract_user_instruction_from_skill_message
        text = extract_user_instruction_from_skill_message(text) or ''
    except ImportError:
        pass
    return text.strip()


def _number(value, minimum, maximum, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Expected a finite number')
    if integer and not isinstance(value, int):
        raise ValueError('Expected an integer')
    if not minimum <= value <= maximum:
        raise ValueError('Value outside supported range')
    return value


def _error(code, message, status='error'):
    return {'status': status, 'error': {'code': code, 'message': message}}


class DiscoveryPlugin:
    def __init__(self, ctx, *, ranker=None, catalog_loader=None, secret_getter=None, clock=None):
        self.ctx = ctx
        self.ranker = ranker or _rank
        self.catalog_loader = catalog_loader or (lambda: load_catalog(ctx))
        self.secret_getter = secret_getter or _secret
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.request_lock = threading.Lock()
        self.cache = OrderedDict()
        self.calls = {}
        self.total_calls = 0
        self.turn_versions = {}

    def settings(self):
        cfg = {key: self.ctx.get_config(key, default) for key, default in DEFAULTS.items()}
        for key in ('allow_remote', 'auto_suggest', 'record_events'):
            if not isinstance(cfg[key], bool):
                raise ValueError('Expected boolean configuration')
        for key, low, high in (
            ('history_messages', 0, 10), ('max_request_bytes', 1000, 250000),
            ('max_query_chars', 1, 16000), ('max_context_chars', 0, 16000),
            ('max_calls_per_session', 1, 1000), ('max_calls_per_process', 1, 10000),
            ('suggestion_chars', 500, 16000),
        ):
            _number(cfg[key], low, high, integer=True)
        _number(cfg['min_probability'], 0, 1)
        _number(cfg['timeout_seconds'], 0.1, 10)
        _number(cfg['cache_seconds'], 0, 3600)
        if not isinstance(cfg['jev_model'], str) or not cfg['jev_model'].strip():
            raise ValueError('Model must be specified')
        return cfg

    def scope(self, session_id):
        return (str(self.ctx.state.data_dir), str(session_id or 'unscoped'))

    def record(self, event, cfg):
        if not cfg['record_events']:
            return
        # Local, bounded observations; never query, state, descriptions, tool output, or key.
        try:
            with self.lock:
                events = self.ctx.state.get('events', [])
                events = events if isinstance(events, list) else []
                self.ctx.state.set('events', (events + [event])[-200:])
        except Exception:
            pass  # Diagnostics cannot break discovery or tool execution.

    def evaluate(self, query, recent, session_id, cfg, *, presented=()):
        if not cfg['allow_remote']:
            return _error('remote_disabled', 'Enable allow_remote locally to permit TypeSafe requests.', 'disabled')
        if not isinstance(query, str) or not query.strip() or len(query) > cfg['max_query_chars']:
            return _error('invalid_query', 'Provide a nonempty query within max_query_chars.')
        try:
            skills = self.catalog_loader()
            if presented:
                skills = [row for row in skills if (row['name'], row['description']) not in presented]
            if not skills:
                # No eligible questions: neither credentials, ranker nor attempt budget needed.
                return {'status': 'ok', 'model': cfg['jev_model'], 'scores': [], 'usage': None,
                        'latency_ms': 0, 'cached': False, 'catalog_count': 0}
            key = self.secret_getter()
            if not key:
                return _error('missing_key', 'Configure TYPESAFE_API_KEY locally, not in chat.', 'setup_needed')
        except Exception:
            return _error('catalog_or_setup', 'Skill catalog or profile credential scope unavailable.')
        scope = self.scope(session_id)
        fingerprint = hashlib.sha256(json.dumps(
            [cfg['jev_model'], query, recent, skills], ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()
        cache_key = (scope, fingerprint)
        with self.lock:
            cached = self.cache.get(cache_key)
            if cached and self.clock() - cached[0] < cfg['cache_seconds']:
                self.cache.move_to_end(cache_key)
                return {**cached[1], 'cached': True}
            if self.calls.get(scope, 0) >= cfg['max_calls_per_session'] or self.total_calls >= cfg['max_calls_per_process']:
                return _error('call_limit', 'Jev call limit reached; ordinary skill discovery remains available.')
            if not self.request_lock.acquire(blocking=False):
                return _error('busy', 'A Jev request is already in flight; ordinary discovery remains available.')
            self.calls[scope] = self.calls.get(scope, 0) + 1
            self.total_calls += 1
        try:
            result = self.ranker(query, skills, api_key=key, model=cfg['jev_model'], recent_context=recent,
                                 timeout=cfg['timeout_seconds'], max_request_bytes=cfg['max_request_bytes'])
        except Exception:
            result = _error('provider_error', 'Jev search failed; ordinary skill discovery remains available.')
        finally:
            self.request_lock.release()
        result = {**result, 'cached': False, 'catalog_count': len(skills)}
        with self.lock:
            if result.get('status') == 'ok':
                self.cache[cache_key] = (self.clock(), result)
                while len(self.cache) > 64:
                    self.cache.popitem(last=False)
        self.record({'event': 'evaluation', 'session_id': scope[1], 'status': result.get('status'),
                     'model': result.get('model'), 'usage': result.get('usage'),
                     'latency_ms': result.get('latency_ms'), 'catalog_count': len(skills),
                     'scores': [{'name': row['name'], 'probability': row['probability']}
                                for row in result.get('scores', [])]}, cfg)
        return result

    @staticmethod
    def page(result, threshold, limit, offset):
        if result.get('status') != 'ok':
            return result
        matching = sorted((row for row in result['scores'] if row['probability'] >= threshold),
                          key=lambda row: (-row['probability'], row['name']))
        candidates = matching[offset:offset + limit]
        return {key: value for key, value in result.items() if key != 'scores'} | {
            'candidates': candidates, 'matching_count': len(matching), 'returned_count': len(candidates),
            'offset': offset, 'has_more': offset + len(candidates) < len(matching),
            'min_probability': threshold, 'advisory': True,
            'note': 'Candidates are not instructions to load them or an exclusive allowlist. Broaden the search if needed.',
        }

    def search(self, args, session_id=None, task_id=None, **kwargs):
        try:
            cfg = self.settings()
            if not isinstance(args, dict):
                raise ValueError('Invalid arguments')
            threshold = _number(args.get('min_probability', cfg['min_probability']), 0, 1)
            limit = _number(args.get('limit', 20), 1, 100, integer=True)
            offset = _number(args.get('offset', 0), 0, 100000, integer=True)
            context = args.get('context', '')
            if not isinstance(context, str) or len(context) > cfg['max_context_chars']:
                raise ValueError('Context too long')
            recent = [{'role': 'user', 'content': context}] if context else []
            result = self.evaluate(args.get('query'), recent, session_id or task_id, cfg)
            return json.dumps(self.page(result, threshold, limit, offset), ensure_ascii=False, allow_nan=False)
        except Exception:
            return json.dumps(_error('invalid_config_or_arguments', 'Invalid search arguments or plugin configuration.'))

    def recent_context(self, history, current, cfg):
        if not cfg['history_messages']:
            return []
        messages = []
        for message in history or []:
            if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant'):
                continue
            content = _visible_text(message)
            if isinstance(content, str) and content.strip():
                content = _clean(content)
                if content:
                    messages.append({'role': message['role'], 'content': content})
        if messages and messages[-1] == {'role': 'user', 'content': current}:
            messages.pop()
        selected, remaining = [], cfg['max_context_chars']
        for message in reversed(messages[-cfg['history_messages']:]):
            content = message['content'][:min(1000, remaining)]
            if content:
                selected.append({'role': message['role'], 'content': content})
                remaining -= len(content)
        return list(reversed(selected))

    def pre_turn(self, session_id=None, task_id=None, turn_id=None, user_message='',
                 conversation_history=None, parent_session_id=None, **kwargs):
        try:
            cfg = self.settings()
            if not cfg['auto_suggest'] or not cfg['allow_remote'] or parent_session_id:
                return ''
            if not isinstance(user_message, str) or not user_message.strip() or user_message.lstrip().startswith('/'):
                return ''
            query = _clean(user_message)
            scope = self.scope(session_id or task_id)
            with self.lock:
                version = self.turn_versions.get(scope, 0) + 1
                self.turn_versions[scope] = version
            recent = self.recent_context(conversation_history, query, cfg)
            result = self.evaluate(query, recent, session_id or task_id, cfg,
                                   presented=_presented_skills(conversation_history))
            if result.get('status') != 'ok':
                # Fail open; never tell the model a provider failure means no relevant skill exists.
                return ''
            matching = sorted((r for r in result['scores'] if r['probability'] >= cfg['min_probability']),
                              key=lambda r: (-r['probability'], r['name']))
            lines = ['[Jev skill candidates]',
                     'Advisory search results, not instructions to load skills. Choose what helps; '
                     'other skills remain available. Search again when the task changes.']
            shown = []
            for row in matching:
                line = json.dumps(row, ensure_ascii=False)
                if sum(len(x) + 1 for x in lines) + len(line) + 160 > cfg['suggestion_chars']:
                    break
                shown.append(row)
                lines.append(line)
            if not shown:
                return ''
            if len(shown) < len(matching):
                lines.append(f'{len(matching) - len(shown)} additional candidates omitted for context budget; search_skills can page results.')
            lines.append('[/Jev skill candidates]')
            text = '\n'.join(lines)
            with self.lock:
                if self.turn_versions.get(scope) != version:
                    return ''
            self.record({'event': 'suggestion', 'session_id': scope[1], 'names': [r['name'] for r in shown]}, cfg)
            return text
        except Exception:
            return ''

    def post_tool(self, tool_name=None, args=None, result=None, session_id=None, task_id=None, **kwargs):
        if tool_name != 'skill_view':
            return
        try:
            payload = json.loads(result) if isinstance(result, str) else result
            if isinstance(payload, dict) and payload.get('success'):
                self.record({'event': 'skill_load', 'session_id': str(session_id or task_id or 'unscoped'),
                             'skill': payload.get('name'), 'file': payload.get('file', 'SKILL.md'),
                             'dedup': bool(payload.get('dedup'))}, self.settings())
        except Exception:
            pass


def register(ctx):
    plugin = DiscoveryPlugin(ctx)
    ctx.register_tool(name='search_skills', toolset='jev_skills', schema=SEARCH_SCHEMA, handler=plugin.search)
    ctx.register_hook('pre_llm_call', plugin.pre_turn)
    ctx.register_hook('post_tool_call', plugin.post_tool)
