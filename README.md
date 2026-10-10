# r8r

Kubernetes cluster rightsizing. r8r reads a cluster's usage history,
replays its scheduler and node autoscaler, and recommends request, replica and
node changes that actually remove nodes.

| Folder | What it is |
|---|---|
| [`backend/`](backend/) | The `rightsizer` Python package and CLI: collects metrics, simulates, and exports the console's data. See [backend/README.md](backend/README.md). |
| [`frontend/`](frontend/) | The console: a React app that reads the exported data. See [frontend/README.md](frontend/README.md). |

Collected snapshots (`backend/out/`), exported console data
(`frontend/public/data/`) and `backend/clusters.json` hold real cluster details,
so git ignores them. Regenerate them locally with the backend.
