# Skills

Sero uses Claude Code skills to keep work consistent with the project's
conventions. They are installed per machine and not committed: this repository
ships only its own work.

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

For UI review, Vercel's `web-design-guidelines` skill checks the secrets UI and admin
surface against the Web Interface Guidelines (accessibility, UX, best practices):

```
npx skills add https://github.com/vercel-labs/agent-skills --skill web-design-guidelines --global
```
