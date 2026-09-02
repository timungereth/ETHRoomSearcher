# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Which ETH rooms at a given location are free at a given date/time?

Data source: the public JSON API behind
https://ethz.ch/staffnet/en/service/rooms-and-buildings/roominfo.html
  room list   : /bin/ethz/roominfo?path=/v2/rooms&lang=en
  allocations : /bin/ethz/roominfo?path=/rooms/{key}/allocations&from=..&to=..

IMPORTANT: the room list returns zero-padded ids ("HG F 030") but the
allocations endpoint only accepts the unpadded "<building> <floor> <room>"
form ("HG F 30").  A padded id returns an empty list instead of an error,
which makes every room look free.  We therefore build the key from the
building/floor/room fields, never from `id`.

Because an empty response is indistinguishable from "no data published", we
also pull a wider window around the target date: a room with no allocation
at all in that window is reported as UNKNOWN (no published occupancy) rather
than as free.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from datetime import date, datetime, time, timedelta
from urllib.parse import quote

import httpx

BASE = "https://ethz.ch/bin/ethz/roominfo"
ROOMS_URL = f"{BASE}?path=/v2/rooms&lang=en"


def alloc_key(room: dict) -> str:
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


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-09-22")
    ap.add_argument("--time", default="14:00")
    ap.add_argument("--location", default="Zürich Zentrum")
    ap.add_argument("--window-days", type=int, default=21,
                    help="+/- days pulled around the date to detect data coverage")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out", default="free_rooms")
    args = ap.parse_args()

    d = date.fromisoformat(args.date)
    hh, mm = (int(x) for x in args.time.split(":"))
    at = datetime.combine(d, time(hh, mm))
    w_from = (d - timedelta(days=args.window_days)).isoformat()
    w_to = (d + timedelta(days=args.window_days)).isoformat()

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        follow_redirects=True,
    ) as client:
        rooms = [r for r in await fetch_json(client, ROOMS_URL)
                 if r.get("locationName") == args.location]
        rooms.sort(key=lambda r: r["id"])
        print(f"{len(rooms)} rooms in {args.location}")
        print(f"checking {at:%a %d %b %Y %H:%M} (coverage window {w_from} .. {w_to})\n")

        sem = asyncio.Semaphore(args.concurrency)
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
                "id": room["id"], "key": key, "building": room["building"],
                "seats": room["seats"], "type": room["type"],
                "covered": bool(win),          # any data at all in the window
                "free": not blocking,
                "blocking": blocking, "day": day,
                "free_from": fs, "free_until": fe,
            })
            done += 1
            if done % 40 == 0:
                print(f"  ...{done}/{len(rooms)}")

        await asyncio.gather(*(one(r) for r in rooms))

    results.sort(key=lambda r: r["id"])
    free = [r for r in results if r["covered"] and r["free"]]
    busy = [r for r in results if r["covered"] and not r["free"]]
    unknown = [r for r in results if not r["covered"]]

    def hm(dt: datetime | None, fb: str) -> str:
        return dt.strftime("%H:%M") if dt else fb

    with open(f"{args.out}.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["room", "building", "seats", "type", "status",
                    "free_from", "free_until", "bookings_that_day"])
        for r in free + busy + unknown:
            status = "FREE" if r in free else ("BUSY" if r in busy else "NO DATA")
            books = "; ".join(
                f"{e['from']:%H:%M}-{e['to']:%H:%M} "
                f"{e['title'] or 'closed/not bookable'}"
                for e in r["day"] if not e["cancelled"]
            )
            w.writerow([r["id"], r["building"], r["seats"], r["type"], status,
                        hm(r["free_from"], "open" if r["covered"] else ""),
                        hm(r["free_until"], "close" if r["covered"] else ""), books])

    json.dump(results, open(f"{args.out}.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)

    print(f"\n{'='*76}\nFREE at {at:%a %d %b %Y %H:%M} — {args.location}\n{'='*76}")
    def window(r: dict) -> str:
        fs, fe = r["free_from"], r["free_until"]
        if not fs and not fe:
            return "all day"
        return f"{hm(fs, 'open')}-{hm(fe, 'close')}"

    print(f"{'ROOM':<15}{'SEATS':>6}  {'FREE FROM-UNTIL':<16}TYPE")
    print("-" * 76)
    for r in free:
        print(f"{r['id']:<15}{(r['seats'] or '-'):>6}  {window(r):<16}{r['type']}")
    print("-" * 76)
    print(f"FREE {len(free)}   BUSY {len(busy)}   NO PUBLISHED DATA {len(unknown)}"
          f"   (of {len(results)})")
    if busy:
        print("\nBUSY at that moment:")
        for r in busy:
            for b in r["blocking"]:
                print(f"  {r['id']:<15}{b['from']:%H:%M}-{b['to']:%H:%M}  "
                      f"{b['title'] or 'closed/not bookable'}")
    print(f"\nWrote {args.out}.csv / {args.out}.json")


if __name__ == "__main__":
    asyncio.run(main())
