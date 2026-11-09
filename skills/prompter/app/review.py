"""Measure a recorded take so Claude can review it.

    python -I review.py [TAKE_FILE_OR_RECORDINGS_FOLDER] [--script FILE] [--no-transcript] [--model NAME]

With a folder (default: ./recordings) it picks the newest take from latest.json.
Prints JSON with duration, resolution, frame rate, loudness, noise floor and a blur score,
and writes a contact sheet (8 frames) next to the take as <take>_sheet.jpg for Claude to look at.
Needs ffmpeg/ffprobe only.

Optional: if faster-whisper is installed (pip install faster-whisper), it also transcribes the take on this
computer, writes <take>_transcript.txt and <take>.srt, measures the speaking pace and long pauses, and compares
what was said with the script that was on screen. Without it, everything else works as before.
"""
import argparse, difflib, json, os, re, subprocess, sys, tempfile, unicodedata, zlib
from pathlib import Path

# Hebrew gets the Israeli fine-tune (clearly better on Hebrew, clearly worse on English); anything else stock large-v3
MODEL_HE = "ivrit-ai/whisper-large-v3-turbo-ct2"
MODEL_OTHER = "Systran/faster-whisper-large-v3"
MODEL_DETECT = "Systran/faster-whisper-tiny"
PAUSE_S = 1.2                                   # a gap between words this long counts as a pause


def run(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def pick(arg):
    p = Path(arg).resolve()
    if p.is_file():
        return p
    latest = p / "latest.json"
    if latest.exists():
        files = [Path(f) for f in json.loads(latest.read_text(encoding="utf-8"))["files"]]
        for ext in (".mp4", ".wav", ".webm", ".ogg"):
            for f in files:
                if f.suffix == ext and f.exists() and ("_original" not in f.stem or ext in (".webm", ".ogg")):
                    return f
    takes = sorted([f for f in p.glob("*") if f.suffix in (".mp4", ".wav") and "_original" not in f.stem], key=lambda f: f.stat().st_mtime)
    if not takes:
        sys.exit(f"No takes found in {p}")
    return takes[-1]


def script_of(arg, take):
    """The script that was on screen for this take, as recorded by the server in latest.json."""
    latest = Path(arg).resolve() / "latest.json"
    if not latest.exists():
        latest = take.parent / "latest.json"
    if not latest.exists():
        return None
    j = json.loads(latest.read_text(encoding="utf-8"))
    if str(take) not in j.get("files", []) or not j.get("script"):
        return None
    s = Path(j["script"])
    return s if s.is_file() else None


def recent(take, n=5):
    """The last few takes in the reviewed take's folder, so a short accidental take right after a real one is noticed."""
    rows = []
    for f in sorted([f for f in take.parent.glob("*") if f.suffix in (".mp4", ".wav") and "_original" not in f.stem], key=lambda f: f.stat().st_mtime)[-2 * n:]:
        if f.suffix == ".wav" and f.with_suffix(".mp4").exists():
            continue                          # a video take's own sound file
        d = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)]).stdout.strip()
        rows.append({"file": f.name, "kind": "video" if f.suffix == ".mp4" else "audio",
                     "duration_s": round(float(d or 0), 1)})
    return rows[-n:]


# ---------- optional transcription ----------

def whisper():
    """faster-whisper if it is installed, else None. `python -I` hides the user site-packages, so look there too."""
    try:
        import faster_whisper
        return faster_whisper
    except ImportError:
        pass
    import site
    user = site.getusersitepackages()
    if user and Path(user).is_dir() and user not in sys.path:
        sys.path.append(user)                 # appended, so it can never shadow the standard library
        try:
            import faster_whisper
            return faster_whisper
        except ImportError:
            pass
    return None


def cuda_dlls():
    """Windows: pip's NVIDIA runtime keeps its DLLs inside site-packages, which Windows does not search.
    CTranslate2 loads them with a plain LoadLibrary, so they must be on PATH ("cublas64_12.dll is not found")."""
    if os.name != "nt":
        return
    import site
    roots = [Path(p) for p in site.getsitepackages() + [site.getusersitepackages()]]
    bins = [str(b) for r in roots for b in sorted((r / "nvidia").glob("*/bin"))]
    if bins:
        os.environ["PATH"] = os.pathsep.join(bins) + os.pathsep + os.environ.get("PATH", "")


_models = {}


def load(fw, name):
    if name not in _models:
        # float16 on the GPU, not int8: CTranslate2's int8 path fails on RTX 50-series cards
        try:
            _models[name] = (fw.WhisperModel(name, device="cuda", compute_type="float16"), "gpu")
        except Exception:
            _models[name] = (fw.WhisperModel(name, device="cpu", compute_type="int8"), "cpu")
    return _models[name]


def words_of(text):
    """Comparable words: no punctuation, no Hebrew vowel marks, lower case."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.findall(r"\w+", text.lower())


def srt_time(s):
    ms = int(round(s * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def compare(said, script_path):
    """How much of the script was said, and which script paragraphs were mostly skipped."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", script_path.read_text(encoding="utf-8")) if p.strip()]
    paras = [p for p in paras if not (p.startswith("(") and p.endswith(")"))]       # stage directions are not spoken
    paras = [q for q in (re.sub(r"\[[^\]]*\]", " ", p).strip() for p in paras) if q]    # nor [notes in square brackets]
    owner, sw = [], []
    for i, p in enumerate(paras):
        w = words_of(p)
        sw += w
        owner += [i] * len(w)
    if not sw:
        return None
    hit = [False] * len(sw)
    for b in difflib.SequenceMatcher(None, sw, said, autojunk=False).get_matching_blocks():
        for k in range(b.a, b.a + b.size):
            hit[k] = True
    missed = []
    for i, p in enumerate(paras):
        idx = [k for k, o in enumerate(owner) if o == i]
        if idx and sum(hit[k] for k in idx) / len(idx) < 0.6:
            missed.append(p if len(p) <= 80 else p[:80] + "…")
    return {"script": str(script_path), "script_words": len(sw),
            "script_words_said": round(sum(hit) / len(sw), 2), "paragraphs_mostly_missed": missed}


def script_language(path):
    """he / en from the letters of the script on screen: far more reliable than guessing from a quiet take."""
    if not path or not path.is_file():
        return None
    t = path.read_text(encoding="utf-8")
    he = len(re.findall(r"[֐-׿]", t))
    lat = len(re.findall(r"[A-Za-z]", t))
    return "he" if he > lat else "en" if lat > he else None


def transcribe(fw, take, model=None, lang=None):
    cuda_dlls()
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        # loudness-normalised, so a quiet take is not mistaken for another language or "heard" as noise
        run(["ffmpeg", "-v", "error", "-y", "-i", str(take), "-vn", "-af", "loudnorm=I=-20:TP=-2",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
        if not wav.exists():
            return {"error": "could not extract the sound"}
        if model:
            name = model
        elif lang:
            name = MODEL_HE if lang == "he" else MODEL_OTHER
        else:
            probe = Path(td) / "p.wav"
            run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-t", "30", str(probe)])
            _, info = load(fw, MODEL_DETECT)[0].transcribe(str(probe), beam_size=1)
            lang = info.language
            name = MODEL_HE if lang == "he" else MODEL_OTHER
        m, device = load(fw, name)
        segs, info = m.transcribe(str(wav), language=lang, beam_size=5, word_timestamps=True, vad_filter=True,
                                  vad_parameters={"min_silence_duration_ms": 500}, condition_on_previous_text=False)
        segs = list(segs)
    words = [w for s in segs for w in (s.words or []) if w.word.strip()]
    text = " ".join(s.text.strip() for s in segs if s.text.strip())
    txt = take.with_name(take.stem + "_transcript.txt")
    txt.write_text(text + "\n", encoding="utf-8")
    srt = take.with_suffix(".srt")
    srt.write_text("".join(f"{i}\n{srt_time(s.start)} --> {srt_time(s.end)}\n{s.text.strip()}\n\n"
                           for i, s in enumerate([s for s in segs if s.text.strip()], 1)), encoding="utf-8")
    out = {"language": info.language, "model": name, "device": device, "transcript_file": str(txt), "subtitles_file": str(srt),
           "words": len(words)}
    if text and len(text) <= 1500:
        out["text"] = text
    if len(words) > 1:
        span = words[-1].end - words[0].start
        if span > 0:
            out["words_per_second"] = round(len(words) / span, 2)   # from first word to last, so silence at the ends does not count
            if out["words_per_second"] > 6:      # nobody speaks that fast: the model invented text
                out["suspect"] = "faster than anyone speaks; the transcript is probably invented. Do not trust it."
        raw = text.encode("utf-8")
        if len(raw) > 80 and len(raw) / len(zlib.compress(raw)) > 2.4:   # Whisper's own repetition test
            out.setdefault("suspect", "the same few words repeat over and over: a test take, or text the model invented")
        gaps = [(words[i].end, words[i + 1].start - words[i].end) for i in range(len(words) - 1)]
        long_ = sorted([g for g in gaps if g[1] >= PAUSE_S], key=lambda g: -g[1])[:5]
        out["long_pauses"] = [{"at_s": round(a, 1), "seconds": round(d, 1)} for a, d in sorted(long_)]
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Hebrew and other non-Latin paths print intact on Windows
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default="recordings")
    ap.add_argument("--script", help="compare with this script instead of the one recorded in latest.json")
    ap.add_argument("--no-transcript", action="store_true")
    ap.add_argument("--model", help="a faster-whisper model name, instead of choosing by language")
    ap.add_argument("--language", help="the spoken language code (he, en, ...), if not the script's language")
    A = ap.parse_args()
    arg = A.target
    take = pick(arg)
    info = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(take)]).stdout)
    dur = float(info["format"].get("duration", 0))
    out = {"take": str(take), "duration_s": round(dur, 1)}
    if not Path(arg).is_file():
        out["recent_takes"] = recent(take)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if v:
        num, den = (v.get("avg_frame_rate") or "0/1").split("/")
        out.update(width=v["width"], height=v["height"], fps=round(float(num) / float(den or 1), 2))

    # loudness: mean/peak level, and the quietest 10% of 50 ms windows as a rough noise floor
    a = run(["ffmpeg", "-hide_banner", "-i", str(take), "-vn", "-af",
             "volumedetect,asetnsamples=2400,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
             "-f", "null", "-"]).stderr
    m = re.search(r"mean_volume: (-?[\d.]+) dB", a)
    p = re.search(r"max_volume: (-?[\d.]+) dB", a)
    rms = sorted(float(x) for x in re.findall(r"RMS_level=(-?[\d.]+)", a) if x not in ("-inf",))
    if m: out["mean_volume_db"] = float(m.group(1))
    if p: out["peak_db"] = float(p.group(1))
    if rms:
        floor = rms[: max(1, len(rms) // 10)]
        out["noise_floor_db"] = round(sum(floor) / len(floor), 1)
        if m: out["speech_to_noise_db"] = round(out["mean_volume_db"] - out["noise_floor_db"], 1)

    if v and dur > 0:
        # blur: ffmpeg blurdetect on the centre of the frame (0 = sharp; higher = softer)
        b = run(["ffmpeg", "-hide_banner", "-i", str(take), "-an", "-vf",
                 f"fps=2,crop=iw/2:ih*2/3,blurdetect,metadata=print:key=lavfi.blur", "-f", "null", "-"]).stderr
        blurs = [float(x) for x in re.findall(r"lavfi\.blur=([\d.]+)", b)]
        if blurs: out["blur_centre"] = round(sum(blurs) / len(blurs), 2)
        sheet = take.with_name(take.stem + "_sheet.jpg")
        n = 8
        run(["ffmpeg", "-v", "error", "-y", "-i", str(take), "-an", "-vf",
             f"fps={n}/{dur:.3f},scale=640:-2,tile=4x2:padding=4:color=black", "-frames:v", "1", "-q:v", "3", str(sheet)])
        if sheet.exists(): out["contact_sheet"] = str(sheet)
        mid = take.with_name(take.stem + "_mid.jpg")
        run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur / 2:.2f}", "-i", str(take), "-frames:v", "1", "-q:v", "2", str(mid)])
        if mid.exists(): out["middle_frame"] = str(mid)

    if A.no_transcript:
        pass
    elif (fw := whisper()) is None:
        out["transcript"] = {"available": False, "install": "pip install faster-whisper"}
    else:
        try:
            script = Path(A.script) if A.script else script_of(arg, take)
            t = transcribe(fw, take, A.model, A.language or script_language(script))
            if script and script.is_file() and t.get("words"):
                said = words_of(take.with_name(take.stem + "_transcript.txt").read_text(encoding="utf-8"))
                t["vs_script"] = compare(said, script)
            out["transcript"] = t
        except Exception as e:                    # transcription is a bonus: never lose the rest of the review over it
            out["transcript"] = {"error": f"{type(e).__name__}: {e}"[:300]}
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
