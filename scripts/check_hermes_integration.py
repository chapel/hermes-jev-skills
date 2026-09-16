"""Offline real-Hermes integration check; run with --hermes-source PATH.

A fresh subprocess uses a project-local synthetic HOME, no inherited secrets,
and a fake Jev transport. No main-model request or live inference is made.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def child(source, enabled):
    sys.path.insert(0, str(source))
    # The network is not part of this integration check, including incidental
    # startup probes. Transport replacement below is the only inference boundary.
    import socket
    def no_network(*args, **kwargs):
        raise AssertionError('Offline integration must not open network connections')
    socket.socket.connect = no_network
    socket.create_connection = no_network

    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from run_agent import AIAgent
    from agent.system_prompt import build_system_prompt, _skills_prompt
    from agent.turn_context import _collect_pre_llm_call_context, compose_user_api_content
    from tools.registry import registry
    import tools.skills_tool  # registers the real skill tools

    discover_plugins()
    manager = get_plugin_manager()
    agent = AIAgent(api_key='offline-dummy', base_url='https://example.invalid/v1',
                    model='test/model', provider='openrouter', platform='cli',
                    quiet_mode=True, skip_context_files=True, skip_memory=True,
                    session_id='integration', enabled_toolsets=['skills', 'jev_skills'])
    initial = build_system_prompt(agent)
    index = _skills_prompt(agent)
    assert 'video' in index and 'pdf' in index
    assert 'hidden-skill' not in index
    calls = []
    if enabled:
        entry = registry.get_entry('search_skills')
        assert entry is not None, 'plugin tool was not registered'
        plugin = entry.handler.__self__
        # Use the production engine through its injectable HTTP boundary.
        from importlib import import_module
        engine = import_module(plugin.__class__.__module__.rsplit('.', 1)[0] + '.engine')
        def requester(payload, api_key, timeout):
            calls.append(payload)
            assert api_key == 'offline-dummy'
            assert set(payload['questions']) == {'video', 'pdf'}
            return {'model': 'offline-fixture', 'answers': {
                name: {'type': 'noul', 'noul': .92 if name == 'video' else .01}
                for name in payload['questions']}, 'usage': {'input_tokens': 50, 'output_tokens': 2}}
        plugin.ranker = lambda *a, **kw: engine.rank_skills(*a, requester=requester, **kw)
        plugin.secret_getter = lambda: 'offline-dummy'
        # Exercise internals once for an actionable traceback before registry error handling.
        plugin.settings()
        plugin.scope('integration')
        plugin.catalog_loader()
        result = json.loads(registry.dispatch('search_skills', {'query': 'Make a video'},
                                              session_id='integration', task_id='integration'))
        assert result['status'] == 'ok', result
        assert [r['name'] for r in result['candidates']] == ['video']
        assert result['catalog_count'] == 2
        user = 'Make a video'
        context = _collect_pre_llm_call_context(agent, effective_task_id='integration', turn_id='t1',
            original_user_message=user, messages=[{'role': 'user', 'content': user}], conversation_history=[])
        assert '[Jev skill candidates]' in context, context
        api_content = compose_user_api_content(user, '', context)
        assert api_content.startswith(user + '\n\n')
        assert len(calls) == 1, 'hook should reuse same session/query/catalog result'
        loaded = json.loads(registry.dispatch('skill_view', {'name': 'video'}, task_id='integration'))
        assert loaded['success'] and 'FULL VIDEO BODY' in loaded['content'], loaded
        # Real slash scaffolds return None when invoked without user instructions.
        from agent.skill_commands import (
            _build_skill_message, _scaffold_header, extract_user_instruction_from_skill_message,
        )
        instruction = 'Use app screenshots'
        activation = ('[IMPORTANT: The user has invoked the "video" skill. '
                      'The full skill content is loaded below.]')
        skill = {'content': 'FULL VIDEO BODY'}
        bundle_body = '\n\n[Loaded as part of the video skill bundle.]\n\nFULL VIDEO BODY'
        scaffolds = {
            'skill': (_build_skill_message(skill, None, activation),
                      _build_skill_message(skill, None, activation, user_instruction=instruction)),
            'bundle': (_scaffold_header('"video" skill bundle', ['video']) + bundle_body,
                       _scaffold_header('"video" skill bundle', ['video'],
                                        user_instruction=instruction) + bundle_body),
        }
        expected = [{'role': 'user', 'content': instruction},
                    {'role': 'assistant', 'content': 'I can capture the UI.'}]
        for kind, (bare, instructed) in scaffolds.items():
            assert extract_user_instruction_from_skill_message(bare) is None
            assert extract_user_instruction_from_skill_message(instructed) == instruction
            useful = [{'role': 'user', 'content': instructed}, expected[1]]
            for position, history in (
                ('older', [{'role': 'user', 'content': bare}, *useful]),
                ('recent', [*useful, {'role': 'user', 'content': bare}]),
            ):
                user = f'Make a video ({kind}, {position})'
                before = len(calls)
                context = _collect_pre_llm_call_context(agent, effective_task_id='integration',
                    turn_id=f'{kind}-{position}', original_user_message=user,
                    messages=[*history, {'role': 'user', 'content': user}], conversation_history=history)
                assert '[Jev skill candidates]' in context, (kind, position, context)
                assert len(calls) == before + 1, 'slash history prevented evaluation'
                assert calls[-1]['state'] == {'user_request': user, 'recent_context': expected}, calls[-1]['state']
                assert 'FULL VIDEO BODY' not in json.dumps(calls[-1]), 'slash skill body leaked to Jev'
        assert build_system_prompt(agent) == initial, 'plugin changed the system prompt'
        assert _skills_prompt(agent) == index, 'plugin changed the skill index'
    else:
        assert registry.get_entry('search_skills') is None
    Path(os.environ['RESULT_FILE']).write_text(json.dumps({
        'enabled': enabled, 'index': index, 'network_calls': 0,
        'fake_inference_calls': len(calls), 'passed': True,
    }))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-source', type=Path, required=True)
    parser.add_argument('--child', choices=['enabled', 'disabled'])
    args = parser.parse_args()
    source = args.hermes_source.resolve()
    if args.child:
        child(source, args.child == 'enabled')
        return
    scratch = ROOT / '.scratch'
    scratch.mkdir(exist_ok=True)
    interpreter = source / '.venv/bin/python'
    if not interpreter.is_file():
        parser.error('Expected verified Hermes source with .venv/bin/python')
    results = []
    with tempfile.TemporaryDirectory(prefix='integration-', dir=scratch) as work:
        for enabled in (False, True):
            home = Path(work) / str(enabled)
            hhome = home / '.hermes'
            target = hhome / 'plugins/jev-skills'
            target.mkdir(parents=True)
            for name in ('plugin.yaml', '__init__.py'):
                shutil.copy2(ROOT / name, target / name)
            shutil.copytree(ROOT / 'jev_skills', target / 'jev_skills', ignore=shutil.ignore_patterns('__pycache__'))
            descriptions = {'video': 'Create videos from web apps.', 'pdf': 'Read PDF documents.',
                            'hidden-skill': 'A disabled skill.'}
            for name, desc in descriptions.items():
                folder = hhome / 'skills' / name
                folder.mkdir(parents=True)
                (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: {desc}\n---\nFULL {name.upper()} BODY\n')
            config = {'plugins': {'enabled': ['jev-skills'] if enabled else [], 'entries': {
                'jev-skills': {'settings': {'allow_remote': True, 'auto_suggest': True,
                                           'history_messages': 2}}}},
                'skills': {'disabled': ['hidden-skill']}, 'memory': {'memory_enabled': False,
                'user_profile_enabled': False}, 'terminal': {'cwd': str(home)}}
            (hhome / 'config.yaml').write_text(json.dumps(config))
            result_file = home / 'result.json'
            env = {'HOME': str(home), 'HERMES_HOME': str(hhome), 'TMPDIR': str(home),
                   'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8',
                   'RESULT_FILE': str(result_file), 'PYTHONDONTWRITEBYTECODE': '1'}
            proc = subprocess.run([str(interpreter), str(Path(__file__).resolve()), '--hermes-source', str(source),
                                   '--child', 'enabled' if enabled else 'disabled'], cwd=home,
                                  env=env, text=True, capture_output=True, timeout=90)
            if proc.returncode:
                print(proc.stdout + proc.stderr)
                raise SystemExit(proc.returncode)
            results.append(json.loads(result_file.read_text()))
    assert results[0]['index'] == results[1]['index'], 'enabling plugin changed baseline skills index'
    print(json.dumps({'passed': True, 'disabled_and_enabled_discovery': True,
                      'unchanged_skill_index': True, 'unchanged_system_prompt_during_use': True,
                      'normal_skill_view': True, 'hook_user_context': True,
                      'slash_history_regression': True,
                      'fake_inference_calls': results[1]['fake_inference_calls'], 'live_requests': 0}, indent=2))


if __name__ == '__main__':
    main()
