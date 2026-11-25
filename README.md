# On-premises feed analysis

Analysts subscribe to Bluesky handles and hashtags and see, per subscription, a sentiment
summary, how many posts it covers, and when it was last updated. Sentiment is computed by a
small local LLM (Ollama). Runs on Kubernetes (k3d locally), packaged with Docker and Helm.

See [docs/DESIGN.md](docs/DESIGN.md) for architecture, decisions and trade-offs.

## Prerequisites
docker, [k3d](https://k3d.io), kubectl, helm. About 10 GB free disk and 8 GB RAM.
The first start needs internet access (container images and the LLM model).

## Build and run

    make build   # build the service images (uv, locked deps, Python 3.12) and the LLM image
    make start   # build, create k3d cluster, import images, install CloudNativePG + the chart
    make stop    # delete the cluster

`make start` blocks until all pods are ready; the first run takes several minutes (Ollama and
Postgres images, 3 database instances). Then open **http://localhost:8080**.

The model (`MODEL`, default `gemma3:1b`) is downloaded at *image build time* and baked into the
`feed/llm` image, so the running LLM pods never need internet access.

## Using it
1. Choose Handle or Hashtag, enter a value (e.g. `bsky.app` or `ai`), click Subscribe.
2. The ingester polls Bluesky every 60 s and stores new posts.
   Only posts from the last 7 days (`RETENTION_DAYS`) are kept, so a quiet account may show 0 items.
3. The analyzer classifies them with the local LLM (CPU only, so a few seconds per post).
   A post that repeatedly fails is skipped after 5 attempts instead of blocking the queue.
4. The row shows the sentiment label with counts, the number of posts covered (the latest 50
   classified posts), and the last update. The summary is computed by the API from the labelled
   posts at read time. The page refreshes every 15 s. Unsubscribing removes the subscription and its data.

## Architecture
ingester (internet) -> Postgres (CloudNativePG, 3 instances) <- analyzer -> llm (Ollama, 2 replicas);
api + UI reads Postgres. Only the ingester can reach the internet. Everything else is
blocked by default-deny NetworkPolicies. Details in the design doc.

## Operations
Logs and status:

    kubectl get pods -n feed
    kubectl logs -n feed deploy/ingester -f
    kubectl logs -n feed deploy/analyzer -f

Verify the security boundary (must fail with a timeout/connection error):

    kubectl exec -n feed deploy/api -- python -c "import urllib.request as u; u.urlopen('https://example.com', timeout=5)"

Updates and rollback (rolling update, no downtime for the API):

    make start                   # local: every run builds images with a new timestamp tag,
                                 # so helm sees a changed image and rolls the pods
    helm rollback feed -n feed   # revert to the previous release

The LLM image is tagged by model (not per build), so it only rolls when `MODEL` changes.

In production, CI would push versioned images and a GitOps tool (Argo CD / Flux) would apply the chart;
the Makefile is a convenience for local use.

## Repository layout
    services/{ingester,analyzer,api}   Python services (pyproject.toml, uv.lock, Dockerfile)
    services/llm                       Ollama image with the model baked in
    chart/                             Helm chart (deployments, network policies, PDBs, ingress)
    docs/DESIGN.md                     design document
    Makefile                           build / start / stop

## Bluesky API notes
- Handles use the anonymous `getAuthorFeed` endpoint (`public.api.bsky.app`).
- Hashtags use `searchPosts` on `api.bsky.app` (`BSKY_SEARCH_BASE`); this is **unverified**: there are
  reports of 403s for unauthenticated search. If blocked, add app-password auth.
- Egress policy allows any public IP on port 443 (NetworkPolicies cannot filter by hostname).
  An egress proxy with a domain allow-list is the tighter production setup.

## Open items
- No CI workflow (deliberately out of scope for now), automated tests, TLS, authentication or backups
- Not yet run end-to-end on a real cluster: hashtag search in particular must be verified against Bluesky
- DB password is a chart value (use an external secret manager)
- Schema is created by the API at startup with idempotent additive SQL (use a migration tool, e.g. Liquibase)
- No metrics or alerting; sentiment quality of the small model is not evaluated (can be achived with monitoring tools such as grafana, prometheus ...)