/** Shared UniFi Network import type definitions for the frontend. */

export type UnifiNodeType = 'switch' | 'ap' | 'router' | 'generic'

export interface UnifiNode {
  id: string
  label: string
  type: UnifiNodeType
  ieee_address: string
  hostname?: string | null
  ip?: string | null
  /** The controller always knows this, even where an ARP scan cannot see it. */
  mac?: string | null
  status: string
  vendor?: string | null
  model?: string | null
  parent_ieee?: string | null
  /** Device Inventory row this node draws — stamped by the import so the
   * canvas save links to it instead of minting a second row. */
  device_id?: string | null
}

export interface UnifiEdge {
  source: string
  target: string
}

export interface UnifiImportResponse {
  nodes: UnifiNode[]
  edges: UnifiEdge[]
  device_count: number
}
