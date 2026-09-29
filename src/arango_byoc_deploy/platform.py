"""Client for the Container Manager endpoints a BYOC release needs.

This is the half every estate deploy script already agreed on. The quirks it
encodes were each paid for by a failed deploy somewhere; they are recorded where
they are handled rather than in a separate list that drifts.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

ACP = "/_platform/acp/v1"
FILEMANAGER = "/_platform/filemanager/global/byoc/"

READY = frozenset({"DEPLOYED"})
FAILED = frozenset({"FAILED", "ERROR", "TERMINATED"})


class DeployError(RuntimeError):
    """A platform call failed, or the cluster refused the request."""


def mount_path(instance: str, db_name: str | None) -> str:
    """The public prefix the platform serves an instance under.

    Omitting ``db_name`` mounts under ``_global`` instead of ``_db/<db>``. This
    must equal the prefix baked into the bundle, or every URL the app emits is
    wrong — which is why pre-flight checks the two against each other.
    """
    scope = f"_db/{db_name}" if db_name else "_global"
    return f"/_service/uds/{scope}/{instance}"


def service_id_of(result: Any) -> tuple[str | None, str | None]:
    """(serviceId, status) from a deploy or status response.

    The create response nests everything under ``serviceInfo``; reading only the
    top level silently yields ``None`` for both — the first estate deploy to
    succeed reported ``serviceId=None`` for exactly this reason.
    """
    info = result.get("serviceInfo") if isinstance(result, dict) else None
    if not isinstance(info, dict):
        info = result if isinstance(result, dict) else {}
    return info.get("serviceId") or info.get("service_id"), info.get("status")


class Platform:
    """Thin, authenticated client over the file manager and ACP."""

    def __init__(self, base: str, user: str, password: str, *, timeout: float = 60.0) -> None:
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self._jwt: str | None = None

    # -- auth -------------------------------------------------------------

    def authenticate(self) -> None:
        response = self.session.post(
            f"{self.base}/_open/auth",
            json={"username": self.user, "password": self.password},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise DeployError(f"auth failed: HTTP {response.status_code}")
        token = response.json().get("jwt")
        if not token:
            raise DeployError("auth response carried no 'jwt' field")
        self._jwt = token

    def headers(self) -> dict[str, str]:
        if self._jwt is None:
            self.authenticate()
        return {"Authorization": f"Bearer {self._jwt}"}

    # -- transport --------------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """An authenticated GET against an absolute URL (for verification)."""
        kwargs.setdefault("timeout", self.timeout)
        return self.session.get(url, headers=self.headers(), **kwargs)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.timeout)
        url = f"{self.base}{path}"
        response = self.session.request(method, url, headers=self.headers(), **kwargs)
        # One transparent re-auth: the JWT outlives most calls but not a slow upload.
        if response.status_code == 401:
            self.authenticate()
            response = self.session.request(method, url, headers=self.headers(), **kwargs)
        if response.status_code >= 400:
            raise DeployError(f"{method} {path} -> HTTP {response.status_code}: {response.text[:400]}")
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text[:400]}

    # -- packages ---------------------------------------------------------

    def list_packages(self) -> list[dict]:
        return self._request("GET", FILEMANAGER).get("services", [])

    def upload(self, tarball: Path, name: str, version: str) -> dict:
        """Upload a package. The platform keys on (name, version) and rejects reuse."""
        with tarball.open("rb") as handle:
            return self._request(
                "POST",
                FILEMANAGER,
                data={"name": name, "version": version, "language": "python", "type": "Service"},
                files={"file": (tarball.name, handle, "application/gzip")},
                timeout=600,
            )

    # -- services ---------------------------------------------------------

    def list_services(self) -> list[dict]:
        return self._request("POST", f"{ACP}/list_services", json={}).get("services", [])

    def deploy(
        self,
        name: str,
        version: str,
        instance: str,
        db_name: str | None,
        base_image: str,
        *,
        has_ui: bool = True,
        display_name: str | None = None,
        description: str | None = None,
    ) -> dict:
        """Create a service from an uploaded package.

        This ``env`` map is platform metadata, NOT the application's environment:
        the platform does not forward arbitrary keys to the container. A mount
        prefix or credentials placed here are accepted and silently dropped —
        bake them into the bundle's ``.env`` instead.

        Every value must be a string: the platform decodes ``env`` as a protobuf
        ``string -> string`` map and rejects a JSON boolean with
        "invalid value for string field value: true".
        """
        env: dict[str, str] = {
            "service_type": "base_type",
            "base_image": base_image,
            "app_instance_name": instance,
        }
        if db_name:
            env["db_name"] = db_name
        # Advertising a UI is what makes the platform present the service as an
        # app rather than a bare endpoint.
        if has_ui:
            env["has_ui"] = "true"
        if display_name:
            env["display_name"] = display_name
        if description:
            env["description"] = description
        return self._request(
            "POST",
            f"{ACP}/uds",
            json={"app_name": name, "app_version": version, "env": env},
            timeout=180,
        )

    def service_status(self, service_id: str) -> dict:
        return self._request("GET", f"{ACP}/service/{service_id}")

    def delete_service(self, service_id: str) -> None:
        """Remove a deployed service. There is no update endpoint — delete then
        create is what an update is on this platform."""
        self._request("DELETE", f"{ACP}/service/{service_id}", timeout=120)

    def find_instances(self, instance: str) -> list[dict]:
        """Every service running under ``app_instance_name``.

        Destructive commands take an instance name rather than a generated id
        like ``arango-user-defined-08fxo``, which nobody can type from memory and
        which changes on every redeploy.
        """
        found = []
        for service in self.list_services():
            uds = ((service.get("serviceMeta") or {}).get("udsMeta")) or {}
            if uds.get("appInstanceName") == instance:
                found.append(
                    {
                        "serviceId": service.get("serviceId"),
                        "version": uds.get("version"),
                        "status": service.get("status"),
                        "dbName": service.get("dbName"),
                    }
                )
        return found

    def resolve_instance(self, instance: str) -> dict | None:
        """Exactly one service for this instance name, or ``None``. Refuses ambiguity."""
        matches = self.find_instances(instance)
        if len(matches) > 1:
            raise DeployError(
                f"{len(matches)} services are running as instance {instance!r}: "
                f"{[m['serviceId'] for m in matches]}. Delete the extras by id first."
            )
        return matches[0] if matches else None

    def wait_until_ready(
        self, service_id: str, *, timeout_s: float = 600.0, interval_s: float = 10.0
    ) -> dict:
        """Poll until DEPLOYED. DEPLOYED means the pod launched — not that it serves."""
        deadline = time.monotonic() + timeout_s
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.service_status(service_id)
            info = last.get("serviceInfo") if isinstance(last, dict) else {}
            if not isinstance(info, dict):
                info = {}
            state = str(info.get("status") or last.get("status") or "").upper()
            if state in READY:
                return last
            if state in FAILED:
                raise DeployError(f"service {service_id} reached {state}: {last}")
            print(f"    status={state or '(unknown)'} — waiting {interval_s:.0f}s", flush=True)
            time.sleep(interval_s)
        raise DeployError(f"timed out after {timeout_s:.0f}s; last status: {last}")


def next_build_version(platform: Platform, name: str, release: str) -> str:
    """``<release>-<n>``: the first build suffix not already uploaded under ``name``.

    The platform rejects re-uploading an existing (name, version), so a rebuild
    of one release needs a fresh build number rather than a version bump.
    """
    taken = {str(p.get("version")) for p in platform.list_packages() if p.get("name") == name}
    for build in range(1, 10_000):
        candidate = f"{release}-{build}"
        if candidate not in taken:
            return candidate
    raise DeployError(f"no free build suffix for {release}")
