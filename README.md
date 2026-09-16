# Jev skill discovery for Hermes

An opt-in, standalone native Hermes plugin that adds **semantic skill search and advisory turn-start suggestions** using TypeSafe AI's Jev. The main agent decides which skills to load.

This first version deliberately leaves the system prompt, existing skills index, `skills_list`, and `skill_view` unchanged. It does not load full skill bodies, replace tool results, rewrite provider requests, or edit Hermes core. It is an additive experiment, not a claim of improved task success.

## What it does

- Registers `search_skills(query, context?, min_probability?, limit?, offset?)`.
- Optionally suggests candidates at the beginning of each ordinary user turn. No automatic requests for subagents or slash commands; no per-tool-loop automatic suggestions yet.
- Reads names and descriptions from Hermes's active `skills_list`, not a separately maintained catalog. It uses the longer returned descriptions rather than the short system-index entries. Hermes currently caps these descriptions at 1,024 characters. This catalog is not guaranteed identical to the prompt index's conditional tool filtering.
- Asks one independent **Noul** question per skill, containing that skill's name and description. State holds the request and optional recent context, not an opaque catalog that questions reference by index.
- Returns raw yes probabilities, descriptions, and explicit pagination totals. Several alternatives can be relevant. A Noul probability is not a calibrated probability of task success or permission to load anything.
- If enabled, uses Hermes's `pre_llm_call` hook to append a bounded candidate block to the current user message's API representation. The system prompt remains unchanged.
- On failure, injects nothing; ordinary discovery remains available. Explicit searches return a distinguishable error, never false “no matches.”

## Install locally, initially disabled

Requires Hermes's native Python plugin APIs (`register_tool`, `pre_llm_call`, `post_tool_call`, `ctx.dispatch_tool`, profile-scoped secrets). Initial target: Hermes 0.21.1, source SHA `bba60eb66b3518f7ee196df3904c45841f73d19b`. Runtime code uses only Python's standard library and Hermes's own APIs. No SDK or package install is needed.

Copy **only** `plugin.yaml`, `__init__.py`, and the `jev_skills/` directory into the intended profile's `plugins/jev-skills/`. Do not copy `.venv`, `.env`, or test artifacts. For the default profile:

```sh
mkdir -p ~/.hermes/plugins/jev-skills
cp plugin.yaml __init__.py ~/.hermes/plugins/jev-skills/
cp -R jev_skills ~/.hermes/plugins/jev-skills/
```

Inspect the code before enabling: native Hermes plugins are trusted Python, not sandboxed. Do not overwrite a previously installed plugin without reviewing its version. For a published Git repository, Hermes also supports pinned Git plugin installation with `--ref <full-commit> --no-enable`.

Store `TYPESAFE_API_KEY` locally in the intended Hermes profile's `.env` or supported secret configuration. Never paste it into a chat, tool query, source file, or YAML settings. The plugin resolves it through Hermes's profile-aware `get_secret` for each evaluation.

Enabling the plugin and remote requests are separate opt-ins. Merge this example into that profile's config; do not replace unrelated configuration:

```yaml
plugins:
  enabled:
    - jev-skills  # preserve other enabled plugins
  entries:
    jev-skills:
      settings:
        allow_remote: true
        auto_suggest: true
        history_messages: 0
        jev_model: jev-latest
        min_probability: 0.20
        record_events: false
```

Alternatively enable the installed plugin with `hermes plugins enable jev-skills`, then configure its settings. A new Hermes process/session is the clean initial test; a running gateway may require a separately approved restart. This repository does not install itself or alter a running gateway.

If your toolset configuration restricts tools, enable the `jev_skills` toolset in the intended platform. When Hermes defers the tool, the agent can discover `search_skills` via `tool_search`, load its schema with `tool_describe`, and invoke it with `tool_call`.

Disable remote calls with `allow_remote: false`; disable just turn-start suggestions with `auto_suggest: false`. Disable the whole plugin with `hermes plugins disable jev-skills` and start a fresh process as needed.

## Using search

```json
{"query":"Make a video showing how my web app works","limit":20}
```

For follow-ups with otherwise unclear meaning:

```json
{"query":"Add captions","context":"We are producing a product demo video from screen recordings."}
```

A result contains `candidates`, `matching_count`, `returned_count`, `has_more`, `offset`, the requested threshold, returned model, usage when supplied, local latency, and cache status. Repeat the same query/context with another `offset` to page; lower `min_probability` to broaden it. These operations reuse the same cached evaluation within its lifetime. `min_probability: 0` retains every skill in the catalog.

The default cutoff **0.20 is provisional and broad**, not a measured optimal threshold. It is a presentation filter, not an access restriction. The ordinary index is still visible. Optional suggestion blocks are character-bounded and state how many candidates were omitted; search can page through them. Full descriptions remain metadata: neither they nor the recommendations override user instructions or skill-loading policy.

## Privacy, limits, and failure behavior

By default both `allow_remote` and `auto_suggest` are false. Merely importing or enabling the plugin sends nothing.

With remote search enabled, TypeSafe receives **the query, optional supplied context, and all eligible catalog names/descriptions**. Turn-start suggestions send the current user text. No automatic secret redaction is promised; do not enable this on conversations whose text must not leave the main provider. A private skill description is also transmitted metadata.

`history_messages` defaults to 0. Setting it to 1–10 explicitly allows bounded recent user/assistant plain-text messages to be transmitted automatically. It excludes system/tool messages, separate reasoning fields, multimodal blocks, prior Jev injections, and expanded slash-skill bodies where Hermes's extraction helper recognizes them. This is selective context, **not a DLP system**: assistant/user text can itself contain sensitive content. Explicit tool `context` is sent when supplied regardless of `history_messages`.

Defaults and scope:

- `timeout_seconds: 3.0`: network socket timeout, configurable 0.1–10 seconds. Not a hard wall-clock cancellation guarantee; DNS and a trickling server may exceed it. Hermes separately bounds its hook worker wait.
- `max_request_bytes: 160000`: reject an oversized serialized payload, never silently omit catalog entries. Configurable up to 250000.
- `max_query_chars: 8000`; `max_context_chars: 4000`. Oversized explicit input is rejected. Automatic history clips each selected message to at most 1000 characters within the shared context budget.
- `max_calls_per_session: 20`, `max_calls_per_process: 200`: in-memory attempt limits, including failures. They reset on process restart; they are not durable dollar/spend limits. Multiple processes have independent limits.
- One request in flight per plugin instance; a concurrent attempt fails open rather than queuing or spawning unbounded work. There are no automatic retries or redirects. Response size is bounded.
- `cache_seconds: 300.0`: at most 64 successful evaluations, keyed by profile/session, model, request, context, and catalog metadata. No cache persisted to disk. Changing threshold or pagination does not trigger inference. The `jev-latest` alias can change server-side during a cache lifetime; configure an account-supported pinned model for controlled comparisons.
- `suggestion_chars: 6000`: maximum advisory block budget, independent from ranking. Identical blocks already present in active history are not reinjected; after compaction removes them, they can appear again.

Errors such as missing credentials, provider failures, invalid response shapes, input limits, or call limits are not negative classifications. Live service responses are validated before ranking. API errors are sanitized rather than returning response bodies that might echo input or credentials.

## Local observations and evaluation

`record_events: true` stores a rolling window of up to 200 local events in Hermes's profile-scoped plugin state. Events include evaluation status/model/usage/latency and skill-name probabilities, emitted suggestion names, and observed successful `skill_view` loads (including dedup markers). It does not store raw queries, context, descriptions, API keys, or full tool output. Session IDs and skill names are still local metadata. State writes are best-effort diagnostics, not a transactionally complete cross-process audit.

These observations show **suggested versus loaded**, not “used correctly.” To decide whether the plugin helps, compare baseline and additive sessions on the same representative tasks. Inspect appropriate skill discovery, important omissions, unnecessary loads, final task success, extra latency and tokens. Include vague follow-ups, no-match requests, alternative approaches, provider errors, and changes of task. Do not infer improved skill use from a good-looking ranking alone. Only after that evidence should index removal become a separate opt-in experiment.

## Verification

Offline unit tests (no third-party dependencies):

```sh
python3 -m unittest discover -s tests -v
```

Integration against a verified local Hermes checkout/interpreter:

```sh
python3 scripts/check_hermes_integration.py --hermes-source /path/to/hermes-source
```

The integration runner uses a project-local scratch HOME, a real plugin loader/catalog/registry/hook path and `skill_view`, a fake external Jev transport, and no inherited credentials. It checks disabled discovery, unchanged index, no system-prompt mutation during use, user-message suggestions, cache reuse, and normal full-skill loading. It does not call a main model or prove a real model chooses better skills.

See [initial verification](docs/verification.md) for the bounded live smoke results and limitations. `scripts/smoke_live.py` makes no requests without `--live`; its opt-in mode sends at most three synthetic queries plus a reviewed catalog snapshot. It needs a key in its process environment, `--catalog <skills-list.json>`, and `--output <local-results.json>`.

**Backend caveat:** Hermes's inspected `codex_app_server` path does not deliver normal pre-turn hook context through the same API composition path. Leave `auto_suggest` off there until separately verified; this initial compatibility proof targets the ordinary Hermes request path.

## Sources and scope

- [Hermes plugin documentation](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/)
- [TypeSafe documentation index](https://docs.typesafe.ai/llms.txt)
- [Noul primitive](https://docs.typesafe.ai/primitives/noul.md)
- [API](https://docs.typesafe.ai/api.md)

This version intentionally has no replacement skills index, rewritten skill library, embedding index, background service, provider-specific request rewriting, automatic full-skill loading, or general within-turn event classifier.
