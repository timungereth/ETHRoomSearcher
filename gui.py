"""ETH Room Finder — tkinter GUI around room_core.py.

Search any ETH location for rooms free at a given date/time; filter the
results instantly by minimum seats, "free until at least", and room type.
Double-click a room to open its official ETH detail page.

`--check` runs a headless self-test (deps + TLS + API reachable) and exits;
used as the CI smoke test for the packaged executables.
"""
from __future__ import annotations

import asyncio
import csv
import queue
import sys
import threading
import webbrowser
from datetime import date, datetime, time as dtime

import httpx

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import room_core as core

DETAIL_URL = ("https://ethz.ch/staffnet/en/service/rooms-and-buildings/"
              "roominfo/detail.html?building={b}&floor={f}&room={r}")
MODES = ("Free only", "Free + no data", "All")
COLS = ("room", "seats", "from", "until", "type", "status", "info")
HEADINGS = {"room": "Room", "seats": "Seats", "from": "Free from",
            "until": "Free until", "type": "Type", "status": "Status",
            "info": "Current / next booking"}


def _say(msg: str) -> None:
    if sys.stdout is not None:  # stdout is None in a --windowed build
        print(msg)


def self_check() -> int:
    """Headless smoke test: bundled deps + TLS certs + API reachable."""
    try:
        r = httpx.get(f"{core.BASE}?path=/locations", headers=core.HEADERS,
                      timeout=20.0, follow_redirects=True)
        r.raise_for_status()
        names = [loc.get("areaDesc") for loc in r.json()]
        if not names:
            raise RuntimeError("empty locations list")
    except Exception as e:  # noqa: BLE001
        _say(f"self-check FAILED: {e}")
        return 1
    _say(f"self-check OK: {len(names)} locations, tkinter {tk.TkVersion}")
    return 0


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("ETH Room Finder")
        root.geometry("1120x680")
        root.minsize(900, 520)

        style = ttk.Style()
        if sys.platform.startswith("linux"):
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass

        self.q: queue.Queue = queue.Queue()
        self.all_rooms: list[dict] = []
        self.results: list[dict] = []
        self.shown: list[dict] = []
        self.by_iid: dict[str, dict] = {}
        self.search_at: datetime | None = None
        self.search_loc = ""
        self.searching = False
        self.sort_col = "room"
        self.sort_desc = False

        # --- row 1: what to search -------------------------------------
        r1 = ttk.Frame(root, padding=(10, 8, 10, 2))
        r1.pack(fill="x")
        ttk.Label(r1, text="Location").pack(side="left")
        self.loc_var = tk.StringVar(value="Zürich Zentrum")
        self.loc_box = ttk.Combobox(r1, textvariable=self.loc_var,
                                    state="disabled", width=22)
        self.loc_box.pack(side="left", padx=(4, 14))
        ttk.Label(r1, text="Date").pack(side="left")
        self.date_var = tk.StringVar(value=date.today().isoformat())
        ttk.Entry(r1, textvariable=self.date_var, width=11).pack(side="left", padx=(4, 14))
        ttk.Label(r1, text="Time").pack(side="left")
        self.time_var = tk.StringVar(value="14:00")
        ttk.Entry(r1, textvariable=self.time_var, width=6).pack(side="left", padx=(4, 14))
        self.search_btn = ttk.Button(r1, text="Search", command=self.on_search,
                                     state="disabled")
        self.search_btn.pack(side="left")
        self.progress = ttk.Progressbar(r1, mode="indeterminate", length=200)
        self.progress.pack(side="right")
        self.progress.start(12)

        # --- row 2: instant view filters -------------------------------
        r2 = ttk.Frame(root, padding=(10, 4, 10, 6))
        r2.pack(fill="x")
        ttk.Label(r2, text="Min seats").pack(side="left")
        self.seats_var = tk.StringVar(value="0")
        sp = ttk.Spinbox(r2, from_=0, to=999, textvariable=self.seats_var,
                         width=5, command=self.refresh)
        sp.pack(side="left", padx=(4, 14))
        sp.bind("<KeyRelease>", self.refresh)
        ttk.Label(r2, text="Free until ≥").pack(side="left")
        self.until_var = tk.StringVar(value="")
        ue = ttk.Entry(r2, textvariable=self.until_var, width=6)
        ue.pack(side="left", padx=(4, 14))
        ue.bind("<KeyRelease>", self.refresh)
        ttk.Label(r2, text="Type").pack(side="left")
        self.type_var = tk.StringVar(value="All")
        self.type_box = ttk.Combobox(r2, textvariable=self.type_var,
                                     state="readonly", width=20, values=("All",))
        self.type_box.pack(side="left", padx=(4, 14))
        self.type_box.bind("<<ComboboxSelected>>", self.refresh)
        ttk.Label(r2, text="Show").pack(side="left")
        self.show_var = tk.StringVar(value=MODES[0])
        show = ttk.Combobox(r2, textvariable=self.show_var, state="readonly",
                            width=14, values=MODES)
        show.pack(side="left", padx=(4, 14))
        show.bind("<<ComboboxSelected>>", self.refresh)
        self.export_btn = ttk.Button(r2, text="Export CSV…", command=self.on_export,
                                     state="disabled")
        self.export_btn.pack(side="right")

        # --- status bar + results table --------------------------------
        self.status = ttk.Label(root, anchor="w", padding=(10, 4),
                                text="Loading room list…")
        self.status.pack(side="bottom", fill="x")

        tf = ttk.Frame(root, padding=(10, 0, 10, 4))
        tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=COLS, show="headings",
                                 selectmode="browse")
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        widths = {"room": 130, "seats": 55, "from": 80, "until": 80,
                  "type": 150, "status": 75, "info": 400}
        for c in COLS:
            self.tree.heading(c, text=HEADINGS[c],
                              command=lambda c=c: self.on_sort(c))
            self.tree.column(c, width=widths[c], stretch=(c == "info"),
                             anchor=("e" if c == "seats" else "w"))
        self.tree.tag_configure("FREE", background="#e6f4ea")
        self.tree.tag_configure("BUSY", background="#fdecea")
        self.tree.tag_configure("NO DATA", background="#eff1f3")
        self.tree.bind("<Double-1>", self.on_open)

        root.bind("<Return>", lambda e: self.on_search())
        root.after(100, self.poll)
        self.bg(self.load_rooms_bg)

    # ------------------------------------------------------------------
    def bg(self, fn, *a) -> None:
        threading.Thread(target=fn, args=a, daemon=True).start()

    def load_rooms_bg(self) -> None:
        try:
            async def go():
                async with httpx.AsyncClient(headers=core.HEADERS,
                                             follow_redirects=True) as c:
                    return await core.fetch_rooms(c)
            self.q.put(("rooms", asyncio.run(go())))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"Could not load the room list:\n{e}"))

    def search_bg(self, rooms: list[dict], at: datetime) -> None:
        try:
            async def go():
                async with httpx.AsyncClient(headers=core.HEADERS,
                                             follow_redirects=True) as c:
                    return await core.check_rooms(
                        c, rooms, at,
                        progress=lambda d, t: self.q.put(("prog", d)))
            self.q.put(("results", asyncio.run(go())))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"Search failed:\n{e}"))

    # ------------------------------------------------------------------
    def poll(self) -> None:
        try:
            while True:
                kind, *p = self.q.get_nowait()
                if kind == "rooms":
                    self.all_rooms = p[0]
                    locs = sorted({r["locationName"] for r in self.all_rooms
                                   if r.get("locationName")})
                    types = sorted({r["type"] for r in self.all_rooms if r.get("type")})
                    self.loc_box.configure(values=locs, state="readonly")
                    self.type_box.configure(values=("All", *types))
                    self.search_btn.state(["!disabled"])
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.status.configure(
                        text=f"Room list loaded ({len(self.all_rooms)} rooms). "
                             "Pick location, date and time, then press Search.")
                elif kind == "prog":
                    self.progress.configure(value=p[0])
                elif kind == "results":
                    self.searching = False
                    self.results = p[0]
                    self.search_btn.state(["!disabled"])
                    self.export_btn.state(["!disabled"])
                    self.refresh()
                elif kind == "error":
                    self.searching = False
                    self.progress.stop()
                    if self.all_rooms:
                        self.search_btn.state(["!disabled"])
                    self.status.configure(text="Error — see message box.")
                    messagebox.showerror("ETH Room Finder", p[0])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    # ------------------------------------------------------------------
    def on_search(self) -> None:
        if self.searching:
            return
        if not self.all_rooms:
            self.status.configure(text="Reloading room list…")
            self.bg(self.load_rooms_bg)
            return
        try:
            d = date.fromisoformat(self.date_var.get().strip())
            hh, mm = (int(x) for x in self.time_var.get().strip().split(":"))
            at = datetime.combine(d, dtime(hh, mm))
        except (ValueError, TypeError):
            messagebox.showerror("Invalid input",
                                 "Date must be YYYY-MM-DD and time HH:MM.")
            return
        loc = self.loc_var.get()
        rooms = [r for r in self.all_rooms if r.get("locationName") == loc]
        if not rooms:
            messagebox.showerror("No rooms", f"No rooms found for {loc!r}.")
            return
        self.search_at, self.search_loc, self.searching = at, loc, True
        self.search_btn.state(["disabled"])
        self.progress.configure(maximum=len(rooms), value=0)
        self.tree.delete(*self.tree.get_children())
        self.status.configure(
            text=f"Checking {len(rooms)} rooms in {loc} "
                 f"for {at:%a %d %b %Y %H:%M} …")
        self.bg(self.search_bg, rooms, at)

    # ------------------------------------------------------------------
    def parse_until(self) -> datetime | None:
        s = self.until_var.get().strip()
        if not s or self.search_at is None:
            return None
        try:
            hh, mm = (int(x) for x in s.split(":"))
            return datetime.combine(self.search_at.date(), dtime(hh, mm))
        except (ValueError, TypeError):
            return None  # ignore a half-typed value

    def passes(self, r: dict, minseats: int, until: datetime | None) -> bool:
        st = core.status_of(r)
        mode = self.show_var.get()
        if mode == "Free only" and st != "FREE":
            return False
        if mode == "Free + no data" and st == "BUSY":
            return False
        if minseats > 0:
            try:
                if int(r["seats"]) < minseats:
                    return False
            except (ValueError, TypeError):
                return False  # unknown capacity can't clear a seat bar
        t = self.type_var.get()
        if t not in ("", "All") and r["type"] != t:
            return False
        if until is not None and st == "FREE":
            fu = r["free_until"]
            if fu is not None and fu < until:
                return False
        return True

    def info_text(self, r: dict) -> str:
        st = core.status_of(r)
        if st == "NO DATA":
            return "no occupancy published — not guaranteed free"
        if st == "BUSY":
            b = r["blocking"][0]
            return f"{b['from']:%H:%M}–{b['to']:%H:%M}  " \
                   f"{b['title'] or 'closed/not bookable'}"
        nxt = next((e for e in r["day"]
                    if not e["cancelled"] and e["from"] > self.search_at), None)
        if nxt:
            return f"next: {nxt['from']:%H:%M}  " \
                   f"{nxt['title'] or 'closed/not bookable'}"
        return "nothing else booked that day"

    def refresh(self, *_ignored) -> None:
        if not self.results:
            return
        try:
            minseats = int(self.seats_var.get() or 0)
        except ValueError:
            minseats = 0
        until = self.parse_until()
        self.shown = [r for r in self.results if self.passes(r, minseats, until)]
        self.render()
        n = {"FREE": 0, "BUSY": 0, "NO DATA": 0}
        for r in self.results:
            n[core.status_of(r)] += 1
        self.status.configure(
            text=f"{self.search_loc}, {self.search_at:%a %d %b %Y %H:%M} — "
                 f"FREE {n['FREE']} / BUSY {n['BUSY']} / NO DATA {n['NO DATA']} "
                 f"of {len(self.results)} rooms  •  showing {len(self.shown)}  "
                 "•  double-click a room for its ETH page")

    def sort_val(self, r: dict, col: str):
        if col == "seats":
            try:
                return int(r["seats"])
            except (ValueError, TypeError):
                return -1
        if col == "from":
            return r["free_from"] or datetime.min
        if col == "until":
            return r["free_until"] or datetime.max
        if col == "status":
            return core.status_of(r)
        if col == "info":
            return self.info_text(r)
        if col == "type":
            return r["type"] or ""
        return r["id"]

    def on_sort(self, col: str) -> None:
        self.sort_desc = not self.sort_desc if col == self.sort_col else (col == "seats")
        self.sort_col = col
        self.render()

    def render(self) -> None:
        rows = sorted(self.shown, key=lambda r: self.sort_val(r, self.sort_col),
                      reverse=self.sort_desc)
        self.tree.delete(*self.tree.get_children())
        self.by_iid.clear()
        def hm(dt, fb=""):
            return dt.strftime("%H:%M") if dt else fb
        for r in rows:
            st = core.status_of(r)
            iid = self.tree.insert("", "end", values=(
                r["id"], r["seats"] or "–",
                hm(r["free_from"], "open" if st == "FREE" else ""),
                hm(r["free_until"], "close" if st == "FREE" else ""),
                r["type"], st, self.info_text(r)), tags=(st,))
            self.by_iid[iid] = r

    # ------------------------------------------------------------------
    def on_open(self, _event) -> None:
        sel = self.tree.selection()
        r = self.by_iid.get(sel[0]) if sel else None
        if r:
            webbrowser.open(DETAIL_URL.format(b=r["building"], f=r["floor"],
                                              r=r["roomnr"]))

    def on_export(self) -> None:
        if not self.shown:
            messagebox.showinfo("Export", "Nothing to export — table is empty.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"free_rooms_{self.search_at:%Y-%m-%d_%H%M}.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["room", "seats", "free_from", "free_until",
                        "type", "status", "info"])
            for iid in self.tree.get_children():
                w.writerow(self.tree.item(iid)["values"])
        self.status.configure(text=f"Exported {len(self.shown)} rows to {path}")


if __name__ == "__main__":
    if "--check" in sys.argv:  # headless: never opens a window
        sys.exit(self_check())
    root = tk.Tk()
    App(root)
    root.mainloop()
