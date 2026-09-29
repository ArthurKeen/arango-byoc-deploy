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
