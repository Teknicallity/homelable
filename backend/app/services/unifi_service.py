"""UniFi Network inventory service: fetch infrastructure devices via the local API.

Mirrors the Proxmox import pipeline, but talks to the UniFi Network Integration
API (``/proxy/network/integration/v1``) over HTTPS with an API key. It returns
plain homelable node dicts + parent->child edge hints; DB persistence lives in
the route layer (``app.api.routes.unifi``).

Auth is a single header, ``X-API-KEY: <key>``, created under Network ->
Settings -> API. Only the managed devices (switches, APs, gateways) are
imported; wired and wireless clients are left to the IP scanner.

The device list does not carry the uplink, so the parent of each device costs
one extra GET against the per-device detail endpoint. Those are fanned out
concurrently and are best-effort: a device whose detail fails still imports,
just without a parent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.services.mac_utils import normalize_mac
from app.services.zigbee_service import merge_zigbee_properties

logger = logging.getLogger(__name__)

# Reuse the zigbee property-merge contract verbatim (same NodeProperty shape +
# visibility-preservation rules) for re-import updates.
merge_unifi_properties = merge_zigbee_properties

_CONNECT_TIMEOUT = 8.0
_READ_TIMEOUT = 20.0
_API_BASE = "/proxy/network/integration/v1"
_PAGE_LIMIT = 200
# A self-hosted controller is not a datacentre API; keep the detail fan-out
# polite rather than opening one socket per device.
_DETAIL_CONCURRENCY = 8

# `features` is a list; a UDM reports several at once. First match wins, so a
# gateway that also switches files as a router. Order matches the priority
# fingerprint.py already uses for port-based guesses.
_FEATURE_TYPES = (
    ("gateway", "router"),
    ("switching", "switch"),
    ("accessPoint", "ap"),
)


def _sanitize_unifi_error(exc: BaseException) -> str:
    """Return a generic, credential-free message for a UniFi/HTTP error.

    Raw httpx errors can echo the request URL and, in some stacks, the
    ``X-API-KEY`` header — which is a single bearer-equivalent secret with read
    access to the whole controller. Map known patterns to coarse categories so
    the key never reaches an API client. The original is logged at WARNING.
    """
    logger.warning("UniFi error (sanitized for client): %r", exc)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "Authentication failed — check the API key and its permissions"
        if code == 404:
            return "UniFi API path not found — is this a UniFi Network controller?"
        return f"UniFi API returned HTTP {code}"
    raw = str(exc).lower()
    if "name or service not known" in raw or "getaddrinfo" in raw or "nodename nor servname" in raw:
        return "UniFi host could not be resolved"
    if "refused" in raw or "connect" in raw:
        # The single most common setup mistake, and it surfaces with nothing
        # pointing at the cause: a UDM/Cloud Key answers on 443, but UniFi OS
        # Server (software-only) only answers on 11443.
        return "Connection refused by UniFi host — check the port (443 for UDM/Cloud Key, 11443 for UniFi OS Server)"
    if "certificate" in raw or "ssl" in raw or "tls" in raw:
        return "TLS verification failed — enable 'skip TLS verify' for self-signed certs"
    if "timed out" in raw or "timeout" in raw:
        return "Connection to UniFi host timed out"
    return "UniFi connection failed"


def _auth_header(api_key: str) -> dict[str, str]:
    return {"X-API-KEY": api_key, "Accept": "application/json"}


def _clean_name(value: Any) -> str | None:
    """Collapse a controller-supplied name. The live API returns 'Kitchen '."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _feature_type(features: Any) -> str:
    """Map the device's `features` list onto a homelable node type."""
    if not isinstance(features, list):
        return "generic"
    for feature, node_type in _FEATURE_TYPES:
        if feature in features:
            return node_type
    return "generic"


def _ports(raw: dict[str, Any]) -> int | None:
    """Port count, detail-only.

    `interfaces` is polymorphic: the list endpoint returns a list of capability
    names (``["ports"]``), the detail endpoint a dict (``{"ports": [...]}``).
    """
    interfaces = raw.get("interfaces")
    if not isinstance(interfaces, dict):
        return None
    ports = interfaces.get("ports")
    return len(ports) if isinstance(ports, list) else None


def _device_node(raw: dict[str, Any], parent_id: str | None) -> dict[str, Any] | None:
    """Build a homelable node from a device list entry overlaid with its detail."""
    device_id = raw.get("id")
    if not device_id:
        return None
    ieee = f"unifi-{device_id}"
    name = _clean_name(raw.get("name"))
    model = raw.get("model") or None
    return {
        "id": ieee,
        "label": name or model or ieee,
        "type": _feature_type(raw.get("features")),
        "ieee_address": ieee,
        "hostname": name,
        "ip": raw.get("ipAddress") or None,
        "mac": normalize_mac(raw.get("macAddress")),
        "status": "online" if raw.get("state") == "ONLINE" else "offline",
        "vendor": "Ubiquiti",
        "model": model,
        "firmware": raw.get("firmwareVersion") or None,
        "port_count": _ports(raw),
        "parent_ieee": f"unifi-{parent_id}" if parent_id else None,
    }


def build_unifi_properties(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a NodeProperty list for a UniFi device.

    All rows default ``visible=False`` — the user opts in from the right panel,
    same as the Proxmox and mesh importers.
    """
    props: list[dict[str, Any]] = []
    if node.get("model"):
        props.append({"key": "Model", "value": str(node["model"]), "icon": None, "visible": False})
    if node.get("firmware"):
        props.append({"key": "Firmware", "value": str(node["firmware"]), "icon": None, "visible": False})
    if node.get("port_count") is not None:
        props.append({"key": "Ports", "value": str(node["port_count"]), "icon": None, "visible": False})
    props.append({"key": "Source", "value": "UniFi Network", "icon": None, "visible": False})
    return props


def _parse_inventory(
    devices_raw: list[dict[str, Any]], details: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn the device list + per-device details into (nodes, edges)."""
    known = {raw["id"] for raw in devices_raw if raw.get("id")}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for item in devices_raw:
        device_id = item.get("id")
        if not device_id:
            continue
        detail = details.get(device_id)
        raw = {**item, **detail} if isinstance(detail, dict) else dict(item)

        # A device with no `uplink` is the root. A device whose detail fetch
        # failed also has none here, so it imports parentless rather than
        # inventing a link — the warning in _fetch_details is the only signal.
        uplink = raw.get("uplink")
        parent_id = uplink.get("deviceId") if isinstance(uplink, dict) else None
        if parent_id == device_id:
            parent_id = None

        node = _device_node(raw, parent_id)
        if node is None:
            continue
        nodes.append(node)

        # Only record an edge whose parent is a device we actually imported. An
        # uplink can name an unadopted device, and a link to an ieee no row
        # holds would never resolve into a canvas edge.
        if parent_id and parent_id in known:
            edges.append({"source": f"unifi-{parent_id}", "target": node["id"]})
        elif parent_id:
            logger.warning("UniFi device %s uplinks to unknown device %s — link skipped", device_id, parent_id)

    return nodes, edges


async def _get_json(client: httpx.AsyncClient, path: str) -> Any:
    """Single-request helper — and the one mock seam the tests patch."""
    resp = await client.get(path)
    resp.raise_for_status()
    return resp.json()


async def _get_paged(client: httpx.AsyncClient, path: str) -> list[dict[str, Any]]:
    """Collect every page of a list endpoint.

    List responses wrap in ``{offset, limit, count, totalCount, data:[...]}``.
    An empty page also terminates, so a wrong ``totalCount`` cannot spin.
    """
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        sep = "&" if "?" in path else "?"
        payload = await _get_json(client, f"{path}{sep}offset={offset}&limit={_PAGE_LIMIT}")
        if not isinstance(payload, dict):
            raise ValueError(f"Malformed UniFi response for {path}")
        page = payload.get("data")
        if not isinstance(page, list):
            raise ValueError(f"Malformed UniFi response for {path}")
        out.extend(item for item in page if isinstance(item, dict))
        total = payload.get("totalCount")
        offset += len(page)
        if not page or not isinstance(total, int) or offset >= total:
            return out


async def _resolve_site_id(client: httpx.AsyncClient, site_id: str | None) -> str:
    """The site to import from: the caller's choice, else the first visible one."""
    if site_id:
        return site_id
    sites = await _get_paged(client, f"{_API_BASE}/sites")
    for site in sites:
        if site.get("id"):
            return str(site["id"])
    raise ValueError("No UniFi sites are visible to this API key")


async def _fetch_details(
    client: httpx.AsyncClient, site_id: str, device_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Fetch every device's detail concurrently. Best-effort per device."""
    semaphore = asyncio.Semaphore(_DETAIL_CONCURRENCY)

    async def one(device_id: str) -> tuple[str, dict[str, Any] | None]:
        async with semaphore:
            try:
                detail = await _get_json(client, f"{_API_BASE}/sites/{site_id}/devices/{device_id}")
            except httpx.HTTPError as exc:
                logger.warning("UniFi device detail failed for %s: %s", device_id, exc)
                return device_id, None
            return device_id, detail if isinstance(detail, dict) else None

    results = await asyncio.gather(*(one(d) for d in device_ids))
    return {device_id: detail for device_id, detail in results if detail is not None}


async def fetch_unifi_inventory(
    host: str,
    port: int,
    api_key: str,
    site_id: str | None = None,
    verify_tls: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch UniFi infrastructure devices, return (nodes, edges).

    Raises:
        ConnectionError: transport/DNS/TLS failures (sanitized message).
        ValueError: malformed API response, or no visible site.
    """
    timeout = httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT)
    try:
        async with httpx.AsyncClient(
            base_url=f"https://{host}:{port}",
            headers=_auth_header(api_key),
            verify=verify_tls,
            # Never follow a redirect: httpx strips Authorization across hosts
            # but not custom headers, so X-API-KEY would travel to wherever the
            # redirect points.
            follow_redirects=False,
            timeout=timeout,
        ) as client:
            resolved_site = await _resolve_site_id(client, site_id)
            devices_raw = await _get_paged(client, f"{_API_BASE}/sites/{resolved_site}/devices")
            device_ids = [str(raw["id"]) for raw in devices_raw if raw.get("id")]
            details = await _fetch_details(client, resolved_site, device_ids)
    except httpx.HTTPError as exc:
        raise ConnectionError(_sanitize_unifi_error(exc)) from exc

    return _parse_inventory(devices_raw, details)


async def test_unifi_connection(
    host: str,
    port: int,
    api_key: str,
    site_id: str | None = None,
    verify_tls: bool = False,
) -> tuple[bool, str]:
    """Probe the controller. Returns (connected, message); never raises."""
    timeout = httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT)
    try:
        async with httpx.AsyncClient(
            base_url=f"https://{host}:{port}",
            headers=_auth_header(api_key),
            verify=verify_tls,
            follow_redirects=False,
            timeout=timeout,
        ) as client:
            info = await _get_json(client, f"{_API_BASE}/info")
            version = info.get("applicationVersion") if isinstance(info, dict) else None
            resolved_site = await _resolve_site_id(client, site_id)
    except httpx.HTTPError as exc:
        return False, _sanitize_unifi_error(exc)
    except ValueError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - the endpoint must never 500
        logger.exception("UniFi connection test failed")
        return False, _sanitize_unifi_error(exc)

    label = f"UniFi Network {version}" if version else "UniFi Network"
    return True, f"Connected to {label} (site {resolved_site})"
