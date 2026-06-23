# Terraform: the cloud substrate (D10)

Terraform owns Ladon's durable cloud substrate and stops at the cluster
boundary: the Kubernetes cluster, the KMS key for auto-unseal, IAM and service
accounts, networking, snapshot storage, and DNS. It does not install OpenBao
(that is the Helm layer, D3a) or configure it (that is the SDK layer, D3b); it
produces outputs those layers consume. See [docs/design.md](../docs/design.md)
(D10) and [docs/deployment-order.md](../docs/deployment-order.md).

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

Cluster, networking, and snapshot-storage modules follow as the substrate layer
grows.
