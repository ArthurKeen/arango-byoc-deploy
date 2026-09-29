"""``arango-byoc-deploy`` — upload, deploy, verify and roll back a BYOC service.

Run from a repository root holding an ``arango-byoc.toml`` (or a
``[tool.arango-byoc]`` table in ``pyproject.toml``) and a ``.env`` with platform
credentials::

    arango-byoc-deploy list                   # uploaded packages + running services
    arango-byoc-deploy release                # pre-flight, upload, swap, verify
    arango-byoc-deploy verify                 # prove the live service serves
    arango-byoc-deploy rollback --to 1.2.0-3  # redeploy an already-uploaded package
    arango-byoc-deploy delete                 # remove the running service

``update`` is accepted as an alias for ``release``: the estate's copies used both
names, and migrating a repository should not retrain anyone's muscle memory.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path
from typing import Any

from . import __version__, preflight, verify
from . import config as config_mod
from . import env as env_mod
from .platform import DeployError, Platform, mount_path, next_build_version, service_id_of


class _Context:
    """Resolved configuration, credentials and database for one invocation."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.repo = Path(args.repo).resolve()
        self.config = config_mod.load(self.repo)
        if args.instance:
            self.config = _replace(self.config, instance=args.instance)
        if getattr(args, "no_ui", False):
            self.config = without_ui(self.config)
        creds = env_mod.resolve(env_mod.load_env(self.repo / ".env"), endpoint_override=args.endpoint)
        self.endpoint = creds.endpoint
        self.platform = Platform(creds.endpoint, creds.user, creds.password)
        # --db beats config beats ARANGO_DB; "" forces a _global mount.
        if args.db is not None:
            self.db_name = args.db or None
        elif self.config.database is not None:
            self.db_name = self.config.database or None
        else:
            self.db_name = creds.database

    @property
    def mount(self) -> str:
        return mount_path(self.config.instance, self.db_name)

    @property
    def url(self) -> str:
        return f"{self.endpoint}{self.mount}/"


def without_ui(cfg: config_mod.AppConfig) -> config_mod.AppConfig:
    """*cfg* for a bare-API deploy of a repo that normally ships a UI.

    Drops the UI from pre-flight as well as from verify: a bundle built without
    the UI must not fail for lacking ``index.html``.
    """
    ui_files = {cfg.index_html} if cfg.index_html else set()
    return _replace(
        cfg,
        has_ui=False,
        index_html=None,
        required_members=tuple(m for m in cfg.required_members if m not in ui_files),
    )


def _replace(cfg: config_mod.AppConfig, **changes: object) -> config_mod.AppConfig:
    from dataclasses import replace

    return replace(cfg, **changes)


def _tarball(ctx: _Context, explicit: str | None) -> Path:
    """The bundle to upload: explicit path, else config path/glob (newest match)."""
    spec = explicit or ctx.config.tarball
    if not spec:
        raise DeployError("no tarball given: pass --tarball or set 'tarball' in the BYOC config")
    matches = sorted(
        (Path(p) for p in glob.glob(str(ctx.repo / spec))),
        key=lambda p: p.stat().st_mtime,
    )
    if not matches:
        raise DeployError(f"no tarball matches {spec!r} under {ctx.repo}")
    return matches[-1]


def _release_version(ctx: _Context, explicit: str | None) -> str:
    return release_version(ctx.repo, ctx.config, explicit)


def release_version(repo: Path, config: config_mod.AppConfig, explicit: str | None) -> str:
    """The release number: --version, else the configured version-source, else
    ``[project].version`` in pyproject.toml."""
    if explicit:
        return explicit
    source = config.version_source
    if source is not None:
        path = repo / source.file
        if not path.is_file():
            raise DeployError(f"version-source file {source.file} not found under {repo}")
        text = path.read_text(encoding="utf-8")
        if source.json_key:
            value: Any = json.loads(text)
            for part in source.json_key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if not value:
                raise DeployError(f"no {source.json_key!r} in {source.file}")
            return str(value)
        match = re.search(source.regex or "", text, re.M)
        if not match or not match.groups():
            raise DeployError(f"version-source regex {source.regex!r} matched nothing in {source.file}")
        return match.group(1)
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        data = config_mod.tomllib.loads(pyproject.read_text(encoding="utf-8"))
        version = (data.get("project") or {}).get("version")
        if version:
            return str(version)
    raise DeployError("no release version: pass --version, or set [project].version in pyproject.toml")


def _verify(ctx: _Context, args: argparse.Namespace, expect_version: str | None = None) -> int:
    ready = ctx.url + ctx.config.effective_ready_path.lstrip("/")
    print(f"==> polling {ready}")
    answered = verify.poll_until_serving(
        ctx.platform, ready, timeout_s=args.wait_timeout, interval_s=args.poll_interval
    )
    if answered is None:
        print(f"error: {ready} never returned 200 within {args.wait_timeout:.0f}s", file=sys.stderr)
        return 1
    result = verify.deep_verify(ctx.platform, ctx.url, ctx.config, expect_version)
    result.report()
    return 0 if result.ok else 1


def _swap(ctx: _Context, args: argparse.Namespace, version: str, expect_version: str | None = None) -> int:
    """Delete-then-create, then verify — what an update *is* on this platform.

    A second install cannot take over the first one's Kubernetes objects, and
    there is no update endpoint.
    """
    existing = ctx.platform.resolve_instance(ctx.config.instance)
    if existing:
        print(f"==> replacing {existing['serviceId']} ({existing['version']} -> {version})")
        ctx.platform.delete_service(existing["serviceId"])
        print("    old service deleted — the URL is down from here")
    else:
        print(f"==> no instance {ctx.config.instance!r} running; creating fresh")
    print(f"    mount {ctx.mount}/   image {ctx.config.base_image}")
    result = ctx.platform.deploy(
        ctx.config.app_name,
        version,
        ctx.config.instance,
        ctx.db_name,
        ctx.config.base_image,
        has_ui=ctx.config.has_ui,
        display_name=ctx.config.display_name or None,
        description=ctx.config.description or None,
    )
    service_id, state = service_id_of(result)
    if not service_id:
        raise DeployError(f"deploy returned no serviceId: {result}")
    print(f"    created {service_id} status={state}")
    ctx.platform.wait_until_ready(service_id, timeout_s=args.wait_timeout)
    print("    DEPLOYED — the pod may still be installing dependencies")
    return _verify(ctx, args, expect_version)


# -- commands ---------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    ctx = _Context(args)
    print(f"cluster  {ctx.endpoint}\napp      {ctx.config.app_name}\n")
    packages = sorted(str(p.get("version")) for p in ctx.platform.list_packages(ctx.config.app_name))
    print(f"uploaded packages: {len(packages)}")
    for version in packages:
        print(f"  {version}")
    services = ctx.platform.find_instances(ctx.config.instance)
    print(f"\nrunning as {ctx.config.instance!r}: {len(services)}")
    for s in services:
        print(f"  {s['serviceId']}  version={s['version']}  status={s['status']}  db={s['dbName']}")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    ctx = _Context(args)
    tarball = _tarball(ctx, args.tarball)
    problems = preflight.check(tarball, ctx.config, ctx.db_name)
    if problems:
        print(f"pre-flight FAILED for {tarball.name}:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"pre-flight OK for {tarball.name} (mount {ctx.mount})")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    """Pre-flight, upload, swap, verify.

    Upload precedes delete so a rejected artifact fails while the old service is
    still serving.
    """
    ctx = _Context(args)
    tarball = _tarball(ctx, args.tarball)
    preflight.require(tarball, ctx.config, ctx.db_name)
    print(f"    pre-flight OK ({tarball.name})")

    release = _release_version(ctx, args.version)
    version = release if args.exact else next_build_version(ctx.platform, ctx.config.app_name, release)
    size_mb = tarball.stat().st_size / 1_048_576
    print(f"==> uploading {tarball.name} ({size_mb:.1f} MB) as {ctx.config.app_name} {version}")
    ctx.platform.upload(tarball, ctx.config.app_name, version, language=ctx.config.language)
    print("    uploaded")
    # The service reports its release, not the build suffix.
    return _swap(ctx, args, version, expect_version=release)


def cmd_upload(args: argparse.Namespace) -> int:
    """Pre-flight and upload only; deploy later with ``rollback --to``."""
    ctx = _Context(args)
    tarball = _tarball(ctx, args.tarball)
    preflight.require(tarball, ctx.config, ctx.db_name)
    release = _release_version(ctx, args.version)
    version = release if args.exact else next_build_version(ctx.platform, ctx.config.app_name, release)
    print(f"==> uploading {tarball.name} as {ctx.config.app_name} {version}")
    ctx.platform.upload(tarball, ctx.config.app_name, version, language=ctx.config.language)
    print(f"    uploaded — deploy it with: arango-byoc-deploy rollback --to {version}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    """Redeploy an already-uploaded package. Code only — data is untouched."""
    ctx = _Context(args)
    available = sorted({str(p.get("version")) for p in ctx.platform.list_packages(ctx.config.app_name)})
    if args.to not in available:
        raise DeployError(f"{ctx.config.app_name} {args.to} is not uploaded. Available: {available[-10:]}")
    print(f"==> ROLLBACK to {args.to}")
    return _swap(ctx, args, args.to)


def cmd_verify(args: argparse.Namespace) -> int:
    return _verify(_Context(args), args, args.expect_version)


def cmd_delete(args: argparse.Namespace) -> int:
    ctx = _Context(args)
    existing = ctx.platform.resolve_instance(ctx.config.instance)
    if not existing:
        print(f"no service runs as {ctx.config.instance!r} — nothing to delete")
        return 0
    print(f"==> deleting {existing['serviceId']} (version {existing['version']})")
    ctx.platform.delete_service(existing["serviceId"])
    print("    deleted")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ctx = _Context(args)
    print(ctx.platform.service_status(args.service_id))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arango-byoc-deploy",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--repo", default=".", help="repository root (default: cwd)")
    parser.add_argument("--endpoint", help="override the cluster URL from .env")
    parser.add_argument("--db", help="database to mount under; '' forces a _global mount")
    parser.add_argument("--instance", help="override the configured app_instance_name")
    parser.add_argument("--no-ui", action="store_true", help="deploy the bare API (skip the UI checks)")
    sub = parser.add_subparsers(dest="command", required=True)

    def _waits(p: argparse.ArgumentParser) -> None:
        p.add_argument("--wait-timeout", type=float, default=600.0)
        p.add_argument("--poll-interval", type=float, default=15.0)

    sub.add_parser("list", help="uploaded packages and running services").set_defaults(func=cmd_list)

    p_pre = sub.add_parser("preflight", help="check a bundle without uploading it")
    p_pre.add_argument("--tarball")
    p_pre.set_defaults(func=cmd_preflight)

    for name in ("release", "update"):
        p_rel = sub.add_parser(name, help="pre-flight, upload, swap, verify")
        p_rel.add_argument("--tarball")
        p_rel.add_argument("--version", dest="version", help="release version (default: pyproject)")
        p_rel.add_argument("--exact", action="store_true", help="use --version verbatim, no -N suffix")
        _waits(p_rel)
        p_rel.set_defaults(func=cmd_release)

    p_up = sub.add_parser("upload", help="pre-flight and upload only (deploy later with rollback --to)")
    p_up.add_argument("--tarball")
    p_up.add_argument("--version", dest="version", help="release version (default: version-source)")
    p_up.add_argument("--exact", action="store_true", help="use --version verbatim, no -N suffix")
    p_up.set_defaults(func=cmd_upload)

    p_rb = sub.add_parser("rollback", help="redeploy an already-uploaded package")
    p_rb.add_argument("--to", required=True, help="package version, e.g. 1.2.0-3")
    _waits(p_rb)
    p_rb.set_defaults(func=cmd_rollback)

    p_ver = sub.add_parser("verify", help="prove the live service serves")
    p_ver.add_argument("--expect-version", help="fail unless the live service reports this release")
    _waits(p_ver)
    p_ver.set_defaults(func=cmd_verify)

    sub.add_parser("delete", help="delete the running service").set_defaults(func=cmd_delete)

    p_st = sub.add_parser("status", help="raw platform status for a service id")
    p_st.add_argument("service_id")
    p_st.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (DeployError, config_mod.ConfigError, env_mod.CredentialError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
