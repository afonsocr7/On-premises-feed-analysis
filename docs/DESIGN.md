# Design document: on-premises feed analysis

## 1. Goal and scope
Analysts subscribe to Bluesky handles and hashtags and see, per subscription: a sentiment summary,
the number of items it covers, and when it was last updated. Sentiment comes from a small local LLM.
Non-functional focus: a hard network boundary, high availability, continuous updates, clean packaging.

## 2. Architecture
```
Bluesky ──443──> [ingester] ──> [Postgres] <── [analyzer] ──> [llm (Ollama)]
 (internet)                          ^
                                     └──── [api + UI] <── LAN ingress
```
| Component | Role | Internet | Talks to |
|---|---|---|---|
| ingester | polls Bluesky, stores posts | yes, 443, public IPs only | Postgres |
| analyzer | classifies posts, writes summaries | no | Postgres, llm |
| llm | Ollama serving a small model | no | nobody (called by analyzer) |
| api | REST + single-page UI | no | Postgres |
| postgres | state and work queue | no | nobody |

The components never call each other directly except analyzer -> llm. They communicate through
Postgres, so each can fail, scale or be updated independently.

## 3. Key decisions
**Postgres as store *and* queue.** Posts with `sentiment IS NULL` are the work queue; analyzers claim
rows with `FOR UPDATE SKIP LOCKED`. One dependency instead of DB + broker, and it is correct for this
volume. Trade-off: not for thousands of posts/second; Kafka/NATS would be the next step.

**Ingestion is separated from analysis.** This is what makes the security boundary enforceable: the
only pod with an internet route is the ingester. Analysis and the UI never touch the internet, so a
compromised analyst-facing component cannot exfiltrate data, and hostile post content cannot reach
anything with egress.

**Local LLM via Ollama, constrained output.** The model is asked for one label and its output is
constrained to a JSON-schema enum with temperature 0. This gives deterministic, parseable results from a
small model and limits the impact of prompt injection (posts are untrusted input). The model is
configurable (`llm.model`); the default is a placeholder small Gemma model.

**LLM stays offline; model arrives via a Job.** A one-off Job (`app=model-puller`, internet egress) writes
the model into a shared PVC. The LLM pod mounts it and never needs the internet. In an air-gapped site
the same volume would be pre-seeded from an internal mirror.

**API owns the schema.** Created at startup under a Postgres advisory lock so replicas do not race.

**Summary window.** Summary = latest 50 classified posts per subscription; label from net score
((pos - neg) / total, threshold ±0.2). `item_count` and `updated_at` come from the same row, so the three
values the analyst sees are always consistent.

## 4. Security boundary
Enforced with Kubernetes NetworkPolicies (`chart/templates/networkpolicies.yaml`):
1. `default-deny` for all ingress and egress in the namespace.
2. `allow-dns` to kube-system only.
3. `internet-egress` only for `app in (ingester, model-puller)`, port 443, excluding RFC1918 ranges.
4. Explicit pairwise allows: clients -> Postgres, analyzer -> llm, ingress controller -> api.

Pods run as non-root, read-only root filesystem, no privilege escalation, all capabilities dropped.

Limitations (honest):
- NetworkPolicies filter by IP, not domain, so the ingester can reach any public host on 443. Production
  fix: route ingester through an egress proxy with a domain allow-list (bsky hosts only).
- Enforcement depends on the CNI. k3s ships kube-router's policy controller; other clusters need Calico/Cilium.
  Verify with: `kubectl exec deploy/api -- python -c "import urllib.request as u;u.urlopen('https://example.com',timeout=5)"` (must fail).
- "LAN only" for analysts is delivered by the ingress. On a real site, bind it to a LAN-only address
  (MetalLB pool / firewall) and add TLS and authentication (OIDC proxy). Not done here.

## 5. High availability
- api: 2 replicas, readiness/liveness on `/healthz` (checks DB), RollingUpdate with `maxUnavailable: 0`,
  PodDisruptionBudget. Anti-affinity across nodes is an easy follow-up.
- analyzer: 2 replicas; safe thanks to `SKIP LOCKED`. If the LLM or DB is down, posts stay pending and are retried.
- ingester: 1 replica; duplicate-safe (`ON CONFLICT DO NOTHING`), so a brief outage only delays freshness.
  Analysts keep seeing the last summaries.
- **Gaps:** Postgres and the LLM are single replicas. Postgres HA -> CloudNativePG (3 instances, automatic
  failover, backups). LLM HA -> 2+ replicas on node-local model volumes (needs ReadOnlyMany or per-node PVCs;
  GPU/CPU capacity is the real constraint). Analysts are still served stale-but-available data when these are down,
  except the API itself needs Postgres.

## 6. Packaging and continuous updates
- One image per service, built with uv on `python:3.12-slim`, non-root user.
- One Helm chart; all environment differences are in `values.yaml`.
- `make build` / `make start` are a reviewer convenience (k3d cluster, image import, `helm upgrade --install`).
  The production path is CI -> registry -> GitOps (Argo CD / Flux) applying the chart; no Makefile involved.
- Updates: `helm upgrade` triggers rolling updates with zero unavailable API pods. Rollback is `helm rollback`.
  Use immutable tags (git SHA) in real use; `dev` with `IfNotPresent` is for local only.
- Schema changes must be backward compatible across one release (expand/contract) because old and new pods overlap.

## 7. What happens when...
- **An API pod dies:** readiness removes it, the other replica serves, Deployment replaces it.
- **Postgres dies:** UI/API return errors, ingester/analyzer retry; StatefulSet restarts it with its PVC. No data loss
  unless the volume is lost (backups: open item).
- **The LLM is down:** posts accumulate as pending; summaries go stale (`updated_at` shows it) but do not break.
- **Bluesky is down or rate-limits:** the fetch error is logged per source; existing data stays visible.
- **Node drain:** PDBs prevent taking down the last API/analyzer replica.

## 8. Open items
- Not run end-to-end on a real cluster; no automated tests; CI workflow not written (`.github/workflows` is empty).
- Hashtag search: `searchPosts` is reported to return 403 on `public.api.bsky.app`; default host is `api.bsky.app`.
  Verify; fall back to authenticated access with an app password.
- Postgres HA and backups; LLM HA; TLS, authentication, LAN-only exposure; egress proxy with domain allow-list.
- Secrets: DB password is a chart value (dev only); use an external secret manager.
- Model-pull Job is a plain Job named per release revision; old jobs are not garbage-collected (set `ttlSecondsAfterFinished`).
- Observability: structured logs only; no metrics/alerts (Prometheus + queue-depth alert would be first).
- Small-model sentiment quality is unevaluated (no labelled set); sarcasm and non-English text will be weak.
