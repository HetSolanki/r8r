import { useEffect, useState } from "react"
import { Card } from "@/components/ui/card"
import { cn, usd } from "@/lib/utils"

/** Eases a number to its target on mount. Skipped entirely under reduced motion,
 *  so the figure is correct on first paint rather than animating from zero. */
function useCountUp(target: number, ms = 800) {
  const [value, setValue] = useState(target)
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setValue(target)
      return
    }
    let frame = 0
    let start: number | undefined
    const step = (t: number) => {
      start ??= t
      const p = Math.min(1, (t - start) / ms)
      setValue(target * (1 - Math.pow(1 - p, 3)))
      if (p < 1) frame = requestAnimationFrame(step)
    }
    frame = requestAnimationFrame(step)
    return () => cancelAnimationFrame(frame)
  }, [target, ms])
  return value
}

interface StatCardProps {
  label: string
  value: number
  sub: string
  tone?: string
  big?: boolean
  delayMs?: number
}

export function StatCard({ label, value, sub, tone, big, delayMs = 0 }: StatCardProps) {
  const n = useCountUp(value)
  return (
    <Card className="animate-rise p-4" style={{ animationDelay: `${delayMs}ms` }}>
      <div className="text-[10.5px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">{label}</div>
      <div
        className={cn(
          "mt-1.5 font-mono font-semibold tracking-[-0.02em] tnum",
          big ? "text-[clamp(22px,4vw,30px)]" : "text-[clamp(18px,3.2vw,24px)]",
          tone,
        )}
      >
        {usd(n)}
      </div>
      <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>
    </Card>
  )
}
