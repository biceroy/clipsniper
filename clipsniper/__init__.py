"""ClipSniper: find the big slot wins and hype moments in a Kick VOD, review them in a local app, cut the clips you want.

`clipsniper` opens the app in your browser. `clipsniper --help` lists the headless flags.
"""
import argparse, base64, getpass, glob, json, os, re, shutil, socket, subprocess, sys, threading, time, traceback
import urllib.error, urllib.parse, urllib.request, webbrowser, zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal

import numpy as np
import anthropic
import httpx2 as httpx  # anthropic 1.x depends on httpx2; used for the transcription upload
from pydantic import BaseModel

__version__ = "0.3.0"
UA = f"Mozilla/5.0 clipsniper/{__version__}"
SR = 8000            # audio sample rate for the loudness scan; peaks() windows on it, so cached audio.pcm files depend on it
# ponytail: whisper gets this 8 kHz audio as-is and resamples. Bump to 16000 (and store "sr" in meta.json) if transcripts read poorly.
WIN = 1.0            # loudness window (s)
BASELINE_S = 300     # rolling baseline (s)
MIN_GAP = 75         # peaks closer than this collapse into one (s)
THRESH_DB = 6        # a peak must be this far above the baseline
FRAME_EVERY = 30     # periodic frames (s); they also make the filmstrip in the app
DEFAULT_MODEL = "claude-sonnet-5"
MODELS = ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5")
PRICES = {"claude-haiku-4-5": (1, 5), "claude-sonnet-5": (2, 10), "claude-opus-5": (5, 25)}  # ponytail: $/MTok as of 2026-09, update by hand
IMG_TOK = 520        # tokens per 480p frame, roughly
CHUNK_S = 600        # transcription upload chunk (s): 10 min of 8 kHz wav is 9.6 MB, under both 25 MB limits
TALK_WIN = 900       # transcript window per model call (s)
LOOK_N = 6           # periodic frames per lookahead call
BONUS = ("bonus_trigger", "bonus", "bonus_end")
BIG_BANNERS = ("MEGA", "EPIC", "SENSATIONAL", "INSANE", "LEGENDARY", "ULTRA")
BUY_MULT = 5         # a bought bonus is a big win only if it also pays this many times its price; the base bet is not the stake
FUNNY_LAUGH = 8      # laugh score (frames or transcript) that makes a moment funny
FUNNY_HYPE = 9       # spoken-reaction score that makes a moment funny on its own
PRE_N = 7            # periodic frames before a would-be big win checked for a bonus buy (PRE_N * FRAME_EVERY s)
FFMPEG_ZIP = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-lgpl.zip"
APPDIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config") / "clipsniper"
CONFIG = APPDIR / "config.json"
APP_HTML = Path(__file__).parent / "app.html"  # also right inside the PyInstaller bundle (--add-data clipsniper/app.html;clipsniper)
FFMPEG = "ffmpeg"    # resolved by find_ffmpeg()
OUT = Path("out")    # set from --out
SPLIT_TOP = 768      # cam panel height in the 1080x1920 split layout; the game gets the other 1152 px
# ponytail: fixed 40/60 split, make it a setting if a channel's cam needs more room
CAP_Y = {"blur": 1450, "split": SPLIT_TOP}  # caption centre line: under the game, or on the cam/game seam


# ---------------------------------------------------------------- Kick

def get(url):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30).read().decode()
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise RuntimeError("Kick blocked the request. Try again in a bit, or check for a newer clipsniper.")
        if e.code == 404:
            raise RuntimeError(f"Kick says not found: {url}")
        raise


def list_vods(channel):
    """Finished VODs, newest first. The v2 API has the HLS source; the web v1 API has the page id and expiry. Joined on start time."""
    vods = [v for v in json.loads(get(f"https://kick.com/api/v2/channels/{channel}/videos")) if not v["is_live"]]
    pages = {}
    if vods:
        try:
            for w in json.loads(get(f"https://web.kick.com/api/v1/channels/{vods[0]['channel_id']}/videos"))["data"]:
                pages[w["start_time"][:19].replace("T", " ")] = w
        except Exception:
            pass  # links fall back to the channel's videos page
    for v in vods:
        w = pages.get(v["start_time"][:19], {})
        v["page_id"], v["expires_at"] = w.get("id"), (w.get("original_expires_at") or "")[:10]
    return vods


def pick_vod(vods, key):
    """key: None/'latest' -> newest, '3' -> list number, 'YYYY-MM-DD' -> that day, else VOD id."""
    if not vods:
        raise RuntimeError("No finished VODs on this channel. Kick only lists the most recent ones.")
    if key in (None, "", "latest"):
        return vods[0]
    key = str(key)
    if key.isdigit() and int(key) <= len(vods):
        return vods[int(key) - 1]
    for v in vods:
        if str(v["id"]) == key or v["created_at"].startswith(key):
            return v
    raise RuntimeError(f"No VOD matching {key!r}. Available: " + ", ".join(v["created_at"][:10] for v in vods))


def rendition(master, want="480p30"):
    lines = get(master).splitlines()
    rel = next((l for l in lines if l.startswith(want)), next(l for l in lines if l and not l.startswith("#")))
    return master.rsplit("/", 1)[0] + "/" + rel


def vod_page(channel, vod):
    return f"https://kick.com/{channel}/videos/{vod['page_id']}" if vod.get("page_id") else f"https://kick.com/{channel}/videos"


# ---------------------------------------------------------------- ffmpeg

def find_ffmpeg(assume_yes, say=print):
    if p := shutil.which("ffmpeg"):
        return p
    local = APPDIR / "ffmpeg" / "ffmpeg.exe"
    if local.exists():
        return str(local)
    if sys.platform != "win32":
        raise RuntimeError("ffmpeg not found. Install it (brew install ffmpeg / apt install ffmpeg) and run again.")
    if not assume_yes and input("ffmpeg not found. Download the LGPL build from BtbN (~170 MB zip, ~50 MB kept)? [Y/n] ").strip().lower().startswith("n"):
        raise RuntimeError("ffmpeg is required. Install it with: winget install ffmpeg")
    local.parent.mkdir(parents=True, exist_ok=True)
    zpath = local.parent / "ffmpeg.zip"
    say("ffmpeg not found, downloading the LGPL build from BtbN (~170 MB)…")
    urllib.request.urlretrieve(FFMPEG_ZIP, zpath)
    with zipfile.ZipFile(zpath) as z:
        local.write_bytes(z.read(next(n for n in z.namelist() if n.endswith("bin/ffmpeg.exe"))))
    zpath.unlink()
    say("ffmpeg ready")
    return str(local)


def ff(*args):
    return [FFMPEG, "-y", "-loglevel", "error", "-user_agent", UA, *args]


def extract(url, out, total_s, limit_min=0, progress=None):
    """One pass: mono pcm audio + a jpeg every FRAME_EVERY seconds. Keyframes only, so ~20x realtime."""
    (out / "frames").mkdir(parents=True, exist_ok=True)
    limit = ["-t", str(limit_min * 60)] if limit_min else []
    total_min = (min(total_s, limit_min * 60) if limit_min else total_s) / 60
    cmd = ff("-nostats", "-progress", "pipe:1", "-skip_frame", "nokey", *limit, "-i", url,
             "-map", "0:a", "-ac", "1", "-ar", str(SR), "-f", "s16le", str(out / "audio.pcm"),
             "-map", "0:v", "-vf", f"fps=1/{FRAME_EVERY}", "-q:v", "4", str(out / "frames" / "%05d.jpg"))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in p.stdout:
        if line.startswith("out_time_us=") and line[12:].strip().isdigit():
            done = int(line[12:]) / 6e7
            print(f"\r  extracting {done:6.1f}/{total_min:.0f} min", end="", flush=True)
            if progress:
                progress(done, total_min)
    p.wait()
    print()
    if p.returncode:
        (out / "audio.pcm").unlink(missing_ok=True)
        raise RuntimeError("ffmpeg failed while reading the VOD. Is it still available on Kick?")


def peaks(pcm_path, top, thresh=THRESH_DB, min_gap=MIN_GAP):
    """Seconds where loudness jumps above the rolling median. Returns [(t, dB_above_baseline)], sorted by time."""
    a = np.fromfile(pcm_path, dtype=np.int16).astype(np.float32)
    n = int(SR * WIN)
    a = a[: len(a) // n * n].reshape(-1, n)
    db = 20 * np.log10(np.sqrt((a ** 2).mean(1)) + 1)
    base = np.array([np.median(db[max(0, i - BASELINE_S): i + 1]) for i in range(len(db))])
    score = db - base
    picked = []
    for i in np.argsort(-score):
        if score[i] < thresh or len(picked) >= top:
            break
        t = i * WIN
        if all(abs(t - p[0]) > min_gap for p in picked):
            picked.append((float(t), float(score[i])))
    return sorted(picked)


def frame_at(url, t, path):
    if not path.exists():
        subprocess.run(ff("-ss", str(t), "-i", url, "-frames:v", "1", "-q:v", "3", str(path)), check=True)
    return base64.standard_b64encode(path.read_bytes()).decode()


def best_rendition(master):
    """Top variant of a master playlist by height, then frame rate. Returns (playlist url, label like '1080p60')."""
    lines = get(master).splitlines()
    best = None
    for i, l in enumerate(lines):
        if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
            h = re.search(r"RESOLUTION=\d+x(\d+)", l)
            f = re.search(r"FRAME-RATE=([\d.]+)", l)
            key = (int(h.group(1)) if h else 0, float(f.group(1)) if f else 0.0)
            if best is None or key > best[0]:
                best = (key, lines[i + 1].strip())
    if best is None:
        return rendition(master), "source"
    (h, fps), rel = best
    return master.rsplit("/", 1)[0] + "/" + rel, f"{h}p{fps:g}"


def cut_clip(playlist, start, length, path):
    """Stream copy: bit-exact video and audio from the source rendition, no re-encode. -ss before -i lands on the
    keyframe at or before `start` (Kick keys every ~2 s), so the clip may begin a moment early, never late."""
    subprocess.run(ff("-ss", str(max(0, start)), "-i", playlist, "-t", str(length), "-c", "copy",
                      "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(path)), check=True)


def crop_of(box):
    """[x, y, w, h] as fractions of the frame -> an ffmpeg crop that works on any rendition. Rejects anything else (it lands in a filter string)."""
    x, y, w, h = (float(v) for v in box)
    if not (0 <= x and 0 <= y and 0.02 <= w and 0.02 <= h and x + w <= 1.001 and y + h <= 1.001):
        raise RuntimeError("Bad layout box. Reset the layout and try again.")
    return f"crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f}"


def vertical_filter(layout=None, ass=None):
    """1080x1920. blur: the 16:9 frame over a blurred, zoomed copy of itself. split: the cam box on top, the game box below."""
    if (layout or {}).get("mode") == "split":
        vf = (f"split[a][b];[a]{crop_of(layout['cam'])},scale=1080:{SPLIT_TOP},setsar=1[t];"
              f"[b]{crop_of(layout['game'])},scale=1080:{1920 - SPLIT_TOP},setsar=1[g];[t][g]vstack")
    else:
        vf = ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:4[bg];"
              "[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    return vf + (f",ass={ass}" if ass else "")


def cut_vertical(playlist, start, length, path, layout=None, ass=None):
    """Re-encode (unlike cut_clip) to 1080x1920 for TikTok/Shorts. `ass` is a captions file next to `path`; ffmpeg runs
    there so the filter gets a bare file name (a Windows drive colon breaks filter syntax)."""
    subprocess.run(ff("-ss", str(max(0, start)), "-i", playlist, "-t", str(length), "-filter_complex", vertical_filter(layout, ass),
                      "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                      "-movflags", "+faststart", path.name), check=True, cwd=path.parent)


def ass_captions(words, y):
    """Burned-in captions from word timings: up to 3 words a line, white caps with a black outline, centred on height y."""
    lines, cur = [], []
    for w in words:
        if cur and (len(cur) == 3 or w["start"] - cur[-1]["end"] > 0.6):
            lines.append(cur)
            cur = []
        cur.append(w)
    lines += [cur] if cur else []
    ts = lambda t: f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"
    ev = []
    for i, l in enumerate(lines):
        end = l[-1]["end"]
        if i + 1 < len(lines) and lines[i + 1][0]["start"] - end < 0.5:
            end = lines[i + 1][0]["start"]  # no flicker between lines that follow each other
        text = re.sub(r"[{}\\]", "", " ".join(w["word"].strip() for w in l)).upper()
        ev.append(f"Dialogue: 0,{ts(l[0]['start'])},{ts(max(end, l[0]['start'] + 0.2))},C,,0,0,0,,{{\\pos(540,{y})}}{text}")
    return ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
            "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: C,Arial Black,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,6,2,5,60,60,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n" + "\n".join(ev) + "\n")


# ---------------------------------------------------------------- vision

class Moment(BaseModel):
    slot: str                    # slot game name if visible, else ""
    bet: str                     # bet size as shown, else ""
    win_amount: str              # win figure as shown, else ""
    bet_num: float | None        # numeric copies, None when unreadable
    win_num: float | None
    banner: str                  # win banner text as shown, else ""
    buy_cost: float | None       # price paid when a bonus or feature is being bought, else None
    stage: Literal["base", "bonus_trigger", "bonus", "bonus_end", "lobby", "other"]
    laugh: int                   # 0-10
    what_happens: str            # one line


class Lookahead(BaseModel):
    end_frame: int
    slot: str
    win_amount: str
    win_num: float | None
    bet_num: float | None


class Buy(BaseModel):
    buy_cost: float | None


class Talk(BaseModel):
    at: str
    what_happens: str
    quote: str
    laugh: int
    hype: int


class Talks(BaseModel):
    moments: list[Talk]


PROMPT = (
    "Frames from a casino slot stream, a few seconds apart, taken where the audio spiked. "
    "Read what the screen shows; do not judge it. slot = game name. bet = bet size as shown. win_amount = the WIN or "
    "TOTAL WIN field of this spin or bonus; never the balance or credit, a bonus-hunt tracker, or the game's advertised "
    "max win (WIN OVER 21,100x BET); leave it empty rather than guess. bet_num and win_num = the same two figures as plain "
    "numbers, null when unreadable. banner = the win banner text if one shows (BIG WIN, MEGA WIN, SENSATIONAL, ...), else "
    "empty. buy_cost = the price paid if a bonus or feature is being bought or was just bought (a BUY FEATURE / BONUS BUY "
    "purchase screen, SUPER FREE SPINS, an upgrades screen with a TOTAL COST); null when the buy button is merely visible. "
    "stage: base = normal spins; bonus_trigger = scatters land or a bonus is bought; bonus = free spins or a feature in "
    "progress; bonus_end = the bonus summary or TOTAL WIN screen; lobby = menus or site browsing; other = blackjack, "
    "plinko, talking, no slot. laugh = 0-10, how funny this moment is on its own, from the frames and the streamer's "
    "words if given; 8+ only if a stranger would laugh. what_happens = one line.")

TALK_PROMPT = (
    "Transcript of a casino slot streamer, one line per segment with its timestamp. Pick only the moments a stranger would "
    "clip on their own: a joke that lands, a rant, a roast of chat, a scream over a win or a pull. Skip ads, sponsor reads, "
    "small talk and ordinary commentary. Most windows have none; at most 3. at = the h:mm:ss of the line it starts on. "
    "quote = the key words. laugh = 0-10, 8+ only if it is funny to someone outside the stream. hype = 0-10, 9+ only for "
    "a real scream over a big win or pull.")

LOOK_PROMPT = (
    "Frames from a casino slot stream, 30 s apart, after a bonus round started. Which is the first frame (1-based) "
    "showing the bonus summary / TOTAL WIN screen, or the base game after it? 0 if the bonus runs in every frame. slot = "
    "the game name shown. Read the total win and bet from that frame, or from the last in-bonus frame; numbers null when "
    "unreadable, never the balance.")

BUY_PROMPT = (
    "Frames from a casino slot stream, 30 s apart, just before a bonus round on {slot} started. buy_cost = the price the "
    "streamer paid if the bonus or a feature was bought: a BUY FEATURE / BONUS BUY / SUPER purchase screen or confirmation, "
    "an upgrades screen with a TOTAL COST, or the streamer saying what this bonus cost. null if the bonus triggered from a "
    "spin, the price only shows on an idle buy button, or the talk is about other buys. Only a price that is shown or said, never a guess.")


def ask(client, model, content, schema, max_tokens=1024):
    extra = {} if model.startswith("claude-haiku") else {"output_config": {"effort": "low"}}
    r = client.messages.parse(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": content}],
                              output_format=schema, **extra)
    return r.parsed_output, r.usage.input_tokens, r.usage.output_tokens


def img(b):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}


def classify(client, model, imgs, said=""):
    text = PROMPT + (f'\nThe streamer said around this moment: "{said}"' if said else "")
    m, i, o = ask(client, model, [*map(img, imgs), {"type": "text", "text": text}], Moment)
    return m or Moment(slot="", bet="", win_amount="", bet_num=None, win_num=None, banner="", buy_cost=None, stage="other", laugh=0,
                       what_happens="(model returned no answer)"), i, o


def same_slot(a, b):
    """Loose game-name match: 'Gates of Olympus 2500' is 'Gates of Olympus', 'Sea Men' is 'Seamen'. Unknown matches anything."""
    a, b = (re.sub(r"\W", "", s or "").lower() for s in (a, b))
    return not a or not b or a.startswith(b) or b.startswith(a)


def judge(m, cfg):
    """The model reads, this decides. big_win = win/bet >= min_mult, or win >= min_win, or a MEGA-tier banner when the numbers
    are unreadable. Never on a lobby/other screen (a stale WIN field), and a bought bonus must also pay BUY_MULT times its price."""
    bet, win, buy = m.get("bet_num") or 0, m.get("win_num") or 0, m.get("buy_cost") or 0
    mult = win / bet if bet and win else None
    big = m.get("stage") not in ("lobby", "other") and win >= BUY_MULT * buy and (
        (mult or 0) >= cfg.get("min_mult", 100) or win >= cfg.get("min_win", 1000)
        or (mult is None and any(w in (m.get("banner") or "").upper() for w in BIG_BANNERS)))
    fun = max(int(m.get("laugh") or 0), int(m.get("talk_laugh") or 0)) >= FUNNY_LAUGH or int(m.get("hype") or 0) >= FUNNY_HYPE
    kind = "big_win" if big else "funny" if fun else "bonus" if m.get("stage") in BONUS else "nothing"
    return {"kind": kind, "clip_worthy": kind in ("big_win", "funny"),
            "multiplier": "" if mult is None else f"{mult:.1f}x" if mult < 10 else f"{mult:.0f}x"}


def merge(results, gap=240):
    """Snapshots of one bonus (same slot, bonus stage, spikes <= gap s apart, no summary screen seen yet) collapse into one
    entry: the loudest spike is the moment and keeps its clip range, the biggest total seen is the win. A later spike on the
    same slot whose WIN field still shows that total is the same win, not a second one."""
    # ponytail: a same-slot rebuy within 4 min that never showed its summary folds in too; compare bet_num if that bites.
    out = []
    for r in results:
        p = out[-1] if out else None
        near = p and (r.get("slot") or p.get("slot")) and same_slot(r.get("slot"), p.get("slot")) and r["t"] - max(p.get("merged") or [p["t"]]) <= gap
        if near and ((r.get("stage") in BONUS and p.get("stage") in BONUS and p.get("stage") != "bonus_end")
                     or (r.get("win_num") and r.get("win_num") == p.get("win_num"))):
            loud, big = max(p, r, key=lambda x: x.get("score") or 0), max(p, r, key=lambda x: x.get("win_num") or 0)
            out[-1] = {**loud, "stage": r["stage"] if r.get("stage") in BONUS else p["stage"], "win_num": big.get("win_num"), "win_amount": big.get("win_amount", ""),
                       "buy_cost": max(p.get("buy_cost") or 0, r.get("buy_cost") or 0) or None,
                       "merged": (p.get("merged") or [p["t"]]) + [r["t"]]}
        else:
            out.append(r)
    return out


def lookahead(client, model, out, t, until=float("inf"), slot=""):
    """Walk the periodic frames after a bonus spike to its summary screen, never past `until` (the next bonus or game) and
    only trusting totals read on the same slot. Returns (end_t or None, win_num, bet_num, win_amount, tin, tout)."""
    k, tin, tout, best = int(t) // FRAME_EVERY + 2, 0, 0, (None, None, "")
    for _ in range(3):
        files = [f for f in (out / "frames" / f"{j:05d}.jpg" for j in range(k, k + LOOK_N) if (j - 1) * FRAME_EVERY < until) if f.exists()]
        if not files:
            break
        la, i, o = ask(client, model, [*(img(base64.standard_b64encode(f.read_bytes()).decode()) for f in files),
                                       {"type": "text", "text": LOOK_PROMPT}], Lookahead, 512)
        tin, tout = tin + i, tout + o
        if not la:
            break
        if la.win_num and la.win_num > (best[0] or 0) and same_slot(la.slot, slot):
            best = (la.win_num, la.bet_num, la.win_amount)
        if 1 <= la.end_frame <= len(files):
            return (int(files[la.end_frame - 1].stem) - 1) * FRAME_EVERY, *best, tin, tout  # frame k sits at (k-1)*FRAME_EVERY
        k += LOOK_N
    return None, *best, tin, tout


def bought(client, model, out, t, slot, talk=""):
    """Was the bonus that started around t bought? Reads the PRE_N periodic frames before it and what the streamer said
    (a long bonus starts well before its loudest spike, and the frames miss the purchase). Returns (price or None, tin, tout)."""
    j0 = int(t) // FRAME_EVERY + 1  # frame j sits at (j-1)*FRAME_EVERY, so this one is at or just before t
    files = [f for f in (out / "frames" / f"{j:05d}.jpg" for j in range(max(1, j0 - PRE_N + 1), j0 + 1)) if f.exists()]
    if not files:
        return None, 0, 0
    text = BUY_PROMPT.format(slot=slot or "a slot") + (f'\nThe streamer said in the minutes before: "{talk}"' if talk else "")
    b, i, o = ask(client, model, [*(img(base64.standard_b64encode(f.read_bytes()).decode()) for f in files), {"type": "text", "text": text}], Buy, 256)
    return (b.buy_cost or None) if b else None, i, o


def stt_api(key):
    """(url, model, $/s): Groq for gsk_ keys, else OpenAI."""
    return (("https://api.groq.com/openai/v1/audio/transcriptions", "whisper-large-v3-turbo", 0.04 / 3600) if key.startswith("gsk_")
            else ("https://api.openai.com/v1/audio/transcriptions", "whisper-1", 0.006 / 60))


def whisper(f, key, say=print, words=False):
    """One wav file -> verbose_json, waiting out rate limits. words=True adds per-word timings."""
    url, model, _ = stt_api(key)
    data = {"model": model, "response_format": "verbose_json", **({"timestamp_granularities[]": "word"} if words else {})}
    for _ in range(6):
        r = httpx.post(url, headers={"Authorization": f"Bearer {key}"}, data=data,
                       files={"file": (f.name, f.read_bytes(), "audio/wav")}, timeout=600)
        if r.status_code != 429:
            break
        wait = float(r.headers.get("retry-after") or 30)
        say(f"Transcription rate limit, waiting {wait:.0f} s")
        time.sleep(wait)
    if r.status_code == 401:
        raise RuntimeError("The transcription API key was rejected. Check it in Settings.")
    r.raise_for_status()
    return r.json()


def transcribe(out, key, say=print):
    """Whole-VOD transcript from the cached audio: ([{s, e, text}], dollars)."""
    cache = out / "transcript.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8")), 0.0
    per_s = stt_api(key)[2]
    d = out / "chunks"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir()
    segs = []
    try:
        # not ff(): its -user_agent is an HTTP option and ffmpeg rejects it for a local input
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", str(out / "audio.pcm"),
                        "-f", "segment", "-segment_time", str(CHUNK_S), str(d / "%03d.wav")], check=True)
        files = sorted(d.glob("*.wav"))
        for k, f in enumerate(files):
            say(f"Transcribing {k + 1}/{len(files)}")
            segs += [{"s": round(g["start"] + k * CHUNK_S, 2), "e": round(g["end"] + k * CHUNK_S, 2), "text": g["text"].strip()}
                     for g in whisper(f, key, say).get("segments") or []]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    cache.write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    return segs, len(files) * CHUNK_S * per_s


def said(segs, a, b):
    return " ".join(g["text"] for g in segs if g["e"] >= a and g["s"] <= b)


def transcript_moments(client, model, segs, say=print):
    """Clip-on-their-own moments picked from the transcript. Returns ([{t, quote, laugh, hype}], tin, tout)."""
    picks, tin, tout = [], 0, 0
    for a in range(0, int(segs[-1]["e"]) + 1 if segs else 0, TALK_WIN):
        lines = [f"[{hms(g['s'])}] {g['text']}" for g in segs if a <= g["s"] < a + TALK_WIN]
        if not lines:
            continue
        say(f"Reading the transcript at {hms(a)}")
        r, i, o = ask(client, model, [{"type": "text", "text": TALK_PROMPT + "\n\n" + "\n".join(lines)}], Talks, 2048)
        tin, tout = tin + i, tout + o
        for m in (r.moments if r else []):
            try:
                t = sum(int(x) * 60 ** n for n, x in enumerate(reversed(m.at.strip().strip("[]").split(":"))))
            except ValueError:
                continue
            picks.append({"t": float(t), "quote": m.quote, "laugh": m.laugh, "hype": m.hype})
    return picks, tin, tout


def estimate(n, model, minutes=0, tkey=""):
    pin, pout = PRICES.get(model, PRICES["claude-opus-5"])
    usd = (n * (6 * IMG_TOK + 400) * pin + n * 180 * pout) / 1e6  # 3 spike frames + a share of the lookahead and buy checks
    if tkey:
        usd += (minutes * 130 * pin + minutes * 20 * pout) / 1e6 + (minutes / 60 * 0.04 if tkey.startswith("gsk_") else minutes * 0.006)
    return usd


def cost(tin, tout, model):
    pin, pout = PRICES.get(model, PRICES["claude-opus-5"])
    return (tin * pin + tout * pout) / 1e6


# ---------------------------------------------------------------- pipeline

def hms(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def slug(s):
    return re.sub(r"[^\w-]+", "_", s).strip("_")[:40] or "clip"


def load_config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def save_config(cfg):
    APPDIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=1))
    return cfg


class Job:
    """One scan of one VOD. Runs analyze() on the app's single worker thread (ffmpeg-bound, so one at a time), or inline for the CLI."""
    jobs = {}
    queue = []          # [running, queued...]; a job stays at the head while it runs
    lock = threading.Lock()
    worker = None

    def __init__(self, channel, vod, model, top, api_key, limit_min=0, gate=None, cfg=None):
        self.channel, self.vod, self.model, self.top, self.api_key, self.limit_min = channel, vod, model, top, api_key, limit_min
        self.cfg = cfg or {}
        self.gate = gate or (lambda n, est: True)
        self.date = vod["created_at"][:10]
        self.run = f"{channel}-{self.date}"
        self.out = OUT / self.run
        self.stage, self.pct, self.message, self.log, self.cost = "queued", 0, "", [], 0.0
        Job.jobs[self.run] = self

    def say(self, m):
        self.message = m
        self.log = (self.log + [m])[-300:]
        print(m)

    def status(self):
        return {"run": self.run, "stage": self.stage, "pct": round(self.pct), "message": self.message, "log": self.log[-60:], "cost": self.cost,
                "ahead": Job.queue.index(self) if self in Job.queue else 0, "title": self.vod["session_title"], "minutes": self.vod["duration"] // 60000}

    def start(self):
        with Job.lock:
            Job.queue.append(self)
            if not Job.worker:
                Job.worker = threading.Thread(target=Job.work, daemon=True)
                Job.worker.start()

    @staticmethod
    def work():
        while True:
            with Job.lock:
                if not Job.queue:
                    Job.worker = None
                    return
                job = Job.queue[0]
            job.go()
            with Job.lock:
                Job.queue.pop(0)

    def go(self):
        try:
            analyze(self)
            self.stage, self.pct = "done", 100
        except Exception as e:
            self.stage, self.message = "error", str(e)
            traceback.print_exc()


def analyze(job):
    global FFMPEG
    vod, out, model = job.vod, job.out, job.model
    out.mkdir(parents=True, exist_ok=True)
    job.stage = "starting"
    job.say(f"{job.channel} · {job.date} · {vod['duration'] // 60000} min · {vod['session_title'][:60]}")

    client = anthropic.Anthropic(api_key=job.api_key)
    try:
        client.models.retrieve(model)
    except anthropic.AuthenticationError:
        raise RuntimeError("Anthropic rejected the API key. Check it in Settings.")
    except anthropic.NotFoundError:
        raise RuntimeError(f"Unknown model {model!r}. Use one of: {', '.join(MODELS)}")

    FFMPEG = find_ffmpeg(True, job.say)
    url = rendition(vod["source"])
    if not (out / "audio.pcm").exists():
        job.stage = "extracting"
        def prog(done, total):
            job.pct = 70 * done / max(total, 1)
            job.message = f"Extracting audio and frames: {done:.0f} / {total:.0f} min"
        extract(url, out, vod["duration"] / 1000, job.limit_min, prog)
        job.say("Extraction done")
    else:
        job.say("Audio and frames already cached, skipping extraction")
    cfg, tkey = job.cfg, (job.cfg.get("transcribe_key") or "").strip()
    minutes = (min(vod["duration"] / 1000, job.limit_min * 60) if job.limit_min else vod["duration"] / 1000) / 60
    cands = [{"t": t, "score": round(score, 1), "source": "audio"} for t, score in peaks(out / "audio.pcm", job.top)]
    est = estimate(len(cands) + 5, model, minutes, tkey)
    note = f" (incl. transcript on {'Groq' if tkey.startswith('gsk_') else 'OpenAI'})" if tkey else ""
    job.say(f"{len(cands)} audio spikes. Estimated API cost ≈ ${est:.2f} on {model}{note}.")
    if not cands:
        raise RuntimeError("Nothing stood out in the audio.")
    if not job.gate(len(cands), est):
        raise RuntimeError("Cancelled before the vision step.")

    segs, stt, tin, tout = [], 0.0, 0, 0
    if tkey:
        job.stage, job.pct = "transcribing", 70
        segs, stt = transcribe(out, tkey, job.say)
        job.say(f"Transcript: {len(segs)} segments" + (f", ${stt:.2f}" if stt else " (cached)"))
        job.pct = 76
        picks, tin, tout = transcript_moments(client, model, segs, job.say)
        new = tagged = 0
        for p in picks:  # a pick near a spike tags that spike with its quote and scores, else it becomes its own candidate
            if p["laugh"] < FUNNY_LAUGH and p["hype"] < FUNNY_HYPE:
                continue
            near = min(cands, key=lambda c: abs(c["t"] - p["t"]))
            if abs(near["t"] - p["t"]) > 60:
                cands.append({"t": p["t"], "score": 0, "source": "transcript", "quote": p["quote"], "talk_laugh": p["laugh"], "hype": p["hype"]})
                new += 1
            elif max(p["laugh"], p["hype"]) > max(near.get("talk_laugh", 0), near.get("hype", 0)):
                near.update(quote=p["quote"], talk_laugh=p["laugh"], hype=p["hype"])
                tagged += 1
        cands.sort(key=lambda c: c["t"])
        job.say(f"{new} extra moments from the transcript, {tagged} spikes tagged with what was said")
    job.cost = stt + cost(tin, tout, model)

    job.stage = "classifying"
    results = []
    for k, c in enumerate(cands, 1):
        t = c["t"]
        job.pct = 78 + 16 * k / len(cands)
        imgs = [frame_at(url, t + d, out / "frames" / f"peak_{int(t)}_{d:+d}.jpg") for d in (-2, 3, 8)]
        m, i, o = classify(client, model, imgs, said(segs, t - 15, t + 15))
        tin, tout = tin + i, tout + o
        job.cost = stt + cost(tin, tout, model)
        r = {**c, "time": hms(t), **m.model_dump(), "quote": c.get("quote") or said(segs, t - 5, t + 10)[:140], "start": max(0, t - 20), "end": t + 30}
        j = judge(r, cfg)
        results.append(r)
        job.say(f"{'★' if j['clip_worthy'] else ' '} {hms(t)}  {j['kind']:8s} {m.stage:13s} {m.slot} {m.win_amount} {j['multiplier']}".rstrip())

    results = merge(results)
    bonuses = [r for r in results if r["stage"] in BONUS]
    job.stage = "bonus totals"
    for k, r in enumerate(bonuses, 1):
        job.pct = 94 + 5 * k / len(bonuses)
        # the walk stops at the next bonus, or at the next spike on another game: this bonus is over by then
        until = next((x["t"] for x in results if x["t"] > r["t"] and (x["stage"] in BONUS or not same_slot(x.get("slot"), r.get("slot")))), float("inf"))
        end_t, win, bet, amount, i, o = lookahead(client, model, out, max(r.get("merged") or [r["t"]]), until, r.get("slot"))
        if win and win > (r.get("win_num") or 0):
            r.update(win_num=win, bet_num=r.get("bet_num") or bet, win_amount=amount or r["win_amount"])
        if end_t is not None and end_t <= r["end"] + 60:  # the summary joins the clip only when it is close; the range stays a clip, not the whole bonus
            r["end"] = max(r["end"], end_t + 8)
        tin, tout = tin + i, tout + o
        job.cost = stt + cost(tin, tout, model)
        job.say(f"  {r['time']} {r['slot']} bonus total {r['win_amount'] or '?'}" + (f", ends {hms(end_t)}" if end_t is not None else ""))
    results = merge(results)  # again: a later spike still showing a bonus total the lookahead just read is the same win
    for r in results:  # only a would-be big win pays for the buy check: a bought bonus is measured against its price
        if not r.get("buy_cost") and judge(r, cfg)["kind"] == "big_win":
            t0 = min(r.get("merged") or [r["t"]])
            r["buy_cost"], i, o = bought(client, model, out, t0, r.get("slot"), said(segs, t0 - 300, t0))
            tin, tout = tin + i, tout + o
            job.cost = stt + cost(tin, tout, model)
            if r["buy_cost"]:
                job.say(f"  {r['time']} {r['slot']} bought for {r['buy_cost']:g}")
    for r in results:
        r.update(judge(r, cfg))

    worthy = [r for r in results if r["clip_worthy"]]
    (out / "moments.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    md = [f"# {job.channel} {job.date} — {vod['duration'] // 60000} min · {model} · ${job.cost:.2f}", "", "| time | kind | slot | bet | win | mult | what |", "|---|---|---|---|---|---|---|"]
    md += [f"| [{r['time']}]({vod_page(job.channel, vod)}?t={int(r['t'])}) | {r['kind']} | {r['slot']} | {r['bet']}{' (buy ' + format(r['buy_cost'], 'g') + ')' if r.get('buy_cost') else ''} | {r['win_amount']} | {r['multiplier']} | {r['what_happens']}{' “' + r['quote'] + '”' if r.get('quote') else ''} |" for r in worthy]
    (out / "moments.md").write_text("\n".join(md), encoding="utf-8")
    meta = {"run": job.run, "channel": job.channel, "date": job.date, "minutes": vod["duration"] // 60000, "title": vod["session_title"],
            "vod_id": vod["id"], "page": vod_page(job.channel, vod), "source": vod["source"], "url": url, "hls": rendition(vod["source"], "720p60"),
            "model": model, "cost": round(job.cost, 3), "total": len(results), "worthy": len(worthy),
            "frames": len([f for f in (out / "frames").glob("0*.jpg")]), "thumb": "frames/00010.jpg"}
    (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    job.say(f"{len(worthy)} clip-worthy moments. API cost ${job.cost:.2f}.")
    return results, meta


def clips_of(out):
    return sorted((p.name for p in (out / "clips").glob("*.mp4")), key=lambda n: (out / "clips" / n).stat().st_mtime) if (out / "clips").exists() else []


def storage(out):
    """Bytes under out, bytes of every */clips, number of scans. Skips what it cannot stat."""
    total = clips = 0
    for p in out.rglob("*"):
        try:
            n = p.stat().st_size if p.is_file() else 0
        except OSError:
            continue
        total += n
        clips += n if p.parent.name == "clips" else 0
    return {"bytes": total, "clips_bytes": clips, "runs": len(list(out.glob("*/meta.json"))), "path": str(out)}


def delete_run(out):
    """Remove a scan's cached audio, frames and results. Exported clips are kept; returns how many."""
    kept = len(clips_of(out))
    for name in ("audio.pcm", "frames", "transcript.json", "chunks", "moments.json", "moments.md", "meta.json"):
        p = out / name
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    if not kept:
        shutil.rmtree(out, ignore_errors=True)
    return kept


def do_cut(run, t, start, end, name="", vertical=False, layout=None, captions=False):
    out = OUT / run
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    r = next((r for r in json.loads((out / "moments.json").read_text(encoding="utf-8")) if int(r["t"]) == int(t)), {})
    base = slug(name) if name else f"{hms(start).replace(':', '-')}_{slug(r.get('slot') or r.get('kind') or 'clip')}{'_' + slug(r['multiplier']) if r.get('multiplier') else ''}"
    if vertical:
        base += "_vertical"
    (out / "clips").mkdir(exist_ok=True)
    path = out / "clips" / f"{base}.mp4"
    k = 2
    while path.exists():
        path = out / "clips" / f"{base}_{k}.mp4"; k += 1
    src, quality = best_rendition(meta["source"])
    if vertical:
        length, ass = max(2, end - start), None
        try:
            if captions:
                # the clip's own audio at 16 kHz, not the scan's 8 kHz cache: a minute costs a fraction of a cent and reads better
                key = (load_config().get("transcribe_key") or "").strip()
                if not key:
                    raise RuntimeError("Captions need a transcription key in Settings.")
                wav = path.with_suffix(".wav")
                subprocess.run(ff("-ss", str(max(0, start)), "-i", src, "-t", str(length), "-vn", "-ac", "1", "-ar", "16000", str(wav)), check=True)
                try:
                    words = whisper(wav, key, words=True).get("words") or []
                finally:
                    wav.unlink(missing_ok=True)
                ass = path.with_suffix(".ass")
                ass.write_text(ass_captions(words, CAP_Y.get((layout or {}).get("mode"), CAP_Y["blur"])), encoding="utf-8")
            cut_vertical(src, start, length, path, layout, ass and ass.name)
        finally:
            if ass:
                ass.unlink(missing_ok=True)
        quality = "1080x1920 " + ("split" if (layout or {}).get("mode") == "split" else "vertical") + (" + captions" if captions else "")
    else:
        cut_clip(src, start, max(2, end - start), path)
    print(f"  cut {path.name} ({quality})")
    return path.name, quality


def make_reel(out, files, name=""):
    """Stitch exported clips into one file. They are all stream copies of the same source, so concat needs no re-encode."""
    d = out / "clips"
    base = slug(name) if name else "reel"
    path = d / (f"{base}.mp4" if name else "reel_1.mp4")
    k = 2
    while path.exists():
        path = d / f"{base}_{k}.mp4"; k += 1
    lst = d / f"{path.stem}.txt"
    lst.write_text("".join("file '" + (d / f).resolve().as_posix().replace("'", r"'\''") + "'\n" for f in files), encoding="utf-8")
    try:
        # not ff(): its -user_agent is an HTTP option and ffmpeg rejects it for a local concat list
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(path)], check=True)
    finally:
        lst.unlink(missing_ok=True)
    print(f"  reel {path.name}")
    return path.name


# ---------------------------------------------------------------- local app

class App(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/":
                body = APP_HTML.read_bytes()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif u.path == "/hls.min.js":
                body = (APP_HTML.parent / "hls.min.js").read_bytes()
                self.send_response(200); self.send_header("Content-Type", "application/javascript")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif u.path == "/api/config":
                cfg = load_config()
                cfg["api_key"] = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
                self.send_json(cfg)
            elif u.path == "/api/vods":
                cfg = load_config()
                ch = q.get("channel", "").strip().lower().lstrip("@")
                top = int(cfg.get("top") or 60)
                vods = []
                for v in list_vods(ch):
                    run = f"{ch}-{v['created_at'][:10]}"
                    vods.append({"id": v["id"], "date": v["created_at"][:16], "title": v["session_title"], "minutes": v["duration"] // 60000,
                                 "thumb": (v.get("thumbnail") or {}).get("src", ""), "expires": v["expires_at"], "page_id": v["page_id"],
                                 "run": run, "done": (OUT / run / "meta.json").exists(), "est": {m: round(estimate(top, m, v["duration"] / 60000, cfg.get("transcribe_key") or ""), 2) for m in MODELS}})
                self.send_json(vods)
            elif u.path == "/api/runs":
                runs = []
                for m in sorted(OUT.glob("*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                    runs.append(json.loads(m.read_text(encoding="utf-8")))
                self.send_json(runs)
            elif u.path == "/api/status":
                j = Job.jobs.get(q.get("run"))
                self.send_json(j.status() if j else {"stage": "error", "message": "unknown run"})
            elif u.path == "/api/jobs":  # running, queued in order, then finished
                self.send_json([j.status() for j in Job.queue + [j for j in Job.jobs.values() if j not in Job.queue]])
            elif u.path == "/api/moments":
                out = OUT / q["run"]
                ctx = json.loads((out / "meta.json").read_text(encoding="utf-8"))
                try:
                    ctx["quality"] = best_rendition(ctx["source"])[1]  # what an export will be, e.g. 1080p60
                except Exception:
                    ctx["quality"] = ""
                self.send_json({"ctx": ctx,
                                "results": json.loads((out / "moments.json").read_text(encoding="utf-8")), "clips": clips_of(out)})
            elif u.path == "/api/storage":
                self.send_json(storage(OUT))
            elif u.path.startswith("/out/"):
                self.directory = str(OUT)
                self.path = self.path[4:]
                super().do_GET()
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            b = self.body()
            if self.path == "/api/config":
                cfg = load_config()
                cfg.update({k: b[k] for k in ("channel", "api_key", "model", "top", "min_mult", "min_win", "transcribe_key", "layouts") if k in b})
                self.send_json(save_config(cfg))
            elif self.path == "/api/run":
                cfg = load_config()
                model = b.get("model") or cfg.get("model") or DEFAULT_MODEL
                key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
                if b.get("run"):  # re-run a cached scan with another model
                    meta = json.loads((OUT / b["run"] / "meta.json").read_text(encoding="utf-8"))
                    ch, vod = meta["channel"], pick_vod(list_vods(meta["channel"]), str(meta["vod_id"]))
                else:
                    ch = b["channel"].strip().lower().lstrip("@")
                    vod = pick_vod(list_vods(ch), str(b["id"]))
                job = Job.jobs.get(f"{ch}-{vod['created_at'][:10]}")
                if not job or job.stage in ("done", "error"):  # already queued or running: hand back that one
                    job = Job(ch, vod, model, int(cfg.get("top") or 60), key, cfg=cfg)
                    job.start()
                self.send_json({"run": job.run})
            elif self.path == "/api/cut":
                name, quality = do_cut(b["run"], float(b["t"]), float(b["start"]), float(b["end"]), b.get("name", ""), bool(b.get("vertical")),
                                       b.get("layout"), bool(b.get("captions")))
                self.send_json({"file": name, "quality": quality, "clips": clips_of(OUT / b["run"])})
            elif self.path == "/api/reel":
                run, files = str(b.get("run", "")), b.get("files") or []
                if not (re.fullmatch(r"[\w.-]+", run) and run.strip(".")):
                    raise RuntimeError("Bad run name.")
                if len(files) < 2 or not all(isinstance(f, str) and re.fullmatch(r"[\w.-]+\.mp4", f) and (OUT / run / "clips" / f).is_file() for f in files):
                    raise RuntimeError("Pick at least 2 exported clips from this run.")
                self.send_json({"file": make_reel(OUT / run, files, str(b.get("name") or "")), "clips": clips_of(OUT / run)})
            elif self.path == "/api/delete":
                run = str(b.get("run", ""))
                if not re.fullmatch(r"[\w.-]+", run) or not (OUT / run / "meta.json").exists():
                    raise RuntimeError("Unknown scan.")
                job = Job.jobs.get(run)
                if job and job.stage not in ("done", "error"):
                    raise RuntimeError("That scan is still running.")
                Job.jobs.pop(run, None)
                self.send_json({"kept_clips": delete_run(OUT / run)})
            elif self.path == "/api/clip/delete":
                run, f = str(b.get("run", "")), str(b.get("file", ""))
                p = OUT / run / "clips" / f
                if not (re.fullmatch(r"[\w.-]+", run) and re.fullmatch(r"[\w.-]+\.mp4", f) and p.is_file()):
                    raise RuntimeError("Unknown clip.")
                p.unlink()
                self.send_json({"clips": clips_of(OUT / run)})
            elif self.path == "/api/open":
                d = OUT / b["run"] / "clips" if b.get("run") else OUT
                d.mkdir(parents=True, exist_ok=True)
                if hasattr(os, "startfile"):
                    os.startfile(d)
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)


def ui(port, open_browser=True):
    global FFMPEG
    try:
        FFMPEG = find_ffmpeg(True)
    except Exception as e:
        print(e)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), App)
    except OSError:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
        srv = ThreadingHTTPServer(("127.0.0.1", port), App)
    url = f"http://127.0.0.1:{port}/"
    print(f"ClipSniper {__version__} running at {url}\nClose this window (or Ctrl+C) to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()


# ---------------------------------------------------------------- CLI

def setup(cfg):
    print("Setup (saved to", CONFIG, ")")
    cfg["channel"] = input(f"Kick channel slug, the part after kick.com/ [{cfg.get('channel', '')}]: ").strip() or cfg.get("channel", "")
    key = getpass.getpass("Anthropic API key from console.anthropic.com (typing is hidden; Enter keeps the current one): ").strip()
    if key:
        cfg["api_key"] = key
    return save_config(cfg)


def parse_args():
    p = argparse.ArgumentParser(prog="clipsniper", description=__doc__.split("\n")[0])
    p.add_argument("--channel", help="Kick channel slug (default: saved config)")
    p.add_argument("--date", help="headless: scan this VOD (YYYY-MM-DD, VOD id, list number, or 'latest') instead of opening the app")
    p.add_argument("--top", type=int, help="how many audio spikes to check (default 60)")
    p.add_argument("--model", help=f"Claude model (default {DEFAULT_MODEL}; {MODELS[0]} is cheapest, {MODELS[2]} most accurate)")
    p.add_argument("--limit-min", type=int, default=0, help="only scan the first N minutes (for testing)")
    p.add_argument("--out", default="out", help="output folder (default ./out)")
    p.add_argument("--cut", choices=["none", "worthy", "all"], default="worthy", help="headless: which clips to cut (default worthy)")
    p.add_argument("--yes", "-y", action="store_true", help="headless: don't ask before spending API credits")
    p.add_argument("--port", type=int, default=8765, help="port for the local app (default 8765)")
    p.add_argument("--no-browser", action="store_true", help="start the app without opening a browser tab")
    p.add_argument("--list", action="store_true", help="list recent VODs and exit")
    p.add_argument("--setup", action="store_true", help="enter channel and API key in the terminal")
    p.add_argument("--version", action="version", version=f"clipsniper {__version__}")
    return p.parse_args()


def run(args):
    global OUT
    OUT = Path(args.out).resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    if args.setup:
        cfg = setup(cfg)
    if not (args.list or args.date):
        return ui(args.port, not args.no_browser)

    channel = (args.channel or cfg.get("channel") or "").strip().lower().lstrip("@")
    if not channel:
        raise RuntimeError("No channel. Pass --channel or run `clipsniper --setup` once.")
    vods = list_vods(channel)
    if args.list:
        print(f"\nRecent VODs on kick.com/{channel}:")
        for i, v in enumerate(vods, 1):
            print(f"  {i:2d}  {v['created_at'][:16]}  {v['duration'] // 60000:4d} min  {v['session_title'][:50]}" + (f"  (gone {v['expires_at']})" if v["expires_at"] else ""))
        return
    api_key = os.environ.get("ANTHROPIC_API_KEY") or cfg.get("api_key")
    if not api_key:
        raise RuntimeError("No API key. Set ANTHROPIC_API_KEY or run `clipsniper --setup` once.")
    interactive = sys.stdin.isatty() and not args.yes
    gate = (lambda n, est: not input("  Continue? [Y/n] ").strip().lower().startswith("n")) if interactive else None
    job = Job(channel, pick_vod(vods, args.date), args.model or cfg.get("model") or DEFAULT_MODEL,
              args.top or int(cfg.get("top") or 60), api_key, args.limit_min, gate, cfg)
    global FFMPEG
    FFMPEG = find_ffmpeg(args.yes)
    results, meta = analyze(job)
    chosen = {"none": [], "worthy": [r for r in results if r["clip_worthy"]], "all": results}[args.cut]
    for r in chosen:
        do_cut(job.run, r["t"], r.get("start", r["t"] - 35), r.get("end", r["t"] + 30))
    print(f"Saved to {job.out}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        print("\nstopped")
    except RuntimeError as e:
        print(e)
    except Exception:
        traceback.print_exc()
    if getattr(sys, "frozen", False):
        try:
            input("\nPress Enter to close")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
