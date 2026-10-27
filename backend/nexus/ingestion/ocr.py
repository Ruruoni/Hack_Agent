"""OCR: read text out of scanned pages, photos/screenshots and pictures embedded in documents.

Engine: RapidOCR (ONNX runtime). It is a pure pip install (`pip install rapidocr-onnxruntime`), ships its models inside the wheel
(no downloads, no separate Tesseract installer) and runs on CPU. The engine is loaded lazily on first use and shared by all
ingestion workers (calls are serialised with a lock).

`OcrService` is the only thing parsers talk to, so another engine can be added behind the same two methods.
"""
from __future__ import annotations

import io
import logging
import statistics
import threading
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("nexus.ocr")

INSTALL_HINT = "Install it with:  .\\.venv\\Scripts\\python.exe -m pip install -r backend\\requirements-ocr.txt   (then restart NEXUS)"


@dataclass
class OcrResult:
    text: str
    confidence: float | None      # mean line confidence 0..1 (None if unknown)
    lines: int


class OcrUnavailable(Exception):
    pass


def _assemble(items: list[tuple[list, str, float]]) -> str:
    """Order recognised lines in reading order: group into rows by vertical position, then left to right."""
    if not items:
        return ""
    boxes = []
    for box, text, _ in items:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        boxes.append((min(ys), max(ys), min(xs), text))
    heights = [b[1] - b[0] for b in boxes if b[1] > b[0]]
    row_h = max(8.0, 0.6 * (statistics.median(heights) if heights else 20.0))
    boxes.sort(key=lambda b: (b[0] + b[1]) / 2)
    rows: list[list[tuple]] = []
    for b in boxes:
        centre = (b[0] + b[1]) / 2
        if rows and abs(centre - statistics.mean((r[0] + r[1]) / 2 for r in rows[-1])) <= row_h:
            rows[-1].append(b)
        else:
            rows.append([b])
    return "\n".join("  ".join(t[3] for t in sorted(r, key=lambda t: t[2])) for r in rows)


def _prepare(data: bytes) -> bytes:
    """White margin + sane aspect ratio. Very wide/short images (one line of text, banners, cropped screenshots) are otherwise
    letterboxed with black bars inside the engine, which hides the text; margins also help text that touches the edge."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            w, h = im.size
            margin = max(16, min(w, h) // 10)
            cw, ch = w + 2 * margin, h + 2 * margin
            if cw / ch > 5:                       # too wide: add vertical space
                ch = int(cw / 5)
            elif ch / cw > 5:
                cw = int(ch / 5)
            canvas = Image.new("RGB", (cw, ch), "white")
            canvas.paste(im, ((cw - w) // 2, (ch - h) // 2))
            out = io.BytesIO()
            canvas.save(out, "PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001 - never lose an OCR attempt to a preprocessing problem
        return data


class OcrService:
    def __init__(self, settings: Any):
        self.s = settings
        self._engine: Any = None
        self._error: str = ""
        self._lock = threading.Lock()
        self._version = ""

    # -- options come from live settings so changes in the UI apply without a restart --------------------
    @property
    def mode(self) -> str:
        m = getattr(self.s, "ocr_mode", "auto")
        return m if m in ("auto", "scanned_only", "off") else "auto"

    @property
    def dpi(self) -> int:
        return max(72, min(400, int(getattr(self.s, "ocr_dpi", 200))))

    @property
    def min_image_px(self) -> int:
        return max(16, int(getattr(self.s, "ocr_min_image_px", 120)))

    @property
    def max_images_per_page(self) -> int:
        return max(0, int(getattr(self.s, "ocr_max_images_per_page", 6)))

    @property
    def min_confidence(self) -> float:
        return float(getattr(self.s, "ocr_min_confidence", 0.5))

    # -- engine ------------------------------------------------------------------------------------------------
    def _load(self) -> Any:
        if self._engine is not None:
            return self._engine
        if self._error:
            raise OcrUnavailable(self._error)
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            self._engine = RapidOCR()
            try:
                from importlib.metadata import version
                self._version = version("rapidocr-onnxruntime")
            except Exception:  # noqa: BLE001
                self._version = ""
        except ImportError as e:
            self._error = f"the OCR engine is not installed ({e.__class__.__name__}: {e}). {INSTALL_HINT}"
            raise OcrUnavailable(self._error) from e
        except Exception as e:  # noqa: BLE001 - onnxruntime DLL problems etc.
            self._error = f"the OCR engine failed to start: {e.__class__.__name__}: {e}"
            raise OcrUnavailable(self._error) from e
        return self._engine

    def reset(self) -> None:
        """Forget a cached load failure (after the user installs the package)."""
        self._engine, self._error = None, ""

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def available(self) -> bool:
        if not self.enabled:
            return False
        try:
            self._load()
            return True
        except OcrUnavailable:
            return False

    def unavailable_reason(self) -> str:
        if not self.enabled:
            return "OCR is turned off (Settings > Ingestion & storage > OCR mode)"
        try:
            self._load()
            return ""
        except OcrUnavailable as e:
            return str(e)

    def ocr_image(self, data: bytes) -> OcrResult:
        """OCR an encoded image (PNG/JPEG/...). Raises OcrUnavailable if the engine cannot run."""
        eng = self._load()
        with self._lock:
            result, _ = eng(_prepare(data))
        items = [(r[0], str(r[1]), float(r[2])) for r in (result or []) if str(r[1]).strip()]
        conf = round(statistics.mean(i[2] for i in items), 3) if items else None
        return OcrResult(text=_assemble(items), confidence=conf, lines=len(items))

    # -- status / self-test (UI, CLI) ----------------------------------------------------------------------------------
    def status(self) -> dict:
        why = self.unavailable_reason()
        return {"mode": self.mode, "available": not why, "engine": f"rapidocr-onnxruntime {self._version}".strip() if not why else None,
                "dpi": self.dpi, "message": why or "OCR is ready.",
                "formats": "scanned and mixed PDFs, PNG/JPG/TIFF/BMP/GIF/WEBP images, pictures embedded in DOCX",
                "languages": "English and Chinese text models are bundled; other scripts are not supported by this engine."}

    def self_test(self) -> dict:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (900, 220), "white")
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.load_default(size=40)
        except TypeError:   # very old Pillow
            font = ImageFont.load_default()
        d.text((20, 30), "NEXUS OCR TEST 4417", fill="black", font=font)
        d.text((20, 110), "Maintenance window: second Tuesday", fill="black", font=font)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        try:
            r = self.ocr_image(buf.getvalue())
        except OcrUnavailable as e:
            return {"ok": False, "recognised": "", "message": str(e)}
        ok = "4417" in r.text.replace(" ", "")
        return {"ok": ok, "recognised": r.text, "confidence": r.confidence,
                "message": "OCR works: the test image was read correctly." if ok else "The engine ran but did not read the test image correctly."}
