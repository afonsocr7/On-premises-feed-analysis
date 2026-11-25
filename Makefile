CLUSTER ?= feed
NS      ?= feed
MODEL   ?= gemma3:1b

SERVICES = ingester analyzer api

# Unique tag per invocation: helm only rolls pods out when the image tag changes.
# (:= freezes the value, so `make start` -> build and helm use the same tag.)
TAG     ?= $(shell date +%Y%m%d%H%M%S)
TAG     := $(TAG)
# The LLM image is large and only depends on the model, so it is tagged by model, not per build.
LLM_TAG := $(subst :,-,$(subst /,-,$(MODEL)))

.PHONY: build start stop lint check

check:  ## verify required tools
	@for t in docker k3d helm kubectl; do \
	  command -v $$t >/dev/null || { echo "missing: $$t (see README prerequisites)"; exit 1; }; \
	done

build:  ## build container images (uv, locked deps) and the LLM image with the model baked in
	@for s in $(SERVICES); do docker build -t feed/$$s:$(TAG) services/$$s || exit 1; done
	docker build -t feed/llm:$(LLM_TAG) --build-arg MODEL=$(MODEL) services/llm

start: check build  ## create k3d cluster, load images, install operator and chart
	@k3d cluster list $(CLUSTER) >/dev/null 2>&1 || k3d cluster create $(CLUSTER) --port "8080:80@loadbalancer" --agents 2
	@for s in $(SERVICES); do k3d image import feed/$$s:$(TAG) -c $(CLUSTER); done
	k3d image import feed/llm:$(LLM_TAG) -c $(CLUSTER)
	helm repo add cnpg https://cloudnative-pg.github.io/charts
	helm repo update
	helm upgrade --install cnpg cnpg/cloudnative-pg -n cnpg-system --create-namespace --wait
	helm upgrade --install feed ./chart -n $(NS) --create-namespace \
	  --set image.tag=$(TAG) --set llm.model=$(MODEL) --set llm.imageTag=$(LLM_TAG) --wait --timeout 15m
	@echo "UI: http://localhost:8080"

stop:  ## delete the cluster
	k3d cluster delete $(CLUSTER)

lint:
	helm lint ./chart