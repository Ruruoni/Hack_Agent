"""Optional browser end-to-end smoke test (NOT collected by pytest; run it by hand).

Starts the real backend (serving the built frontend) against the protocol-level LM Studio mock, then drives the UI with
Playwright/Chromium: dashboard, upload a document, grounded streaming chat with a cited source, every page, the setup wizard.

    pip install playwright            # and have a Chromium available (playwright install chromium, or set NEXUS_CHROMIUM=<path to chrome>)
    cd frontend && npm run build      # the UI must be built first
    cd backend && python tests/e2e_browser_smoke.py [screenshot_dir]

Exit code 0 = all assertions passed and the browser console stayed free of errors.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import uvicorn  # noqa: E402
from mock_lmstudio import MockLM  # noqa: E402

from nexus.api.app import create_app  # noqa: E402
from nexus.config import load_settings  # noqa: E402
from nexus.core import AppContext  # noqa: E402

PORT = int(os.environ.get("NEXUS_E2E_PORT", "8765"))


def main() -> int:
    from playwright.sync_api import sync_playwright
    shots = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="nexus_shots_"))
    shots.mkdir(parents=True, exist_ok=True)
    lm = MockLM().start()
    s = load_settings(tempfile.mkdtemp(prefix="nexus_e2e_"))
    s.vector_backend = "sqlite"
    s.lmstudio_base_url = lm.base_url
    s.allow_cors_origins = [f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"]
    server = uvicorn.Server(uvicorn.Config(create_app(AppContext(s)), host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(2)
    errors: list[str] = []
    ok = True
    with sync_playwright() as p:
        exe = os.environ.get("NEXUS_CHROMIUM")
        b = p.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if exe else p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.goto(f"http://127.0.0.1:{PORT}/")
        pg.wait_for_selector("text=Everything NEXUS is doing", timeout=20000)
        pg.screenshot(path=str(shots / "dashboard.png"))
        pg.click("nav >> text=Chat")
        pg.set_input_files("input[type=file]", {"name": "nimbus.txt", "mimeType": "text/plain",
                                                "buffer": b"Project Nimbus gateway listens on TCP port 7421 and forwards metrics to the collector cluster every 30 seconds."})
        pg.wait_for_selector("text=chunks indexed", timeout=20000)
        pg.fill("textarea[aria-label=Message]", "Which port does the Nimbus gateway use?")
        pg.keyboard.press("Enter")
        pg.wait_for_selector("text=/sources? retrieved/", timeout=20000)
        pg.wait_for_function("document.body.innerText.includes('[S1]')", timeout=20000)      # streaming finished
        body = pg.inner_text("body")
        grounded = "7421" in body and "[S1]" in body
        print("grounded answer with citation visible:", grounded)
        ok &= grounded
        for label in ["Data ingestion", "Knowledge & memory", "Models & learning", "Agent tasks", "Web research", "Skills & extensions", "Settings", "Setup wizard"]:
            pg.click(f"nav >> text={label}")
            pg.wait_for_timeout(700)
            pg.screenshot(path=str(shots / (label.split()[0].lower() + ".png")))
        pg.click("text=Run all basic checks")
        pg.wait_for_selector("text=checks passed", timeout=20000)
        pg.wait_for_function("document.body.innerText.match(/(\\d+) of 15 checks passed/) && +document.body.innerText.match(/(\\d+) of 15 checks passed/)[1] >= 10", timeout=60000)
        b.close()
    server.should_exit = True
    lm.stop()
    print("console/page errors:", errors or "none")
    print("screenshots in", shots)
    return 0 if ok and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
