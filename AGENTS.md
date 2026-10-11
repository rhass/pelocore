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
| `src/pelocore/fitdump.py` | raw FIT walker/decoder for debugging (reads files fit_tool rejects) |
| `src/pelocore/sports.py` | Peloton discipline -> FIT sport/subsport + remap table |
| `src/pelocore/sync.py` | orchestrator: two-phase per cycle (hydrate all, then upload) |
| `src/pelocore/server.py` | loop-mode scheduler + stdlib HTTP server |
| `src/pelocore/status.py` | HTML/JSON/Prometheus renderers (pure functions) |
| `tests/` | pytest; HTTP mocked with `responses`; fakes in `conftest.py` |
| `scripts/backfill_reupload.py` | one-off: delete COROS activities + clear state for re-upload |
| `scripts/experiment_stretch_sport.py` | one-off experiment harness: importer categorization trials (T1-T22) |
| `scripts/recategorize_bootcamps.py` | one-off: fold-recategorize historical bootcamps to converter v2 |
| `mise-tasks/` | mise file tasks (release tag cutter) |
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
- The importer consults (primary sport, sub_sport) pair overrides first,
  then a primary-sport table that defaults each family to its indoor
  variant. Exhaustively mapped by re-uploading one real session with every
  candidate encoding (`scripts/experiment_stretch_sport.py`, 2026-10-11):
  `RUNNING`->101 Indoor Run, `CYCLING`->201 Indoor Cycling,
  `ROWING`->701 Indoor Rower, `FITNESS_EQUIPMENT`->903 Elliptical,
  `TRAINING`->402 Strength, `GENERIC`->400 Gym Cardio, `JUMP_ROPE`->901,
  `FLOOR_CLIMBING`->902 Stair Climb, `BOXING`->906; `WALKING`,
  `MEDITATION`, `DANCE`, `MOBILITY` and `HIIT` are unmapped (walking once
  landed uncategorized as 65535; HIIT/DANCE/MOBILITY/MEDITATION imports
  were dropped with no activity at all - state still records "uploaded",
  so state can lie about activity creation). Pair overrides exist only in
  the FITNESS_EQUIPMENT family: `+PILATES`->905 and `+ELLIPTICAL`->903;
  unknown subsports there (`YOGA`, `FLEXIBILITY_TRAINING`) ERROR the
  import (errorSize=1, no activity) instead of falling back.
  `MULTISPORT` primary sport also errors outright.
- pelocore's mapping decisions (converter v2): yoga/stretching/meditation
  -> GENERIC (Gym Cardio 400 - user preference over Strength; Yoga 904 is
  unreachable), pilates -> FITNESS_EQUIPMENT+PILATES (real 905 Pilates),
  all bootcamp variants -> TRAINING/STRENGTH_TRAINING (Strength 402 -
  Hybrid Fitness 1200 has no watch mode to learn an encoding from, and
  MULTISPORT/HIIT imports fail).
- COROS multisport exports are ZIPs of per-leg single-sport FITs (cycling
  leg + strength leg, each standard-encoded with its own session); no
  "multisport" sport value appears in any file - the grouped 10001
  Multisport activity is produced only by the watch sync path. Re-importing
  the byte-exact ZIP through the import pipeline creates separate per-leg
  activities (201 + 402), never a grouped one. A direct S3 PUT of a zip
  (no inner `{md5}/{file}` wrapper) imports cleanly, mirroring the web
  portal's bulk upload.
- Yoga (904) is watch-path only and unreachable via import - proven by
  re-importing the byte-exact native watch export of a yoga session: it
  lands as 402 Strength. The watch writes yoga as `TRAINING`+`YOGA` and
  syncs it as 904; the same encoding through the importer lands as 402.
  The COROS app cannot change an activity's type either (title only), and
  `activity/update` rejects sportType changes, so imported activities are
  stuck in their importer-assigned category. TCX imports do not help: a
  TCX labeled `Sport="Yoga"` lands as 400 Gym Cardio (unknown sport strings
  convert to GENERIC).
- COROS's own docs list the officially supported import modes (Run, Indoor
  Run, Trail Run, Track Run, Bike, Indoor Bike, Pool Swim, Open Water,
  Mountain Climb, Hike, Multisport, Bouldering, GPS Cardio, Strength,
  Indoor Climb, Gym Cardio, ball sports) - the list is stale (rowing
  imports fine despite being unlisted) but yoga/stretching really are
  unsupported. Muscle heatmaps are not available for imported activities
  (they are derived server-side from COROS plan sessions; native FIT
  exports contain no muscle data).
- Re-uploading a FIT whose start time matches an existing activity folds
  into it in place (same labelId): the category is re-derived from the
  file's sport encoding and the display name resets to the sport default
  (a TCX fold is an exception observed once: name survived). No duplicate
  is created; rename after any re-upload. This makes targeted
  recategorization experiments possible without delete+recreate churn.
- The activity FIT download endpoint (`POST /activity/detail/download?labelId
  &sportType&fileType`, `accessToken` header, fileType 4=fit/3=tcx/2=kml/
  1=gpx/0=csv) returns a JSON envelope with a presigned S3 `fileUrl`; it
  only serves device-recorded activities - imported ones return `data: {}`.
  COROS export CRCs match fit_tool's `crc16` over the data section.
- The importer rejects future-dated files silently (import succeeds, no
  activity, errorSize=0) and files with broken CRCs with errorSize=1.
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
  `startDay`/`endDay` (YYYYMMDD). Unscoped queries can also return stale
  partial results: freshly imported activities may be invisible to them.
  Always scope.
- The import pipeline is md5-keyed: uploading identical FIT bytes twice
  returns the same import job id and creates no second activity (verified
  experimentally, `scripts/experiment_subsport.py`). Import-list entries
  are retained for many months, making stateless (no-volume) operation
  safe; a lost state file costs wasted uploads, never duplicates.
- Conversion drift handling: FIT filenames carry the converter version
  (`peloton-<id>.fit` = v1, `.v2`+ suffixed); the import list doubles as
  the version record, so drift detection needs no rebuilds or refetches.
  `CONVERTER_VERSION` in `fitbuild.py` bumps only with conversion-behavior
  changes. Sync then deletes the stale activity (unique start+sport match
  required; ambiguity raises and is skipped, never deleted blind) and
  re-uploads. `pelocore sync` (cron/serverless) ignores
  `PELOCORE_SYNC_INTERVAL_SECONDS` - that knob is loop-mode-only.
- Manufacturer spoofing (file_id manufacturer 294 / product 822 plus a
  matching `device_info` message, cloned from a real COROS export) does NOT
  change import categorization either. Primary-sport categorization is a
  server-side rule with no COROS-device special case.
- `activity/update` renames only: `type=1` with `name` works; sportType
  change payloads are rejected (`Parameter input error`, result 1031) or
  silently ignored, so an imported activity's category cannot be changed via
  the API after the fact.
- `activity/query` with `startDay == endDay` has returned empty results;
  always span at least two days around the target.
- An invalidated session token surfaces as HTTP 200 with an error envelope
  ("Access token is invalid"), not HTTP 401, so the client's 401-retry path
  does not trigger; a fresh process (new login) resolves it.

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
- Debugging FIT contents: `uv run pelocore fitdump <file> [--json]
  [--records N] [--trace]` (`fitdump.py`). It walks raw records with no
  profile assumptions and never fails on vendor quirks; use it on COROS
  exports and to verify our generated files.
- Message numbering gotcha confirmed empirically: fit_tool writes the
  activity message as gnum 34 and exercise_title as gnum 264 (not the
  140/229 one might expect); COROS's own exports agree on 34.

### Toolchain

- `pylotoncycle` git dependency requires `git` in the Docker builder stage.
- act (local CI) cannot mint GitHub OIDC tokens: cosign signing, GHCR push,
  and SARIF upload only run in real CI.

### Release and CI internals

- Cutting a release: `mise run release vX.Y.Z` cuts and pushes a signed
  annotated tag (`git tag -s`). Release CI refuses an unsigned tag: it
  imports the GPG keys registered to the repo owner's GitHub account and
  verifies the tag cryptographically. Prerequisites: register the GPG key
  under GitHub Settings > SSH and GPG keys, and sign tags by default
  (`git config tag.gpgsign true`) or pass `-s` per tag. Local signing needs
  a gpg binary (macOS: MacPorts `gnupg2`, Homebrew `gnupg`, or GPG Suite).
- The release pipeline verifies the tag, runs lint/typecheck/tests, builds
  both architectures on the Chainguard base, gates on a HIGH/CRITICAL image
  scan, pushes by digest to GHCR, assembles the multi-arch manifest list,
  signs with cosign (keyless), attests SBOM + provenance, publishes the
  GitHub release, and then the `bump-version` job bumps the patch version
  on the default branch (v0.2.0 -> 0.2.1, committed as
  `github-actions[bot]` with `[skip ci]`) so dev/PR builds never repeat a
  released version number. It is idempotent (skips when already bumped).
  If branch protection ever requires signed commits, the bot push fails and
  the bump must be done locally instead.
- The version lives in TWO places that must stay in sync: `pyproject.toml`
  and `src/pelocore/__init__.py` (`__version__`). App version bumps should
  be one more-significant level than converter bumps: converter
  `CONVERTER_VERSION` 2 shipped with app 0.2.0.
- Local CI with act: the CI and release workflows run locally with
  [nektos/act](https://nektosact.com) (installed via mise; needs Docker):
  `mise run act-ci` (verify + both Trivy scans + image build) and
  `mise run act-release-verify` (release verify path, dry-run). The image
  build job logs in to cgr.dev: the act-ci task forwards the CGDEV secrets
  with `-s NAME` inside `op run --`, resolving the references from
  `.mise.local.toml` before act starts (add the CGDEV entries from
  `.mise.local.toml.example`). Runner images and architecture come from
  `.actrc`. What act cannot exercise locally: keyless cosign signing (needs
  GitHub's OIDC provider), GHCR push, and the SARIF upload to the
  code-scanning API (needs `GITHUB_TOKEN` with `security_events` scope) -
  those steps stay in real CI.
- Base image: Chainguard's minimal Python. Builder
  `cgr.dev/chainguard/python:latest-dev` (bash, apk, discarded at build
  end); runtime `cgr.dev/chainguard/python:latest` (nonroot uid 65532, no
  shell, no pip - so it contains no RUN instructions; all setup happens in
  the builder and is copied in). Public images pull anonymously, but CI
  logs in with the `CGDEV_USERNAME`/`CGDEV_PASSWORD` repo secrets:
  authenticated pulls raise the rate limit that shared GitHub runner IPs
  otherwise hit. `latest` floats with Chainguard's packaging (currently
  Python 3.15, ahead of the 3.13 local toolchain; Chainguard's versioned
  tags like `3.14` are a paid feature, so the free tier always rides
  `latest`); the Dockerfile asserts the expected minor version and fails
  the build on drift.

## Open threads

- COROS importer ignores FIT-provided names; renaming works via
  `activity/update` (implemented, automated post-import). If COROS ever
  starts honoring `sport_profile_name`, the rename step becomes a no-op
  safeguard.
- COROS muscle heatmaps for imported strength workouts: not possible via
  FIT today; revisit only if COROS documents importer support.
- Yoga (904) for imported stretch/yoga sessions: unreachable - the
  importer's (sport, sub_sport) pair table lacks `TRAINING`+`YOGA` and the
  watch path is the only route to 904. Filed with COROS support
  (2026-10-11); revisit if COROS adds the pair or an activity-type API.
  Yoga/stretching/meditation ship as Gym Cardio (400) in the meantime.
- Hybrid Fitness (sportType 1200, "hyrox") exists in COROS's taxonomy and
  web app but not as a watch sport mode, so no FIT encoding reference
  exists and bootcamps ship as Strength (402) instead. Revisit if a watch
  mode appears or the importer documents a mapping. A multi-session
  multisport FIT (per-leg sessions in ONE file, not the per-leg ZIP COROS
  exports) is an untested variant that might group differently.
