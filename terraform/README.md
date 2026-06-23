# Terraform: Ladon's OpenBao substrate (D10)

Terraform here owns only the cloud resources that are specific to running
OpenBao, on a cluster that already exists. Cluster creation, networking, CIDR
planning, and project/account setup are NOT Ladon's job: a separate foundation
(landing-zone) repo owns those and creates the cluster to best practices, the
way an org-level platform repo does. Ladon, like a deploy platform that registers
an existing cluster, runs on top of what the foundation provides.

So this layer is deliberately small. It produces the OpenBao-specific substrate
the install (D3a) and config (D3b) layers consume:

- the cloud KMS key for auto-unseal, and the IAM grant to use it;
- (future) node pools for OpenBao under a foundation-created cluster.

It does not install OpenBao (Helm, D3a) or configure it (SDK, D3b). See
[docs/design.md](../docs/design.md) (D10) and
[docs/deployment-order.md](../docs/deployment-order.md).

## Conventions

- **Registry modules over raw resources.** Use the official cloud-provider
  Terraform modules where one exists, rather than hand-rolling provider resource
  blocks. Pin each to a stable version in [docs/versions.md](../docs/versions.md);
  module and provider bumps are their own reviewed PRs.
- **Multi-cloud, pluggable from the start.** Substrate pieces are organized as
  one module per cloud sharing a documented input/output contract (see
  [modules/unseal-key/README.md](modules/unseal-key/README.md)), so a deployment
  picks a cloud without the rest of Ladon changing. GCP is implemented first;
  AWS and Azure follow against the same contract.
- **No secret material in state output.** Modules grant the OpenBao identity key
  use via that cloud's workload identity; they do not create or export keys.

## Modules

| Module | Purpose | State |
| --- | --- | --- |
| [`modules/unseal-key`](modules/unseal-key) | Cloud KMS key for OpenBao auto-unseal (D2a), plus the key-use IAM grant. | GCP implemented; AWS/Azure planned. |

A node-pool module (OpenBao node pools under a foundation-created cluster) may
follow. Cluster, network, and project provisioning are out of scope here; they
belong to the foundation repo.
