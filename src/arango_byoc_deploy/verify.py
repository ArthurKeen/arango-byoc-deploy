"""Prove the right code is live and serving — not merely that a pod launched.

Three failures each passed a naive health check somewhere in the estate:

* the pod reported DEPLOYED while the entrypoint was still installing
  dependencies, so the first probes 404'd;
* a service answered every endpoint it defined but 404'd at its bare mount
  root — the path the platform's Apps view actually opens — so the launcher
  showed "App Not Responding" behind a green light;
* a mount-prefix mismatch served an HTML shell whose assets all 404'd: a blank
  page behind a 200.

So the root is polled first, every relative asset it references must load, and
each repository adds probes that prove the app is talking to the right data.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import requests

from .config import AppConfig, Probe
from .platform import Platform

#: Relative URLs only — not "/", "http" or "//". Root-absolute assets are the
#: prefix bug; pre-flight rejects them before upload.
_RELATIVE_ASSET = re.compile(r'(?:src|href)="(?!https?:|//|/|#|data:|mailto:)([^"]+)"')

#: What each non-200 means while a service comes up. Observed on the pilot
#: cluster: the gateway answers 401 to an authenticated caller until the pod is
#: ready, so 401 is a cold-start signal here, not an auth failure.
_COLD_START_HINTS = {
    404: "route not registered yet",
    401: "gateway/pod not ready yet",
    502: "upstream not ready",
    503: "pod not ready",
}


@dataclass
class Result:
    ok: bool
    lines: list[str]

    def report(self) -> None:
        for line in self.lines:
            print(line)
        print("    => VERIFIED" if self.ok else "    => VERIFICATION FAILED")


def poll_until_serving(
    platform: Platform, url: str, *, timeout_s: float = 600.0, interval_s: float = 15.0
) -> requests.Response | None:
    """GET the mount root until it returns 200, or give up."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            response = platform.get(url, allow_redirects=False)
            if response.status_code == 200:
                return response
            hint = _COLD_START_HINTS.get(response.status_code, "not serving yet")
            print(f"    HTTP {response.status_code} ({hint}) — retrying in {interval_s:.0f}s", flush=True)
        except requests.RequestException as exc:
            print(f"    {type(exc).__name__} — retrying in {interval_s:.0f}s", flush=True)
        time.sleep(interval_s)
    return None


def _check_probe(platform: Platform, base: str, probe: Probe) -> tuple[bool, str]:
    label = probe.label or probe.path
    try:
        response = platform.get(base + probe.path.lstrip("/"), timeout=probe.timeout)
    except requests.RequestException as exc:
        return False, f"    [FAIL] {label:18s} {type(exc).__name__}"
    if response.status_code != 200:
        return False, f"    [FAIL] {label:18s} HTTP {response.status_code}"

    if probe.expect_json_key is None and probe.min_items is None:
        return True, f"    [ OK ] {label:18s} 200"

    try:
        body = response.json()
    except ValueError:
        return False, f"    [FAIL] {label:18s} expected JSON, got {response.text[:40]!r}"

    value = body
    if probe.expect_json_key is not None:
        if not isinstance(body, dict) or probe.expect_json_key not in body:
            return False, f"    [FAIL] {label:18s} no {probe.expect_json_key!r} in response"
        value = body[probe.expect_json_key]

    if probe.min_items is not None:
        count = len(value) if isinstance(value, (list, dict)) else 0
        if count < probe.min_items:
            # The check that catches "healthy service, empty or wrong database".
            return False, (
                f"    [FAIL] {label:18s} {count} item(s), expected >= {probe.min_items} "
                f"— wrong or partially loaded database?"
            )
        return True, f"    [ OK ] {label:18s} {count} item(s)"
    return True, f"    [ OK ] {label:18s} 200"


def deep_verify(platform: Platform, base: str, config: AppConfig, root: requests.Response) -> Result:
    """Everything beyond "the root answered". ``base`` ends with ``/``."""
    lines: list[str] = ["    [ OK ] app root           200"]
    ok = True

    if config.has_ui:
        assets = list(dict.fromkeys(_RELATIVE_ASSET.findall(root.text)))
        broken = []
        for asset in assets:
            try:
                response = platform.get(base + asset.lstrip("./"))
                if response.status_code != 200:
                    broken.append((response.status_code, asset))
            except requests.RequestException as exc:
                broken.append((type(exc).__name__, asset))
        if not assets:
            ok = False
            lines.append("    [FAIL] assets             the root references no relative assets — wrong page?")
        else:
            tag = "FAIL" if broken else " OK "
            served = len(assets) - len(broken)
            lines.append(f"    [{tag}] assets             {served}/{len(assets)} served")
            for code, asset in broken:
                ok = False
                lines.append(f"           {code} {asset}")

    for probe in config.probes:
        passed, line = _check_probe(platform, base, probe)
        ok = ok and passed
        lines.append(line)

    return Result(ok=ok, lines=lines)
