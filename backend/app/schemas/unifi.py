"""Pydantic v2 schemas for UniFi Network import.

Every connection field is optional on requests — when omitted the backend falls
back to the server-configured value (env / .env), the same way the Proxmox
token does. No response schema ever carries the API key; secrets are kept out of
responses by structural omission.
"""

from pydantic import BaseModel, Field


class UnifiConnectionRequest(BaseModel):
    host: str | None = Field(None, description="UniFi controller host or IP (falls back to server env)")
    port: int | None = Field(
        None, ge=1, le=65535, description="Controller API port (falls back to server env, then 443)"
    )
    api_key: str | None = Field(None, description="UniFi API key (falls back to server env)")
    site_id: str | None = Field(None, description="Site id (falls back to server env, then the first site)")
    verify_tls: bool | None = Field(None, description="Verify the controller TLS certificate")


class UnifiTestConnectionResponse(BaseModel):
    connected: bool
    message: str


class UnifiNodeOut(BaseModel):
    """A homelable-ready node representation of a UniFi device."""

    id: str
    label: str
    type: str  # switch | ap | router | generic
    ieee_address: str
    hostname: str | None = None
    ip: str | None = None
    # Unlike the Proxmox equivalent this does carry the MAC: it is the whole
    # point of a UniFi import on a bridge network, where the scanner sees none.
    mac: str | None = None
    status: str
    vendor: str | None = None
    model: str | None = None
    parent_ieee: str | None = None
    # The Device Inventory row this node draws, stamped by the import. The
    # canvas carries it back on save so the node links to that row rather
    # than minting a second one for the same device.
    device_id: str | None = None


class UnifiEdgeOut(BaseModel):
    source: str
    target: str


class UnifiImportResponse(BaseModel):
    nodes: list[UnifiNodeOut]
    edges: list[UnifiEdgeOut]
    device_count: int


class UnifiImportPendingResponse(BaseModel):
    """Result of importing a UniFi inventory into the pending section."""

    pending_created: int
    pending_updated: int
    links_recorded: int
    device_count: int


class UnifiConfig(BaseModel):
    """Non-secret UniFi connection config (GET response).

    Env-only and read-only here — surfaced so the import dialog can prefill
    rather than making the user retype the host and port every time.
    ``api_key_configured`` reflects whether a server-side key is present.
    Never carries the key itself."""

    host: str = ""
    port: int = Field(443, ge=1, le=65535)
    site_id: str = ""
    verify_tls: bool = False
    api_key_configured: bool = False
