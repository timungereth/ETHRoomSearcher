"""CLI: which ETH rooms at a given location are free at a given date/time?

Run inside this project:  uv run check_rooms.py --date 2026-09-22 --time 14:00
All API handling lives in room_core.py (see its docstring for the API traps).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from datetime import date, datetime, time

import httpx

import room_core as core


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

    async with httpx.AsyncClient(headers=core.HEADERS, follow_redirects=True) as client:
        rooms = [r for r in await core.fetch_rooms(client)
                 if r.get("locationName") == args.location]
        rooms.sort(key=lambda r: r["id"])
        print(f"{len(rooms)} rooms in {args.location}")
        print(f"checking {at:%a %d %b %Y %H:%M} "
              f"(coverage window +/- {args.window_days} days)\n")

        def prog(done: int, total: int) -> None:
            if done % 40 == 0:
                print(f"  ...{done}/{total}")

        results = await core.check_rooms(
            client, rooms, at, window_days=args.window_days,
            concurrency=args.concurrency, progress=prog)

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

    def window(r: dict) -> str:
        fs, fe = r["free_from"], r["free_until"]
        if not fs and not fe:
            return "all day"
        return f"{hm(fs, 'open')}-{hm(fe, 'close')}"

    print(f"\n{'='*76}\nFREE at {at:%a %d %b %Y %H:%M} — {args.location}\n{'='*76}")
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
