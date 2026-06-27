# canvas_sync.py
import requests as req
from datetime import datetime, timedelta
from config import CANVAS_TOKEN, CANVAS_URL

HDR = {"Authorization": f"Bearer {CANVAS_TOKEN}"}


def get_courses():
    r = req.get(f"{CANVAS_URL}/api/v1/courses", headers=HDR,
                params={"enrollment_state": "active", "per_page": 50})
    return [c for c in r.json() if isinstance(c, dict) and "id" in c]


def get_canvas_assignments():
    events = []
    for course in get_courses():
        name = course.get("name", "Unknown")
        r = req.get(f"{CANVAS_URL}/api/v1/courses/{course['id']}/assignments",
                    headers=HDR, params={"bucket": "upcoming", "per_page": 50})
        for a in r.json():
            if not isinstance(a, dict) or not a.get("due_at"):
                continue
            events.append({
                "title": a.get("name", "Assignment"),
                "course": name,
                "due": a["due_at"],
                "source": "Canvas",
                "color": "#1A7F37"
            })
    return events


def sync_to_kanban(flask_url):
    assignments = get_canvas_assignments()
    existing = {t["title"] for t in req.get(f"{flask_url}/api/tasks").json()}
    for a in assignments:
        due_str = datetime.fromisoformat(a["due"].replace("Z", "")).strftime("%b %-d")
        title = f"{a['title']} — due {due_str}"
        if any(a["title"].lower() in t.lower() for t in existing):
            continue
        req.post(f"{flask_url}/api/tasks",
                 json={"title": title, "project": a["course"], "family": "school"})


def get_canvas_events_for_calendar():
    return [
        {
            "title": f"{a['title']} ({a['course']})",
            "start": a["due"],
            "source": "Canvas",
            "color": "#1A7F37"
        }
        for a in get_canvas_assignments()
    ]
