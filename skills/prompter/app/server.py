"""TakeCheck by Tamir Naim: a local teleprompter that records voice or video takes.

Runs on 127.0.0.1 only. While you record, the take streams to disk every second, so a browser crash
does not lose it. The take is finished the moment you stop, in the output folder, so Claude can review
it without being told where it is. This server sends nothing anywhere.
Scripts can be edited in the page; edits are written back to the script file so Claude sees them.

    python -I server.py [--out recordings] [--script FILE_OR_FOLDER ...] [--port 8770]

Needs Python 3.9+ and ffmpeg on PATH. No Python packages.
"""
import argparse, hashlib, json, os, re, shutil, socket, subprocess, sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

HERE = Path(__file__).resolve().parent
MAX_AUDIO = 300 * 1024 * 1024
MAX_VIDEO = 4 * 1024 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
MAX_SCRIPT = 1024 * 1024
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/logo.webp": ("logo.webp", "image/webp")}
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

ap = argparse.ArgumentParser(description="Local teleprompter + recorder")
ap.add_argument("--out", default="recordings", help="folder for takes (created; git-ignored)")
ap.add_argument("--script", action="append", default=[], help="a .txt/.md script, or a folder of them")
ap.add_argument("--port", type=int, default=8770, help="first port to try; the next free one is used if taken")
A = ap.parse_args()
OUT = Path(A.out).resolve()
PARTS = OUT / ".parts"                        # takes in progress, streamed during recording
PORT = A.port                                 # the port actually bound (set at start-up)
CFR = ["-fps_mode", "cfr"]                    # replaced by the old spelling on ffmpeg older than 5.1


def scripts():
    """Only these files are ever read by the page."""
    found = []
    for s in A.script:
        p = Path(s).resolve()
        if p.is_dir():
            found += sorted(list(p.glob("*.txt")) + list(p.glob("*.md")))
        elif p.is_file():
            found.append(p)
    seen, out = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def key(p):
    """A stable id for a script file: list positions shift when a file is added, this does not."""
    return hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:12]


def rev(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def script_dir():
    """The first --script folder: where text pasted in the page is saved as a file."""
    return next((Path(s).resolve() for s in A.script if Path(s).resolve().is_dir()), None)


def write_text(p, text):
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)                        # atomic: the page's 2-second re-read never sees half a file


def ffmpeg(*args):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print("ffmpeg failed: " + (r.stderr.strip().splitlines() or ["(no message)"])[-1], flush=True)
    return r.returncode == 0


def check_ffmpeg():
    """ffmpeg must exist; ffmpeg older than 5.1 spells the constant-frame-rate option differently."""
    global CFR
    try:
        first = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, errors="replace").stdout.splitlines()[0]
    except (OSError, IndexError):
        sys.exit("ffmpeg was not found on PATH. Install it first: Windows `winget install ffmpeg`, "
                 "macOS `brew install ffmpeg`, Linux your distribution's package.")
    m = re.search(r"version n?(\d+)\.(\d+)", first)
    if m and (int(m.group(1)), int(m.group(2))) < (5, 1):
        CFR = ["-vsync", "cfr"]
        print(f"Note: {first.split(' Copyright')[0]} is older than 5.1; using its older options. Upgrading is recommended.", flush=True)


def prepare_out():
    OUT.mkdir(parents=True, exist_ok=True)
    gi = OUT / ".gitignore"
    if not gi.exists():                       # recordings hold a face and a voice: never commit them
        gi.write_text("*\n", encoding="utf-8")


def recover_parts():
    """A take still streaming when the browser or the computer died is kept, not lost."""
    if not PARTS.is_dir():
        return
    for p in PARTS.glob("*.part"):
        if p.stat().st_size == 0:
            p.unlink()
            continue
        stamp = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d_%H%M%S")
        dest = OUT / f"{stamp}_recovered_original.webm"
        shutil.move(str(p), str(dest))
        print(f"Recovered an unfinished take: {dest}", flush=True)


PICK = r"""
import sys, tkinter as tk
from tkinter import filedialog
r = tk.Tk(); r.withdraw(); r.attributes("-topmost", True)
p = filedialog.askdirectory(title=sys.argv[1], initialdir=sys.argv[2], mustexist=False)
sys.stdout.write(p or "")
"""


def pick_folder(title, start):
    """Open the system's own folder dialog on this computer (in a child process, so the server never blocks on Tk)."""
    try:
        r = subprocess.run([sys.executable, "-I", "-c", PICK, title, start], capture_output=True, text=True,
                           encoding="utf-8", timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None, "dialog-failed"
    if r.returncode != 0:
        return None, "no-dialog"              # e.g. Python built without tkinter: the page falls back to typing a path
    return r.stdout.strip(), None


def target_dir(header):
    """The folder the viewer chose in the page, or the default. Only absolute paths are accepted."""
    raw = unquote(header or "").strip()
    if not raw:
        return OUT
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("folder must be an absolute path")
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def take_id(s):
    return s if s and re.fullmatch(r"[a-z0-9]{8,40}", s) else None


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        # the page may talk only to this server; the one outside address is Google Fonts, for the fonts alone
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(b)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _ours(self, api):
        """Only this prompter's own page may use it.
        - Host must be this exact address and port (blocks DNS rebinding).
        - A browser Origin, when sent, must be this exact origin: other local apps on other ports are refused too.
        - API calls must carry the X-Prompter header. Another page cannot add it without a CORS preflight,
          which this server never approves, and an <img> or form cannot add it at all."""
        hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if (self.headers.get("Host") or "") not in hosts:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {"http://" + h for h in hosts}:
            return False
        return not api or self.headers.get("X-Prompter") == "1"

    def do_GET(self):
        u = urlparse(self.path)
        if not self._ours(api=u.path not in STATIC):
            return self._send(403, "forbidden", "text/plain")
        if u.path in STATIC:
            name, ctype = STATIC[u.path]
            return self._send(200, (HERE / name).read_bytes(), ctype)
        if u.path == "/config":
            return self._json({"default_folder": str(OUT)})
        if u.path == "/pick-folder":
            q = parse_qs(u.query)
            start = q.get("start", [str(OUT)])[0]
            path, err = pick_folder(q.get("title", ["Choose a folder for your takes"])[0],
                                    start if Path(start).is_dir() else str(OUT))
            return self._json({"folder": path or "", "error": err})
        if u.path == "/scripts":
            return self._json([{"id": i, "name": p.stem, "key": key(p)} for i, p in enumerate(scripts())])
        if u.path == "/script":
            lst = scripts()
            try:
                i = int(parse_qs(u.query).get("id", ["0"])[0])
            except ValueError:
                i = 0
            if not 0 <= i < len(lst):
                return self._json({"name": "take", "text": ""})
            text = lst[i].read_text(encoding="utf-8")
            return self._json({"name": lst[i].stem, "text": text, "key": key(lst[i]), "rev": rev(text)})
        self._send(404, "not found", "text/plain")

    def _read_into(self, f, n):
        left = n
        while left:
            b = self.rfile.read(min(left, 1 << 20))
            if not b:
                break
            f.write(b)
            left -= len(b)
        return n - left

    def do_POST(self):
        u = urlparse(self.path)
        if not self._ours(api=True):
            return self._send(403, "forbidden", "text/plain")
        n = int(self.headers.get("Content-Length") or 0)
        if u.path == "/append":                # one second of recording, appended while the take runs
            tid = take_id(parse_qs(u.query).get("id", [""])[0])
            if not tid or n <= 0 or n > MAX_CHUNK:
                return self._send(400, "bad chunk", "text/plain")
            prepare_out()
            PARTS.mkdir(exist_ok=True)
            part = PARTS / f"{tid}.part"
            if part.exists() and part.stat().st_size + n > MAX_VIDEO:
                return self._send(413, "take too large", "text/plain")
            with open(part, "ab") as f:
                self._read_into(f, n)
            return self._json({"ok": True})
        if u.path == "/script":                # a script edited in the page, written back to its file
            if n < 0 or n > MAX_SCRIPT:
                return self._send(413, "script too large", "text/plain")
            body = self.rfile.read(n).decode("utf-8", errors="replace").replace("\r\n", "\n")
            k, base = self.headers.get("X-Script-Key") or "", self.headers.get("X-Base-Rev") or ""
            if k == "mine":                    # pasted text becomes a real script file, so Claude can see it
                d = script_dir()
                if d is None:
                    return self._json({"error": "no-folder"}, 409)
                p, i = d / "my-text.txt", 2
                while p.exists():
                    p, i = d / f"my-text-{i}.txt", i + 1
                write_text(p, body)
                print(f"script saved: {p}", flush=True)
                return self._json({"key": key(p), "rev": rev(body), "name": p.stem})
            p = next((q for q in scripts() if key(q) == k), None)
            if p is None:
                return self._json({"error": "unknown script"}, 404)
            cur = p.read_text(encoding="utf-8")
            kept = None
            if base and rev(cur) != base and cur != body:
                # someone else (usually Claude) changed the file while the viewer typed: the viewer's text wins
                # on screen, and the other version is kept next to it so nothing is lost
                q = p.with_name(f"{p.stem}_claude_{datetime.now():%H%M%S}{p.suffix}")
                write_text(q, cur)
                kept = q.name
            write_text(p, body)
            print(f"script saved: {p}" + (f"  (the version changed meanwhile kept as {kept})" if kept else ""), flush=True)
            return self._json({"key": k, "rev": rev(body), "name": p.stem, "conflict": kept})
        if u.path != "/save":
            return self._send(404, "not found", "text/plain")

        video = (self.headers.get("X-Kind") or "") == "video"
        tid = take_id(self.headers.get("X-Take-Id") or "")
        part = PARTS / f"{tid}.part" if tid else None
        streamed = n == 0 and part is not None and part.exists() and part.stat().st_size > 0
        if not streamed and (n <= 0 or n > (MAX_VIDEO if video else MAX_AUDIO)):
            return self._send(413, "bad size", "text/plain")
        raw = unquote(self.headers.get("X-Take-Name") or "take")
        safe = re.sub(r"[^\w\-]+", "_", raw, flags=re.UNICODE).strip("_")[:60] or "take"
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        prepare_out()
        try:
            folder = target_dir(self.headers.get("X-Out-Dir"))
        except (ValueError, OSError) as e:
            return self._json({"error": str(e)}, 400)
        # the browser's own format: WebM in Chrome/Edge/Firefox, MP4 in Safari; the name says which
        mime = (self.headers.get("X-Mime") or "").lower()
        ext = ".mp4" if "mp4" in mime else ".ogg" if "ogg" in mime else ".webm"
        src = folder / f"{stamp}_{safe}_original{ext}"
        if streamed:
            shutil.move(str(part), str(src))
        else:
            with open(src, "wb") as f:        # the whole take in one upload (fallback when streaming failed)
                self._read_into(f, n)
            if part is not None and part.exists():
                part.unlink()
        wav = folder / f"{stamp}_{safe}.wav"
        wav_ok = ffmpeg("-i", str(src), "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s24le", str(wav))
        files, warnings = [], []
        if video:
            try:
                fps = max(15, min(60, round(float(self.headers.get("X-Fps") or 30))))
            except ValueError:
                fps = 30
            mp4 = folder / f"{stamp}_{safe}.mp4"
            # browsers record a variable frame rate; editors and lip-sync tools want a constant one
            if ffmpeg("-i", str(src), "-r", str(fps), *CFR, "-c:v", "libx264", "-preset", "medium",
                      "-crf", "14", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart", str(mp4)):
                files.append(mp4)
            else:
                warnings.append("mp4-failed")
            files.append(src)                 # the untouched original
            if wav_ok:
                files.append(wav)
        elif wav_ok:
            src.unlink()                      # voice: keep only the WAV
            files.append(wav)
        else:
            files.append(src)
            warnings.append("wav-failed")
        # which script was on screen, so the review can compare what was said with what was written
        script, sid = None, self.headers.get("X-Script") or ""
        if sid.isdigit():
            found = scripts()
            if int(sid) < len(found):
                script = str(found[int(sid)])
        elif sid == "mine":
            text = unquote(self.headers.get("X-Script-Text") or "")
            if text.strip():
                sp = folder / f"{stamp}_{safe}_script.txt"
                sp.write_text(text, encoding="utf-8")
                script = str(sp)
        latest = {"kind": "video" if video else "audio", "saved_at": datetime.now().isoformat(timespec="seconds"),
                  "files": [str(p) for p in files], "script": script, "warnings": warnings}
        body = json.dumps(latest, ensure_ascii=False, indent=2)
        # the default folder always holds the pointer to the newest take, wherever it was saved,
        # so Claude finds it without being told; the chosen folder gets its own copy too
        for d in {OUT, folder}:
            (d / "latest.json").write_text(body, encoding="utf-8")
        print("saved " + ", ".join(str(p) for p in files) + (f"  (warnings: {', '.join(warnings)})" if warnings else ""), flush=True)
        self._json({"saved": [p.name for p in files], "folder": str(folder), "warnings": warnings})

    def log_message(self, *a):
        pass


class Server(ThreadingHTTPServer):
    # On Windows "reuse address" lets a second server silently share a busy port; lock the port instead.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve():
    global PORT
    for port in range(A.port, A.port + 20):    # a taken port is not a crash: try the next one
        try:
            httpd = Server(("127.0.0.1", port), H)
        except OSError:
            continue
        PORT = port
        if port != A.port:
            print(f"Port {A.port} is busy; using {port} instead.", flush=True)
        print(f"TakeCheck running: http://127.0.0.1:{port}  (open it in Chrome or Edge)", flush=True)
        print(f"Takes are saved to: {OUT}", flush=True)
        httpd.serve_forever()
        return
    sys.exit(f"No free port between {A.port} and {A.port + 19}. Start with --port <another number>.")


if __name__ == "__main__":
    check_ffmpeg()
    prepare_out()
    recover_parts()
    serve()
