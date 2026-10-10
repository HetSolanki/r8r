import { Cloud as CloudGlyph } from "lucide-react"
import { cn } from "@/lib/utils"
import type { Cluster } from "@/types"

/** The clouds the exporter knows, by the adapter name it writes (`cloud`). */
export const CLOUDS: Record<string, { label: string; title: string }> = {
  aws: { label: "AWS", title: "Amazon Web Services" },
  oci: { label: "OCI", title: "Oracle Cloud Infrastructure" },
}

/** Exports from before clouds were recorded came from AWS, the only one then. */
export const cloudOf = (c: Pick<Cluster, "cloud">) => c.cloud || "aws"

export const cloudLabel = (cloud: string) => CLOUDS[cloud]?.label ?? cloud.toUpperCase()

/** A small square mark for the cloud a cluster runs in. Brand colours are fixed
 *  rather than themed: they identify the cloud, and read on both backgrounds. */
export function CloudIcon({ cloud, className }: { cloud: string; className?: string }) {
  const title = CLOUDS[cloud]?.title ?? cloud
  // `!`: a Button sizes every svg inside it to 16x16, which would squash the
  // wider AWS wordmark into a square.
  const box = cn("inline-block !h-4 !w-4 shrink-0 rounded-[3px]", className)
  if (cloud === "aws") {
    // The real wordmark (public/icons8-aws-48.png): navy letters and the orange
    // smile on a transparent ground. Navy vanishes on a dark surface, so
    // index.css inverts it there, keyed on `data-mark`. The path is relative,
    // like the data files', so the bundle works under any base.
    return (
      <img src="icons8-aws-48.png" alt={title} title={title} role="img" data-mark="aws"
           className={cn("inline-block !h-6 !w-6 shrink-0 object-contain", className)} />
    )
  }
  if (cloud === "oci") {
    return (
      <svg viewBox="0 0 16 16" className={box} role="img" aria-label={title}>
        <title>{title}</title>
        <rect width="16" height="16" rx="3" fill="#C74634" />
        {/* Oracle's rounded "O" */}
        <rect x="3.2" y="5.4" width="9.6" height="5.2" rx="2.6" fill="none" stroke="#fff" strokeWidth="1.5" />
      </svg>
    )
  }
  return (
    <span className={cn(box, "inline-flex items-center justify-center text-muted-foreground")} title={title}>
      <CloudGlyph className="h-3.5 w-3.5" aria-label={title} />
    </span>
  )
}

/** A cluster's name with its cloud's mark before it. */
export function ClusterName({ cluster, inherit = false, className }: {
  cluster: Pick<Cluster, "env" | "cloud">
  /** Keep the surrounding text size (a list) instead of the picker size. */
  inherit?: boolean
  className?: string
}) {
  return (
    <span className={cn("inline-flex min-w-0 items-center gap-2", className)}>
      <CloudIcon cloud={cloudOf(cluster)} />
      <span className={cn("truncate", !inherit && "text-[13.5px]")}>
        {cluster.env}
      </span>
    </span>
  )
}
