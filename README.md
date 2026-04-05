# dot-ai-usage

A tiny macOS tool that renders your live **Claude Code** and **Codex** quota
bars to a 296×152 1-bit PNG and pushes it to a [Dot.](https://dot.mindreset.tech)
e-ink device every 10 minutes. It's the e-ink equivalent of a menu-bar usage
widget — glanceable, always on, and doesn't use a single byte of your quotas.

![preview](docs/preview.png)

Each tool shows:

- **Session** — 5-hour rolling rate-limit window, `% left` + countdown to reset
- **Weekly** — 7-day rolling rate-limit window, `% left` + countdown
- Plan-tier pill (`Max 20x`, `Pro`, …)
- A shared footer with the owner name (optional) and last-updated timestamp
  in your chosen timezone

## How it works

```
┌──────────────┐      ┌────────────────┐     ┌──────────────┐
│  OpenUsage   │ HTTP │  dot_eink.py   │ API │  Dot. cloud  │
│ (localhost)  │─────▶│  (launchd 10m) │────▶│   → device   │
└──────────────┘      └────────────────┘     └──────────────┘
```

There are three moving parts and each one is the best tool for its job:

1. **Data source — [OpenUsage](https://www.openusage.ai/) running locally.** It
   exposes a unified JSON endpoint at `http://localhost:6736/v1/usage` with the
   authoritative rolling 5h + 7d rate-limit numbers for both Claude Code and
   Codex, pulled from each tool's own native source. That's strictly better
   than any hand-rolled path: `ccusage blocks` uses fixed hour-aligned 5h
   windows (an approximation), Codex's own JSONL only contains rate limits at
   the moment of the last sent message (stale the minute you stop using it),
   and Claude Code doesn't persist rate-limit headers locally at all.
2. **Renderer — [Pillow](https://python-pillow.org/) on a 1-bit PIL image.** The
   Dot. API wants a 296×152 base64-PNG with dithering disabled. Two rows per
   tool, bars stippled in the empty portion so they read as "empty" on 1-bit,
   a title row with a pill badge, a divider, and a footer.
3. **Scheduler — launchd.** One plist under `~/Library/LaunchAgents/`,
   `StartInterval: 600`, logs to `~/Library/Logs/dot-ai-usage.log`. Survives
   reboot. `install.sh` wires it up from a template.

The whole Python script is one file (`dot_eink.py`, ~300 lines, stdlib +
Pillow) and is run through [uv](https://github.com/astral-sh/uv)'s inline
script metadata, so you don't manage a venv — uv caches Pillow the first time
and reuses it on every tick.

## Prerequisites

- **macOS.** This uses launchd. Linux users: the Python script runs anywhere,
  swap `install.sh` for a systemd timer or cron entry.
- **[uv](https://github.com/astral-sh/uv)** — `brew install uv`.
- **[OpenUsage](https://www.openusage.ai/)** installed and running locally, with
  **Launch at Login enabled** in its settings (otherwise a reboot will freeze
  your device on the last image). The tool listens on `localhost:6736`.
- **A Dot. device** with an **Image API content block** added to it in the
  Dot. mobile app (Content Studio → your device → add content → Image API).
  Without that, the upload 404s with `THE_API_KEY_HAS_BEEN_VERIFIED_BUT_THE_IMAGE_API_CONTENT_IS_NOT_FOUND_IN_THE_DEVICE_TASK`.
- **A Dot. API key** from the Dot. app's Developer settings.

## Install

```sh
git clone https://github.com/rayzhux/dot-ai-usage.git
cd dot-ai-usage
cp .env.example .env
$EDITOR .env            # fill in DOT_DEVICE_ID and DOT_API_KEY
./install.sh
tail -f ~/Library/Logs/dot-ai-usage.log
```

`install.sh` renders the launchd plist from `launchd/dot-ai-usage.plist.template`
with your env values, drops the result at `~/Library/LaunchAgents/sh.rayzhux.dot-ai-usage.plist`
(mode 600 because it contains your API key), and bootstraps the agent. You
should see a `POST 200` in the log within a few seconds.

## Try it without installing

```sh
DOT_API_KEY=... DOT_DEVICE_ID=... uv run --script dot_eink.py --dry-run
open preview.png
```

`--dry-run` fetches data, renders `preview.png`, and exits — no upload.

## Configuration

All via environment variables (read by the script; set in `.env` and inlined
into the plist by `install.sh`).

| var                     | required | default                     | notes                                                |
|-------------------------|----------|-----------------------------|------------------------------------------------------|
| `DOT_DEVICE_ID`         | yes      | —                           | device serial, shown in the Dot. mobile app          |
| `DOT_API_KEY`           | yes      | —                           | Dot. app → Developer settings                        |
| `DOT_OWNER_NAME`        | no       | *(empty = hide)*            | shown in the bottom-left footer                      |
| `DOT_TZ`                | no       | `UTC`                       | IANA zone for the timestamp (e.g. `Asia/Dubai`)      |
| `DOT_INTERVAL_SECONDS`  | no       | `600`                       | launchd `StartInterval`                              |
| `DOT_STALE_SECONDS`     | no       | `900`                       | flag stale if OpenUsage data is older than this      |
| `OPENUSAGE_URL`         | no       | `http://localhost:6736/v1/usage` | override if you run OpenUsage elsewhere         |
| `DOT_FONT_BOLD` / `DOT_FONT_REG` | no | `/System/Library/Fonts/SFNS.ttf` | swap the font if you want a different look |

## Uninstall

```sh
./uninstall.sh
```

Stops the launchd agent and removes the installed plist. Leaves your `.env`,
the clone, and the log file alone.

## Customization tips

- **Different layout** — everything happens in `render_png()`. The file is
  short; the title row / bar rows / footer are each isolated helpers.
- **Different font** — set `DOT_FONT_BOLD` / `DOT_FONT_REG` to any TTF on your
  system, or bundle one in the repo and point the env vars at it.
- **Different cadence** — edit `DOT_INTERVAL_SECONDS` in `.env` and re-run
  `./install.sh` (it'll bootout + re-bootstrap the agent with the new value).
- **Show sub-plan bars** (Sonnet, Spark, Today's cost…) — OpenUsage returns
  these in the same payload (`provider.lines[]` has extra `progress` and
  `text` entries). Uncomment/extend `_draw_section()` in `dot_eink.py` to draw
  whichever fits.

## Why SFNS, not a pixel font?

I started with [Silkscreen](https://fonts.google.com/specimen/Silkscreen)
thinking a true pixel-grid font would look sharper on 1-bit e-ink. It didn't.
Silkscreen is all-caps and only renders pixel-perfect at 8 px and 16 px, so
pairing them for a title/body hierarchy gives you a 2× jump with no middle
ground — "CLAUDE" in chunky 16 px over tiny 8 px body text destroyed the
clean dashboard look. macOS's hand-hinted SFNS at 20 / 13 / 11 / 10 px
thresholds cleanly to 1-bit and preserves real typographic hierarchy. If you
prefer the pixel-font aesthetic, try **Pixel Operator** or **Departure Mono**
and override `DOT_FONT_BOLD` / `DOT_FONT_REG`.

## License

MIT — see [LICENSE](./LICENSE).
