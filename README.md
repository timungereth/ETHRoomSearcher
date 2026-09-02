# ETH Room Finder

Find free rooms at ETH Zürich for any location, date and time — with instant
filters for minimum seats, "free until at least", and room type.

Data comes from the public JSON API behind the official
[roominfo page](https://ethz.ch/staffnet/en/service/rooms-and-buildings/roominfo.html).

## Use in the browser (easiest — nothing to install)

The app runs on Streamlit Community Cloud; anyone can open it on any device
(phone included) — no download, no install:

> **https://eth-room-finder.streamlit.app** *(deploy once: sign in at
> [share.streamlit.io](https://share.streamlit.io) with GitHub → New app →
> repo `timungereth/ETHRoomSearcher`, branch `main`, file `app.py`, and set
> the subdomain to `eth-room-finder`.)*

Free-tier note: after ~12 h without visitors the app sleeps; the next visitor
sees a "get this app back up" button and waits ~30 s. Run it locally with
`uv run --group web streamlit run app.py`.

Why not a plain static web page? The ETH API sends no CORS headers, so a
browser cannot call it directly — a small server (Streamlit here) has to sit
in between.

## Download & run (no install)

Grab the build for your OS from the [**Releases** page](https://github.com/timungereth/ETHRoomSearcher/releases), unzip, run:

| OS | File | Note |
|---|---|---|
| Windows | `eth-room-finder-windows-x86_64.zip` | SmartScreen may warn (unsigned): *More info → Run anyway* |
| macOS (Apple Silicon) | `eth-room-finder-macos-arm64.zip` | Unsigned app: right-click → *Open* the first time |
| macOS (Intel) | `eth-room-finder-macos-x86_64.zip` | same |
| Linux | `eth-room-finder-linux-x86_64.tar.gz` | `tar xzf … && ./eth-room-finder` |

## Use

1. Pick **location** (e.g. Zürich Zentrum), **date** and **time**, press **Search**.
2. Filter the result instantly: **Min seats**, **Free until ≥** (e.g. `16:00`),
   **Type**, and **Show** (free only / + no data / all).
3. **Double-click** a room to open its official ETH detail page.
   **Export CSV…** saves the current view.

Status meanings:

- **FREE** — no booking overlaps the chosen time (a booking ending exactly
  then counts as free).
- **BUSY** — a booking or closure covers that moment.
- **NO DATA** — ETH publishes no occupancy for this room at all
  (mostly exhibition/outdoor areas). *Not* guaranteed free.

## Run from source

Needs [uv](https://docs.astral.sh/uv/):

```sh
uv run python gui.py                       # GUI
uv run check_rooms.py --location "Zürich Zentrum" --date 2026-09-22 --time 14:00   # CLI
```

## Build executables

Locally (builds for the OS you are on — PyInstaller cannot cross-compile):

```sh
uv sync
uv run pyinstaller --noconfirm --onefile --windowed --name eth-room-finder gui.py
```

CI: `.github/workflows/build.yml` builds Windows, macOS (arm64 + Intel) and
Linux on every `v*` tag push and attaches them to a GitHub Release.

## API notes (the traps)

- The room list returns zero-padded ids (`HG F 030`) but the allocations
  endpoint only accepts the unpadded `<building> <floor> <room>` form
  (`HG F 30`) — a padded id silently returns an empty list, making every
  room look free.
- An empty allocations response is indistinguishable from "no occupancy
  published", so a ±21-day window around the target date is used as a
  coverage probe; rooms with nothing at all in it are reported **NO DATA**.
- Entries with `belegungstyp` 8 and no event are outside-opening-hours
  closures; cancelled events (`findetStatt == 0`) do not occupy a room.
