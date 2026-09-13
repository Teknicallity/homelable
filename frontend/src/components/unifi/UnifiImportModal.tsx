import { useEffect, useState } from 'react'
import { Wifi, Network, Router, HelpCircle, CheckCircle2, XCircle, Loader2, Plus } from 'lucide-react'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { unifiApi, type UnifiConnection } from '@/api/client'
import { toast } from 'sonner'
import type { UnifiNode, UnifiEdge, UnifiNodeType } from './types'

interface UnifiImportModalProps {
  open: boolean
  onClose: () => void
  onAddToCanvas: (nodes: UnifiNode[], edges: UnifiEdge[]) => void
  onInventoryImported?: () => void
}

type ImportMode = 'pending' | 'canvas'

const ACCENT = '#0559c9'

interface ConnectionForm {
  host: string
  port: string
  api_key: string
  site_id: string
  verify_tls: boolean
}

const DEFAULT_FORM: ConnectionForm = {
  host: '',
  port: '443',
  api_key: '',
  site_id: '',
  verify_tls: false,
}

const DEVICE_TYPE_ICON: Record<UnifiNodeType, typeof Wifi> = {
  switch: Network,
  ap: Wifi,
  router: Router,
  generic: HelpCircle,
}

const DEVICE_TYPE_LABEL: Record<UnifiNodeType, string> = {
  switch: 'Switches',
  ap: 'Access Points',
  router: 'Gateways',
  generic: 'Other Devices',
}

const DEVICE_TYPE_COLOR: Record<UnifiNodeType, string> = {
  switch: '#0559c9',
  ap: '#00d4ff',
  router: '#39d353',
  generic: '#8b949e',
}

export function UnifiImportModal({ open, onClose, onAddToCanvas, onInventoryImported }: UnifiImportModalProps) {
  const [form, setForm] = useState<ConnectionForm>(DEFAULT_FORM)
  const [connectionStatus, setConnectionStatus] = useState<'idle' | 'testing' | 'ok' | 'fail'>('idle')
  const [connectionMsg, setConnectionMsg] = useState('')
  const [keyConfigured, setKeyConfigured] = useState(false)
  const [loading, setLoading] = useState(false)
  const [devices, setDevices] = useState<UnifiNode[]>([])
  const [edges, setEdges] = useState<UnifiEdge[]>([])
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [importMode, setImportMode] = useState<ImportMode>('pending')

  // Prefill from the server env. The port especially is worth not retyping:
  // 443 suits a UDM or Cloud Key, but UniFi OS Server answers on 11443.
  useEffect(() => {
    if (!open) return
    unifiApi.getConfig()
      .then((res) => {
        setKeyConfigured(res.data.api_key_configured)
        setForm((f) => ({
          ...f,
          host: f.host || res.data.host,
          port: res.data.host ? String(res.data.port) : f.port,
          site_id: f.site_id || res.data.site_id,
          verify_tls: res.data.verify_tls,
        }))
      })
      .catch(() => { /* prefill is a convenience; the form still works */ })
  }, [open])

  const updateField = (field: keyof ConnectionForm, value: string) =>
    setForm((f) => ({ ...f, [field]: value }))

  const buildPayload = (): UnifiConnection => ({
    host: form.host.trim() || undefined,
    port: Number(form.port) || undefined,
    api_key: form.api_key.trim() || undefined,
    site_id: form.site_id.trim() || undefined,
    verify_tls: form.verify_tls,
  })

  const extractError = (err: unknown): string | undefined => {
    if (err && typeof err === 'object' && 'response' in err) {
      return (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
    }
    return undefined
  }

  const handleTestConnection = async () => {
    setConnectionStatus('testing')
    try {
      const res = await unifiApi.testConnection(buildPayload())
      setConnectionStatus(res.data.connected ? 'ok' : 'fail')
      setConnectionMsg(res.data.message)
    } catch (err) {
      setConnectionStatus('fail')
      setConnectionMsg(extractError(err) ?? 'Request failed — check host address')
    }
  }

  const handleFetchDevices = async () => {
    setLoading(true)
    try {
      if (importMode === 'pending') {
        await unifiApi.importToPending(buildPayload())
        toast.success('UniFi import started — track progress in Scan History')
        onInventoryImported?.()
        handleClose()
      } else {
        const res = await unifiApi.importNetwork(buildPayload())
        setDevices(res.data.nodes)
        setEdges(res.data.edges)
        setChecked(new Set(res.data.nodes.map((n) => n.id)))
        if (res.data.device_count === 0) {
          toast.info('No UniFi devices found')
        } else {
          toast.success(`Found ${res.data.device_count} device${res.data.device_count !== 1 ? 's' : ''}`)
        }
      }
    } catch (err: unknown) {
      toast.error(extractError(err) ?? 'Failed to fetch UniFi inventory')
    } finally {
      setLoading(false)
    }
  }

  const toggleCheck = (id: string) =>
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })

  const toggleAll = () => {
    setChecked(checked.size === devices.length ? new Set() : new Set(devices.map((d) => d.id)))
  }

  const handleAddToCanvas = () => {
    const selectedDevices = devices.filter((d) => checked.has(d.id))
    const selectedIds = new Set(selectedDevices.map((d) => d.id))
    const selectedEdges = edges.filter((e) => selectedIds.has(e.source) && selectedIds.has(e.target))
    onAddToCanvas(selectedDevices, selectedEdges)
    toast.success(`Added ${selectedDevices.length} device${selectedDevices.length !== 1 ? 's' : ''} to canvas`)
    onClose()
  }

  const handleClose = () => {
    setDevices([])
    setEdges([])
    setChecked(new Set())
    setConnectionStatus('idle')
    setConnectionMsg('')
    setImportMode('pending')
    onClose()
  }

  const groupedDevices: Record<UnifiNodeType, UnifiNode[]> = {
    router: devices.filter((d) => d.type === 'router'),
    switch: devices.filter((d) => d.type === 'switch'),
    ap: devices.filter((d) => d.type === 'ap'),
    generic: devices.filter((d) => d.type === 'generic'),
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && handleClose()}>
      <DialogContent className="bg-[#161b22] border-border max-w-2xl max-h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="text-foreground flex items-center gap-2">
            <Wifi size={16} style={{ color: ACCENT }} />
            UniFi Network Import
          </DialogTitle>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto space-y-4 py-2 min-h-0">
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-x-4 gap-y-3">
              <div className="col-span-2 space-y-1">
                <Label className="text-xs text-muted-foreground">Controller Host</Label>
                <Input
                  value={form.host}
                  onChange={(e) => updateField('host', e.target.value)}
                  placeholder="192.168.1.x or unifi.local"
                  className="font-mono text-sm bg-[#0d1117] border-border"
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">Port</Label>
                <Input
                  value={form.port}
                  onChange={(e) => updateField('port', e.target.value)}
                  placeholder="443"
                  type="number"
                  className="font-mono text-sm bg-[#0d1117] border-border"
                />
                <span className="block text-[10px] text-muted-foreground">
                  443 for UDM / Cloud Key, 11443 for UniFi OS Server
                </span>
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">Site ID <span className="text-[10px]">(optional)</span></Label>
                <Input
                  value={form.site_id}
                  onChange={(e) => updateField('site_id', e.target.value)}
                  placeholder="first site"
                  className="font-mono text-sm bg-[#0d1117] border-border"
                />
              </div>
              <div className="col-span-2 space-y-1">
                <Label className="text-xs text-muted-foreground">API Key</Label>
                <Input
                  value={form.api_key}
                  onChange={(e) => updateField('api_key', e.target.value)}
                  placeholder={keyConfigured ? 'configured on the server — leave blank to use it' : '••••••••••••••••••••••••••••••••'}
                  type="password"
                  autoComplete="new-password"
                  className="text-sm bg-[#0d1117] border-border"
                />
              </div>
              <div className="col-span-2 flex flex-wrap items-center gap-x-6 gap-y-2 pt-1">
                <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.verify_tls}
                    onChange={(e) => setForm((f) => ({ ...f, verify_tls: e.target.checked }))}
                    className="w-3 h-3 cursor-pointer"
                    style={{ accentColor: ACCENT }}
                  />
                  Verify TLS certificate
                </label>
              </div>
            </div>

            {connectionStatus !== 'idle' && (
              <div className={`flex items-center gap-1.5 text-xs px-2 py-1.5 rounded-md border ${
                connectionStatus === 'ok'
                  ? 'bg-[#39d353]/10 border-[#39d353]/30 text-[#39d353]'
                  : connectionStatus === 'fail'
                  ? 'bg-[#f85149]/10 border-[#f85149]/30 text-[#f85149]'
                  : 'bg-[#e3b341]/10 border-[#e3b341]/30 text-[#e3b341]'
              }`}>
                {connectionStatus === 'testing' && <Loader2 size={12} className="animate-spin" />}
                {connectionStatus === 'ok' && <CheckCircle2 size={12} />}
                {connectionStatus === 'fail' && <XCircle size={12} />}
                <span>{connectionStatus === 'testing' ? 'Testing…' : connectionMsg}</span>
              </div>
            )}

            <div className="space-y-2 rounded-md border border-border bg-[#0d1117]/60 px-3 py-2.5">
              <span className="block text-xs text-muted-foreground">Send devices to</span>
              <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-xs">
                <label className="flex items-center gap-1.5 cursor-pointer text-foreground">
                  <input
                    type="radio"
                    name="unifi-import-mode"
                    checked={importMode === 'pending'}
                    onChange={() => setImportMode('pending')}
                    className="cursor-pointer"
                    style={{ accentColor: ACCENT }}
                  />
                  Device inventory only
                </label>
                <label className="flex items-center gap-1.5 cursor-pointer text-foreground">
                  <input
                    type="radio"
                    name="unifi-import-mode"
                    checked={importMode === 'canvas'}
                    onChange={() => setImportMode('canvas')}
                    className="cursor-pointer"
                    style={{ accentColor: ACCENT }}
                  />
                  Inventory + canvas
                </label>
              </div>
            </div>
            <div className="flex flex-wrap gap-2 pt-1">
              <Button
                size="sm"
                variant="ghost"
                className="gap-1.5 text-muted-foreground hover:text-foreground border border-border hover:bg-[#21262d]"
                onClick={handleTestConnection}
                disabled={connectionStatus === 'testing' || loading}
              >
                {connectionStatus === 'testing'
                  ? <Loader2 size={13} className="animate-spin" />
                  : <CheckCircle2 size={13} />}
                Test Connection
              </Button>
              <Button
                size="sm"
                style={{ background: ACCENT, color: '#0d1117' }}
                className="gap-1.5"
                onClick={handleFetchDevices}
                disabled={loading || connectionStatus === 'testing'}
              >
                {loading ? <Loader2 size={13} className="animate-spin" /> : <Wifi size={13} />}
                {importMode === 'pending' ? 'Import to Inventory' : 'Fetch Devices'}
              </Button>
            </div>
            <p className="text-[11px] text-muted-foreground italic">
              Leave any field blank to use what is configured on the server (.env).
              Create the key under Network → Settings → API. Switches, access points and
              gateways are imported; clients are left to the network scanner.
            </p>
          </div>

          {devices.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={checked.size === devices.length}
                    ref={(el) => { if (el) el.indeterminate = checked.size > 0 && checked.size < devices.length }}
                    onChange={toggleAll}
                    className="w-3 h-3 cursor-pointer"
                    style={{ accentColor: ACCENT }}
                    title="Select all"
                  />
                  <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                    Devices ({checked.size}/{devices.length} selected)
                  </span>
                </div>
              </div>

              {(Object.entries(groupedDevices) as [UnifiNodeType, UnifiNode[]][])
                .filter(([, group]) => group.length > 0)
                .map(([type, group]) => {
                  const Icon = DEVICE_TYPE_ICON[type]
                  const color = DEVICE_TYPE_COLOR[type]
                  return (
                    <div key={type}>
                      <div className="flex items-center gap-1.5 mb-1">
                        <Icon size={11} style={{ color }} />
                        <span className="text-[10px] font-medium uppercase tracking-wider" style={{ color }}>
                          {DEVICE_TYPE_LABEL[type]} ({group.length})
                        </span>
                      </div>
                      {group.map((device) => (
                        <div
                          key={device.id}
                          className={`flex items-start gap-2 p-2 mb-1 rounded-md text-xs cursor-pointer transition-colors border ${
                            checked.has(device.id)
                              ? 'bg-[#21262d] border-[#0559c9]/40'
                              : 'bg-[#21262d] border-transparent hover:bg-[#30363d]'
                          }`}
                          onClick={() => toggleCheck(device.id)}
                        >
                          <input
                            type="checkbox"
                            checked={checked.has(device.id)}
                            onChange={() => toggleCheck(device.id)}
                            onClick={(e) => e.stopPropagation()}
                            className="w-3 h-3 mt-0.5 cursor-pointer shrink-0"
                            style={{ accentColor: ACCENT }}
                          />
                          <div className="flex-1 min-w-0">
                            <div className="text-foreground font-medium truncate">{device.label}</div>
                            {device.ip && (
                              <div className="font-mono text-[10px] text-muted-foreground truncate">{device.ip}</div>
                            )}
                            <div className="text-[10px] text-muted-foreground truncate">
                              {[device.model, device.mac, device.status].filter(Boolean).join(' · ')}
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )
                })}
            </div>
          )}
        </div>

        <DialogFooter className="gap-2 shrink-0 pt-2 border-t border-border">
          <Button variant="ghost" onClick={handleClose}>Cancel</Button>
          {devices.length > 0 && (
            <Button
              onClick={handleAddToCanvas}
              disabled={checked.size === 0}
              style={{ background: ACCENT, color: '#0d1117' }}
              className="gap-1.5"
            >
              <Plus size={13} />
              Add {checked.size} to Canvas
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
