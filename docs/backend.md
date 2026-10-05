# The backend

One long-running process serves the API the UI calls, the built UI itself, and
the apply operations, which run as background jobs. It is the `web` workload a
deployment runs next to OpenBao.

It never holds a secret value. Listings and details return masked key names;
values live only in OpenBao. The one route that returns plaintext is `reveal`,
and it is refused unless a deployment turns it on.

## Trust boundary

Read this before deploying. The backend does not authenticate anybody. It
believes what the proxy in front of it says, so two things are deployment
requirements, not suggestions:

1. **The pod must be reachable only through the authenticating proxy.** Anything
   that can open a connection to this process can name itself in a header and be
   that person. In practice: a default-deny `NetworkPolicy` that admits only the
   proxy or ingress controller, and a `Service` of type `ClusterIP`, never
   `NodePort` or `LoadBalancer`. Nothing else in the cluster should be able to
   reach the port, including other workloads in the same namespace.
2. **Where the proxy can issue a signed assertion, it MUST be configured.** A
   plain header is a claim and depends entirely on requirement 1 holding. An
   assertion is a proof and keeps holding if it does not. Configure
   `identity.assertion` (below) whenever the proxy supports it.

The backend logs a warning at startup when no assertion is configured, because
at that point the network is the only thing standing between a stranger and
every namespace. Task 6 ships the manifests that enforce requirement 1.

## Quickstart on a laptop

This runs the API with no proxy, no cluster and a throwaway database. Write
`./dev-config.yaml`:

```yaml
deployment:
  environment: dev
  openbao:
    address: https://vault.example.com
identity:
  # No proxy in front, so trust an environment variable instead. Laptop only.
  allow_dev_identity: true
access:
  admins: [you@example.com]
  members:
    you@example.com: [team-a]
```

Then:

```
uv sync
LADON_DATABASE_URL=sqlite:///./ladon.db \
LADON_CONFIG=./dev-config.yaml \
LADON_DEV_IDENTITY=you@example.com \
BAO_TOKEN=<a token you already have> \
PYTHONPATH=src uv run python -m ladon.web
```

`python -m ladon.web` migrates the datastore and then serves on port 8000.
Note your own address appears twice on purpose: under `admins` so you can use
the admin routes, and under `members` so you hold a namespace, because being an
admin grants none.

One more step before the secret routes answer: put yourself on the allow list,
which gates both this surface and the agent one.

```
curl -X POST localhost:8000/api/admin/allow-list \
  -H 'content-type: application/json' \
  -d '{"email": "you@example.com"}'
```

`GET /api/me` should now show your namespaces, and `GET /api/namespaces` should
answer instead of 403.

For the UI, run `cd ui && npm run dev`: its dev server proxies `/api` to
`http://127.0.0.1:8000`. There is no `ui_dist` in the config above, so the
backend serves the API only and the UI comes from Vite.

### Serving the built UI from the backend

Build it once (`cd ui && npm ci && npm run build`) and point `LADON_UI_DIST` at
`ui/dist`. Unknown non-`/api` paths then return `index.html` so the client-side
router works on a refresh.

### Running under uvicorn directly

```
PYTHONPATH=src uv run uvicorn --factory ladon.web.app:build_default_app --port 8000
```

This does **not** run migrations. Run them first
(`LADON_DATABASE_URL=... uv run alembic upgrade head`) or use `python -m
ladon.web`, which migrates and then serves.

## Environment

| Variable | Required | What it does |
| --- | --- | --- |
| `LADON_CONFIG` | no | Path to the deployment config (`config/example.yaml` is the template). Without it, and without `LADON_CONFIG_YAML`, nobody holds a namespace and nobody is an admin. |
| `LADON_CONFIG_YAML` | no | The same document inline, for a deployment that delivers config as environment variables rather than a mounted file. `LADON_CONFIG` wins when both are set. |
| `LADON_DATABASE_URL` | for anything but a smoke test | The datastore. Without it the routes that need one answer 503. |
| `LADON_UI_DIST` | no | Directory holding the built UI. Overrides `deployment.ui_dist`. |
| `LADON_DEV_IDENTITY` | no | The local identity hatch. Inert unless the config sets `identity.allow_dev_identity`, and inert whenever `identity.assertion` is configured. |
| `LADON_MIGRATIONS_DIR` | **yes, in an image or an installed layout** | Where the Alembic revisions live. The default is computed from the source tree (`<repo>/migrations`), which only exists when the process runs from a checkout. Any layout that installs the package without the repository around it must either set this or copy `migrations/` to the default path, or boot fails with `no migration scripts at ...`. |
| `LADON_HOST` / `LADON_PORT` | no | What `python -m ladon.web` binds. Defaults to every interface on 8000. |
| `BAO_ADDR` or `VAULT_ADDR` | no | OpenBao address, when the config does not give one. |
| `BAO_TOKEN` or `VAULT_TOKEN` | yes, to reach OpenBao | The token the backend authenticates with. It is never read from the config file, and a config that names one is rejected. |

## Identity

There is no login form. The proxy authenticates the person and tells the backend
who they are, one of two ways.

**A plain header** named by `identity.header`, defaulting to
`x-goog-authenticated-user-email`. The value may carry an `<issuer>:` prefix,
which is stripped, and it is lowercased before anything authorizes on it. This
is a claim; see the trust boundary above.

**A signed assertion**, configured under `identity.assertion`. For a cloud
identity-aware proxy in front of an external HTTPS load balancer, which is
the front door in
[deployment-order.md](deployment-order.md#6a-the-front-door):

```yaml
identity:
  assertion:
    header: x-goog-iap-jwt-assertion
    jwks_url: https://www.gstatic.com/iap/verify/public_key-jwk
    issuer: https://cloud.google.com/iap
    audience: /projects/<project-number>/global/backendServices/<backend-service-id>
    email_claim: email
```

The header, the key set and the issuer are the provider's, identical in every
deployment of this shape. The audience is not: it names one backend service,
and it is the field that stops a token the same proxy signed for another
service behind it from working here. The project *number* and the backend
service's numeric `id` are what go in it, neither of which is the name you
know them by; `gcloud compute backend-services describe <name> --global
--format='value(id)'` prints the second.

Any other proxy that signs what it asserts takes the same five fields with
its own values:

```yaml
identity:
  assertion:
    header: x-proxy-assertion
    jwks_url: https://proxy.example.com/keys
    issuer: https://proxy.example.com
    audience: https://ladon.example.com
    email_claim: email
```

When this is present it is required on every `/api` route and it decides the
identity; the plain header is not read at all, and neither is the dev hatch. The
token must carry a signature (ES256 or RS256 only, which is what refuses an
`alg: none` downgrade) over a key published at `jwks_url`, which must be an
https URL (over plain HTTP anyone on the path chooses the signer), and its issuer,
audience and expiry must all match. Anything else is `401 {"error": "no
identity"}`, indistinguishable from presenting nothing. Keys are cached for 15
minutes; an unknown key id buys one early refetch so rotation works, rate limited
so unknown ids cannot drive a fetch per request. Thirty seconds of clock skew is
tolerated on `exp` and `nbf`.

If the key endpoint goes down, the last key set that arrived keeps working for
an hour so an outage does not log everybody out, and refresh attempts are made
at most once a minute rather than once a request. Past that hour there are no
usable keys and every assertion is refused, because a key revoked during the
outage has to stop working eventually.

Every field is required. Verifying a signature without checking the audience
would accept any token the same proxy signed for any other service behind it.

Sending the deciding header twice is `401`. Two values mean something on the
path is adding or rewriting headers, and picking one of them is how a proxy's
value gets replaced by an attacker's.

`/healthz` is unauthenticated either way, because a probe carries no identity.

### The development hatch

Set `identity.allow_dev_identity: true` in the config and `LADON_DEV_IDENTITY`
in the environment and every request with no header is attributed to that
address. The backend logs a warning at startup saying identity is unverified.
Use it on a laptop and nowhere else: with it on, anyone who can set an
environment variable is anyone. A real header still wins, so the hatch cannot
mask what a proxy said, and an instance verifying assertions ignores it
entirely.

## One deployment, one environment

`deployment.environment` names the single environment this deployment serves.
One Ladon talks to one OpenBao, whose KV holds one set of paths, so a request
labelled `prod` and a request labelled `dev` would read and write the same
secrets under different names. Any route taking an `env` refuses another with
`400 {"error": "this deployment serves environment 'dev'"}`, and `/api/me`
reports the name so a client can show it rather than offer choices that will be
refused. The agent surface enforces the same rule through the same function, so
an MCP client cannot write prod-labelled secrets into a dev vault's paths
either. An instance that does not declare one pins nothing and says so at
startup.

Serving two environments means running two Ladons against two OpenBaos.

## Access

`access:` in the deployment config decides who reaches what (the shape is
documented in `config/example.yaml`):

- `admins` may use the admin routes. Being an admin grants no namespace.
- `members` maps an address to namespaces directly.
- `groups` maps a group to namespaces and `group_members` maps that group to
  addresses. A caller gets the union of both paths.

This is the first `GroupSource`. A directory-backed one replaces it without
touching a route.

Two gates stand in front of the secret routes. The first is the allow list
(`mcp_allow_list`), which governs the agent surface and this one alike, because
both call the same functions. The second is namespace membership. A caller who
is not on the allow list, or who asks for a namespace they do not hold, gets
`403`.

That ordering means a new deployment bootstraps through its admins: an admin
comes from the config, `/api/me` is outside the allow list so they can see
themselves, and `POST /api/admin/allow-list` is how the first person gets on.

## The machine lane

Some writers are not people. A provisioner that stands up an application
provisions that application's secret on behalf of whoever asked it to, minutes
or hours later, with nobody's browser in the loop. There are only bad ways to do
that on the human lane: hold a person's credential past their session, or let
the proxy header be set by something the proxy never authenticated.

So machines get their own lane (D16), reachable at `/api/service/*` and nowhere
else. It shares the human lane's token verification and shares nothing else.

### Why it is separate

- **A different audience.** A caller whose `audience` equals the human
  assertion's is refused at config load, not at request time. Share the audience
  and a person's proxy assertion is a machine credential, which is the failure
  the separation exists to prevent.
- **A pinned subject.** Trusting an issuer is not trusting every workload that
  issuer will sign for. The lane accepts exactly one `sub` per caller.
- **Pinned namespaces.** A machine that can reach one namespace must not thereby
  reach all of them. The namespaces a caller may write are config, next to its
  subject, and so are the Kubernetes namespaces it may place an object in.
- **A named human.** `acting_email` says who the write is for. It is recorded
  and never authorizes: the caller supplies it, so it could say anything, and
  nothing this lane decides depends on it. Read it as "this machine says it
  acted for this person", which is what it is.
- **A short-lived token.** An issuer's own expiry is the issuer's decision, and
  issuers are generous with it. This lane sets its own ceiling.

The plain identity header is not read on these routes, the development hatch is
not read, and a deployment that configures no callers refuses the lane entirely.
The lane opens because somebody configured it, never because somebody forgot to.

### Configuration

```yaml
deployment:
  # The one cluster this deployment serves, by the label team onboarding used
  # for its jwt-<cluster> mount. Needed for the lane to onboard an application
  # whose store is not there yet; without it the lane refuses to, and says so.
  cluster: cluster-a
service_callers:
  - name: provisioner
    issuer: https://issuer.example.com
    jwks_url: https://issuer.example.com/keys
    audience: https://ladon.example.com/service
    subject: system:serviceaccount:ladon:provisioner
    namespaces: [team-a]
    k8s_namespaces: []
    acting_email_required: true
    rate_limit_per_minute: 60
    read_limit_per_minute: 300
```

`name`, `issuer`, `jwks_url`, `audience`, `subject` and `namespaces` are all
required, because each one is a boundary. `jwks_url` must be https: over plain
HTTP anyone on the path chooses the signer. `acting_email_required` may only be
`true`; a lane that wrote on nobody's behalf would leave a machine write with no
trace of a human, so `false` is refused rather than honoured, and every route
on the lane requires the field, the read included. Two callers may not share an issuer and audience pair,
because that pair is what picks the caller a token belongs to.

No two namespaces pinned to one caller may end in the same segment. A store
name is built from the last segment of a namespace, because namespaces nest and
a team's namespace ends in the team's own name, so `platform/team-a` and
`other/team-a` name the same stores for the same application names and the
checks above could not tell one team's object from the other's. That pair is
refused at config load rather than left as a deployment where those checks mean
less than they say.

`k8s_namespaces` is where this caller may place an ExternalSecret beyond the
application's own name. Leaving it empty is the right answer for the common
deployment, not an oversight: one application, one namespace named after it.

`namespaces` entries may be nested OpenBao paths (`platform/team-a`), and both
routes accept the same shape.

### Reaching it from inside the cluster

The trust boundary above says nothing but the proxy may reach this port, and
`deploy/ladon/k8s/base/networkpolicy.yaml` enforces exactly that. A machine
caller running in the cluster is not the proxy, so it is denied until the
deployment admits it: add an ingress rule naming that workload, alongside the
proxy rule, and nothing else.

**Do not do this unless `identity.assertion` is configured.** There is one port,
not one port per lane. A NetworkPolicy admits a workload to the whole process,
including `/api/namespaces/*` and `/api/admin/*`, and on a header-only
deployment those routes believe whatever `identity.header` says. An admitted
machine could therefore set that header to any address and be any person: read
every namespace that person holds, reveal where reveal is on, and put itself on
the allow list through the admin routes. None of the machine lane's pins apply,
because none of those routes are the machine lane.

`/api/service/*` is also the only place a `DELETE` reaches a secret at all. The
human surface has no route that removes a path: a person who wants one gone
asks an operator, and the reason it is here instead is that a machine that
created an application is the thing that knows when that application is over.

With an assertion configured the human routes want a signed proof instead, and
the machine's own token does not verify as one: the audiences must differ, which
config enforces. That is what makes admitting the workload safe, and it is why
the two requirements go together rather than either one alone.

Given that, admitting it is not a hole in requirement 1: a caller that reaches
the port still has to present a token signed by the configured issuer, for the
configured audience, naming the pinned subject. Keep the rule as narrow as the
lane is: one workload, one port.

### What the token must be

`Authorization: Bearer <OIDC ID token>`, verified by the same machinery the
human assertion uses: ES256 or RS256 only (which is what refuses an `alg: none`
downgrade) over a key published at `jwks_url`, the configured issuer and
audience, thirty seconds of clock leeway, and the same key cache. On top of that
the lane requires `iat` and `sub`, and the subject must equal the caller's
pinned one.

Two things are stricter here than on the human lane:

- **A maximum token age of 900 seconds**, measured from `iat` and independent of
  the token's own `exp`. A token minted eight hours ago and still unexpired has
  had eight hours to end up in a log, an environment variable or a queue. Mint a
  fresh one per call; that is what the issuer is for. A token stamped in the
  future, past the leeway, is refused for the mirror reason.
- **A ten-minute stale-key grace**, not the human lane's hour. Keeping a UI
  usable through a key-endpoint outage is worth an hour of risk; letting a
  machine retry its provisioning call later is not. In practice this means the
  first refresh that fails after the cache's fifteen-minute lifetime empties it,
  rather than the keys riding out an hour of outage.

Anything else is `401 {"error": "no service identity"}`: an absent token, a
token for the human lane, another audience, another issuer, another subject, an
expired token, one not yet valid, one too old, a signature from a key that is
not published, or a token naming the audiences of two configured callers at
once (whose subject and namespaces would apply is not a question this lane
guesses at).

### What a caller can do

Five things, which together are the life of one application secret: write it,
see what is in it by name, put the cluster object in front of it, take that
object away, and delete the secret itself.

**`POST /api/service/secrets`** merges keys into one application path. The
semantics are the human lane's, because they are the same two functions: values
merge into what is there, `remove_keys` removes, a key named on both sides is
refused, a merge that would empty the path is refused, placeholder values are
refused, and nothing is written without `apply`.

```json
{
  "acting_email": "operator@example.com",
  "namespace": "team-a",
  "path": "apps/sample/demo",
  "values": {"DB_URL": "<the value, never logged or stored by Ladon>"},
  "remove_keys": [],
  "apply": false
}
```

The answer is the write summary the other surfaces return, key names only:

```json
{
  "applied": false,
  "action": "update",
  "namespace": "team-a",
  "path": "apps/sample/demo",
  "env": "dev",
  "written_keys": ["DB_URL"],
  "removed_keys": []
}
```

**`GET /api/service/secrets`** answers which keys one path holds, by name. It
takes its arguments in the query string (`acting_email`, `namespace`, `path`)
because there is no body on a read, and `acting_email` is as required here as
it is on a write.

```
GET /api/service/secrets?acting_email=operator@example.com&namespace=team-a&path=apps/sample/demo
```

```json
{
  "namespace": "team-a",
  "path": "apps/sample/demo",
  "keys": ["API_KEY", "DB_URL"],
  "version": 2,
  "updated_at": "2026-09-22T10:00:00.000000Z"
}
```

Names, sorted, and nothing else. This is narrower than the human read beside
it, which returns a masked preview of every value: a mask is a hint about a
value, and a caller assembling an application needs to know whether `DB_URL` is
there, not what it starts with. No value, masked or otherwise, leaves through
this route. The version and the timestamp are what make the answer useful for
deciding whether a path needs writing; they say a path changed without saying
what it changed to.

A path that is not there is `404 {"error": "there is no secret at ..."}`, and
still an audited read: the request was authorized and the lookup happened, and
who asked about a path that does not exist is worth being able to answer.

Only a 404 from OpenBao means "not there". A denial, an expired token, a sealed
vault or an unreachable one is a `500` with an audit row saying `error`, never
a `404`: "I could not look" and "there is nothing there" are different answers,
and a caller acting on the wrong one would treat a live secret as gone. The
`DELETE` below inherits the same rule, which is why it can say `deleted: false`
and mean it.

**`DELETE /api/service/secrets`** removes one application path and every
version of it. This is the half of provisioning the lane did not have: an
application that goes away otherwise leaves its secret behind, and a KV path
nobody is watching is the one whose values go stale in place.

```json
{
  "acting_email": "operator@example.com",
  "namespace": "team-a",
  "path": "apps/sample/demo",
  "apply": false
}
```

Metadata and all versions, not KV v2's default soft delete: a soft-deleted path
still exists, still lists, can be undeleted, and its versions still hold the
values. Dry run by default, like every write here, and the dry run says what it
would remove:

```json
{
  "applied": false,
  "namespace": "team-a",
  "path": "apps/sample/demo",
  "keys": ["API_KEY", "DB_URL"],
  "would_delete": true
}
```

With `apply`, `would_delete` becomes `deleted`. Deleting a path that is already
gone is `"deleted": false` with an empty `keys` and a `200`, because a destroy
step that fails when it is run twice is a destroy step nobody can retry. The key
names are read before the delete and go into the audit row as `key_names`, so
the record still says what was destroyed once the path is gone.

The delete runs whether or not the read found anything, and the path is read
once more afterwards. OpenBao answers a metadata delete the same either way, so
skipping it on the strength of a read taken a moment earlier only adds a race;
and a delete Ladon reported that OpenBao did not perform is worth catching here
rather than the next time somebody finds the credential still live.

Two grants this route needs that the write routes do not. The backend's OpenBao
token needs `delete` on `kv/metadata/<path>`, which is the capability that
removes the versions rather than soft-deleting one; without it the call reaches
OpenBao and fails there. And the backend's ServiceAccount needs `delete` on
`externalsecrets` for the route below, which `deploy/ladon/k8s/base/clusterrole.yaml`
grants on that kind and no other.

**`POST /api/service/sync`** makes sure the Kubernetes Secret exists. Writing
the value into OpenBao is half the job; until an `ExternalSecret` reads the path,
the workload's Secret is not there and the pod does not start. One document,
converged, so a second call over the same application reports it already current
and writes nothing.

```json
{
  "acting_email": "operator@example.com",
  "namespace": "team-a",
  "app": "sample",
  "path": "apps/sample/demo",
  "name": "sample-demo",
  "refresh_interval": "1h",
  "apply": true
}
```

The answer carries the store it pointed at and the converge report, which is
verbs and object names:

```json
{
  "applied": true,
  "namespace": "team-a",
  "app": "sample",
  "path": "apps/sample/demo",
  "store": "ladon-team-a-sample-<digest>",
  "k8s_namespace": "sample",
  "name": "sample-demo",
  "target_name": "sample-demo",
  "report": ["ExternalSecret/sample/sample-demo: create"],
  "blocked": []
}
```

The store name comes from the same function that named it at onboarding, so a
typo cannot point a document at a store nothing created.

#### An application that has not been onboarded

The lane reads the store before it renders anything. When the store is not
there, the application has no identity on the cluster yet, and an
ExternalSecret pointing at it would be a Secret that never arrives. So the sync
onboards the application first, from the same `app-onboarding` phase the CLI
and the admin endpoint run: the read-only policy on `apps/<app>/` in the
caller's pinned OpenBao namespace, the JWT role on the deployment's
`jwt-<cluster>` mount bound to the application's own service account, that
service account in Ladon's namespace, and the ClusterSecretStore admitted into
the one Kubernetes namespace the sync names (the application's own, or one the
caller is pinned to). Nothing is rendered twice: the phase runner is handed the
flags an operator would type.

The OpenBao half converges first. A blocked half, or a cluster document that
would not land, is a `409` and the ExternalSecret is not written (the report
in the answer says what each operation did, since a policy and a role may
already have landed); a vault or a cluster that would not answer, or a
deployment whose config the phase refuses, is a `503`. A dry-run sync plans
the onboarding and writes nothing. The answer carries `onboarding`, the run op
by op in the same names-and-verbs vocabulary as `report`, empty when the store
was already there, and the audit row for the sync records `onboarded: true`
for an applied onboarding or `would_onboard: true` for a planned one.

The ownership check on an existing ExternalSecret (below) runs before any of
this, so a request that ends in a `403` or a `409` has not onboarded an
identity on the way. And when the store is already there, the sync reads
which Kubernetes namespaces it admits: onboarding fixes that set, an
ExternalSecret anywhere else is one ESO refuses at refresh time, so a sync
into a namespace the store does not admit is a `409` naming both. Widening a
store is an operator's `app-onboarding` decision, not this lane's.

This is why `deployment.cluster` exists in the config (see
[Configuration](#configuration)): the role's mount is named after the cluster,
and a deployment that has not named one refuses to onboard from the lane with a
message that says so, rather than guess.

An ExternalSecret that is **already there** has to be this application's before
it is converged, and the check is the one the `DELETE` applies: the live object
must read through this application's own `ClusterSecretStore` and pull a path
inside `apps/<app>/`, or the answer is `403`, and its live field manager must
be `ladon`, or the answer is `409`. A name nobody has used is still this
caller's to create.

This is not belt and braces on top of the delete gate; it is what makes that
gate mean anything. `app` is a field in the body and `k8s_namespace` defaults
to it, and a server-side apply under Ladon's own field manager takes the fields
it sets from Ladon's own earlier apply without a conflict, because as far as
the API server can tell that is the manager doing its job. Without this check,
a caller pinned to one namespace could write over another team's live
ExternalSecret, repoint it at a store it does control, and then delete the
object it had just made look like its own.

#### Where the object lands

`k8s_namespace` is optional and defaults to the application's name, which is
where onboarding puts it. Naming a different one requires that namespace to be
listed in the caller's `k8s_namespaces`; otherwise it is `403`.

This is not the same gate as the store's `namespaceSelector`, and it is worth
being precise about which does what:

- The store's selector, pinned at onboarding to the application's Kubernetes
  namespace, decides **what ESO may read through it**. An ExternalSecret in
  another namespace referencing that store gets nothing.
- This pinning decides **where an object may be written on the cluster**. That
  is a separate question, and nothing in ESO answers it: the backend holds the
  `ladon` field manager, so without this an admitted caller could create or
  replace an ExternalSecret in any namespace on the cluster. The object would
  read no secrets, but creating and taking ownership of objects in namespaces
  the caller has no business in is its own problem.

`target_name` is subject to the same list. Naming a target different from `name`
is how an ExternalSecret adopts a Secret the consumer already uses, which is
also how it would take over one it should not, so it is allowed only in a
namespace the deployment admitted on purpose. In the default namespace, the
target is the ExternalSecret's own name.

**`DELETE /api/service/sync`** removes one ExternalSecret. Deleting the KV path
without this leaves an object ESO keeps reconciling: an error in the operator's
log every interval over a path that is gone on purpose, and the Kubernetes
Secret it owns still sitting there.

**It takes the Secret with it.** Ladon renders every ExternalSecret with
`creationPolicy: Owner`, so the Secret ESO creates carries an owner reference
back to it, and when the ExternalSecret goes the garbage collector deletes the
Secret. A pod already running keeps the copy it mounted until it restarts, and
then does not start. That is what taking an application down means, and it is
the reason this route is the most guarded thing on the lane: its blast radius
is a workload's Secret, not one custom resource. The answer says so in
`removes_target_secret`, read from the live object rather than assumed, so a
dry run tells a caller what an apply would take away.

```json
{
  "acting_email": "operator@example.com",
  "namespace": "team-a",
  "app": "sample",
  "k8s_namespace": "sample",
  "name": "sample-demo",
  "apply": true
}
```

```json
{
  "applied": true,
  "namespace": "team-a",
  "app": "sample",
  "store": "ladon-team-a-sample-<digest>",
  "k8s_namespace": "sample",
  "name": "sample-demo",
  "removes_target_secret": true,
  "state": "gone",
  "recreated": false,
  "report": ["ExternalSecret/sample/sample-demo: delete"],
  "deleted": true
}
```

`state` says where the cluster is now, which is not always the same as what was
asked for. `gone` is the ordinary outcome. `terminating` means the API server
accepted the delete and a finalizer has not finished, so the object and the
Secret it owns are still there for the moment; `removes_target_secret` stays
`true`, because the Secret is going and has not gone, and both of those are
worth saying. `recreated` means somebody created a new object under the same
name after this one was removed: what this call deleted is gone, and what
stands at the name now is not it. That one is repeated as its own boolean,
because a caller that reads `deleted` and stops would be leaving a live
ExternalSecret it does not know about. `absent` and `unchanged` are the two
where no delete ran.

The uid is what tells those apart: the object is read before the delete and
again after, and a different uid is a different object. This narrows the window
rather than closing it. `kubectl delete` takes no uid precondition, so an
object replaced between the check and the delete is one the delete removes;
closing that needs a precondition only the API server can enforce.

A dry run answers `would_delete` in place of `deleted`, like the secrets
`DELETE`: what a call has not done, it does not report as done.

The body names the object and nothing else. No path, no store, no refresh
interval: a removal should not depend on the store name and the path still
resolving the way they did when the object was created.

Four rules stand in front of it.

- **It must be Ladon's object.** The live object is read at the moment of the
  delete, and if its `managedFields` do not record `ladon`, the answer is `409`
  naming the manager that does own it and nothing is deleted. This is the
  mirror of never passing `--force-conflicts` on the apply side: taking a field
  from another manager is an operator's decision, and so is deleting that
  manager's object. Entries for a subresource do not count as ownership, since
  ESO records itself on the status of every ExternalSecret it reconciles.
- **It must be this application's object, in this caller's namespace.** `app`
  is a field in the body and `k8s_namespace` defaults to it, so the ownership
  check alone would let a caller pinned to one OpenBao namespace name another
  team's application and reach an ExternalSecret Ladon manages for somebody
  else. The live object has to read through this application's own store,
  whose name is derived exactly as the POST derives it, with
  `secretStoreRef.kind` of `ClusterSecretStore` (ESO's default for a ref that
  names no kind is the namespaced `SecretStore`, and a matching name in a ref
  of the other kind is a different object), and it has to pull a path inside
  `apps/<app>/`. Either mismatch is `403`. The message names the store this
  caller's own application would use and the prefix it would read, both derived
  from the request; it says nothing about the live object, whose store name
  carries another tenant's team and application and whose key is their path. A
  caller that may not touch an object may not learn about it either.
- **The same pinning as the POST.** `k8s_namespace` defaults to the
  application's name, and anywhere else must be in the caller's
  `k8s_namespaces`; otherwise `403`.
- **Dry run by default.** A dry run does the reads and both checks, so it
  answers the same `403` and `409` an apply would, and deletes nothing.
  `would_delete` is `false` when the object was already gone, which is a
  success; the report line reads `absent`. An object the route's own read found
  absent is answered there and not looked for a second time: the removal reads
  again, and an object created in between would be one nothing had checked.

kubectl is called with `--ignore-not-found` and `--wait=false`, and the object
is read again afterwards: a delete kubectl accepted is not a delete the cluster
performed, so the report says `delete` only for an object that is gone or
carries a `deletionTimestamp`, and one still present raises.

It removes the ExternalSecret and only that. The application's
ClusterSecretStore, its JWT role, its read-only policy and its service account
stay, and so does the KV path. Those are the application's identity, and
removing them is a separate call:

**`DELETE /api/service/app`** removes exactly those four, derived from the
caller's pinned namespace and the application by the functions that named
them at onboarding. Body: `acting_email`, `namespace`, `app`, optional
`k8s_namespace` (the application's own unless the caller is pinned to
another), `apply`. Cluster first, store before service account (with the
store gone nothing can log in as the account), then OpenBao, role before
policy. Every cluster object is read and checked for the `ladon` field
manager before it goes, an object that is not there is `absent`, and a dry
run removes nothing. It is refused with a `409` while any ExternalSecret in
the application's Kubernetes namespace still reads through the store: remove
those with `DELETE /service/sync` first, then the identity. The deployment
must name its cluster (`deployment.cluster`) to name the role's mount.

```json
{
  "applied": true,
  "namespace": "team-a",
  "app": "sample",
  "store": "ladon-team-a-sample-<digest>",
  "k8s_namespace": "sample",
  "report": [
    "ClusterSecretStore/ladon-team-a-sample-<digest>: delete",
    "ServiceAccount/ladon/eso-team-a-sample-<digest>: delete",
    "openbao auth role eso-team-a-sample-<digest>: delete",
    "openbao policy eso-sample-readonly: delete"
  ],
  "blocked": []
}
```

### What a caller cannot do

- Write outside `apps/`. This lane provisions application secrets and nothing
  else; any other path is `400`. A path with an empty or relative segment is
  refused too, because `..` reads as inside the prefix while naming something
  outside it, and so is one carrying a percent escape (`%2e%2e` becomes `..`
  one decoding hop later) or one longer than 256 characters.
- Write outside the application's own prefix, on sync: `apps/<app>` is the only
  prefix that application's identity can read.
- Write in an OpenBao namespace its config does not pin to it. That is `403`,
  whatever the acting email holds on the human lane.
- Place a cluster object in a Kubernetes namespace its config does not pin to
  it, or name a target Secret outside one. Also `403`; see above.
- Read a value. The lane's read returns key names, and the reveal route, the
  one place a plaintext value leaves Ladon, is not on it.
- Touch the allow list, approve a namespace request, or run an apply phase.
  Those are human routes and stay human.
- Off-board an application: remove its ClusterSecretStore, its JWT role, its
  policy or its service account. The lane takes one secret down, not an
  application's identity.
- Delete a cluster object another field manager owns. That is `409`, and the
  message names the manager.
- Exceed its call budget. Each caller has two in-process token buckets: 60
  writes a minute and 300 reads, by default. Past either, the answer is `429`
  and the message says which ran out. The read is counted separately because a
  caller watching an application come up polls it, and one shared bucket would
  let that polling spend the allowance the same caller's writes need: the call
  that got refused would be the one it was polling in order to make. Neither is
  a distributed quota, only a guard against a stuck loop filling the vault, so
  a second replica has a second pair.

### Auditing

One row per call, in the same table as everything else. Every route records
against `acting_email`, under a tool name that says which one it was:

| Route | Tool name |
| --- | --- |
| `POST /api/service/secrets` | `service.put_secret` |
| `GET /api/service/secrets` | `service.get_secret` |
| `DELETE /api/service/secrets` | `service.delete_secret` |
| `POST /api/service/sync` | `service.sync_secret` |
| `DELETE /api/service/app` | `service.offboard_app` |
| `DELETE /api/service/sync` | `service.delete_sync` |

A dry run is recorded as `dry-run` and an apply as `ok`, so the trail
distinguishes what was asked from what happened. Every row this lane writes
carries three arguments saying how it got there:

| Argument | What it says |
| --- | --- |
| `lane` | Always `service`. One predicate separates every machine-on-behalf row from a person's own. |
| `via` | The caller's configured name: which machine. |
| `subject` | The identity that machine proved. |

`user_email` is the person the call named, not the person Ladon authenticated,
and the difference matters when reading these rows. On the human lane that
column is an address a proxy verified. Here it is an address a machine supplied,
and nothing the lane decided depended on it: a caller may name any address at
all, including one that has never used Ladon. The three arguments above are what
is actually proven, so read such a row as "`via` acted, and said it acted for
`user_email`". A row with no `lane` is a person's own.

Ladon does not require the acting email to be on the allow list, on purpose. The
lane exists to provision secrets for people at the moment they ask an automated
system for something, and many of those people never touch Ladon directly.

Denials are recorded too, including the ones that never got past the token.
Those carry `via` as the caller the token claimed to be, or `unknown` when it
matched none, and are filed under `unidentified`. A `429` is filed under the
caller's name, which is known by then.

## Apply operations as jobs

An apply phase reads and writes dozens of objects across OpenBao and the
cluster. That takes longer than a load balancer will hold a request open, so an
apply is started, not awaited:

```
POST /api/admin/apply/root-config
POST /api/admin/apply/team-onboarding
POST /api/admin/apply/app-onboarding
POST /api/admin/apply/eso-install
```

Each answers `202` with `{"job_id": 7, "kind": "app-onboarding", "state":
"queued"}`. Admin only, like the rest of `/api/admin`.

The body is the flags the matching CLI command takes, with one deliberate
exception: there is no `config`. The deployment config is the one this process
was started with, and a request that could name another could plan against
teams, namespaces and mounts nobody deployed. An unknown field is refused with
`422`, and a field belonging to another phase with `400` naming it, rather than
either being ignored: it was sent to do something, and dropping it quietly is
how a caller ends up believing a run was narrowed or pointed somewhere it was
not.

| Phase | Body |
| --- | --- |
| `root-config` | `jwt_discovery_url`, optionally `oidc_discovery_url` with `oidc_client_id` and `oidc_client_secret_ref`; or `teams: ["team-a"]` to narrow the run to those teams' namespaces and apply nothing at the root. |
| `team-onboarding` | `team`, `cluster`, `oidc_discovery_url`. |
| `app-onboarding` | `team`, `app`, `cluster`, optionally `k8s_namespace`, `sa_namespace`, `audience`, `server`. |
| `eso-install` | optionally `namespace` (where to install; an operator already on the cluster is reused where it is). |

Every body takes `apply`, which is `false` by default: a dry-run job whose
report is the diff against live state. Nothing is written until a caller says
`{"apply": true}`, the same rule the CLI follows.

A body that cannot produce a plan (a missing input, a team the config does not
define) is a `400` while the caller is still listening, rather than a job that
fails a second later for a reason nobody is watching for. So is a `server` that
carries credentials in the URL: an address is a location, and the refusal does
not repeat what it found.

`eso-install` reports `would install` on a dry run and `install` only when it
installed. An operator already on the cluster is `already current`, whichever
mode asked.

### One job per kind

Two applies of the same phase converge the same objects against each other's
half-written state, so one job of a kind runs at a time. A second request while
one is in flight answers `409` with the job that holds it:

```json
{"error": "an app-onboarding job is already running (job 7)", "job_id": 7, "kind": "app-onboarding"}
```

Another kind may start meanwhile. A job builds its own OpenBao client from this
deployment's address and the token in the environment, so nothing a request
carried does the writing, and it runs on its own datastore session, so the
request it came from is free to finish.

### Reading a report

```
GET /api/admin/jobs/7
GET /api/admin/jobs?kind=app-onboarding&limit=20
```

```json
{
  "id": 7,
  "kind": "app-onboarding",
  "state": "done",
  "requested_by": "admin@example.com",
  "created_at": "2026-09-22T12:47:13",
  "started_at": "2026-09-22T12:47:13.773830",
  "finished_at": "2026-09-22T12:47:13.776860",
  "elapsed_seconds": 0.003,
  "report": [
    {"surface": "openbao", "action": "create",
     "target": "[team-a] write-policy sys/policies/acl/eso-sample-readonly"},
    {"surface": "cluster", "action": "already current",
     "target": "ServiceAccount/ladon/eso-team-a-sample-a84085"}
  ],
  "error": null
}
```

`state` is `queued`, `running`, `done` or `failed`. `elapsed_seconds` is
computed by the server, so a client does not have to agree with it about the
clock.

Each report line is one operation: which half it ran against (`openbao` or
`cluster`), what it did or would do (`create`, `update`, `already current`,
`blocked`), and the object it names. An `update` carries `changes`, the fields
that differ, and a `blocked` line carries the `reason` an operator has to
resolve by hand. A line never carries a value, a policy body, or a token.

A job is `failed` when the work raised, and also when an operation was blocked:
nothing was written, which is not a success. The report is kept either way,
because what a run managed to do before it stopped is what an operator needs.
`error` is redacted on its way into the datastore, like every other stored
message.

Two runs of the same phase converge: the second reports every operation
`already current` and writes nothing.

### Approving a namespace request

`POST /api/admin/namespace-requests/<id>/approve` records the decision and
starts the run that carries it out: a `root-config` job narrowed to that one
team, which creates its namespace, the deployment's KV mount inside it, and the
policies its access level grants. Nothing at the root is touched, so approving
one team's request cannot rewrite the deployment's auth methods.

The decision is recorded and committed before the run is started, so it does
not depend on one. The request row then carries `job_id`, and a run that does
not land, or one that could not start at all because a `root-config` job was
already in flight, leaves its message in `error` while the request stays
`approved`: the decision stands, and what failed is the run.

A team the deployment config does not define is refused with `400` before
anything is decided, because the namespace, the mount and the policies all come
from that entry. A request that has already been approved or rejected is
refused with `400` too: a request is decided once, and a second decision would
silently replace the first in the record.

Jobs are threads inside the one process, and they do not survive a restart. The
rows do, so at startup every job still saying `queued` or `running` is ended as
`failed` with the error `interrupted by a restart`, and the count is logged
once. Without that, a kind would stay claimed by a thread that no longer exists
and every later job of that kind would answer `409`. A phase that was
interrupted is re-run: applying converges, so running it again is how it
finishes.

## Migrations

Migrations run at boot, from `LADON_DATABASE_URL`. SQLite is the development
target and works with a file path (`sqlite:////data/ladon.db`, four slashes for
an absolute path); PostgreSQL is the answer for anything shared
(`postgresql+psycopg://user@host:5432/ladon`). A failed upgrade is fatal: the
process exits non-zero rather than serving against a schema it cannot trust. An
absent `LADON_DATABASE_URL` is not a failure; the process warns and starts.

In an image, set `LADON_MIGRATIONS_DIR` or copy `migrations/` where the default
expects it. See the environment table.

## Errors

Every failure is one JSON object with an `error` string:

| Status | When |
| --- | --- |
| 400 | A write was refused (a placeholder value, a key named in both `values` and `remove_keys`, a merge that would empty a path, an update that changes nothing, a reserved path name), the request named another environment, an apply body could not produce a plan or named another phase's fields or an address carrying credentials, or a namespace request had already been decided. |
| 401 | No identity: no header, an assertion that did not verify, or the deciding header sent twice. On `/api/service/*`, no token, or one that is not the configured caller's. |
| 403 | Not on the allow list, not a member of the namespace, not an admin, reveal is not enabled, a machine caller is not pinned to the namespace, or a live ExternalSecret belongs to another application or namespace. |
| 404 | No such namespace request, job, apply phase, revealed key, or path, including any unmatched `/api` path. Only a route that means it answers this; anything else that goes wrong is a 500. |
| 405 | A real route, asked with the wrong method. |
| 409 | A conflict with state that changed under the request: a job of that kind is already running (the body carries its `job_id`), a cluster object another field manager owns, or a secret path that was created between the check and the write. |
| 422 | The request body does not match the route, or names a field the route does not take. The message names the field and never quotes what was sent. |
| 429 | A machine caller has spent its read or its write budget for this minute. The message says which. |
| 500 | Something broke. The body says only `internal error`; the traceback is in the log. |
| 503 | The datastore or OpenBao is not configured, or OpenBao cannot be reached. |

Messages name the namespace, path, or field at fault. They never name a value.

## Auditing

Every call records a row through the same path the agent surface uses, and a
call that did not succeed records one in its own transaction, so the events
worth investigating are not the ones that vanish. Four statuses:

| Status | Meaning |
| --- | --- |
| `ok` / `dry-run` | It happened, or it was previewed. |
| `denied` | We would not let you: the allow list, namespace membership, the reveal gate, or no usable identity. The row names which. |
| `refused` | You asked for something this deployment does not do: a placeholder value, a key named twice, a merge that would empty a path, a reserved name, another environment. |
| `error` | Something broke. |

A request that never got past identity is recorded against `unidentified`, with
the deciding header's name and whether one arrived, never what it said.
Arguments and error messages are both redacted before they are stored, so a
token quoted inside a message does not reach the table.

The agent surface writes the same rows for the same reasons: the recorder lives
in `ladon.audit`, which neither surface owns, and every tool call runs through
it. A tool call refused before it starts, or one that fails halfway, leaves a
`denied` or `error` row there exactly as it does here.

Starting an apply is audited too, as `apply_<phase>`, with the job id and the
inputs it was given. Those are recorded field by field from a named list, so a
field added to the body later is not audited by accident, and the reference to
the OIDC client secret is not recorded at all: it is a way to find a secret,
and it is not needed to know what was asked for.

## Reveal

Revealing is off unless `deployment.reveal.enabled` is true, and an instance
that says nothing about reveal answers
`403 {"error": "reveal is not enabled in this deployment"}`. A deployment that
wants the feature with friction (a re-auth, an identity-provider step-up)
supplies its own gate; the route does not change. The audit row records which
key was asked for, never what it held, and a refused attempt is recorded too.

Because the route lives at `<path>/reveal`, creating a secret whose path ends in
`/reveal` is refused: it could never be read back through its own detail route.
