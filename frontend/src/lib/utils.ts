import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

const MINUS = "−"

/** Money with cents. Uses a true minus sign, not a hyphen, so columns align. */
export function usd(n: number): string {
  return (
    (n < 0 ? MINUS : "") +
    "$" +
    Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  )
}

/** Money, no cents — for chart labels and dense tables. */
export function usd0(n: number): string {
  return (n < 0 ? MINUS : "") + "$" + Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })
}

export function millicores(cores: number): string {
  return cores < 1 ? `${Math.round(cores * 1000)}m` : cores.toFixed(2)
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

/** "25 Sep" -- short, in UTC, so every viewer sees the same day. */
export function shortDate(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`
}

/** "25 Sep 15:10" -- when one cluster's data was collected, UTC. */
export function shortDateTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const hm = `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`
  return `${shortDate(iso)} ${hm}`
}

/** The dates a set of clusters was collected over: "25 Sep", "22–28 Sep" or
 *  "30 Aug – 2 Sep". Clusters are collected on their own schedules, so a
 *  window has a span, not one moment -- and the latest of them is not "now". */
export function dateSpan(isos: string[]): string {
  const ds = isos.map((i) => new Date(i)).filter((d) => !Number.isNaN(d.getTime()))
  if (!ds.length) return ""
  const lo = new Date(Math.min(...ds.map((d) => d.getTime())))
  const hi = new Date(Math.max(...ds.map((d) => d.getTime())))
  const a = shortDate(lo.toISOString())
  const b = shortDate(hi.toISOString())
  if (a === b) return a
  if (lo.getUTCMonth() === hi.getUTCMonth()) return `${lo.getUTCDate()}–${b}`
  return `${a} – ${b}`
}

export function gib(v: number): string {
  return v >= 1 ? `${v.toFixed(2)}Gi` : `${Math.round(v * 1024)}Mi`
}

/** True when a cluster was sized from less history than was asked for. */
export function windowShortened(c: { windowDays: number; requestedWindowDays?: number }): boolean {
  return (c.requestedWindowDays ?? c.windowDays) > c.windowDays
}

/** "7 days" or "7 of 15 days": the history a cluster's figures rest on. */
export function windowLabel(c: { windowDays: number; requestedWindowDays?: number }): string {
  const days = (n: number) => `${n} day${n === 1 ? "" : "s"}`
  return windowShortened(c) ? `${c.windowDays} of ${days(c.requestedWindowDays!)}` : days(c.windowDays)
}
