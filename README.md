# arango-byoc-deploy

Pre-flight, upload, deploy, verify and roll back a BYOC service on the
**Arango Container Manager** — the platform half of a deploy, in one place.

It replaces seven hand-copied `scripts/byoc_deploy.py` files across the estate.
Each copy recorded its parent in its docstring and said the same thing:

> the platform-shaped half is identical for every project. What differs here:
> the defaults, the pre-flight checks and the verifier.

This package is that platform half. Each repository keeps only the part that
differs, as configuration.

## Install

It runs on a developer machine or in CI — never in the service pod — so install
it as a tool, not as an application dependency:

```bash
uv tool install arango-byoc-deploy        # or: pipx install arango-byoc-deploy
```

## Configure

Add `arango-byoc.toml` at the repository root, or a `[tool.arango-byoc]` table
in `pyproject.toml`. The standalone file exists because several consumers are
not Python-packaged.

```toml
app-name     = "arango-cypher-py"          # package name on the platform
instance     = "arango-cypher-py"          # app_instance_name
display-name = "Arango Cypher Workbench"
description  = "openCypher → AQL translation and a query Workbench."
base-image   = "py12base"                  # default; see "Base images" below
database     = "AIM"                       # omit to use ARANGO_DB; "" for _global
tarball      = "arango-cypher-byoc.tar.gz" # path or glob (newest match wins)

required-members = ["entrypoint", "pyproject.toml", "ui/dist/index.html"]
index-html       = "ui/dist/index.html"    # checked for root-absolute assets
prefix-env-var   = "ROOT_PATH"             # baked mount prefix to cross-check
has-ui           = true                    # false for a bare API (no root page)
ready-path       = "/health"               # polled after deploy; default "/" with a UI, "/health" without

# Prove the app talks to the right data, not merely that it answers.
[[probes]]
path  = "/health"

[[probes]]
path      = "/sample-queries"
label     = "sample queries"
expect-json-key = "queries"
min-items = 1
```

Credentials come from the repository's `.env`. Every estate spelling is accepted —
`ARANGO_ENDPOINT` or `ARANGO_URL`, `ARANGO_USERNAME` or `ARANGO_USER`,
`ARANGO_PASSWORD` or `ARANGO_PASS` — so no repository has to rename anything to
migrate. Nothing is written to disk and no credential is ever printed.

## Use

```bash
arango-byoc-deploy list                    # uploaded packages + running services
arango-byoc-deploy preflight               # check a bundle without uploading
arango-byoc-deploy release                 # pre-flight, upload, swap, verify
arango-byoc-deploy verify                  # prove the live service serves
arango-byoc-deploy rollback --to 1.2.0-3   # redeploy an already-uploaded package
arango-byoc-deploy delete                  # remove the running service
```

`update` is an alias for `release`; the copies used both names.

`release` uploads **before** it deletes, so a rejected artifact fails while the
old service is still serving. The release number comes from `--version`, else
`[project].version`; a build suffix (`-1`, `-2`, …) is appended automatically
because the platform refuses to re-upload an existing `(name, version)`.

## What it checks, and why

Every rule below exists because a deploy somewhere in the estate failed without
it. Pre-flight reports **every** problem in one pass, so one run costs one
rebuild rather than one per defect.

| Check | What it prevents |
| --- | --- |
| `entrypoint` at the archive root | a nested layout fails with a bare "No entrypoint found" |
| line 1 of `entrypoint` is the literal token `entrypoint` | the platform runs `python /project/<first word>`; a shebang or docstring there breaks detection |
| no `*_API_KEY` in a baked `.env` | tarballs are uploaded, archived and shared |
| baked Arango endpoint is not loopback | no database runs inside the service pod |
| baked mount prefix equals the deploy's mount | the app emitting every URL for the wrong prefix while serving 200s |
| the SPA shell uses no root-absolute assets | a blank page behind a green health check |

Verification polls the **mount root** — the path the platform's Apps view opens —
until it serves, then requires every relative asset the page references to load,
then runs each configured probe. `min-items` is the probe that catches a healthy
service pointed at an empty or wrong database.

## Platform behaviour worth knowing

- **The deploy call cannot carry application environment.** The platform's
  deploy `env` map is metadata; arbitrary keys are accepted and silently
  dropped. A mount prefix or credentials placed there never reach the container.
  Bake them into the bundle's `.env`. This is the single most expensive thing to
  rediscover, and none of the seven copies documented it.
- **Every deploy `env` value must be a string.** It is decoded as a protobuf
  `string → string` map; a JSON boolean is rejected.
- **There is no update endpoint.** An update is delete-then-create.
- **`DEPLOYED` means the pod launched, not that it serves.** Dependency install
  at boot takes roughly 45 seconds.
- **During cold start the gateway answers 401 to an authenticated caller**, then
  flips to 200. Here, 401 means "not ready", not "wrong password".
- **Base images are per-cluster.** The house standard `py13base` does not exist
  on `prod.demo.pilot.arango.ai`, which offers `node22base`, `py12base`,
  `py12cugraph`, `py12torch` and `test`. Hence the `py12base` default.

## Migrating a repository

1. Add `arango-byoc.toml` with the defaults from the existing script's
   `DEFAULT_*` constants and its pre-flight `required` list.
2. Port the script's `deep_verify` data checks into `[[probes]]`.
3. Run `arango-byoc-deploy preflight` against a current bundle, then `list`.
4. Delete `scripts/byoc_deploy.py`, and point runbooks at `arango-byoc-deploy`.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

MIT licensed.
