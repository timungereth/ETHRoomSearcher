"""Core logic shared by the CLI (check_rooms.py) and the GUI (gui.py).

Data source: the public JSON API behind
https://ethz.ch/staffnet/en/service/rooms-and-buildings/roominfo.html
  room list   : /bin/ethz/roominfo?path=/v2/rooms&lang=en
  allocations : /bin/ethz/roominfo?path=/rooms/{key}/allocations&from=..&to=..

API traps this module works around:

- The room list returns zero-padded ids ("HG F 030") but the allocations
  endpoint only accepts the unpadded "<building> <floor> <room>" form
  ("HG F 30").  A padded id returns an empty list instead of an error,
  which makes every room look free.  The key is therefore built from the
  building/floor/room fields, never from `id` (see alloc_key()).
- An empty response is indistinguishable from "no occupancy published", so
  a wider window around the target date is pulled as a coverage probe.
  Rooms with no allocation at all in that window get covered=False.

Occupancy uses half-open intervals: a booking ending exactly at the queried
time leaves the room free.  Cancelled events (findetStatt == 0) do not
occupy the room.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx

BASE = "https://ethz.ch/bin/ethz/roominfo"
ROOMS_URL = f"{BASE}?path=/v2/rooms&lang=en"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def alloc_key(room: dict) -> str:
    """Unpadded '<building> <floor> <room>' key the allocations endpoint needs."""
    return f"{room['building']} {room['floor']} {room['room']}"


async def fetch_json(client: httpx.AsyncClient, url: str, tries: int = 4):
    last = None
    for i in range(tries):
        try:
            r = await client.get(url, timeout=60.0)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            await asyncio.sleep(1.5 * (i + 1))
    raise RuntimeError(f"failed {url}: {last}")


def entries(allocs: list) -> list[dict]:
    out = []
    for a in allocs or []:
        try:
            f, t = datetime.fromisoformat(a["date_from"]), datetime.fromisoformat(a["date_to"])
        except Exception:  # noqa: BLE001
            continue
        bs = a.get("belegungsserie") or {}
        ev = bs.get("veranstaltung") or {}
        out.append({
            "from": f, "to": t,
            "title": ev.get("allocationTitle"),
            "organiser": ev.get("veranstalter"),
            "kind": ev.get("veranstaltungstyp"),
            "typ": bs.get("belegungstyp"),
            # findetStatt == 0 means the event was cancelled -> room not occupied
            "cancelled": bool(ev) and ev.get("findetStatt") == 0,
        })
    out.sort(key=lambda e: e["from"])
    return out


def free_window(day: list[dict], at: datetime) -> tuple[datetime | None, datetime | None]:
    start = end = None
    for e in day:
        if e["cancelled"]:
            continue
        if e["to"] <= at and (start is None or e["to"] > start):
            start = e["to"]
        if e["from"] > at and (end is None or e["from"] < end):
            end = e["from"]
    return start, end


async def fetch_rooms(client: httpx.AsyncClient) -> list[dict]:
    """The full public room list (all locations)."""
    return await fetch_json(client, ROOMS_URL)


async def check_rooms(
    client: httpx.AsyncClient,
    rooms: list[dict],
    at: datetime,
    window_days: int = 21,
    concurrency: int = 8,
    progress=None,
) -> list[dict]:
    """Check every room in `rooms` for availability at instant `at`."""
    d = at.date()
    w_from = (d - timedelta(days=window_days)).isoformat()
    w_to = (d + timedelta(days=window_days)).isoformat()
    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []
    done = 0

    async def one(room: dict) -> None:
        nonlocal done
        key = alloc_key(room)
        url = f"{BASE}?path=/rooms/{quote(key)}/allocations&from={w_from}&to={w_to}"
        async with sem:
            allocs = await fetch_json(client, url)
        win = entries(allocs)
        day = [e for e in win if e["from"].date() <= d <= e["to"].date()]
        blocking = [e for e in day
                    if not e["cancelled"] and e["from"] <= at < e["to"]]
        fs, fe = free_window(day, at)
        results.append({
            "id": room["id"], "key": key,
            "building": room["building"], "floor": room["floor"],
            "roomnr": room["room"], "seats": room["seats"], "type": room["type"],
            "covered": bool(win),          # any data at all in the window
            "free": not blocking,
            "blocking": blocking, "day": day,
            "free_from": fs, "free_until": fe,
        })
        done += 1
        if progress:
            progress(done, len(rooms))

    await asyncio.gather(*(one(r) for r in rooms))
    results.sort(key=lambda r: r["id"])
    return results


def status_of(r: dict) -> str:
    if not r["covered"]:
        return "NO DATA"
    return "FREE" if r["free"] else "BUSY"
