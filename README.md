# stlsplit

Scale, split, and add connector dowels to an STL so large prints can be broken
into print-bed-sized pieces and reassembled. See [CLAUDE.md](CLAUDE.md) for
the full project spec.

Connectors are **sockets carved into both mating pieces**, plus a separate
standalone dowel (round, D-shaped, square, or hex) meant to be printed on its
own and glued/pressed in at assembly — not a peg embedded in either piece.

## Setup

```
python -m venv .venv
```

Windows:
```
.venv\Scripts\pip install -e ".[web]"
```

macOS/Linux:
```
.venv/bin/pip install -e ".[web]"
```

Drop `[web]` if you only want the CLI (skips fastapi/uvicorn).

## CLI

```
.venv/Scripts/stlsplit input.stl --axis z --pieces 3 --out ./output/
```

or with a print-bed size (auto-fits on whichever axis is oversized):

```
.venv/Scripts/stlsplit input.stl --bed-x 220 --bed-y 220 --bed-z 250 --out ./output/
```

Run `stlsplit --help` for the full flag list. Notable options:
- `--dowel-shape {round,d,square,hex}` — connector cross-section (default round)
- `--hollow-wall <mm>` — hollow the model to this wall thickness before splitting, instead of a solid interior. Connectors are skipped wherever the resulting wall is too thin to safely carve a socket into, rather than punching through — thin-walled hollowed limbs commonly end up with no connectors at that interface.
- `--format 3mf` — a single 3MF project bundling all pieces *and* dowels, each rested flat on the plate and auto-arranged so nothing overlaps. `--format stl` (default) writes one flattened STL per piece plus a combined `_dowels.stl`.

## Web UI

Start the server:

```
.venv/Scripts/stlsplit-web
```

or equivalently:

```
.venv/Scripts/python -m stlsplit.web
```

Then open **http://127.0.0.1:8000** in a browser. Upload an STL, set your
parameters, and click "Split & preview" — it streams live progress while
splitting runs in the background, then renders the input mesh, each
resulting piece, and the generated dowels in-browser (three.js). Choose
"Separate STL files" (zip) or "Single 3MF project" (auto-arranged, no
overlaps) as the output format.

A sample file is included at [standing_figure.stl](standing_figure.stl) if
you want something to test with right away.

### Editing the auto-computed split

Setting a **Bed X/Y/Z (mm)** value seeds that axis's cut planes using the
same math as typing a value into its "Spacing (mm)" field (explicit spacing
still wins if both are set) — the resulting cuts land in the same plane
editor as manual mode, so they're fully draggable/removable/tiltable, not a
black box. Each bed field shows a pill badge (green ✓ / amber ⚠) that
conservatively checks whether every resulting piece still fits that axis's
bed dimension as you drag — conservative because it compares against the
uncut slab width between planes, which is always >= the actual piece's real
size, so it can only under-claim a fit, never falsely claim one.

**Perf note:** `compute_cut_planes`'s spacing mode (`stlsplit/geometry.py`)
retries with more pieces when the current count can't find a fully safe cut
(see the floating-region-avoidance notes in that function) — that retry cap
must stay a small constant, not scale with the mesh. An earlier version
capped it at `span // 5mm`, which for something like a 200mm-tall model
scaled to an 800mm target meant up to ~160 retries, each re-running an
expensive per-cut search over an ever-larger plane list — on a mesh where no
piece count ever finds every segment "safe" (common for organic/asymmetric
shapes), this compounded into an effectively-hung `/plane_preview` request,
which is exactly what broke the "target dimension + bed size -> auto-split"
flow this note exists to warn against regressing again. Capped at a handful
of extra attempts now; if none succeed, the largest-piece-count attempt is
returned as a best effort and `cutting.py`'s real post-cut check still
catches a genuine floating region with a clear, actionable error, same as
if the retry didn't exist at all.

**Perf note 2:** on real, complex meshes (100k+ faces) `refine_cut_planes`'s
per-cut safety search was still pathologically slow even with the retry cap
above fixed — a *single* call could take 75+ seconds, because whenever
nothing near the ideal position was safe, it fell back to checking
`safe()` (two real `mesh.split()` calls each, O(faces)) on *every* sampled
candidate. Fixed by checking candidates closest-to-ideal first with an
early exit, and capping the "just find anything safe" fallback to the top
few candidates by area instead of the whole sample set — same result
(closest-safe-candidate), far fewer expensive checks. Also trimmed the
widening stages (4 → 3) and samples per stage (15 → 11). Net effect on a
151k-face test mesh: ~75s → ~32s for the slow axis.

**Perf note 2b:** the spacing-mode retry loop above had a second bug even
after the retry cap was fixed: it kept overwriting its best-effort fallback
with the *latest* (most fragmented) attempt on every failed retry, on the
theory that more pieces is generally safer. For a mesh with a genuinely
unavoidable pinch (an appendage that only reconnects to the main body
outside the slab a cut would create — e.g. a tail or limb), more pieces
never actually fixes it, so this always burned through all 5 extra attempts
and returned the most-fragmented one: a request that should need 1-2 cuts
came back with 6+. Fixed by falling back to the *first* (smallest,
closest-to-requested-spacing) attempt instead — `cutting.py`'s real
post-cut check is still the actual safety net regardless of which attempt
is returned here.

**Perf note 2c:** `refine_cut_planes`'s candidate-safety search (see perf
note 2) is now also parallelized across candidates via a
`ProcessPoolExecutor`, for the same reason as perf note 3 below — each
candidate's connectivity check only *reads* the mesh, so a whole batch
(the closest-to-ideal candidates, then the top-K-by-area fallback) can be
checked concurrently instead of one at a time with early-exit. This
matters most exactly when the search is otherwise slowest: a mesh where
many candidates in a row are genuinely unsafe (e.g. the same
unavoidable-appendage case from 2b) degrades the old early-exit strategy
into checking almost the whole batch anyway, just serially. One executor is
created per `compute_cut_planes` call and reused across every
`refine_cut_planes`/safety-check it makes, including all of the spacing
mode's escalating retry attempts, so pool-startup cost is paid once, not
per candidate batch. Only enabled above `_PARALLEL_FACE_THRESHOLD` (20k
faces) — below that, each check is already well under a second and
spawning processes costs more than it saves; verified by measurement, not
assumption, since an early attempt at an additional optimization here (an
`initializer`-based pool that loads the mesh once per worker instead of
once per task) turned out *slower* in practice (~144s vs ~121s on a
371k-face test file) — the eager, blocking per-worker load plus its
synchronization cost more than the repeated pickling it aimed to avoid,
given how few total candidates this workload actually checks. Net effect on
that same 371k-face file (500mm target height, 250mm bed spacing, Y axis):
~206s → ~121s, same result (a single genuinely-safe cut) either way —
parallelizing changed the wall-clock time, not the outcome.

**Perf note 3:** `_cut_all_axes` (`stlsplit/pipeline.py`), used for every
multi-axis split (which is all of them from the web UI — see above), used to
process one top-level piece at a time even on the *second* and later axes,
where multiple independent pieces from the previous axis all need their own
cut + connectors. Each piece's work never touches another piece's geometry,
so it's now farmed out to a `concurrent.futures.ProcessPoolExecutor` (real
processes, not threads — the per-piece work is a mix of pure-Python/numpy
loops and C-extension calls, and the Python-level parts stay GIL-bound even
inside `asyncio.to_thread`) whenever there are 2+ pieces to process on an
axis; a single piece (always true for the very first axis cut) still runs
inline, since spawning a pool costs more than it saves for one item. Workers
exchange plain vertex/face arrays rather than `Trimesh` objects, sidestepping
whatever cached, unpicklable state (e.g. a ray-query engine) a `Trimesh` may
have picked up. A `CutPlacementError` raised inside a worker still comes back
with `axis`/`positions` filled in for the caller, same as the sequential path
(both call the same `_annotate_cut_error` helper). Progress reporting is
coarser in the parallel branch (one step per completed piece, vs. the
sequential path's per-cut/per-interface granularity) since a live callback
can't cross a process boundary — acceptable since `job.fraction` is already
described elsewhere as an estimate, not an exact count.

Separately, `/plane_preview` runs this synchronous, CPU-bound work directly
in an `async def` endpoint with no `await` points — on uvicorn's single
event loop, that blocks *everything* (every other axis's request, the SSE
job-progress stream, plain page loads) for the full duration of one axis's
computation, which is arguably worse for "does this feel broken" than the
raw compute time. Wrapped the work in `asyncio.to_thread` so the event loop
stays free; verified three concurrent axis requests actually overlap in
wall-clock time afterward instead of queuing behind whichever one runs
first. The per-axis plane editor also now shows a spinner and a
"computing…" hint while a request is in flight (`state.js`'s
`state.loading`), since a slow-but-working request looks identical to a
hung one without one.

Note this means the web UI always submits an explicit list of cut planes
(never `bed_x`/`bed_y`/`bed_z` as auto-fit parameters) — the backend's
*recursive* auto-fit algorithm (`stlsplit/autofit.py`, still used by the CLI's
`--bed-x/y/z` flags) re-checks and can cut each resulting piece differently
from its siblings, which isn't representable in a single flat, editable
plane list. The web UI's bed fields are a one-shot seed value for that
tradeoff, not the same recursive algorithm.

### Cancelling a running split

A "Cancel" button appears next to "Split & preview" while a job is running
(`POST /jobs/{job_id}/cancel`, which just sets a `threading.Event` on the
job). Cancellation is **cooperative, not instant**: `ProgressReporter.step()`
(called `stlsplit/progress.py`) is where it's actually checked, so it takes
effect at the next cut/connector-interface/piece boundary, not mid-operation
— Python has no safe way to kill a computation already running inside a
thread. On the per-piece `ProcessPoolExecutor` branch in `_cut_all_axes`
(pipeline.py, see the parallelization perf notes above) this can mean
waiting for whichever future is first to finish before the cancel is
noticed, since that's the next checkpoint — but once noticed, the pool is
torn down immediately (`cancel_futures=True`), which does stop any
still-pending worker processes rather than waiting for all of them. A
cancelled job shows a neutral "Cancelled" message (`job.status ===
'cancelled'` in `state.js`), distinct from an error.

### Saved settings

The "Saved settings" panel (top of the form) stores named presets in the
browser's `localStorage` — no account, no server round-trip, nothing
written to disk beyond the browser profile. A preset captures every setting
except the uploaded file itself: scale/target-dimension, per-axis
spacing/piece-count/bed-size, cut order, connector params, interior
hollowing, and output format. It deliberately does **not** capture the live
manual cut-plane edits from the plane editor, since those are mm positions
against one specific mesh's bounding box and wouldn't mean anything applied
to a different file. Save, load, and delete any number of named presets;
loading one overwrites the current form fields (not the currently loaded
file or in-progress job). See `stlsplit/static/js/presets.js`.

### Frontend architecture

The UI is a **build-free Vue 3 SPA** — no npm/Node.js required, ever. Vue is
loaded straight in the browser via an ES module import (`vue.esm-browser.js`,
the build that ships its own template compiler), the same pattern already
used for three.js. Components are plain JS objects with an inline
`template:` string instead of `.vue` single-file components, so there's
nothing to build: edit a file under `stlsplit/static/js/`, refresh the page.
Visual styling is **Bootstrap 5** (via CDN `<link>`, no build step either) —
`stlsplit/static/css/app.css` only covers what Bootstrap doesn't (the three.js
viewer chrome, the plane-editor gizmo swatches, a few layout specifics).

Static assets are served with `Cache-Control: no-store` (see the
`_no_cache_static` middleware in `stlsplit/web.py`) specifically so editing a
JS/CSS file always takes effect on the next reload — browsers' heuristic
caching can otherwise silently keep serving a stale module for a short
window after it changes on disk, which is exactly as confusing to debug as
it sounds.

Layout:
```
stlsplit/static/
  css/app.css              -- everything Bootstrap doesn't cover
  js/
    main.js                -- createApp(App).mount('#app')
    api.js                 -- fetch/EventSource wrappers for the backend endpoints
    state.js                -- useSplitForm(): the single reactive store (form fields,
                               per-axis cut-plane editor state, job/progress/result state)
    viewer.js               -- framework-agnostic three.js scene helper
    config.js               -- small static config mirrored from the Python side (e.g. dowel shapes)
    presets.js              -- localStorage-backed named settings presets
    components/
      App.js                -- root layout, wires the store + tab state to every section
      InputSection.js, PresetBar.js  -- always-visible, above the tabs
      TabNav.js               -- the Split/Connectors/Output/Advanced tab strip
      SplitSection.js, ConnectorsSection.js, OutputSection.js,
      AdvancedSection.js      -- one per tab (Advanced = Scale + Interior)
      AxisPlaneEditor.js     -- per-axis cut-plane list (drag position/tilt, add/remove/hide)
      MeshViewer.js           -- <mesh-viewer> component, one three.js scene per instance
      ProgressBar.js, PieceGrid.js, PieceCard.js, ResultModal.js
```

The sidebar uses a **tab strip** (`TabNav.js`, plain `v-show` per tab, no
bootstrap.js) rather than stacking every section vertically: **Split /
Connectors / Output / Advanced** (Scale + Interior). `InputSection` and
`PresetBar` sit above the tabs since they apply regardless of which tab is
open. Earlier this used per-section collapse toggles instead
(`CollapsePanel`/`CollapsibleCard`, since removed) — those hid rarely-used
fields (cut order, socket depth/clearance/count) behind an extra click even
within a section a user was already looking at; tabs remove that extra click
by scoping *whole groups* of fields to when they're relevant, and every field
within an open tab is directly visible (see the `.section-block` rules in
`app.css` for the small header-plus-rule style used to group fields within
a tab, in place of the old boxed cards). The whole sidebar is also
noticeably denser than plain Bootstrap defaults (see the `.split-form`
density rules in `app.css`) — this is a form with a lot of fields, not a
handful, so default card/input padding added up to a lot of unnecessary
scrolling.

The sidebar is a **fixed width** (380px, 420px above a 1400px viewport), not
a proportional Bootstrap column — on a very wide monitor a percentage-based
sidebar just stretches a single-column settings form with no benefit, while
the preview pane (3D viewer, piece grid) is what actually gains from extra
space. See the `.layout`/`.split-form`/`.preview-pane` rules in `app.css`.
The main editor's `<mesh-viewer>` deliberately doesn't get an explicit
`height` prop, so its CSS `clamp()` height rule (scales with viewport height
on tall screens) can apply — an inline style always wins over any stylesheet
rule regardless of specificity, so passing `height` as a prop anywhere it
matters intentionally opts out of that (piece cards, the modal).

To add a new form field or section: add it to `useSplitForm()`'s `form`
reactive object in `state.js`, add the corresponding `FormData` entry in
`buildJobFormData()`, and add the input to whichever section component
matches (or add a new section component and register it in `App.js`).

**Gotcha:** Vue 3's `v-model` auto-casts `<input type="number">` to a real
JS number once a user types into it (it starts out as the plain empty string
`""` until then) — so every numeric form field is a string-or-number union
in practice. Never call `.trim()`/string methods on a form field directly;
use `state.js`'s `strVal()` helper first. This bit us once already (a bed-size
field silently failed for any real user input, masked in testing by only
ever setting values programmatically as strings) — worth remembering before
adding the next numeric field.

Progress reporting uses **Server-Sent Events**
(`GET /jobs/{job_id}/stream`, consumed via the browser's native
`EventSource` in `api.js`'s `streamJob()`) instead of client-side polling —
the backend still just watches the same in-memory `Job` dict a background
thread updates (see `_run_job` in `stlsplit/web.py`), SSE just turns that
into one push-style connection instead of a new request every few hundred
ms. There is no more `GET /jobs/{job_id}` poll endpoint.

**Gotcha:** a plain `EventSource` auto-reconnects whenever its connection
closes, including when the *server* closes it deliberately after a terminal
message — from the browser's side that's indistinguishable from a dropped
connection, so without the client closing first, it'll silently reconnect
and re-deliver the same final message every ~3s forever. `streamJob()`
closes the connection *before* calling the caller's `onUpdate`, specifically
so an exception in that handler can never leave the socket open to loop.
Also: the web UI always submits splits as explicit `cut_planes_x/y/z` (see
above), which routes through `_cut_all_axes` in `pipeline.py` — that path
used to leave `job.fraction` `None` (a permanently indeterminate progress
bar) for every web split, single-axis included, since exact step counting
across its recursive per-axis cuts wasn't implemented. Fixed with a rough
upfront estimate (`run_pipeline` calls `progress.set_total()` before
calling `_cut_all_axes`); it doesn't need to be exact since completion
always forces `fraction` to `1.0` regardless.

## Tests

```
.venv/Scripts/python -m pytest tests/
```
