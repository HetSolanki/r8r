import type { FleetEntry } from "@/types"

/** An OCI type without the "VM.Standard." every one of them carries:
 *  "VM.Standard.E5.Flex:2ocpu-16gb" reads "E5.Flex:2ocpu-16gb". AWS names pass
 *  through unchanged. */
export function shortType(type: string): string {
  return type.replace(/^(VM|BM)\.Standard\./, "")
}

/** "2 × 4ocpu-16gb, 1 × 8ocpu-32gb" -- biggest count first. */
export function fleetText(fleet: FleetEntry[] | undefined | null): string {
  return [...(fleet ?? [])]
    .sort((a, b) => b.nodes - a.nodes || a.type.localeCompare(b.type))
    .map((f) => `${f.nodes} × ${shortType(f.type)}`)
    .join(", ")
}

/** The instance column: a pool's fleet when it has one, else the one type. */
export function typeText(row: { instanceType: string; fleet?: FleetEntry[] | null }): string {
  return row.fleet?.length ? fleetText(row.fleet) : shortType(row.instanceType)
}

/** `n × type` for a header; a pool reads as its fleet. */
export function sizeText(row: { nodes: number; instanceType: string; fleet?: FleetEntry[] | null }): string {
  return row.fleet?.length ? fleetText(row.fleet) : `${row.nodes} × ${shortType(row.instanceType)}`
}
