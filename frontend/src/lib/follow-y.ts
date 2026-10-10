import { useLayoutEffect, useRef, useState, type PointerEvent } from "react"

/** Keeps a chart tooltip level with the pointer, so the eye never leaves the
 *  line to read it. Spread `box` on the positioned wrapper and give `tip` to the
 *  tooltip. With no pointer (the arrow keys), the tooltip keeps its class `top`.
 *  It is clamped inside the wrapper, so it never hangs past the chart's edge. */
export function useFollowY<B extends HTMLElement = HTMLDivElement, T extends HTMLElement = HTMLDivElement>() {
  const boxRef = useRef<B>(null)
  const tipRef = useRef<T>(null)
  const [y, setY] = useState<number | null>(null)

  // Every render: the tooltip's height changes with its rows.
  useLayoutEffect(() => {
    const tip = tipRef.current
    const box = boxRef.current
    if (!tip || !box) return
    if (y === null) { tip.style.top = ""; return }
    const h = tip.offsetHeight
    tip.style.top = `${Math.max(0, Math.min(box.clientHeight - h, y - h / 2))}px`
  })

  return {
    box: {
      ref: boxRef,
      onPointerMove: (e: PointerEvent<B>) => setY(e.clientY - e.currentTarget.getBoundingClientRect().top),
      onPointerLeave: () => setY(null),
    },
    tip: tipRef,
  }
}
