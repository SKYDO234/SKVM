#!/usr/bin/env python3
"""Keep the three icon tables in sync.

``skvm/routes/icons.py::PATHS`` is the single source of truth.  This script
regenerates

  * ``templates/_icons.html`` — the Jinja macro used by every template
  * ``static/js/skvm.js``     — the ``ICONS`` table used at runtime

Run it after adding an icon:  ``python3 .scripts/sync_icons.py``
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ICONS_PY = ROOT / "skvm" / "routes" / "icons.py"
ICONS_HTML = ROOT / "templates" / "_icons.html"
SKVM_JS = ROOT / "static" / "js" / "skvm.js"

ENTRY_RE = re.compile(r'^    "([A-Za-z0-9_]+)": \'(.+)\',$')


def read_paths() -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in ICONS_PY.read_text().splitlines():
        match = ENTRY_RE.match(line)
        if match:
            entries.append((match.group(1), match.group(2)))
    if not entries:
        sys.exit("no icon entries parsed from skvm/routes/icons.py")
    return entries


def rewrite(text: str, start_marker: str, end_marker: str, body: str, path: pathlib.Path) -> str:
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker, start)
    updated = text[:start] + "\n" + body + "\n" + text[end:]
    if updated == text:
        return text
    return updated


def main() -> None:
    entries = read_paths()

    html_body = "\n".join(f"    '{name}': '{path}'," for name, path in entries)
    html_body = html_body.rstrip(",")
    html = ICONS_HTML.read_text()
    html = rewrite(html, "{%- set paths = {", "  } -%}", html_body, ICONS_HTML)
    ICONS_HTML.write_text(html)

    js_body = "\n".join(f"    {name}: '{path}'," for name, path in entries)
    js_body = js_body.rstrip(",")
    js = SKVM_JS.read_text()
    start = js.index("  const ICONS = {") + len("  const ICONS = {")
    end = js.index("  };", start)
    js = js[:start] + "\n" + js_body + "\n" + js[end:]
    SKVM_JS.write_text(js)

    print(f"synced {len(entries)} icons -> templates/_icons.html, static/js/skvm.js")


if __name__ == "__main__":
    main()
