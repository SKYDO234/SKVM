# Bundled third-party assets

These files are copied into `static/vendor` by `.scripts/build_vendor.py`.
SKVM loads **no** remote scripts, fonts or stylesheets at runtime.

| Library | Version | License |
| --- | --- | --- |
| xterm.js | 5.5.0 | MIT |
| xterm-addon-fit | 0.10.0 | MIT |
| xterm-addon-web-links | 0.11.0 | MIT |
| qrcode | 1.5.4 | MIT |

The `qrcode` core is bundled with a miniature CommonJS loader that lives in the
build script; only the encoder is included, SKVM draws the matrix itself.
