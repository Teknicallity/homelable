"""Tests for the UniFi import router: endpoints, identity/merge, links."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.unifi import _persist_pending_import, _replace_links
from app.db.models import Design, InventoryDevice, InventoryDeviceLink, Node

CORE = "93ea5dc5-aded-3df1-b357-ac8ed1d4eb8e"
ULTRA = "b5885f9f-e9aa-3e01-ab79-a9c172b5ed61"


def _device(device_id: str, label: str, ip: str, mac: str, node_type: str = "switch") -> dict:
    return {
        "id": f"unifi-{device_id}",
        "ieee_address": f"unifi-{device_id}",
        "label": label,
        "type": node_type,
        "hostname": label,
        "ip": ip,
        "mac": mac,
        "status": "online",
        "vendor": "Ubiquiti",
        "model": "USW Pro 24 PoE",
        "firmware": "7.1.26",
        "port_count": 24,
        "parent_ieee": None,
    }


@pytest.fixture(autouse=True)
def _clear_env_config():
    """Keep a developer's real .env out of the request-body tests."""
    from app.core.config import settings

    saved = (settings.unifi_api_key, settings.unifi_host, settings.unifi_port)
    settings.unifi_api_key = ""
    settings.unifi_host = ""
    yield settings
    settings.unifi_api_key, settings.unifi_host, settings.unifi_port = saved


# --- Endpoints ---------------------------------------------------------------

async def test_import_requires_auth(client: AsyncClient):
    res = await client.post("/api/v1/unifi/import", json={"host": "h", "api_key": "k"})
    assert res.status_code == 401


async def test_missing_host_is_rejected(client: AsyncClient, headers):
    res = await client.post("/api/v1/unifi/import", json={"api_key": "k"}, headers=headers)
    assert res.status_code == 400
    assert "host" in res.json()["detail"].lower()


async def test_missing_api_key_is_rejected(client: AsyncClient, headers):
    res = await client.post("/api/v1/unifi/import", json={"host": "h"}, headers=headers)
    assert res.status_code == 400
    assert "api key" in res.json()["detail"].lower()


async def test_connection_settings_fall_back_to_server_env(client: AsyncClient, headers, _clear_env_config):
    """An empty body is a valid request when the server is configured."""
    _clear_env_config.unifi_host = "10.1.1.10"
    _clear_env_config.unifi_port = 11443
    _clear_env_config.unifi_api_key = "env-key"

    with patch("app.api.routes.unifi.fetch_unifi_inventory", new_callable=AsyncMock) as mock:
        mock.return_value = ([], [])
        res = await client.post("/api/v1/unifi/import", json={}, headers=headers)

    assert res.status_code == 200
    assert mock.await_args.kwargs["host"] == "10.1.1.10"
    assert mock.await_args.kwargs["port"] == 11443
    assert mock.await_args.kwargs["api_key"] == "env-key"


async def test_request_body_overrides_server_env(client: AsyncClient, headers, _clear_env_config):
    _clear_env_config.unifi_host = "10.1.1.10"
    _clear_env_config.unifi_port = 11443
    _clear_env_config.unifi_api_key = "env-key"

    with patch("app.api.routes.unifi.fetch_unifi_inventory", new_callable=AsyncMock) as mock:
        mock.return_value = ([], [])
        res = await client.post(
            "/api/v1/unifi/import",
            json={"host": "other", "port": 443, "api_key": "body-key"},
            headers=headers,
        )

    assert res.status_code == 200
    assert mock.await_args.kwargs["host"] == "other"
    assert mock.await_args.kwargs["port"] == 443
    assert mock.await_args.kwargs["api_key"] == "body-key"


@pytest.mark.parametrize(
    ("exc", "status"),
    [(ConnectionError("nope"), 502), (ValueError("bad"), 422), (RuntimeError("boom"), 500)],
)
async def test_import_maps_failures_to_status_codes(client: AsyncClient, headers, exc, status):
    with patch("app.api.routes.unifi.fetch_unifi_inventory", new_callable=AsyncMock) as mock:
        mock.side_effect = exc
        res = await client.post(
            "/api/v1/unifi/import", json={"host": "h", "api_key": "k"}, headers=headers
        )
    assert res.status_code == status


async def test_config_never_returns_the_api_key(client: AsyncClient, headers, _clear_env_config):
    _clear_env_config.unifi_api_key = "super-secret"
    _clear_env_config.unifi_host = "10.1.1.10"
    res = await client.get("/api/v1/unifi/config", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["api_key_configured"] is True
    assert body["host"] == "10.1.1.10"
    assert "super-secret" not in res.text


async def test_import_returns_nodes_with_mac(client: AsyncClient, headers):
    """UnifiNodeOut must carry the MAC — it is the point of the import."""
    nodes = [_device(CORE, "core", "10.1.1.3", "58:d6:1f:7e:81:a4")]
    with patch("app.api.routes.unifi.fetch_unifi_inventory", new_callable=AsyncMock) as mock:
        mock.return_value = (nodes, [])
        res = await client.post(
            "/api/v1/unifi/import", json={"host": "h", "api_key": "k"}, headers=headers
        )
    assert res.status_code == 200
    assert res.json()["nodes"][0]["mac"] == "58:d6:1f:7e:81:a4"


# --- Persistence / identity --------------------------------------------------

async def test_persist_creates_pending_rows(db_session: AsyncSession):
    nodes = [
        _device(CORE, "core", "10.1.1.3", "58:d6:1f:7e:81:a4"),
        _device(ULTRA, "ultra", "10.1.0.41", "1c:6a:1b:67:48:2d"),
    ]
    result = await _persist_pending_import(db_session, nodes, [])
    assert result.pending_created == 2

    rows = (await db_session.execute(select(InventoryDevice))).scalars().all()
    assert {r.discovery_source for r in rows} == {"unifi"}
    assert all(r.discovery_sources == ["unifi"] for r in rows)
    assert all(r.status == "pending" for r in rows)


async def test_reimport_is_idempotent(db_session: AsyncSession):
    nodes = [_device(CORE, "core", "10.1.1.3", "58:d6:1f:7e:81:a4")]
    await _persist_pending_import(db_session, nodes, [])
    result = await _persist_pending_import(db_session, nodes, [])
    assert result.pending_created == 0
    assert result.pending_updated == 1
    assert len((await db_session.execute(select(InventoryDevice))).scalars().all()) == 1


async def test_merges_into_a_scanned_row_by_mac(db_session: AsyncSession):
    db_session.add(
        InventoryDevice(
            ip="10.1.1.3", mac="58:d6:1f:7e:81:a4", hostname="unknown",
            status="pending", discovery_source="arp", discovery_sources=["arp"],
        )
    )
    await db_session.commit()

    nodes = [_device(CORE, "core", "10.1.1.3", "58:d6:1f:7e:81:a4")]
    await _persist_pending_import(db_session, nodes, [])

    rows = (await db_session.execute(select(InventoryDevice))).scalars().all()
    assert len(rows) == 1
    assert rows[0].ieee_address == f"unifi-{CORE}"
    assert set(rows[0].discovery_sources) == {"arp", "unifi"}


async def test_merges_into_a_macless_scanned_row_by_ip(db_session: AsyncSession):
    """On a Docker bridge network the scanner records no MAC, so IP is the only
    thing joining its row to the controller's — and the import supplies the MAC."""
    db_session.add(
        InventoryDevice(
            ip="10.1.1.3", mac=None, hostname="unknown",
            status="pending", discovery_source="arp", discovery_sources=["arp"],
        )
    )
    await db_session.commit()

    nodes = [_device(CORE, "core", "10.1.1.3", "58:d6:1f:7e:81:a4")]
    await _persist_pending_import(db_session, nodes, [])

    rows = (await db_session.execute(select(InventoryDevice))).scalars().all()
    assert len(rows) == 1
    assert rows[0].mac == "58:d6:1f:7e:81:a4"
    assert rows[0].ieee_address == f"unifi-{CORE}"


async def test_readopted_device_repoints_instead_of_duplicating(db_session: AsyncSession):
    """Forget + re-adopt mints a new deviceId for the same hardware."""
    await _persist_pending_import(
        db_session, [_device(CORE, "core", "10.1.1.3", "58:d6:1f:7e:81:a4")], []
    )
    new_id = "11111111-2222-3333-4444-555555555555"
    await _persist_pending_import(
        db_session, [_device(new_id, "core", "10.1.1.3", "58:d6:1f:7e:81:a4")], []
    )

    rows = (await db_session.execute(select(InventoryDevice))).scalars().all()
    assert len(rows) == 1
    assert rows[0].ieee_address == f"unifi-{new_id}"


async def test_ip_fallback_never_steals_another_unifi_row(db_session: AsyncSession):
    """Two devices sharing an IP must not collapse onto one row."""
    await _persist_pending_import(
        db_session, [_device(CORE, "core", "10.1.1.3", "58:d6:1f:7e:81:a4")], []
    )
    # Same IP, different hardware and no MAC match.
    await _persist_pending_import(
        db_session, [_device(ULTRA, "ultra", "10.1.1.3", "1c:6a:1b:67:48:2d")], []
    )

    rows = (await db_session.execute(select(InventoryDevice))).scalars().all()
    assert {r.ieee_address for r in rows} == {f"unifi-{CORE}", f"unifi-{ULTRA}"}


async def test_hidden_rows_stay_hidden(db_session: AsyncSession):
    db_session.add(
        InventoryDevice(
            ieee_address=f"unifi-{CORE}", ip="10.1.1.3", mac="58:d6:1f:7e:81:a4",
            status="hidden", discovery_source="unifi", discovery_sources=["unifi"],
        )
    )
    await db_session.commit()

    await _persist_pending_import(
        db_session, [_device(CORE, "core", "10.1.1.3", "58:d6:1f:7e:81:a4")], []
    )
    row = (await db_session.execute(select(InventoryDevice))).scalars().one()
    assert row.status == "hidden"


async def test_a_mesh_ieee_is_never_overwritten(db_session: AsyncSession):
    """A real hardware IEEE outranks our synthetic one."""
    db_session.add(
        InventoryDevice(
            ieee_address="0x00124b0022a1b2c3", ip="10.1.1.3", mac="58:d6:1f:7e:81:a4",
            status="pending", discovery_source="zigbee", discovery_sources=["zigbee"],
        )
    )
    await db_session.commit()

    await _persist_pending_import(
        db_session, [_device(CORE, "core", "10.1.1.3", "58:d6:1f:7e:81:a4")], []
    )
    row = (await db_session.execute(select(InventoryDevice))).scalars().one()
    assert row.ieee_address == "0x00124b0022a1b2c3"


# --- Links -------------------------------------------------------------------

async def test_replace_links_only_wipes_unifi_links(db_session: AsyncSession):
    db_session.add(
        InventoryDeviceLink(source_ieee="pve-node-a", target_ieee="pve-a-101", discovery_source="proxmox")
    )
    await db_session.commit()

    await _replace_links(db_session, [{"source": f"unifi-{CORE}", "target": f"unifi-{ULTRA}"}])
    await db_session.commit()

    links = (await db_session.execute(select(InventoryDeviceLink))).scalars().all()
    assert {link.discovery_source for link in links} == {"proxmox", "unifi"}


async def test_replace_links_dedupes_and_recounts(db_session: AsyncSession):
    edges = [
        {"source": f"unifi-{CORE}", "target": f"unifi-{ULTRA}"},
        {"source": f"unifi-{CORE}", "target": f"unifi-{ULTRA}"},
    ]
    assert await _replace_links(db_session, edges) == 1


async def test_uplink_resolves_to_an_ethernet_edge(db_session: AsyncSession):
    """The scan.py dispatch must render a unifi link as 'ethernet', not 'iot'."""
    from app.api.routes.scan import _resolve_pending_links_for_ieee

    design = Design(name="d")
    db_session.add(design)
    await db_session.flush()

    parent = InventoryDevice(ieee_address=f"unifi-{CORE}", status="approved")
    child = InventoryDevice(ieee_address=f"unifi-{ULTRA}", status="approved")
    db_session.add_all([parent, child])
    await db_session.flush()
    db_session.add_all([
        Node(label="core", type="switch", pos_x=0, pos_y=0, design_id=design.id, device_id=parent.id),
        Node(label="ultra", type="switch", pos_x=0, pos_y=0, design_id=design.id, device_id=child.id),
        InventoryDeviceLink(
            source_ieee=f"unifi-{CORE}", target_ieee=f"unifi-{ULTRA}", discovery_source="unifi"
        ),
    ])
    await db_session.commit()

    created = await _resolve_pending_links_for_ieee(db_session, f"unifi-{ULTRA}", design.id)
    assert len(created) == 1
    assert created[0]["type"] == "ethernet"
    assert created[0]["source_handle"] == "bottom"
    # A '-t' suffixed target handle does not resolve in React Flow.
    assert created[0]["target_handle"] == "top"


# --- Auto-sync config ---------------------------------------------------------

async def test_config_reports_sync_state(client: AsyncClient, headers, _clear_env_config):
    _clear_env_config.unifi_sync_enabled = True
    _clear_env_config.unifi_sync_interval = 900
    try:
        res = await client.get("/api/v1/unifi/config", headers=headers)
        assert res.json()["sync_enabled"] is True
        assert res.json()["sync_interval"] == 900
    finally:
        _clear_env_config.unifi_sync_enabled = False
        _clear_env_config.unifi_sync_interval = 3600


async def test_enable_sync_rejected_without_host_or_key(client: AsyncClient, headers):
    res = await client.post(
        "/api/v1/unifi/config", json={"sync_enabled": True, "sync_interval": 3600}, headers=headers
    )
    assert res.status_code == 400
    assert "auto-sync" in res.json()["detail"]


async def test_disabling_sync_needs_no_credentials(client: AsyncClient, headers, _clear_env_config):
    """Turning it off must always be possible, even unconfigured."""
    with patch("app.core.config.Settings.save_overrides"), \
            patch("app.api.routes.unifi.set_unifi_sync_enabled"):
        res = await client.post(
            "/api/v1/unifi/config", json={"sync_enabled": False, "sync_interval": 3600}, headers=headers
        )
    assert res.status_code == 200
    assert res.json()["sync_enabled"] is False


async def test_save_config_applies_to_the_scheduler(client: AsyncClient, headers, _clear_env_config):
    _clear_env_config.unifi_host = "10.1.1.10"
    _clear_env_config.unifi_api_key = "k"
    with patch("app.core.config.Settings.save_overrides"), \
            patch("app.api.routes.unifi.set_unifi_sync_enabled") as mock_set, \
            patch("app.api.routes.unifi.reschedule_unifi_sync") as mock_resched:
        res = await client.post(
            "/api/v1/unifi/config", json={"sync_enabled": True, "sync_interval": 1800}, headers=headers
        )
    assert res.status_code == 200
    mock_set.assert_called_once_with(True)
    mock_resched.assert_called_once_with(1800)
    _clear_env_config.unifi_sync_enabled = False


async def test_sync_interval_floor_is_enforced(client: AsyncClient, headers, _clear_env_config):
    _clear_env_config.unifi_host = "10.1.1.10"
    _clear_env_config.unifi_api_key = "k"
    res = await client.post(
        "/api/v1/unifi/config", json={"sync_enabled": True, "sync_interval": 60}, headers=headers
    )
    assert res.status_code == 422


async def test_sync_now_rejected_without_server_config(client: AsyncClient, headers):
    res = await client.post("/api/v1/unifi/sync-now", headers=headers)
    assert res.status_code == 400


async def test_sync_now_creates_a_scan_run(client: AsyncClient, headers, _clear_env_config):
    _clear_env_config.unifi_host = "10.1.1.10"
    _clear_env_config.unifi_api_key = "k"
    with patch("app.api.routes.unifi._background_unifi_import", new_callable=AsyncMock):
        res = await client.post("/api/v1/unifi/sync-now", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "unifi"
    assert body["ranges"] == ["10.1.1.10:443"]


async def test_sync_now_requires_auth(client: AsyncClient):
    res = await client.post("/api/v1/unifi/sync-now")
    assert res.status_code == 401
