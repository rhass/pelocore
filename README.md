# pelocore

Peloton → COROS activity bridge. Fetches your Peloton workouts, converts them
to Garmin FIT files, and uploads them to your COROS Training Hub - on a
schedule, with a built-in status page.

```
Peloton API ──► FIT builder ──► zip+md5 ──► COROS upload ──► state
(pylotoncycle)   (fit_tool)                (unofficial API)
```

## Features

- **Backfill sync**: picks up any workout from the last N days (default 7)
  that is not already in COROS - survives downtime and restarts.
- **All disciplines**: cycling, treadmill/outdoor running, rowing, walking,
  strength, yoga, stretching, meditation, bootcamps, cardio.
  Per-second metrics (power, cadence, heart rate, speed, distance) where
  Peloton provides them; summary-level sessions otherwise.
- **Dedupe, two ways**: a local state file *and* reconciliation against the
  COROS import list (so a lost state file never double-uploads).
- **Status page**: HTML at `/`, JSON at `/api/status`, Prometheus metrics at
  `/metrics`, manual trigger via `POST /sync`. Optional bearer-token gate.
- **One-shot or service**: `pelocore sync` for k8s CronJobs, `pelocore run`
  for a long-running Deployment with the status server.

## Quickstart

Requires [mise](https://mise.jdx.dev) (Python 3.13, uv, ruff come from
`mise.toml`).

```console
$ mise run install        # uv sync --all-groups
$ export PELOTON_USERNAME=you@example.com
$ export PELOTON_PASSWORD='...'
$ export COROS_EMAIL=you@example.com
$ export COROS_PASSWORD='...'
$ mise exec -- pelocore doctor     # validates both sides
$ mise exec -- pelocore sync       # one-shot sync
$ mise run start                   # loop mode; `start` wraps with `op run --` when
                                   # 1Password is configured (see Secrets via 1Password)
```

Open http://localhost:8080 for the status page.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PELOTON_USERNAME` / `PELOTON_PASSWORD` | - | Peloton credentials |
| `PELOTON_REFRESH_TOKEN` | - | Alternative to password login |
| `COROS_EMAIL` / `COROS_PASSWORD` | - | COROS credentials |
| `COROS_ACCESS_TOKEN` | - | Browser session token instead of password (value of the `CPL-coros-token` cookie on training.coros.com) |
| `COROS_REGION` | `en` | `en`, `eu`, or `cn` (uploads unsupported on `cn`) |
| `PELOCORE_TIMEZONE_QUARTERS` | host offset | COROS timezone in quarter-hours east of UTC (32 = UTC+8) |
| `PELOCORE_BACKFILL_DAYS` | `7` | How far back to look for unsynced workouts |
| `PELOCORE_SPORT_REMAPS` | `stretching=yoga` built-in | Discipline remaps, e.g. `meditation=yoga`. COROS has no stretching type and buckets unknown TRAINING files into Strength, so stretching maps to Yoga by default; Pilates has a native mapping |
| `PELOCORE_SYNC_INTERVAL_SECONDS` | `900` | Loop-mode cycle interval (ignored by `sync` - cron mode cadence is the scheduler's job) |
| `PELOCORE_IMPORT_POLL_SECONDS` | `60` | How long to poll COROS import status per upload |
| `PELOCORE_AUTO_UPGRADE` | `on` | When a conversion change would produce different FIT bytes, delete the stale COROS activity and re-upload automatically (conservative: ambiguous matches are skipped, never deleted blind) |
| `PELOCORE_IMPORT_POLL_SECONDS` | `120` | How long to poll COROS import status |
| `PELOCORE_STATE_PATH` | `data/state.json` | State file location |
| `PELOCORE_SERVER_HOST` / `PELOCORE_SERVER_PORT` | `0.0.0.0` / `8080` | Status server bind |
| `PELOCORE_STATUS_TOKEN` | - | When set, `/`, `/api/status`, `/metrics`, `POST /sync` require `Authorization: Bearer <token>`; probes stay open |
| `PELOCORE_LOG_LEVEL` | `INFO` | Log verbosity |

Credentials may also live in a `.env` file (pydantic-settings loads it).

## CLI

```
pelocore sync     # run a single sync cycle and exit (cron mode)
pelocore run      # loop mode: internal scheduler + status server
pelocore status   # print cycle history from the state file
pelocore doctor   # verify Peloton and COROS credentials
```

Exit code of `pelocore sync` is `1` when the cycle outcome is `failed`
(partial failures still exit `0`; inspect via `pelocore status` or the logs).

## How COROS upload works

pelocore implements the reverse-engineered Training Hub upload protocol:

1. `POST /account/login` with an MD5-hashed password (or paste a browser
   token via `COROS_ACCESS_TOKEN`) → `accessToken`.
2. `GET https://faq.coros.com/openapi/oss/sts` → temporary S3 credentials
   (per-region bucket, hard-coded STS signs).
3. The FIT file is zipped (`{md5}/peloton-{workoutId}.fit`, stored) and PUT
   to `s3://{bucket}/fit_zip/{userId}/{md5}.zip` with SigV4.
4. `POST /activity/fit/import` registers the object; import progress is
   polled via `activity/fit/getImportSportList` (`status == 2` = success).

If COROS rotates the STS signs, uploads fail with `401 signature error`
until the constants in `src/pelocore/coros.py` are updated.

## Data fidelity notes

- **Activity names**: the COROS importer ignores FIT-provided names
  (`session.sport_profile_name`), so imports start with sport-generic names
  ("Indoor Bike", "Strength"). pelocore renames activities to their Peloton
  titles after import via `POST /activity/update`; `pelocore rename` fixes
  any state-tracked activities retroactively.
- **Content dedupe**: the COROS import pipeline is keyed by file md5 -
  uploading identical FIT bytes twice yields the same import job and no
  second activity (verified experimentally). This is what makes stateless
  operation safe: a lost state file only ever causes wasted uploads, never
  duplicates.
- **Conversion changes (auto-upgrade)**: FIT filenames carry the converter
  version (`peloton-<id>.fit` = v1, `peloton-<id>.v2.fit` = v2). The import
  list reveals which version produced each upload, so when the converter
  changes, the next sync detects stale versions, deletes the old activity
  (unique start-time + sport match required), purges the import entry, and
  re-uploads. No manual backfill needed for data-fidelity fixes.
- **Query scoping**: `activity/query` must be scoped with
  `startDay`/`endDay`; unbounded queries can return stale partial results
  (which once made fresh imports invisible). pelocore scopes everything.
- **Cycling distance/speed**: Peloton reports speed per second and distance
  only in the performance-graph summaries (there is no per-second distance
  series for rides). pelocore writes both - session `total_distance`,
  `avg_speed`/`max_speed` (classic + enhanced fields) and per-record
  cumulative distance integrated from the speed series.
- **`total_work`**: Peloton reports joules; FIT wants kJ - converted.
- **Strength / yoga / stretching**: Peloton strength workouts have no
  per-second data. FIT files carry the class plan as `exercise_title` + `set`
  messages (one per exercise block, named from the plan) plus session
  calories. **COROS's importer categorizes imported files by primary sport
  only** - anything with FIT sport `TRAINING` lands as Strength (402),
  regardless of sub-sport (verified: yoga/stretching imports with YOGA
  sub-sport still import as Strength), and the muscle heatmap is not
  available for imported activities (COROS derives it server-side from its
  own plan structure; a native watch export contains no muscle data).
- **Training Load needs heart rate**: imported strength/yoga/stretching
  without HR data gets `trainingLoad = 0` in COROS. Peloton captures HR
  when you wear a monitor - it flows through to the FIT (session
  avg/max HR and per-second records), and COROS then computes a nonzero
  load. Wear the strap for strength and pilates classes if Training Load
  matters to you.

### Heart rate setup: broadcast from your COROS wearable

Peloton hardware and the Peloton app cannot read a wrist-based wearable
directly. The way to get your HR into Peloton workouts is to **broadcast
heart rate from the COROS watch** (Settings → Broadcast Heart Rate on
supported models) and pair that broadcast as a standard Bluetooth heart-rate
sensor with whatever is running the class:

- **Peloton Bike / Tread / Row hardware**: add the broadcast as a Bluetooth
  HR sensor in the device's sensor-pairing screen.
- **Peloton app (tablet or phone)**: pair the broadcast as a Bluetooth HR
  sensor from the app's heart-rate settings before starting the class.

Once paired, HR is captured by Peloton per workout and flows through
pelocore into the generated FIT files - which is what COROS needs to
compute Training Load for strength, yoga and stretching imports (and gives
cycling/running their full HR metrics).
- Peloton `muscle_group_score` (per-muscle percentages) is available in the
  API but has no standard FIT representation; it is not written today.

## Backfilling history

```console
$ pelocore sync --since 2026-02-01 --chunk-size 10 --chunk-delay 30
```

- `--since YYYY-MM-DD` overrides the 7-day window (365-day cap); Peloton's
  history pagination supports up to 1000 workouts.
- `--chunk-size` / `--chunk-delay` pace the upload: after every N uploads the
  bridge pauses S seconds, keeping the request rate gentle on both APIs.
  Workouts upload oldest-first, so an interrupted run leaves the most recent
  ones for last.
- Backfills are resumable: state + the COROS import list dedupe every
  workout, so re-running skips what already landed (and even a full state
  loss cannot create duplicates, since the import pipeline is md5-keyed).
- `--dry-run` prints the plan (new/skip per workout) without uploading or
  re-fetching performance data.
- A 280-workout backfill (~8 months) takes roughly 45-70 minutes including
  chunk delays; per-workout failures are logged and retried on the next run.

## Stateless operation

`pelocore sync` (cron mode) needs no state at all: dedupe runs against the
COROS import list (server-side, retained for many months) and the import
pipeline itself is md5-keyed, so even a re-upload of identical bytes cannot
create a duplicate activity. The shipped `cronjob.yaml` mounts no volume
and every run starts from scratch.

- **k8s CronJob**: the default stateless shape (`deploy/k8s/cronjob.yaml`).
  The schedule is the citizen knob: it sets how often Peloton and COROS get
  polled (an idle run costs ~3 light reads). The shipped default is `*/15`;
  relax it to lower API traffic.
- **Serverless** (Cloud Run Jobs, etc.): works the same way; tune
  `PELOCORE_IMPORT_POLL_SECONDS` down if per-invocation time is capped,
  since polling (import + rename resolution) dominates runtime. AWS Lambda's
  15-minute cap fits small backfills only. Note `pelocore sync` is one-shot
  and ignores `PELOCORE_SYNC_INTERVAL_SECONDS` entirely - that setting only
  applies to `pelocore run`.
- **API observability**: `/metrics` reports
  `pelocore_peloton_api_calls_total` and
  `pelocor_coros_api_calls_total` so the request budget stays visible.
- **Deployment with status page**: state.json adds cycle history for the
  status page and skips already-known workouts (fewer API calls), but is
  never required for correctness.

## Kubernetes

Both modes are shipped under `deploy/k8s/`:

- **`cronjob.yaml`** - one-shot `pelocore sync` every 15 minutes. No volume
  needed: dedupe runs against the COROS import list. Visibility: logs.
- **`deployment.yaml`** - long-running `pelocore run` with the status page
  (ClusterIP Service, liveness/readiness probes, optional PVC for state).
  Keep it at 1 replica: syncers would race on the state file.

```console
$ kubectl create namespace pelocore
$ cp deploy/k8s/secret.example.yaml secret.yaml   # fill in, then:
$ kubectl -n pelocore apply -f secret.yaml
$ kubectl -n pelocore apply -f deploy/k8s/cronjob.yaml     # or deployment.yaml
```

## Container image

Multi-arch (`linux/amd64`, `linux/arm64`), non-root (uid 65532), released to
GHCR on tags:

```console
ghcr.io/rhass/pelocore:vX.Y.Z
```

Pull and run:

```console
$ docker run -d -p 8080:8080 -v pelocore-data:/data \
    -e PELOTON_USERNAME=... -e PELOTON_PASSWORD=... \
    -e COROS_EMAIL=... -e COROS_PASSWORD=... \
    ghcr.io/rhass/pelocore:vX.Y.Z
```

### Verifying images

Images are signed with Sigstore cosign (keyless) and carry SBOM +
provenance attestations:

```console
$ cosign verify \
    --certificate-identity-regexp \
      '^https://github\.com/rhass/pelocore/\.github/workflows/release\.yml@refs/tags/v.*' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    ghcr.io/rhass/pelocore:vX.Y.Z

$ cosign verify-attestation --type slsaprovenance \
    --certificate-identity-regexp \
      '^https://github\.com/rhass/pelocore/\.github/workflows/release\.yml@refs/tags/v.*' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    ghcr.io/rhass/pelocore:vX.Y.Z
```

### Cutting a release

Release CI runs on `v*` tags and refuses to ship an unsigned tag: it imports
the GPG keys registered to the repo owner's GitHub account and verifies the
tag signature cryptographically. Prerequisites:

1. Register your GPG public key under Settings > SSH and GPG keys on GitHub.
2. Sign tags by default (`git config tag.gpgsign true`) or pass `-s` per tag.
3. Local signing needs a gpg binary (CI runners ship one; macOS installs it
   via MacPorts `gnupg2`, Homebrew `gnupg`, or GPG Suite).
4. Cut and push the tag (or use the mise task, which does both):

   ```console
   $ mise run release v0.1.0
   ```

CI then verifies the tag, runs lint/typecheck/tests, builds both
architectures on the Chainguard base, gates on a HIGH/CRITICAL image scan,
pushes by digest to GHCR, assembles the multi-arch manifest list, signs with
cosign (keyless), attests SBOM + provenance, and publishes the GitHub release.

### Base image

The image builds on Chainguard's minimal Python:

| Stage | Image |
|---|---|
| Builder | `cgr.dev/chainguard/python:latest-dev` (bash, apk, discarded at build end) |
| Runtime | `cgr.dev/chainguard/python:latest` (nonroot uid 65532, no shell, no pip) |

The runtime ships no shell, so it contains no RUN instructions: all setup
happens in the builder and is copied in. The public images can be pulled
anonymously, but CI logs in with the `CGDEV_USERNAME`/`CGDEV_PASSWORD` repo
secrets: authenticated pulls raise the rate limit that shared GitHub runner
IPs otherwise hit. `latest` floats with Chainguard's packaging (currently
Python 3.14, ahead of the 3.13 local dev toolchain); the Dockerfile asserts
the expected minor version and fails the build on drift.

## Development

```console
$ mise run install     # deps
$ mise run test        # pytest (unit + mocked integration)
$ mise run lint        # ruff
$ mise run typecheck   # mypy (strict)
```

Live smoke tests against the real services are opt-in:

```console
$ PELOCORE_LIVE=1 mise exec -- uv run pytest -m live
```

### Local CI with act

The CI and release workflows can run locally with
[nektos/act](https://nektosact.com) (installed via mise; requires Docker).
This catches CI-only behavior differences - action wrappers, job wiring -
before pushing:

```console
$ mise run act-ci                 # verify + both Trivy scans + image build
$ mise run act-release-verify     # release verify path (dry-run)
```

The image build job logs in to cgr.dev: the act-ci task forwards the CGDEV
secrets with `-s NAME` inside `op run --`, resolving the references from
`.mise.local.toml` before act starts (add the CGDEV entries from
[.mise.local.toml.example](.mise.local.toml.example)).

Runner images and architecture come from `.actrc`. What act cannot exercise
locally: keyless cosign signing (needs GitHub's OIDC provider), GHCR push,
and the SARIF upload to the code-scanning API (needs `GITHUB_TOKEN` with
`security_events` scope) - those steps stay in real CI.

### Secrets via 1Password

Local credentials live in `.mise.local.toml` (git-ignored; see
[.mise.local.toml.example](.mise.local.toml.example)). The recommended
pattern uses 1Password secret references instead of plaintext values:

1. Add the 1Password CLI to the toolchain and sign in:

   ```console
   $ mise use 1password      # adds [tools] 1password = "latest" (to your local toml)
   $ op signin               # or unlock via the 1Password desktop app
   ```

2. Point env values at vault items - `op://<vault>/<item>/<field>`:

   ```toml
   [env]
   PELOTON_PASSWORD = "op://Private/pelocore/peloton-password"
   COROS_PASSWORD = "op://Private/pelocore/coros-password"
   ```

3. Wrap commands with `op run --`, which resolves every `op://` reference in
   the environment for the child process. The template ships task overrides
   for this (`mise start`, `mise run-sync`, `mise run-doctor`):

   ```toml
   [tasks.start]
   run = "op run -- uv run pelocore run"
   ```

Secrets resolved by `op run --` exist only in the child process environment -
references never touch disk or shell history. Plain values also work in
`.mise.local.toml` but are stored in plaintext; prefer references.

## Security notes

- Trivy scans run on every PR (filesystem: dependency vulns via `uv.lock` +
  secrets; config: Dockerfile/k8s misconfigurations) and on every release
  (image gate, HIGH/CRITICAL, unfixed ignored).
- Keyless cosign signatures publish image digests to the public Rekor
  transparency log. Fine for a public repo; be aware if you fork privately.
- The status page never renders credentials, only workout metadata and
  errors. Protect it with `PELOCORE_STATUS_TOKEN` when exposing beyond the
  cluster.

## Disclaimer

This project is not affiliated with, endorsed by, or approved by Peloton
Interactive, Inc. or COROS Wearables, Inc. It depends on **unofficial,
reverse-engineered APIs** that may change or stop working at any time, and
its use may violate the respective Terms of Service. Use at your own risk,
with your own credentials, for personal data you own.

## License

Apache-2.0 - see [LICENSE](LICENSE).
