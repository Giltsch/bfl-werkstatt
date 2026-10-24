# place.py: server-side placement (Phase 2)

## What it is

`place.py` is an exact Python port of the W3D math (`static/werkstatt3d.js`) that a tap in step ③ uses, plus the bit of
`static/ws/editor.js` (`sceneFor`, `reprojected`) and `static/ws/step-platzieren.js` (tap → `onChange` → `commit`) that
turns a tap into the stored `placement`. With it the agent can place a piece from an image point and store exactly what
the UI would store, with no browser involved.

It has no dependencies beyond the stdlib. `workshop` is imported lazily, and only when no catalog is passed. It writes
nothing to disk and has no routes.

Files:
- `place.py`: the module.
- `tests/test_place.py`: 15 tests (13 pure Python and 2 node cross-checks).
- `tests/place_xcheck.mjs`: the node harness. It loads the real `werkstatt3d.js` and `editor.js` and fires the tap through
  `W3D.attach` on a fake canvas.
- `tests/fixtures/place_geometries.json`: 10 geometry dicts. One is the TEST demo order `80be15109b36` (read-only copy of
  its `geometry`). Eight are the FLUX rooms in `evalset/rooms/`, analysed with DepthPro into the scratchpad (no photos
  copied). The last is the demo room after `geometry.rescale(…, 1.12)`, so a calibrated scene is covered.

## API

```python
import place

pl = place.placement_for(doc, piece_or_id, point_grid, against_wall=None, yaw=None, catalog=None)
# point_grid = [top, left] on the 0–1000 grid (the same grid as boxes / ground.locate)
# → {"mode": "3d", "pose": {"x", "z", "yaw", "wall"}, "box": [t, l, b, r]}  (+ "elev": int for wall-hung pieces)
# → None when the UI would ignore the tap (no room/measurement, above the horizon, box not visible)

ops = place.tap_ops(doc, piece_or_id, pl)          # exactly what commit() PATCHes
doc = projects.apply_ops(pid, doc["rev"], ops, who)
```

Parameters of `placement_for`:
- `against_wall=None`: the plain tap, identical to the UI. Floor pieces snap when the back is within 30 cm of a wall. If
  `piece.elev > 0`, the piece hangs on the wall the tap ray meets (`hang`) and the result carries the new `elev`. If the
  tap meets no wall, it falls back to the floor search, like `W3D.place`.
- `against_wall=True`: the tap, followed by "An die Wand" (`toWall`: nearest wall + `snapToWall`). Returns None if no
  wall fits or the snapped box is not visible.
- `against_wall=False`: never snaps. Gives the free floor pose from `centreAt` (a wall-hung piece floats at its elev).
- `yaw=None`: keeps the piece's current turn (`old.yaw || 0`), as the UI does. A number overrides it. When the piece
  snaps, the wall's angle wins.
- Extension beyond the UI: a wall-hung catalog type (Wandregal, Hängeschrank) with `elev` None (for example fresh from
  `intent.py`) gets the catalog elevation that step ② would set. That elevation is then also returned as `elev`, and
  `tap_ops` stores it.
- The returned `elev` is not part of the placement: `tap_ops` moves it to `pieces.<id>.elev`. `projects._piece` drops
  unknown keys anyway.

`tap_ops` returns ops in the UI's order: `set placement`, then `set elev` (only when it changed), then `set w/h/d` from
the catalog dims (rounded) for each dimension the piece does not have yet.

Lower-level calls, with the same names as JS in snake_case: `place.init(geometry, catalog) -> Scene | None`,
`place.scene_for(doc)`, `Scene.floor_point / to_image / box_corners / project_box / nearest_wall / snap_to_wall /
centre_at / wall_hit / hang / place / distance / relations / dims / elev`, `place.reprojected`, `place.to_grid`,
`place.from_grid`, `place.box_to_grid`, `place.hull_of`, `place.frame_of`, `place.scale_poses`, `place.js_round`.

How calibration scale is handled: the geometry dict already has the scale baked in, because `geometry.rescale` scales
`floor.d`, wall offsets and wall from/to. So a scene is built from the stored geometry with no extra factor, as in JS.
`scale_poses(pieces, k)` mirrors `W3D.scalePoses` for adopting a new calibration (`k = new.scale / old.scale`).

## Fidelity

The port follows the JS expression by expression, so both sides produce the same IEEE results:
- the same operation order;
- `unit` multiplies by `1 / hypot` rather than dividing;
- `Math.round` becomes `floor(x + .5)`, never Python's banker's `round`;
- JS unary plus on piece fields (`"120"` counts, `""`, `null` and `"x"` fall back);
- the dedupe and sort order of `hullOf`;
- the Newton loop of `centreAt`, including its 0.02·H step-down above the horizon and the 3 % acceptance.

## Test and eval results (measured 2026-10-18)

`.venv/bin/python -m pytest -q tests/test_place.py`: **15 passed in 2.0 s** (node v24.14.0).

- **Primitives cross-check.** 1,440 cases: 10 geometries, 6 pieces and 144 grid points, split across the pieces. Each
  case compares `floorPoint, centreAt, wallHit, hang, place, toImage, projectBox (corners/hull/box/visible),
  nearestWall, snapToWall (auto + by id), distance, relations, dims, elev, toGrid`. Results: 77,495 floats compared,
  **max relative error 5.1e-11**; **5,760/5,760 grid box values identical**; every null/non-null decision agrees.
- **Tap cross-check, through the real `W3D.attach` and `editor.reprojected`.** 12,480 taps: 10 geometries × 7 piece
  kinds × 144 points, plus a `against_wall=True` subset. The piece kinds are sideboard, Einbauschrank with catalog dims,
  table, typeless with default dims, Wandregal with elev 119, Hängeschrank with elev 40, and a turned bed that keeps its
  yaw. Results: 7,384 taps placed (3,956 snapped to a wall, 2,217 hung); 48,861 floats compared, **max relative error
  5.0e-11**; **54,560/54,560 box/op values identical**; the "ignored" (None) decisions all agree. `tap_ops` equals the
  harness's `commit()` ops in every case.
- **Mutation check.** Changing SNAP_CM 30→31, the Newton stop threshold 0.25→0.5, or `js_round` → `round` makes the
  cross-checks fail.
- **Evalset analysis.** All 8 FLUX rooms analysed `ok`, confidence `high`, with 2–3 walls each and ceilings of
  273–310 cm. DepthPro took 1.5 s per room once loaded and was unloaded afterwards.

The node tests are skipped when no `node` is found. They look on PATH, then in `~/.nvm/versions/node/*/bin`.

## Known limits

- A tap on an existing piece's box or rotate handle starts a drag in the UI, not a placement. `placement_for` always
  places, which is what the agent wants. The harness flags such taps; in the test set none occurred.
- The 2D fallback is not ported. Without a measurement (`geometry.ok` false or no room) the result is None, and the caller
  must fall back to a 2D frame itself (`{"mode": "2d", "pose": None, "box": [...]}`).
- `relations` returns the JS (camelCase) keys of `W3D.relations`, not the snake_case form in `geometry.relations`.
- Drag, turn (`turn`, `snapYaw`), measure and the drawing code are not ported. A tap does not need them.
- The point is the box *centre* in the photo (the `centreAt` semantics). A "where" phrase from `ground.locate` should
  therefore point at the middle of where the piece should appear, not at its floor contact.

## Integration

No shared files need to change: `place.py` is a library with no router. To use it in `agent.py` / `ground.py`:

```python
import place, projects

doc = projects.load("workshop", pid)
pl = place.placement_for(doc, piece_id, point_grid, against_wall=None)   # point_grid = [top, left] 0–1000
if pl:
    doc = projects.apply_ops(pid, doc["rev"], place.tap_ops(doc, piece_id, pl), who)
else:
    pass  # no measurement / unusable point: ask again, or set a 2D frame
```

For a preview image, `place.init(doc["geometry"], workshop.CATALOG).project_box(piece)` gives the corner pixels and hull
for drawing outlines, matching the UI overlay.
