"""FastAPI router for UniFi Network import.

Fetches managed devices (switches, APs, gateways) from the UniFi Network
Integration API and upserts them into the pending inventory (same review->approve
flow as scans and the other imports).

Connection settings: every field comes from the request body when provided, else
falls back to the server-configured env values (``settings.unifi_*``), so a
one-off import can be driven entirely from the dialog while a configured server
needs nothing typed. The API key is never persisted by the app and never
returned by any endpoint.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import delete as sa_delete
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.db.database import AsyncSessionLocal, get_db
from app.db.models import InventoryDevice, InventoryDeviceLink, Node, ScanRun
from app.schemas.scan import ScanRunResponse
from app.schemas.unifi import (
    UnifiConfig,
    UnifiConnectionRequest,
    UnifiEdgeOut,
    UnifiImportPendingResponse,
    UnifiImportResponse,
    UnifiNodeOut,
    UnifiTestConnectionResponse,
)
from app.services.device_merge import reconcile_duplicates
from app.services.discovery_sources import add_source
from app.services.inventory_sync import attach_device_ids
from app.services.mac_utils import normalize_mac
from app.services.node_dedupe import dedupe_nodes_by_device
from app.services.unifi_service import (
    build_unifi_properties,
    fetch_unifi_inventory,
    merge_unifi_properties,
    test_unifi_connection,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# UniFi has one link shape: an uplink from a device to its parent, rendered as
# an 'ethernet' edge (see the dispatch in app/api/routes/scan.py).
_UNIFI_SOURCE = "unifi"
# Every imported device carries a synthetic ieee under this prefix
# (``unifi-{deviceId}``), built in unifi_service. The prefix is what tells an
# imported row apart from a scanned one, so it must not be dropped.
_UNIFI_IEEE_PREFIX = "unifi-"


def _resolve_connection(payload: UnifiConnectionRequest) -> tuple[str, int, str, str | None, bool]:
    """Connection settings for this request: body first, else the server env.

    Raises HTTP 400 when neither carries a host or an API key.
    """
    host = payload.host or settings.unifi_host
    port = payload.port or settings.unifi_port
    api_key = payload.api_key or settings.unifi_api_key
    site_id = payload.site_id or settings.unifi_site_id or None
    verify_tls = payload.verify_tls if payload.verify_tls is not None else settings.unifi_verify_tls
    if not host:
        raise HTTPException(
            status_code=400,
            detail="No UniFi host provided and none configured on the server.",
        )
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="No UniFi API key provided and none configured on the server.",
        )
    return host, port, api_key, site_id, verify_tls


@router.post("/test-connection", response_model=UnifiTestConnectionResponse)
async def test_connection_endpoint(
    payload: UnifiConnectionRequest,
    _: str = Depends(get_current_user),
) -> UnifiTestConnectionResponse:
    """Validate host reachability + API key before importing."""
    host, port, api_key, site_id, verify_tls = _resolve_connection(payload)
    connected, message = await test_unifi_connection(
        host=host,
        port=port,
        api_key=api_key,
        site_id=site_id,
        verify_tls=verify_tls,
    )
    return UnifiTestConnectionResponse(connected=connected, message=message)


@router.post("/import", response_model=UnifiImportResponse)
async def import_unifi(
    payload: UnifiConnectionRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> UnifiImportResponse:
    """Fetch the inventory and return nodes + edges ready for canvas drop."""
    host, port, api_key, site_id, verify_tls = _resolve_connection(payload)
    try:
        nodes_raw, edges_raw = await fetch_unifi_inventory(
            host=host,
            port=port,
            api_key=api_key,
            site_id=site_id,
            verify_tls=verify_tls,
        )
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error during UniFi import")
        raise HTTPException(status_code=500, detail="Unexpected error during UniFi import") from exc

    # A canvas import is an import: the devices land in the Device Inventory too,
    # and each node carries the id of the row it draws so the canvas save links
    # to it instead of minting a second row for the same device.
    await _persist_pending_import(db, nodes_raw, edges_raw)
    nodes_raw = await attach_device_ids(db, nodes_raw)

    nodes = [UnifiNodeOut(**n) for n in nodes_raw]
    edges = [UnifiEdgeOut(**e) for e in edges_raw]
    return UnifiImportResponse(nodes=nodes, edges=edges, device_count=len(nodes))


@router.post("/import-pending", response_model=ScanRunResponse)
async def import_unifi_to_pending(
    payload: UnifiConnectionRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> ScanRun:
    """Queue a UniFi pending import as a background scan run (kind=unifi)."""
    host, port, api_key, site_id, verify_tls = _resolve_connection(payload)
    run = ScanRun(
        status="running",
        kind="unifi",
        ranges=[f"{host}:{port}"],
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    background_tasks.add_task(
        _background_unifi_import,
        run.id,
        host,
        port,
        api_key,
        site_id,
        verify_tls,
    )
    return run


@router.get("/config", response_model=UnifiConfig)
async def get_unifi_config(_: str = Depends(get_current_user)) -> UnifiConfig:
    """Return non-secret UniFi config so the import dialog can prefill. Never
    includes the API key — only whether one is configured on the server."""
    return UnifiConfig(
        host=settings.unifi_host,
        port=settings.unifi_port,
        site_id=settings.unifi_site_id,
        verify_tls=settings.unifi_verify_tls,
        api_key_configured=bool(settings.unifi_api_key),
    )


async def _background_unifi_import(
    run_id: str,
    host: str,
    port: int,
    api_key: str,
    site_id: str | None,
    verify_tls: bool,
) -> None:
    async with AsyncSessionLocal() as db:
        try:
            nodes_raw, edges_raw = await fetch_unifi_inventory(
                host=host,
                port=port,
                api_key=api_key,
                site_id=site_id,
                verify_tls=verify_tls,
            )
            result = await _persist_pending_import(db, nodes_raw, edges_raw)
            run = await db.get(ScanRun, run_id)
            if run:
                run.status = "done"
                run.devices_found = result.device_count
                run.finished_at = datetime.now(timezone.utc)
                await db.commit()
            # Nudge the frontend to reload the inventory (same signal the IP scan
            # emits) so imported/merged devices appear without a manual refresh.
            from app.api.routes.status import broadcast_scan_update
            await broadcast_scan_update(run_id=run_id, devices_found=result.device_count)
        except Exception as exc:
            logger.exception("UniFi import %s failed", run_id)
            await db.rollback()
            run = await db.get(ScanRun, run_id)
            if run:
                run.status = "error"
                run.error = str(exc)[:500]
                run.finished_at = datetime.now(timezone.utc)
                await db.commit()


async def _persist_pending_import(
    db: AsyncSession,
    nodes_raw: list[dict[str, Any]],
    edges_raw: list[dict[str, Any]],
) -> UnifiImportPendingResponse:
    """Upsert UniFi devices/links into device_inventory + device_inventory_links.

    Two-tier identity (order matters) — see ``_find_pending``:
      1. Match by the synthetic ``ieee_address`` (``unifi-{deviceId}``).
      2. Else match an existing row by MAC, then IP, to merge into a device a
         scan found first — never duplicate. Rows already claimed by another
         UniFi device are excluded from the IP fallback.

    Update-in-place only. Nothing is ever deleted; hidden rows stay hidden.
    """
    await dedupe_nodes_by_device(db)

    pending_created = 0
    pending_updated = 0

    for n in nodes_raw:
        ieee = n.get("ieee_address")
        if not ieee:
            continue
        ip = n.get("ip")
        mac = normalize_mac(n.get("mac"))
        props = build_unifi_properties(n)

        # Match by ieee OR mac OR ip. MAC is the cross-source dedup key and the
        # reason this import is worth having: on a Docker bridge network the
        # scanner cannot see one, so its row for this device carries only an IP.
        pending = await _find_pending(db, ieee, ip, mac)
        drawn = bool(
            pending
            and (
                await db.execute(select(Node.id).where(Node.device_id == pending.id).limit(1))
            ).scalar_one_or_none()
        )

        if pending is None:
            db.add(_new_pending(ieee, ip, mac, n, props, status="pending"))
            pending_created += 1
        else:
            if drawn:
                # Already on a canvas: refresh the facts but leave the lifecycle
                # alone (_refresh_pending would revive it as pending).
                await _ensure_inventory_row(db, ieee, ip, mac, n, props, approved=True)
            else:
                _refresh_pending(pending, ieee, ip, mac, n, props)
            pending_updated += 1

    # Fold in any row this import just proved to be the same device: a device
    # matched here by its synthetic ieee can share a MAC with a row an earlier
    # scan minted before either side carried the address that would match them.
    await reconcile_duplicates(db)

    links_recorded = await _replace_links(db, edges_raw)
    await db.commit()

    return UnifiImportPendingResponse(
        pending_created=pending_created,
        pending_updated=pending_updated,
        links_recorded=links_recorded,
        device_count=len(nodes_raw),
    )


async def _find_pending(
    db: AsyncSession, ieee: str, ip: str | None, mac: str | None
) -> InventoryDevice | None:
    """The inventory row this device *is*, in strict precedence order.

    1. The synthetic ``ieee_address`` (``unifi-{deviceId}``).
    2. Else the MAC, else the IP — merging into a row an IP/ARP scan found
       first rather than duplicating it.

    An IP is not an identity: a re-used DHCP lease or a re-addressed device
    makes one address describe several machines over time. So the IP fallback
    skips any row already claimed by another UniFi device. Oldest row wins, so
    a re-import is stable.

    A MAC *is* an identity, so its fallback matches a claimed row too. It has
    to: forgetting and re-adopting a device mints a new deviceId for the same
    hardware, and excluding claimed rows would file it as a new device and
    orphan the old row. See ``_adopted_ieee``.
    """
    exact = (
        await db.execute(
            select(InventoryDevice).where(InventoryDevice.ieee_address == ieee)
        )
    ).scalars().first()
    if exact is not None:
        return exact

    unclaimed = or_(
        InventoryDevice.ieee_address.is_(None),
        ~InventoryDevice.ieee_address.startswith(_UNIFI_IEEE_PREFIX),
    )
    for column, value, claimed_ok in (
        (InventoryDevice.mac, mac, True),
        (InventoryDevice.ip, ip, False),
    ):
        if not value:
            continue
        where = [column == value] if claimed_ok else [column == value, unclaimed]
        row = (
            await db.execute(
                select(InventoryDevice)
                .where(*where)
                .order_by(InventoryDevice.discovered_at, InventoryDevice.id)
            )
        ).scalars().first()
        if row is not None:
            return row
    return None


def _adopted_ieee(current: str | None, ieee: str) -> str:
    """The ``ieee_address`` a row should carry once this device merges into it.

    Re-point a row that already belongs to a UniFi device: matched by MAC, it is
    this same hardware under a stale deviceId (forgotten and re-adopted). Links
    are rebuilt keyed on the ieee, so a row left on the old one stops resolving
    as an uplink endpoint. A mesh ieee is a real hardware address and is never
    overwritten.
    """
    if not current or current.startswith(_UNIFI_IEEE_PREFIX):
        return ieee
    return current


def _new_pending(
    ieee: str,
    ip: str | None,
    mac: str | None,
    n: dict[str, Any],
    props: list[dict[str, Any]],
    status: str,
) -> InventoryDevice:
    return InventoryDevice(
        ieee_address=ieee,
        ip=ip,
        mac=mac,
        hostname=n.get("hostname"),
        friendly_name=n.get("label"),
        suggested_type=n.get("type"),
        vendor=n.get("vendor"),
        model=n.get("model"),
        properties=props,
        status=status,
        discovery_source=_UNIFI_SOURCE,
        discovery_sources=[_UNIFI_SOURCE],
    )


def _sources_after_merge(row: InventoryDevice) -> list[str]:
    """Discovery sources for an inventory row after a UniFi import merges in.

    Must run BEFORE the ``unifi-`` ieee is adopted onto the row, so it can tell
    whether the row was originally a scanned device. Preserves the prior scan
    origin — including legacy rows created before ``discovery_sources`` existed
    (empty list) and possibly with a NULL ``discovery_source`` — so the IP tag
    survives the merge.
    """
    sources = add_source(row.discovery_sources, row.discovery_source)
    was_scanned = not (row.ieee_address or "").startswith(_UNIFI_IEEE_PREFIX)
    if was_scanned and row.ip and not any(s in ("arp", "mdns") for s in sources):
        sources = add_source(sources, "arp")
    return add_source(sources, _UNIFI_SOURCE)


def _refresh_pending(
    pending: InventoryDevice,
    ieee: str,
    ip: str | None,
    mac: str | None,
    n: dict[str, Any],
    props: list[dict[str, Any]],
) -> None:
    # Compute sources before adopting the unifi ieee (needs the pre-merge origin).
    pending.discovery_sources = _sources_after_merge(pending)
    pending.ieee_address = _adopted_ieee(pending.ieee_address, ieee)
    pending.ip = ip or pending.ip
    # The MAC the controller reports is authoritative — a scanner row on a
    # bridge network has none at all, which is the gap this import closes.
    pending.mac = mac or pending.mac
    pending.hostname = n.get("hostname") or pending.hostname
    pending.friendly_name = n.get("label") or pending.friendly_name
    pending.suggested_type = n.get("type") or pending.suggested_type
    pending.vendor = n.get("vendor") or pending.vendor
    pending.model = n.get("model") or pending.model
    pending.properties = merge_unifi_properties(list(pending.properties or []), props)
    if pending.status == "approved":
        # Approved earlier but the canvas Node is gone — revive so it reappears.
        pending.status = "pending"
    # hidden stays hidden.


async def _ensure_inventory_row(
    db: AsyncSession,
    ieee: str,
    ip: str | None,
    mac: str | None,
    n: dict[str, Any],
    props: list[dict[str, Any]],
    approved: bool,
) -> None:
    """Ensure an inventory row exists for a device already on a canvas, so it
    shows in the inventory with an 'In N canvas' badge. Never changes status."""
    inv = await _find_pending(db, ieee, ip, mac)
    if inv is None:
        db.add(_new_pending(ieee, ip, mac, n, props, status="approved" if approved else "pending"))
    else:
        # Compute sources before adopting the unifi ieee (needs the pre-merge origin).
        inv.discovery_sources = _sources_after_merge(inv)
        inv.ieee_address = _adopted_ieee(inv.ieee_address, ieee)
        inv.ip = ip or inv.ip
        inv.mac = mac or inv.mac
        inv.hostname = n.get("hostname") or inv.hostname
        inv.suggested_type = n.get("type") or inv.suggested_type
        inv.properties = merge_unifi_properties(list(inv.properties or []), props)


async def _replace_links(db: AsyncSession, edges_raw: list[dict[str, Any]]) -> int:
    """Wipe all unifi-source links and re-insert the freshly discovered set."""
    await db.execute(
        sa_delete(InventoryDeviceLink).where(
            InventoryDeviceLink.discovery_source == _UNIFI_SOURCE
        )
    )
    recorded = 0
    seen: set[tuple[str, str]] = set()

    for e in edges_raw:
        src = e.get("source")
        tgt = e.get("target")
        if not src or not tgt or (src, tgt) in seen:
            continue
        seen.add((src, tgt))
        db.add(InventoryDeviceLink(source_ieee=src, target_ieee=tgt, discovery_source=_UNIFI_SOURCE))
        recorded += 1

    return recorded
