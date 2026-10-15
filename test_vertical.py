"""Self-check for the vertical layouts and burned-in captions. Renders a 3 s test pattern through ffmpeg when it is installed."""
import shutil, subprocess, tempfile, threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import clipsniper as cs

assert cs.crop_of([0.1, 0.2, 0.3, 0.4]) == "crop=iw*0.3000:ih*0.4000:iw*0.1000:ih*0.2000"
for bad in ([0.8, 0, 0.3, 0.3], [0, 0, 0, 0.3], [-0.1, 0, 0.3, 0.3], ["1;drawtext", 0, 0.3, 0.3]):
    try:
        cs.crop_of(bad)
        raise AssertionError(bad)
    except (RuntimeError, ValueError):
        pass
split = {"mode": "split", "cam": [0, 0, 0.3, 0.38], "game": [0.27, 0.07, 0.46, 0.87]}
assert "vstack" in cs.vertical_filter(split) and "boxblur" in cs.vertical_filter({"mode": "blur"}) and "boxblur" in cs.vertical_filter()
assert cs.vertical_filter(None, "c.ass").endswith(",ass=c.ass")

w = lambda word, s, e: {"word": word, "start": s, "end": e}
ass = cs.ass_captions([w("no", 0, .3), w("way", .3, .6), w("that's", .6, .9), w("{1000x}", 1.0, 1.4), w("LET'S", 3, 3.3), w("go", 3.3, 3.6)], 768)
ev = [l for l in ass.splitlines() if l.startswith("Dialogue")]
assert len(ev) == 3, ev                                     # 3 words, then 1 word cut off by the 1.6 s pause, then 2
assert ev[0].startswith("Dialogue: 0,0:00:00.00,0:00:01.00") and ev[0].endswith("NO WAY THAT'S"), ev[0]  # held to the next line
assert ev[1].endswith("}1000X") and "\\pos(540,768)" in ev[1], ev[1]  # braces stripped, override kept
assert cs.ass_captions([], 768).count("Dialogue") == 0

if shutil.which("ffmpeg"):
    cs.FFMPEG = shutil.which("ffmpeg")
    d = Path(tempfile.mkdtemp())
    src = d / "src.ts"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=3", "-f", "lavfi", "-i", "sine=d=3",
                    "-shortest", str(src)], check=True)
    (d / "c.ass").write_text(ass, encoding="utf-8")
    # ff() always sends -user_agent, an HTTP-only option, so serve the source the way Kick does
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(d)))
    SimpleHTTPRequestHandler.log_message = lambda *a: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for name, lay in (("split.mp4", split), ("blur.mp4", None)):
        cs.cut_vertical(f"http://127.0.0.1:{srv.server_port}/src.ts", 0, 3, d / name, lay, "c.ass")
        wh = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                             str(d / name)], capture_output=True, text=True).stdout.strip()
        assert wh == "1080,1920", (name, wh)
    srv.shutdown()
    shutil.rmtree(d)
print("ok")
