# ical_sync.py
import re
import requests
from icalendar import Calendar
from datetime import datetime, timezone, date as date_type

_PALETTE = [
    "#7C3AED",  # violet
    "#2563EB",  # blue
    "#0D9488",  # teal
    "#D97706",  # amber
    "#DC2626",  # red
    "#BE185D",  # pink
    "#059669",  # emerald
    "#B45309",  # brown
]

def _course_color(name):
    return _PALETTE[hash(name.lower()) % len(_PALETTE)]


def _is_separator(line):
    return len(line.strip("-_ \t*=")) == 0


def _extract_code(component):
    """Return the raw course code from the SUMMARY bracket, or None."""
    summary = str(component.get("SUMMARY", "")).strip()
    m = re.search(r"\[([^\]]+)\]", summary)
    return m.group(1) if m else None


def _extract_course(component, mappings=None):
    """Return a course name using user mappings, then fallback heuristics."""
    code = _extract_code(component)
    if code:
        if mappings and code in mappings:
            return mappings[code]
        return code.lower()

    cats = component.get("CATEGORIES")
    if cats:
        val = str(cats).strip()
        if val and not _is_separator(val):
            return val.lower()

    desc = str(component.get("DESCRIPTION", "")).strip()
    for line in desc.splitlines():
        line = line.strip()
        if line and not line.startswith("http") and not _is_separator(line) and len(line) < 100:
            return line.lower()

    return "canvas"


def detect_course_codes(url):
    """Fetch the feed and return a sorted list of unique course codes found."""
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    cal = Calendar.from_ical(r.content)
    codes = set()
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        code = _extract_code(component)
        if code:
            codes.add(code)
    return sorted(codes)


def fetch_ical_events(url, mappings=None):
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    cal = Calendar.from_ical(r.content)
    events = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        summary = str(component.get("SUMMARY", "")).strip()
        if not summary:
            continue
        summary = re.sub(r"\s*\[[^\]]+\]\s*$", "", summary).strip()
        dtstart = component.get("DTSTART")
        if not dtstart:
            continue
        dt = dtstart.dt
        if isinstance(dt, date_type) and not isinstance(dt, datetime):
            dt = datetime(dt.year, dt.month, dt.day, 23, 59, tzinfo=timezone.utc)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        raw_desc = str(component.get("DESCRIPTION", "")).strip()
        course = _extract_course(component, mappings=mappings)
        events.append({
            "title": summary,
            "start": dt.isoformat(),
            "source": "Canvas",
            "color": _course_color(course),
            "course": course,
            "description": raw_desc or None,
        })
    return events


def sync_ical_to_kanban(url, flask_url, mappings=None):
    now = datetime.now(timezone.utc)
    events = fetch_ical_events(url, mappings=mappings)
    try:
        existing = {t["title"] for t in requests.get(f"{flask_url}/api/tasks").json()}
    except Exception:
        return

    SKIP_PATTERNS = ("class session", "class meeting", "lecture", "office hours")
    for e in events:
        title = e["title"]
        if any(title.lower().startswith(p) for p in SKIP_PATTERNS):
            continue
        try:
            if datetime.fromisoformat(e["start"]) < now:
                continue
        except Exception:
            pass
        if any(title.lower() in t.lower() for t in existing):
            continue
        requests.post(f"{flask_url}/api/tasks", json={
            "title": title,
            "project": e["course"],
            "family": "school",
            "due_date": e["start"],
            "description": e.get("description"),
        })
