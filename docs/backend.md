# The backend

One long-running process serves the API the UI calls, the built UI itself, and
(as they land) the apply operations as background jobs. It is the `web` workload
a deployment runs next to OpenBao.

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
uv run python -m ladon.web
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
uv run uvicorn --factory ladon.web.app:build_default_app --port 8000
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

**A signed assertion**, configured under `identity.assertion`:

```yaml
identity:
  assertion:
    header: x-goog-iap-jwt-assertion
    jwks_url: https://proxy.example.com/keys
    audience: /projects/<project-number>/apps/<app-id>
    issuer: https://proxy.example.com
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
| 400 | A write was refused (a placeholder value, a key named in both `values` and `remove_keys`, a merge that would empty a path, an update that changes nothing, a reserved path name) or the request named another environment. |
| 401 | No identity: no header, an assertion that did not verify, or the deciding header sent twice. |
| 403 | Not on the allow list, not a member of the namespace, not an admin, or reveal is not enabled. |
| 404 | No such namespace request, revealed key, or path, including any unmatched `/api` path. Only a route that means it answers this; anything else that goes wrong is a 500. |
| 405 | A real route, asked with the wrong method. |
| 422 | The request body does not match the route. The message names the field and never quotes what was sent. |
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

## Reveal

Revealing is off unless `deployment.reveal.enabled` is true, and an instance
that says nothing about reveal answers
`403 {"error": "reveal is not enabled in this deployment"}`. A deployment that
wants the feature with friction (a re-auth, an identity-provider step-up)
supplies its own gate; the route does not change. The audit row records which
key was asked for, never what it held, and a refused attempt is recorded too.

Because the route lives at `<path>/reveal`, creating a secret whose path ends in
`/reveal` is refused: it could never be read back through its own detail route.
