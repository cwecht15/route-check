"""Route Check on Streamlit: the same TomTom + 511NJ check as route_check.py, as a phone page.

    streamlit run app.py                 local test (key from .env or the environment)

On Streamlit Community Cloud everything private comes from the app's Secrets: TOMTOM_API_KEY,
APP_PASSWORD and the two addresses (HOSPITAL_LABEL, HOSPITAL_ADDRESS, HOME_LABEL, HOME_ADDRESS).
Locally the same names are read from the environment or .env. Without APP_PASSWORD the app
refuses to run. The last result for each direction is shared by everyone using the app, so the second person
can open it without spending another TomTom check.
"""
from __future__ import annotations

import contextlib
import hmac
import io
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

# Streamlit Cloud servers run on UTC; the trip and every clock shown are New Jersey time.
if hasattr(time, "tzset"):                  # Linux / macOS only; Windows already runs local time
    os.environ["TZ"] = "America/New_York"
    time.tzset()

import streamlit as st
import streamlit.components.v1 as components

import route_check as rc

DIRECTIONS = ("there", "back")
SECRET_NAMES = ("TOMTOM_API_KEY", "APP_PASSWORD", "HOSPITAL_LABEL", "HOSPITAL_ADDRESS",
                "HOME_LABEL", "HOME_ADDRESS")


@st.cache_resource
def _shared() -> tuple[dict[str, dict], dict[str, threading.Lock]]:
    """One store for the whole server process (a plain module global would be rebuilt on every
    Streamlit rerun): the latest result per direction, and one lock per direction."""
    return {}, {d: threading.Lock() for d in DIRECTIONS}


LATEST, LOCKS = _shared()


def secrets() -> dict[str, str]:
    """The app's Streamlit secrets as a plain dict ({} when there is no secrets.toml, the normal
    case when run locally)."""
    out = {}
    for name in SECRET_NAMES:
        try:
            val = st.secrets.get(name)
        except Exception:
            return {}
        if val is not None and str(val).strip():
            out[name] = str(val).strip()
    return out


def setting(name: str) -> str:
    """Streamlit secret first, then the environment, then .env."""
    val = secrets().get(name) or os.environ.get(name, "").strip()
    if not val and rc.ENV_FILE.is_file():
        val = (rc.dotenv_values(rc.ENV_FILE).get(name) or "").strip()
    return val


def get_key() -> str:
    key = setting("TOMTOM_API_KEY")
    return "" if key.lower().startswith("your") else key


def password_gate() -> None:
    """Stop here unless this browser session has entered APP_PASSWORD. No password set = no app."""
    if st.session_state.get("authed"):
        return
    expected = setting("APP_PASSWORD")
    if not expected:
        st.error("This app has no password yet, so it will not run.\n\n"
                 "Owner: open the app's **Settings → Secrets** on Streamlit and add\n\n"
                 '`APP_PASSWORD = "choose-a-password"`\n\n'
                 f"Running locally: put `APP_PASSWORD=...` in `{rc.ENV_FILE}` or the environment.")
        st.stop()
    with st.form("login"):
        typed = st.text_input("Password", type="password")
        sent = st.form_submit_button("Open", type="primary", width="stretch")
    if sent:
        if hmac.compare_digest(typed.encode("utf-8"), expected.encode("utf-8")):
            st.session_state["authed"] = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()


def report_text(rep: dict) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc.print_report(rep)
    return buf.getvalue()


def pdf_bytes(rep: dict) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        return rc.write_pdf(rep, Path(tmp) / "route.pdf").read_bytes()


def build_result(rep: dict) -> dict:
    """Everything the results view needs, made once per check and shared."""
    return {"rep": rep, "html": rc.map_html(rep), "pdf_bytes": pdf_bytes(rep),
            "text": report_text(rep), "when": datetime.now(),
            "stem": rc.report_stem(rep["direction"], rep["now"])}


def check(key: str, places: dict, direction: str, leaving: str, no_tolls: bool) -> dict:
    """Run one check, or hand back the one a simultaneous press of the same button just made."""
    pressed = datetime.now()
    with LOCKS[direction]:
        prev = LATEST.get(direction)
        if prev and prev["when"] >= pressed and prev["args"] == (leaving, no_tolls):
            return prev                          # someone else ran it while we waited
        now = datetime.now().replace(microsecond=0)
        depart = rc.parse_departure(leaving, now)
        if depart is None:
            raise ValueError(f"Could not read the departure time '{leaving}'. "
                             "Try 5:30pm, 17:30, tomorrow 7am, fri 6:15am.")
        result = build_result(rc.run_check(key, direction, depart, now, no_tolls=no_tolls,
                                         places=places))
        result["args"] = (leaving, no_tolls)
        LATEST[direction] = result
        return result


def ago(when: datetime) -> str:
    mins = int((datetime.now() - when).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 90:
        return f"{mins} min ago"
    return f"{mins // 60} h {mins % 60} min ago"


def show_result(res: dict) -> None:
    rep = res["rep"]
    st.divider()
    st.subheader(f"{rep['origin']} → {rep['dest']}")
    c1, c2 = st.columns([2, 1])
    c1.link_button("Open in Waze", rep["waze"], type="primary", width="stretch")
    c2.link_button("Google Maps", rep["gmaps"], width="stretch")
    if rep["routes"]:
        best = rep["routes"][0]
        st.markdown(f"**{best['tag']}** {best['via']} · **{rc.minutes(best['travel_s'])}** "
                    f"({rc.plus_minutes(best['delay_s'])} traffic) · arrive "
                    f"**{rc.clock(best['arrive'])}**")
    leaving = ("leaving now, live traffic" if not rep["future"] else
               f"leaving {rc.day_and_clock(rep['depart'])}, TomTom's historic traffic model")
    st.caption(f"Checked {rc.day_and_clock(rep['now'])} · {leaving}")
    if hasattr(st, "iframe"):                # Streamlit 1.5x+: components.v1.html is deprecated
        st.iframe(res["html"], height=640)
    else:
        components.html(res["html"], height=640, scrolling=False)
    st.download_button("Download PDF", res["pdf_bytes"], file_name=f"{res['stem']}.pdf",
                       mime="application/pdf", width="stretch")
    with st.expander("Text report"):
        st.code(res["text"], language=None)


def main() -> None:
    st.set_page_config(page_title="Route Check", page_icon="🚗", layout="centered")
    st.title("Route Check")
    password_gate()

    key = get_key()
    if not key:
        st.error("No TomTom API key found.\n\n"
                 "On Streamlit Cloud: open the app's **Settings → Secrets** and add\n\n"
                 '`TOMTOM_API_KEY = "your-key"`\n\n'
                 f"Running locally: put `TOMTOM_API_KEY=your-key` in `{rc.ENV_FILE}`.")
        st.stop()
    try:
        places = rc.load_places(secrets())
    except rc.ConfigError as e:
        st.error(f"{e}\n\nOn Streamlit Cloud: **Settings → Secrets** needs `HOSPITAL_ADDRESS` "
                 "and `HOME_ADDRESS` (and optionally `HOSPITAL_LABEL`, `HOME_LABEL`).")
        st.stop()
    hosp, home = places["hospital"][0], places["home"][0]
    labels = {"there": f"{hosp} → {home}", "back": f"{home} → {hosp}"}

    leaving = st.text_input("Leaving at", placeholder="now, or 5:30pm, tomorrow 7am")
    no_tolls = st.checkbox("Avoid tolls")

    for direction, label in labels.items():
        if st.button(label, type="primary", width="stretch", key=f"run_{direction}"):
            try:
                with st.spinner("Checking traffic…"):
                    check(key, places, direction, leaving, no_tolls)
            except ValueError as e:
                st.error(str(e))
            except rc.ApiError as e:
                st.error(str(e))
            else:
                st.session_state["show"] = direction
        prev = LATEST.get(direction)
        if prev:
            st.caption(f"Last checked {rc.clock(prev['when'])}, {ago(prev['when'])}")
            if st.button("Show last result", key=f"show_{direction}", width="stretch"):
                st.session_state["show"] = direction

    shown = LATEST.get(st.session_state.get("show", ""))
    if shown:
        show_result(shown)


if __name__ == "__main__":                  # streamlit run sets __main__; tests can import
    main()
