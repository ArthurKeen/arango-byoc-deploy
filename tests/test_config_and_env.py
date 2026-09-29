from __future__ import annotations

from pathlib import Path

import pytest

from arango_byoc_deploy import config, env

MINIMAL = 'app-name = "demo"\ninstance = "demo-inst"\n'


def test_standalone_file_is_read(tmp_path: Path) -> None:
    (tmp_path / "arango-byoc.toml").write_text(MINIMAL)

    cfg = config.load(tmp_path)

    assert (cfg.app_name, cfg.instance) == ("demo", "demo-inst")
    assert cfg.base_image == "py12base"
    assert cfg.has_ui is True


def test_pyproject_table_is_read(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(f"[tool.arango-byoc]\n{MINIMAL}")

    assert config.load(tmp_path).app_name == "demo"


def test_standalone_wins_over_pyproject(tmp_path: Path) -> None:
    (tmp_path / "arango-byoc.toml").write_text(MINIMAL)
    (tmp_path / "pyproject.toml").write_text('[tool.arango-byoc]\napp-name = "other"\ninstance = "x"\n')

    assert config.load(tmp_path).app_name == "demo"


def test_no_configuration_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="arango-byoc.toml"):
        config.load(tmp_path)


@pytest.mark.parametrize("missing", ["app-name", "instance"])
def test_required_keys_are_enforced(missing: str) -> None:
    raw = {"app-name": "a", "instance": "b"}
    del raw[missing]

    with pytest.raises(config.ConfigError, match=missing):
        config.from_mapping(raw)


def test_probes_accept_strings_and_tables() -> None:
    cfg = config.from_mapping(
        {
            "app-name": "a",
            "instance": "b",
            "probes": [
                "/health",
                {"path": "/api/tickers", "min-items": 700, "label": "tickers"},
                {"path": "/api/years", "expect-json-key": "anchors"},
            ],
        }
    )

    assert [p.path for p in cfg.probes] == ["/health", "/api/tickers", "/api/years"]
    assert cfg.probes[1].min_items == 700
    assert cfg.probes[2].expect_json_key == "anchors"


def test_an_empty_database_string_is_kept_distinct_from_absent() -> None:
    """'' forces a _global mount; absent defers to ARANGO_DB. They must not collapse."""
    assert config.from_mapping({"app-name": "a", "instance": "b", "database": ""}).database == ""
    assert config.from_mapping({"app-name": "a", "instance": "b"}).database is None


# -- env -------------------------------------------------------------------


def test_every_estate_spelling_resolves(tmp_path: Path) -> None:
    """Six repos disagree on names; none should have to rename to migrate."""
    for endpoint_key, user_key in (("ARANGO_ENDPOINT", "ARANGO_USERNAME"), ("ARANGO_URL", "ARANGO_USER")):
        creds = env.resolve({endpoint_key: "https://h:8529/", user_key: "u", "ARANGO_PASSWORD": "p"})
        assert (creds.endpoint, creds.user, creds.password) == ("https://h:8529", "u", "p")


def test_quotes_are_stripped_so_a_quoted_password_does_not_401(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("ARANGO_PASSWORD=\"s3cr3t\"\nARANGO_USER='me'\n# comment\n\n")

    parsed = env.load_env(dotenv)

    assert parsed == {"ARANGO_PASSWORD": "s3cr3t", "ARANGO_USER": "me"}


def test_missing_credentials_name_every_accepted_spelling() -> None:
    with pytest.raises(env.CredentialError) as excinfo:
        env.resolve({"ARANGO_USER": "u"})

    message = str(excinfo.value)
    assert "ARANGO_ENDPOINT/ARANGO_URL" in message
    assert "ARANGO_PASSWORD" in message


def test_an_explicit_endpoint_overrides_env() -> None:
    creds = env.resolve(
        {"ARANGO_URL": "https://a", "ARANGO_USER": "u", "ARANGO_PASSWORD": "p"},
        endpoint_override="https://b",
    )

    assert creds.endpoint == "https://b"
