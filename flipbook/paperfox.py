"""PAPERFOX: a flipbook story rendered through the Klein Studio API (http://localhost:7860).

Character anchors are generated first; every shot is then an edit with the previous frame and the anchors as
references, so characters and look carry from frame to frame. Resumable: frames already on disk are skipped.

    ../.venv/bin/python paperfox.py          # render all frames
    ../.venv/bin/python paperfox.py --movie  # only rebuild the movie from the frames
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

API = "http://localhost:7860"
HERE = Path(__file__).parent / "paperfox"
ANCHORS, FRAMES = HERE / "anchors", HERE / "frames"
OUTPUTS = Path(__file__).parent.parent / "outputs"
W, H = 1344, 768

LOOK = ("Cinematic film still of a handcrafted miniature set, photographed with a macro lens, shallow depth of field, "
        "moody teal-and-amber color grade, soft film grain.")

FOX = ("a small origami fox folded from one sheet of vermilion red washi paper with faint gold flecks, crisp sharp "
       "creases, a pointed snout, two triangular ears and a long folded tail; the reverse side of the paper is cream "
       "white and shows inside the ears and at the tip of the tail")
CRANE = "an origami crane folded from deep indigo paper printed with tiny white stars"
GIRL = ("a girl of about eight with a short black bob, round tortoiseshell glasses, a bright yellow raincoat and "
        "yellow rubber boots")

ANCHOR_SHOTS = {
    "fox": f"Studio product photograph of {FOX}, standing in three-quarter view on a plain warm grey paper backdrop, "
           "soft even light, the whole fox sharp and fully visible.",
    "crane": f"Studio product photograph of {CRANE}, wings spread, standing on a plain warm grey paper backdrop, "
             "soft even light, the whole crane sharp and fully visible.",
    "girl": f"Full-body studio photograph of {GIRL}, standing and smiling shyly, holding a clear umbrella, plain warm "
            "grey backdrop, soft even light.",
}

# Each shot: (instruction, cast). The previous frame is always the first reference unless the shot says "new";
# cast members follow as further references. {prev}, {fox}, {crane}, {girl} become "the first image", ...
SCENES = [
    ("Closing time", [
        ("new", f"{LOOK} Night exterior of a tiny old paper shop on a narrow rainy street, warm light glowing in its "
                "small shop window, a hand-painted wooden sign reading \"KAMI\" above the door, neon reflections "
                "rippling in puddles on the cobblestones.", []),
        ("", "Keep {prev} as the canvas. Push the camera closer to the shop window; the warm light inside switches "
             "off, so the window is lit only by cold blue moonlight and the rain.", []),
        ("new", f"{LOOK} Inside the dark paper shop at night: a wooden counter where one flat, unfolded square sheet "
                "of vermilion red washi paper with gold flecks (the same paper as the fox in {fox}, but completely "
                "flat) lies beside a cutting mat, moonlight through a rain-streaked window, paper cranes hanging on "
                "threads in the dim background.", ["fox"]),
        ("", "Keep {prev} exactly. One corner of the red sheet lifts by itself, and a faint golden glow runs along "
             "its edge.", []),
    ]),
    ("The fold", [
        ("", "Keep {prev} as the canvas. The red sheet folds itself in half along the diagonal, hovering a finger's "
             "width above the counter; thin golden light shines from the fresh creases.", []),
        ("", "Keep {prev} as the canvas. The hovering red paper folds further on its own into an abstract, "
             "half-finished shape of stacked triangular flaps, not yet an animal; two small points start to lift "
             "where ears will be, gold light in every crease. No hands or people anywhere.", []),
        ("", "Keep {prev} as the canvas. The self-folding paper is now roughly fox-shaped: a blocky body, a "
             "pointed snout and two ears, but the tail is still a long flat unfolded strip of paper curling in the "
             "air, the golden glow at its brightest. Nothing touches it; no hands or people anywhere.", []),
        ("", "Keep {prev} as the canvas. The fold is complete: the origami fox from {fox} stands on the counter, the "
             "golden glow fading into the paper.", ["fox"]),
        ("", "Close-up of the origami fox from {fox} in the setting of {prev}: it tilts its head curiously, "
             "moonlight catching the crisp crease along its snout.", ["fox"]),
    ]),
    ("Into the shop", [
        ("", "In the dark paper shop of {prev}, the origami fox from {fox} leaps off the counter, caught mid-air "
             "above a row of glass ink bottles.", ["fox"]),
        ("", "The origami fox from {fox} lands on the shop floor among rolls of paper and looks up at the paper "
             "cranes hanging on threads above it, the dark shop of {prev} around it.", ["fox"]),
        ("", "Low angle in the shop of {prev}: {crane_desc} hangs on a thread among the cranes; it unfurls its wings "
             "on its own and the thread snaps loose. The crane looks exactly like {crane}.", ["crane"]),
        ("", "The crane from {crane} lands on the floor face to face with the origami fox from {fox}, both lit by "
             "moonlight in the shop of {prev}.", ["fox", "crane"]),
    ]),
    ("The window", [
        ("", "The origami fox from {fox} and the crane from {crane} stand on the shop's windowsill behind "
             "rain-streaked glass, the blurred lights of the city beyond; the window is open a small gap. Same dark "
             "moonlit mood as {prev}.", ["fox", "crane"]),
        ("", "Keep the setting of {prev}. The fox from {fox} squeezes through the gap onto the outside ledge, "
             "raindrops as big as its head falling around it.", ["fox"]),
        ("", "Close-up on the outside ledge from {prev}: a fat raindrop lands on the ear of the fox from {fox} and "
             "darkens a spot of the red paper; the fox flinches.", ["fox"]),
    ]),
    ("Rooftops", [
        ("", "Low-angle shot: the origami fox from {fox} runs across wet clay roof tiles at night, neon signs "
             "glowing pink and teal below, light rain. Same look as {prev}.", ["fox"]),
        ("", "Keep the rooftops of {prev}. The fox from {fox} leaps across a narrow gap between two roofs, a deep "
             "alley far below.", ["fox"]),
        ("", "Keep the rooftops of {prev}. The fox from {fox} lands but slips on a wet tile and slides toward the "
             "roof edge, paws scrabbling.", ["fox"]),
        ("", "Keep the rooftops of {prev}. The crane from {crane} swoops in low with wings spread and the fox from "
             "{fox} leaps onto its back.", ["fox", "crane"]),
        ("", "The crane from {crane} carries the fox from {fox} on its back off the roof edge of {prev}, the "
             "alley and its neon far below.", ["fox", "crane"]),
    ]),
    ("Flight", [
        ("new", f"{LOOK} Wide aerial shot: tiny {CRANE} with {FOX} riding on its back flies over a miniature night "
                "city of tiled roofs and hundreds of glowing paper lanterns, the rain has stopped and clouds are "
                "parting. They match {fox} and {crane}.", ["fox", "crane"]),
        ("", "Tracking close-up of the scene in {prev}: the fox from {fox} rides on the back of the crane from "
             "{crane}, its ears pressed back by the wind, lanterns blurred below.", ["fox", "crane"]),
        ("", "The crane from {crane} with the fox from {fox} on its back flies past a giant illuminated clock tower "
             "face, its golden hands at midnight; same city and look as {prev}.", ["fox", "crane"]),
        ("", "Above the clouds: the crane from {crane} glides with the fox from {fox} on its back toward a huge "
             "full moon, the fox gazing at it; same look as {prev}.", ["fox", "crane"]),
        ("", "Keep {prev}. Below them, through gaps in the clouds, hundreds of sky lanterns rise from the city and "
             "float up around the crane and the fox.", ["fox", "crane"]),
    ]),
    ("Storm", [
        ("", "The sky turns: dark storm clouds lit from inside by lightning surround the crane from {crane} and the "
             "fox from {fox}; the crane's wings bend in the gusts. Continue from {prev}.", ["fox", "crane"]),
        ("", "Keep {prev}. Heavy rain hits; the crane's paper wings darken, soak and sag, the fox from {fox} clings "
             "on.", ["fox", "crane"]),
        ("", "Keep the storm of {prev}. The crane from {crane} and the fox from {fox} tumble downward through the "
             "rain toward the street lights below.", ["fox", "crane"]),
        ("", "Street level at night, rain: the fox from {fox} and the crane from {crane} hit a puddle on the "
             "cobblestones with a big crown-shaped splash; same storm as {prev}.", ["fox", "crane"]),
        ("", "Close-up after {prev}: the fox from {fox} lies half-soaked in the puddle, its creases gone soft and "
             "limp, a red traffic light reflected in the water; the crumpled crane from {crane} lies beside it.",
             ["fox", "crane"]),
    ]),
    ("Found", [
        ("", "Very low angle at the puddle of {prev}: a small yellow rubber boot of the girl from {girl} steps up "
             "and stops right at the puddle's edge, rain bouncing off it.", ["girl"]),
        ("", "On the rainy night street of {prev}: the girl from {girl} crouches under her clear umbrella and looks "
             "down at the soaked fox and crane in the puddle, curious.", ["girl"]),
        ("", "Close-up: the small hands of the girl from {girl} gently scoop the soggy fox from {fox} and the "
             "crumpled crane out of the puddle; same rainy night as {prev}.", ["girl", "fox"]),
        ("new", f"{LOOK} A child's cozy bedroom at night: on a desk under a warm lamp, the soggy red origami fox "
                "and the soaked indigo star crane lie drying on a folded tissue, a cup of cocoa and colored pencils "
                "nearby. They match {fox} and {crane}.", ["fox", "crane"]),
        ("", "Keep the desk of {prev}. The girl from {girl}, now in pajamas, has fallen asleep at the desk with her "
             "head on her arms, next to the drying fox and crane.", ["girl"]),
    ]),
    ("Dawn", [
        ("", "Keep {prev}. Soft golden sunrise light now falls through the window onto the desk; the fox from "
             "{fox} is dry again, its creases crisp, and it stands up with a faint golden glow.", ["fox"]),
        ("", "Keep {prev}. The crane from {crane} beside the fox spreads its dried, restored wings in the morning "
             "light.", ["crane"]),
        ("", "Keep {prev}. The fox from {fox} gently nudges the sleeping girl's hand with its paper snout.", ["fox"]),
        ("", "Eye-level close-up at the desk of {prev}: the girl (her face, bob and glasses as in {girl}, but wearing "
             "the striped pajamas from {prev}, no raincoat) lifts her head, wakes and smiles at the fox from {fox} "
             "standing right in front of her face.", ["girl", "fox"]),
        ("", "Wide shot from inside the room of {prev}: at the windowsill at sunrise, the girl (face, bob and glasses "
             "as in {girl}, still in the striped pajamas from {prev}, no raincoat), the fox from {fox} and the crane "
             "from {crane} look out over the rooftops of the city; next to the fox sits a "
             "second, slightly clumsy little origami fox the girl folded herself from a paper flyer.",
             ["girl", "fox", "crane"]),
    ]),
    ("Title", [
        ("new", f"{LOOK} Title card: the word \"PAPERFOX\" folded from vermilion red washi paper letters with crisp "
                "creases, standing on a warm grey paper backdrop, the origami fox from {fox} sitting beside the "
                "last letter, soft morning light.", ["fox"]),
    ]),
]

ORD = ["the first image", "the second image", "the third image", "the fourth image", "the fifth image"]


def run_job(client, prompt, refs, seed):
    files = [("refs", (p.name, p.read_bytes(), "image/png")) for p in refs]
    params = {"task": "edit" if refs else "create", "prompt": prompt, "upsample": True, "mode": "distilled",
              "width": W, "height": H, "seed": seed, "count": 1}
    job = client.post(f"{API}/api/jobs", data={"params": json.dumps(params)}, files=files or None).raise_for_status().json()
    while job["state"] not in ("done", "error"):
        time.sleep(3)
        job = client.get(f"{API}/api/jobs/{job['id']}").json()
    if job["state"] == "error":
        raise RuntimeError(job["error"])
    return job["results"][0]


def wait_for_server(client):
    for _ in range(120):
        try:
            if client.get(f"{API}/api/status").json()["loaded"]["pup"]:
                return
        except Exception:
            pass
        time.sleep(10)
    raise RuntimeError("studio not ready")


def render():
    ANCHORS.mkdir(parents=True, exist_ok=True)
    FRAMES.mkdir(parents=True, exist_ok=True)
    manifest_path = HERE / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    client = httpx.Client(timeout=120)
    wait_for_server(client)

    def keep(result, dest, key, info):
        shutil.copy(OUTPUTS / f"{result['id']}.png", dest)
        manifest[key] = {**info, "caption": result["caption"], "seed": result["seed"], "gallery_id": result["id"]}
        manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))

    for i, (name, prompt) in enumerate(ANCHOR_SHOTS.items()):
        dest = ANCHORS / f"{name}.png"
        if not dest.exists():
            print(f"anchor {name} …", flush=True)
            keep(run_job(client, prompt, [], 500 + i), dest, f"anchor_{name}", {"prompt": prompt})

    n, prev = 0, None
    for s, (title, shots) in enumerate(SCENES, 1):
        for k, (flag, text, cast) in enumerate(shots, 1):
            n += 1
            dest = FRAMES / f"{n:03d}_s{s:02d}_{k}.png"
            refs = ([] if flag == "new" or prev is None else [prev]) + [ANCHORS / f"{c}.png" for c in cast]
            if dest.exists():
                prev = dest
                continue
            names = ({"prev": ORD[0]} if refs and refs[0] == prev else {}) | {
                c: ORD[refs.index(ANCHORS / f"{c}.png")] for c in cast}
            prompt = text.replace("{crane_desc}", CRANE)
            for key, ordinal in names.items():
                prompt = prompt.replace("{" + key + "}", ordinal)
            assert "{" not in prompt, f"unresolved placeholder in shot {n}: {prompt}"
            t0 = time.time()
            print(f"[{n:02d}] {title} {k}: {len(refs)} refs …", end=" ", flush=True)
            for attempt in range(3):
                try:
                    result = run_job(client, prompt, refs, 1000 + n)
                    break
                except Exception as e:
                    print(f"retry ({e})", end=" ", flush=True)
                    time.sleep(20)
            else:
                raise RuntimeError(f"frame {n} failed")
            keep(result, dest, dest.stem, {"scene": title, "prompt": prompt, "refs": [r.name for r in refs]})
            print(f"{time.time() - t0:.0f}s", flush=True)
            prev = dest
    print("all frames done", flush=True)


def movie(hold=1.8, fade=0.5, fps=30):
    """Each frame as a slow push-in (alternating direction), joined with crossfades."""
    frames = sorted(FRAMES.glob("*.png"))
    clips = HERE / "clips"
    clips.mkdir(exist_ok=True)
    dur = hold + fade
    n = int(dur * fps)
    for i, f in enumerate(frames):
        out = clips / f"{f.stem}.mp4"
        if out.exists():
            continue
        zoom = f"1+0.06*on/{n}" if i % 2 == 0 else f"1.06-0.06*on/{n}"
        vf = (f"scale={W * 2}:{H * 2},zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={n}:s={W}x{H}:fps={fps},"
              "format=yuv420p")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(f), "-vf", vf, "-frames:v", str(n),
                        "-c:v", "libx264", "-crf", "18", "-preset", "medium", str(out)], check=True)
    clip_files = [clips / f"{f.stem}.mp4" for f in frames]
    inputs, chain, last = [], [], "[0:v]"
    for c in clip_files:
        inputs += ["-i", str(c)]
    for i in range(1, len(clip_files)):
        label = f"[v{i}]"
        chain.append(f"{last}[{i}:v]xfade=transition=fade:duration={fade}:offset={i * hold:.3f}{label}")
        last = label
    script = HERE / "xfade.txt"
    script.write_text(";\n".join(chain) if chain else "[0:v]null[v0]")
    out = HERE / "paperfox.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex_script", str(script), "-map", last,
                    "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p", str(out)], check=True)
    print(f"movie: {out}", flush=True)


if __name__ == "__main__":
    if "--movie" not in sys.argv:
        render()
    movie()
