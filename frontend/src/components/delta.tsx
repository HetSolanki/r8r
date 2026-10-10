import { ArrowDown, ArrowUp, Minus } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import type { Direction } from "@/types"

/** A signed capacity change.
 *
 *  Negative frees capacity and is shown in the savings colour; positive needs
 *  more and is shown in the warning colour. Never colour alone — the sign is
 *  always printed, so the meaning survives a greyscale print or a colour-blind
 *  reader. */
export function Delta({ value, unit, className }: { value: number; unit: string; className?: string }) {
  if (Math.abs(value) < 0.005) return <span className="text-muted-foreground">—</span>
  const frees = value < 0
  return (
    <span
      className={cn(
        "font-mono tnum",
        frees ? "text-series-1" : "text-status-serious",
        className,
      )}
    >
      {frees ? "−" : "+"}
      {Math.abs(value) >= 100 ? Math.round(Math.abs(value)) : Math.abs(value).toFixed(1)}
      {unit}
    </span>
  )
}

const DIRECTION: Record<Direction, { label: string; className: string; Icon: typeof ArrowDown }> = {
  frees: { label: "Frees capacity", className: "border-series-1 text-series-1", Icon: ArrowDown },
  needs: { label: "Needs more", className: "border-status-serious text-status-serious", Icon: ArrowUp },
  mixed: { label: "CPU down, RAM up", className: "border-status-warning text-status-warning", Icon: Minus },
  none: { label: "No change", className: "", Icon: Minus },
}

export function DirectionBadge({ direction }: { direction: Direction }) {
  const d = DIRECTION[direction] ?? DIRECTION.none
  return (
    <Badge variant="outline" className={cn("border", d.className)}>
      <d.Icon className="h-3 w-3" />
      {d.label}
    </Badge>
  )
}

export { DIRECTION }
