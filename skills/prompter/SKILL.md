---
name: prompter
description: Local teleprompter that records voice or video takes of the user on their own computer, saves every take automatically, and lets Claude review the latest take (framing, eye line, light, sharpness, sound) without the user pointing to any file. Use when the user wants to record themselves reading a script, film a talking-head or piece to camera, record a voice-over, capture footage for a digital twin, avatar or lip-sync tool, wants a script written for a video they will read on camera ("write me a script for a reel", "תכתוב לי תסריט לסרטון"), or asks "check my take", "did it come out ok", "צילמתי", "תבדוק את הטייק".
---

# TakeCheck by Tamir Naim

A teleprompter page that runs only on `127.0.0.1`. The user reads from it while it records the microphone,
or the camera and microphone. **Every take saves itself the moment the user presses Stop**, into a
recordings folder, and `latest.json` there always points at the newest take. So when the user says
"done", you review it yourself; never ask them where the file is. You can also write the script for them:
it appears on the prompter by itself.

Files in this skill (`${CLAUDE_SKILL_DIR}` is this skill's folder):
- `app/server.py`: the local server. Python 3.9+ and `ffmpeg` on PATH; no packages.
- `app/index.html`, `app/logo.webp`: the page (Hebrew/English, follows the browser language, toggle in the bar).
- `app/review.py`: measures a take and writes a contact sheet. `ffmpeg`/`ffprobe` only.

## 1. Start it

1. Check `ffmpeg -version` works. If not, tell the user how to install it (Windows: `winget install ffmpeg`;
   macOS: `brew install ffmpeg`; Linux: the distro package) and stop. Use `python` if it is Python 3,
   otherwise `python3` (macOS and most Linux), in every command below.
2. Pick the recordings folder: `recordings/` in the current project unless the user names another.
   The server creates it with a `.gitignore` of `*`, so takes are never committed.
3. Start the server **in the background** (it keeps running):
   `python -I "${CLAUDE_SKILL_DIR}/app/server.py" --out "<recordings folder>" [--script <file or folder>] [--port 8770]`
   `--script` can repeat; a folder loads every `.txt`/`.md` in it. Without a script the page opens on
   a built-in "Sample text" (first in the script menu, in the page's language) that explains the page as it is
   read; the user can paste their own text with the "✎ Edit text" button. Always pass a scripts folder too (create `scripts/` in the
   project if there is none), so any script you write later appears in the page without a restart.
   If the port is busy the server moves to the next free one and prints `Prompter running: <url>`:
   read that line and give the user that exact address.
4. Tell the user to open that address **in their normal browser (Chrome or Edge)** and allow
   the camera and microphone. Claude's built-in browser pane blocks camera and microphone, so do not open it there
   and do not report capture as tested from it.
5. Tell them: choose "Video + voice" or "Voice only", frame themselves in the preview (it sits right under
   the bar so the eyes stay near a camera placed above the screen), press Record (3-second countdown for video),
   Stop when done, and then just tell you "done".
6. The 📁 button lets the user save takes to any folder they choose (system folder dialog, or a typed
   path if the dialog is unavailable); ↺ returns to the default. Wherever a take is saved, the default
   recordings folder's `latest.json` points at it, so always review via the default folder. A folder the user
   picks does not get an automatic `.gitignore`: if it is inside a git repository, mention that once.

## 2. Script format

Plain text. A blank line starts a new paragraph. A paragraph wrapped in `(round brackets)` is a stage
direction and shows small and grey. Write scripts in the user's language; keep each paragraph short.

The page re-reads its script files every 2 seconds while not recording. So when the user asks you to write
or change a script, write it to a `.txt` file that the server was started with (or inside a `--script` folder)
and it appears on the prompter by itself; tell the user it is there. A new file in a `--script` folder shows up
in the script menu the same way.

The user can also edit any script right on the page ("✎ Edit text"). Their edits are saved back to that script file
about a second after they stop typing, so re-read the file before changing it, and build on their wording rather
than restoring yours. Text they paste with no file behind it is saved as `my-text.txt` in the first `--script`
folder (only in their browser if the server has no script folder). If you change a file while they are typing,
their text wins and your version is kept next to it as `<name>_claude_<time>.txt`: if you see one, tell the user
and offer to merge what they want from it.

## Writing a script on request

When the user asks for a script (a marketing video, a reel, an intro, a lecture opening, a self-tape, anything),
write it straight into the scripts folder as a `.txt` file with a short clear name, and tell them it is on the
prompter. Do not paste it into the chat as well unless they ask.

- **Ask only what you cannot infer.** Purpose, audience and length usually come with the request. If length is
  missing, write about 45 seconds for social video and say so.
- **Length.** Start from about 2.5 spoken words per second (150 a minute) and say it is an estimate. After the
  first take, use that person's real pace: `transcript.words_per_second` from `review.py` if there is a transcript,
  otherwise the script's word count divided by the take's `duration`.
- **Write for the mouth, not the eye.** Short sentences, one idea per paragraph; a paragraph is one breath and one
  screen. Numbers, dates and symbols written the way they are said. No lists, headings or parentheses inside spoken
  lines. Read each line in your head: cut anything that is hard to say in one go.
- **Hebrew scripts:** keep each paragraph in one direction. Write a foreign word in Hebrew letters when it is said in
  Hebrew, or give it its own short paragraph, so the line does not flip direction on the prompter.
- **Stage directions** go in their own `(round brackets)` paragraph: `(pause)`, `(look into the lens)`,
  `(smile)`, `(slower)`. Keep them few.
- **The user's voice, not a copywriter's.** If they have earlier scripts, posts or takes in the project, match their
  wording and rhythm. No slogans, no hype words, no generic tip lists. One point per script, said plainly. If it is
  meant to sell, end with one concrete next step.
- **Versions.** When the user is unsure of the tone, write two files side by side (for example `-calm` and
  `-energetic`) and let them pick in the script menu.
- **For a digital twin or voice capture,** write varied natural speech instead of marketing copy: statements,
  questions, numbers, a short story, a few emotions, and the sounds of the user's language, about 2 to 3 minutes.
- **After a take,** if the user wants changes, edit the same file. The page picks it up within 2 seconds, unless
  they are recording or editing.

## Recovery

Each take streams to disk every second while recording. If the browser or computer dies mid-take, the next
server start moves the unfinished take into the recordings folder as `<time>_recovered_original.webm`.
If a save fails, the page shows a "Save again" button.

## 3. Review a take ("done", "צילמתי", "check it")

1. Run `python -I "${CLAUDE_SKILL_DIR}/app/review.py" "<recordings folder>"`. It reads `latest.json`, prints JSON,
   and writes `<take>_sheet.jpg` (8 frames) and `<take>_mid.jpg` (full-resolution middle frame).
   Check `recent_takes`: if the newest take is a few seconds of voice right after a longer video take,
   it was probably accidental; review the video too (pass its file path) and say which take you reviewed.
2. **Look at both images** with the Read tool. Judge only what you can see and what the numbers say;
   label any guess as a guess.
3. Report in the user's language, short, problems first, each with one concrete fix:
   - **Framing:** eyes on the upper third line; chest to just above the head; face centred; not too much empty wall.
   - **Camera height and eye line:** camera at eye level; eyes on the lens, not dropping to the text.
   - **Light:** soft light on the face from the front or 45°; no window behind; no mixed yellow/daylight.
   - **Sharpness:** in `_mid.jpg`, can you see eyelashes, stubble, teeth edges? `blur_centre` is only a hint
     (lower is sharper; around 5 is crisp, 7 to 8 is a typical soft webcam, 9+ is soft). Content changes it.
   - **Sound:** `mean_volume_db` around -20 to -16 is healthy for speech; below -30 is too quiet.
     `speech_to_noise_db` under 20 means the room or the mic is the problem: move the mic closer.
   - **Background and clothes:** plain wall, solid shirt; busy patterns and cushions confuse AI tools.
4. **Transcript (optional).** If `faster-whisper` is installed, the JSON has a `transcript` block: the language and
   model, `words_per_second`, `long_pauses`, a `_transcript.txt` and an `.srt` subtitle file next to the take, and,
   when the script that was on screen is known, `vs_script`: the share of script words that were said and the
   paragraphs that were mostly skipped. Use it to tell the user what they skipped or changed, and whether the pace
   suits the purpose. If it has `suspect`, do not report the words as what was said; say the transcript failed.
   The server records which script was on screen in `latest.json`; pass `--script <file>` to compare with another.
   If the block says `available: false`, offer once, in one line: transcription runs on this computer, needs
   `pip install faster-whisper` and a model download of about a gigabyte or more the first time (from Hugging Face;
   nothing of the take is sent), and it is much faster with an NVIDIA GPU. Install only if the user says yes, and do
   not offer again in the same conversation. Hebrew uses the `ivrit-ai` model, other languages stock Whisper; the
   language comes from the script, or pass `--language`. `--no-transcript` skips it.
5. If the take is meant for a **digital twin, avatar or lip-sync tool**, also say plainly whether it is usable
   for that: frontal face, at least about a quarter of the frame width, sharp mouth and teeth, steady head,
   continuous talking. If it is not, say so and give the fixes; a phone on a stand at eye level facing a window
   usually beats a laptop webcam.

## Privacy rules (always)

- Recordings hold the user's face and voice. They stay in the recordings folder on this computer.
- Reviewing a take means you Read two still images from it, so those frames enter the conversation with the
  model. Never describe the tool as "nothing leaves the computer". The first time you review, say in one line
  that you are looking at a few still frames; if the user does not want that, review from the numbers only.
- A transcript is made on this computer, but its words enter the conversation when you read them, like the frames.
- Never upload a take to any service, model or vendor unless the user explicitly approves that specific
  vendor for that specific take.
- The server binds to `127.0.0.1`, refuses requests from other origins, and only ever reads the script files
  it was started with.

## Stopping

When the user is finished, stop the background server process you started.
