"""Re-record the README images in docs/img/ from a mock lab.

    pip install playwright pillow && playwright install chromium
    python scripts/capture_readme.py [output-dir]     (default: docs/img)

Seeds a throwaway database with seed_mock_lab.py, starts a server on it (port
8099, syslog on 5598), drives Chromium, then stops the server and deletes the
database. Writes themes.png, layouts.png and confetti-butler-demo.gif. Run it
after any UI change or rename so the README never shows an old look.
"""

import io
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
BUTLER = os.path.dirname(HERE)
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BUTLER, "..", "docs", "img")
PORT = 8099
BASE = "http://127.0.0.1:{}".format(PORT)
SIZE = {"width": 1280, "height": 720}

THEMES = ["light", "terminal", "confetti-night", "neon"]      # themes.png, in order
LAYOUTS = ["classic", "modern", "retro95", "amber", "greencrt"]           # layouts.png, in order
TOUR = ["/", "/devices", "/devices/1", "/topology", "/ipam", "/templates", "/syslog", "/conflicts"]


def wait_for_server():
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + "/api/health", timeout=1)
            return
        except OSError:
            time.sleep(0.2)
    sys.exit("server did not start")


def shot(browser, path, theme="confetti-night", layout="classic", wait_ms=900):
    """One screenshot as a PIL image, in a fresh browser context that is closed again
    (open contexts keep polling /api/health and starve the server). Saved theme and
    layout are set in localStorage first."""
    context = browser.new_context(viewport=SIZE)
    page = context.new_page()
    page.add_init_script(
        "try{localStorage.setItem('confetti-butler-theme','%s');"
        "localStorage.setItem('confetti-butler-layout','%s');}catch(e){}" % (theme, layout))
    page.goto(BASE + path)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(wait_ms + (1500 if path == "/topology" else 0))
    img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    context.close()
    return img


def collage(images, out_path):
    w, h = SIZE["width"] // 2, SIZE["height"] // 2
    sheet = Image.new("RGB", (SIZE["width"], SIZE["height"]))
    for i, img in enumerate(images):
        sheet.paste(img.resize((w, h), Image.LANCZOS), ((i % 2) * w, (i // 2) * h))
    sheet.save(out_path, optimize=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="butler-capture-")
    db_path = os.path.join(tmp, "mock-lab.db")
    subprocess.run([sys.executable, os.path.join(HERE, "seed_mock_lab.py"), db_path], check=True)

    env = dict(os.environ, BUTLER_DB_PATH=db_path, BUTLER_PORT=str(PORT), BUTLER_SYSLOG_PORT="5598")
    server = subprocess.Popen([sys.executable, os.path.join(BUTLER, "serve.py")], cwd=BUTLER, env=env)
    try:
        wait_for_server()
        with sync_playwright() as p:
            browser = p.chromium.launch()
            collage([shot(browser, "/", theme=t) for t in THEMES], os.path.join(OUT, "themes.png"))
            collage([shot(browser, "/", layout=l) for l in LAYOUTS], os.path.join(OUT, "layouts.png"))

            frames = [shot(browser, path) for path in TOUR]
            frames += [shot(browser, "/", theme=t) for t in THEMES]
            frames += [shot(browser, "/", layout=l) for l in LAYOUTS[1:]]
            browser.close()

        small = [f.resize((960, 540), Image.LANCZOS).quantize(colors=128) for f in frames]
        small[0].save(os.path.join(OUT, "confetti-butler-demo.gif"), save_all=True,
                      append_images=small[1:], duration=1800, loop=0, optimize=True)
        print("wrote themes.png, layouts.png, confetti-butler-demo.gif to", os.path.normpath(OUT))
    finally:
        server.terminate()
        server.wait(timeout=10)
        for name in os.listdir(tmp):
            os.remove(os.path.join(tmp, name))
        os.rmdir(tmp)


main()
