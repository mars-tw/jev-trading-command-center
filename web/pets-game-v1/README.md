# JEV game companion art v1

Seven original, transparent PNG atlases: `codex.png`, `claude.png`, `grok.png`, `agy.png`, `jev.png`, `muse.png`, and `qwen.png`.

Each atlas is **1536 × 2304 RGBA**, arranged as four columns and six rows of **384 × 384** complete character drawings. Crops never reconstruct limbs, flip a front view into a side view, or add procedural work props.

| Row | Column 0 | Column 1 | Column 2 | Column 3 |
|---|---|---|---|---|
| 0 · idle | front | right | back | left |
| 1 · walk A | front | right | back | left |
| 2 · walk B | front | right | back | left |
| 3 · work A | front | right | back | left |
| 4 · work B | front | right | back | left |
| 5 · expressions | pleased, front | focused, front | concerned, front | tired, front |

`LineWorkers.makeTextureCanvas(alias, view, frame, working)` retains a stable 192 × 192 canvas for each normalized signature. Patrol frames map to rows `[1, 0, 2, 0]`, working frames to `[3, 4, 3, 4]`. SYSTEM, JEV, and unknown aliases use `jev.png`. Artwork support for MUSE/QWEN does not create live workers.

The runtime exposes `artStatus()`, `artRevision()`, `whenArtReady()`, `onArtChange(callback)`, and `makeExpressionCanvas(alias, expression)`. Images validate dimensions before cropping. Loading and failed assets remain transparent. A successful asynchronous load redraws the same canvas objects, invalidates SVG data URLs, and refreshes Three textures even while motion is paused. A frozen scene is recomposited from its base before drawing the updated sprites, without advancing its positions or animation frames. Work images are selected only from the existing truthful live/demo working flags; they never alter backend state.

Open [the interactive gallery](preview.html) to inspect playback, all four facings, work art, expressions, and every 24-cell source sheet. The gallery explicitly previews artwork; it does not represent live execution.

Focused acceptance: `node tests/test_game_pet_art.cjs`. Existing status, task identity, motion/pause, and label checks: `node tests/test_line_workers.cjs`.

Chromium acceptance: `node tests/test_game_pet_art_browser.cjs web <receipt-directory>`. It requires an installed `playwright` package, or its module path in `JEV_PLAYWRIGHT_MODULE`. This test uses its own random loopback HTTP port and isolated browser, serves synthetic API responses, and intercepts the original guild endpoint; it never calls a live trading service. It delays one atlas deliberately, then checks actual transparent PNG pixels, distinct facings/work/expressions, stable canvas identity, SVG URL refresh, Three texture-version updates while all motion is paused, and gallery controls at desktop/mobile widths. The self-hosted `preview.js` works with the existing script CSP; no policy relaxation is needed.
