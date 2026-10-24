# Sero backend image.
#
# One long-running process (D14): it migrates the datastore, then serves the
# API, the built UI, and the apply operations. It also shells out to `helm` and
# `kubectl` for the install and cluster-apply layers (D18), so both ship in the
# image at the versions docs/versions.md pins, verified by checksum rather than
# trusted from whatever a package index served that day.
#
# Every FROM is pinned by digest, not by tag: a tag moves, and an image that
# moves under you is an unreviewed dependency bump. See docs/versions.md for
# each pin and the date it was verified.
#
# Build (from the repository root):
#   docker build -t sero:local .

# --- Stage: build the UI -----------------------------------------------------
# node 22-trixie-slim, see docs/versions.md.
FROM node:22-trixie-slim@sha256:c5849ff9c9ebcd66615412f0b548ca5b8ecaef84003dc9ac2e077ebe46aaa3f6 AS ui

WORKDIR /ui
# The manifests first, so a source-only change does not reinstall the tree.
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

# --- Stage: resolve the locked dependency set --------------------------------
# python 3.14-slim, see docs/versions.md. The same digest as the runtime stage.
FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2 AS deps

# uv is a build-time tool only: the runtime installs from the exported
# requirements with pip, so nothing in the shipped image depends on it.
ARG UV_VERSION=0.11.21
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /lock
COPY pyproject.toml uv.lock ./
# --frozen refuses to re-resolve: the image carries exactly the lock, with the
# hashes pip then verifies. --no-dev leaves the test and lint tooling out.
RUN uv export --frozen --no-dev -o requirements.txt

# --- Stage: fetch the pinned cluster tools -----------------------------------
# Downloading here rather than in the runtime stage keeps curl, the archives,
# and their unpacked trees out of the shipped image.
FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2 AS tools

ARG TARGETARCH=amd64
# Pinned in docs/versions.md. Bumps are their own reviewed PR.
ARG HELM_VERSION=4.2.1
ARG KUBECTL_VERSION=1.37.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Checksums are per architecture, published by each project alongside the
# artifact. A download that does not match is a failed build, never a warning.
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) \
        helm_sha=479dca836e5b45e8bd222400c5591b0e3a647378f03ff96597180db97c17fdae; \
        kubectl_sha=6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f; \
        ;; \
      arm64) \
        helm_sha=596b9a73d366c1e72ce67d595c22805480e30914593aafbc9f547694e72814db; \
        kubectl_sha=922df28df248cc00a9e025f947704f1d1482de64ece54cfe57e61f19eaf1eef3; \
        ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /out; \
    curl -fsSL -o /tmp/helm.tar.gz \
      "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${TARGETARCH}.tar.gz"; \
    echo "${helm_sha}  /tmp/helm.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/helm.tar.gz -C /tmp; \
    install -m 0755 "/tmp/linux-${TARGETARCH}/helm" /out/helm; \
    curl -fsSL -o /tmp/kubectl \
      "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl"; \
    echo "${kubectl_sha}  /tmp/kubectl" | sha256sum -c -; \
    install -m 0755 /tmp/kubectl /out/kubectl; \
    /out/helm version; \
    /out/kubectl version --client

# --- Stage: the runtime ------------------------------------------------------
FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2 AS runtime

# A base image is only as fresh as the day it was built, and a stale base ships
# every CVE patched since. Upgrade at build time so the image is current as of
# its own build, not as of the base's.
RUN apt-get update \
 && apt-get -y upgrade \
 && rm -rf /var/lib/apt/lists/*

COPY --from=tools /out/helm /usr/local/bin/helm
COPY --from=tools /out/kubectl /usr/local/bin/kubectl

# --require-hashes makes the lock the contract: pip refuses anything whose
# artifact does not match the hash uv exported.
COPY --from=deps /lock/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

WORKDIR /app
COPY src/ /app/src/
COPY migrations/ /app/migrations/
COPY alembic.ini /app/alembic.ini
# Only the CLI wrapper. The other scripts are lint and contract checks that run
# in CI and on a laptop; shipping them puts code in the image that nothing in
# the image runs.
COPY scripts/sero /app/scripts/sero
COPY --from=ui /ui/dist/ /srv/sero/ui/

# A fixed uid so a volume's ownership can be set once and stay right. The
# deploy contract's fs_group matches it (see deploy/sero/gojira.yaml).
RUN groupadd --gid 10001 sero \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin sero
USER 10001:10001

# The image lays the repository out under /app, so the two path variables that
# default to a checkout's layout are set explicitly. helm and kubectl write
# caches and config under HOME, which a read-only root filesystem does not
# allow, so they are pointed at the writable scratch mount.
ENV SERO_UI_DIST=/srv/sero/ui \
    SERO_MIGRATIONS_DIR=/app/migrations \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    HELM_CACHE_HOME=/tmp/helm/cache \
    HELM_CONFIG_HOME=/tmp/helm/config \
    HELM_DATA_HOME=/tmp/helm/data \
    KUBECACHEDIR=/tmp/kube/cache

EXPOSE 8000
ENTRYPOINT ["python", "-m", "sero.web"]
