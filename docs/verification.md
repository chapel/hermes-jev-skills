# Initial verification

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
