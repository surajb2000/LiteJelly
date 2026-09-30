"""Does a painted subtitle actually reach the screen in real Chrome?

The DOM can say a cue is there while the GPU compositor never draws it, so
this checks both: the cue element's text and box, and the real pixels inside
that box from a screenshot. Run against any LiteJelly server:

    python tools/subtitle_visibility_probe.py http://host:8000 "Thor" "Iron Man 3"

The progress endpoint is stubbed inside the page, so resume points on the
server are left exactly as they were.
"""

import base64
import json
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import launch  # noqa: E402

OPEN = """
(async () => {
  window.__lj_fetch = window.__lj_fetch || window.fetch;
  window.fetch = (u, o) => String(u).indexOf('/api/progress') >= 0
    ? Promise.resolve(new Response('{}')) : window.__lj_fetch(u, o);
  navigator.sendBeacon = () => true;
  const lib = await (await fetch('/api/library')).json();
  const hit = lib.videos.find(v => v.name === %s);
  if (!hit) return 'missing';
  document.querySelectorAll('#search-input').forEach(i => i.blur());
  // Same entry point a click on a card uses.
  const card = document.createElement('div');
  window.dispatchEvent(new Event('focus'));
  return hit.id;
})()
"""

STATE = """
(() => {
  const v = document.querySelector('#video-player');
  const layer = document.querySelector('#subtitle-layer');
  const cue = layer.querySelector('.subtitle-cue');
  const r = cue ? cue.getBoundingClientRect() : null;
  const tt = v.textTracks[0];
  return {
    t: +v.currentTime.toFixed(1), paused: v.paused, ready: v.readyState,
    osdHidden: document.querySelector('#osd').classList.contains('hidden'),
    text: layer.textContent.slice(0, 40),
    box: r ? [r.left, r.top, r.width, r.height].map(Math.round) : null,
    dpr: devicePixelRatio,
    track: tt ? tt.mode + '/' + (tt.cues ? tt.cues.length : 'null') : 'none',
    badge: document.querySelector('.osd-meta-badge') ? document.querySelector('.osd-meta-badge').title.replace(/\\n/g, ' | ') : ''
  };
})()
"""


def png_pixels(data: bytes):
    """Decode an RGBA/RGB 8-bit PNG from Chrome into rows of pixels."""
    pos, width, height, colour, chunks = 8, 0, 0, 0, b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, _, colour = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            chunks += body
        pos += 12 + length
    channels = 4 if colour == 6 else 3
    raw = zlib.decompress(chunks)
    stride = width * channels
    rows, prev, i = [], bytearray(stride), 0
    for _ in range(height):
        f = raw[i]; line = bytearray(raw[i + 1:i + 1 + stride]); i += 1 + stride
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]; c = prev[x - channels] if x >= channels else 0
            if f == 1: line[x] = (line[x] + a) & 255
            elif f == 2: line[x] = (line[x] + b) & 255
            elif f == 3: line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line); prev = line
    return width, height, channels, rows


def white_in_box(tab, box, dpr):
    """Fraction of near-white pixels inside the cue's box, from real pixels."""
    shot = tab.call("Page.captureScreenshot", format="png")
    w, h, ch, rows = png_pixels(base64.b64decode(shot["data"]))
    x0, y0 = int(box[0] * dpr), int(box[1] * dpr)
    x1, y1 = min(w, int((box[0] + box[2]) * dpr)), min(h, int((box[1] + box[3]) * dpr))
    white = total = 0
    for y in range(max(0, y0), y1, 2):
        row = rows[y]
        for x in range(max(0, x0), x1, 2):
            r, g, b = row[x * ch], row[x * ch + 1], row[x * ch + 2]
            total += 1
            if r > 225 and g > 225 and b > 225:
                white += 1
    return round(white / total, 3) if total else None


def probe(tab, name):
    vid = tab.js(OPEN % json.dumps(name))
    if vid == "missing":
        return {"film": name, "error": "not in library"}
    tab.js(f"""(() => {{
      const c = Array.from(document.querySelectorAll('[aria-label]'))
        .find(n => n.getAttribute('aria-label') === 'Play ' + {json.dumps(name)});
      if (c) c.click();
      return !!c;
    }})()""")
    # Let it start and get well past the opening, where there is dialogue.
    for _ in range(60):
        s = tab.js(STATE)
        if s["ready"] >= 3 and not s["paused"]:
            break
        time.sleep(0.5)
    tab.js("document.querySelector('#video-player').currentTime = 600")
    time.sleep(4)

    samples = []
    for phase in ("osd shown", "osd hidden"):
        if phase == "osd shown":
            tab.js("document.querySelector('#player').dispatchEvent(new MouseEvent('mousemove', {bubbles:true}))")
        else:
            time.sleep(5)  # longer than OSD_TIMEOUT
        for _ in range(12):
            s = tab.js(STATE)
            if s["box"]:
                s["white"] = white_in_box(tab, s["box"], s["dpr"])
                break
            time.sleep(0.5)
        s["phase"] = phase
        samples.append(s)
    tab.js("document.querySelector('#player-back-btn').click()")
    time.sleep(1.5)
    return {"film": name, "samples": samples}


def main():
    base = sys.argv[1].rstrip("/")
    films = sys.argv[2:] or ["Thor", "Iron Man 3"]
    proc, tab = launch(base + "/")
    try:
        tab.call("Page.enable")
        time.sleep(4)
        for name in films:
            print(json.dumps(probe(tab, name), indent=1))
    finally:
        proc.kill()


if __name__ == "__main__":
    main()
