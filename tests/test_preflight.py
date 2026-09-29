"""Pre-flight against real tarballs — the archive layout is the thing under test.

Each case reproduces a defect that shipped, or nearly shipped, from one of the
seven estate copies this package replaces.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from arango_byoc_deploy.config import AppConfig
from arango_byoc_deploy.platform import DeployError
from arango_byoc_deploy.preflight import check, require

GOOD_ENTRY = "entrypoint = __file__\nimport os\n"
MOUNT = "/_service/uds/_db/AIM/demo"


def _tarball(tmp_path: Path, members: dict[str, str], *, nested: str | None = None) -> Path:
    path = tmp_path / "bundle.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, text in members.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{nested}/{name}" if nested else f"./{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def _config(**overrides) -> AppConfig:
    base = {
        "app_name": "demo",
        "instance": "demo",
        "required_members": ("entrypoint", "app.py"),
    }
    base.update(overrides)
    return AppConfig(**base)


def test_a_well_formed_bundle_passes(tmp_path: Path) -> None:
    bundle = _tarball(tmp_path, {"entrypoint": GOOD_ENTRY, "app.py": "", ".env": f"ROOT_PATH={MOUNT}\n"})

    assert check(bundle, _config(prefix_env_var="ROOT_PATH"), "AIM") == []


def test_missing_tarball_is_reported_not_raised(tmp_path: Path) -> None:
    assert check(tmp_path / "absent.tar.gz", _config(), None) == [
        f"no tarball at {tmp_path / 'absent.tar.gz'}"
    ]


def test_a_nested_layout_is_rejected(tmp_path: Path) -> None:
    """myservice/entrypoint fails on the platform with a bare 'No entrypoint found'."""
    bundle = _tarball(tmp_path, {"entrypoint": GOOD_ENTRY, "app.py": ""}, nested="myservice")

    problems = check(bundle, _config(), None)

    assert "entrypoint missing from the archive root" in problems


@pytest.mark.parametrize(
    "first_line",
    ['"""Service entrypoint."""', "#!/usr/bin/env python3", "# entrypoint", "import os"],
)
def test_entrypoint_line_one_must_be_the_literal_token(tmp_path: Path, first_line: str) -> None:
    """The platform runs `python /project/<first word>` — any of these breaks it."""
    bundle = _tarball(tmp_path, {"entrypoint": f"{first_line}\nentrypoint = __file__\n", "app.py": ""})

    problems = check(bundle, _config(), None)

    assert any("line 1 must begin with the token 'entrypoint'" in p for p in problems)


def test_a_baked_api_key_is_refused(tmp_path: Path) -> None:
    """Tarballs are uploaded and archived; an LLM key has no business in one."""
    bundle = _tarball(
        tmp_path, {"entrypoint": GOOD_ENTRY, "app.py": "", ".env": "OPENAI_API_KEY=sk-live-abc\n"}
    )

    problems = check(bundle, _config(), None)

    assert any("API_KEY is baked" in p for p in problems)


def test_a_loopback_endpoint_is_refused(tmp_path: Path) -> None:
    bundle = _tarball(
        tmp_path,
        {"entrypoint": GOOD_ENTRY, "app.py": "", ".env": "ARANGO_URL=http://localhost:8529\n"},
    )

    problems = check(bundle, _config(), None)

    assert any("loopback" in p for p in problems)


def test_a_baked_prefix_must_match_the_deploy_mount(tmp_path: Path) -> None:
    """The ROOT_PATH class of bug: bundle built for one mount, deployed to another.

    Without this check the app emits URLs for the wrong prefix while serving 200s.
    """
    bundle = _tarball(
        tmp_path,
        {"entrypoint": GOOD_ENTRY, "app.py": "", ".env": "ROOT_PATH=/_service/uds/_db/OTHER/demo\n"},
    )

    problems = check(bundle, _config(prefix_env_var="ROOT_PATH"), "AIM")

    assert any("will mount at '/_service/uds/_db/AIM/demo'" in p for p in problems)


def test_a_required_prefix_with_no_env_at_all_is_refused(tmp_path: Path) -> None:
    """The exact defect that shipped: no prefix baked, Swagger pointed at the wrong API."""
    bundle = _tarball(tmp_path, {"entrypoint": GOOD_ENTRY, "app.py": ""})

    problems = check(bundle, _config(prefix_env_var="ROOT_PATH"), "AIM")

    assert any("ROOT_PATH is required but no .env is baked" in p for p in problems)


def test_a_trailing_slash_on_the_baked_prefix_is_tolerated(tmp_path: Path) -> None:
    bundle = _tarball(tmp_path, {"entrypoint": GOOD_ENTRY, "app.py": "", ".env": f"ROOT_PATH={MOUNT}/\n"})

    assert check(bundle, _config(prefix_env_var="ROOT_PATH"), "AIM") == []


def test_root_absolute_assets_are_refused(tmp_path: Path) -> None:
    """They resolve against the cluster root under a mount prefix: a blank page behind a 200."""
    bundle = _tarball(
        tmp_path,
        {
            "entrypoint": GOOD_ENTRY,
            "app.py": "",
            "static/index.html": '<script src="/assets/app.js"></script>',
        },
    )

    problems = check(bundle, _config(index_html="static/index.html"), None)

    assert any("outside the mount prefix" in p for p in problems)


def test_relative_and_external_assets_are_fine(tmp_path: Path) -> None:
    bundle = _tarball(
        tmp_path,
        {
            "entrypoint": GOOD_ENTRY,
            "app.py": "",
            "static/index.html": (
                '<script src="./assets/app.js"></script>'
                '<link href="https://fonts.example/x.css">'
                '<script src="//cdn.example/lib.js"></script>'
            ),
        },
    )

    assert check(bundle, _config(index_html="static/index.html"), None) == []


def test_a_required_baked_env_that_is_absent_is_refused(tmp_path: Path) -> None:
    bundle = _tarball(tmp_path, {"entrypoint": GOOD_ENTRY, "app.py": ""})

    problems = check(bundle, _config(require_baked_env=True), None)

    assert any("platform injects nothing" in p for p in problems)


def test_every_problem_is_reported_in_one_pass(tmp_path: Path) -> None:
    """One pre-flight run should cost one rebuild, not one per defect."""
    bundle = _tarball(
        tmp_path,
        {"entrypoint": "#!/bin/sh\n", ".env": "OPENAI_API_KEY=sk-x\nARANGO_URL=http://127.0.0.1:8529\n"},
    )

    problems = check(bundle, _config(), None)

    assert len(problems) >= 4  # app.py missing, entry token, API key, loopback


def test_require_raises_with_every_problem_listed(tmp_path: Path) -> None:
    bundle = _tarball(tmp_path, {"entrypoint": "#!/bin/sh\n"})

    with pytest.raises(DeployError) as excinfo:
        require(bundle, _config(), None)

    assert "app.py missing" in str(excinfo.value)
    assert "line 1 must begin" in str(excinfo.value)


def test_prefix_baked_absolute_assets_pass_preflight(tmp_path: Path) -> None:
    """Next.js basePath output is absolute but under the mount — not the prefix bug."""
    bundle = _tarball(
        tmp_path,
        {
            "entrypoint": GOOD_ENTRY,
            "app.py": "",
            "static/index.html": f'<script src="{MOUNT}/_next/a.js"></script>',
        },
    )

    assert check(bundle, _config(index_html="static/index.html"), "AIM") == []


def test_a_required_directory_needs_something_under_it(tmp_path: Path) -> None:
    """worldview must ship node_modules/: the pod cannot reach the npm registry at boot."""
    cfg = _config(required_members=("entrypoint", "node_modules/"))
    without = _tarball(tmp_path, {"entrypoint": GOOD_ENTRY})
    (tmp_path / "with").mkdir()
    with_modules = _tarball(
        tmp_path / "with", {"entrypoint": GOOD_ENTRY, "node_modules/express/index.js": ""}
    )

    assert any("node_modules/ missing" in p for p in check(without, cfg, None))
    assert check(with_modules, cfg, None) == []


# -- env-rules ----------------------------------------------------------------------


from arango_byoc_deploy.config import EnvRule  # noqa: E402

SECRET_RULE = EnvRule(
    key="APP_SECRET_KEY",
    min_length=32,
    reject_prefixes=("change-me", "changeme"),
    unless="AUTH_DEV_BYPASS",
    reason="the backend refuses to start without a real signing key",
)
RESET_RULE = EnvRule(key="ALLOW_SYSTEM_RESET", forbid=("true",), reason="ships a system-wipe endpoint")


def _env_bundle(tmp_path: Path, env: str) -> Path:
    return _tarball(tmp_path, {"entrypoint": GOOD_ENTRY, "app.py": "", ".env": env})


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ("APP_SECRET_KEY=" + "a" * 64 + "\n", []),
        ("APP_SECRET_KEY=short\n", ["under 32 characters"]),
        ("APP_SECRET_KEY=change-me-" + "x" * 40 + "\n", ["placeholder"]),
        ("AUTH_DEV_BYPASS=true\n", []),  # bypass: no secret needed
        ("APP_SECRET_KEY=" + "a" * 64 + "\nALLOW_SYSTEM_RESET=TRUE\n", ["not allowed"]),
    ],
)
def test_app_env_rules(tmp_path: Path, env: str, expected: list[str]) -> None:
    """ontoextract: a missing secret crash-loops (reported as a 503); a reset flag ships a wipe endpoint."""
    problems = check(_env_bundle(tmp_path, env), _config(env_rules=(SECRET_RULE, RESET_RULE)), None)

    assert len(problems) == len(expected)
    for fragment, problem in zip(expected, problems, strict=True):
        assert fragment in problem


def test_env_rule_messages_never_echo_a_secret(tmp_path: Path) -> None:
    problems = check(
        _env_bundle(tmp_path, "APP_SECRET_KEY=hunter2\n"), _config(env_rules=(SECRET_RULE,)), None
    )

    assert problems and "hunter2" not in problems[0]


def test_an_env_rule_can_require_an_exact_value(tmp_path: Path) -> None:
    """agentic-graph-analytics keeps its metadata in aga_workspace, not the analytics graph."""
    rule = EnvRule(
        key="ARANGO_DATABASE", equals="aga_workspace", reason="the product API's metadata database"
    )
    good = _env_bundle(tmp_path, "ARANGO_DATABASE=aga_workspace\n")
    (tmp_path / "bad").mkdir()
    bad = _env_bundle(tmp_path / "bad", "ARANGO_DATABASE=analytics\n")

    assert check(good, _config(env_rules=(rule,)), None) == []
    assert any("must be 'aga_workspace'" in p for p in check(bad, _config(env_rules=(rule,)), None))
