#!/usr/bin/env python3
"""
Vendor asset builder for SKVM.

SKVM ships with **zero runtime CDN dependencies** — every front-end library is
copied / bundled into ``static/vendor`` at build time by this script so the panel
also works on an air-gapped VPS.

Usage
-----
    python3 .scripts/build_vendor.py --nodemods /tmp/vendor

The ``--nodemods`` directory is expected to contain *extracted* npm tarballs
(``npm pack <pkg>`` + ``tar xzf``).  We deliberately do not run a JS toolchain
for this: the qrcode core is bundled with a ~60 line mini-bundler below, which
keeps the build reproducible with nothing but CPython.

What gets produced
------------------
    static/vendor/xterm/xterm.js, xterm.css
    static/vendor/xterm/addon-fit.js, addon-web-links.js
    static/vendor/qrcode/qrcode.bundle.js      (UMD global: SKQR)
    static/vendor/VENDOR.md                    (versions + licenses)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "vendor"

# qrcode's core needs this module for "optimal segment" splitting.  SKVM always
# encodes a single byte-mode segment, so a throwing stub is safe and keeps the
# bundle tiny (no network, no dijkstrajs dependency).
STUBS = {
    "dijkstrajs": (
        "module.exports = { find_path: function () { "
        "throw new Error('SKVM vendor bundle: dijkstrajs is not bundled'); } };\n"
    ),
}


def find_pkg(nodemods: Path, *names: str) -> Path:
    """Locate an extracted npm package directory (``<name>-<version>/package``)."""
    for name in names:
        for candidate in sorted(nodemods.glob(f"{name}*/package")):
            if (candidate / "package.json").is_file():
                return candidate
    raise SystemExit(f"[vendor] could not find extracted package for {names} under {nodemods}")


def pkg_version(pkg: Path) -> str:
    return json.loads((pkg / "package.json").read_text())["version"]


def copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    print(f"[vendor] {dst.relative_to(ROOT)}  ({dst.stat().st_size // 1024} KiB)")


# --------------------------------------------------------------------------- #
# Mini CommonJS bundler
# --------------------------------------------------------------------------- #
REQUIRE_RE = re.compile(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)")


def bundle_core(src_dir: Path, entries: list[str], global_name: str, header: str) -> str:
    """Bundle a tiny CommonJS graph into one UMD file with a global export."""
    modules: dict[str, tuple[str, str]] = {}  # id -> (dir, source)

    def module_id(path: Path) -> str:
        return "./" + path.relative_to(src_dir).as_posix()

    def collect(path: Path) -> str:
        mid = module_id(path)
        if mid in modules:
            return mid
        src = path.read_text(encoding="utf-8")
        modules[mid] = (path.parent.relative_to(src_dir).as_posix(), src)
        for spec in REQUIRE_RE.findall(src):
            if spec in STUBS or not spec.startswith("."):
                continue
            target = (path.parent / spec)
            for cand in (target.with_suffix(".js"), target / "index.js", Path(f"{target}.js")):
                if cand.is_file():
                    collect(cand.resolve())
                    break
            else:
                raise SystemExit(f"[vendor] unresolved require '{spec}' from {path}")
        return mid

    entry_ids = [collect((src_dir / e).resolve()) for e in entries]

    chunks = [
        "/*! " + header + " */",
        "(function (root, factory) {",
        "  if (typeof module === 'object' && module.exports) { module.exports = factory(); }",
        f"  else {{ root.{global_name} = factory(); }}",
        "})(typeof self !== 'undefined' ? self : this, function () {",
        "  'use strict';",
        "  var __mods = {}, __cache = {};",
        "  function __require(id) {",
        "    if (__cache[id]) { return __cache[id].exports; }",
        "    var m = { exports: {} };",
        "    __cache[id] = m;",
        "    __mods[id](m, m.exports, __require);",
        "    return m.exports;",
        "  }",
    ]

    def emit(mid: str, dirname: str, code: str) -> None:
        def _resolve(match: re.Match) -> str:
            spec = match.group(1)
            if spec in STUBS or not spec.startswith("."):
                return f"__require({json.dumps(spec)})"
            target = os.path.normpath(os.path.join(dirname, spec)).replace(os.sep, "/")
            cands = [f"./{target}.js", f"./{target}/index.js"]
            for c in cands:
                if c in modules:
                    return f"__require({json.dumps(c)})"
            raise SystemExit(f"[vendor] cannot resolve '{spec}' inside {mid}")

        code = REQUIRE_RE.sub(_resolve, code)
        # SKVM is a browser bundle; keep module scope intact.
        chunks.append(f"  __mods[{json.dumps(mid)}] = function (module, exports, require) {{")
        chunks.append(code)
        chunks.append("  };")

    for spec, stub in STUBS.items():
        chunks.append(f"  __mods[{json.dumps(spec)}] = function (module, exports, require) {{")
        chunks.append(stub)
        chunks.append("  };")

    for mid in sorted(modules):
        dirname, code = modules[mid]
        emit(mid, dirname, code)

    if len(entry_ids) == 1:
        chunks.append(f"  return __require({json.dumps(entry_ids[0])});")
    else:
        chunks.append("  return {")
        for eid, name in zip(entry_ids, entries):
            key = Path(name).stem
            chunks.append(f"    {json.dumps(key)}: __require({json.dumps(eid)}),")
        chunks.append("  };")
    chunks.append("});")
    chunks.append("")
    return "\n".join(chunks)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodemods", default="/tmp/vendor", help="dir with extracted npm tarballs")
    args = ap.parse_args()
    nodemods = Path(args.nodemods)
    if not nodemods.is_dir():
        raise SystemExit(f"[vendor] {nodemods} is not a directory")

    licenses: list[tuple[str, str, str]] = []

    # ---- xterm.js -------------------------------------------------------- #
    xterm = find_pkg(nodemods, "xterm-xterm", "xterm")
    copy(xterm / "lib" / "xterm.js", OUT / "xterm" / "xterm.js")
    copy(xterm / "css" / "xterm.css", OUT / "xterm" / "xterm.css")
    copy(xterm / "lib" / "xterm.js.map", OUT / "xterm" / "xterm.js.map")
    licenses.append(("xterm.js", pkg_version(xterm), str(xterm / "LICENSE")))

    fit = find_pkg(nodemods, "xterm-addon-fit")
    copy(fit / "lib" / "addon-fit.js", OUT / "xterm" / "addon-fit.js")
    licenses.append(("xterm-addon-fit", pkg_version(fit), str(fit / "LICENSE")))

    links = find_pkg(nodemods, "xterm-addon-web-links")
    copy(links / "lib" / "addon-web-links.js", OUT / "xterm" / "addon-web-links.js")
    licenses.append(("xterm-addon-web-links", pkg_version(links), str(links / "LICENSE")))

    # ---- qrcode (bundled core, canvas renderer is SKVM's own) ------------- #
    qr = find_pkg(nodemods, "qrcode")
    core = qr / "lib" / "core"
    entries = sorted(p.name for p in core.glob("*.js") if p.name in {
        "qrcode.js", "bit-matrix.js", "segments.js", "byte-data.js", "utils.js",
    })
    header = f"qrcode {pkg_version(qr)} core (bundled for SKVM) — MIT © Ryan Day"
    bundle = bundle_core(core, ["qrcode.js"], "SKQR", header)
    Out_bundle = OUT / "qrcode" / "qrcode.bundle.js"
    Out_bundle.parent.mkdir(parents=True, exist_ok=True)
    Out_bundle.write_text(bundle, encoding="utf-8")
    print(f"[vendor] {Out_bundle.relative_to(ROOT)}  ({Out_bundle.stat().st_size // 1024} KiB, {len(bundle.splitlines())} lines)")
    licenses.append(("qrcode", pkg_version(qr), str(qr / "license")))
    shutil.copyfile(qr / "license", OUT / "qrcode" / "LICENSE")

    # ---- manifest -------------------------------------------------------- #
    lines = [
        "# Bundled third-party assets",
        "",
        "These files are copied into `static/vendor` by `.scripts/build_vendor.py`.",
        "SKVM loads **no** remote scripts, fonts or stylesheets at runtime.",
        "",
        "| Library | Version | License |",
        "| --- | --- | --- |",
    ]
    for name, ver, lic in licenses:
        lic_name = "MIT"
        try:
            text = Path(lic).read_text(errors="ignore")
            if "Apache License" in text:
                lic_name = "Apache-2.0"
            elif "GNU GENERAL PUBLIC" in text:
                lic_name = "GPL"
        except OSError:
            pass
        lines.append(f"| {name} | {ver} | {lic_name} |")
    lines += [
        "",
        "The `qrcode` core is bundled with a miniature CommonJS loader that lives in the",
        "build script; only the encoder is included, SKVM draws the matrix itself.",
        "",
    ]
    (OUT / "VENDOR.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[vendor] {OUT.relative_to(ROOT)}/VENDOR.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
