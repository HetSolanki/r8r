# r8r

Kubernetes cluster rightsizing that keeps production safe. r8r reads a cluster's
usage history, replays its scheduler and node autoscaler, and recommends request,
replica and node changes that actually remove nodes — alongside the fixes that
keep workloads from running out of memory.

[Video](https://github.com/user-attachments/assets/e742ac89-6a89-431e-b874-c800edb4ba56)

A 5-minute walkthrough: the fleet overview, cloud and autoscaler adapters,
stability findings, and a recommendation opened end to end — instance types
compared, requests ticked and edited, and the node count re-packed live.

## What it does

- **Savings that remove nodes.** A smaller request saves nothing until a node
  goes away. r8r packs the pods pod by pod and replays the autoscaler, so a saving
  is only reported when the node count actually drops — and when something blocks
  it (a PodDisruptionBudget, local storage, a node group's minimum size), it says
  what.
- **Options, not a single answer.** For each node group: right-size the requests,
  move to another instance type, lower the HPA floors, or a combination — each
  with its monthly saving, the steps it takes and why it ranks where it does.
  Untick a change or type your own value and the packing is recomputed in the
  browser.
- **Stability alongside cost.** Each workload's averages, bursts and peaks over
  the window flag containers above their memory request and HPAs pinned at their
  ceiling. Those fixes cost money, and r8r recommends them anyway.
- **Clouds and autoscalers are adapters, discovered automatically.** AWS (EKS) and
  Oracle Cloud (OKE) today, detected from the kubeconfig context or the nodes'
  labels. Cluster Autoscaler and Karpenter (including Karpenter for OCI), detected
  from what the cluster actually runs; Karpenter pools are replayed with
  Karpenter's own provisioning and consolidation. A node group no autoscaler
  manages is shown as fixed, never simulated. New clouds and autoscalers plug in
  through Python entry points.
- **Read-only.** It queries Prometheus (or Thanos) and the Kubernetes and cloud
  APIs with read-only calls, and never changes a cluster.

## Quick start

Requires Python 3.11+, Node.js, `kubectl`, the cloud's CLI (`aws` or `oci`) and a
Prometheus-compatible endpoint (`THANOS_URL`, default `http://localhost:9090`).

```bash
# 1. Collect and analyze a cluster
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/rightsizer --env my-cluster --context <kube-context> run

# 2. Export the console's data
python3 export_ui.py

# 3. Open the console
cd ../frontend
npm install
npm run dev          # http://localhost:5173
```

[backend/README.md](backend/README.md) covers every command and flag, and
[frontend/README.md](frontend/README.md) covers the console and deploying it.

## Layout

| Folder | What it is |
|---|---|
| [`backend/`](backend/) | The `rightsizer` Python package and CLI: collects metrics, simulates, and exports the console's data. See [backend/README.md](backend/README.md). |
| [`frontend/`](frontend/) | The console: a React app that reads the exported data. See [frontend/README.md](frontend/README.md). |

Collected snapshots (`backend/out/`), exported console data
(`frontend/public/data/`) and `backend/clusters.json` hold real cluster details,
so git ignores them. Regenerate them locally with the backend.

