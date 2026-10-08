# AGENTS.md

Guidance for agents (and humans) working in this repository. User-facing and
deployment documentation lives in `README.md`; this file covers how to work
in the codebase effectively.

## What this is

pelocore bridges Peloton workouts into COROS: pulls workouts from the
Peloton API, builds Garmin FIT files with `fit_tool`, uploads them to COROS
via its unofficial Training Hub protocol, and tracks state. Runs as a CLI,
a long-running service with a status page, a Docker container, or a k8s
CronJob/Deployment.

## Toolchain

Everything goes through mise (pins python 3.13, uv, ruff, act; 1Password CLI
may come from your `.mise.local.toml`):

```
mise run install        # uv sync --all-groups (alias: i)
mise run test           # pytest
mise run lint           # ruff check src tests
mise run typecheck      # mypy (strict)
mise run act-ci         # run CI workflow locally via act (needs Docker)
```

Direct interpreter access: `uv run python ...`. Config: `mise.toml` (tracked)
plus `.mise.local.toml` (git-ignored; see `.mise.local.toml.example`).

Before every commit: `mise run lint && mise run typecheck && mise run test`.
Commit and push only when the user asks.

## House rules

- **No em dashes. Ever. Anywhere.** Docs, code, comments, commit messages.
  Use a hyphen, comma, or restructure the sentence.
- No code comments unless asked; keep existing docstrings accurate.
- Never commit secrets. Credentials live in `.mise.local.toml` as 1Password
  `op://` references resolved with `op run --` (see
  `.mise.local.toml.example`).
- Live syncs hit real Peloton and COROS accounts and create real activities.
  Tests must use fakes or mocked HTTP; live checks are opt-in
  (`PELOCORE_LIVE=1 mise exec -- uv run pytest -m live`).
- Validate YAML after editing workflows or manifests
  (`python -c "import yaml; yaml.safe_load(open(...))"`).
- Do not hand-edit `uv.lock`; use `uv` commands. Do not modify `LICENSE`.

## Repo map

| Path | Responsibility |
|---|---|
| `src/pelocore/cli.py` | argparse CLI: `sync` / `run` / `status` / `doctor` / `rename` |
| `src/pelocore/config.py` | pydantic-settings; env vars (PELOTON_*, COROS_*, PELOCORE_*) |
| `src/pelocore/state.py` | atomic JSON state file: per-workout records + cycle history |
| `src/pelocore/peloton.py` | pylotoncycle wrapper: listing, performance parsing, class plans |
| `src/pelocore/coros.py` | COROS client: login, STS, SigV4 S3 PUT, import, poll, rename |
| `src/pelocore/fitbuild.py` | performance data -> FIT activity files (fit_tool) |
| `src/pelocore/sports.py` | Peloton discipline -> FIT sport/subsport + remap table |
| `src/pelocore/sync.py` | orchestrator: two-phase per cycle (hydrate all, then upload) |
| `src/pelocore/server.py` | loop-mode scheduler + stdlib HTTP server |
| `src/pelocore/status.py` | HTML/JSON/Prometheus renderers (pure functions) |
| `tests/` | pytest; HTTP mocked with `responses`; fakes in `conftest.py` |
| `scripts/backfill_reupload.py` | one-off: delete COROS activities + clear state for re-upload |
| `deploy/k8s/` | CronJob (one-shot) and Deployment (service) manifests |

### Testing conventions

- All HTTP mocked with `responses`; no test touches the network.
- Engine tests use `FakePeloton` / `FakeCoros` from `tests/conftest.py`;
  extend those fakes when the protocols grow.
- FIT files are validated by re-parsing with `fit_tool.FitFile.from_bytes`
  and asserting `validate()` has no errors, then checking fields via
  `to_rows()` (row layout: `[Type, LocalID, Message, field, value, units, ...]`).
- Live tests are marked `live` and skipped unless `PELOCORE_LIVE=1`.

## Hard-won learnings

Full details live in `README.md` ("Data fidelity notes") and the module
docstrings of `coros.py` and `peloton.py`. Non-obvious traps:

### COROS

- STS credentials come from the Training Hub web BFF proxy
  (`{region proxy}/api/proxy/oss/sts`, cookie-authenticated with
  `CPL-coros-token`). The old open endpoint
  (`faq.coros.com/openapi/oss/sts`) went offline 2026-10-03; it is kept as a
  fallback channel.
- The import (`activity/fit/import`) is multipart with a `jsonParameter`
  field and uses the header `AccessToken` (capital A); all other API calls
  use `accessToken`. Renames (`activity/update`) use `accesstoken` (all
  lowercase) plus a `yfheader` JSON blob. Three different header casings.
- The importer categorizes imported files by primary FIT sport only;
  `sub_sport` is ignored, so anything with sport `TRAINING` lands as
  Strength. Muscle heatmaps are not available for imported activities (they
  are derived server-side from COROS plan sessions; native FIT exports
  contain no muscle data).
- Training Load for imported strength/yoga/stretching requires heart rate
  data; without it COROS reports load 0. Peloton captures HR when the user
  broadcasts HR from the COROS wearable to Peloton hardware/app.
- FIT-provided activity names (`session.sport_profile_name`) are ignored on
  import; pelocore renames activities after import via
  `activity/update` (`{"type": 1, "labelId", "name"}`). The labelId only
  exists after import processing, so rename-after-upload resolves it by
  polling `activity/query` and matching start time.
- `activity/query` is a GET with query params (POST yields "Service
  exceptions") and rejects unbounded queries; scope it with
  `startDay`/`endDay` (YYYYMMDD).

### Peloton

- `total_work` is in joules; FIT `total_work` is kJ. Convert.
- Cycling has no per-second distance series; distance only exists in the
  performance-graph `summaries` block (imperial: miles). Speed is per-second
  mph. Running has a per-second distance slug.
- All API units are imperial; convert to FIT units (m/s, meters).
- `pylotoncycle` is pinned to git main by commit: the PyPI release (0.5.2)
  is stale and lacks OAuth. `hatch` needs `allow-direct-references = true`.

### fit_tool

- The encoder expands component fields (e.g. `speed` -> `enhanced_speed`)
  at encode time; set both explicitly or the data outgrows the auto-built
  definition and WIRE validation fails.
- Activity files need at least one `record`; summary-only workouts get a
  timestamp-only anchor record.
- fit_tool cannot parse COROS's own exports (nonstandard field sizes in
  event and field_description messages). Our generated files validate.

### Toolchain

- `pylotoncycle` git dependency requires `git` in the Docker builder stage.
- act (local CI) cannot mint GitHub OIDC tokens: cosign signing, GHCR push,
  and SARIF upload only run in real CI.

## Open threads

- COROS importer ignores FIT-provided names; renaming works via
  `activity/update` (implemented, automated post-import). If COROS ever
  starts honoring `sport_profile_name`, the rename step becomes a no-op
  safeguard.
- COROS muscle heatmaps for imported strength workouts: not possible via
  FIT today; revisit only if COROS documents importer support.
