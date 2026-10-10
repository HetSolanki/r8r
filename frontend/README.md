# rightsizer-ui

Frontend for the `rightsizer` analyser. Reads static JSON files -- one per
cluster -- and renders the fleet: what each cluster costs, what is reducible, and the evidence behind every
recommendation.

Vite · React 18 · TypeScript · Tailwind · shadcn/ui (new-york, zinc).

## The data contract

The UI **never talks to a cluster**. It fetches `data/` from wherever it is
served, and those files are produced by the Python analyser:

```
collect (Thanos + kubectl + AWS)  ->  out/<env>/snapshot.json
export_ui.py                      ->  public/data/<env>/current.json   (one per cluster and period)
                                      public/data/<env>/hpa/, trends/
                                      public/data/index.json           (rebuilt from the above)
                                         |
                                     this UI
```

Regenerate from the backend (`../backend`):

```bash
cd ../backend
python3 export_ui.py                    # every cluster
python3 export_ui.py --env prod-us2   # one cluster; the others' files are not touched
```

The UI loads `data/index.json`, then every cluster file it lists in parallel. A
cluster whose file is missing, malformed or from an older export is left out of
the figures and named on the Overview; the rest load as normal. Paths are in
`src/lib/data-paths.ts`, mirroring `rightsizer/core/layout.py`.

The cluster payload's shape is defined in `src/types.ts`. Keep that file and `export_ui.py` in step —
they are the contract, and nothing else validates it at runtime.

## Develop

```bash
npm install
npm run dev          # http://localhost:5173
npm run build        # -> dist/
npm run preview      # serve dist/ locally
npm run lint         # tsc --noEmit
```

## Deploy

`vite.config.ts` sets `base: "./"`, so the build is path-agnostic — it works at a
domain root, under a subpath, or from an S3 prefix without rebuilding.

**S3 + CloudFront** (matches the read-only architecture: a CronJob writes
`data/` to the bucket, the UI reads it, neither touches a cluster):

```bash
npm run build
# The bundle: --delete is safe, data/ is excluded from it.
aws s3 sync dist/ s3://<bucket>/console/ --delete --exclude "data/*"
# The data changes on every collection, so do not let it cache. No --delete:
# a machine that exported only some clusters must not remove the others.
aws s3 sync public/data/ s3://<bucket>/console/data/ \
  --cache-control "no-cache, max-age=0"
aws cloudfront create-invalidation --distribution-id <id> --paths "/console/*"
```

**Any static host** — Nginx, S3 website hosting, GitHub Pages, a sidecar container.
There is no server, no API and no routing to configure: the app is a single page
and view state lives in the URL hash.

To point at a different dataset (a per-environment bucket, say), change `DATA`
in `src/lib/data-paths.ts`.

## Structure

```
src/
  types.ts              the export_ui.py contract
  lib/fleet.ts          loading (index + per-cluster files), fleet totals, labels
  lib/data-paths.ts     where data/ files live; mirrors rightsizer/core/layout.py
  lib/utils.ts          cn() plus money/cores/bytes formatting
  components/ui/        shadcn components (owned source, edit freely)
  components/           StatCard, SpendBar, CopyButton
  views/                Overview, Clusters, Actions, Workloads, ActionRow
  App.tsx               sidebar, hash routing, theme toggle
```

`components/ui/*` are shadcn sources copied into the repo, which is how shadcn is
meant to be used — they are yours to edit, not a dependency. Add more with
`npx shadcn@latest add <component>`; `components.json` is already configured.

## Conventions worth keeping

- **Signed money.** Negative reduces spend, positive increases it, `$0.00` is a
  correctness fix. `usd()` renders a true minus sign so columns align.
- **Status colour is never alone.** Every severity carries a label; colour is the
  secondary channel.
- **Charts use one scale.** Bars are a single stacked scale with a 2px gap between
  segments, and every label names a value the chart reaches.
- **Caveats stay visible.** The sidebar footer states that dollars are a ceiling and
  that nothing has been verified against a CUR bill. Those are load-bearing, not
  boilerplate — remove them only when a ledger makes them untrue.
