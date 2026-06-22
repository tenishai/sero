"""Entrypoint for ``python -m ladon``: dispatch to the apply CLI."""

from __future__ import annotations

import sys

from ladon.apply.cli import main

if __name__ == "__main__":
    sys.exit(main())
