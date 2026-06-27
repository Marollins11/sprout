# ical_sync.py
import requests
from icalendar import Calendar
from datetime import datetime, timezone, date as date_type


def _is_separator(line):
    """True for lines that are just dashes, underscores, or similar fillers."""
    stripped = line.strip("-_ \t*=")
    return len(stripped) == 0


def _extract_course(component):
    """Best-effort course name extraction from a Canvas VEVENT."""
    # Canvas sometimes puts the course in CATEGORIES
    cats = component.get("CATEGORIES")
    if cats:
        val = str(cats).strip()
        if val and not _is_separator(val):
            return val.lower()

    # Canvas often embeds the course code in the SUMMARY: "Title [COURSE-CODE]"
    summary = str(component.get("SUMMARY", "")).strip()
    import re
    bracket = re.search(r"\[261V-MGMT-(\d+)-", summary)
    if bracket:
        code_map = {"241": "tech and innovation", "266": "new product development"}
        course = code_map.get(bracket.group(1))
        if course:
            return course

    # Fall back to first meaningful line of DESCRIPTION
    desc = str(component.get("DESCRIPTION", "")).strip()
    if desc:
        for line in desc.splitlines():
            line = line.strip()
            if line and not line.startswith("http") and not _is_separator(line) and len(line) < 100:
                return line.lower()

    return "canvas"


def fetch_ical_events(url):
    import re
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
        # Strip "[COURSE-CODE]" Canvas appends to the title
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
        events.append({
            "title": summary,
            "start": dt.isoformat(),
            "source": "Canvas",
            "color": "#1A7F37",
            "course": _extract_course(component),
            "description": raw_desc or None,
        })
    return events


def sync_ical_to_kanban(url, flask_url):
    now = datetime.now(timezone.utc)
    events = fetch_ical_events(url)
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
            due_dt = datetime.fromisoformat(e["start"])
            if due_dt < now:
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
