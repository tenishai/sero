# The backend

One long-running process serves the API the UI calls, the built UI itself, and
(as they land) the apply operations as background jobs. It is the `web` workload
a deployment runs next to OpenBao.

It never holds a secret value. Listings and details return masked key names;
values live only in OpenBao. The one route that returns plaintext is `reveal`,
and it is refused unless a deployment turns it on.

## Running it locally

```
uv sync
LADON_DATABASE_URL=sqlite:///./ladon.db \
LADON_CONFIG=./config/example.yaml \
BAO_ADDR=https://vault.example.com \
BAO_TOKEN=<a token you already have> \
LADON_DEV_IDENTITY=you@example.com \
uv run uvicorn --factory ladon.web.app:build_default_app --port 8000
```

`uvicorn --factory` does not run migrations. Either run them yourself
(`LADON_DATABASE_URL=... uv run alembic upgrade head`) or start the process the
way an image does, which migrates first and then serves:

```
LADON_DATABASE_URL=sqlite:///./ladon.db uv run python -m ladon.web
```

The UI's dev server proxies `/api` to `http://127.0.0.1:8000`, so
`cd ui && npm run dev` gives you the full application with the backend running
beside it. To serve the built UI from the backend instead, build it once
(`cd ui && npm ci && npm run build`) and point `LADON_UI_DIST` at `ui/dist`.

## Environment

| Variable | Required | What it does |
| --- | --- | --- |
| `LADON_CONFIG` | no | Path to the deployment config (`config/example.yaml` is the template). Without it nobody holds a namespace and nobody is an admin. |
| `LADON_DATABASE_URL` | for anything but a smoke test | The datastore. Without it the routes that need one answer 503. |
| `LADON_UI_DIST` | no | Directory holding the built UI. Overrides `deployment.ui_dist`. |
| `LADON_DEV_IDENTITY` | no | The local identity hatch. Inert unless the config sets `identity.allow_dev_identity`. |
| `LADON_MIGRATIONS_DIR` | no | Where the Alembic revisions live, when an image lays the repository out differently. |
| `LADON_HOST` / `LADON_PORT` | no | What `python -m ladon.web` binds. Defaults to every interface on 8000. |
| `BAO_ADDR` or `VAULT_ADDR` | no | OpenBao address, when the config does not give one. |
| `BAO_TOKEN` or `VAULT_TOKEN` | yes, to reach OpenBao | The token the backend authenticates with. It is never read from the config file, and a config that names one is rejected. |

## Identity

There is no login form. A proxy in front of the platform authenticates the
person and passes the result in a header, named by `identity.header` and
defaulting to `x-goog-authenticated-user-email`. The value may carry an
`<issuer>:` prefix, which is stripped, and it is lowercased before anything
authorizes on it. A request to any `/api` route with no such header is answered
`401 {"error": "no identity"}`. `/healthz` is unauthenticated, because a probe
carries no identity.

### The development hatch

Set `identity.allow_dev_identity: true` in the config and `LADON_DEV_IDENTITY`
in the environment and every request with no header is attributed to that
address. The backend logs a warning at startup saying identity is unverified.
Use it on a laptop and nowhere else: with it on, anyone who can set an
environment variable is anyone. A real header still wins, so the hatch cannot
mask what a proxy said.

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

## Errors

Every failure is one JSON object with an `error` string:

| Status | When |
| --- | --- |
| 400 | A write was refused: a placeholder value, a key named in both `values` and `remove_keys`, a merge that would empty a path, an update that changes nothing. |
| 401 | No identity header. |
| 403 | Not on the allow list, not a member of the namespace, not an admin, or reveal is not enabled. |
| 404 | No such request, key, or path. |
| 422 | The request body does not match the route. |
| 503 | The datastore or OpenBao is not configured, or OpenBao cannot be reached. |

Messages name the namespace, path, or field at fault. They never name a value.

## Reveal

Revealing is off unless `deployment.reveal.enabled` is true, and an instance
that says nothing about reveal answers
`403 {"error": "reveal is not enabled in this deployment"}`. A deployment that
wants the feature with friction (a re-auth, an identity-provider step-up)
supplies its own gate; the route does not change. The audit row records which
key was revealed, never what it held.
