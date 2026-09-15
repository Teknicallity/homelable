# UniFi Network Import

This feature connects Homelable to your UniFi Network controller, reads the
managed devices over the official Integration API, and drops your switches,
access points and gateways onto the canvas as typed nodes — with names, models
and MAC addresses — joined by the uplinks the controller already knows about.
It **merges** with devices already discovered by a network scan rather than
duplicating them.

> 🔒 **Server-dependent feature** — requires the Homelable backend. It is hidden
> in the no-backend standalone/demo build.

---

## Feature Overview

- **API-based discovery** — Reads `/proxy/network/integration/v1` from the controller using an API key.
- **Typed nodes** — Devices map to existing Homelable node types:
  - `switch` — a UniFi switch
  - `ap` — an access point
  - `router` — a gateway (UDM, UXG)
  - `generic` — anything whose role the controller does not report
- **Topology** — Each device is linked to the one it uplinks to with an `ethernet` edge.
- **MAC addresses** — The controller reports a MAC for every device. This matters more than it sounds: on the default Docker bridge network the scanner can see none at all (ARP is layer 2, and every LAN host sits one hop away behind the Docker gateway), so a UniFi import fills in the identity the scanner structurally cannot reach.
- **Merge** — Re-importing updates existing devices in place and never deletes anything. A device whose IP or MAC matches a previously scanned row merges onto it.
- **Infrastructure only** — Wired and wireless clients are deliberately not imported; the network scanner already finds those by IP.
- **Auto-sync** — Optional scheduled re-import into the pending inventory, alongside the Zigbee, Z-Wave and Proxmox schedules in Settings.

---

## Prerequisites

### Supported controllers

This import authenticates with an **API key**, and API keys only exist on UniFi OS:

| Controller | Works? |
|---|---|
| UniFi OS console (UDM, UDR, UCG, Cloud Key Gen2+) | Yes |
| **UniFi OS Server** (software-only, self-hosted) | Yes |
| **Legacy UniFi Network Server** (the old self-hosted "UniFi Controller" package) | **No** |

> **The legacy self-hosted Network Server cannot be used with this feature.** It has
> no way to issue an API key, so there is no credential for the import to send.
> Art of WiFi's survey of UniFi authentication states it plainly: *"A UniFi OS
> console or UniFi OS Server. The legacy self-hosted Network Application does not
> support API key authentication."* Its only option is a local admin
> username and password, which this import does not implement.

Ubiquiti has itself moved on from that product. Its own documentation now calls it
the **legacy** Network Server and describes UniFi OS Server as **replacing** it:

> "The UniFi OS Server is the new standard for self-hosting UniFi, **replacing the
> legacy UniFi Network Server**. While the Network Server provided basic hosting
> functionality, it lacked support for key UniFi OS features like Organizations,
> IdP Integration, or Site Magic SD-WAN."
> — [Self-Hosting UniFi](https://help.ui.com/hc/en-us/articles/34210126298775-Self-Hosting-UniFi), Ubiquiti Help Center

The older article is now headed *"Looking for the next generation of UniFi
self-hosting?"* and advises that self-hosting a Network Server *"should only be done
by experienced network administrators"*
([Self-Hosting a UniFi Network Server](https://help.ui.com/hc/en-us/articles/360012282453-Self-Hosting-a-UniFi-Network-Server)).
That page also documents its migration path to UniFi OS Server.

Note Ubiquiti has not published a formal end-of-life date; "legacy" and "replacing"
are their words, not an announced EOL. The practical position is that new UniFi OS
features are not coming to it, and — decisive here — it cannot issue an API key.

**If you are on the legacy Network Server**, migrate to UniFi OS Server
([instructions](https://help.ui.com/hc/en-us/articles/34210126298775-Self-Hosting-UniFi)),
then create a key. Back the Network Server up and shut it down before installing
UniFi OS Server, as that guide notes.

### Create an API key

1. Open the **Network** application on your controller.
2. Go to **Settings → Control Plane → Integrations** (older firmware: the profile icon at bottom-left → **API**).
3. **Create API Key**, give it a name, and copy the value — it is shown once.

The key is read-only for everything this feature does.

### Find your port

This is the single most common setup mistake, and it fails as a bare
"connection refused" with nothing pointing at the cause:

| Controller | Port |
|---|---|
| UniFi Dream Machine / Dream Router / Cloud Key | `443` |
| UniFi OS Server (software-only, self-hosted) | `11443` |

The import dialog says this under the Port field too.

### Where the key is stored

The API key is a real credential and is treated as one:

- For a **one-off import**, type the key into the import dialog. It is sent with
  that request only and is **never stored**.
- To avoid retyping it, configure it on the **server** via environment
  variables (below). It is read from `.env` and kept in memory — never written
  to disk by the app, and never returned by any endpoint.

```env
# backend/.env
UNIFI_API_KEY=your-api-key
UNIFI_HOST=192.168.1.10
UNIFI_PORT=11443                   # 443 for UDM/Cloud Key
UNIFI_SITE_ID=                     # optional; blank uses the first site
UNIFI_VERIFY_TLS=false             # controllers ship a self-signed cert

# Auto-sync (optional). Also togglable from Settings once host + key are set.
UNIFI_SYNC_ENABLED=false
UNIFI_SYNC_INTERVAL=3600           # seconds, minimum 300
```

Anything set here prefills the dialog, and any field left blank in the dialog
falls back to it. Only the host and the key are required — from either source.

---

## Step-by-step Usage

### 1. Open the UniFi Import dialog

Sidebar → **UniFi Import**.

### 2. Configure the connection

Host, port, and API key. Site ID is optional — leave it blank and the first
site the key can see is used. Leave **Verify TLS certificate** off unless you
have replaced the controller's self-signed certificate.

### 3. Test the connection (optional)

**Test Connection** reports the Network application version and the site it
resolved, so you know the key and port are right before importing.

### 4. Choose an import target

- **Device inventory only** — devices land in the pending inventory for review.
- **Inventory + canvas** — the same, and the dialog then lets you pick which devices to place.

### 5. Select and add to canvas

In canvas mode, **Fetch Devices** lists what was found, grouped by role. Tick
the ones you want and **Add N to Canvas**. Uplinks between the devices you
selected are drawn as `ethernet` edges.

---

## Auto-sync configuration

A scheduled re-import keeps the inventory current without opening the dialog. It
sits with the other importers' schedules in **Settings → UniFi auto-sync**,
beside **Zigbee auto-sync**, **Z-Wave auto-sync** and **Proxmox auto-sync** —
all four behave the same way.

### Turning it on

The panel only activates once the server has both a host and a key, because a
scheduled job runs with no user present and has nowhere else to get them:

```env
# backend/.env
UNIFI_HOST=192.168.1.10
UNIFI_API_KEY=your-api-key
```

Restart the backend, then open **Settings**, tick **Auto-sync UniFi inventory**,
set an interval and **Save**. Until those two are set the panel shows what to
configure instead of the controls.

### What it does

- Runs `fetch → upsert` on the interval, exactly what the manual **Import to
  Inventory** does. Devices merge in place; nothing is deleted and hidden rows
  stay hidden.
- Records a `ScanRun` of kind `unifi`, so every scheduled run shows in **Scan
  History** next to IP scans and the other imports.
- Minimum interval is **300s (5 min)**, enforced on write. The default is 3600s.
- Overlapping runs are impossible: the job is registered with `max_instances=1`
  and `coalesce=True`, so a slow sync cannot stack up behind itself and missed
  firings collapse into one.

### Re-sync now

**Re-sync now** in the same panel runs one import immediately using the server
`.env` config — useful for checking the credentials work before trusting a
schedule to them.

### What is and is not persisted

Only the activation (`sync_enabled`, `sync_interval`) is written to
`scan_config.json` so it survives a restart. The connection config — host, port,
site, key, TLS verification — stays **env-only** and is never written to disk.
That split is deliberate: persisting a host alongside its environment variable
creates two sources of truth, which is a bug the Proxmox importer had to undo.

---

## Node Type Mapping

| UniFi | Homelable type | Notes |
|---|---|---|
| `features: switching` | `switch` | |
| `features: accessPoint` | `ap` | |
| `features: gateway` | `router` | Wins when a device reports several roles |
| anything else | `generic` | |
| `state` ONLINE / other | node status online / offline | |
| `model` | Model property | hidden by default |
| `firmwareVersion` | Firmware property | hidden by default |
| port count | Ports property | hidden by default |
| device id | synthetic identity (`unifi-{deviceId}`) | stable across re-imports |

Device hierarchy is rendered as `ethernet` edges (parent → child).

---

## Notes and Limits

- **Topology costs one request per device.** The device list does not include
  the uplink, so each device's parent is read from its detail endpoint. Those
  run concurrently, and a device whose detail request fails still imports —
  just without a parent that round.
- **Switch ports are not imported.** The official API reports *that* two devices
  are connected, not which port the cable is in. The controller's internal API
  does expose port numbers; wiring that into the Rack Canvas as real patch
  cables is the obvious next step, but it is undocumented and can change between
  firmware versions, so it is deliberately not used here.
- **One site per import.** Blank uses the first site the key can see; set
  `UNIFI_SITE_ID` or the dialog field to choose another.

---

## Troubleshooting

**"Connection refused by UniFi host — check the port"**
Almost always the port. `443` for a UDM or Cloud Key, `11443` for UniFi OS Server.

**"Authentication failed — check the API key and its permissions"**
The key is wrong, or was revoked. Keys are shown once at creation; make a new one.

**"TLS verification failed"**
Controllers ship a self-signed certificate. Turn **Verify TLS certificate** off.

**"No UniFi sites are visible to this API key"**
The key exists but can't see any site. Recreate it from the Network application.

**Devices import as `generic`**
The controller did not report a role for them. They still import, and you can
set the type yourself when approving them onto a canvas.
