import json
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from jev_skills.hermes_adapter import DiscoveryPlugin


class FakeState:
    data_dir = '/isolated-test/plugin-data/jev-skills'

    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class FakeContext:
    def __init__(self, **settings):
        self.settings = settings
        self.state = FakeState()

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.catalog = [
            {'name': 'video', 'description': 'Create videos with real app screens.'},
            {'name': 'alternative', 'description': 'Generate video with another tool.'},
            {'name': 'pdf', 'description': 'Read and edit PDFs.'},
        ]
        self.ctx = FakeContext(allow_remote=True, auto_suggest=True, record_events=True)
        self.now = 0.0
        self.plugin = DiscoveryPlugin(
            self.ctx, ranker=self.ranker, catalog_loader=lambda: self.catalog,
            secret_getter=lambda: 'offline-dummy', clock=lambda: self.now,
        )

    def ranker(self, query, skills, **kwargs):
        self.calls.append((query, skills, kwargs))
        probs = {'video': .95, 'alternative': .7, 'pdf': .01}
        return {'status': 'ok', 'model': 'offline-fixture', 'usage': {'input_tokens': 100, 'output_tokens': 5},
                'latency_ms': 1, 'scores': [dict(row, probability=probs[row['name']]) for row in skills]}

    def test_search_preserves_plausible_alternatives_and_has_exact_totals(self):
        result = json.loads(self.plugin.search({'query': 'Make a video', 'limit': 1}, session_id='a'))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual([x['name'] for x in result['candidates']], ['video'])
        self.assertEqual(result['matching_count'], 2)
        self.assertTrue(result['has_more'])
        next_page = json.loads(self.plugin.search({'query': 'Make a video', 'offset': 1}, session_id='a'))
        self.assertEqual([x['name'] for x in next_page['candidates']], ['alternative'])
        self.assertEqual(len(self.calls), 1)

    def test_remote_opt_in_and_missing_key_send_nothing(self):
        self.ctx.settings['allow_remote'] = False
        self.assertEqual(json.loads(self.plugin.search({'query': 'video'}))['status'], 'disabled')
        self.assertFalse(self.calls)
        self.ctx.settings['allow_remote'] = True
        plugin = DiscoveryPlugin(self.ctx, ranker=self.ranker, catalog_loader=lambda: self.catalog,
                                 secret_getter=lambda: None)
        self.assertEqual(json.loads(plugin.search({'query': 'video'}))['status'], 'setup_needed')
        self.assertFalse(self.calls)

    def test_turn_suggestions_are_advisory_and_not_repeated(self):
        message = self.plugin.pre_turn(session_id='a', user_message='Make a video', conversation_history=[])
        self.assertIn('video', message)
        self.assertIn('alternative', message)
        self.assertIn('not instructions', message)
        self.assertNotIn('Read and edit PDFs', message)
        history = [{'role': 'user', 'content': 'Make a video', 'api_content': 'Make a video\n\n' + message}]
        self.assertEqual(self.plugin.pre_turn(session_id='a', user_message='Make a video', conversation_history=history), '')
        # After compression removes the earlier suggestion it must be eligible again.
        self.assertEqual(self.plugin.pre_turn(session_id='a', user_message='Make a video', conversation_history=[]), message)
        self.assertEqual(len(self.calls), 1)

    def test_recent_context_is_off_by_default_then_bounded_and_text_only(self):
        history = [
            {'role': 'system', 'content': 'SECRET SYSTEM'},
            {'role': 'tool', 'content': 'SECRET TOOL'},
            {'role': 'user', 'content': 'Use app screenshots'},
            {'role': 'assistant', 'content': 'I can capture the UI.', 'reasoning': 'SECRET REASONING'},
            {'role': 'user', 'content': 'Make a video'},
        ]
        self.plugin.pre_turn(session_id='a', user_message='Make a video', conversation_history=history)
        self.assertFalse(self.calls[-1][2]['recent_context'])
        self.ctx.settings['history_messages'] = 2
        self.plugin.pre_turn(session_id='b', user_message='Make a video', conversation_history=history)
        recent = self.calls[-1][2]['recent_context']
        self.assertEqual(recent, [{'role': 'user', 'content': 'Use app screenshots'},
                                  {'role': 'assistant', 'content': 'I can capture the UI.'}])
        self.assertNotIn('SECRET', json.dumps(recent))

    def test_bare_slash_history_does_not_disable_suggestions_or_consume_window(self):
        self.ctx.settings['history_messages'] = 2
        scaffolds = {
            'skill': '[IMPORTANT: The user has invoked the "video" skill. '
                     'The full skill content is loaded below.]\n\nFULL VIDEO BODY',
            'bundle': '[IMPORTANT: The user has invoked the "video" skill bundle, '
                      'loading 1 skills together.]\n\n'
                      '[Loaded as part of the video skill bundle.]\n\nFULL VIDEO BODY',
        }
        # Mirror Hermes's Optional[str] contract; integration checks the real helper.
        commands = ModuleType('agent.skill_commands')
        setattr(commands, 'extract_user_instruction_from_skill_message',
            lambda text: None if text in scaffolds.values() else text
        )
        expected = [{'role': 'user', 'content': 'Use app screenshots'},
                    {'role': 'assistant', 'content': 'I can capture the UI.'}]
        current = {'role': 'user', 'content': 'Make a video'}
        with patch.dict(sys.modules, {'agent.skill_commands': commands}):
            for kind, scaffold in scaffolds.items():
                bare = {'role': 'user', 'content': scaffold}
                for position, history in (
                    ('older', [bare, *expected, current]),
                    ('recent', [*expected, bare, current]),
                ):
                    with self.subTest(kind=kind, position=position):
                        before = len(self.calls)
                        result = self.plugin.pre_turn(session_id=f'{kind}-{position}',
                            user_message=current['content'], conversation_history=history)
                        self.assertIn('[Jev skill candidates]', result)
                        self.assertEqual(len(self.calls), before + 1)
                        self.assertEqual(self.calls[-1][2]['recent_context'], expected)
                        self.assertNotIn('FULL VIDEO BODY', json.dumps(self.calls[-1]))

    def test_empty_cleaned_messages_do_not_consume_history_window(self):
        self.ctx.settings['history_messages'] = 2
        expected = [{'role': 'user', 'content': 'Use app screenshots'},
                    {'role': 'assistant', 'content': 'I can capture the UI.'}]
        history = [*expected,
                   {'role': 'assistant', 'content': '[Jev skill candidates]\nold suggestions'},
                   {'role': 'user', 'content': 'Make a video'}]
        self.assertEqual(self.plugin.recent_context(history, 'Make a video', self.plugin.settings()), expected)

    def test_errors_are_not_no_matches_and_limits_count_failures(self):
        self.ctx.settings['max_calls_per_session'] = 1
        def failure(*args, **kwargs):
            self.calls.append(args)
            return {'status': 'error', 'error': {'code': 'http_error', 'message': 'Service failed'}}
        self.plugin.ranker = failure
        result = json.loads(self.plugin.search({'query': 'video'}, session_id='a'))
        self.assertEqual(result['status'], 'error')
        again = json.loads(self.plugin.search({'query': 'different'}, session_id='a'))
        self.assertEqual(again['error']['code'], 'call_limit')
        self.assertEqual(len(self.calls), 1)

    def test_config_or_catalog_change_invalidates_cache_and_sessions_are_separate(self):
        self.plugin.search({'query': 'video'}, session_id='a')
        self.plugin.search({'query': 'video'}, session_id='b')
        self.ctx.settings['jev_model'] = 'chosen-model'
        self.plugin.search({'query': 'video'}, session_id='a')
        self.catalog[0]['description'] = 'Changed capability'
        self.plugin.search({'query': 'video'}, session_id='a')
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.calls[-1][2]['model'], 'chosen-model')

    def test_subagents_and_auto_disabled_do_not_send(self):
        self.assertEqual(self.plugin.pre_turn(session_id='child', parent_session_id='parent', user_message='video'), '')
        self.ctx.settings['auto_suggest'] = False
        self.assertEqual(self.plugin.pre_turn(session_id='a', user_message='video'), '')
        self.assertFalse(self.calls)

    def test_local_observation_records_no_request_text(self):
        self.plugin.search({'query': 'PRIVATE REQUEST'}, session_id='a')
        self.plugin.post_tool(tool_name='skill_view', args={'name': 'video'},
                              result=json.dumps({'success': True, 'name': 'video'}), session_id='a')
        events = self.ctx.state.get('events', [])
        self.assertEqual(events[-1]['event'], 'skill_load')
        self.assertEqual(events[-1]['skill'], 'video')
        self.assertNotIn('PRIVATE REQUEST', json.dumps(events))
        self.assertNotIn('offline-dummy', json.dumps(events))


if __name__ == '__main__':
    unittest.main()
