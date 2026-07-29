#!/usr/bin/env python3
"""
Odyssey 70mm IMAX watcher — Cinema City Praha Flora.

Правярае API Cinema City і шле ў Telegram, калі:
  * з'явіўся новы сеанс (новая дата / новы час),
  * сеанс быў sold out, а цяпер зноў ёсць месцы (вяртанні квіткоў),
  * засталося мала месцаў (апцыянальна, LOW_SEATS_RATIO).

Без залежнасцяў — толькі стандартная бібліятэка Python 3.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------- налады ----

CINEMA_ID = os.getenv("CINEMA_ID", "1052")          # Praha Flora
TENANT = os.getenv("CC_TENANT", "10101")            # Cinema City CZ
BASE = os.getenv("CC_BASE", "https://www.cinemacity.cz/cz/data-api-service/v1/quickbook")

FILM_QUERY = os.getenv("FILM_QUERY", "odys").lower()  # супадзенне па назве фільма
REQUIRE_ATTR = os.getenv("REQUIRE_ATTR", "70-mm")     # "" — не фільтраваць па фармаце
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "120"))
LOW_SEATS_RATIO = float(os.getenv("LOW_SEATS_RATIO", "0"))  # напр. 0.05

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_FILE = os.getenv("STATE_FILE", "state.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ------------------------------------------------------------------ утыл ----


def get_json(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            if attempt == tries - 1:
                print(f"[warn] {url} -> {e}", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))
    return None


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[info] Telegram не сканфігураваны, друкую ў кансоль:\n" + text)
        return
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except Exception as e:  # noqa: BLE001
        print(f"[error] telegram: {e}", file=sys.stderr)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


# ----------------------------------------------------------------- логіка ---


def fetch_dates():
    until = (date.today() + timedelta(days=DAYS_AHEAD)).isoformat()
    url = f"{BASE}/{TENANT}/dates/in-cinema/{CINEMA_ID}/until/{until}?attr=&lang=cs_CZ"
    j = get_json(url)
    if not j:
        return []
    return j.get("body", {}).get("dates", [])


def fetch_events(day):
    url = f"{BASE}/{TENANT}/film-events/in-cinema/{CINEMA_ID}/at-date/{day}?attr=&lang=cs_CZ"
    j = get_json(url)
    if not j:
        return {}
    body = j.get("body", {})
    films = {f["id"]: f for f in body.get("films", [])}

    wanted = {fid for fid, f in films.items()
              if FILM_QUERY in f.get("name", "").lower()}
    if not wanted:
        return {}

    out = {}
    for ev in body.get("events", []):
        if ev.get("filmId") not in wanted:
            continue
        if REQUIRE_ATTR and REQUIRE_ATTR not in ev.get("attributeIds", []):
            continue
        out[str(ev["id"])] = {
            "film": films[ev["filmId"]].get("name", "?"),
            "when": ev.get("eventDateTime", ""),
            "hall": ev.get("auditorium", ""),
            "sold_out": bool(ev.get("soldOut")),
            "ratio": float(ev.get("availabilityRatio") or 0),
            "link": (ev.get("compositeBookingLink", {})
                       .get("bookingUrl", {})
                       .get("url") or ev.get("bookingLink", "")),
        }
    return out


def pretty(ev_id, ev):
    try:
        when = datetime.fromisoformat(ev["when"]).strftime("%a %d.%m %H:%M")
    except ValueError:
        when = ev["when"]
    seats = ("SOLD OUT" if ev["sold_out"] or ev["ratio"] <= 0
             else f"~{ev['ratio'] * 100:.0f}% вольна")
    link = ev["link"] or f"https://www.cinemacity.cz/cz/booking-router/launch/{ev_id}?lang=cs"
    return f'• <b>{when}</b> — {ev["hall"]} — {seats}\n  <a href="{link}">купіць</a>'


def block(title, ids, current):
    rows = sorted(ids, key=lambda x: current[x]["when"])
    return title + "\n" + "\n".join(pretty(i, current[i]) for i in rows)


def main():
    days = fetch_dates()
    if not days:
        print("[warn] не атрымаў спіс дат — выходжу без алертаў")
        return 0

    current = {}
    for day in days:
        current.update(fetch_events(day))
        time.sleep(0.3)  # не грукаем у API занадта хутка

    print(f"[info] знойдзена сеансаў: {len(current)} за {len(days)} дзён")

    old = load_state()
    first_run = not old

    new_ids, back_ids, low_ids = [], [], []
    for eid, ev in current.items():
        prev = old.get(eid)
        available = not ev["sold_out"] and ev["ratio"] > 0
        if prev is None:
            new_ids.append(eid)
            continue
        was_available = not prev.get("sold_out") and prev.get("ratio", 0) > 0
        if available and not was_available:
            back_ids.append(eid)
        elif (LOW_SEATS_RATIO and available
              and ev["ratio"] <= LOW_SEATS_RATIO
              and prev.get("ratio", 0) > LOW_SEATS_RATIO):
            low_ids.append(eid)

    save_state(current)

    if first_run:
        head = (f"🎬 <b>Вартаўнік запушчаны.</b> Зараз у Praha Flora "
                f"{len(current)} сеансаў 70mm.")
        rows = sorted(new_ids, key=lambda x: current[x]["when"])[:15]
        body = "\n".join(pretty(i, current[i]) for i in rows)
        tg_send(head + ("\n\n" + body if body else ""))
        return 0

    chunks = []
    if new_ids:
        chunks.append(block("🆕 <b>Новыя сеансы Odyssea 70mm — Praha Flora</b>", new_ids, current))
    if back_ids:
        chunks.append(block("♻️ <b>Зноў з'явіліся месцы</b>", back_ids, current))
    if low_ids:
        chunks.append(block("⚠️ <b>Засталося мала месцаў</b>", low_ids, current))

    if chunks:
        tg_send("\n\n".join(chunks))
    else:
        print("[info] змен няма")
    return 0


if __name__ == "__main__":
    sys.exit(main())
