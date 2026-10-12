# OpenBao install layer

This directory holds the OpenBao install layer: the pinned Helm chart reference
and the opinionated, generic values Sero installs OpenBao with. It is the D3a
layer from [../../docs/design.md](../../docs/design.md): a direct Helm install,
not the Terraform Helm provider, so OpenBao can be installed, upgraded, and
rolled back independently of the cloud substrate.

## What is here

- `values/values-base.yaml` - the opinionated baseline: HA on Integrated Storage
  (Raft), the built-in UI enabled, persistent data and audit volumes, and a seal
  stanza left to be injected per deployment. No company-specific values.
- `values/values-dev.yaml` - a dev overlay: single node, TLS disabled, Shamir
  unseal, small volumes. For local clusters only, never production.

The chart itself is not vendored. Sero installs the upstream `openbao/openbao`
chart at the exact version pinned in [../../docs/versions.md](../../docs/versions.md).

## How it installs

The Sero tooling composes the base values with an overlay and any
deployment-specific config, then applies the install in the agent-driven,
dry-run-first flow:

1. Add the pinned chart repository and refresh.
2. Render the effective values (base, then overlay, then deployment config).
3. Show the diff with `helm diff upgrade` (or `helm upgrade --install --dry-run`)
   so the change is visible before it lands.
4. After confirmation, `helm upgrade --install` at the pinned chart version.

Always pass the pinned chart version explicitly. Do not track a floating chart.

```
# Versions are illustrative; use the exact pins from docs/versions.md.
helm repo add openbao https://openbao.github.io/openbao-helm
helm repo update

# Dry-run (diff against live) before applying.
helm upgrade --install sero-openbao openbao/openbao \
  --version <pinned-chart-version> \
  --namespace openbao --create-namespace \
  -f deploy/openbao/values/values-base.yaml \
  -f deploy/openbao/values/values-dev.yaml \
  --dry-run
```

## Seal and unseal

`values-base.yaml` leaves the seal stanza to be injected per deployment.
Production uses auto-unseal via a cloud KMS (the seal provider and key reference
come from config); the dev overlay uses Shamir (manual unseal) so there is no
cloud dependency for local work. Never commit a real KMS key reference or any
unseal material; the patterns in the values file are placeholders.

## What this layer does not do

- It does not provision a cluster. That is the optional cloud substrate
  (Terraform, D10), and many adopters bring their own cluster.
- It does not configure OpenBao (namespaces, policies, auth, KV). That is the
  config layer (the OpenBao SDK, D3b), applied after OpenBao is up.
- It does not install the External Secrets Operator. That is the separate ESO
  bootstrap capability (D4a).
