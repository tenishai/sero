"""Entrypoint for ``python -m sero``: dispatch to the apply CLI."""

from __future__ import annotations

import sys

from sero.apply.cli import main

if __name__ == "__main__":
    sys.exit(main())
