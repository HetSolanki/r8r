/** Where the console's data lives, relative to the page so the bundle works at
 *  a domain root, under a subpath or from an S3 prefix.
 *
 *    data/index.json         which clusters and periods exist
 *    data/<file>             one cluster, one period, as the index names it
 *    <that file's folder>/hpa/*.json      per-HPA minute history
 *    <that file's folder>/trends/*.json   per-container usage trends
 *
 *  Locally (`export_ui.py`, `rightsizer/core/layout.py`) a cluster's files are
 *  in `data/<env>/`. From the central bucket each day has its own folder,
 *  `data/<env>/<YYYY-MM>/<DD>/`, so HPA history and trends are read from the
 *  folder of the period being viewed. */
export const DATA = "data/"
export const indexUrl = () => `${DATA}index.json`
export const dataUrl = (file: string) => `${DATA}${file}`

/** Per period and cluster: the folder its index file sits in. */
const dirs = new Map<string, string>()
let activePeriod = ""
const dirKey = (periodId: string, env: string) => `${periodId}\n${env}`

/** Remember where a cluster's files are for one period (from its index entry). */
export function recordDataDir(periodId: string, env: string, file: string) {
  dirs.set(dirKey(periodId, env), file.slice(0, file.lastIndexOf("/") + 1))
}

/** The period whose HPA history and trends the console reads. */
export function selectPeriod(periodId: string) {
  activePeriod = periodId
}

const dirOf = (env: string) => dirs.get(dirKey(activePeriod, env)) ?? `${encodeURIComponent(env)}/`

export const hpaUrl = (env: string, slug: string) => `${DATA}${dirOf(env)}hpa/${slug}.json`
export const trendUrl = (env: string, slug: string) => `${DATA}${dirOf(env)}trends/${slug}.json`
