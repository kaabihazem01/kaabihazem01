#!/usr/bin/env python3
"""Render the profile README as a stack of animated SVG cards.

    python scripts/build_cards.py            # writes cards/*.svg and updates README.md
    STATIC=1 python scripts/build_cards.py   # final frame only -> cards-static/ (+ preview.html)

Everything the cards need is inlined, because GitHub shows them through <img>
tags, where scripts, external fonts and external images are all blocked:

  * animation:  CSS @keyframes in an inline <style>, plus SMIL <animate>
  * fonts:      subset per card with pyftsubset, embedded as base64 WOFF
  * motion:     every CSS animation is disabled under prefers-reduced-motion and
                the SMIL-only decorations are hidden (display:none)

Input: data/contributions.json (refreshed daily by the GitHub Action, see
scripts/fetch_contributions.py). Needs: Python 3.9+, fonttools.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# Config: edit this block
# --------------------------------------------------------------------------
NAME = "Hazem Kaabi"
EYEBROW = "HELLO WORLD!"
ROLE_LINE = "Full Stack Developer from Tunisia."

# Three linked tiles: (small label, big text, link, accent colour or None for brand)
TILES = [
    ("LANGUAGE", "TypeScript", "https://www.typescriptlang.org", None),
    ("FRAMEWORK", "Next.js", "https://nextjs.org", "#FF4FA3"),
    ("STYLING", "Tailwind + shadcn", "https://ui.shadcn.com", "#9B8CFF"),
]
MAX_REPOS = 4  # featured repos shown (taken from your pinned repos)

# Optional portrait: drop a square-ish image at assets/portrait.(png|jpg|webp).
# Without one, the hero shows your initials instead.
PORTRAIT_CANDIDATES = ["assets/portrait.png", "assets/portrait.jpg", "assets/portrait.jpeg", "assets/portrait.webp"]

# Brand colour. Override per run with BRAND=#rrggbb. The rest of the palette
# comes from the neon design (swap these once you have a website to match).
BRAND = os.environ.get("BRAND", "#2DE2C8")
PINK = "#FF4FA3"
VIOLET = "#9B8CFF"
GROUND = "#0A0B10"
SURFACE = "#12141C"
LINE = "#232634"
EMPTY = "#151823"
TEXT = "#E8EAF2"
MUTED = "#9AA0B4"

# Fonts, first existing path wins. Drop Space Grotesk / JetBrains Mono (or your
# website's fonts) into fonts/ and list them first to switch typefaces.
FONT_CANDIDATES = {
    "display": ["fonts/SpaceGrotesk-Bold.ttf", "fonts/SpaceGrotesk-Bold.otf", "fonts/Inter-Bold.otf"],
    "body": ["fonts/SpaceGrotesk-Regular.ttf", "fonts/SpaceGrotesk-Regular.otf", "fonts/Inter-Regular.otf"],
    "mono": ["fonts/JetBrainsMono-Regular.ttf", "fonts/LiberationMono-Regular.ttf"],
}
FONT_WEIGHT = {"display": 700, "body": 400, "mono": 400}
FONT_FAMILY = {"display": "HzDisplay", "body": "HzBody", "mono": "HzMono"}
FONT_CLASS = {"display": "d", "body": "b", "mono": "m"}
FONT_FALLBACK = {"display": "system-ui,sans-serif", "body": "system-ui,sans-serif", "mono": "ui-monospace,monospace"}

# Page timeline: each card starts this many seconds after load, so the page
# plays top to bottom. Tiles and repos add a small per-item stagger on top.
T_HERO, T_TILES, T_STATS, T_REPOS, T_GRAPH = 0.0, 0.5, 1.1, 1.8, 2.6

STATIC = os.environ.get("STATIC", "").strip().lower() in ("1", "true", "yes")
OUT_DIR = ROOT / ("cards-static" if STATIC else "cards")
DATA_FILE = ROOT / "data" / "contributions.json"
README = ROOT / "README.md"
MARK_START, MARK_END = "<!--CARDS:START-->", "<!--CARDS:END-->"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

if not re.fullmatch(r"#[0-9a-fA-F]{6}", BRAND):
    sys.exit(f"BRAND must look like #2DE2C8, got {BRAND!r}")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def fmt(n: int) -> str:
    return f"{n:,}"


def fmt_day(d: dt.date) -> str:
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def lerp_hex(a: str, b: str, t: float) -> str:
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(pa, pb))


def attr(s: str) -> str:
    return html.escape(s, quote=True)


class Font:
    """A font file plus exact text measurement (no kerning, close enough to centre and wrap)."""

    def __init__(self, key: str, path: Path):
        self.key, self.path = key, path
        self.tt = TTFont(str(path), lazy=True)
        self.upem = self.tt["head"].unitsPerEm
        self.cmap = self.tt.getBestCmap()
        self.hmtx = self.tt["hmtx"]

    def width(self, s: str, size: float, spacing: float = 0.0) -> float:
        total = 0
        for ch in s:
            g = self.cmap.get(ord(ch), ".notdef")
            total += self.hmtx[g][0]
        return total / self.upem * size + spacing * len(s)


def load_fonts() -> dict[str, Font]:
    fonts = {}
    for key, cands in FONT_CANDIDATES.items():
        for c in cands:
            p = ROOT / c
            if p.exists():
                fonts[key] = Font(key, p)
                break
        else:
            sys.exit(f"No font found for '{key}'. Looked for: {', '.join(cands)}")
    return fonts


FONTS = load_fonts()
_subset_cache: dict[tuple, str] = {}


def subset_woff_b64(font: Font, chars: set[str]) -> str:
    """Subset `font` to `chars` with pyftsubset and return base64 WOFF."""
    text = "".join(sorted(chars))
    key = (str(font.path), text)
    if key in _subset_cache:
        return _subset_cache[key]
    with tempfile.TemporaryDirectory() as td:
        chars_file = Path(td) / "chars.txt"
        chars_file.write_text(text, encoding="utf-8")
        out = Path(td) / "out.woff"
        exe = shutil.which("pyftsubset")
        cmd = ([exe] if exe else [sys.executable, "-m", "fontTools.subset"]) + [
            str(font.path),
            f"--text-file={chars_file}",
            "--flavor=woff",
            f"--output-file={out}",
            "--layout-features=kern",
            "--no-hinting",
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        b64 = base64.b64encode(out.read_bytes()).decode("ascii")
    _subset_cache[key] = b64
    return b64


# --------------------------------------------------------------------------
# Card: one SVG document
# --------------------------------------------------------------------------
CSS_ANIM = """
.rise{animation:rise .8s cubic-bezier(.2,.7,.2,1) both}
.fade{animation:fade .6s ease-out both}
.grow{transform-box:fill-box;transform-origin:50% 100%;animation:grow .9s cubic-bezier(.2,.7,.2,1) both}
.draw{stroke-dasharray:1;animation:draw 1.1s ease-out both}
@keyframes rise{from{opacity:0;transform:translateY(10px)}}
@keyframes fade{from{opacity:0}}
@keyframes grow{from{transform:scaleY(0)}}
@keyframes draw{from{stroke-dashoffset:1}to{stroke-dashoffset:0}}
@media (prefers-reduced-motion:reduce){
.rise,.fade,.grow,.draw{animation:none!important}
.smil{display:none!important}
}
"""


class Card:
    def __init__(self, stem: str, w: int, h: int, title: str, t0: float):
        self.stem, self.w, self.h, self.title, self.t0 = stem, w, h, title, t0
        self.chars: dict[str, set[str]] = {k: set() for k in FONTS}
        self.defs: list[str] = []
        self.out: list[str] = []

    # -- animation helpers --------------------------------------------------
    def at(self, off: float = 0.0) -> float:
        return round(self.t0 + off, 2)

    def cls(self, kind: str | None = None, off: float = 0.0, *extra: str) -> str:
        """class (+ delay) attributes. In STATIC mode the animation class is dropped."""
        names = [e for e in extra if e]
        style = ""
        if kind and not STATIC:
            names.append(kind)
            style = f' style="animation-delay:{self.at(off):.2f}s"'
        return (f' class="{" ".join(names)}"' if names else "") + style

    def smil(self, svg: str) -> None:
        """Decorative SMIL-only element: hidden in reduced motion, omitted in STATIC."""
        if not STATIC:
            self.out.append(svg)

    # -- drawing helpers ----------------------------------------------------
    def add(self, s: str) -> None:
        self.out.append(s)

    def text(self, x, y, s, font, size, fill, anchor="start", kind="rise", off=0.0, spacing=0.0, extra_attrs=""):
        self.chars[font].update(s)
        a = f'x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}"'
        if anchor != "start":
            a += f' text-anchor="{anchor}"'
        if spacing:
            a += f' letter-spacing="{spacing}"'
        return f'<text {a}{self.cls(kind, off, FONT_CLASS[font])}{extra_attrs}>{xml_escape(s)}</text>'

    def frame(self, fill: str, r: int = 24, top_bar: str | None = None) -> None:
        self.defs.append(f'<clipPath id="clip"><rect width="{self.w}" height="{self.h}" rx="{r}"/></clipPath>')
        self.out.append(f'<rect width="{self.w}" height="{self.h}" rx="{r}" fill="{fill}"/>')
        if top_bar:
            self.out.append(
                f'<rect width="{self.w}" height="4" fill="{top_bar}" clip-path="url(#clip)"{self.cls("fade", 0.05)}/>'
            )

    def border(self, r: int = 24) -> None:
        self.out.append(
            f'<rect x=".5" y=".5" width="{self.w - 1}" height="{self.h - 1}" rx="{r - 0.5}" fill="none" stroke="{LINE}"/>'
        )

    def render(self) -> str:
        faces = []
        for key, used in self.chars.items():
            if not used:
                continue
            b64 = subset_woff_b64(FONTS[key], used)
            faces.append(
                f"@font-face{{font-family:'{FONT_FAMILY[key]}';font-weight:{FONT_WEIGHT[key]};"
                f"src:url(data:font/woff;base64,{b64}) format('woff')}}"
            )
        classes = "".join(
            f".{FONT_CLASS[k]}{{font-family:'{FONT_FAMILY[k]}',{FONT_FALLBACK[k]};font-weight:{FONT_WEIGHT[k]}}}"
            for k in FONTS
        )
        css = "".join(faces) + classes + ("" if STATIC else CSS_ANIM)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" height="{self.h}" '
            f'role="img" aria-label={json.dumps(self.title, ensure_ascii=False)}>\n'
            f"<title>{xml_escape(self.title)}</title>\n"
            f"<style>{css}</style>\n"
            f"<defs>{''.join(self.defs)}</defs>\n"
            + "\n".join(self.out)
            + "\n</svg>\n"
        )


def fit(font: Font, s: str, size: float, max_w: float, min_size: float = 12, spacing: float = 0.0) -> float:
    while size > min_size and font.width(s, size, spacing) > max_w:
        size -= 0.5
    return size


def ellipsize(font: Font, s: str, size: float, max_w: float) -> str:
    while len(s) > 1 and font.width(s, size) > max_w:
        s = s[:-2].rstrip() + "…"
    return s


def wrap(font: Font, s: str, size: float, max_w: float, max_lines: int) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        trial = (cur + " " + word).strip()
        if font.width(trial, size) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .,;:") + "…"
    out = []
    for ln in lines:
        while len(ln) > 1 and font.width(ln, size) > max_w:
            ln = ln[:-2].rstrip() + "…"
        out.append(ln)
    return out


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def load_data() -> dict:
    if not DATA_FILE.exists():
        sys.exit(f"Missing {DATA_FILE.relative_to(ROOT)}. Run scripts/fetch_contributions.py first.")
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    data["days"] = sorted(
        ({"date": dt.date.fromisoformat(d["date"]), "count": int(d["count"])} for d in data["days"]),
        key=lambda d: d["date"],
    )
    if len(data["days"]) != 365:
        sys.exit(f"Expected 365 daily contribution records, got {len(data['days'])}. Refusing to render stale or partial data.")
    for previous, current in zip(data["days"], data["days"][1:]):
        if current["date"] - previous["date"] != dt.timedelta(days=1):
            sys.exit(f"Contribution dates are not consecutive: {previous['date']} then {current['date']}.")
    return data


def compute_stats(days: list[dict]) -> dict:
    counts = [d["count"] for d in days]
    longest = run = 0
    for c in counts:
        run = run + 1 if c > 0 else 0
        longest = max(longest, run)
    i = len(counts) - 1
    if counts[i] == 0:  # today may still be in progress; a streak only breaks once a full day is missed
        i -= 1
    current = 0
    while i >= 0 and counts[i] > 0:
        current += 1
        i -= 1
    busiest = max(days, key=lambda d: (d["count"], d["date"]))

    last = days[-1]["date"]
    ym = []
    y, m = last.year, last.month
    for _ in range(12):
        ym.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    ym.reverse()
    by_month = {k: 0 for k in ym}
    for d in days:
        k = (d["date"].year, d["date"].month)
        if k in by_month:
            by_month[k] += d["count"]
    nz = sorted(c for c in counts if c > 0)

    def q(p: float) -> int:
        return nz[min(len(nz) - 1, int(len(nz) * p))] if nz else 1

    return {
        "total": sum(counts),
        "active": sum(1 for c in counts if c > 0),
        "n_days": len(counts),
        "current": current,
        "longest": longest,
        "busiest": busiest,
        "months": [(MONTHS[m - 1], by_month[(y, m)]) for y, m in ym],
        "thresholds": (q(0.25), q(0.5), q(0.75)),
    }


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------
def portrait_data_uri() -> str | None:
    for c in PORTRAIT_CANDIDATES:
        p = ROOT / c
        if p.exists():
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}[p.suffix[1:].lower()]
            if p.stat().st_size > 250_000:
                print(f"warning: {c} is {p.stat().st_size // 1000} KB; downscale it to keep hero.svg small", file=sys.stderr)
            return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")
    return None


def tile_color(c: str | None) -> str:
    return c or BRAND


def card_hero() -> Card:
    W, H, PAD, BOX = 1000, 320, 72, 188
    c = Card("hero", W, H, f"{NAME}. {ROLE_LINE}", T_HERO)
    boxx, boxy = W - PAD - BOX, (H - BOX) / 2
    max_w = boxx - 40 - PAD
    disp, body, mono = FONTS["display"], FONTS["body"], FONTS["mono"]

    c.defs += [
        '<pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">'
        f'<path d="M40 0H0V40" fill="none" stroke="{EMPTY}"/></pattern>',
        f'<radialGradient id="gp"><stop offset="0" stop-color="{PINK}" stop-opacity=".30"/><stop offset="1" stop-color="{PINK}" stop-opacity="0"/></radialGradient>',
        f'<radialGradient id="gb"><stop offset="0" stop-color="{BRAND}" stop-opacity=".22"/><stop offset="1" stop-color="{BRAND}" stop-opacity="0"/></radialGradient>',
        '<filter id="glow" x="-20%" y="-60%" width="140%" height="220%"><feGaussianBlur stdDeviation="12"/></filter>',
    ]
    c.frame(GROUND)
    c.add('<g clip-path="url(#clip)">')
    c.add(f'<rect width="{W}" height="{H}" fill="url(#grid)"/>')
    c.add('<circle cx="900" cy="20" r="340" fill="url(#gp)"/>')
    c.add('<circle cx="70" cy="340" r="300" fill="url(#gb)"/>')
    c.add("</g>")
    c.border()

    c.add(c.text(PAD, 84, EYEBROW, "mono", 15, BRAND, spacing=2.4, off=0.1))

    nsize = fit(disp, NAME, 92, max_w, 40)
    nx, ny = PAD, 172
    c.chars["display"].update(NAME)
    c.add(
        f'<g{c.cls("rise", 0.25)}>'
        f'<text x="{nx}" y="{ny}" font-size="{nsize}" fill="{BRAND}" opacity=".55" filter="url(#glow)" class="d">{xml_escape(NAME)}</text>'
        f'<text x="{nx}" y="{ny}" font-size="{nsize}" fill="{TEXT}" class="d">{xml_escape(NAME)}</text></g>'
    )

    uw = 160
    c.add(
        f'<path d="M{PAD} 198H{PAD + uw}" pathLength="1" fill="none" stroke="{BRAND}" stroke-width="4" '
        f'stroke-linecap="round"{c.cls("draw", 0.7)}/>'
    )
    c.add(
        f'<path d="M{PAD + uw + 16} 198h24" pathLength="1" fill="none" stroke="{PINK}" stroke-width="4" '
        f'stroke-linecap="round"{c.cls("draw", 0.95)}/>'
    )
    c.smil(
        f'<rect class="smil" x="{PAD}" y="196" width="36" height="4" rx="2" fill="#fff" opacity="0">'
        f'<animate attributeName="x" values="{PAD};{PAD + uw - 36};{PAD}" dur="3.6s" begin="{c.at(1.8)}s" repeatCount="indefinite"/>'
        f'<animate attributeName="opacity" values="0;.8;0" dur="3.6s" begin="{c.at(1.8)}s" repeatCount="indefinite"/></rect>'
    )

    tsize = fit(body, ROLE_LINE, 24, max_w, 14)
    c.add(c.text(PAD, 240, ROLE_LINE, "body", tsize, TEXT, off=0.9))

    x = PAD
    for i, (_, label, _, col) in enumerate(TILES):
        w = mono.width(label, 14) + 32
        col = tile_color(col)
        c.chars["mono"].update(label)
        c.add(
            f'<g{c.cls("rise", 1.1 + 0.12 * i)}>'
            f'<rect x="{x:.1f}" y="262" width="{w:.1f}" height="32" rx="16" fill="none" stroke="{col}"/>'
            f'<text x="{x + w / 2:.1f}" y="283" font-size="14" fill="{col}" text-anchor="middle" class="m">{xml_escape(label)}</text></g>'
        )
        x += w + 12

    uri = portrait_data_uri()
    right = []
    if uri:
        c.defs.append(f'<clipPath id="pc"><rect x="{boxx}" y="{boxy}" width="{BOX}" height="{BOX}" rx="28"/></clipPath>')
        right.append(
            f'<image href="{uri}" x="{boxx}" y="{boxy}" width="{BOX}" height="{BOX}" '
            f'preserveAspectRatio="xMidYMid slice" clip-path="url(#pc)"/>'
        )
        right.append(f'<rect x="{boxx}" y="{boxy}" width="{BOX}" height="{BOX}" rx="28" fill="none" stroke="{BRAND}" stroke-width="2"/>')
    else:
        initials = "".join(p[0] for p in NAME.split()[:2]).upper()
        c.chars["display"].update(initials)
        cx, cy = boxx + BOX / 2, boxy + BOX / 2 + 30
        right.append(f'<rect x="{boxx}" y="{boxy}" width="{BOX}" height="{BOX}" rx="28" fill="{GROUND}" stroke="{BRAND}" stroke-width="2"/>')
        right.append(f'<text x="{cx}" y="{cy}" font-size="84" fill="{BRAND}" opacity=".6" filter="url(#glow)" text-anchor="middle" class="d">{xml_escape(initials)}</text>')
        right.append(f'<text x="{cx}" y="{cy}" font-size="84" fill="{BRAND}" text-anchor="middle" class="d">{xml_escape(initials)}</text>')
    c.add(f'<g{c.cls("rise", 0.4)}>{"".join(right)}</g>')
    c.smil(
        f'<rect class="smil" x="{boxx - 6}" y="{boxy - 6}" width="{BOX + 12}" height="{BOX + 12}" rx="34" fill="none" stroke="{BRAND}" opacity="0">'
        f'<animate attributeName="opacity" values="0;.55;0" dur="3.2s" begin="{c.at(1.2)}s" repeatCount="indefinite"/></rect>'
    )
    return c


def card_tile(i: int, label: str, name: str, url: str, col: str | None, t0: float) -> Card:
    W, H = 325, 150
    col = tile_color(col)
    host = urlparse(url).netloc.removeprefix("www.")
    c = Card(f"tile-{i + 1}", W, H, f"{label.title()}: {name}", t0)
    c.frame(SURFACE, r=18, top_bar=col)
    c.border(18)
    d = FONTS["display"]
    c.add(c.text(24, 44, label, "mono", 14, MUTED, spacing=1.6, off=0.05))
    nsize = fit(d, name, 34, W - 48, 16)
    c.add(c.text(24, 98, ellipsize(d, name, nsize, W - 48), "display", nsize, TEXT, off=0.15))
    c.add(c.text(24, 128, host, "mono", 14, MUTED, off=0.25))
    c.add(
        f'<path d="M283 122H307M299 114L307 122L299 130" pathLength="1" fill="none" stroke="{col}" stroke-width="2.5" '
        f'stroke-linecap="round" stroke-linejoin="round"{c.cls("draw", 0.35)}/>'
    )
    c.add(f'<circle cx="299" cy="38" r="4" fill="{col}"{c.cls("fade", 0.3)}/>')
    c.smil(
        f'<circle class="smil" cx="299" cy="38" r="4" fill="none" stroke="{col}" opacity="0">'
        f'<animate attributeName="r" values="4;13" dur="2.4s" begin="{c.at(1.0)}s" repeatCount="indefinite"/>'
        f'<animate attributeName="opacity" values=".8;0" dur="2.4s" begin="{c.at(1.0)}s" repeatCount="indefinite"/></circle>'
    )
    return c


def card_stats(st: dict) -> Card:
    W, H = 1000, 400
    c = Card("stats", W, H, f"Contributions per month, current streak {st['current']} days, {st['active']} active days", T_STATS)
    mono, disp, body = FONTS["mono"], FONTS["display"], FONTS["body"]
    c.frame(GROUND)
    c.border()

    c.add(c.text(48, 62, "CONTRIBUTIONS", "mono", 15, BRAND, spacing=2.4, off=0.05))
    c.add(c.text(640, 62, f"{fmt(st['total'])} in the last 12 months", "mono", 14, MUTED, anchor="end", off=0.1))

    x0, x1, top, base = 48, 640, 104, 326
    months = st["months"]
    slot = (x1 - x0) / 12
    bw = 28
    peak = max((v for _, v in months), default=0) or 1
    for yy in (top, (top + base) / 2, base):
        c.add(f'<line x1="{x0}" x2="{x1}" y1="{yy}" y2="{yy}" stroke="{LINE}"{c.cls("fade", 0.1)}/>')
    for i, (name, v) in enumerate(months):
        cx = x0 + slot * i + slot / 2
        hot = v == peak and v > 0
        col = PINK if hot else BRAND
        off = 0.25 + 0.06 * i
        bh = max(3.0, (base - top) * v / peak) if v else 2.0
        fill = col if v else LINE
        dim = "" if hot or not v else ' fill-opacity=".78"'
        c.add(
            f'<rect x="{cx - bw / 2:.1f}" y="{base - bh:.1f}" width="{bw}" height="{bh:.1f}" rx="4" fill="{fill}"'
            f'{dim}{c.cls("grow", off)}/>'
        )
        c.add(c.text(cx, base - bh - 9, fmt(v), "mono", 13, TEXT if hot else MUTED, anchor="middle", off=off + 0.45))
        c.add(c.text(cx, 352, name, "mono", 14, MUTED, anchor="middle", off=0.2 + 0.03 * i))

    c.add(f'<line x1="668" x2="668" y1="96" y2="372" stroke="{LINE}"{c.cls("fade", 0.2)}/>')
    busiest = st["busiest"]
    blocks = [
        ("CURRENT STREAK", fmt(st["current"]), "day" if st["current"] == 1 else "days", f"longest: {fmt(st['longest'])} days", BRAND),
        ("ACTIVE DAYS", fmt(st["active"]), f"of {fmt(st['n_days'])} days", f"{round(100 * st['active'] / st['n_days'])}% of the period", PINK),
        ("BUSIEST DAY", fmt(busiest["count"]), "contributions" if busiest["count"] != 1 else "contribution", fmt_day(busiest["date"]), VIOLET),
    ]
    bx = 700
    for i, (label, num, unit, sub, col) in enumerate(blocks):
        y0 = 90 + 98 * i
        off = 0.35 + 0.18 * i
        c.add(c.text(bx, y0 + 14, label, "mono", 14, MUTED, spacing=1.8, off=off))
        c.add(c.text(bx, y0 + 64, num, "display", 54, TEXT, off=off + 0.1))
        c.add(c.text(bx + disp.width(num, 54) + 10, y0 + 64, unit, "body", 18, MUTED, off=off + 0.2))
        c.add(c.text(bx, y0 + 86, sub, "mono", 14, col, off=off + 0.3))
        if i == 0 and st["current"] > 0:
            dx = bx + mono.width(label, 14, 1.8) + 14
            c.add(f'<circle cx="{dx:.1f}" cy="{y0 + 9}" r="4" fill="{col}"{c.cls("fade", off + 0.2)}/>')
            c.smil(
                f'<circle class="smil" cx="{dx:.1f}" cy="{y0 + 9}" r="4" fill="none" stroke="{col}" opacity="0">'
                f'<animate attributeName="r" values="4;12" dur="2.2s" begin="{c.at(1.4)}s" repeatCount="indefinite"/>'
                f'<animate attributeName="opacity" values=".8;0" dur="2.2s" begin="{c.at(1.4)}s" repeatCount="indefinite"/></circle>'
            )
    return c


def star_points(cx: float, cy: float, R: float = 8.0, r: float = 3.4) -> str:
    import math

    pts = []
    for k in range(10):
        ang = -math.pi / 2 + k * math.pi / 5
        rad = R if k % 2 == 0 else r
        pts.append(f"{cx + rad * math.cos(ang):.1f},{cy + rad * math.sin(ang):.1f}")
    return " ".join(pts)


def card_repo(i: int, repo: dict, t0: float) -> Card:
    W, H = 495, 185
    accent = [BRAND, PINK, VIOLET, BRAND][i % 4]
    name = repo["name"]
    c = Card(f"repo-{i + 1}", W, H, f"Repository {name}", t0)
    disp, body, mono = FONTS["display"], FONTS["body"], FONTS["mono"]
    c.frame(SURFACE, r=18, top_bar=accent)
    c.border(18)
    off = 0.0
    tsize = fit(disp, name, 24, W - 56, 14)
    c.add(c.text(28, 52, ellipsize(disp, name, tsize, W - 56), "display", tsize, TEXT, off=off + 0.05))
    desc = (repo.get("description") or "No description yet.").strip()
    for j, ln in enumerate(wrap(body, desc, 16, W - 56, 2)):
        c.add(c.text(28, 86 + 24 * j, ln, "body", 16, MUTED, off=off + 0.15 + 0.08 * j))

    y = 158
    x = 28
    lang = repo.get("language")
    row = []
    if lang:
        col = repo.get("languageColor") or accent
        c.chars["mono"].update(lang)
        row.append(f'<circle cx="{x + 6}" cy="{y - 5}" r="6" fill="{col}"/>')
        row.append(f'<text x="{x + 20}" y="{y}" font-size="14" fill="{TEXT}" class="m">{xml_escape(lang)}</text>')
        x += 20 + mono.width(lang, 14) + 24
    stars, forks = fmt(int(repo.get("stars", 0))), fmt(int(repo.get("forks", 0)))
    c.chars["mono"].update(stars + forks)
    row.append(f'<polygon points="{star_points(x + 8, y - 5)}" fill="none" stroke="{MUTED}" stroke-width="1.5" stroke-linejoin="round"/>')
    row.append(f'<text x="{x + 24}" y="{y}" font-size="14" fill="{MUTED}" class="m">{stars}</text>')
    x += 24 + mono.width(stars, 14) + 22
    fx, fy = x + 8, y - 5
    row.append(
        f'<g fill="none" stroke="{MUTED}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        f'<circle cx="{fx - 5}" cy="{fy - 6}" r="2.4"/><circle cx="{fx + 5}" cy="{fy - 6}" r="2.4"/><circle cx="{fx}" cy="{fy + 6.5}" r="2.4"/>'
        f'<path d="M{fx - 5} {fy - 3.6}V{fy - 1}H{fx + 5}V{fy - 3.6}M{fx} {fy - 1}V{fy + 4}"/></g>'
    )
    row.append(f'<text x="{fx + 16}" y="{y}" font-size="14" fill="{MUTED}" class="m">{forks}</text>')
    c.add(f'<g{c.cls("rise", off + 0.3)}>{"".join(row)}</g>')
    return c


def card_graph(days: list[dict], st: dict) -> Card:
    W, H = 1000, 280
    c = Card("graph", W, H, f"Contribution graph: {fmt(st['total'])} contributions in the last year", T_GRAPH)
    c.frame(GROUND)
    c.border()
    c.add(c.text(36, 46, "CONTRIBUTION GRAPH", "mono", 15, BRAND, spacing=2.4, off=0.05))
    c.add(c.text(964, 46, f"{fmt(st['total'])} contributions in the last year", "mono", 14, MUTED, anchor="end", off=0.1))

    first_wd = (days[0]["date"].weekday() + 1) % 7  # GitHub weeks start on Sunday
    ncols = (len(days) + first_wd - 1) // 7 + 1
    gx, gy, gap = 66, 104, 4
    grid_w = 964 - gx
    pitch = (grid_w + gap) / ncols
    cell = pitch - gap
    scale = [lerp_hex(EMPTY, BRAND, t) for t in (0, 0.30, 0.55, 0.78, 1.0)]
    t1, t2, t3 = st["thresholds"]

    def level(n: int) -> int:
        return 0 if n == 0 else 1 if n <= t1 else 2 if n <= t2 else 3 if n <= t3 else 4

    cols: dict[int, list[str]] = {}
    month_col: dict[int, int] = {}  # month number -> first column holding the 1st of that month
    for k, d in enumerate(days):
        col, row = divmod(k + first_wd, 7)
        if d["date"].day == 1:
            month_col[d["date"].month] = col
        cols.setdefault(col, []).append(
            f'<rect x="{gx + col * pitch:.1f}" y="{gy + row * pitch:.1f}" width="{cell:.1f}" height="{cell:.1f}" rx="3" fill="{scale[level(d["count"])]}"/>'
        )
    for col in sorted(cols):
        c.add(f'<g{c.cls("fade", 0.3 + 0.018 * col)}>{"".join(cols[col])}</g>')

    for m, col in month_col.items():  # label the column that contains the 1st of each month
        c.add(c.text(gx + col * pitch, gy - 14, MONTHS[m - 1], "mono", 14, MUTED, off=0.2 + 0.018 * col))
    for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        c.add(c.text(36, gy + row * pitch + cell - 1, name, "mono", 12, MUTED, off=0.2))

    mono = FONTS["mono"]
    ly = 258
    more_w = mono.width("More", 13)
    sq = 12
    sx = 964 - more_w - 8 - (5 * sq + 4 * 4)
    c.add(c.text(964, ly, "More", "mono", 13, MUTED, anchor="end", off=0.3))
    c.add(c.text(sx - 8, ly, "Less", "mono", 13, MUTED, anchor="end", off=0.3))
    legend = "".join(
        f'<rect x="{sx + i * (sq + 4):.1f}" y="{ly - sq + 1}" width="{sq}" height="{sq}" rx="3" fill="{col}"/>' for i, col in enumerate(scale)
    )
    c.add(f'<g{c.cls("fade", 0.3)}>{legend}</g>')

    gh = 7 * pitch - gap
    c.smil(
        f'<rect class="smil" x="{gx}" y="{gy - 4}" width="2" height="{gh + 8:.1f}" rx="1" fill="{BRAND}" opacity="0">'
        f'<animate attributeName="x" values="{gx};{gx + grid_w - 2};{gx}" dur="7s" begin="{c.at(1.6)}s" repeatCount="indefinite"/>'
        f'<animate attributeName="opacity" values="0;.6;.6;0" keyTimes="0;.05;.95;1" dur="7s" begin="{c.at(1.6)}s" repeatCount="indefinite"/></rect>'
    )
    return c


# --------------------------------------------------------------------------
# README block / preview
# --------------------------------------------------------------------------
def readme_block(prefix: str, repos: list[dict], st: dict) -> str:
    lines = [
        '<p align="center">',
        f'  <img src="{prefix}hero.svg" alt="{attr(NAME + ". " + ROLE_LINE)}" width="100%">',
        "</p>",
        '<p align="center">',
    ]
    for i, (label, name, url, _) in enumerate(TILES):
        lines.append(
            f'  <a href="{attr(url)}"><img src="{prefix}tile-{i + 1}.svg" alt="{attr(label.title() + ": " + name)}" width="32.5%"></a>'
        )
    lines += ["</p>", '<p align="center">', f'  <img src="{prefix}stats.svg" alt="Contributions per month, with current streak, active days and busiest day" width="100%">', "</p>"]
    for a in range(0, len(repos), 2):
        lines.append('<p align="center">')
        for j in range(a, min(a + 2, len(repos))):
            r = repos[j]
            lines.append(f'  <a href="{attr(r["url"])}"><img src="{prefix}repo-{j + 1}.svg" alt="{attr("Repository " + r["name"])}" width="49.5%"></a>')
        lines.append("</p>")
    lines += [
        '<p align="center">',
        f'  <img src="{prefix}graph.svg" alt="Contribution graph, {fmt(st["total"])} contributions in the last year" width="100%">',
        "</p>",
    ]
    return "\n".join(lines)


def update_readme(block: str) -> None:
    if not README.exists():
        README.write_text(f"{MARK_START}\n{block}\n{MARK_END}\n", encoding="utf-8")
        print("created README.md")
        return
    text = README.read_text(encoding="utf-8")
    pat = re.compile(re.escape(MARK_START) + r".*?" + re.escape(MARK_END), re.S)
    if not pat.search(text):
        print(f"note: README.md has no {MARK_START} ... {MARK_END} markers; left unchanged", file=sys.stderr)
        return
    new = pat.sub(lambda _m: f"{MARK_START}\n{block}\n{MARK_END}", text)
    if new != text:
        README.write_text(new, encoding="utf-8")
        print("updated README.md")


def write_preview(block: str) -> None:
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Cards preview (final frame)</title>'
        "<style>body{margin:0;background:#0d1117;display:flex;justify-content:center}"
        "main{width:min(888px,100%);padding:32px 16px;box-sizing:border-box}"
        "p{margin:0 0 16px}img{vertical-align:top}</style></head><body><main>\n"
        + block
        + "\n</main></body></html>\n"
    )
    (OUT_DIR / "preview.html").write_text(page, encoding="utf-8")


# --------------------------------------------------------------------------
def main() -> None:
    data = load_data()
    days = data["days"]
    st = compute_stats(days)
    repos = data.get("repos", [])[:MAX_REPOS]
    if data.get("sample"):
        print("warning: data/contributions.json is SAMPLE data. The daily Action replaces it.", file=sys.stderr)

    OUT_DIR.mkdir(exist_ok=True)
    cards = [card_hero()]
    for i, (label, name, url, col) in enumerate(TILES):
        cards.append(card_tile(i, label, name, url, col, T_TILES + 0.12 * i))
    cards.append(card_stats(st))
    for i, r in enumerate(repos):
        cards.append(card_repo(i, r, T_REPOS + 0.14 * i))
    cards.append(card_graph(days, st))

    keep = set()
    for card in cards:
        path = OUT_DIR / f"{card.stem}.svg"
        path.write_text(card.render(), encoding="utf-8")
        keep.add(path.name)
        print(f"  {path.relative_to(ROOT)}  {path.stat().st_size / 1000:.1f} KB")
    for old in OUT_DIR.glob("repo-*.svg"):  # repos that are no longer pinned
        if old.name not in keep:
            old.unlink()

    block_for = lambda prefix: readme_block(prefix, repos, st)
    if STATIC:
        write_preview(block_for(""))
        print(f"STATIC=1: final-frame cards in {OUT_DIR.relative_to(ROOT)}/. Open preview.html to screenshot.")
        print("These are for screenshots only. Run without STATIC to produce the cards you commit.")
    else:
        update_readme(block_for("cards/"))


if __name__ == "__main__":
    main()
