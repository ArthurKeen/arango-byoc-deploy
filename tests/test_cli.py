from __future__ import annotations

import pytest

from arango_byoc_deploy.cli import build_parser, main


def test_update_is_an_alias_for_release() -> None:
    """The estate's copies used both names; migrating should not break habits."""
    parser = build_parser()

    release = parser.parse_args(["release"])
    update = parser.parse_args(["update"])

    assert release.func is update.func


def test_rollback_requires_a_target() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["rollback"])


def test_tool_version_and_release_version_do_not_collide(capsys: pytest.CaptureFixture[str]) -> None:
    """`--version` on the tool prints it; `release --version X` sets the release."""
    args = build_parser().parse_args(["release", "--version", "1.2.3"])
    assert args.version == "1.2.3"

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--version"])
    assert "arango-byoc-deploy" in capsys.readouterr().out


def test_missing_configuration_is_a_clean_error_not_a_traceback(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--repo", str(tmp_path), "list"])

    assert code == 1
    assert "no BYOC configuration" in capsys.readouterr().err


# -- release number sources ----------------------------------------------------


from pathlib import Path  # noqa: E402

from arango_byoc_deploy.cli import release_version  # noqa: E402
from arango_byoc_deploy.config import AppConfig, VersionSource  # noqa: E402
from arango_byoc_deploy.platform import DeployError  # noqa: E402


def _cfg(source: VersionSource | None = None) -> AppConfig:
    return AppConfig(app_name="a", instance="a", version_source=source)


def test_release_defaults_to_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "a"\nversion = "1.4.0"\n')

    assert release_version(tmp_path, _cfg(), None) == "1.4.0"


def test_release_from_a_json_file(tmp_path: Path) -> None:
    """worldview is Node.js: its release lives in package.json."""
    (tmp_path / "package.json").write_text('{"name": "w", "version": "2.1.0"}')

    assert (
        release_version(tmp_path, _cfg(VersionSource(file="package.json", json_key="version")), None)
        == "2.1.0"
    )


def test_release_from_a_regex(tmp_path: Path) -> None:
    """sparql-py keeps its release in __init__.py, not pyproject."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text('__version__ = "0.3.1"\n')
    source = VersionSource(file="pkg/__init__.py", regex=r'^__version__ = "([^"]+)"')

    assert release_version(tmp_path, _cfg(source), None) == "0.3.1"


def test_an_explicit_version_wins(tmp_path: Path) -> None:
    assert release_version(tmp_path, _cfg(), "9.9.9") == "9.9.9"


def test_a_missing_version_source_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(DeployError, match="not found"):
        release_version(tmp_path, _cfg(VersionSource(file="nope.json", json_key="version")), None)


def test_upload_is_a_command() -> None:
    args = build_parser().parse_args(["upload", "--version", "1.0", "--exact"])

    assert args.func.__name__ == "cmd_upload"
    assert (args.version, args.exact) == ("1.0", True)


def test_verify_accepts_an_expected_version() -> None:
    assert build_parser().parse_args(["verify", "--expect-version", "1.0"]).expect_version == "1.0"


def test_no_ui_drops_the_ui_from_preflight_and_verify() -> None:
    from arango_byoc_deploy.cli import without_ui

    cfg = AppConfig(
        app_name="a",
        instance="a",
        required_members=("entrypoint", "ui/dist/index.html"),
        index_html="ui/dist/index.html",
    )

    bare = without_ui(cfg)

    assert (bare.has_ui, bare.index_html, bare.required_members) == (False, None, ("entrypoint",))
    assert bare.effective_ready_path == "/health"
    assert build_parser().parse_args(["--no-ui", "release"]).no_ui is True


def test_release_from_the_bundles_filename(tmp_path: Path) -> None:
    """FinReflectKG / gdelt: the artifact being deployed is the authority."""
    source = VersionSource(tarball_regex=r"finreflectkg-timetravel-(.+)\.tar\.gz")
    bundle = tmp_path / "finreflectkg-timetravel-1.0.3.tar.gz"

    assert release_version(tmp_path, _cfg(source), None, bundle) == "1.0.3"
    with pytest.raises(DeployError, match="cannot read a version"):
        release_version(tmp_path, _cfg(source), None, tmp_path / "other.tar.gz")
