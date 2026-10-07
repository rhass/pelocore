# pelocore

Peloton → COROS activity bridge. Fetches your Peloton workouts, converts them
to Garmin FIT files, and uploads them to your COROS Training Hub — on a
schedule, with a built-in status page.

```
Peloton API ──► FIT builder ──► zip+md5 ──► COROS upload ──► state
(pylotoncycle)   (fit_tool)                (unofficial API)
```

## Features

- **Backfill sync**: picks up any workout from the last N days (default 7)
  that is not already in COROS — survives downtime and restarts.
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
$ mise run run                     # loop mode + status page on :8080
```

Open http://localhost:8080 for the status page.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PELOTON_USERNAME` / `PELOTON_PASSWORD` | — | Peloton credentials |
| `PELOTON_REFRESH_TOKEN` | — | Alternative to password login |
| `COROS_EMAIL` / `COROS_PASSWORD` | — | COROS credentials |
| `COROS_ACCESS_TOKEN` | — | Browser session token instead of password (value of the `CPL-coros-token` cookie on training.coros.com) |
| `COROS_REGION` | `en` | `en`, `eu`, or `cn` (uploads unsupported on `cn`) |
| `PELOCORE_TIMEZONE_QUARTERS` | host offset | COROS timezone in quarter-hours east of UTC (32 = UTC+8) |
| `PELOCORE_BACKFILL_DAYS` | `7` | How far back to look for unsynced workouts |
| `PELOCORE_SYNC_INTERVAL_SECONDS` | `900` | Loop-mode cycle interval |
| `PELOCORE_IMPORT_POLL_SECONDS` | `120` | How long to poll COROS import status |
| `PELOCORE_STATE_PATH` | `data/state.json` | State file location |
| `PELOCORE_SERVER_HOST` / `PELOCORE_SERVER_PORT` | `0.0.0.0` / `8080` | Status server bind |
| `PELOCORE_STATUS_TOKEN` | — | When set, `/`, `/api/status`, `/metrics`, `POST /sync` require `Authorization: Bearer <token>`; probes stay open |
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

## Kubernetes

Both modes are shipped under `deploy/k8s/`:

- **`cronjob.yaml`** — one-shot `pelocore sync` every 15 minutes. No volume
  needed: dedupe runs against the COROS import list. Visibility: logs.
- **`deployment.yaml`** — long-running `pelocore run` with the status page
  (ClusterIP Service, liveness/readiness probes, optional PVC for state).
  Keep it at 1 replica: syncers would race on the state file.

```console
$ kubectl create namespace pelocore
$ cp deploy/k8s/secret.example.yaml secret.yaml   # fill in, then:
$ kubectl -n pelocore apply -f secret.yaml
$ kubectl -n pelocore apply -f deploy/k8s/cronjob.yaml     # or deployment.yaml
```

## Container image

Multi-arch (`linux/amd64`, `linux/arm64`), non-root (uid 10001), released to
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

### Base image

The Dockerfile accepts a swappable base:

| Base | Image | Access |
|---|---|---|
| Default | `python:3.13-slim` | none |
| **Docker Hardened Images** (default in release CI) | `dhi.io/python:3.13` | free Docker account; set `DOCKERHUB_USERNAME` + `DOCKERHUB_TOKEN` repo secrets |
| Iron Bank | `registry1.dso.mil/ironbank/opensource/python/python3.13` | DoD Platform One access request |
| Minimus | `registry.minimus.io/minimus/python` | commercial subscription |

Build with an alternative:

```console
$ docker build --build-arg BASE_IMAGE=registry1.dso.mil/ironbank/opensource/python/python3.13 -t pelocore .
```

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

Apache-2.0 — see [LICENSE](LICENSE).
