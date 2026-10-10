import { usd0 } from "@/lib/utils"

/** A single stacked bar: what stays, then what is reducible.
 *
 *  Two segments carry a 2px surface gap between them so adjacent fills never
 *  read as one mark, and the data end is rounded while the baseline end is not. */
export function SpendBar({
  keep,
  save,
  max,
  delayMs = 0,
}: {
  keep: number
  save: number
  max: number
  delayMs?: number
}) {
  const keepPct = max > 0 ? (keep / max) * 100 : 0
  const savePct = max > 0 ? (save / max) * 100 : 0
  return (
    <div className="flex h-5 min-w-0 flex-1">
      <div
        className="animate-grow origin-left rounded-l bg-muted-foreground/30"
        style={{ width: `${keepPct}%`, animationDelay: `${delayMs}ms` }}
      />
      {savePct > 0 && <div className="w-0.5 shrink-0" />}
      {savePct > 0 && (
        <div
          className="animate-grow origin-left rounded-r bg-series-1"
          style={{ width: `${savePct}%`, animationDelay: `${delayMs + 40}ms` }}
        />
      )}
    </div>
  )
}

export function SpendLegend() {
  return (
    <div className="flex flex-wrap gap-3.5 text-xs">
      <span className="inline-flex items-center gap-1.5">
        <i className="inline-block h-2.5 w-2.5 rounded-sm bg-muted-foreground/30" />
        Projected
      </span>
      <span className="inline-flex items-center gap-1.5">
        <i className="inline-block h-2.5 w-2.5 rounded-sm bg-series-1" />
        Reducible
      </span>
    </div>
  )
}

export function BarScaleNote({ max, extra }: { max: number; extra?: string }) {
  return (
    <div className="mt-3 border-t pt-3 text-[11.5px] text-muted-foreground">
      Bars scale to {usd0(max)}/month{extra ? `. ${extra}` : "."}
    </div>
  )
}
