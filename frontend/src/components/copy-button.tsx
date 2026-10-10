import { useState } from "react"
import { Check, Copy } from "lucide-react"
import { Button } from "@/components/ui/button"

/** Clipboard writes can be refused (permissions, insecure origin). The button
 *  reports failure rather than claiming a copy that did not happen. */
export function CopyButton({ text, label = "Copy command" }: { text: string; label?: string }) {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle")
  return (
    <Button
      variant="outline"
      size="sm"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
          setState("done")
        } catch {
          setState("failed")
        }
        setTimeout(() => setState("idle"), 1800)
      }}
    >
      {state === "done" ? <Check /> : <Copy />}
      {state === "done" ? "Copied" : state === "failed" ? "Press Ctrl+C" : label}
    </Button>
  )
}
