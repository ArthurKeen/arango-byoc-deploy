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
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .config import AppConfig, Probe, VersionProbe
from .platform import Platform

#: Relative URLs only — not "/", "http" or "//". Root-absolute assets are the
#: prefix bug; pre-flight rejects them before upload.
_REF = re.compile(r'(?:src|href)="([^"]+)"')
_SKIP_REF = ("http://", "https://", "//", "#", "data:", "mailto:", "javascript:")


def page_assets(html: str, page_url: str, mount: str) -> tuple[list[str], list[str]]:
    """``(urls, outside)`` for every same-host reference on a page.

    References resolve the way a browser resolves them (``urljoin``), so a
    relative ``./assets/x.js`` and a prefix-baked absolute
    ``/_service/uds/_db/d/app/_next/x.js`` (Next.js ``basePath``) both land
    under the mount. Anything that resolves *outside* the mount prefix is the
    prefix bug — it would load from the cluster root — and is returned in
    ``outside``. External and in-page references are skipped.
    """
    urls: list[str] = []
    outside: list[str] = []
    prefix = mount.rstrip("/") + "/"
    for ref in dict.fromkeys(_REF.findall(html)):
        if ref.startswith(_SKIP_REF):
            continue
        url = urljoin(page_url, ref)
        path = urlparse(url).path
        if path.startswith(prefix) or path == prefix.rstrip("/"):
            urls.append(url)
        else:
            outside.append(ref)
    return urls, outside


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

    if not probe.keys and probe.min_items is None and probe.must_contain is None:
        return True, f"    [ OK ] {label:18s} 200"

    try:
        body = response.json()
    except ValueError:
        return False, f"    [FAIL] {label:18s} expected JSON, got {response.text[:40]!r}"

    notes: list[str] = []
    for key in probe.keys or ("",):
        found, value = resolve(body, key) if key else (True, body)
        name = key or "body"
        if not found:
            return False, f"    [FAIL] {label:18s} no {name!r} in response"
        if probe.min_items is not None:
            size = _size(value)
            if size < probe.min_items:
                # The check that catches "healthy service, empty or wrong database".
                return False, (
                    f"    [FAIL] {label:18s} {name}={size}, expected >= {probe.min_items} "
                    f"— wrong or partially loaded database?"
                )
            notes.append(f"{name}={size}")
        if probe.must_contain is not None:
            if not isinstance(value, list) or probe.must_contain not in value:
                return False, f"    [FAIL] {label:18s} {name} does not include {probe.must_contain!r}"
            notes.append(f"{name} has {probe.must_contain!r}")
    return True, f"    [ OK ] {label:18s} {', '.join(notes) or '200'}"


def resolve(body: Any, key: str) -> tuple[bool, Any]:
    """``(found, value)`` for a dotted key; ``name[]`` flattens a list.

    ``windows[].id`` on ``{"windows": [{"id": "a"}, {"id": "b"}]}`` is
    ``(True, ["a", "b"])``.
    """
    values: list[Any] = [body]
    flattened = False
    for part in key.split("."):
        spread = part.endswith("[]")
        name = part[:-2] if spread else part
        nxt: list[Any] = []
        for current in values:
            if not isinstance(current, dict) or name not in current:
                if not flattened:
                    return False, None
                continue
            item = current[name]
            if spread:
                if not isinstance(item, list):
                    return False, None
                nxt.extend(item)
            else:
                nxt.append(item)
        values = nxt
        flattened = flattened or spread
    if flattened:
        return True, values
    return (True, values[0]) if values else (False, None)


def _size(value: Any) -> float:
    """List/dict length, or a number's own value (``events: 51234``)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, dict, str)):
        return len(value)
    return 0


def _dotted(value: Any, key: str) -> Any:
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _check_version(
    platform: Platform, base: str, probe: VersionProbe, expected: str | None
) -> tuple[bool, str]:
    """The live version, compared with *expected* when one is demanded.

    Fails closed when a version was demanded but cannot be read: an unreadable
    endpoint means the right build cannot be proven live.
    """
    label = "live version"
    try:
        response = platform.get(base + probe.path.lstrip("/"))
        live = _dotted(response.json(), probe.json_key) if response.status_code == 200 else None
    except (requests.RequestException, ValueError) as exc:
        live, detail = None, type(exc).__name__
    else:
        detail = f"HTTP {response.status_code}"
    if expected is None:
        return True, f"    [ -- ] {label:18s} {live if live is not None else f'unreadable ({detail})'}"
    if live is None:
        return False, f"    [FAIL] {label:18s} unreadable at {probe.path} ({detail}); expected {expected}"
    if str(live) != expected:
        return False, f"    [FAIL] {label:18s} {live}, expected {expected} — the old build is still serving"
    return True, f"    [ OK ] {label:18s} {live}"


def deep_verify(
    platform: Platform, base: str, config: AppConfig, expect_version: str | None = None
) -> Result:
    """Everything beyond "it answered". ``base`` ends with ``/``.

    With a UI, the bare mount root must return 200 — it is where the platform's
    app launcher opens the service — and every relative asset it references
    must load. Checked here, not left to the readiness poll, because the poll
    may target ``/health``, which answers even when the root does not.
    """
    lines: list[str] = []
    ok = True

    if config.has_ui:
        try:
            root = platform.get(base, allow_redirects=False)
        except requests.RequestException as exc:
            return Result(ok=False, lines=[f"    [FAIL] app root           {type(exc).__name__}"])
        if root.status_code != 200:
            return Result(
                ok=False,
                lines=[
                    f"    [FAIL] app root           HTTP {root.status_code} — the app launcher opens here"
                ],
            )
        lines.append("    [ OK ] app root           200")
        page_url, page = base, root
        if config.asset_page:
            # Some apps verify a deeper page (agentic-graph-analytics: /workspace/).
            page_url = base + config.asset_page.lstrip("/")
            try:
                page = platform.get(page_url, allow_redirects=False)
            except requests.RequestException as exc:
                return Result(ok=False, lines=[*lines, f"    [FAIL] asset page         {type(exc).__name__}"])
            if page.status_code != 200:
                return Result(
                    ok=False, lines=[*lines, f"    [FAIL] asset page         HTTP {page.status_code}"]
                )
        assets, outside = page_assets(page.text, page_url, urlparse(base).path)
        for ref in outside:
            ok = False
            lines.append(f"    [FAIL] outside prefix     {ref} (would load from the cluster root)")
        broken = []
        for asset in assets:
            try:
                response = platform.get(asset)
                if response.status_code != 200:
                    broken.append((response.status_code, asset))
            except requests.RequestException as exc:
                broken.append((type(exc).__name__, asset))
        if not assets:
            ok = False
            lines.append(
                "    [FAIL] assets             the page references no assets under the mount — wrong page?"
            )
        else:
            tag = "FAIL" if broken else " OK "
            served = len(assets) - len(broken)
            lines.append(f"    [{tag}] assets             {served}/{len(assets)} served")
            for code, asset in broken:
                ok = False
                lines.append(f"           {code} {asset}")

    if config.version_probe is not None:
        passed, line = _check_version(platform, base, config.version_probe, expect_version)
        ok = ok and passed
        lines.append(line)

    for probe in config.probes:
        passed, line = _check_probe(platform, base, probe)
        ok = ok and passed
        lines.append(line)

    return Result(ok=ok, lines=lines)
