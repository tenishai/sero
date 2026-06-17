# Skills

Ladon uses Claude Code skills to keep work consistent with the project's
conventions. There are two kinds: marketplace skills (installed per machine, not
committed) and project-local skills (committed to this repo).

## Project-local skills (committed)

These live in `.agents/skills/` and are exposed to Claude Code through symlinks
in `.claude/skills/`. Committing them means every contributor and agent picks
them up from a clean clone, with no extra install step.

| Skill | Source | Use for |
| --- | --- | --- |
| `web-design-guidelines` | Vercel ([vercel-labs/web-interface-guidelines](https://github.com/vercel-labs/web-interface-guidelines)) | Reviewing the secrets UI and admin surface against Web Interface Guidelines (accessibility, UX, best practices). |

Layout:

```
.agents/skills/web-design-guidelines/SKILL.md   # the real file, tracked in git
.claude/skills/web-design-guidelines            # symlink -> ../../.agents/skills/web-design-guidelines
```

To add another project-local skill, create it under `.agents/skills/<name>/` and
add a matching symlink under `.claude/skills/<name>`.

## Marketplace skills (installed per machine)

These come from the Anthropic plugin marketplace and Vercel, and are installed
locally via `/plugin`. They are not committed. Install these before doing the
work each one covers:

| Skill | Use for |
| --- | --- |
| `doc-coauthoring` | All docs, specs, and ADRs. |
| `claude-api` | Any code that calls the Claude API; read before any LLM, model, or pricing answer. |
| `mcp-builder` | Any MCP server (for example, exposing OpenBao operations as MCP tools). |
| `skill-creator` | Authoring this repo's own skills. |
| `webapp-testing` | Playwright checks of the UI. |
| `frontend-design` | Building or reshaping the secrets UI and admin surface. |
| `web-artifacts-builder` | Multi-component UI artifacts. |

The project-local `web-design-guidelines` skill above is the committed
counterpart used for UI review.
