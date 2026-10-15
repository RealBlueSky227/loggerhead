# Vendored Plotly

Package: `plotly.js-basic-dist-min` 4.1.2 (MIT).
Source: https://registry.npmjs.org/plotly.js-basic-dist-min/-/plotly.js-basic-dist-min-4.1.2.tgz
Package integrity: `sha512-LvfKTeVYIKzEyMj9TuH+8ot0I7IMG1fET2YxgUtbpZVHd2fBrOlBFsk26wKgnAckHyNhKG7vDk4tEyqGCYs2Aw==`
JavaScript SHA-256: `fa18ed1c94c06576c0a867bb2477534716c39e5b22affe673f2a184317974926`.

`plotly-basic-4.1.2.min.js` is the unmodified package asset. The gzip copy
is precompressed for serving without compression work on the Raspberry Pi.
`PLOTLY-LICENSE` is the original package license.

Updating: download a pinned official npm package, verify its registry integrity,
copy its basic bundle and license, regenerate gzip, and change the versioned
URL in `plots.mjs`, `web.py`, and tests. There is no npm installation or CDN
request at controller runtime. Plotly basic supports SVG scatter/step charts;
the full Plotly bundle and WebGL are unnecessary.
