import { useEffect, useRef, useState } from "react"
import { cn } from "@/lib/utils"

/** A compact numeric input that commits valid values as they are typed.
 *
 *  Holds its own draft so "0." or an emptied box mid-edit is not rejected, and
 *  follows the value it is given whenever it is not being edited -- which is
 *  how a Reset elsewhere reaches it. */
export function NumberField({
  value, onCommit, min = 0, step = 1, unit, label, changed, className,
}: {
  value: number
  onCommit: (n: number) => void
  min?: number
  step?: number
  unit?: string
  label: string
  /** Differs from the recommendation: shown so an override is never silent. */
  changed?: boolean
  className?: string
}) {
  const [draft, setDraft] = useState(String(value))
  const editing = useRef(false)
  useEffect(() => { if (!editing.current) setDraft(String(value)) }, [value])

  const commit = (raw: string) => {
    const n = Number(raw)
    if (raw.trim() !== "" && Number.isFinite(n) && n >= min) onCommit(n)
  }
  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      <input
        type="number" inputMode="decimal" min={min} step={step} aria-label={label}
        value={draft}
        onFocus={() => { editing.current = true }}
        onChange={(e) => { setDraft(e.target.value); commit(e.target.value) }}
        onBlur={() => { editing.current = false; setDraft(String(value)) }}
        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur() }}
        onClick={(e) => e.stopPropagation()}
        className={cn(
          "h-7 w-[4.75rem] rounded-md border bg-background px-1.5 text-right font-mono text-[12px] tnum",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          changed ? "border-series-2 font-semibold" : "border-input",
        )}
      />
      {unit && <span className="text-[11px] text-muted-foreground">{unit}</span>}
    </span>
  )
}
