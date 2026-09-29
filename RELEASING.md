# Releasing

Publishing goes to PyPI through Trusted Publishing (OIDC), so no API tokens are
involved. The publish workflow runs only in `ArthurKeen/arango-byoc-deploy`. The
`arango-solutions` copy is a push mirror, is not registered as a publisher, and
skips the job.

## One-time setup

1. On PyPI and TestPyPI, add a pending Trusted Publisher:
   - Project name: `arango-byoc-deploy`
   - Owner: `ArthurKeen`
   - Repository: `arango-byoc-deploy`
   - Workflow: `publish.yml`
   - Environment: `pypi` (or `testpypi` on test.pypi.org)
2. In this repo's Settings → Environments, create `pypi` and `testpypi`. They
   need no secrets.

The binding must match owner, repo, workflow and environment exactly. If the
repo is renamed or moved, publishing breaks until PyPI's entry is updated.

## Each release

1. Bump `__version__` in `src/arango_byoc_deploy/__init__.py` and commit.
2. Run a dry run first: Actions → *Publish to PyPI* → Run workflow with
   `target=testpypi`. Wait until the upload succeeds.
3. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`. The tag publishes
   to PyPI.

Test before you tag. A version number you tag but cannot publish is used up,
because PyPI never accepts the same version twice.
