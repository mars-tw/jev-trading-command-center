# Third-party notices

## Three.js 0.186.1

The local renderer dependencies in `web/vendor/three/` are from the official
Three.js package, version 0.186.1. They are distributed under the MIT license.
The upstream copyright and complete license are preserved in
`web/vendor/three/LICENSE`; the exact file list, origin URLs and hashes are
recorded in `web/vendor/three/PROVENANCE.json`.

Upstream: https://github.com/mrdoob/three.js

## Runtime and optional tools

Python and its standard library are separately licensed and are not bundled.
The real public-market paper mode installs Requests as a separate dependency;
Requests is licensed under Apache-2.0 (https://github.com/psf/requests).
The owner-password gateway, StrategyLab viewer and ten-timeframe browser panel
add no bundled third-party runtime dependency or asset.

Native AI CLIs, Antigravity/AGY tooling, MCP servers, plugins and external model
services are not bundled or relicensed by this repository. Preparing a public
research context does not grant rights to those tools and is not proof that a
provider or model ran. Follow the terms of each program and service you enable.

Jev / TypeSafe is an external API dependency. No Jev model, model weights,
provider credential or right to the TypeSafe service is granted by this
application's MIT license. See https://docs.typesafe.ai/legal.

Public Binance market data is fetched only in explicitly enabled real research
mode. Binance data and services are not bundled, owned or relicensed here and
remain subject to their applicable terms.

## Original artwork and music

The seven original game-pet PNG atlases in `web/pets-game-v1/`, their animation
code and the procedural visual elements are included under the root MIT
license. The atlases contain complete character drawings for each facing,
walking pose, work pose and expression. Unverified legacy image layers and
local private captures were excluded.

The showcase soundtrack "Neon Circuit" is an original procedural composition
and synthesized recording. No existing recording, sample pack, loop,
soundfont or paid music-generation service was used. The music source,
provenance and its MIT license accompany the release. NumPy, Python and
FFmpeg are external production tools and retain their own licenses; their
binaries are not redistributed here.

## OrbitControls for the v0.3 view

`web/orbit-controls.js` and `web/vendor/three/examples/jsm/controls/OrbitControls.js` contain Three.js 0.186.1 OrbitControls under the same MIT license. The upstream source SHA-256 is `3d79d07ecb686b4e5d93232eedab255331c1beef711e13164eaa1f68655a5f2b`; delivery adds the preserved Three.js MIT header. The vendor inventory records both the source and delivered hashes.
