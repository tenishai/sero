#!/usr/bin/env python3
"""Fail when an installed Python package carries a licence this project cannot ship.

Reads the metadata of every package in the interpreter that runs it, so the
release runs it inside the built image and checks exactly what ships:

    docker run --rm -i --entrypoint python <image> - < scripts/check_licences.py

A package with no licence in its metadata fails too: add it to KNOWN with the
licence its repository states, and say where you read it.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata

# Strong copyleft and source-available licences. LGPL and MPL are fine to depend on.
REFUSED = re.compile(
    r"(?<!lesser )(?<!library )(?<!l)\b(a?gpl|general public|affero)|sspl|server side public|"
    r"busl|business source|commons clause",
    re.IGNORECASE,
)

# name -> licence, for packages whose metadata does not say.
KNOWN: dict[str, str] = {}


def licence_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    found = [meta.get("License-Expression") or ""]
    classifiers = meta.get_all("Classifier") or []
    found += [c.split("::")[-1].strip() for c in classifiers if c.startswith("License ::")]
    text = (meta.get("License") or "").strip()
    if text and len(text) < 120:  # longer is a pasted licence text, not a name
        found.append(text)
    return "; ".join(dict.fromkeys(f for f in found if f and f != "OSI Approved"))


def main() -> int:
    refused, unknown, seen = [], [], 0
    for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
        name = dist.metadata["Name"]
        if not name:
            continue
        seen += 1
        licence = licence_of(dist) or KNOWN.get(name.lower(), "")
        if not licence:
            unknown.append(f"{name} {dist.version}")
        elif all(REFUSED.search(choice) for choice in re.split(r"\s+OR\s+", licence)):
            # "A OR B" lets us pick: refused only when every choice is.
            refused.append(f"{name} {dist.version}: {licence}")
    for line in refused:
        print(f"refused: {line}")
    for line in unknown:
        print(f"no licence in metadata: {line}")
    if refused or unknown:
        return 1
    print(f"licences: {seen} packages, none refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
