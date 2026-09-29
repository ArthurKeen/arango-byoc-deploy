"""Platform client and verifier against a fake transport.

Mock fidelity: the fake returns genuine ``requests.Response`` objects, not
duck-typed stand-ins, so a change in how the client reads responses (status,
JSON, text) breaks these tests rather than silently passing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from arango_byoc_deploy.config import AppConfig, Probe
from arango_byoc_deploy.platform import (
    ACP,
    FILEMANAGER,
    PAGE_SIZE,
    DeployError,
    Platform,
    mount_path,
    next_build_version,
    service_id_of,
)
from arango_byoc_deploy.verify import deep_verify, poll_until_serving


def _response(status: int, body: Any = None, *, text: str | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    payload = text if text is not None else json.dumps(body if body is not None else {})
    r._content = payload.encode()
    r.headers["content-type"] = "text/html" if text is not None else "application/json"
    return r


class FakeSession:
    """Routes (method, url-suffix) to queued responses and records every call."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[requests.Response]] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def route(self, method: str, suffix: str, *responses: requests.Response) -> None:
        self.routes.setdefault((method, suffix), []).extend(responses)

    def _serve(self, method: str, url: str, kwargs: dict) -> requests.Response:
        self.calls.append((method, url, kwargs))
        for (m, suffix), queue in self.routes.items():
            if m == method and url.endswith(suffix) and queue:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        return _response(404, {"error": "no route in fake"})

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._serve("POST", url, kwargs)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._serve("GET", url, kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        return self._serve(method, url, kwargs)


@pytest.fixture
def platform() -> tuple[Platform, FakeSession]:
    p = Platform("https://cluster.example", "u", "p")
    fake = FakeSession()
    fake.route("POST", "/_open/auth", _response(200, {"jwt": "t0k"}))
    p.session = fake  # type: ignore[assignment]
    return p, fake


# -- pure helpers ----------------------------------------------------------


def test_mount_path_global_versus_database() -> None:
    assert mount_path("inst", None) == "/_service/uds/_global/inst"
    assert mount_path("inst", "AIM") == "/_service/uds/_db/AIM/inst"


def test_service_id_is_read_from_the_nested_serviceinfo() -> None:
    """Reading only the top level silently yielded serviceId=None in the estate."""
    assert service_id_of({"serviceInfo": {"serviceId": "svc-1", "status": "DEPLOYED"}}) == (
        "svc-1",
        "DEPLOYED",
    )
    assert service_id_of({"serviceId": "svc-2"}) == ("svc-2", None)


# -- client ----------------------------------------------------------------


def test_deploy_env_values_are_all_strings(platform: tuple[Platform, FakeSession]) -> None:
    """The platform decodes env as protobuf string->string; a bool is rejected."""
    p, fake = platform
    fake.route("POST", f"{ACP}/uds", _response(200, {"serviceInfo": {"serviceId": "s"}}))

    p.deploy("app", "1.0-1", "inst", "AIM", "py12base", has_ui=True, display_name="Demo")

    body = next(kw["json"] for m, url, kw in fake.calls if url.endswith(f"{ACP}/uds"))
    assert all(isinstance(v, str) for v in body["env"].values())
    assert body["env"]["has_ui"] == "true"
    assert body["env"]["db_name"] == "AIM"


def test_a_global_mount_omits_db_name(platform: tuple[Platform, FakeSession]) -> None:
    p, fake = platform
    fake.route("POST", f"{ACP}/uds", _response(200, {}))

    p.deploy("app", "1.0-1", "inst", None, "py12base")

    body = next(kw["json"] for m, url, kw in fake.calls if url.endswith(f"{ACP}/uds"))
    assert "db_name" not in body["env"]


def test_one_transparent_reauth_on_401(platform: tuple[Platform, FakeSession]) -> None:
    """The JWT outlives most calls but not a slow upload."""
    p, fake = platform
    fake.route("GET", FILEMANAGER, _response(401, {}), _response(200, {"services": [{"name": "a"}]}))

    assert p.list_packages() == [{"name": "a"}]
    assert sum(1 for m, url, _ in fake.calls if url.endswith("/_open/auth")) == 2


def test_list_packages_follows_every_page(platform: tuple[Platform, FakeSession]) -> None:
    """One unpaged call returned only the first 100 — prod.demo already held 94."""
    p, fake = platform
    first = [{"name": "app", "version": f"1.0-{i}"} for i in range(1, PAGE_SIZE + 1)]
    second = [{"name": "app", "version": f"1.0-{i}"} for i in range(PAGE_SIZE + 1, PAGE_SIZE + 51)]
    fake.route(
        "GET",
        FILEMANAGER,
        _response(200, {"services": first, "total": 150, "limit": PAGE_SIZE, "offset": 0}),
        _response(200, {"services": second, "total": 150, "limit": PAGE_SIZE, "offset": PAGE_SIZE}),
    )

    packages = p.list_packages("app")

    assert len(packages) == 150
    offsets = [kw["params"]["offset"] for m, url, kw in fake.calls if url.endswith(FILEMANAGER)]
    assert offsets == [0, PAGE_SIZE]
    assert next_build_version(p, "app", "1.0") == "1.0-151"


def test_list_packages_rechecks_the_name_filter(platform: tuple[Platform, FakeSession]) -> None:
    """A looser server-side match must not leak another app's builds."""
    p, fake = platform
    fake.route(
        "GET",
        FILEMANAGER,
        _response(200, {"services": [{"name": "app"}, {"name": "app-legacy"}], "total": 2}),
    )

    assert p.list_packages("app") == [{"name": "app"}]
    assert next(kw for m, url, kw in fake.calls if url.endswith(FILEMANAGER))["params"]["name"] == "app"


def test_resolve_instance_refuses_ambiguity(platform: tuple[Platform, FakeSession]) -> None:
    p, fake = platform
    svc = {"serviceMeta": {"udsMeta": {"appInstanceName": "inst"}}}
    fake.route(
        "POST",
        f"{ACP}/list_services",
        _response(200, {"services": [dict(svc, serviceId="a"), dict(svc, serviceId="b")]}),
    )

    with pytest.raises(DeployError, match="2 services are running"):
        p.resolve_instance("inst")


def test_next_build_version_is_one_past_the_highest(platform: tuple[Platform, FakeSession]) -> None:
    """A gap left by a deleted package (1.0-2) is never refilled."""
    p, fake = platform
    fake.route(
        "GET",
        FILEMANAGER,
        _response(
            200,
            {
                "services": [
                    {"name": "app", "version": "1.0-1"},
                    {"name": "app", "version": "1.0-3"},
                    {"name": "app", "version": "1.0.1-9"},
                    {"name": "other", "version": "1.0-7"},
                ]
            },
        ),
    )

    assert next_build_version(p, "app", "1.0") == "1.0-4"


def test_first_build_of_a_release_is_one(platform: tuple[Platform, FakeSession]) -> None:
    p, fake = platform
    fake.route("GET", FILEMANAGER, _response(200, {"services": []}))

    assert next_build_version(p, "app", "2.0") == "2.0-1"


def test_http_errors_carry_method_path_and_status(platform: tuple[Platform, FakeSession]) -> None:
    p, fake = platform
    fake.route("POST", f"{ACP}/list_services", _response(500, {"error": "boom"}))

    with pytest.raises(DeployError, match=r"POST .*list_services -> HTTP 500"):
        p.list_services()


# -- verify ----------------------------------------------------------------

BASE = "https://cluster.example/_service/uds/_db/AIM/inst/"


def test_poll_treats_404_401_503_as_cold_start_then_returns_the_200(
    platform: tuple[Platform, FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    p, fake = platform
    monkeypatch.setattr("arango_byoc_deploy.verify.time.sleep", lambda _s: None)
    fake.route(
        "GET",
        "/inst/",
        _response(404),
        _response(401),
        _response(503),
        _response(200, text="<html></html>"),
    )

    root = poll_until_serving(p, BASE, timeout_s=60, interval_s=0)

    assert root is not None and root.status_code == 200


def test_every_relative_asset_must_load(platform: tuple[Platform, FakeSession]) -> None:
    """A mount-prefix mismatch serves the shell while every asset 404s."""
    p, fake = platform
    fake.route("GET", "/inst/assets/app.js", _response(200, text="js"))
    fake.route("GET", "/inst/assets/app.css", _response(404))
    root = _response(200, text='<script src="./assets/app.js"></script><link href="./assets/app.css">')

    result = deep_verify(p, BASE, AppConfig(app_name="a", instance="inst"), root)

    assert result.ok is False
    assert any("1/2 served" in line for line in result.lines)


def test_a_root_with_no_relative_assets_fails(platform: tuple[Platform, FakeSession]) -> None:
    p, _ = platform
    root = _response(200, text="<html><body>nothing here</body></html>")

    assert deep_verify(p, BASE, AppConfig(app_name="a", instance="inst"), root).ok is False


def test_a_headless_service_skips_the_asset_check(platform: tuple[Platform, FakeSession]) -> None:
    p, _ = platform
    root = _response(200, text='{"status": "ok"}')

    result = deep_verify(p, BASE, AppConfig(app_name="a", instance="inst", has_ui=False), root)

    assert result.ok is True


def test_min_items_catches_a_healthy_service_on_the_wrong_database(
    platform: tuple[Platform, FakeSession],
) -> None:
    p, fake = platform
    fake.route("GET", "/inst/api/tickers", _response(200, [{"t": "aapl"}] * 12))
    cfg = AppConfig(
        app_name="a",
        instance="inst",
        has_ui=False,
        probes=(Probe(path="/api/tickers", label="tickers", min_items=700),),
    )

    result = deep_verify(p, BASE, cfg, _response(200, text="{}"))

    assert result.ok is False
    assert any("12 item(s), expected >= 700" in line for line in result.lines)


def test_expect_json_key_is_enforced(platform: tuple[Platform, FakeSession]) -> None:
    p, fake = platform
    fake.route("GET", "/inst/api/years", _response(200, {"years": [2020]}))
    cfg = AppConfig(
        app_name="a",
        instance="inst",
        has_ui=False,
        probes=(Probe(path="/api/years", expect_json_key="anchors"),),
    )

    assert deep_verify(p, BASE, cfg, _response(200, text="{}")).ok is False
