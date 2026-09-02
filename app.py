"""ETH Room Finder — web version (Streamlit).

Runs server-side, so the missing CORS headers on the ETH API don't matter.
Deployed on Streamlit Community Cloud anyone can use it from a browser with
zero installation.  Reuses room_core.py — the same verified logic as the
desktop GUI and the CLI.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import streamlit as st

import room_core as core

st.set_page_config(page_title="ETH Room Finder", page_icon="🏛️", layout="wide")

ZRH = ZoneInfo("Europe/Zurich")
MODES = ("Free only", "Free + no data", "All")


@st.cache_data(ttl=3600, show_spinner="Loading ETH room list…")
def load_rooms() -> list[dict]:
    async def go():
        async with httpx.AsyncClient(headers=core.HEADERS,
                                     follow_redirects=True) as c:
            return await core.fetch_rooms(c)
    return asyncio.run(go())


def run_search(rooms: list[dict], at: datetime) -> list[dict]:
    bar = st.progress(0.0, text=f"Checking {len(rooms)} rooms…")
    async def go():
        async with httpx.AsyncClient(headers=core.HEADERS,
                                     follow_redirects=True) as c:
            return await core.check_rooms(
                c, rooms, at,
                progress=lambda d, t: bar.progress(
                    d / t, text=f"Checking rooms… {d}/{t}"))
    try:
        return asyncio.run(go())
    finally:
        bar.empty()


st.title("🏛️ ETH Room Finder")
st.caption("Which rooms are free, when?  Live data from the official ETH "
           "[roominfo](https://ethz.ch/staffnet/en/service/rooms-and-buildings/roominfo.html)"
           " service.")

rooms_all = load_rooms()
locations = sorted({r["locationName"] for r in rooms_all if r.get("locationName")})

c1, c2, c3, c4 = st.columns([2, 1, 1, 1], vertical_alignment="bottom")
loc = c1.selectbox("Location", locations,
                   index=locations.index("Zürich Zentrum")
                   if "Zürich Zentrum" in locations else 0)
day = c2.date_input("Date", value=datetime.now(ZRH).date())
tm = c3.time_input("Time", value=dtime(14, 0), step=900)
go = c4.button("Search", type="primary", width="stretch")

if go:
    at = datetime.combine(day, tm)
    sel = [r for r in rooms_all if r.get("locationName") == loc]
    st.session_state["results"] = run_search(sel, at)
    st.session_state["at"] = at
    st.session_state["loc"] = loc

if "results" not in st.session_state:
    st.info("Pick a location, date and time, then press **Search**.")
    st.stop()

results: list[dict] = st.session_state["results"]
at: datetime = st.session_state["at"]

# ---- instant view filters (no refetch) --------------------------------
f1, f2, f3, f4 = st.columns([1, 1, 2, 2], vertical_alignment="bottom")
min_seats = f1.number_input("Min seats", min_value=0, value=0, step=5)
until_s = f2.text_input("Free until at least", value="", placeholder="16:00")
types = sorted({r["type"] for r in results if r["type"]})
sel_types = f3.multiselect("Room type", types, placeholder="All types")
mode = f4.radio("Show", MODES, horizontal=True)

until = None
if until_s.strip():
    try:
        hh, mm = (int(x) for x in until_s.strip().split(":"))
        until = datetime.combine(at.date(), dtime(hh, mm))
    except (ValueError, TypeError):
        st.warning("“Free until” must be HH:MM — ignoring it.")


def passes(r: dict) -> bool:
    stt = core.status_of(r)
    if mode == "Free only" and stt != "FREE":
        return False
    if mode == "Free + no data" and stt == "BUSY":
        return False
    if min_seats > 0:
        try:
            if int(r["seats"]) < min_seats:
                return False
        except (TypeError, ValueError):
            return False  # unknown capacity can't clear a seat bar
    if sel_types and r["type"] not in sel_types:
        return False
    if until is not None and stt == "FREE":
        fu = r["free_until"]
        if fu is not None and fu < until:
            return False
    return True


shown = [r for r in results if passes(r)]
n = Counter(core.status_of(r) for r in results)
st.markdown(f"**{st.session_state['loc']}, {at:%a %d %b %Y %H:%M}** — "
            f"🟢 {n['FREE']} free · 🔴 {n['BUSY']} busy · "
            f"⚪ {n['NO DATA']} no data · showing **{len(shown)}**")


def hm(dt: datetime | None, fb: str = "") -> str:
    return dt.strftime("%H:%M") if dt else fb


rows = []
for r in shown:
    stt = core.status_of(r)
    rows.append({
        "Room": r["id"],
        "Seats": int(r["seats"]) if str(r["seats"] or "").isdigit() else None,
        "Free from": hm(r["free_from"], "open" if stt == "FREE" else ""),
        "Free until": hm(r["free_until"], "close" if stt == "FREE" else ""),
        "Type": r["type"],
        "Status": stt,
        "Current / next booking": core.info_text(r, at),
        "ETH page": core.DETAIL_URL.format(b=r["building"], f=r["floor"],
                                           r=r["roomnr"]),
    })

if not rows:
    st.info("No rooms match the filters.")
else:
    df = pd.DataFrame(rows)
    df["Seats"] = df["Seats"].astype("Int64")
    st.dataframe(
        df, hide_index=True, width="stretch",
        height=min(620, 42 + 35 * len(df)),
        column_config={
            "ETH page": st.column_config.LinkColumn(display_text="open ↗"),
            "Seats": st.column_config.NumberColumn(format="%d"),
        })
    st.download_button(
        "⬇️ Download CSV",
        df.drop(columns=["ETH page"]).to_csv(index=False).encode("utf-8"),
        file_name=f"free_rooms_{at:%Y-%m-%d_%H%M}.csv", mime="text/csv")

st.caption("**NO DATA** rooms publish no occupancy at all — they are *not* "
           "guaranteed free.  A booking ending exactly at the chosen time "
           "counts as free.")
