"""Speech to text for the Werkstatt: the carpenter talks instead of typing ("links neben dem Fenster ein Einbauschrank,
zwei-zwanzig breit, raumhoch, Weißlack"). Whisper large-v3-turbo (MIT, ~1.6 GB, from the local HF cache) runs on CUDA
in fp16; it loads on first use and unloads itself after IDLE_UNLOAD seconds so it never sits on memory the FLUX models
need. Audio of any common container (webm/ogg opus from browsers, m4a from iPhones, wav, mp3) is decoded by ffmpeg.
Never raises into the caller: transcribe() reports "error" (unreadable audio) or "unavailable" (no model / no memory).

Also the evaluation-sample recorder (static/aufnahme.html → POST /api/voice/sample → evalset/audio/<id>.*)."""

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zlib
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

import judge

REPO = os.environ.get("FLUX_VOICE_MODEL", "openai/whisper-large-v3-turbo")
RATE = 16000
WINDOW = 30.0          # Whisper's receptive field; longer audio is cut into windows of at most this length
SEEK = 6.0             # a window ends at the quietest moment of its last SEEK seconds, not in the middle of a word
BATCH = 8              # windows per generate() call (a 2-minute memo is one batch of 4-5)
MAX_SECONDS = 600      # longer recordings are cut off (a voice memo for one order is well below a minute)
MAX_AUDIO = 25 * 2**20     # upload bytes: 10 min of opus are ~1 MB, of 16-bit wav ~19 MB
MAX_PHOTO = 40 * 2**20
SILENCE_RMS = 0.002    # ~ -54 dBFS: windows whose loud frames stay below this are skipped (Whisper invents text on silence)
IDLE_UNLOAD = 600
LOAD_HEADROOM_GB = 3   # the model needs ~1.6 GB plus activations; loading must not push free memory below judge.MIN_FREE_GB
MEMORY_WAIT = 30
FFMPEG_TIMEOUT = 60
DATA = Path(os.environ.get("FLUX_DATA") or Path(__file__).parent)
SAMPLES = DATA / "evalset" / "audio"

# The prompt is decoder context that looks like earlier transcript: it nudges spelling ("Weißlack", "grifflos", "Korpus")
# and number style (digits, which intent.normalize_de reads best) without forcing words that were not said.
PROMPT_DE = ("Einbauschrank links neben dem Fenster, 2,20 m breit, raumhoch, 60 tief, Korpus und Fronten in Weißlack, "
             "grifflos mit Push-to-open. Sideboard in Eiche geölt mit Schubkästen, Sockelblende, Fachböden, Dachschräge, "
             "Wandboard, Garderobe. Der alte Schrank kommt weg.")

# Phrases Whisper is known to produce from silence, music or noise (subtitle credits from its training data)
HALLUCINATIONS = re.compile(
    r"untertitel|amara\.org|vielen dank (fürs|für's|für das) zuschauen|copyright|swr \d|zdf|wdr|"
    r"bis zum nächsten mal|abonniert|thanks for watching|субтитры", re.I)

_lock = threading.Lock()      # one load / inference at a time; the GPU is shared with FLUX anyway
_model = _proc = None
_last_use = 0.0
_load_seconds = None
_watcher = None
_sleep = time.sleep           # tests skip the memory wait

router = APIRouter()


# ---------------------------------------------------------------- audio

def ffmpeg_ok() -> bool:
    return shutil.which("ffmpeg") is not None


def decode(data: bytes, max_seconds: float = MAX_SECONDS) -> np.ndarray:
    """Any audio container → float32 mono 16 kHz in [-1, 1]. Raises ValueError for empty or unreadable input.
    Goes through a temp file, not a pipe: iPhone m4a keeps its index at the end, which ffmpeg cannot read from stdin."""
    if not data:
        raise ValueError("empty audio")
    # Only real audio containers, with the demuxer forced and no protocols but "file": left to its probing, ffmpeg would
    # also accept text playlists (ffconcat, HLS) and read other files on this machine into the "recording".
    fmt = DEMUXER.get(_audio_ext(data) or "")
    if fmt is None:
        raise ValueError("unknown audio container")
    with tempfile.TemporaryDirectory(prefix="voice-") as tmp:       # private (0700), not next to other /tmp files
        path = Path(tmp) / "input.audio"
        path.write_bytes(data)
        try:
            p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                                "-protocol_whitelist", "file", "-f", fmt, "-i", str(path), "-vn",
                                "-t", str(max_seconds), "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"],
                               capture_output=True, timeout=FFMPEG_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ValueError(f"ffmpeg failed: {e}") from None
    if p.returncode != 0 or not p.stdout:
        raise ValueError("unreadable audio: " + p.stderr.decode(errors="replace").strip()[-300:])
    audio = np.frombuffer(p.stdout[: len(p.stdout) // 4 * 4], dtype="<f4").astype(np.float32)
    return np.clip(np.nan_to_num(audio), -1.0, 1.0)


def _frame_rms(audio: np.ndarray, frame: int = 480) -> np.ndarray:
    n = len(audio) // frame
    if n == 0:
        return np.array([float(np.sqrt(np.mean(audio ** 2)))]) if len(audio) else np.zeros(1)
    return np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1))


def silent(audio: np.ndarray) -> bool:
    """True when even the loud frames (95th percentile of 30 ms RMS) stay below SILENCE_RMS."""
    return len(audio) == 0 or float(np.percentile(_frame_rms(audio), 95)) < SILENCE_RMS


def windows(audio: np.ndarray, window: float = WINDOW, seek: float = SEEK) -> list[tuple[int, int]]:
    """[(start, end)] sample ranges of at most `window` seconds covering the audio. Each cut lands on the quietest
    100 ms of the window's last `seek` seconds, so words are not split between two windows."""
    n, w, s, hop = len(audio), int(window * RATE), int(seek * RATE), RATE // 10
    out, start = [], 0
    while n - start > w:
        lo, hi = start + w - s, start + w
        seg = audio[lo:hi]
        k = len(seg) // hop
        energy = np.sqrt(np.mean(seg[: k * hop].reshape(k, hop) ** 2, axis=1))
        cut = lo + int(np.argmin(energy)) * hop + hop // 2 if k else hi
        cut = min(max(cut, start + RATE), hi)
        out.append((start, cut))
        start = cut
    if n - start > 0:
        out.append((start, n))
    return out


def looping(text: str) -> bool:
    """Whisper's own hallucination test: text that gzip compresses more than 2.4x is a repetition loop."""
    b = (text or "").encode()
    return len(b) > 40 and len(b) / len(zlib.compress(b)) > 2.4


_REPEAT = re.compile(r"(\S.{2,80}?)(?:[\s,.;]+\1){2,}", re.S)


def clean(text: str, prompt: str | None = None) -> str:
    """Drops known silence hallucinations and an echoed prompt, collapses a phrase repeated 3+ times in a row,
    normalises whitespace."""
    text = re.sub(r"\s+", " ", text or "").strip()
    text = _REPEAT.sub(r"\1", text).strip()
    if not text.strip(" .,!?-") or HALLUCINATIONS.search(text) and len(text) < 80:
        return ""
    if prompt and len(text.strip(" .")) >= 20 and text.strip(" .") in prompt:
        return ""   # the decoder repeated a sentence of its context instead of hearing anything (a single word may be real)
    return text


# ---------------------------------------------------------------- model

def loaded() -> bool:
    return _model is not None


def _ensure_watcher() -> None:
    global _watcher
    if _watcher is None or not _watcher.is_alive():
        _watcher = threading.Thread(target=_idle_loop, daemon=True, name="voice-idle")
        _watcher.start()


def idle_check(now: float | None = None) -> bool:
    """Unloads the model when it has not been used for IDLE_UNLOAD seconds and nothing is running; True if unloaded."""
    now = time.time() if now is None else now
    if _model is None or now - _last_use <= IDLE_UNLOAD or not _lock.acquire(blocking=False):
        return False
    try:
        if _model is None or now - _last_use <= IDLE_UNLOAD:
            return False
        _drop()
        return True
    finally:
        _lock.release()


def _idle_loop() -> None:
    while _model is not None:
        time.sleep(30)
        idle_check()


def _load():
    """Called with _lock held. Raises RuntimeError("memory"/"cuda") when the model cannot be loaded now."""
    global _model, _proc, _load_seconds, _last_use
    if _model is not None:
        return _model, _proc
    waited = 0.0
    while judge.mem_available_gb() < judge.MIN_FREE_GB + LOAD_HEADROOM_GB:
        if waited >= MEMORY_WAIT:
            raise RuntimeError("memory")
        _sleep(5)
        waited += 5
    import torch
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
    if not torch.cuda.is_available():
        raise RuntimeError("cuda")
    t0 = time.time()
    proc = AutoProcessor.from_pretrained(REPO, local_files_only=True)
    model = AutoModelForSpeechSeq2Seq.from_pretrained(REPO, dtype=torch.float16, local_files_only=True,
                                                      low_cpu_mem_usage=True).to("cuda").eval()
    _model, _proc, _load_seconds = model, proc, round(time.time() - t0, 1)
    _last_use = time.time()
    _ensure_watcher()
    return _model, _proc


def _drop() -> None:
    global _model, _proc
    _model = _proc = None
    try:
        import gc

        import torch
        gc.collect()
        torch.cuda.empty_cache()
    except Exception:
        pass


def unload() -> None:
    with _lock:
        _drop()


def _prompt_ids(proc, prompt: str):
    import torch
    return torch.tensor(proc.get_prompt_ids(prompt), device="cuda")


def _strip_prompt(text: str, prompt: str | None) -> str:
    # some transformers versions return the prompt in front of the transcript
    if prompt and text.lstrip().startswith(prompt.strip()):
        return text.lstrip()[len(prompt.strip()):]
    return text


def _decode_batch(model, proc, chunks: list[np.ndarray], lang: str | None, prompt: str | None) -> list[str]:
    import torch
    out = []
    for i in range(0, len(chunks), BATCH):
        part = chunks[i:i + BATCH]
        feats = proc.feature_extractor(part, sampling_rate=RATE, return_tensors="pt").input_features
        kw = {"task": "transcribe", "max_new_tokens": 440 - (len(proc.get_prompt_ids(prompt)) if prompt else 0)}
        if lang:
            kw["language"] = lang
        if prompt:
            kw["prompt_ids"] = _prompt_ids(proc, prompt)
        with torch.inference_mode():
            seq = model.generate(feats.to("cuda", torch.float16), **kw)
        out += [_strip_prompt(t, prompt) for t in proc.batch_decode(seq, skip_special_tokens=True)]
    return out


def _generate(chunks: list[np.ndarray], lang: str | None, prompt: str | None) -> list[str]:
    """Called with _lock held: one decoded string per chunk. A window that comes back as a repetition loop (Whisper
    reciting the prompt over a tone or noise) is decoded once more without the prompt."""
    model, proc = _load()
    texts = _decode_batch(model, proc, chunks, lang, prompt)
    redo = [i for i, t in enumerate(texts) if prompt and looping(t)]
    if redo:
        for i, t in zip(redo, _decode_batch(model, proc, [chunks[i] for i in redo], lang, None)):
            texts[i] = t
    return texts


def transcribe(data: bytes, lang: str = "de", prompt: str | None = None) -> dict:
    """Audio bytes → {"status": "ok"|"error"|"unavailable", "text", "language", "duration" (audio s), "seconds"
    (wall time)}, plus "message" (German) when not ok. `prompt=None` uses the carpenter prompt for German, "" none."""
    global _last_use
    t0 = time.time()
    lang = (lang or "").strip().lower()[:8] or None
    res = {"status": "ok", "text": "", "language": lang or "auto", "duration": 0.0, "seconds": 0.0}

    def done(**kw):
        res.update(kw, seconds=round(time.time() - t0, 2))
        return res

    try:
        audio = decode(data)
    except ValueError:
        return done(status="error", message="Die Aufnahme konnte nicht gelesen werden.")
    res["duration"] = round(len(audio) / RATE, 2)
    if prompt is None:
        prompt = PROMPT_DE if lang in (None, "de") else ""
    prompt = (prompt or "").strip()[:600] or None
    chunks = [audio[a:b] for a, b in windows(audio)]
    keep = [c for c in chunks if not silent(c)]
    if not keep:
        return done(message="Keine Sprache erkannt.")
    try:
        with _lock:
            texts = _generate(keep, lang, prompt)
            _last_use = time.time()
    except RuntimeError as e:
        msg = {"memory": "Gerade ist zu wenig Speicher frei – bitte gleich noch einmal versuchen.",
               "cuda": "Die Spracherkennung braucht die Grafikkarte."}.get(str(e), "Die Spracherkennung ist nicht verfügbar.")
        return done(status="unavailable", message=msg)
    except Exception as e:   # missing model files, CUDA errors: the studio keeps working without voice
        _last_use = time.time()
        return done(status="unavailable", message="Die Spracherkennung ist nicht verfügbar.", detail=str(e)[:300])
    text = " ".join(t for t in (clean(t, prompt) for t in texts) if t)
    return done(text=text, **({} if text else {"message": "Keine Sprache erkannt."}))


def status() -> dict:
    return {"loaded": loaded(), "model": REPO, "ffmpeg": ffmpeg_ok(), "load_seconds": _load_seconds,
            "idle_s": round(time.time() - _last_use) if loaded() and _last_use else None,
            "unload_after_s": IDLE_UNLOAD, "mem_free_gb": round(judge.mem_available_gb(), 1),
            "max_seconds": MAX_SECONDS, "max_mb": MAX_AUDIO // 2**20}


# ---------------------------------------------------------------- evaluation samples

def _audio_ext(data: bytes) -> str | None:
    """Container by magic bytes; the browser's MIME type is not trusted (Safari records mp4, Chrome webm)."""
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if data[:4] == b"OggS":
        return "ogg"
    if data[4:8] == b"ftyp":
        return "m4a"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        return "mp3"
    if data[:4] == b"fLaC":
        return "flac"
    return None


DEMUXER = {"webm": "matroska", "ogg": "ogg", "m4a": "mov", "wav": "wav", "mp3": "mp3", "flac": "flac"}


def _photo_jpeg(data: bytes) -> bytes:
    """Re-encoded JPEG (long side ≤ 2048) without EXIF: phone photos carry GPS coordinates of the customer's home."""
    from PIL import Image
    img = judge.rgb(Image.open(io.BytesIO(data)))
    if max(img.size) > 2048:
        img.thumbnail((2048, 2048), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def save_sample(audio: bytes, text: str = "", photo: bytes | None = None, who: str = "", lang: str = "de",
                note: str = "") -> dict:
    """Stores one consented evaluation sample: evalset/audio/<id>.<ext> (+ <id>.jpg) and <id>.json. Raises ValueError
    with a German message for bad input."""
    ext = _audio_ext(audio)
    if not ext:
        raise ValueError("Unbekanntes Audioformat – bitte als webm, ogg, m4a, wav oder mp3 aufnehmen.")
    try:
        pcm = decode(audio)
    except ValueError:
        raise ValueError("Die Aufnahme konnte nicht gelesen werden.") from None
    duration = round(len(pcm) / RATE, 2)
    if duration < 0.5:
        raise ValueError("Die Aufnahme ist zu kurz.")
    jpg = None
    if photo:
        try:
            jpg = _photo_jpeg(photo)
        except Exception:
            raise ValueError("Das Foto konnte nicht gelesen werden.") from None
    SAMPLES.mkdir(parents=True, exist_ok=True)
    sid = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    (SAMPLES / f"{sid}.{ext}").write_bytes(audio)
    if jpg:
        (SAMPLES / f"{sid}.jpg").write_bytes(jpg)
    meta = {"id": sid, "at": judge.now(), "audio": f"{sid}.{ext}", "photo": f"{sid}.jpg" if jpg else None,
            "duration": duration, "silent": silent(pcm), "language": (lang or "de")[:8],
            "text": (text or "").strip()[:2000], "note": (note or "").strip()[:500], "who": (who or "")[:80],
            "consent": True}
    tmp = SAMPLES / f".{sid}.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    tmp.replace(SAMPLES / f"{sid}.json")
    return meta


def list_samples() -> list[dict]:
    out = []
    for f in sorted(SAMPLES.glob("*.json"), reverse=True) if SAMPLES.is_dir() else []:
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            pass
    return out


def delete_sample(sid: str) -> bool:
    if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", sid or ""):
        return False
    hit = False
    for f in SAMPLES.glob(f"{sid}.*"):
        f.unlink(missing_ok=True)
        hit = True
    return hit


# ---------------------------------------------------------------- routes

async def _read(upload: UploadFile, limit: int, what: str) -> bytes:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"{what} ist zu groß (höchstens {limit // 2**20} MB).")
    return data


@router.get("/api/voice/status")
def voice_status():
    return status()


@router.post("/api/voice/transcribe")
async def voice_transcribe(audio: UploadFile = File(...), lang: str = Form("de")):
    data = await _read(audio, MAX_AUDIO, "Die Aufnahme")
    res = await run_in_threadpool(transcribe, data, lang)
    if res["status"] == "error":
        raise HTTPException(400, res.get("message") or "Die Aufnahme konnte nicht gelesen werden.")
    if res["status"] == "unavailable":
        raise HTTPException(503, res.get("message") or "Die Spracherkennung ist nicht verfügbar.")
    res.pop("detail", None)
    return res


@router.post("/api/voice/sample")
async def voice_sample(request: Request, audio: UploadFile = File(...), consent: str = Form(""), text: str = Form(""),
                       note: str = Form(""), lang: str = Form("de"), photo: UploadFile | None = File(None)):
    if consent.strip().lower() not in ("1", "true", "on", "ja", "yes"):
        raise HTTPException(400, "Bitte zuerst das Einverständnis bestätigen.")
    data = await _read(audio, MAX_AUDIO, "Die Aufnahme")
    pic = await _read(photo, MAX_PHOTO, "Das Foto") if photo is not None and photo.filename else None
    who = getattr(request.state, "owner_name", "") or ""
    try:
        meta = await run_in_threadpool(save_sample, data, text, pic or None, who, lang, note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return meta


@router.get("/api/voice/samples")
def voice_samples():
    items = list_samples()
    return {"count": len(items), "minutes": round(sum(s.get("duration") or 0 for s in items) / 60, 1),
            "recent": [{k: s.get(k) for k in ("id", "at", "duration", "text", "who", "photo")} for s in items[:10]]}


@router.delete("/api/voice/sample/{sid}")
def voice_sample_delete(sid: str):
    if not delete_sample(sid):
        raise HTTPException(404, "Aufnahme nicht gefunden.")
    return {"ok": True}
