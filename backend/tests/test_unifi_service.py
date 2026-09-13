"""Unit tests for the UniFi import service (parsing, props, paging, sanitizer)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import unifi_service as svc

SITE = "88f7af54-98f8-306a-a1c7-c9349722b1f6"
DEVICES = f"{svc._API_BASE}/sites/{SITE}/devices"

# Shaped after the live controller: a root switch, a downstream switch, two APs.
CORE = "93ea5dc5-aded-3df1-b357-ac8ed1d4eb8e"
ULTRA = "b5885f9f-e9aa-3e01-ab79-a9c172b5ed61"
KITCHEN = "ae88c43a-4b92-3033-982a-080af7b15589"


def _list_item(device_id: str, name: str, features: list[str], **over) -> dict:
    item = {
        "id": device_id,
        "name": name,
        "model": "USW Pro 24 PoE",
        "macAddress": "58:D6:1F:7E:81:A4",
        "ipAddress": "10.1.1.3",
        "state": "ONLINE",
        "features": features,
        "interfaces": ["ports"],
    }
    item.update(over)
    return item


def test_feature_type_maps_onto_existing_node_types() -> None:
    assert svc._feature_type(["switching"]) == "switch"
    assert svc._feature_type(["accessPoint"]) == "ap"
    assert svc._feature_type(["gateway"]) == "router"
    # A UDM reports several at once; the gateway role wins.
    assert svc._feature_type(["switching", "gateway"]) == "router"
    assert svc._feature_type([]) == "generic"
    assert svc._feature_type(None) == "generic"
    assert svc._feature_type("switching") == "generic"


def test_feature_type_reads_the_detail_endpoints_dict_form() -> None:
    """The detail endpoint sends {"accessPoint": {}} where the list sends
    ["accessPoint"], and the detail overlays the list — so a list-only check
    silently types every device as generic."""
    assert svc._feature_type({"accessPoint": {}}) == "ap"
    assert svc._feature_type({"switching": {}}) == "switch"
    assert svc._feature_type({"gateway": {}, "switching": {}}) == "router"
    assert svc._feature_type({}) == "generic"


def test_clean_name_collapses_controller_whitespace() -> None:
    # The live controller really does return "Kitchen " with a trailing space.
    assert svc._clean_name("Kitchen ") == "Kitchen"
    assert svc._clean_name("  Patch  Panel ") == "Patch Panel"
    assert svc._clean_name("") is None
    assert svc._clean_name(None) is None


def test_ports_handles_the_polymorphic_interfaces_field() -> None:
    # Detail shape: a dict of interface kinds.
    assert svc._ports({"interfaces": {"ports": [{"idx": 1}, {"idx": 2}]}}) == 2
    # List shape: a list of capability names, which carries no port data.
    assert svc._ports({"interfaces": ["ports"]}) is None
    assert svc._ports({}) is None


def test_device_node_normalizes_name_and_mac() -> None:
    node = svc._device_node(_list_item(KITCHEN, "Kitchen ", ["accessPoint"]), CORE)
    assert node["label"] == "Kitchen"
    assert node["hostname"] == "Kitchen"
    assert node["mac"] == "58:d6:1f:7e:81:a4"
    assert node["ieee_address"] == f"unifi-{KITCHEN}"
    assert node["id"] == node["ieee_address"]
    assert node["type"] == "ap"
    assert node["vendor"] == "Ubiquiti"
    assert node["parent_ieee"] == f"unifi-{CORE}"


def test_device_node_falls_back_to_model_then_ieee() -> None:
    assert svc._device_node(_list_item(CORE, "", ["switching"]), None)["label"] == "USW Pro 24 PoE"
    bare = svc._device_node({"id": CORE}, None)
    assert bare["label"] == f"unifi-{CORE}"
    assert svc._device_node({}, None) is None


def test_device_node_status_only_online_counts() -> None:
    assert svc._device_node(_list_item(CORE, "sw", ["switching"]), None)["status"] == "online"
    offline = _list_item(CORE, "sw", ["switching"], state="OFFLINE")
    assert svc._device_node(offline, None)["status"] == "offline"


def test_build_unifi_properties_are_hidden_by_default() -> None:
    node = {"model": "USW Ultra", "firmware": "7.1.26", "port_count": 8}
    props = svc.build_unifi_properties(node)
    assert [p["key"] for p in props] == ["Model", "Firmware", "Ports", "Source"]
    assert all(p["visible"] is False for p in props)
    assert all(isinstance(p["value"], str) for p in props)
    assert props[-1]["value"] == "UniFi Network"


def test_parse_inventory_builds_uplink_edges() -> None:
    devices = [
        _list_item(CORE, "USW Pro 24 PoE", ["switching"]),
        _list_item(ULTRA, "USW Ultra", ["switching"]),
        _list_item(KITCHEN, "Kitchen ", ["accessPoint"]),
    ]
    details = {
        CORE: {"interfaces": {"ports": [{"idx": 1}]}},  # root: no uplink key
        ULTRA: {"uplink": {"deviceId": CORE}},
        KITCHEN: {"uplink": {"deviceId": CORE}},
    }
    nodes, edges = svc._parse_inventory(devices, details)
    assert len(nodes) == 3
    assert sorted(edges, key=lambda e: e["target"]) == sorted(
        [
            {"source": f"unifi-{CORE}", "target": f"unifi-{ULTRA}"},
            {"source": f"unifi-{CORE}", "target": f"unifi-{KITCHEN}"},
        ],
        key=lambda e: e["target"],
    )
    root = next(n for n in nodes if n["id"] == f"unifi-{CORE}")
    assert root["parent_ieee"] is None
    assert root["port_count"] == 1


def test_parse_inventory_drops_edge_to_unknown_parent() -> None:
    """An uplink can name an unadopted device; that link would never resolve."""
    devices = [_list_item(ULTRA, "USW Ultra", ["switching"])]
    nodes, edges = svc._parse_inventory(devices, {ULTRA: {"uplink": {"deviceId": "not-imported"}}})
    assert len(nodes) == 1
    assert edges == []


def test_parse_inventory_ignores_a_self_uplink() -> None:
    devices = [_list_item(CORE, "USW Pro 24 PoE", ["switching"])]
    nodes, edges = svc._parse_inventory(devices, {CORE: {"uplink": {"deviceId": CORE}}})
    assert edges == []
    assert nodes[0]["parent_ieee"] is None


def test_parse_inventory_keeps_a_device_whose_detail_failed() -> None:
    """A failed detail is not the same as 'this is the root' — but it still imports."""
    devices = [
        _list_item(CORE, "USW Pro 24 PoE", ["switching"]),
        _list_item(ULTRA, "USW Ultra", ["switching"]),
    ]
    nodes, edges = svc._parse_inventory(devices, {CORE: {}})  # ULTRA detail missing
    assert len(nodes) == 2
    assert edges == []


@pytest.mark.asyncio
async def test_get_paged_follows_offset() -> None:
    first = [{"id": f"d{i}"} for i in range(svc._PAGE_LIMIT)]
    second = [{"id": "tail"}]

    async def fake_get_json(client, path: str):
        offset = int(path.split("offset=")[1].split("&")[0])
        page = first if offset == 0 else second
        return {"offset": offset, "limit": svc._PAGE_LIMIT, "count": len(page),
                "totalCount": len(first) + len(second), "data": page}

    with patch.object(svc, "_get_json", new=AsyncMock(side_effect=fake_get_json)) as mock:
        out = await svc._get_paged(None, DEVICES)
    assert len(out) == svc._PAGE_LIMIT + 1
    assert mock.await_count == 2


@pytest.mark.asyncio
async def test_get_paged_stops_on_a_lying_total_count() -> None:
    async def fake_get_json(client, path: str):
        offset = int(path.split("offset=")[1].split("&")[0])
        page = [{"id": "only"}] if offset == 0 else []
        return {"totalCount": 9999, "data": page}

    with patch.object(svc, "_get_json", new=AsyncMock(side_effect=fake_get_json)):
        assert await svc._get_paged(None, DEVICES) == [{"id": "only"}]


@pytest.mark.asyncio
async def test_get_paged_rejects_a_malformed_envelope() -> None:
    with patch.object(svc, "_get_json", new=AsyncMock(return_value=[1, 2, 3])), \
            pytest.raises(ValueError, match="Malformed UniFi response"):
        await svc._get_paged(None, DEVICES)


@pytest.mark.asyncio
async def test_resolve_site_id_prefers_the_caller_then_the_first_site() -> None:
    async def fake_get_json(client, path: str):
        return {"totalCount": 1, "data": [{"id": SITE, "name": "Default"}]}

    with patch.object(svc, "_get_json", new=AsyncMock(side_effect=fake_get_json)) as mock:
        assert await svc._resolve_site_id(None, "explicit") == "explicit"
        assert mock.await_count == 0  # an explicit site skips the lookup entirely
        assert await svc._resolve_site_id(None, None) == SITE


@pytest.mark.asyncio
async def test_resolve_site_id_raises_when_the_key_sees_no_site() -> None:
    with patch.object(svc, "_get_json", new=AsyncMock(return_value={"totalCount": 0, "data": []})), \
            pytest.raises(ValueError, match="No UniFi sites"):
        await svc._resolve_site_id(None, None)


def _inventory_fake(detail_error_for: str | None = None):
    async def fake_get_json(client, path: str):
        base = path.split("?")[0]
        if base == f"{svc._API_BASE}/sites":
            return {"totalCount": 1, "data": [{"id": SITE}]}
        if base == DEVICES:
            return {
                "totalCount": 2,
                "data": [
                    _list_item(CORE, "USW Pro 24 PoE", ["switching"]),
                    _list_item(ULTRA, "USW Ultra", ["switching"]),
                ],
            }
        if base == f"{DEVICES}/{CORE}":
            return {"interfaces": {"ports": [{"idx": 1}]}}
        if base == f"{DEVICES}/{ULTRA}":
            if detail_error_for == ULTRA:
                raise httpx.ConnectError("boom")
            return {"uplink": {"deviceId": CORE}}
        return None

    return fake_get_json


@pytest.mark.asyncio
async def test_fetch_inventory_uses_detail_for_the_uplink() -> None:
    with patch.object(svc, "_get_json", new=AsyncMock(side_effect=_inventory_fake())):
        nodes, edges = await svc.fetch_unifi_inventory("h", 11443, "key")
    assert len(nodes) == 2
    assert edges == [{"source": f"unifi-{CORE}", "target": f"unifi-{ULTRA}"}]


@pytest.mark.asyncio
async def test_fetch_inventory_survives_a_failed_detail() -> None:
    """One unreachable device must not cost us the whole import."""
    with patch.object(svc, "_get_json", new=AsyncMock(side_effect=_inventory_fake(detail_error_for=ULTRA))):
        nodes, edges = await svc.fetch_unifi_inventory("h", 11443, "key")
    assert len(nodes) == 2
    assert edges == []


@pytest.mark.asyncio
async def test_fetch_inventory_raises_sanitized_connection_error() -> None:
    with patch.object(svc, "_get_json", new=AsyncMock(side_effect=httpx.ConnectError("refused"))), \
            pytest.raises(ConnectionError) as exc:
        await svc.fetch_unifi_inventory("h", 11443, "sup3r-secret-key")
    assert "sup3r-secret-key" not in str(exc.value)


@pytest.mark.asyncio
async def test_test_connection_reports_version_and_never_raises() -> None:
    async def fake_get_json(client, path: str):
        if path.split("?")[0] == f"{svc._API_BASE}/info":
            return {"applicationVersion": "10.6.101"}
        return {"totalCount": 1, "data": [{"id": SITE}]}

    with patch.object(svc, "_get_json", new=AsyncMock(side_effect=fake_get_json)):
        ok, message = await svc.test_unifi_connection("h", 11443, "key")
    assert ok is True
    assert "10.6.101" in message

    with patch.object(svc, "_get_json", new=AsyncMock(side_effect=RuntimeError("kaboom"))):
        ok, message = await svc.test_unifi_connection("h", 11443, "key")
    assert ok is False
    assert message


def test_sanitize_error_never_leaks_the_api_key() -> None:
    key = "y9aTNFUbNqP5wo1zy6cVGMldOWwokkCu"
    request = httpx.Request("GET", f"https://h:11443{svc._API_BASE}/sites", headers={"X-API-KEY": key})
    for code, expected in ((401, "Authentication failed"), (404, "not found"), (500, "HTTP 500")):
        exc = httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))
        message = svc._sanitize_unifi_error(exc)
        assert expected in message
        assert key not in message

    assert "port" in svc._sanitize_unifi_error(httpx.ConnectError("Connection refused"))
    assert "resolved" in svc._sanitize_unifi_error(httpx.ConnectError("Name or service not known"))
    assert "TLS" in svc._sanitize_unifi_error(httpx.ConnectError("certificate verify failed"))
    assert "timed out" in svc._sanitize_unifi_error(httpx.ReadTimeout("timed out"))
