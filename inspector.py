"""FactoryPulse visual quality inspection.

Gemini multimodal pipeline that detects, localizes and classifies surface defects
in component images, plus an offline mock and a synthetic sample-image generator
so the app is fully demonstrable without credentials.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import time
from enum import Enum
from typing import Any, Callable, List, Literal, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from PIL.PngImagePlugin import PngInfo
from pydantic import BaseModel, Field, ValidationError, field_validator

from llm import (  # noqa: F401  (re-exported for backwards compatibility)
    LLMCallError,
    Provenance,
    generate_with_retry,
    get_genai_client,
    get_llm_mode,
    get_model,
    llm_status,
)

logger = logging.getLogger("factorypulse.inspector")

DEFAULT_MODEL = get_model()


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class DefectType(str, Enum):
    HAIRLINE_FATIGUE_CRACK = "hairline_fatigue_crack"
    HEAT_DISCOLORATION = "heat_discoloration"
    SURFACE_PITTING = "surface_pitting"
    EXCESSIVE_WEAR = "excessive_wear"
    CORROSION = "corrosion"
    WELD_POROSITY = "weld_porosity"
    OTHER = "other"


Severity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]


def normalize_box(raw: Any) -> Optional[List[int]]:
    """Coerce a model-supplied box to [ymin, xmin, ymax, xmax] ints in 0-1000, ordered and clamped.

    Returns None when the value is not a usable box (wrong length, non-numeric, NaN/inf,
    or zero area after clamping).
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        vals = [float(c) for c in raw]
    except (TypeError, ValueError):
        return None
    if any(math.isnan(c) or math.isinf(c) for c in vals):
        return None
    vals = [int(round(min(max(c, 0.0), 1000.0))) for c in vals]
    ymin, xmin, ymax, xmax = vals
    ymin, ymax = sorted((ymin, ymax))
    xmin, xmax = sorted((xmin, xmax))
    if ymax == ymin or xmax == xmin:
        return None
    return [ymin, xmin, ymax, xmax]


def box_to_pixels(box: Optional[List[int]], width: int, height: int) -> Optional[Tuple[int, int, int, int]]:
    """Convert a normalized [ymin, xmin, ymax, xmax] (0-1000) box to pixel (x0, y0, x1, y1).

    The result is clamped to the image bounds; None for a missing/invalid box.
    """
    nb = normalize_box(box)
    if nb is None or width <= 0 or height <= 0:
        return None
    ymin, xmin, ymax, xmax = nb
    x0 = min(max(int(round(xmin / 1000 * width)), 0), width - 1)
    x1 = min(max(int(round(xmax / 1000 * width)), 0), width - 1)
    y0 = min(max(int(round(ymin / 1000 * height)), 0), height - 1)
    y1 = min(max(int(round(ymax / 1000 * height)), 0), height - 1)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


class Defect(BaseModel):
    defect_type: DefectType
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence between 0 and 1")
    box_2d: Optional[List[int]] = Field(
        default=None,
        description="Bounding box [ymin, xmin, ymax, xmax] as integers normalized to 0-1000 (origin top-left)",
    )
    description: str = Field(description="What is visible and why it is classified this way")
    recommended_action: str

    @field_validator("box_2d", mode="before")
    @classmethod
    def _coerce_box(cls, v: Any) -> Optional[List[int]]:
        """Never let a malformed box fail the whole report: unusable boxes become None."""
        return normalize_box(v)


class InspectionReport(BaseModel):
    component_type: str = Field(description="e.g. weld seam, spur gear, rolling bearing")
    overall_condition: Literal["PASS", "MARGINAL", "FAIL"]
    quality_score: int = Field(ge=0, le=100)
    defects: List[Defect]
    summary: str


SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
SEVERITY_COLOR = {
    "LOW": (80, 200, 120),
    "MEDIUM": (255, 200, 40),
    "HIGH": (255, 130, 30),
    "CRITICAL": (255, 50, 60),
}

INSPECTION_PROMPT = """You are a certified Level-II non-destructive testing (NDT) and visual quality inspector
for rotating machinery and welded/machined components.

Inspect the supplied image carefully and report every visible defect. Work in this order:
1. Identify the component type (weld seam, gear, rolling bearing, shaft, housing, other).
2. Scan the whole surface systematically, quadrant by quadrant, for these defect classes only:
   - hairline_fatigue_crack: thin, linear, often branching dark lines, frequently at stress risers/weld toes/tooth roots
   - heat_discoloration: blue, purple, straw or brown temper colors indicating overheating
   - surface_pitting: clusters of small craters/spalls on contact surfaces
   - excessive_wear: flattened, polished or scored contact areas, profile loss
   - corrosion: orange/brown oxide, scaling or rust
   - weld_porosity: gas pores or voids in weld metal
   - other: anything else safety-relevant
3. For each defect give a tight bounding box as [ymin, xmin, ymax, xmax] normalized to the range 0-1000
   (origin top-left), a severity (LOW, MEDIUM, HIGH, CRITICAL), confidence 0-1, a one-sentence evidence
   description, and a recommended action.
4. Severity guidance: any fatigue crack in a load-bearing area is at least HIGH; propagating or branching
   cracks are CRITICAL. Cosmetic discoloration without cracking is LOW or MEDIUM.
5. overall_condition: FAIL if any HIGH/CRITICAL defect, MARGINAL if only LOW/MEDIUM defects, else PASS.
   quality_score: 0-100 (100 = flawless).

Rules: do not invent defects you cannot see; if the image is clean return an empty defects list.
Do not report image artefacts, lighting glare or labels as defects. Respond only with JSON matching the schema.
{context}"""


# --------------------------------------------------------------------------- #
# Inspection
# --------------------------------------------------------------------------- #
def _mime_for(image_bytes: bytes) -> str:
    try:
        fmt = (Image.open(io.BytesIO(image_bytes)).format or "PNG").upper()
    except Exception:
        fmt = "PNG"
    return {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(fmt, "image/png")


def inspect_image(
    image_bytes: bytes,
    component_hint: str = "",
    use_mock: Optional[bool] = None,
    model: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Tuple[InspectionReport, Provenance]:
    """Analyze an image. Returns (report, provenance).

    Provenance is 'gemini-live', 'fallback' (live call failed; error text retained) or 'mock'.
    """
    model = model or get_model()
    status = llm_status()
    if use_mock or status["mode"] == "mock":
        reason = "offline mode selected" if use_mock and status["mode"] != "mock" else status["reason"]
        return _inspect_mock(image_bytes, component_hint), Provenance.mock(reason)
    started = time.time()
    try:
        report, attempts = _inspect_gemini(image_bytes, component_hint, status["mode"], model, sleep)
        return report, Provenance.live(model, started, attempts)
    except LLMCallError as exc:
        logger.warning("Gemini inspection failed (%s); using mock analysis.", exc)
        return _inspect_mock(image_bytes, component_hint), Provenance.fallback(exc, model, started)


def _inspect_gemini(image_bytes: bytes, component_hint: str, mode: str, model: str,
                    sleep: Callable[[float], None]) -> Tuple[InspectionReport, int]:
    from google.genai import types

    client = get_genai_client(mode)
    context = f"\nOperator context: {component_hint}" if component_hint else ""
    response, attempts = generate_with_retry(
        client,
        model=model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=_mime_for(image_bytes)),
            INSPECTION_PROMPT.format(context=context),
        ],
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=InspectionReport,
        ),
        sleep=sleep,
    )
    try:
        return InspectionReport.model_validate_json(response.text or ""), attempts
    except (ValidationError, ValueError) as exc:
        raise LLMCallError("response failed schema validation", str(exc)[:1500], attempts) from exc


_ACTIONS = {
    DefectType.HAIRLINE_FATIGUE_CRACK: "Quarantine component; confirm with dye-penetrant or MPI; do not return to service.",
    DefectType.HEAT_DISCOLORATION: "Investigate lubrication/cooling; verify no sub-surface hardness loss.",
    DefectType.SURFACE_PITTING: "Check lubricant contamination and load; monitor progression every shift.",
    DefectType.EXCESSIVE_WEAR: "Measure profile against tolerance; schedule replacement at next window.",
    DefectType.CORROSION: "Remove oxide, inspect wall loss, restore protective coating.",
    DefectType.WELD_POROSITY: "Grind out and re-weld per WPS; perform radiographic check.",
    DefectType.OTHER: "Escalate to inspector for manual review.",
}


def _inspect_mock(image_bytes: bytes, component_hint: str) -> InspectionReport:
    """Offline analysis: reads ground truth embedded in generated samples, otherwise
    produces a deterministic pseudo-analysis keyed on the image hash."""
    img = Image.open(io.BytesIO(image_bytes))
    embedded = img.info.get("factorypulse_defects")
    component = img.info.get("factorypulse_component") or (component_hint or "machined component")
    defects: List[Defect] = []

    if embedded:
        for d in json.loads(embedded):
            defects.append(
                Defect(
                    defect_type=DefectType(d["type"]),
                    severity=d["severity"],
                    confidence=d.get("confidence", 0.9),
                    box_2d=d["box"],
                    description=d["description"],
                    recommended_action=_ACTIONS[DefectType(d["type"])],
                )
            )
    else:
        digest = hashlib.sha256(image_bytes).digest()
        rng = np.random.default_rng(int.from_bytes(digest[:8], "big"))
        types_ = list(DefectType)[:-1]
        for _ in range(int(rng.integers(0, 3))):
            t = types_[int(rng.integers(0, len(types_)))]
            y, x = int(rng.integers(100, 700)), int(rng.integers(100, 700))
            h, w = int(rng.integers(60, 200)), int(rng.integers(60, 200))
            defects.append(
                Defect(
                    defect_type=t,
                    severity=["LOW", "MEDIUM", "HIGH"][int(rng.integers(0, 3))],
                    confidence=round(float(rng.uniform(0.55, 0.8)), 2),
                    box_2d=[y, x, y + h, x + w],
                    description=f"Offline heuristic estimate of {t.value.replace('_', ' ')} "
                    "(mock mode; connect Gemini for real analysis).",
                    recommended_action=_ACTIONS[t],
                )
            )

    worst = max((SEVERITY_RANK[d.severity] for d in defects), default=0)
    condition = "FAIL" if worst >= 3 else "MARGINAL" if worst >= 1 else "PASS"
    score = int(max(0, 100 - sum({1: 6, 2: 14, 3: 28, 4: 40}[SEVERITY_RANK[d.severity]] for d in defects)))
    if defects:
        names = ", ".join(sorted({d.defect_type.value.replace("_", " ") for d in defects}))
        summary = f"{len(defects)} defect(s) detected on {component}: {names}. Overall condition {condition}."
    else:
        summary = f"No visible defects detected on {component}. Overall condition PASS."
    return InspectionReport(
        component_type=component,
        overall_condition=condition,
        quality_score=score,
        defects=defects,
        summary=summary,
    )


# --------------------------------------------------------------------------- #
# Annotation
# --------------------------------------------------------------------------- #
def annotate_image(image_bytes: bytes, report: InspectionReport) -> Image.Image:
    """Draw numbered, severity-colored bounding boxes onto the image.

    Defects whose box is missing or unusable are skipped (they still appear in the table).
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    font = ImageFont.load_default(size=max(14, w // 55))
    for i, d in enumerate(report.defects, start=1):
        px = box_to_pixels(d.box_2d, w, h)
        if px is None:
            continue
        box = px
        color = SEVERITY_COLOR[d.severity]
        draw.rectangle(box, outline=color + (255,), width=max(2, w // 300), fill=color + (30,))
        label = f"{i}. {d.defect_type.value.replace('_', ' ')} [{d.severity}] {d.confidence:.0%}"
        tb = draw.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        ly = box[1] - th - 8 if box[1] - th - 8 > 0 else box[3] + 2
        draw.rectangle((box[0], ly, box[0] + tw + 10, ly + th + 8), fill=color + (235,))
        draw.text((box[0] + 5, ly + 2), label, fill=(15, 15, 15), font=font)
    return img


def list_sample_gallery(folder: Any) -> List[str]:
    """Names of image files in the sample gallery folder (sorted, non-recursive)."""
    from pathlib import Path

    d = Path(folder)
    if not d.is_dir():
        return []
    return sorted(f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})


# --------------------------------------------------------------------------- #
# Synthetic sample images (with embedded ground truth for the offline mock)
# --------------------------------------------------------------------------- #
SAMPLE_KINDS = {
    "weld": "Butt weld seam (fatigue crack + heat tint)",
    "gear": "Spur gear (pitting + wear)",
    "bearing": "Bearing inner race (spalling)",
}


def _steel_background(w: int, h: int, rng: np.random.Generator, base=(120, 124, 130)) -> Image.Image:
    yy = np.linspace(0.85, 1.1, h)[:, None]
    noise = rng.normal(0, 7, (h, w))
    brush = np.tile(rng.normal(0, 5, (1, w)), (h, 1))  # horizontal brushing streaks
    arr = np.stack([base[c] * yy + noise + brush for c in range(3)], axis=-1)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))


def _box(cx: float, cy: float, hw: float, hh: float, w: int, h: int) -> List[int]:
    return [
        int((cy - hh) / h * 1000),
        int((cx - hw) / w * 1000),
        int((cy + hh) / h * 1000),
        int((cx + hw) / w * 1000),
    ]


def _crack_path(rng: np.random.Generator, x0: float, y0: float, length: float, angle: float, steps: int = 40):
    pts = [(x0, y0)]
    a = angle
    for _ in range(steps):
        a += rng.normal(0, 0.35)
        x, y = pts[-1]
        pts.append((x + math.cos(a) * length / steps, y + math.sin(a) * length / steps))
    return pts


def make_sample_image(kind: str = "weld", seed: int = 7, size: Tuple[int, int] = (1024, 768)) -> bytes:
    """Render a synthetic component photo as PNG bytes with embedded defect ground truth."""
    w, h = size
    rng = np.random.default_rng(seed)
    img = _steel_background(w, h, rng)
    draw = ImageDraw.Draw(img, "RGBA")
    truth: List[dict] = []
    component = ""

    if kind == "weld":
        component = "butt weld seam"
        cy = h * 0.5
        for x in range(0, w, 3):  # weld bead body
            yoff = math.sin(x / 6.0) * 3
            draw.ellipse((x - 14, cy - 70 + yoff, x + 14, cy + 70 + yoff), fill=(168, 160, 150, 40))
        for x in range(30, w, 22):  # ripple scales
            draw.arc((x - 18, cy - 62, x + 18, cy + 62), 100, 260, fill=(205, 198, 188, 150), width=3)
        # heat tint along toe
        tx, tw_ = w * 0.22, 190
        for i in range(60):
            t = i / 60
            col = (int(60 + 120 * t), int(70 + 20 * t), int(190 - 140 * t), 55)
            draw.rectangle((tx, cy - 120 + i * 0.6, tx + tw_, cy - 120 + i * 0.6 + 6), fill=col)
        truth.append(
            dict(type="heat_discoloration", severity="MEDIUM", confidence=0.88,
                 box=_box(tx + tw_ / 2, cy - 100, tw_ / 2 + 5, 45, w, h),
                 description="Blue/straw temper colours along the weld toe indicate local overheating.")
        )
        # fatigue crack at toe, with a branch
        cx0, cy0 = w * 0.62, cy + 62
        main = _crack_path(rng, cx0, cy0, 170, -1.2)
        draw.line(main, fill=(15, 15, 18, 255), width=3)
        draw.line(main, fill=(0, 0, 0, 120), width=6)
        branch = _crack_path(rng, *main[22], 60, -0.4, 14)
        draw.line(branch, fill=(15, 15, 18, 255), width=2)
        xs = [p[0] for p in main + branch]
        ys = [p[1] for p in main + branch]
        truth.append(
            dict(type="hairline_fatigue_crack", severity="CRITICAL", confidence=0.93,
                 box=_box((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2,
                          (max(xs) - min(xs)) / 2 + 12, (max(ys) - min(ys)) / 2 + 12, w, h),
                 description="Branching hairline crack initiating at the weld toe and propagating into the base metal.")
        )

    elif kind == "gear":
        component = "spur gear"
        cx, cy, r_out, r_in, teeth = w / 2, h / 2, 290, 235, 22
        pts = []
        for i in range(teeth * 4):
            a = 2 * math.pi * i / (teeth * 4)
            r = r_out if i % 4 in (1, 2) else r_in
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        draw.polygon(pts, fill=(150, 152, 158, 255), outline=(60, 60, 64, 255))
        draw.ellipse((cx - 70, cy - 70, cx + 70, cy + 70), fill=(60, 62, 68, 255))
        draw.ellipse((cx - 150, cy - 150, cx + 150, cy + 150), outline=(110, 112, 118, 255), width=4)
        # worn tooth: flat polished facet
        a = math.radians(-35)
        wx, wy = cx + 262 * math.cos(a), cy + 262 * math.sin(a)
        draw.ellipse((wx - 34, wy - 22, wx + 34, wy + 22), fill=(215, 218, 224, 200))
        truth.append(
            dict(type="excessive_wear", severity="HIGH", confidence=0.86,
                 box=_box(wx, wy, 42, 32, w, h),
                 description="Polished flat facet on the tooth flank shows profile loss from abrasive wear.")
        )
        # pitting cluster
        a = math.radians(140)
        px, py = cx + 255 * math.cos(a), cy + 255 * math.sin(a)
        for _ in range(26):
            ox, oy = rng.normal(0, 16), rng.normal(0, 16)
            rr = rng.uniform(2.5, 5.5)
            draw.ellipse((px + ox - rr, py + oy - rr, px + ox + rr, py + oy + rr), fill=(25, 25, 28, 235))
        truth.append(
            dict(type="surface_pitting", severity="MEDIUM", confidence=0.9,
                 box=_box(px, py, 46, 46, w, h),
                 description="Cluster of small craters on the pitch line consistent with contact-fatigue pitting.")
        )

    else:  # bearing
        kind = "bearing"
        component = "rolling bearing inner race"
        cx, cy = w / 2, h / 2
        for r, col in [(300, (95, 98, 104)), (250, (170, 172, 178)), (200, (140, 142, 148)), (150, (72, 74, 80))]:
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col + (255,))
        draw.ellipse((cx - 110, cy - 110, cx + 110, cy + 110), fill=(30, 30, 34, 255))
        for i in range(12):  # rollers
            a = 2 * math.pi * i / 12
            rx, ry = cx + 225 * math.cos(a), cy + 225 * math.sin(a)
            draw.ellipse((rx - 26, ry - 26, rx + 26, ry + 26), fill=(190, 192, 198, 255), outline=(80, 80, 86, 255))
        # spall on raceway
        a = math.radians(25)
        sx, sy = cx + 180 * math.cos(a), cy + 180 * math.sin(a)
        draw.polygon([(sx - 30, sy - 12), (sx + 24, sy - 20), (sx + 34, sy + 14), (sx - 10, sy + 26)],
                     fill=(35, 30, 28, 255))
        for _ in range(14):
            ox, oy = rng.normal(0, 22), rng.normal(0, 22)
            rr = rng.uniform(2, 4.5)
            draw.ellipse((sx + ox - rr, sy + oy - rr, sx + ox + rr, sy + oy + rr), fill=(25, 25, 28, 230))
        truth.append(
            dict(type="surface_pitting", severity="HIGH", confidence=0.91,
                 box=_box(sx, sy, 58, 50, w, h),
                 description="Spalled area with satellite pits on the inner raceway — advanced rolling-contact fatigue.")
        )
        # heat tint on the bore
        for i in range(30):
            draw.arc((cx - 125 - i, cy - 125 - i, cx + 125 + i, cy + 125 + i), 200, 260,
                     fill=(60 + i * 3, 70, 200 - i * 5, 45), width=2)
        a = math.radians(230)
        hx, hy = cx + 135 * math.cos(a), cy + 135 * math.sin(a)
        truth.append(
            dict(type="heat_discoloration", severity="LOW", confidence=0.8,
                 box=_box(hx, hy, 60, 45, w, h),
                 description="Blue tint on the bore shoulder suggests transient overheating.")
        )

    pnginfo = PngInfo()
    pnginfo.add_text("factorypulse_defects", json.dumps(truth))
    pnginfo.add_text("factorypulse_component", component)
    buf = io.BytesIO()
    img.save(buf, format="PNG", pnginfo=pnginfo)
    return buf.getvalue()
