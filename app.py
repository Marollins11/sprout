# app.py
from flask import Flask, jsonify, request, render_template, redirect, session, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import sqlite3, threading, time as _time, schedule as sched, os, json, secrets
from datetime import datetime
from google import genai

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
DB  = "tasks.db"

from config import FLASK_SECRET, GEMINI_API_KEY
app.secret_key = FLASK_SECRET

login_manager = LoginManager(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id, email, name):
        self.id = str(id)
        self.email = email
        self.name = name or email

@login_manager.user_loader
def load_user(user_id):
    row = get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return None
    return User(row['id'], row['email'], row['name'])

_genai_client = None

def get_genai_client():
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=GEMINI_API_KEY)
    return _genai_client

FAMILY_PALETTES = {
    "work":     ["1F6FEB", "1565C0", "0D47A1", "3B82F6", "60A5FA", "93C5FD", "BFDBFE"],
    "school":   ["1A7F37", "166534", "14532D", "22C55E", "4ADE80", "86EFAC", "BBF7D0"],
    "personal": ["6B21A8", "7E22CE", "9333EA", "A855F7", "C084FC", "DDD6FE"],
}
AUTO_PALETTE = ["C25100", "0E7490", "B45309", "BE185D", "065F46", "1D4ED8"]
auto_idx = 0

VOICE_SYSTEM = """You are Sprout, a voice-first personal desk assistant.
Respond with EXACTLY ONE structured reply or a plain conversational answer.
ADD TASK:       TASK|title|family|sub-project
MOVE STATUS:    MOVE_STATUS|partial title|new_status  (todo/in_progress/done)
REASSIGN:       MOVE_PROJECT|partial title|family|new sub-project
FLAG/UNFLAG:    FLAG|partial title|1 or 0
FILTER BOARD:   FILTER|family|sub-project  (use "all" to clear)
SET REMINDER:   REMINDER|HH:MM|task description
CANVAS SYNC:    CANVAS_SYNC
For anything else reply conversationally in 2-3 sentences.
Infer the family from context: work=professional, school=courses, personal=everything else.
Keep replies short — they are spoken aloud."""

_chat_sessions = {}


def get_chat(sid):
    if sid not in _chat_sessions:
        _chat_sessions[sid] = get_genai_client().chats.create(
            model="gemini-2.5-pro",
            config={"system_instruction": VOICE_SYSTEM}
        )
    return _chat_sessions[sid]


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        status TEXT DEFAULT 'todo',
        project TEXT DEFAULT 'personal',
        family TEXT DEFAULT 'personal',
        color TEXT DEFAULT '#555555',
        flagged INTEGER DEFAULT 0,
        created_at TEXT,
        due_date TEXT,
        description TEXT
    )""")
    for col, defn in [("due_date", "TEXT"), ("description", "TEXT")]:
        try:
            db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {defn}")
        except Exception:
            pass
    db.execute("""CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        family TEXT NOT NULL,
        color TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT, task TEXT, fired INTEGER DEFAULT 0
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS calendar_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        label TEXT NOT NULL,
        credentials TEXT,
        active INTEGER DEFAULT 1,
        created_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT,
        google_id TEXT,
        name TEXT,
        created_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS canvas_feeds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        url TEXT NOT NULL,
        created_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS course_mappings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        course_code TEXT NOT NULL,
        course_name TEXT NOT NULL,
        UNIQUE(user_id, course_code)
    )""")
    db.execute("INSERT OR IGNORE INTO projects VALUES (1,'personal','personal','#6B21A8')")
    db.commit()


def get_or_create_project(name, family):
    global auto_idx
    db = get_db()
    nm = name.lower().strip()
    fm = (family or "").lower().strip()
    row = db.execute("SELECT color FROM projects WHERE name=?", (nm,)).fetchone()
    if row:
        return row["color"]
    if fm in FAMILY_PALETTES:
        used = [r["color"] for r in db.execute(
            "SELECT color FROM projects WHERE family=?", (fm,)).fetchall()]
        pal = FAMILY_PALETTES[fm]
        color = next((c for c in pal if c not in used), pal[-1])
    else:
        color = AUTO_PALETTE[auto_idx % len(AUTO_PALETTE)]
        auto_idx += 1
        if fm:
            FAMILY_PALETTES[fm] = [color]
    db.execute("INSERT OR IGNORE INTO projects (name,family,color) VALUES (?,?,?)",
               (nm, fm or "other", "#" + color))
    db.commit()
    return "#" + color


# ── Voice command handler ────────────────────────────────────────────────────

def handle_voice_reply(raw, host_url):
    """Execute a Gemini structured reply against the DB. Returns (spoken_text, action)."""
    action = {}
    db = get_db()

    if raw.startswith("TASK|"):
        _, title, family, project = raw.split("|", 3)
        color = get_or_create_project(project, family)
        db.execute(
            "INSERT INTO tasks (title,status,project,family,color,created_at) VALUES (?,?,?,?,?,?)",
            (title, "todo", project.lower(), family.lower(), color, datetime.now().isoformat())
        )
        db.commit()
        return f"Added {title} to {project}.", action

    if raw.startswith("MOVE_STATUS|"):
        _, match, status = raw.split("|", 2)
        task = db.execute(
            "SELECT id FROM tasks WHERE instr(lower(title), ?) > 0", (match.lower(),)
        ).fetchone()
        if not task:
            return "I couldn't find that task.", action
        db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task["id"]))
        db.commit()
        return f"Moved to {status.replace('_', ' ')}.", action

    if raw.startswith("MOVE_PROJECT|"):
        _, match, family, project = raw.split("|", 3)
        task = db.execute(
            "SELECT id FROM tasks WHERE instr(lower(title), ?) > 0", (match.lower(),)
        ).fetchone()
        if not task:
            return "I couldn't find that task.", action
        color = get_or_create_project(project, family)
        db.execute(
            "UPDATE tasks SET project=?,family=?,color=? WHERE id=?",
            (project.lower(), family.lower(), color, task["id"])
        )
        db.commit()
        return f"Moved to {project}.", action

    if raw.startswith("FLAG|"):
        _, match, flagged = raw.split("|", 2)
        task = db.execute(
            "SELECT id FROM tasks WHERE instr(lower(title), ?) > 0", (match.lower(),)
        ).fetchone()
        if not task:
            return "I couldn't find that task.", action
        db.execute("UPDATE tasks SET flagged=? WHERE id=?", (int(flagged), task["id"]))
        db.commit()
        return "Flagged." if flagged == "1" else "Flag removed.", action

    if raw.startswith("FILTER|"):
        _, family, sub = raw.split("|", 2)
        action = {"type": "FILTER", "family": family, "sub": sub}
        return "Showing all tasks." if family == "all" else f"Filtering to {sub}.", action

    if raw.startswith("REMINDER|"):
        _, t, task_desc = raw.split("|", 2)
        db.execute("INSERT INTO reminders (time,task) VALUES (?,?)", (t, task_desc))
        db.commit()
        return f"Reminder set for {t}.", action

    if raw.strip() == "CANVAS_SYNC":
        from canvas_sync import sync_to_kanban
        base = host_url.rstrip("/")
        threading.Thread(target=lambda: sync_to_kanban(base), daemon=True).start()
        return "Syncing Canvas now.", action

    return raw, action


# ── Auth ──────────────────────────────────────────────────────────────────────

_PUBLIC = {'login', 'google_login_start', 'google_login_callback', 'static'}

@app.before_request
def require_login():
    if request.endpoint in _PUBLIC or current_user.is_authenticated:
        return
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect('/')
    if request.method == 'POST':
        mode     = request.form.get('mode')
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        db = get_db()
        if mode == 'register':
            if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
                flash('An account with that email already exists.', 'error')
                return redirect('/login')
            db.execute(
                "INSERT INTO users (email,password_hash,created_at) VALUES (?,?,?)",
                (email, generate_password_hash(password), datetime.now().isoformat())
            )
            db.commit()
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            login_user(User(row['id'], row['email'], row['name']))
            return redirect('/')
        else:
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not row or not row['password_hash'] or \
               not check_password_hash(row['password_hash'], password):
                flash('Invalid email or password.', 'error')
                return redirect('/login')
            login_user(User(row['id'], row['email'], row['name']))
            return redirect('/')
    return render_template('login.html')


@app.route('/logout')
def logout():
    logout_user()
    return redirect('/login')


def _google_flow(redirect_uri):
    from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
    from google_auth_oauthlib.flow import Flow
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    return Flow.from_client_config(
        {'web': {
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': [redirect_uri],
        }},
        scopes=['openid',
                'https://www.googleapis.com/auth/userinfo.email',
                'https://www.googleapis.com/auth/userinfo.profile'],
        redirect_uri=redirect_uri,
    )


def _callback_uri():
    base = request.host_url.rstrip('/')
    if base.startswith('http://') and not base.startswith('http://127') and not base.startswith('http://localhost'):
        base = 'https://' + base[7:]
    return base + '/auth/google/callback'


@app.route('/auth/google/start')
def google_login_start():
    from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        flash('Google login is not configured.', 'error')
        return redirect('/login')
    try:
        flow = _google_flow(_callback_uri())
        auth_url, state = flow.authorization_url(access_type='offline')
        session['google_login_state'] = state
        # Save code_verifier if PKCE was used
        cv = getattr(flow, 'code_verifier', None)
        if cv is None:
            try:
                cv = flow.oauth2session.code_challenge
            except Exception:
                pass
        session['google_cv'] = cv
        return redirect(auth_url)
    except Exception as e:
        flash(f'Google login error: {e}', 'error')
        return redirect('/login')


@app.route('/auth/google/callback')
def google_login_callback():
    import requests as req
    try:
        callback_uri = _callback_uri()
        flow = _google_flow(callback_uri)
        flow.state = session.get('google_login_state')
        auth_response = request.url
        if auth_response.startswith('http://') and 'localhost' not in auth_response and '127.0.0.1' not in auth_response:
            auth_response = 'https://' + auth_response[7:]
        flow.fetch_token(authorization_response=auth_response,
                         code_verifier=session.get('google_cv'))
        creds = flow.credentials
        info  = req.get(
            f'https://www.googleapis.com/oauth2/v1/userinfo?access_token={creds.token}'
        ).json()
        email     = info.get('email', '')
        name      = info.get('name', '')
        google_id = info.get('id', '')
        db  = get_db()
        row = db.execute(
            "SELECT * FROM users WHERE email=? OR google_id=?", (email, google_id)
        ).fetchone()
        if row:
            db.execute("UPDATE users SET google_id=?,name=? WHERE id=?",
                       (google_id, name, row['id']))
            db.commit()
            login_user(User(row['id'], row['email'], name))
        else:
            db.execute(
                "INSERT INTO users (email,google_id,name,created_at) VALUES (?,?,?,?)",
                (email, google_id, name, datetime.now().isoformat())
            )
            db.commit()
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            login_user(User(row['id'], email, name))
        return redirect('/')
    except Exception as e:
        flash(f'Google login failed: {e}', 'error')
        return redirect('/login')


# ── Core routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/tasks")
def get_tasks():
    tasks = get_db().execute(
        "SELECT * FROM tasks ORDER BY flagged DESC, created_at DESC"
    ).fetchall()
    return jsonify([dict(t) for t in tasks])


@app.route("/api/tasks", methods=["POST"])
def add_task():
    d = request.json
    color = get_or_create_project(d.get("project", "personal"), d.get("family", "personal"))
    db = get_db()
    db.execute(
        "INSERT INTO tasks (title,status,project,family,color,created_at,due_date,description) VALUES (?,?,?,?,?,?,?,?)",
        (d["title"], "todo", d.get("project", "personal").lower(),
         d.get("family", "personal").lower(), color, datetime.now().isoformat(),
         d.get("due_date"), d.get("description"))
    )
    db.commit()
    return jsonify({"ok": True, "color": color})


@app.route("/api/tasks/<int:tid>", methods=["PATCH"])
def update_task(tid):
    d = request.json
    db = get_db()
    if "status"      in d: db.execute("UPDATE tasks SET status=?      WHERE id=?", (d["status"], tid))
    if "flagged"     in d: db.execute("UPDATE tasks SET flagged=?     WHERE id=?", (d["flagged"], tid))
    if "due_date"    in d: db.execute("UPDATE tasks SET due_date=?    WHERE id=?", (d["due_date"], tid))
    if "description" in d: db.execute("UPDATE tasks SET description=? WHERE id=?", (d["description"], tid))
    if "title"       in d: db.execute("UPDATE tasks SET title=?       WHERE id=?", (d["title"], tid))
    if "project" in d:
        color = get_or_create_project(d["project"], d.get("family", ""))
        db.execute("UPDATE tasks SET project=?,family=?,color=? WHERE id=?",
                   (d["project"], d.get("family", ""), color, tid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:tid>", methods=["DELETE"])
def delete_task(tid):
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id=?", (tid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/projects")
def get_projects():
    rows = get_db().execute("SELECT * FROM projects ORDER BY family,name").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/reminders", methods=["POST"])
def add_reminder():
    d = request.json
    db = get_db()
    db.execute("INSERT INTO reminders (time,task) VALUES (?,?)", (d["time"], d["task"]))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/reminders/pending")
def pending_reminders():
    now = datetime.now().strftime("%H:%M")
    db = get_db()
    rows = db.execute(
        "SELECT id, task FROM reminders WHERE time=? AND fired=0", (now,)
    ).fetchall()
    for row in rows:
        db.execute("UPDATE reminders SET fired=1 WHERE id=?", (row["id"],))
    db.commit()
    return jsonify([dict(r) for r in rows])


@app.route("/api/events")
def get_events():
    from calendar_sync import get_all_events
    events = get_all_events()
    try:
        from canvas_sync import get_canvas_events_for_calendar
        events += get_canvas_events_for_calendar()
    except Exception:
        pass
    db = get_db()
    feed = db.execute("SELECT url FROM canvas_feeds WHERE user_id=?",
                      (current_user.id,)).fetchone()
    if feed:
        try:
            from ical_sync import fetch_ical_events
            mappings = _get_user_mappings(db)
            events += fetch_ical_events(feed["url"], mappings=mappings)
        except Exception as e:
            print(f"iCal fetch error: {e}")
    return jsonify(sorted(events, key=lambda x: x["start"]))


@app.route("/api/canvas_sync", methods=["POST"])
def do_canvas_sync():
    from canvas_sync import sync_to_kanban
    sync_to_kanban(request.host_url.rstrip("/"))
    return jsonify({"ok": True})


# ── Canvas iCal feed (per-user) ───────────────────────────────────────────────

def _get_user_mappings(db=None):
    db = db or get_db()
    rows = db.execute(
        "SELECT course_code, course_name FROM course_mappings WHERE user_id=?",
        (current_user.id,)
    ).fetchall()
    return {r["course_code"]: r["course_name"] for r in rows}


@app.route("/api/canvas/ical", methods=["GET"])
def get_canvas_ical():
    row = get_db().execute(
        "SELECT url FROM canvas_feeds WHERE user_id=?", (current_user.id,)
    ).fetchone()
    return jsonify({"url": row["url"] if row else None})


@app.route("/api/canvas/ical", methods=["POST"])
def save_canvas_ical():
    url = (request.json.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "URL required"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO canvas_feeds (user_id,url,created_at) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET url=excluded.url",
        (current_user.id, url, datetime.now().isoformat())
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/canvas/ical", methods=["DELETE"])
def delete_canvas_ical():
    db = get_db()
    db.execute("DELETE FROM canvas_feeds WHERE user_id=?", (current_user.id,))
    db.execute("DELETE FROM course_mappings WHERE user_id=?", (current_user.id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/canvas/courses")
def get_canvas_courses():
    row = get_db().execute(
        "SELECT url FROM canvas_feeds WHERE user_id=?", (current_user.id,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "No Canvas feed configured"}), 400
    try:
        from ical_sync import detect_course_codes
        codes = detect_course_codes(row["url"])
        mappings = _get_user_mappings()
        return jsonify({"ok": True, "codes": codes, "mappings": mappings})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/canvas/course-mappings", methods=["POST"])
def save_course_mappings():
    d = request.json
    db = get_db()
    for code, name in d.items():
        name = (name or "").strip()
        if name:
            db.execute(
                "INSERT INTO course_mappings (user_id,course_code,course_name) VALUES (?,?,?) "
                "ON CONFLICT(user_id,course_code) DO UPDATE SET course_name=excluded.course_name",
                (current_user.id, code, name)
            )
        else:
            db.execute(
                "DELETE FROM course_mappings WHERE user_id=? AND course_code=?",
                (current_user.id, code)
            )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/canvas/ical/sync", methods=["POST"])
def sync_canvas_ical_now():
    db = get_db()
    row = db.execute(
        "SELECT url FROM canvas_feeds WHERE user_id=?", (current_user.id,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "No Canvas feed configured"}), 400
    mappings = _get_user_mappings(db)
    host = request.host_url.rstrip("/")
    threading.Thread(
        target=lambda: _run_ical_sync(row["url"], host, mappings), daemon=True
    ).start()
    return jsonify({"ok": True})


def _run_ical_sync(url, host, mappings=None):
    try:
        from ical_sync import sync_ical_to_kanban
        sync_ical_to_kanban(url, host, mappings=mappings)
    except Exception as e:
        print(f"Canvas iCal sync error: {e}")


# ── Voice ─────────────────────────────────────────────────────────────────────

@app.route("/api/voice", methods=["POST"])
def voice_command():
    d = request.json
    text = (d.get("text") or "").strip()
    if not text:
        return jsonify({"reply": "", "action": {}})

    sid = session.get("chat_id")
    if not sid:
        sid = secrets.token_hex(8)
        session["chat_id"] = sid

    chat = get_chat(sid)
    response = chat.send_message(text)
    reply, action = handle_voice_reply(response.text.strip(), request.host_url)
    return jsonify({"reply": reply, "action": action})


# ── Calendar accounts ─────────────────────────────────────────────────────────

@app.route("/api/calendar/accounts")
def list_calendar_accounts():
    rows = get_db().execute(
        "SELECT id, type, label, active FROM calendar_accounts ORDER BY created_at"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/calendar/accounts/<int:aid>", methods=["DELETE"])
def delete_calendar_account(aid):
    db = get_db()
    db.execute("DELETE FROM calendar_accounts WHERE id=?", (aid,))
    db.commit()
    return jsonify({"ok": True})


# ── Google OAuth ──────────────────────────────────────────────────────────────

@app.route("/api/calendar/auth/google/start")
def google_auth_start():
    from google_auth_oauthlib.flow import Flow
    from config import GOOGLE_CREDS
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    flow = Flow.from_client_secrets_file(
        GOOGLE_CREDS,
        scopes=[
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
            "openid",
        ],
        redirect_uri=request.host_url.rstrip("/") + "/api/calendar/auth/google/callback"
    )
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true")
    session["google_state"] = state
    return redirect(auth_url)


@app.route("/api/calendar/auth/google/callback")
def google_auth_callback():
    from google_auth_oauthlib.flow import Flow
    from config import GOOGLE_CREDS
    import requests as req
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    redirect_uri = request.host_url.rstrip("/") + "/api/calendar/auth/google/callback"
    flow = Flow.from_client_secrets_file(
        GOOGLE_CREDS,
        scopes=[
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
            "openid",
        ],
        redirect_uri=redirect_uri,
        state=session.get("google_state")
    )
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    r = req.get(f"https://www.googleapis.com/oauth2/v1/userinfo?access_token={creds.token}")
    email = r.json().get("email", "Google Account")
    db = get_db()
    db.execute(
        "INSERT INTO calendar_accounts (type,label,credentials,active,created_at) VALUES (?,?,?,1,?)",
        ("google", email, creds.to_json(), datetime.now().isoformat())
    )
    db.commit()
    return redirect("/?accounts=1")


# ── Outlook OAuth ─────────────────────────────────────────────────────────────

@app.route("/api/calendar/auth/outlook/start")
def outlook_auth_start():
    import msal
    from config import OUTLOOK_CLIENT_ID, OUTLOOK_SECRET, OUTLOOK_TENANT
    msal_app = msal.ConfidentialClientApplication(
        OUTLOOK_CLIENT_ID,
        client_credential=OUTLOOK_SECRET,
        authority=f"https://login.microsoftonline.com/{OUTLOOK_TENANT}"
    )
    state = secrets.token_urlsafe(16)
    session["outlook_state"] = state
    redirect_uri = request.host_url.rstrip("/") + "/api/calendar/auth/outlook/callback"
    auth_url = msal_app.get_authorization_request_url(
        scopes=["https://graph.microsoft.com/Calendars.Read", "offline_access"],
        redirect_uri=redirect_uri,
        state=state
    )
    return redirect(auth_url)


@app.route("/api/calendar/auth/outlook/callback")
def outlook_auth_callback():
    import msal
    import requests as req
    from config import OUTLOOK_CLIENT_ID, OUTLOOK_SECRET, OUTLOOK_TENANT
    if request.args.get("state") != session.get("outlook_state"):
        return "State mismatch — please try connecting again.", 400
    redirect_uri = request.host_url.rstrip("/") + "/api/calendar/auth/outlook/callback"
    msal_app = msal.ConfidentialClientApplication(
        OUTLOOK_CLIENT_ID,
        client_credential=OUTLOOK_SECRET,
        authority=f"https://login.microsoftonline.com/{OUTLOOK_TENANT}"
    )
    result = msal_app.acquire_token_by_authorization_code(
        request.args["code"],
        scopes=["https://graph.microsoft.com/Calendars.Read", "offline_access"],
        redirect_uri=redirect_uri
    )
    if "error" in result:
        return f"Auth error: {result.get('error_description', result['error'])}", 400
    r = req.get("https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {result['access_token']}"})
    info = r.json()
    email = info.get("mail") or info.get("userPrincipalName", "Outlook Account")
    db = get_db()
    db.execute(
        "INSERT INTO calendar_accounts (type,label,credentials,active,created_at) VALUES (?,?,?,1,?)",
        ("outlook", email, json.dumps(result), datetime.now().isoformat())
    )
    db.commit()
    return redirect("/?accounts=1")


# ── iCloud ────────────────────────────────────────────────────────────────────

@app.route("/api/calendar/auth/icloud", methods=["POST"])
def icloud_auth():
    import caldav
    d = request.json
    username = (d.get("username") or "").strip()
    password = (d.get("password") or "").strip()
    if not username or not password:
        return jsonify({"ok": False, "error": "Apple ID and password are required"}), 400
    try:
        client = caldav.DAVClient(
            url="https://caldav.icloud.com",
            username=username,
            password=password
        )
        client.principal()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    db = get_db()
    db.execute(
        "INSERT INTO calendar_accounts (type,label,credentials,active,created_at) VALUES (?,?,?,1,?)",
        ("icloud", username, json.dumps({"username": username, "password": password}),
         datetime.now().isoformat())
    )
    db.commit()
    return jsonify({"ok": True})


# ── Background jobs ───────────────────────────────────────────────────────────

def canvas_auto_loop():
    def job():
        try:
            from canvas_sync import sync_to_kanban
            sync_to_kanban("http://localhost:5001")
        except Exception as e:
            print(f"Canvas API sync error: {e}")
        try:
            row = get_db().execute(
                "SELECT credentials FROM calendar_accounts WHERE type='canvas_ical' AND active=1 LIMIT 1"
            ).fetchone()
            if row:
                _run_ical_sync(json.loads(row["credentials"])["url"], "http://localhost:5001")
        except Exception as e:
            print(f"Canvas iCal loop error: {e}")
    sched.every(15).minutes.do(job)
    while True:
        sched.run_pending()
        _time.sleep(30)


init_db()
threading.Thread(target=canvas_auto_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
