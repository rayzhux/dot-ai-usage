#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10"]
# ///
"""Render Claude Code + Codex quota bars to a 296x152 1-bit PNG and push it to
a Dot. e-ink device.

Data source: OpenUsage (https://openusage.ai) running locally at :6736.
Scheduling: launchd (see launchd/dot-ai-usage.plist.template + install.sh).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

# ---------- config ----------

WIDTH, HEIGHT = 296, 152

DEVICE_ID = os.environ.get("DOT_DEVICE_ID", "").strip()
API_KEY = os.environ.get("DOT_API_KEY", "").strip()
API_URL = (
    f"https://dot.mindreset.tech/api/authV2/open/device/{DEVICE_ID}/image"
    if DEVICE_ID
    else ""
)

OPENUSAGE_URL = os.environ.get("OPENUSAGE_URL", "http://localhost:6736/v1/usage")
OWNER_NAME = os.environ.get("DOT_OWNER_NAME", "").strip()
TZ_NAME = os.environ.get("DOT_TZ", "UTC").strip() or "UTC"
# IANA drops friendly abbreviations for most zones now (e.g. Asia/Dubai → "+04"
# instead of "GST"). Set DOT_TZ_ABBR to override with whatever you want shown.
TZ_ABBR_OVERRIDE = os.environ.get("DOT_TZ_ABBR", "").strip()
# How old OpenUsage data can be before we flag it stale (a small dot next to the
# owner name in the footer). OpenUsage caches for a bit, so 900s is generous.
STALE_AFTER_SECONDS = int(os.environ.get("DOT_STALE_SECONDS", "900"))

# macOS San Francisco. SFNS is a variable font and Pillow handles the weight
# axis, so we use one file for both bold and regular. SF is hand-hinted at small
# sizes and thresholds cleanly to 1-bit. Tried pixel fonts (Silkscreen, Pixel
# Operator) first — at 8/16 px the hierarchy pairing is too extreme and the
# all-caps glyphs fight the dashboard aesthetic. SF at 20/13/11/10 px keeps the
# modern sans look from the reference mock and reads crisp on the panel.
FONT_BOLD_PATH = os.environ.get("DOT_FONT_BOLD", "/System/Library/Fonts/SFNS.ttf")
FONT_REG_PATH = os.environ.get("DOT_FONT_REG", "/System/Library/Fonts/SFNS.ttf")
_FALLBACK_FONT = "/System/Library/Fonts/Helvetica.ttc"


# ---------- data model ----------


@dataclass
class Quota:
    percent_left: float        # 0..100
    resets_at: datetime        # tz-aware

    def reset_label(self) -> str:
        delta = self.resets_at - datetime.now(timezone.utc)
        secs = int(delta.total_seconds())
        if secs <= 0:
            return "now"
        days, rem = divmod(secs, 86_400)
        hours, rem = divmod(rem, 3_600)
        mins = rem // 60
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{mins}m"


@dataclass
class ToolQuota:
    name: str
    plan: str
    session: Quota | None
    weekly: Quota | None


@dataclass
class Snapshot:
    claude: ToolQuota
    codex: ToolQuota
    fetched_at: datetime | None  # newest fetchedAt from openusage, or None


# ---------- openusage (single source of truth) ----------


def fetch_openusage() -> Snapshot:
    """Fetch live Claude + Codex quotas from the local OpenUsage daemon.

    OpenUsage (https://www.openusage.ai/) runs a local HTTP API at :6736 that
    exposes authoritative rolling 5h/7d rate-limit data for both Claude Code and
    Codex, pulled from each tool's native source. This is more accurate than
    anything else we can do locally:
      * ccusage `blocks` uses fixed hour-aligned 5h windows — an approximation
        of Anthropic's rolling rate limit, off by up to ~4h.
      * Raw Codex JSONL only contains rate_limits at the moment of the last
        sent message, so it's stale whenever you haven't used Codex recently.
      * Claude doesn't persist rate-limit headers locally at all.

    Returns a Snapshot. Missing providers degrade to all-None ToolQuota so the
    renderer still has something to draw (showing "--" in place of numbers).
    """
    default = Snapshot(
        claude=ToolQuota("Claude", "", None, None),
        codex=ToolQuota("Codex", "", None, None),
        fetched_at=None,
    )
    try:
        with urllib.request.urlopen(OPENUSAGE_URL, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as e:  # noqa: BLE001
        print(f"openusage fetch failed: {e}", file=sys.stderr)
        return default

    tools: dict[str, ToolQuota] = {}
    newest_fetched: datetime | None = None

    for provider in data:
        pid = provider.get("providerId")
        name = provider.get("displayName") or pid or "?"
        plan = provider.get("plan") or ""
        session: Quota | None = None
        weekly: Quota | None = None
        for line in provider.get("lines", []):
            if line.get("type") != "progress":
                continue
            label = (line.get("label") or "").lower()
            used = float(line.get("used") or 0.0)
            limit = float(line.get("limit") or 100.0)
            resets_iso = line.get("resetsAt")
            if not resets_iso or limit <= 0:
                continue
            percent_left = max(0.0, 100.0 - (100.0 * used / limit))
            try:
                resets_at = datetime.fromisoformat(resets_iso.replace("Z", "+00:00"))
            except ValueError:
                continue
            q = Quota(percent_left=percent_left, resets_at=resets_at)
            if label == "session" and session is None:
                session = q
            elif label == "weekly" and weekly is None:
                weekly = q
        tools[pid] = ToolQuota(name=name, plan=plan, session=session, weekly=weekly)

        fetched_iso = provider.get("fetchedAt")
        if fetched_iso:
            try:
                fetched = datetime.fromisoformat(fetched_iso.replace("Z", "+00:00"))
                if newest_fetched is None or fetched > newest_fetched:
                    newest_fetched = fetched
            except ValueError:
                pass

    claude = tools.get("claude") or ToolQuota("Claude", "", None, None)
    codex = tools.get("codex") or ToolQuota("Codex", "", None, None)
    return Snapshot(claude=claude, codex=codex, fetched_at=newest_fetched)


# ---------- rendering ----------


def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        try:
            return ImageFont.truetype(_FALLBACK_FONT, size)
        except OSError:
            return ImageFont.load_default()


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _draw_pill(draw: ImageDraw.ImageDraw, right_x: int, y: int, text: str, font) -> int:
    if not text:
        return right_x
    pad_x, pad_y = 6, 2
    tw = _text_w(draw, text, font)
    w = tw + pad_x * 2
    h = font.size + pad_y * 2 + 2
    x0 = right_x - w
    draw.rounded_rectangle((x0, y, x0 + w, y + h), radius=h // 2, outline=0, width=1)
    draw.text((x0 + pad_x, y + pad_y - 1), text, font=font, fill=0)
    return x0


def _draw_bar(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, pct_left: float) -> None:
    draw.rectangle((x, y, x + w, y + h), outline=0, width=1)
    inner_w = w - 2
    fill_w = int(round(inner_w * max(0.0, min(100.0, pct_left)) / 100.0))
    if fill_w > 0:
        draw.rectangle((x + 1, y + 1, x + 1 + fill_w, y + h - 1), fill=0)
    # stipple the empty portion so it reads as "empty" on 1-bit
    for sx in range(x + 1 + fill_w + 1, x + w - 1, 2):
        for sy in range(y + 2, y + h - 1, 2):
            draw.point((sx, sy), fill=0)


def _status_text(q: Quota | None) -> str:
    return "--" if q is None else f"{int(round(q.percent_left))}%  {q.reset_label()}"


def _draw_section(
    draw: ImageDraw.ImageDraw,
    y0: int,
    tool: ToolQuota,
    fonts: dict,
    bar_x1: int,
) -> int:
    """Draw one tool block. `bar_x1` is the shared right edge for all bars in
    the image so Session/Weekly/Session/Weekly all have identical lengths."""
    x_margin = 6
    draw.text((x_margin, y0), tool.name, font=fonts["title"], fill=0)
    pill_y = y0 + (fonts["title"].size - fonts["pill"].size) // 2 - 1
    _draw_pill(draw, WIDTH - x_margin, pill_y, tool.plan, fonts["pill"])
    y = y0 + fonts["title"].size + 3

    def _row(label: str, q: Quota | None) -> None:
        nonlocal y
        row_h = fonts["label"].size + 7
        draw.text((x_margin, y + 1), label, font=fonts["label"], fill=0)
        status = _status_text(q)
        sw = _text_w(draw, status, fonts["status"])
        draw.text((WIDTH - x_margin - sw, y + 1), status, font=fonts["status"], fill=0)
        bar_x0 = x_margin + 62
        if bar_x1 - bar_x0 >= 20:
            bar_h = 8
            bar_y = y + (row_h - bar_h) // 2
            _draw_bar(draw, bar_x0, bar_y, bar_x1 - bar_x0, bar_h, q.percent_left if q else 0.0)
        y += row_h

    _row("Session", tool.session)
    _row("Weekly", tool.weekly)
    return y


def render_png(snap: Snapshot) -> bytes:
    img = Image.new("1", (WIDTH, HEIGHT), color=1)  # 1 = white
    draw = ImageDraw.Draw(img)

    fonts = {
        "title": _load_font(FONT_BOLD_PATH, 20),
        "pill": _load_font(FONT_REG_PATH, 11),
        "label": _load_font(FONT_BOLD_PATH, 13),
        "status": _load_font(FONT_REG_PATH, 13),
        "meta": _load_font(FONT_REG_PATH, 10),
    }

    # Compute a single bar_x1 shared by all 4 rows so every progress bar has
    # the same length regardless of how wide its status text is.
    x_margin = 6
    max_sw = max(
        _text_w(draw, _status_text(q), fonts["status"])
        for q in (snap.claude.session, snap.claude.weekly, snap.codex.session, snap.codex.weekly)
    )
    bar_x1 = WIDTH - x_margin - max_sw - 8

    y = 2
    y = _draw_section(draw, y, snap.claude, fonts, bar_x1)
    y += 2
    draw.line((x_margin, y, WIDTH - x_margin, y), fill=0, width=1)
    y += 4
    _draw_section(draw, y, snap.codex, fonts, bar_x1)

    # footer: owner name (optional) bottom-left, local-time stamp bottom-right.
    # If openusage data is stale, draw a filled dot next to the owner name so
    # you can tell the upstream daemon froze without reading the log.
    x_margin = 6
    foot_y = HEIGHT - fonts["meta"].size - 2
    now_local = datetime.now(ZoneInfo(TZ_NAME))
    tz_abbr = TZ_ABBR_OVERRIDE or now_local.strftime("%Z") or TZ_NAME
    stamp = f"Updated {now_local.strftime('%H:%M')} {tz_abbr}".rstrip()
    sw = _text_w(draw, stamp, fonts["meta"])
    draw.text((WIDTH - x_margin - sw, foot_y), stamp, font=fonts["meta"], fill=0)

    stale = (
        snap.fetched_at is None
        or (datetime.now(timezone.utc) - snap.fetched_at) > timedelta(seconds=STALE_AFTER_SECONDS)
    )
    x_cursor = x_margin
    if stale:
        r = 2
        cy = foot_y + fonts["meta"].size // 2 + 1
        draw.ellipse((x_cursor, cy - r, x_cursor + 2 * r, cy + r), fill=0)
        x_cursor += 2 * r + 3
    if OWNER_NAME:
        draw.text((x_cursor, foot_y), OWNER_NAME, font=fonts["meta"], fill=0)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------- upload ----------


def post_image(png_bytes: bytes) -> None:
    if not API_KEY:
        raise RuntimeError("DOT_API_KEY is not set")
    if not DEVICE_ID:
        raise RuntimeError("DOT_DEVICE_ID is not set")
    body = json.dumps(
        {
            "image": base64.b64encode(png_bytes).decode("ascii"),
            "refreshNow": True,
            "border": 0,
            "ditherType": "NONE",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(
            f"[{datetime.now().isoformat(timespec='seconds')}] dot.app POST {resp.status}",
            flush=True,
        )


# ---------- main ----------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect + render to preview.png, skip the upload",
    )
    ap.add_argument("--preview", default="preview.png", help="Dry-run output path")
    args = ap.parse_args()

    snap = fetch_openusage()
    print(
        f"claude={snap.claude} codex={snap.codex} fetched_at={snap.fetched_at}",
        flush=True,
    )

    png = render_png(snap)

    if args.dry_run:
        Path(args.preview).write_bytes(png)
        print(f"wrote {args.preview} ({len(png)} bytes)")
        return 0

    post_image(png)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"dot-ai-usage error: {e}", file=sys.stderr)
        sys.exit(1)
