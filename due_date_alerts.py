# due_date_alerts.py
import sqlite3, json, base64
from datetime import date, timedelta
from email.mime.text import MIMEText

import os as _os
DB = _os.getenv("DATABASE_PATH", "tasks.db")


def _due_soon_tasks(db, user_id):
    today = date.today().isoformat()
    cutoff = (date.today() + timedelta(days=2)).isoformat()
    return db.execute(
        "SELECT id, title, due_date, project FROM tasks "
        "WHERE user_id=? AND archived=0 AND status!='done' "
        "AND due_date IS NOT NULL AND due_date != '' "
        "AND due_date BETWEEN ? AND ? AND notified_48h=0 "
        "ORDER BY due_date",
        (user_id, today, cutoff)
    ).fetchall()


def _send_gmail(account, to_addr, subject, body):
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

    msg = MIMEText(body)
    msg["to"] = to_addr
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    svc = build("gmail", "v1", credentials=creds)
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_due_date_alerts():
    from calendar_sync import get_active_accounts

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    for account in get_active_accounts():
        if account["type"] != "google":
            continue
        try:
            tasks = _due_soon_tasks(db, account["user_id"])
            if not tasks:
                continue
            lines = [f"- {t['title']} (due {t['due_date']}, {t['project']})" for t in tasks]
            body = "These tasks are due within the next 48 hours:\n\n" + "\n".join(lines)
            _send_gmail(account, account["label"], "Sprout: tasks due soon", body)
            db.execute(
                f"UPDATE tasks SET notified_48h=1 WHERE id IN ({','.join('?' * len(tasks))})",
                [t["id"] for t in tasks]
            )
            db.commit()
        except Exception as ex:
            print(f"Due-date alert error ({account['label']}): {ex}")

    db.close()
