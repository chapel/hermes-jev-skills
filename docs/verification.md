# Verification

## Scope

Target runtime: Hermes 0.21.1, source commit `bba60eb66b3518f7ee196df3904c45841f73d19b`.

The default-profile plugin was **not installed or enabled** for these checks. No Hermes core edits, gateway restart, or main-model request was performed. Installation/activation is a separate step.

## Offline checks

- `python -m unittest discover -s tests -v`: **37 tests passed**, including the bare slash-invocation history regression.
- `python scripts/check_hermes_integration.py --hermes-source <verified-source>`: passed with isolated synthetic HOME/config, real plugin discovery and tool registry, real skill catalog, real hook collector and user-message composition, and real full-body `skill_view`.
- The integration compared baseline and plugin-enabled skill indexes for equality and verified no system-prompt mutation during plugin use. No network connection was permitted; Jev responses were explicit test fixtures.
- Disabled-plugin discovery did not expose `search_skills`.
- `python scripts/smoke_live.py` without `--live` sent nothing.
- Independent review found one bare slash-history handling defect. After a focused fix, independent closure review approved the change and reran all 37 tests plus the real-Hermes integration (5 fake evaluations, zero live requests).

Integration testing exposed that Hermes rejects a plugin-relative `model` configuration key as reserved. The plugin uses `jev_model`. Payload sizing also exposed excess repeated instructions: compact self-contained questions now fit the 203-entry snapshot under the default byte limit without dropping descriptions.

## Context-based per-skill deduplication

This follow-up was verified offline in the standalone plugin worktree, without installing,
enabling, configuring, or restarting a live plugin or Hermes process. The source at
`/home/chapel/.hermes/releases/hermes-agent-stable-363be842494a` reports Git HEAD
`bba60eb66b3518f7ee196df3904c45841f73d19b`; the directory suffix is not its Git identity.

All commands ran with an empty inherited environment, project-local `.scratch` HOME/TMPDIR,
`PATH=/usr/bin:/bin`, `LANG=C.UTF-8`, and `PYTHONDONTWRITEBYTECODE=1`. The outer interpreter was
`/home/chapel/Projects/hermes-jev-skills/.venv/bin/python`; integration children use the
verified Hermes source's `.venv/bin/python` and separate synthetic homes.

### Initial deduplication RED → GREEN

- Before implementation, `python -m unittest discover -s tests -p test_adapter.py -v`
  ran 24 tests and reported **21 failing assertions/subtests**. Failures showed already-presented
  skills still reaching the ranker, repeated rows with changed scores/lists, omitted rows blocked
  by whole-block deduplication, and credential lookup on an empty eligible catalog. That
  initial run also included an incorrect expectation that outbound recent context should
  use sidecars; the privacy review below replaces that expectation with a regression.
- Before implementation, the extended real-Hermes integration failed because the second
  hook request still contained both `video` and `pdf` questions when retained context had
  already presented `video`; only `pdf` should have been evaluated.
- After implementation, `python -m unittest discover -s tests -v`: **51 tests passed**.
  Adapter tests exercise the real engine with an injected external evaluation response.
- `python scripts/check_hermes_integration.py --hermes-source
  /home/chapel/.hermes/releases/hermes-agent-stable-363be842494a`: **passed**, with
  **12 fake evaluations and zero live requests**. Network connects are blocked, including
  Hermes's incidental metadata startup probe.

Coverage includes exact `(name, description)` identity regardless of score/order; separate
namespaces and overlapping alternatives; changed descriptions; explicit full-catalog search
and cache partitioning; omitted rows remaining eligible; empty eligibility skipping the
ranker and budgets; complete/incomplete blocks, malformed rows and multiple blocks; effective
user/assistant sidecars and ignored system/tool/reasoning fields; unchanged caller history
and catalog; context removal versus retained tails; summary-only mentions; recreated plugin
instances and session isolation; and the existing stale-turn result guard.

The integration uses the actual registry, hook collector, catalog, ranking engine,
`compose_user_api_content`, `build_api_messages`, and `drop_stale_api_content`. It verifies
that retained recommendations prefilter the provider payload, that full removal restores
eligibility, and that system-prompt and skills-index bytes stay unchanged.

### Privacy review correction: observed RED → GREEN

The sole P1 finding in `4326fea` was that `recent_context` read effective `api_content`
rather than raw `content`. Hermes composes raw text, then recalled memory, then plugin
context. Stripping from the Jev marker therefore left earlier memory and other-plugin
injections in the outbound TypeSafe state when history sharing was enabled.

- **RED, before the fix:** `python -m unittest discover -s tests -p test_adapter.py
  -k test_sidecars_deduplicate_locally_without_sending_injected_context -v` ran one test
  with **two failing subtests**, one per user/assistant role. The fake external transport
  received `SYNTHETIC_PRIVATE_MEMORY` and `SYNTHETIC_OTHER_PLUGIN_CONTEXT` in
  `state.recent_context` instead of exactly the raw text. The retained `video` question
  was already absent, proving local deduplication worked while privacy failed.
- **RED, real Hermes:** the integration command above failed its exact outbound-state
  assertion with a real `<memory-context>` block and both synthetic markers still present.
  It used `compose_user_api_content`, the real hook collector and engine, and fake transport;
  no live service was contacted.
- **Fix:** only outbound history selection returns to `message.get('content')`.
  `_visible_text` still supplies effective sidecars for the separate local deduplication scan.
  The incorrect sidecar-sharing test and documentation were replaced, not preserved as policy.
- **GREEN:** `python -m unittest discover -s tests -v`: **51 tests passed**, including
  both privacy subtests. The real-Hermes integration passed with **14 fake evaluations
  and zero live requests** under the same isolated environment described above.

The regression keeps an eligible question so an actual fake-transport call is required.
For both user and assistant sidecars it asserts exact raw outbound state, absence of the
private markers and Jev block from the entire payload, absence of the retained `video`
question, and continued delivery of another eligible candidate. Local model-visible
deduplication evidence is deliberately broader than authorized remote task evidence.

**Evidence ceiling:** compaction boundaries and rewritten summary content are synthetic;
only the sidecar invalidation helper is executed, not an LLM compression call or full
conversation lifecycle. These checks prove the ordinary hook/sidecar path, not all provider
backends or improved agent decisions. Legacy candidate blocks have no provenance signature;
recognition is format-based, not authentication. No new live inference was used for this change.

## Bounded live smoke

Three physical inference requests, zero retries, no conversation history. Inputs were synthetic task strings plus the reviewed 203-entry local skill-name/description snapshot. Each response contained all 203 expected Nouls, validated before ranking. Pagination reused the adapter's cache.

Requested alias: `jev-latest`. Returned model for all three: `jev-1.13.0`.

### Web-app video

Query: “Make me a video about my webapp”

- Highest: `media-production-workflows` 0.97; `hyperframes-production` 0.95; `video-generation-workflows` 0.95; `hyperframes` 0.91.
- `comfyui`: 0.84, retained as an alternative.
- 40 candidates at the provisional 0.20 cutoff, before suggestion character-budget limits.
- Measured engine/network time: 405.0 ms.
- Reported usage: 27,544 input / 4,269 output tokens.

### Scanned PDF to Excel

Query: “Extract tables from this scanned PDF into an Excel workbook.”

- Highest: `pdf` 0.90; `ocr-and-documents` 0.86; `xlsx` 0.77.
- Broad helpers also scored well: `autonomous-coding-cli-agents` 0.74, `computer-use` 0.69. These are not automatically loaded and should be examined in a task-quality comparison rather than declared correct or wrong solely from the ranking.
- 17 candidates at 0.20.
- Measured engine/network time: 315.9 ms.
- Reported usage: 27,547 input / 4,269 output tokens.

### No further task

Query: “Thanks, that answers my question. Nothing else to do.”

- Highest probability: 0.06. Zero candidates at 0.20.
- Measured engine/network time: 499.4 ms.
- Reported usage: 27,548 input / 4,269 output tokens.

Total reported usage: **82,639 input / 12,807 output tokens**. No cost estimate is claimed. Local latency includes request preparation, network and validation; it is not server-only evaluation time, full-agent latency, or a representative latency benchmark.

## What remains unproven

- Whether suggestions improve a real agent's skill discovery, appropriate loading, or final task success versus the existing index alone.
- Recall or calibration of the provisional 0.20 cutoff.
- Production failure rate, latency distribution, pricing, or scale beyond this catalog.
- Other Hermes versions and execution backends. In particular, the inspected `codex_app_server` path bypasses normal hook-context API composition: do not enable automatic suggestions there expecting equivalent delivery; on-demand tool support needs backend-specific verification.

The additive version intentionally keeps ordinary discovery intact so these questions can be tested without turning a ranking omission into a denied capability.
