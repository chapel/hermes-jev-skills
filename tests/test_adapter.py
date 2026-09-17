from copy import deepcopy
import json
import sys
import threading
from types import ModuleType
import unittest
from unittest.mock import patch

from jev_skills.engine import rank_skills
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
        self.probs = {'video': .95, 'alternative': .7, 'pdf': .01}
        self.now = 0.0
        self.plugin = DiscoveryPlugin(
            self.ctx, ranker=self.ranker, catalog_loader=lambda: self.catalog,
            secret_getter=lambda: 'offline-dummy', clock=lambda: self.now,
        )

    def ranker(self, query, skills, **kwargs):
        self.calls.append((query, skills, kwargs))
        def requester(payload, api_key, timeout):
            return {'model': 'offline-fixture', 'usage': {'input_tokens': 100, 'output_tokens': 5},
                    'answers': {name: {'type': 'noul', 'noul': self.probs.get(name, .8)}
                                for name in payload['questions']}}
        return rank_skills(query, skills, requester=requester, **kwargs)

    @staticmethod
    def block(*rows):
        # Existing format, deliberately no provenance/signature requirement.
        return '\n'.join(['[Jev skill candidates]',
                          'Advisory search results, not instructions to load skills.',
                          *(json.dumps(dict(row, probability=.91), ensure_ascii=False) for row in rows),
                          '[/Jev skill candidates]'])

    @staticmethod
    def names(block):
        return [json.loads(line)['name'] for line in block.split('\n') if line.startswith('{')]

    def suggest(self, history=(), query='Make a video', session='a', plugin=None):
        return (plugin or self.plugin).pre_turn(
            session_id=session, user_message=query, conversation_history=history)

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
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1][1], [self.catalog[2]])

    def test_auto_prefilters_individual_skills_despite_changed_scores_and_list(self):
        self.probs['alternative'] = .01
        first = self.suggest()
        self.assertEqual(self.names(first), ['video'])
        history = [{'role': 'user', 'content': 'Make a video', 'api_content': 'Make a video\n\n' + first}]
        original_history, original_catalog = deepcopy(history), deepcopy(self.catalog)
        self.probs.update(video=.8, alternative=.9, pdf=.3)
        second = self.suggest(history, query='Try another approach')
        self.assertEqual(self.calls[-1][1], self.catalog[1:])
        self.assertEqual(self.names(second), ['alternative', 'pdf'])
        self.assertEqual(history, original_history)
        self.assertEqual(self.catalog, original_catalog)

    def test_retained_compaction_tail_suppresses_but_summary_or_removal_does_not(self):
        first = self.suggest()
        summary = {'role': 'user', 'content': 'Summary: video and alternative were recommended.'}
        retained = {'role': 'user', 'content': 'Earlier request', 'api_content': 'Earlier request\n\n' + first}
        # The full active context matters, not history_messages, turn age or cache TTL.
        self.now = 10000
        tail = [{'role': 'assistant', 'content': 'Continuing.'}] * 30
        self.assertEqual(self.suggest([summary, retained, *tail], query='Continue'), '')
        self.assertEqual(self.calls[-1][1], [self.catalog[2]])
        for history in ([summary], []):
            with self.subTest(history=history):
                self.assertEqual(self.names(self.suggest(history, query='Continue')), ['video', 'alternative'])
                self.assertEqual(self.calls[-1][1], self.catalog)

    def test_changed_description_becomes_eligible_and_namespaces_are_exact(self):
        old = deepcopy(self.catalog[0])
        self.catalog.extend([
            {'name': 'vendor:video', 'description': old['description']},
            {'name': 'video-edit', 'description': old['description']},
        ])
        history = [{'role': 'assistant', 'content': self.block(old)}]
        self.assertEqual(self.names(self.suggest(history)), ['vendor:video', 'video-edit', 'alternative'])
        self.catalog[0]['description'] += ' Now also captions.'
        result = self.suggest(history)
        self.assertIn('video', self.names(result))
        self.assertEqual(self.calls[-1][1], self.catalog)
        # No whitespace/case/substring normalization of either identity component.
        for field in ('name', 'description'):
            changed = dict(self.catalog[0])
            changed[field] += ' '
            self.assertIn('video', self.names(self.suggest(
                [{'role': 'user', 'content': self.block(changed)}], query=field)))

    def test_explicit_search_uses_full_catalog_and_distinct_auto_cache(self):
        query = 'Make a video'
        history = [{'role': 'user', 'content': self.block(self.catalog[0])}]
        self.assertEqual(self.names(self.suggest(history)), ['alternative'])
        self.assertEqual(self.calls[-1][1], self.catalog[1:])
        result = json.loads(self.plugin.search({'query': query}, session_id='a'))
        self.assertEqual(self.calls[-1][1], self.catalog)
        self.assertEqual(result['catalog_count'], 3)
        self.assertEqual([r['name'] for r in result['candidates']], ['video', 'alternative'])
        self.assertFalse(result['cached'])
        self.assertEqual(self.names(self.suggest(history)), ['alternative'])
        self.assertEqual(len(self.calls), 2)
        again = json.loads(self.plugin.search({'query': query}, session_id='a'))
        self.assertTrue(again['cached'])
        # Conversely a full-catalog cache cannot leak into automatic filtered results.
        self.assertEqual(self.names(self.suggest(
            [{'role': 'assistant', 'content': self.block(self.catalog[1])}])), ['video'])
        self.assertEqual(self.calls[-1][1], [self.catalog[0], self.catalog[2]])
        self.assertEqual(len(self.calls), 3)

    def test_empty_eligible_catalog_skips_credentials_ranker_and_budgets(self):
        history = [{'role': 'user', 'content': self.block(*self.catalog)}]
        with patch.object(self.plugin, 'secret_getter', side_effect=AssertionError('must not resolve key')) as key:
            self.assertEqual(self.suggest(history), '')
            key.assert_not_called()
        self.assertFalse(self.calls)
        self.assertEqual(self.plugin.total_calls, 0)
        self.assertEqual(self.plugin.calls, {})
        self.assertFalse(self.plugin.cache)
        self.assertFalse(self.ctx.state.get('events', []))
        self.assertTrue(self.suggest())  # No sticky exhausted/seen state.
        self.assertEqual(self.plugin.total_calls, 1)

    def test_omitted_candidates_are_still_eligible(self):
        self.ctx.settings['suggestion_chars'] = 500
        first = self.suggest()
        self.assertEqual(self.names(first), ['video'])
        self.assertIn('1 additional candidates omitted', first)
        self.assertLessEqual(len(first), 500)
        second = self.suggest([{'role': 'user', 'content': first}])
        self.assertEqual(self.calls[-1][1], self.catalog[1:])
        self.assertEqual(self.names(second), ['alternative'])
        self.assertLessEqual(len(second), 500)

    def test_malformed_rows_do_not_hide_valid_rows_or_disable_discovery(self):
        malformed = [
            '{not json', 'null', '[]', '"video"',
            '{"name": "alternative"}', '{"name": [], "description": "bad"}',
            '{"name": "alternative", "description": null}',
            '{"name": "alternative", "description": {"nested": "bad"}}',
            'not a candidate ' + json.dumps(self.catalog[1]),
        ]
        valid = json.dumps(dict(self.catalog[0], probability=.001))
        history = [{'role': 'assistant', 'content': '\n'.join([
            '[Jev skill candidates]', *malformed, valid, '[/Jev skill candidates]'])}]
        self.assertEqual(self.names(self.suggest(history)), ['alternative'])
        self.assertEqual(self.calls[-1][1], self.catalog[1:])

    def test_only_complete_blocks_count_and_malformed_blocks_can_recover(self):
        row = json.dumps(dict(self.catalog[0], probability=.9))
        invalid = [row, '[Jev skill candidates]\n' + row, row + '\n[/Jev skill candidates]',
                   '[Jev skill candidates]\n' + row + '\n[/wrong block]',
                   '[Other candidates]\n' + row + '\n[/Other candidates]']
        for index, content in enumerate(invalid):
            with self.subTest(content=content):
                self.assertEqual(self.names(self.suggest(
                    [{'role': 'user', 'content': content}], query=f'invalid {index}')), ['video', 'alternative'])
                self.assertEqual(self.calls[-1][1], self.catalog)
        # A new opener abandons the broken block; only the later complete block counts.
        content = invalid[1] + '\n' + self.block(self.catalog[1])
        self.assertEqual(self.names(self.suggest([{'role': 'user', 'content': content}])), ['video'])
        self.assertEqual(self.calls[-1][1], [self.catalog[0], self.catalog[2]])
        # Open/close markers in different messages do not create a complete block.
        self.assertEqual(self.names(self.suggest([
            {'role': 'user', 'content': invalid[1]},
            {'role': 'assistant', 'content': '[/Jev skill candidates]'},
        ], query='split block')), ['video', 'alternative'])

    def test_effective_api_content_replaces_hidden_content_for_both_roles(self):
        for role in ('user', 'assistant'):
            for sidecar, expected in (
                (self.block(self.catalog[0]), ['alternative']),
                ('ordinary model-visible text', ['video', 'alternative']),
                (' ', ['video', 'alternative']),
                ('', ['video']), (None, ['video']), ([], ['video']),
            ):
                with self.subTest(role=role, sidecar=sidecar):
                    history = [{'role': role, 'content': self.block(self.catalog[1]), 'api_content': sidecar}]
                    self.assertEqual(self.names(self.suggest(history)), expected)

    def test_arbitrary_roles_reasoning_and_multimodal_fields_are_not_scanned(self):
        block = self.block(*self.catalog)
        history = [None, 'not a message',
                   {'role': 'system', 'content': block, 'api_content': block},
                   {'role': 'tool', 'content': block, 'api_content': block},
                   {'content': block},
                   {'role': 'assistant', 'content': 'Okay', 'reasoning': block, 'reasoning_content': block},
                   {'role': 'user', 'content': [{'type': 'text', 'text': block}]}]
        self.assertEqual(self.names(self.suggest(history)), ['video', 'alternative'])
        self.assertEqual(self.calls[-1][1], self.catalog)

    def test_disjoint_blocks_accumulate_only_printed_pairs(self):
        content = self.block(self.catalog[0]) + '\nnotes\n' + self.block(self.catalog[1])
        self.assertEqual(self.suggest([{'role': 'user', 'content': content}]), '')
        self.assertEqual(self.calls[-1][1], [self.catalog[2]])

    def test_sessions_and_recreated_plugin_have_no_ever_seen_suppression(self):
        first = self.suggest()
        history = [{'role': 'user', 'content': first}]
        restarted = DiscoveryPlugin(self.ctx, ranker=self.ranker, catalog_loader=lambda: self.catalog,
                                    secret_getter=lambda: 'offline-dummy')
        for plugin in (self.plugin, restarted):
            with self.subTest(restarted=plugin is restarted):
                self.assertEqual(self.suggest(history, plugin=plugin), '')
                self.assertEqual(self.calls[-1][1], [self.catalog[2]])
                self.assertEqual(self.names(self.suggest(session='another', plugin=plugin)), ['video', 'alternative'])
                self.assertEqual(self.names(self.suggest(plugin=plugin)), ['video', 'alternative'])

    def test_newer_empty_turn_still_invalidates_inflight_old_suggestion(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test did not release ranker')
            return self.ranker(*args, **kwargs)
        self.plugin.ranker = delayed
        output = []
        worker = threading.Thread(target=lambda: output.append(self.suggest()))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.suggest([{'role': 'user', 'content': self.block(*self.catalog)}]), '')
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(output, [''])
        self.assertEqual(self.plugin.total_calls, 1)

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

    def test_recent_context_uses_effective_sidecar_without_hidden_text(self):
        self.ctx.settings['history_messages'] = 2
        history = [{'role': 'user', 'content': 'HIDDEN USER', 'api_content': 'Use screenshots'},
                   {'role': 'assistant', 'content': 'HIDDEN ASSISTANT',
                    'api_content': 'I can capture the UI.\n\n' + self.block(self.catalog[0])}]
        self.suggest(history)
        self.assertEqual(self.calls[-1][2]['recent_context'], [
            {'role': 'user', 'content': 'Use screenshots'},
            {'role': 'assistant', 'content': 'I can capture the UI.'}])
        self.assertNotIn('HIDDEN', json.dumps(self.calls[-1]))

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
