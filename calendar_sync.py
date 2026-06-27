# calendar_sync.py
import sqlite3, json
from datetime import datetime, timedelta

DB = "tasks.db"


def get_active_accounts():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM calendar_accounts WHERE active=1").fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_google_events(account):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GRequest
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_info(json.loads(account["credentials"]))
    if creds.expired and creds.refresh_token:
        creds.refresh(GRequest())
        db = sqlite3.connect(DB)
        db.execute("UPDATE calendar_accounts SET credentials=? WHERE id=?",
                   (creds.to_json(), account["id"]))
        db.commit()
        db.close()

    svc = build("calendar", "v3", credentials=creds)
    now = datetime.utcnow().isoformat() + "Z"
    end = (datetime.utcnow() + timedelta(days=14)).isoformat() + "Z"
    items = svc.events().list(
        calendarId="primary", timeMin=now, timeMax=end,
        singleEvents=True, orderBy="startTime"
    ).execute().get("items", [])
    return [
        {
            "title": e.get("summary", "(No title)"),
            "start": e["start"].get("dateTime", e["start"].get("date")),
            "source": "Google",
            "color": "#2563EB"
        }
        for e in items
    ]


def get_outlook_events(account):
    import requests
    import msal
    from config import OUTLOOK_CLIENT_ID, OUTLOOK_SECRET, OUTLOOK_TENANT

    creds = json.loads(account["credentials"])
    access_token = creds.get("access_token", "")

    if creds.get("refresh_token"):
        msal_app = msal.ConfidentialClientApplication(
            OUTLOOK_CLIENT_ID,
            client_credential=OUTLOOK_SECRET,
            authority=f"https://login.microsoftonline.com/{OUTLOOK_TENANT}"
        )
        result = msal_app.acquire_token_by_refresh_token(
            creds["refresh_token"],
            scopes=["https://graph.microsoft.com/Calendars.Read"]
        )
        if "access_token" in result:
            access_token = result["access_token"]
            db = sqlite3.connect(DB)
            db.execute("UPDATE calendar_accounts SET credentials=? WHERE id=?",
                       (json.dumps({**creds, **result}), account["id"]))
            db.commit()
            db.close()

    now = datetime.utcnow().isoformat() + "Z"
    end = (datetime.utcnow() + timedelta(days=14)).isoformat() + "Z"
    r = requests.get(
        "https://graph.microsoft.com/v1.0/me/calendarView",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"startDateTime": now, "endDateTime": end,
                "$select": "subject,start", "$top": 100}
    )
    return [
        {
            "title": e.get("subject", "(No title)"),
            "start": e["start"]["dateTime"],
            "source": "Outlook",
            "color": "#0078D4"
        }
        for e in r.json().get("value", [])
    ]


def get_icloud_events(account):
    import caldav
    creds = json.loads(account["credentials"])
    client = caldav.DAVClient(
        url="https://caldav.icloud.com",
        username=creds["username"],
        password=creds["password"]
    )
    events = []
    start = datetime.now()
    end = start + timedelta(days=14)
    for cal in client.principal().calendars():
        for e in cal.date_search(start=start, end=end, expand=True):
            v = e.vobject_instance.vevent
            events.append({
                "title": str(v.summary.value),
                "start": str(v.dtstart.value),
                "source": "iCloud",
                "color": "#7C3AED"
            })
    return events


_HANDLERS = {
    "google":  get_google_events,
    "outlook": get_outlook_events,
    "icloud":  get_icloud_events,
}


def get_all_events():
    events = []
    for account in get_active_accounts():
        fn = _HANDLERS.get(account["type"])
        if not fn:
            continue
        try:
            events += fn(account)
        except Exception as ex:
            print(f"Calendar error ({account['type']} / {account['label']}): {ex}")
    return sorted(events, key=lambda x: x["start"])
