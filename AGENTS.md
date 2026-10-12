# AGENTS.md — sero

Instructions for AI coding agents working in this repo. Claude Code loads this via
`CLAUDE.md` (`@AGENTS.md`); other AGENTS.md-aware tools read it directly.

Start with `ARCHITECTURE.md`: what this repo is, its entry points, and where its seams are.

## Architecture cards

`ARCHITECTURE.md` is the repo card: what this repo is, its entry points, and an index of its seams. Each seam that has a card lists it under `SEAMS → card:`, pointing at `docs/arch/<seam>.md`. Read the card before changing the code it describes.

Every card pins the file it describes with an `anchor: <path> sha=<git blob sha>` line in its frontmatter. When you change an anchored file, update the card in the same change: fix the text, set `updated:`, and replace the sha with `git hash-object <path>`. To add a seam card, add its line to the `SEAMS → card:` index too.

Check before you push:

```
python3 scripts/check_arch_cards.py
```

It fails when an anchored file has changed without its card, or when a card and the index disagree.
