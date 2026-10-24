# ground.py: finding places and objects in the room photo

Status: built and measured 2026-10-18. Nothing is public: the module only talks to the local VLM (vLLM, port 8002) and
SAM on this machine's GPU. Its single HTTP route lives in the main app (7860). It has **not** been hooked into `server.py`
yet; see "Integration".

## What was built

| File | Purpose |
|---|---|
| `ground.py` | `locate()`, `find_object()`, geometry check (`surface_at`, `check_place`, `nearest_wall`), SAM refinement (`refine`), grid overlay, router `POST /api/workshop/{pid}/locate` |
| `tests/test_ground.py` | 20 unit tests: fake VLM, synthetic geometry (§2.1 fixture), fake SAM masks, router on a tmp store |
| `tools/eval_ground.py` | live accuracy and latency check on the eval set (local VLM + SAM only) |
| `evalset/ground.json` | 85 hand-written grounding cases (42 place, 43 object) on 14 FLUX rooms |
| `evalset/rooms/*.png` | 14 FLUX rooms: the 8 demo rooms (living and dining rooms) plus 6 generated for this eval (kitchen, attic bedroom with a sloped ceiling, hallway, bathroom, nearly empty living room, child's room). No customer photos. |
| `evalset/rooms/*.geometry.json` | `POST /api/geometry/analyze` result for each room (analysed from an upload copy) |
| `evalset/ground_results.json` | raw per-case results of the two eval runs below |

### Contract

All points are `[x, y]` and all boxes are `[top, left, bottom, right]`, both on the 0–1000 grid over the photo.
Note that `W3D.toGrid` returns `[y, x]`, so swap the order before you feed a point into code that uses that order.

```python
ground.locate(photo, phrase, geo=None, doc=None, method="native") -> {
    "status": "ok" | "not_found" | "unavailable" | "error",
    "point": [x, y] | None, "wall": "w0" | None, "surface": "floor" | "wall" | None,
    "confidence": 0..1, "why": "rechts neben dem Fernsehschrank, Rückwand (Wand hinten)",
    "reference": "...", "method": "native", "seconds": 8.3}

ground.find_object(photo, phrase, refine_with_sam=True, sam_prompt="box") -> {
    "status": "ok" | "not_found" | "unavailable" | "error",
    "point": [x, y], "box": [t, l, b, r],              # VLM frame
    "parts": [{"poly": [[x, y], ...], "box": [t, l, b, r]}],   # SAM outline (segment.outline format) or the frame
    "refined": bool, "iou": SAM-mask-vs-VLM-frame IoU | None, "label": "...", "why": "...",
    "seconds": 11.4, "timing": {"vlm": 10.8, "sam": 0.3}}
```

- `photo` can be a PIL image, a path or raw bytes. `geo` is a geometry dict (`doc["geometry"]`), and `doc` is the order,
  which is only used for its `removes`.
- `status` `not_found` is an extra value beyond the SPEC: the VLM says the reference ("neben dem Kamin") is not in the
  photo. The agent should ask back instead of placing something.
- Neither function raises. A stopped VLM returns `unavailable`, a garbled answer is re-asked once (temperature 0.3) and
  then returns `error`. An unknown `method` is a programming error and raises `ValueError`.
- `parts` from `find_object` has the same format as `POST /api/geometry/segment`. It can go straight into
  `{"op": "set", "path": "removes", ...}` (`projects._remove` accepts `{"poly", "box"}`).

### How locate works

1. **VLM (default `native`)**: Qwen3.8 gets the photo (≤ 1024 px JPEG via `judge.data_url`) and a short system prompt
   about carpenter location phrases (links/rechts = in the photo, "neben" = beside not on, "wo X steht" = on X, …).
   It returns JSON (schema-constrained): `reference_de` first (a short chain of thought), `found`, `surface`
   (floor/wall), and `point_2d` normalised to 0–1000. Qwen's native coordinate space is 0–1000, so no conversion is needed.
2. **Replacement phrases**: if the phrase says "wo … steht", "anstelle", "statt" or "an Stelle" and the point lies in
   (or within 20 units of) a region already marked in `doc.removes`, the point moves to the middle of that region.
3. **Geometry check** (`check_place`): one ray through the point is intersected with the floor plane, every wall plane
   (only within its `from`/`to` extent ± 50 cm and between floor and ceiling, as W3D `covers`/`wallHit` do) and the
   ceiling. The nearest hit is what the photo shows.
   - Wall: `wall` = that wall's id.
   - Floor: `wall` = the nearest covering wall within 60 cm (the piece will be snapped against it), or else `None`.
   - Ceiling: the point slides down onto the wall below it, and confidence drops by 0.2.
   - No surface hit (outside a window, beyond every detected wall): confidence drops by 0.25.
   - The VLM's surface claim disagrees with the geometry: confidence drops by 0.1. The penalty is deliberately mild,
     because the middle of a standing cabinet seen in a photo is often the floor strip just in front of the wall.
4. **Confidence**: starts at 0.85 for `native`, 0.8 for `native_short`, 0.7 for `grid` and 0.75 for `grid2`, minus the
   penalties above.

### How find_object works

1. The VLM returns `label_de`, `found`, `bbox_2d` and `point_2d` (a point *on* the object). A point outside the frame
   is replaced by the frame's centre.
2. **SAM 2.1 with a box prompt**: the VLM frame plus the point go to SAM through `segment._load()` under `segment._lock`.
   Out of SAM's three candidate masks, the one whose own tight box best matches the VLM frame (IoU) wins. The outline
   comes from `segment.outline` (grown by 1.2 % as for the eraser).
3. If SAM is unavailable (less than 18 GB free before loading, or any exception), or its best mask has IoU < 0.4 with the
   frame, `parts` is the VLM rectangle and `refined` is false.

## How to use it

```python
import ground
from PIL import Image
img = Image.open("evalset/rooms/20261018-193206-1992693316.png")
ground.locate(img, "rechts neben dem Fernsehschrank", geo)   # geo = doc["geometry"]
ground.find_object(img, "die alte Kommode")
```

HTTP (after integration): `POST /api/workshop/{pid}/locate {"phrase": "...", "kind": "place"|"object"}` runs on the
order's room photo and geometry. The call goes through `workshop.EXCLUSIVE`, so it never runs alongside a FLUX image.
Errors (German): 400 "Bitte beschreiben, was gesucht wird.", 400 "Unbekannte Art – „place“ oder „object“.", 400 "Der
Auftrag hat noch kein Raumfoto.", 404 for an unknown order.

Agent flow (`agent.py`): for every understood piece with a `where` phrase, call `locate(photo, piece["where"], geo, doc)`.
On `ok`, pass `point` to `place.placement_for(doc, piece, point, against_wall=bool(res["wall"]))`. On `not_found`, or on
`confidence < 0.5`, ask back. For removals, call `find_object(photo, remove["what"])` and append `parts[0]` (or all
parts) to `removes`.

Eval:

```
.venv/bin/python tools/eval_ground.py --methods native,grid,grid2 --sam box,point,none --out report.json -v
.venv/bin/python -m pytest -q tests/test_ground.py
```

## Test and eval results (real numbers)

**Unit tests:** `.venv/bin/python -m pytest -q tests/test_ground.py`: **20 passed** (≈ 1 s, no GPU and no network).

**Eval set:** 85 cases on 14 FLUX rooms. The expected regions were drawn by hand after looking at each image; every box
was rendered onto its photo and checked visually. Place cases: "links neben …", "zwischen den Fenstern", "unter dem
Fenster", "in der hinteren Ecke", "wo die alte Kommode steht", "Mitte des Raums", "unter der Dachschräge", "am Ende des
Flurs", … Object cases: furniture, radiators, sockets, a coat hook, a roof window, a mirror, groups ("die Sitzgruppe").

**Metrics:**
- Place: the point lies inside the expected region (*strict*), or within ±25 grid units of it (*near*; a hand-drawn
  region is not exact).
- Object: the VLM point lies on the object; the VLM box has IoU ≥ 0.5 with the expected frame; the final outline's box
  (after SAM) has IoU ≥ 0.5.

### Place phrases (42 cases)

| Method | Strict hit | Near (±25) | Geometry surface = expected | Latency (mean / p50 / max) |
|---|---|---|---|---|
| **native** (run 1) | **36/42 = 86 %** | **40/42 = 95 %** | 27/35 = 77 % | 8.3 / 8.5 / 10.1 s |
| **native** (run 2) | **38/42 = 90 %** | **40/42 = 95 %** | 28/35 = 80 % | 10.2 / 10.5 / 14.0 s |
| native_short (no reference text) | 35/42 = 83 % | 40/42 = 95 % | 24/35 = 69 % | 6.1 / 6.2 / 10.1 s |
| grid (12 × 8 labelled cells) | 31/42 = 74 % | 37/42 = 88 % | 25/35 = 71 % | 4.8 / 4.5 / 6.8 s |
| grid2 (grid + 6 × 6 zoomed refinement) | 34/42 = 81 % | 38/42 = 90 % | 26/35 = 74 % | 9.4 / 9.2 / 11.3 s |

**Native pointing wins and is the default.** Run-to-run variance at temperature 0 is about ±2 cases (batching on the
shared server). The grid overlay is quicker because a cell label is only a few output tokens, but it is clearly less
accurate. Its typical failure is picking a cell on the floor in front of the target. `native_short` saves about 4 s,
but loses about 1–3 strict hits and the `why` text. It stays available for latency-critical paths.

Native misses:
- g57 "unter der Dachschräge": the attic room; the point landed on the back wall or on the floor left of the chest.
- g71 "zwischen Waschbecken und Toilette": it pointed at the floor in front of the gap, below the expected wall region.

The near-only cases (g25, g53, g65, g77) are all points on the floor strip directly in front of the intended wall. In each
of them the geometry still attaches the correct wall (`w0`), so snapping places the piece against the right wall.

The "surface agrees" column is informational. Most disagreements are the same floor-in-front-of-the-wall pattern:
the expectation said "wall" and the geometry says "floor" with the right nearest wall.

### Object phrases (43 cases)

| Refinement | VLM point on object | VLM box IoU ≥ 0.5 | Final outline IoU ≥ 0.5 | Mean IoU VLM / final | Latency (mean; VLM part / SAM part) |
|---|---|---|---|---|---|
| **SAM box prompt** (default) | 42/43 = 98 % | 40/43 = 93 % | **42/43 = 98 %** | 0.791 / **0.829** | 11.4 s; 10.8 s / 0.58 s (p50 0.29 s, first load 12.9 s) |
| SAM point prompt (`segment.masks_at`) | 42/43 = 98 % | 40/43 = 93 % | 39/43 = 91 % | 0.791 / 0.797 | 12.6 s; 12.3 s / 0.29 s |
| none (VLM frame only) | 42/43 = 98 % | 40/43 = 93 % | 40/43 = 93 % | 0.790 / 0.790 | 13.4 s (VLM only) |

The SAM box prompt repairs loose VLM frames, for example on the wash basin (0.39 → 0.59) and the coat hook (0.43 → 0.51).
The single remaining failure is g44 "die Steckdose an der Rückwand", a socket of about 30 px in a 1536 px photo, where
the VLM picked another spot (IoU 0.11). Small fixtures far from the camera are the weak spot.

### Latency notes

All timings were measured while another agent used the same vLLM server (2 concurrent requests during most of the
run). On an idle server, a single native pointing request took about 5 s (first probe). Decoding the output tokens
dominates: a 27B FP8 model on the Spark decodes about 10 tokens/s. That is why `native_short` (≈ 15 output tokens) is
faster than `native` (≈ 40–50 tokens with the reference text). SAM adds about 0.3 s per object once loaded, and about
12 s on its first load in a process.

Live router check (TestClient app with only `ground.router`, tmp project store, the real VLM and SAM):
- place "rechts neben dem Fernsehschrank" → 200, point [600, 625], wall w0, 14.1 s (busy server).
- object "das graue Sofa" → 200, refined (IoU 0.86), 31.9 s including the 12.4 s SAM first load.

## Known limits

- **Latency is 6–14 s per phrase** on the shared server. A multi-piece order with removals takes 20–40 s of grounding.
  The agent should run grounding in its worker thread, which the SPEC already plans.
- **Sloped ceilings (Dachschräge) are not modelled** by `geometry.py`: it fits only vertical walls, so "unter der
  Dachschräge" is grounded by the VLM alone and was the weakest place case.
- **Tiny objects** (sockets, switches) are found unreliably (1 of 2 sockets).
- **Plane geometry ignores furniture**: a point on a sofa reads as the floor or wall behind it. The check only
  classifies the visible room shell. It does not test for occlusion.
- **Ambiguous phrases** ("in der Ecke" with several corners) get the model's single best guess. There is no
  multiple-choice yet. The confidence does not reflect ambiguity, because Qwen's own certainty is not calibrated.
- **Mirrors and reflections**: in the bathroom the model correctly picked the mirror and not the window reflected in it,
  but that is one case.
- The eval rooms are FLUX images. Real phone photos (noise, clutter, wide-angle distortion) will probably score lower.
  Re-measure with carpenter samples once `evalset/audio` exists. Use test photos only, never customer photos.
- `refine()` uses `segment._load()` and `segment._lock` (private names in `segment.py`) for the box prompt, because
  `segment.masks_at` only takes points. If `segment.py` changes, a public `masks_in_box()` there would be cleaner.

## Housekeeping

The geometry analysis needed upload copies. These were created by this module's build and contain FLUX rooms only:
`outputs/uploads/20261018-{50c0a2d889,2d87748168,97166eca3b,2f23c0fc52,e1bdcdd4ac,99f24fd753,468c8819b6,139ffe214f,
c87486b8b8,e8c1acc9d1,7d57988f1b,aacb34ff8d,7edcad221e,d8cef724f1}.png`, plus their caches
`outputs/geometry/<same stem>.json / -depth.npy / -depth.jpg`. They are kept so the rooms can be re-analysed or
calibrated; they may be deleted at any time, since `evalset/rooms/*.geometry.json` holds the results. The 6 new rooms
are also in `outputs/` as normal studio results (label "evalset"):
`20261018-211134-2056750832`, `-211158-435550560`, `-211222-135330405`, `-211254-1354562784`, `-211325-958375101`,
`-211422-149525771`. Their prompts are in `evalset/ground.json` → `rooms.<name>.prompt`.

## Integration

Only `server.py` needs changes. No other shared file is touched.

1. Import, next to the other module imports (alphabetical, after `import geometry_api`):

```python
import ground
```

2. Output path, next to the existing path wiring (after the `geometry.GEO_DIR, workshop.OUT = ...` line):

```python
ground.OUT = OUT
```

3. Router: add `ground.router` to the include loop:

```python
for r in (projects.router, team.router, live.router, backup.router, geometry_api.router, workshop.router, judge.router,
          jobs.router, delivery.router, public.router, ground.router):
    app.include_router(r)
```

The route `POST /api/workshop/{pid}/locate` sits behind the team middleware like every other `/api/workshop/` route.
It needs no `team.py` change. It reads `workshop.EXCLUSIVE` at call time, so the existing `workshop.configure(...,
exclusive=...)` call in the server also covers grounding.

Frontend (later, not built here): a "Wo?" text or voice field could call the route and then mark the returned point,
like a tap in the Platzieren step (`point` is `[x, y]` on the 0–1000 grid; pixel = `point * size / 1000`).
