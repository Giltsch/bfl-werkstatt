"""Speech to text (voice.py): ffmpeg decoding of every browser/phone container, 30 s windowing, silence and loop
filters, the model lifecycle (memory guard, idle unload, one inference at a time) with a fake Whisper, and the routes
incl. the consented evaluation-sample recorder on a tmp evalset. No GPU and no network needed."""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import judge
import voice

FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg missing")


def tone(seconds: float, freq: float = 440.0, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * voice.RATE)) / voice.RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def encode(audio: np.ndarray, fmt: str) -> bytes:
    """float32 16 kHz → container bytes via ffmpeg (the formats browsers and phones send)."""
    args = {"webm": ["-c:a", "libopus", "-f", "webm"], "ogg": ["-c:a", "libopus", "-f", "ogg"],
            "m4a": ["-c:a", "aac", "-f", "mp4"], "wav": ["-f", "wav"], "mp3": ["-c:a", "libmp3lame", "-f", "mp3"]}[fmt]
    with tempfile.TemporaryDirectory() as d:   # mp4 cannot be written to a pipe (its index is written at the end)
        out = Path(d) / f"a.{fmt}"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(voice.RATE), "-ac", "1", "-i", "-", *args,
                        str(out)], input=audio.astype("<f4").tobytes(), capture_output=True, check=True)
        return out.read_bytes()


def wav(audio: np.ndarray) -> bytes:
    return encode(audio, "wav")


class FakeWhisper:
    """Stands in for _decode_batch: records calls, answers per chunk via `answer(chunk, prompt)`, can be slow."""

    def __init__(self, answer=None, delay=0.0):
        self.calls, self.active, self.overlap = [], 0, False
        self.answer = answer or (lambda chunk, prompt: f"Einbauschrank {len(chunk) // voice.RATE} Sekunden")
        self.delay = delay

    def __call__(self, model, proc, chunks, lang, prompt):
        self.active += 1
        self.overlap |= self.active > 1
        time.sleep(self.delay)
        self.calls.append({"n": len(chunks), "lang": lang, "prompt": prompt, "lens": [len(c) for c in chunks]})
        self.active -= 1
        return [self.answer(c, prompt) for c in chunks]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """voice with a fake loaded model and a tmp evalset; restores module state afterwards."""
    fw = FakeWhisper()
    monkeypatch.setattr(voice, "_decode_batch", fw)
    monkeypatch.setattr(voice, "_model", object())
    monkeypatch.setattr(voice, "_proc", object())
    monkeypatch.setattr(voice, "_ensure_watcher", lambda: None)
    monkeypatch.setattr(voice, "SAMPLES", tmp_path / "evalset" / "audio")
    return fw


@pytest.fixture
def client(fake):
    app = FastAPI()
    app.include_router(voice.router)
    return TestClient(app)


# ---------------------------------------------------------------- decoding

@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["webm", "ogg", "m4a", "wav", "mp3"])
def test_decode_every_container_to_16k_mono_float(fmt):
    src = tone(2.0)
    out = voice.decode(encode(src, fmt))
    assert out.dtype == np.float32 and out.ndim == 1
    assert abs(len(out) / voice.RATE - 2.0) < 0.1           # codec padding is a few ms at most
    assert 0.15 < float(np.sqrt(np.mean(out ** 2))) < 0.3     # the 0.3-amplitude sine has RMS 0.21
    assert np.abs(out).max() <= 1.0


@needs_ffmpeg
def test_decode_resamples_stereo_44k():
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=1.5:sample_rate=44100",
                        "-ac", "2", "-f", "wav", "-"], capture_output=True, check=True)
    out = voice.decode(p.stdout)
    assert abs(len(out) - 1.5 * voice.RATE) < 400


@needs_ffmpeg
def test_decode_rejects_garbage_and_empty():
    with pytest.raises(ValueError):
        voice.decode(b"")
    with pytest.raises(ValueError):
        voice.decode(b"this is not audio at all" * 50)


@needs_ffmpeg
def test_decode_never_follows_playlists(tmp_path, monkeypatch):
    """A text upload in ffconcat/HLS form must not make ffmpeg read other files into the "recording"."""
    secret = tmp_path / "secret.wav"
    secret.write_bytes(wav(tone(1.0)))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))          # the temp dir next to the planted file
    for payload in (f"ffconcat version 1.0\nfile {secret.name}\n", f"ffconcat version 1.0\nfile {secret}\n",
                    f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{secret}\n#EXT-X-ENDLIST\n"):
        with pytest.raises(ValueError):
            voice.decode(payload.encode())
    # a real container whose bytes claim another format is still read only by the forced demuxer
    with pytest.raises(ValueError):
        voice.decode(b"OggS" + f"ffconcat version 1.0\nfile {secret.name}\n".encode())


@needs_ffmpeg
def test_decode_caps_length():
    out = voice.decode(wav(tone(5.0)), max_seconds=2)
    assert abs(len(out) - 2 * voice.RATE) < 200


# ---------------------------------------------------------------- windows, silence, filters

def test_windows_cover_long_audio_and_cut_in_pauses():
    # 2 minutes: 9 s "speech" blocks separated by 0.6 s pauses
    parts, pauses = [], []
    while sum(map(len, parts)) < 120 * voice.RATE:
        parts.append(tone(9.0))
        start = sum(map(len, parts))
        pauses.append((start, start + int(0.6 * voice.RATE)))
        parts.append(np.zeros(int(0.6 * voice.RATE), np.float32))
    audio = np.concatenate(parts)
    w = voice.windows(audio)
    assert w[0][0] == 0 and w[-1][1] == len(audio)
    assert all(a == b for (_, a), (b, _) in zip(w, w[1:]))                      # contiguous, no gaps or overlaps
    assert all(b - a <= voice.WINDOW * voice.RATE for a, b in w)
    assert len(w) >= 4
    for _, cut in w[:-1]:                                                         # every cut lands in a pause
        assert any(lo <= cut <= hi for lo, hi in pauses), cut / voice.RATE


def test_windows_short_and_empty():
    assert voice.windows(tone(3.0)) == [(0, 3 * voice.RATE)]
    assert voice.windows(np.zeros(0, np.float32)) == []
    w = voice.windows(tone(65.0))                                                 # no pause at all: still ≤ 30 s
    assert len(w) == 3 and all(b - a <= 30 * voice.RATE for a, b in w)


def test_silence_detection():
    assert voice.silent(np.zeros(voice.RATE, np.float32))
    assert voice.silent(np.random.default_rng(0).normal(0, 0.0005, voice.RATE).astype(np.float32))
    assert not voice.silent(tone(1.0, amp=0.05))
    assert voice.silent(np.zeros(0, np.float32))


def test_clean_drops_hallucinations_and_collapses_loops():
    assert voice.clean("Untertitel im Auftrag des ZDF, 2021") == ""
    assert voice.clean("  Vielen Dank fürs Zuschauen!  ") == ""
    assert voice.clean(" . ") == ""
    assert voice.clean("Dachschräge, Wandboard, " * 6 + "Ende").startswith("Dachschräge, Wandboard")
    assert voice.clean("Dachschräge, Wandboard, " * 6).count("Wandboard") == 1
    said = "Einbauschrank zwei zwanzig breit, raumhoch, Weißlack, grifflos."
    assert voice.clean(said, voice.PROMPT_DE) == said
    assert voice.clean("Der alte Schrank kommt weg.", voice.PROMPT_DE) == ""     # verbatim echo of the prompt
    assert voice.clean("Weißlack.", voice.PROMPT_DE) == "Weißlack."                # one real word that is also in the prompt
    assert voice.clean("ja ja ja, gut") == "ja ja ja, gut"                        # short words are not "loops"


def test_looping():
    assert voice.looping("Dachschräge, Wandboard, " * 10)
    assert not voice.looping("Einbauschrank links neben dem Fenster, zwei zwanzig breit, raumhoch, Weißlack.")
    assert not voice.looping("")


# ---------------------------------------------------------------- transcribe (fake model)

@needs_ffmpeg
def test_transcribe_short_memo_uses_german_prompt(fake):
    r = voice.transcribe(wav(tone(4.0)))
    assert r["status"] == "ok" and r["text"] == "Einbauschrank 4 Sekunden" and r["language"] == "de"
    assert abs(r["duration"] - 4.0) < 0.1 and r["seconds"] >= 0
    assert fake.calls == [{"n": 1, "lang": "de", "prompt": voice.PROMPT_DE, "lens": [4 * voice.RATE]}]


@needs_ffmpeg
def test_transcribe_other_language_and_custom_prompt(fake):
    voice.transcribe(wav(tone(2.0)), lang="en")
    voice.transcribe(wav(tone(2.0)), lang="", prompt="Kitchen")
    voice.transcribe(wav(tone(2.0)), lang="de", prompt="")
    assert [(c["lang"], c["prompt"]) for c in fake.calls] == [("en", None), (None, "Kitchen"), ("de", None)]


@needs_ffmpeg
def test_transcribe_two_minute_memo_in_windows_and_skips_silent_ones(fake):
    audio = np.concatenate([tone(50.0), np.zeros(40 * voice.RATE, np.float32), tone(30.0)])
    r = voice.transcribe(encode(audio, "webm"))
    assert r["status"] == "ok" and abs(r["duration"] - 120) < 0.2
    (call,) = fake.calls
    assert call["n"] == 4 and all(n <= 30 * voice.RATE for n in call["lens"])   # the silent window was skipped
    assert r["text"].count("Einbauschrank") == 4


@needs_ffmpeg
def test_transcribe_silence_never_runs_the_model(fake):
    r = voice.transcribe(wav(np.zeros(5 * voice.RATE, np.float32)))
    assert r["status"] == "ok" and r["text"] == "" and r["message"] == "Keine Sprache erkannt."
    assert fake.calls == []


@needs_ffmpeg
def test_transcribe_retries_a_prompt_loop_without_prompt(fake):
    fake.answer = lambda chunk, prompt: "Dachschräge, Wandboard, " * 12 if prompt else "Sockel"
    r = voice.transcribe(wav(tone(3.0)))
    assert r["text"] == "Sockel"
    assert [c["prompt"] for c in fake.calls] == [voice.PROMPT_DE, None]


def test_transcribe_bad_audio_is_an_error_not_an_exception(fake):
    r = voice.transcribe(b"garbage" * 100)
    assert r["status"] == "error" and r["text"] == "" and "gelesen" in r["message"]
    assert voice.transcribe(b"")["status"] == "error"


@needs_ffmpeg
def test_transcribe_unavailable_when_memory_is_short(fake, monkeypatch):
    monkeypatch.setattr(voice, "_model", None)
    monkeypatch.setattr(judge, "mem_available_gb", lambda: 4.0)
    waits = []
    monkeypatch.setattr(voice, "_sleep", waits.append)
    r = voice.transcribe(wav(tone(2.0)))
    assert r["status"] == "unavailable" and "Speicher" in r["message"]
    assert sum(waits) >= voice.MEMORY_WAIT and fake.calls == [] and not voice.loaded()


@needs_ffmpeg
def test_transcribe_unavailable_when_the_model_breaks(fake, monkeypatch):
    def boom(*a):
        raise OSError("model files missing")
    monkeypatch.setattr(voice, "_decode_batch", boom)
    r = voice.transcribe(wav(tone(2.0)))
    assert r["status"] == "unavailable" and r["text"] == ""


@needs_ffmpeg
def test_one_inference_at_a_time(fake):
    fake.delay = 0.15
    data, results = wav(tone(2.0)), []
    ts = [threading.Thread(target=lambda: results.append(voice.transcribe(data))) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(results) == 4 and all(r["status"] == "ok" for r in results)
    assert len(fake.calls) == 4 and not fake.overlap


def test_idle_unload(fake, monkeypatch):
    dropped = []
    monkeypatch.setattr(voice, "_drop", lambda: dropped.append(1) or monkeypatch.setattr(voice, "_model", None))
    monkeypatch.setattr(voice, "_last_use", 1000.0)
    assert not voice.idle_check(now=1000.0 + voice.IDLE_UNLOAD - 1)            # still warm
    with voice._lock:                                                           # busy: never unload mid-inference
        assert not voice.idle_check(now=1000.0 + voice.IDLE_UNLOAD + 1)
    assert voice.idle_check(now=1000.0 + voice.IDLE_UNLOAD + 1)
    assert dropped == [1] and not voice.loaded()
    assert not voice.idle_check(now=1e12)                                       # nothing left to unload


def test_status_shape(fake):
    s = voice.status()
    assert s["loaded"] is True and s["model"] == voice.REPO and s["unload_after_s"] == voice.IDLE_UNLOAD
    assert s["ffmpeg"] == FFMPEG and isinstance(s["mem_free_gb"], float)


# ---------------------------------------------------------------- routes

def test_route_status(client):
    r = client.get("/api/voice/status")
    assert r.status_code == 200 and r.json()["loaded"] is True


@needs_ffmpeg
def test_route_transcribe(client, fake):
    r = client.post("/api/voice/transcribe", files={"audio": ("memo.webm", encode(tone(3.0), "webm"), "audio/webm")},
                    data={"lang": "de"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["text"] == "Einbauschrank 3 Sekunden" and "detail" not in body


def test_route_transcribe_errors(client, fake, monkeypatch):
    r = client.post("/api/voice/transcribe", files={"audio": ("x.webm", b"nope" * 100, "audio/webm")})
    assert r.status_code == 400 and "gelesen" in r.json()["detail"]
    monkeypatch.setattr(voice, "MAX_AUDIO", 1000)
    r = client.post("/api/voice/transcribe", files={"audio": ("x.webm", b"\0" * 2000, "audio/webm")})
    assert r.status_code == 413 and "zu groß" in r.json()["detail"]
    assert client.post("/api/voice/transcribe").status_code == 422


@needs_ffmpeg
def test_route_transcribe_unavailable_is_503(client, monkeypatch):
    monkeypatch.setattr(voice, "transcribe", lambda data, lang: {"status": "unavailable", "text": "", "message": "weg"})
    r = client.post("/api/voice/transcribe", files={"audio": ("x.wav", wav(tone(1.0)), "audio/wav")})
    assert r.status_code == 503 and r.json()["detail"] == "weg"


def _photo_with_gps() -> bytes:
    img = Image.new("RGB", (3000, 2000), (180, 160, 140))
    exif = Image.Exif()
    exif[0x8825] = {2: (49.0, 18.0, 0.0)}      # GPSInfo: the customer's home
    exif[0x010F] = "PhoneMaker"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


@needs_ffmpeg
def test_sample_needs_consent(client, fake):
    r = client.post("/api/voice/sample", files={"audio": ("a.webm", encode(tone(2.0), "webm"), "audio/webm")},
                    data={"text": "Einbauschrank"})
    assert r.status_code == 400 and "Einverständnis" in r.json()["detail"]
    assert not voice.SAMPLES.exists() or not any(voice.SAMPLES.iterdir())


@needs_ffmpeg
def test_sample_is_stored_with_photo_without_exif(client, fake):
    audio = encode(tone(2.5), "webm")
    r = client.post("/api/voice/sample",
                    files={"audio": ("a.webm", audio, "audio/webm"), "photo": ("p.jpg", _photo_with_gps(), "image/jpeg")},
                    data={"consent": "1", "text": "Einbauschrank zwei zwanzig breit, Weißlack", "note": "Werkstatt laut"})
    assert r.status_code == 200, r.text
    meta = r.json()
    sid = meta["id"]
    assert (voice.SAMPLES / f"{sid}.webm").read_bytes() == audio
    stored = json.loads((voice.SAMPLES / f"{sid}.json").read_text())
    assert stored == meta and stored["consent"] is True and stored["text"].startswith("Einbauschrank")
    assert abs(stored["duration"] - 2.5) < 0.1 and stored["silent"] is False and stored["audio"] == f"{sid}.webm"
    img = Image.open(voice.SAMPLES / f"{sid}.jpg")
    assert max(img.size) == 2048 and len(img.getexif()) == 0                     # downscaled, no GPS/maker tags
    assert not list(voice.SAMPLES.glob(".*.tmp"))
    lst = client.get("/api/voice/samples").json()
    assert lst["count"] == 1 and lst["recent"][0]["id"] == sid and lst["minutes"] == round(stored["duration"] / 60, 1)
    assert client.delete(f"/api/voice/sample/{sid}").json() == {"ok": True}
    assert list(voice.SAMPLES.iterdir()) == [] and client.get("/api/voice/samples").json()["count"] == 0
    assert client.delete(f"/api/voice/sample/{sid}").status_code == 404


@needs_ffmpeg
@pytest.mark.parametrize("fmt,ext", [("m4a", "m4a"), ("ogg", "ogg"), ("wav", "wav"), ("mp3", "mp3")])
def test_sample_keeps_the_real_container(client, fake, fmt, ext):
    r = client.post("/api/voice/sample", files={"audio": ("a.webm", encode(tone(1.0), fmt), "audio/webm")},
                    data={"consent": "on"})
    assert r.status_code == 200, r.text
    assert r.json()["audio"].endswith("." + ext) and r.json()["photo"] is None


@needs_ffmpeg
def test_sample_rejects_bad_input(client, fake, monkeypatch):
    def post(audio, **extra):
        return client.post("/api/voice/sample", files={"audio": ("a", audio, "audio/webm"), **extra},
                           data={"consent": "1"})
    assert post(b"hello world, no audio" * 20).status_code == 400                  # unknown container
    assert "zu kurz" in post(encode(tone(0.2), "webm")).json()["detail"]
    r = post(encode(tone(1.0), "webm"), photo=("p.jpg", b"not a picture", "image/jpeg"))
    assert r.status_code == 400 and "Foto" in r.json()["detail"]
    monkeypatch.setattr(voice, "MAX_AUDIO", 500)
    assert post(encode(tone(3.0), "wav")).status_code == 413
    assert not voice.SAMPLES.exists() or not any(voice.SAMPLES.iterdir())


def test_delete_sample_validates_ids(client, fake):
    voice.SAMPLES.mkdir(parents=True)
    (voice.SAMPLES / "keep.json").write_text("{}")
    for bad in ("..", "keep", "20261018-120000-zzzzzz", "*"):
        assert client.delete(f"/api/voice/sample/{bad}").status_code in (404, 405)
    assert (voice.SAMPLES / "keep.json").exists()
    assert not voice.delete_sample("../../projects/settings")
