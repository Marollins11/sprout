# app.py
from flask import Flask, jsonify, request, render_template, redirect, session, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import sqlite3, threading, time as _time, schedule as sched, os, json, secrets, traceback
from datetime import datetime
from google import genai

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
DB = os.getenv("DATABASE_PATH", "tasks.db")

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
            model="gemini-2.5-flash",
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
    for col, defn in [("due_date", "TEXT"), ("description", "TEXT"),
                       ("archived", "INTEGER DEFAULT 0"), ("done_at", "TEXT"),
                       ("user_id", "INTEGER"), ("priority", "TEXT"),
                       ("position", "INTEGER DEFAULT 0")]:
        try:
            db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {defn}")
        except Exception:
            pass
    # Projects: recreate with per-user unique constraint if needed
    proj_cols = [r[1] for r in db.execute("PRAGMA table_info(projects)").fetchall()]
    if 'user_id' not in proj_cols:
        db.execute("DROP TABLE IF EXISTS projects")
        db.execute("""CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            name TEXT NOT NULL,
            family TEXT NOT NULL,
            color TEXT NOT NULL,
            UNIQUE(user_id, name)
        )""")
    else:
        db.execute("""CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            name TEXT NOT NULL,
            family TEXT NOT NULL,
            color TEXT NOT NULL,
            UNIQUE(user_id, name)
        )""")
    db.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT, task TEXT, fired INTEGER DEFAULT 0, user_id INTEGER
    )""")
    for col, defn in [("user_id", "INTEGER")]:
        try:
            db.execute(f"ALTER TABLE reminders ADD COLUMN {col} {defn}")
        except Exception:
            pass
    db.execute("""CREATE TABLE IF NOT EXISTS calendar_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        label TEXT NOT NULL,
        credentials TEXT,
        active INTEGER DEFAULT 1,
        created_at TEXT,
        user_id INTEGER
    )""")
    for col, defn in [("user_id", "INTEGER")]:
        try:
            db.execute(f"ALTER TABLE calendar_accounts ADD COLUMN {col} {defn}")
        except Exception:
            pass
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
        color TEXT,
        UNIQUE(user_id, course_code)
    )""")
    try:
        db.execute("ALTER TABLE course_mappings ADD COLUMN color TEXT")
    except Exception:
        pass
    db.execute("""CREATE TABLE IF NOT EXISTS subtasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        completed INTEGER DEFAULT 0,
        created_at TEXT
    )""")
    for col, defn in [("status", "TEXT DEFAULT 'todo'"), ("flagged", "INTEGER DEFAULT 0"), ("due_date", "TEXT"),
                       ("position", "INTEGER DEFAULT 0")]:
        try:
            db.execute(f"ALTER TABLE subtasks ADD COLUMN {col} {defn}")
        except Exception:
            pass
    db.execute("""CREATE TABLE IF NOT EXISTS status_updates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        note TEXT NOT NULL,
        created_at TEXT
    )""")
    db.execute("INSERT OR IGNORE INTO projects (id,user_id,name,family,color) VALUES (1,0,'personal','personal','#6B21A8')")
    db.commit()


def get_or_create_project(name, family, db=None, uid=None):
    global auto_idx
    _own = db is None
    db = db or get_db()
    nm = name.lower().strip()
    fm = (family or "").lower().strip()
    if uid is not None:
        row = db.execute("SELECT color FROM projects WHERE name=? AND user_id=?", (nm, uid)).fetchone()
    else:
        row = db.execute("SELECT color FROM projects WHERE name=?", (nm,)).fetchone()
    if row:
        return row["color"]
    if fm in FAMILY_PALETTES:
        if uid is not None:
            used = [r["color"] for r in db.execute(
                "SELECT color FROM projects WHERE family=? AND user_id=?", (fm, uid)).fetchall()]
        else:
            used = [r["color"] for r in db.execute(
                "SELECT color FROM projects WHERE family=?", (fm,)).fetchall()]
        pal = FAMILY_PALETTES[fm]
        color = next((c for c in pal if c not in used), pal[-1])
    else:
        color = AUTO_PALETTE[auto_idx % len(AUTO_PALETTE)]
        auto_idx += 1
        if fm:
            FAMILY_PALETTES[fm] = [color]
    effective_uid = uid if uid is not None else 0
    db.execute("INSERT OR IGNORE INTO projects (user_id,name,family,color) VALUES (?,?,?,?)",
               (effective_uid, nm, fm or "other", "#" + color))
    if _own:
        db.commit()
    return "#" + color


# ── Voice command handler ────────────────────────────────────────────────────

def handle_voice_reply(raw, host_url, uid=None):
    """Execute a Gemini structured reply against the DB. Returns (spoken_text, action)."""
    action = {}
    db = get_db()

    if raw.startswith("TASK|") or raw.startswith("ADD_TASK|"):
        _, title, family, project = raw.split("|", 3)
        project = project.strip() or family.strip() or "general"
        color = get_or_create_project(project, family, uid=uid)
        db.execute(
            "INSERT INTO tasks (title,status,project,family,color,created_at,user_id) VALUES (?,?,?,?,?,?,?)",
            (title, "todo", project.lower(), family.lower(), color, datetime.now().isoformat(), uid)
        )
        db.commit()
        return f"Added {title} to {project}.", action

    if raw.startswith("MOVE_STATUS|"):
        _, match, status = raw.split("|", 2)
        task = db.execute(
            "SELECT id FROM tasks WHERE instr(lower(title), ?) > 0 AND user_id=?", (match.lower(), uid)
        ).fetchone()
        if not task:
            return "I couldn't find that task.", action
        db.execute("UPDATE tasks SET status=? WHERE id=? AND user_id=?", (status, task["id"], uid))
        db.commit()
        return f"Moved to {status.replace('_', ' ')}.", action

    if raw.startswith("MOVE_PROJECT|"):
        _, match, family, project = raw.split("|", 3)
        task = db.execute(
            "SELECT id FROM tasks WHERE instr(lower(title), ?) > 0 AND user_id=?", (match.lower(), uid)
        ).fetchone()
        if not task:
            return "I couldn't find that task.", action
        color = get_or_create_project(project, family, uid=uid)
        db.execute(
            "UPDATE tasks SET project=?,family=?,color=? WHERE id=? AND user_id=?",
            (project.lower(), family.lower(), color, task["id"], uid)
        )
        db.commit()
        return f"Moved to {project}.", action

    if raw.startswith("FLAG|"):
        _, match, flagged = raw.split("|", 2)
        task = db.execute(
            "SELECT id FROM tasks WHERE instr(lower(title), ?) > 0 AND user_id=?", (match.lower(), uid)
        ).fetchone()
        if not task:
            return "I couldn't find that task.", action
        db.execute("UPDATE tasks SET flagged=? WHERE id=? AND user_id=?", (int(flagged), task["id"], uid))
        db.commit()
        return "Flagged." if flagged == "1" else "Flag removed.", action

    if raw.startswith("FILTER|"):
        _, family, sub = raw.split("|", 2)
        action = {"type": "FILTER", "family": family, "sub": sub}
        return "Showing all tasks." if family == "all" else f"Filtering to {sub}.", action

    if raw.startswith("REMINDER|"):
        _, t, task_desc = raw.split("|", 2)
        db.execute("INSERT INTO reminders (time,task,user_id) VALUES (?,?,?)", (t, task_desc, uid))
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
    # Return JSON for API routes so fetch() doesn't receive an HTML redirect
    if request.path.startswith('/api/'):
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    return redirect(url_for('login'))


@app.errorhandler(Exception)
def handle_exception(e):
    orig = getattr(e, 'original_exception', e)
    tb = traceback.format_exc()
    print(f"[ERROR] {request.method} {request.path}\n{tb}", flush=True)
    msg = f"{type(orig).__name__}: {orig}"
    if request.path.startswith('/api/'):
        return jsonify({"ok": False, "error": msg}), 500
    return f"<pre>Error on {request.path}:\n{msg}\n\n{tb}</pre>", 500


@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith('/api/'):
        return jsonify({"ok": False, "error": "Not found"}), 404
    return f"Not found: {request.path}", 404


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect('/')
    if request.method == 'POST':
        mode     = request.form.get('mode')
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        db = get_db()
        remember = request.form.get('remember') == 'on'
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
            login_user(User(row['id'], row['email'], row['name']), remember=remember)
            return redirect('/')
        else:
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not row or not row['password_hash'] or \
               not check_password_hash(row['password_hash'], password):
                flash('Invalid email or password.', 'error')
                return redirect('/login')
            login_user(User(row['id'], row['email'], row['name']), remember=remember)
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
    db = get_db()
    uid = current_user.id
    db.execute("""
        UPDATE tasks SET archived=1
        WHERE status='done' AND (archived IS NULL OR archived=0)
          AND done_at IS NOT NULL AND user_id=?
          AND julianday('now') - julianday(done_at) > 14
    """, (uid,))
    db.commit()
    sub_counts = """
        (SELECT COUNT(*) FROM subtasks s WHERE s.task_id=t.id) as subtask_total,
        (SELECT COUNT(*) FROM subtasks s WHERE s.task_id=t.id AND (s.status='done' OR (s.status IS NULL AND s.completed=1))) as subtask_done
    """
    if request.args.get('archived') == '1':
        tasks = db.execute(
            f"SELECT t.*, {sub_counts} FROM tasks t WHERE t.archived=1 AND t.user_id=? ORDER BY t.done_at DESC, t.created_at DESC",
            (uid,)
        ).fetchall()
    else:
        tasks = db.execute(
            f"SELECT t.*, {sub_counts} FROM tasks t WHERE (t.archived IS NULL OR t.archived=0) AND t.user_id=? ORDER BY t.flagged DESC, t.created_at DESC",
            (uid,)
        ).fetchall()
    task_list = [dict(t) for t in tasks]
    task_ids = [t['id'] for t in task_list]
    subtasks_by_task = {}
    if task_ids:
        placeholders = ','.join('?' * len(task_ids))
        sub_rows = db.execute(
            f"SELECT * FROM subtasks WHERE task_id IN ({placeholders}) ORDER BY position, created_at",
            task_ids
        ).fetchall()
        for r in sub_rows:
            sd = dict(r)
            if not sd.get('status'):
                sd['status'] = 'done' if sd.get('completed') else 'todo'
            subtasks_by_task.setdefault(sd['task_id'], []).append(sd)
    for t in task_list:
        t['subtasks'] = subtasks_by_task.get(t['id'], [])
    return jsonify(task_list)


@app.route("/api/tasks", methods=["POST"])
def add_task():
    d = request.json
    uid = current_user.id
    project = d.get("project", "personal").strip().lower()
    family = d.get("family", "personal").strip().lower()
    color = get_or_create_project(project, family, uid=uid)
    db = get_db()
    next_pos = db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM tasks WHERE user_id=? AND status='todo'", (uid,)
    ).fetchone()[0]
    db.execute(
        "INSERT INTO tasks (title,status,project,family,color,created_at,due_date,description,user_id,priority,position) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (d["title"], "todo", project,
         family, color, datetime.now().isoformat(),
         d.get("due_date"), d.get("description"), uid, d.get("priority") or None, next_pos)
    )
    db.commit()
    return jsonify({"ok": True, "color": color})


@app.route("/api/tasks/<int:tid>", methods=["PATCH"])
def update_task(tid):
    d = request.json
    db = get_db()
    uid = current_user.id
    if "status" in d:
        new_status = d["status"]
        db.execute("UPDATE tasks SET status=? WHERE id=? AND user_id=?", (new_status, tid, uid))
        if new_status == "done":
            db.execute("UPDATE tasks SET done_at=? WHERE id=? AND user_id=?", (datetime.now().isoformat(), tid, uid))
        else:
            db.execute("UPDATE tasks SET done_at=NULL WHERE id=? AND user_id=?", (tid, uid))
    if "archived" in d:
        db.execute("UPDATE tasks SET archived=? WHERE id=? AND user_id=?", (d["archived"], tid, uid))
        if d["archived"] == 0:
            db.execute("UPDATE tasks SET status='done', done_at=? WHERE id=? AND user_id=?",
                       (datetime.now().isoformat(), tid, uid))
    if "flagged"     in d: db.execute("UPDATE tasks SET flagged=?     WHERE id=? AND user_id=?", (d["flagged"], tid, uid))
    if "due_date"    in d: db.execute("UPDATE tasks SET due_date=?    WHERE id=? AND user_id=?", (d["due_date"], tid, uid))
    if "description" in d: db.execute("UPDATE tasks SET description=? WHERE id=? AND user_id=?", (d["description"], tid, uid))
    if "title"       in d: db.execute("UPDATE tasks SET title=?       WHERE id=? AND user_id=?", (d["title"], tid, uid))
    if "priority"    in d: db.execute("UPDATE tasks SET priority=?    WHERE id=? AND user_id=?", (d["priority"] or None, tid, uid))
    color = None
    if "project" in d:
        project = d["project"].strip().lower()
        family = (d.get("family") or "").strip().lower()
        color = get_or_create_project(project, family, uid=uid)
        db.execute("UPDATE tasks SET project=?,family=?,color=? WHERE id=? AND user_id=?",
                   (project, family, color, tid, uid))
    db.commit()
    return jsonify({"ok": True, "color": color})


@app.route("/api/tasks/<int:tid>", methods=["DELETE"])
def delete_task(tid):
    db = get_db()
    db.execute("DELETE FROM subtasks WHERE task_id=?", (tid,))
    db.execute("DELETE FROM tasks WHERE id=? AND user_id=?", (tid, current_user.id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/tasks/reorder", methods=["PATCH"])
def reorder_tasks():
    d = request.json
    uid = current_user.id
    status = d.get("status")
    order = d.get("order", [])
    db = get_db()
    for idx, tid in enumerate(order):
        if status is not None:
            db.execute("UPDATE tasks SET status=?, position=? WHERE id=? AND user_id=?", (status, idx, tid, uid))
        else:
            db.execute("UPDATE tasks SET position=? WHERE id=? AND user_id=?", (idx, tid, uid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:tid>/subtasks")
def get_subtasks(tid):
    db = get_db()
    task = db.execute("SELECT id FROM tasks WHERE id=? AND user_id=?", (tid, current_user.id)).fetchone()
    if not task:
        return jsonify([])
    rows = db.execute(
        "SELECT * FROM subtasks WHERE task_id=? ORDER BY position, created_at", (tid,)
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if not d.get('status'):
            d['status'] = 'done' if d.get('completed') else 'todo'
        result.append(d)
    return jsonify(result)


@app.route("/api/tasks/<int:tid>/subtasks", methods=["POST"])
def add_subtask(tid):
    db = get_db()
    task = db.execute("SELECT id FROM tasks WHERE id=? AND user_id=?", (tid, current_user.id)).fetchone()
    if not task:
        return jsonify({"ok": False, "error": "Not found"}), 404
    title = (request.json.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "Title required"}), 400
    next_pos = db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM subtasks WHERE task_id=?", (tid,)
    ).fetchone()[0]
    cur = db.execute(
        "INSERT INTO subtasks (task_id,title,created_at,position) VALUES (?,?,?,?)",
        (tid, title, datetime.now().isoformat(), next_pos)
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/subtasks/<int:sid>", methods=["PATCH"])
def update_subtask(sid):
    d = request.json
    db = get_db()
    owns = "task_id IN (SELECT id FROM tasks WHERE user_id=?)"
    if "status" in d:
        completed = 1 if d["status"] == "done" else 0
        db.execute(f"UPDATE subtasks SET status=?, completed=? WHERE id=? AND {owns}",
                   (d["status"], completed, sid, current_user.id))
    if "completed" in d and "status" not in d:
        status = "done" if d["completed"] else "todo"
        db.execute(f"UPDATE subtasks SET completed=?, status=? WHERE id=? AND {owns}",
                   (d["completed"], status, sid, current_user.id))
    if "flagged" in d:
        db.execute(f"UPDATE subtasks SET flagged=? WHERE id=? AND {owns}", (d["flagged"], sid, current_user.id))
    if "due_date" in d:
        db.execute(f"UPDATE subtasks SET due_date=? WHERE id=? AND {owns}", (d["due_date"], sid, current_user.id))
    if "title" in d:
        db.execute(f"UPDATE subtasks SET title=? WHERE id=? AND {owns}", (d["title"].strip(), sid, current_user.id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/subtasks/<int:sid>", methods=["DELETE"])
def delete_subtask(sid):
    db = get_db()
    db.execute(
        "DELETE FROM subtasks WHERE id=? AND task_id IN (SELECT id FROM tasks WHERE user_id=?)",
        (sid, current_user.id)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:tid>/subtasks/reorder", methods=["PATCH"])
def reorder_subtasks(tid):
    db = get_db()
    task = db.execute("SELECT id FROM tasks WHERE id=? AND user_id=?", (tid, current_user.id)).fetchone()
    if not task:
        return jsonify({"ok": False, "error": "Not found"}), 404
    order = request.json.get("order", [])
    for idx, sid in enumerate(order):
        db.execute("UPDATE subtasks SET position=? WHERE id=? AND task_id=?", (idx, sid, tid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:tid>/updates")
def get_status_updates(tid):
    db = get_db()
    task = db.execute("SELECT id FROM tasks WHERE id=? AND user_id=?", (tid, current_user.id)).fetchone()
    if not task:
        return jsonify([])
    rows = db.execute(
        "SELECT * FROM status_updates WHERE task_id=? ORDER BY created_at DESC", (tid,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/tasks/<int:tid>/updates", methods=["POST"])
def add_status_update(tid):
    db = get_db()
    task = db.execute("SELECT id FROM tasks WHERE id=? AND user_id=?", (tid, current_user.id)).fetchone()
    if not task:
        return jsonify({"ok": False, "error": "Not found"}), 404
    note = (request.json.get("note") or "").strip()
    if not note:
        return jsonify({"ok": False, "error": "Note required"}), 400
    cur = db.execute(
        "INSERT INTO status_updates (task_id,note,created_at) VALUES (?,?,?)",
        (tid, note, datetime.now().isoformat())
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/updates/<int:update_id>", methods=["DELETE"])
def delete_status_update(update_id):
    db = get_db()
    db.execute(
        "DELETE FROM status_updates WHERE id=? AND task_id IN (SELECT id FROM tasks WHERE user_id=?)",
        (update_id, current_user.id)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/projects")
def get_projects():
    rows = get_db().execute(
        "SELECT * FROM projects WHERE user_id=? ORDER BY family,name", (current_user.id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/reminders", methods=["POST"])
def add_reminder():
    d = request.json
    db = get_db()
    db.execute("INSERT INTO reminders (time,task,user_id) VALUES (?,?,?)", (d["time"], d["task"], current_user.id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/reminders/pending")
def pending_reminders():
    now = datetime.now().strftime("%H:%M")
    db = get_db()
    rows = db.execute(
        "SELECT id, task FROM reminders WHERE time=? AND fired=0 AND user_id=?", (now, current_user.id)
    ).fetchall()
    for row in rows:
        db.execute("UPDATE reminders SET fired=1 WHERE id=?", (row["id"],))
    db.commit()
    return jsonify([dict(r) for r in rows])


@app.route("/api/events")
def get_events():
    from calendar_sync import get_all_events
    events = get_all_events(user_id=current_user.id)
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
            colors  = _get_user_mapping_colors(db)
            events += fetch_ical_events(feed["url"], mappings=mappings, colors=colors)
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


def _get_user_mapping_colors(db=None):
    db = db or get_db()
    rows = db.execute(
        "SELECT course_code, color FROM course_mappings WHERE user_id=? AND color IS NOT NULL",
        (current_user.id,)
    ).fetchall()
    return {r["course_code"]: r["color"] for r in rows}


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
    try:
        row = get_db().execute(
            "SELECT url FROM canvas_feeds WHERE user_id=?", (current_user.id,)
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "No Canvas feed configured"}), 400
        from ical_sync import detect_course_codes
        codes = detect_course_codes(row["url"])
        mappings = _get_user_mappings()
        return jsonify({"ok": True, "codes": codes, "mappings": mappings})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/canvas/course-mappings", methods=["GET"])
def get_course_mappings():
    rows = get_db().execute(
        "SELECT course_code, course_name, color FROM course_mappings WHERE user_id=? ORDER BY course_name",
        (current_user.id,)
    ).fetchall()
    return jsonify([{"code": r["course_code"], "name": r["course_name"],
                     "color": r["color"] or "#818cf8"} for r in rows])


@app.route("/api/canvas/course-mappings/color", methods=["POST"])
def update_mapping_color():
    data  = request.json
    code  = data.get("code", "")
    color = data.get("color", "#818cf8")
    db = get_db()
    row = db.execute(
        "SELECT course_name FROM course_mappings WHERE user_id=? AND course_code=?",
        (current_user.id, code)
    ).fetchone()
    db.execute(
        "UPDATE course_mappings SET color=? WHERE user_id=? AND course_code=?",
        (color, current_user.id, code)
    )
    if row:
        nm = row["course_name"].lower().strip()
        db.execute("UPDATE projects SET color=? WHERE name=? AND user_id=?", (color, nm, current_user.id))
        db.execute("UPDATE tasks SET color=? WHERE LOWER(project)=? AND user_id=?", (color, nm, current_user.id))
    db.commit()
    return jsonify({"ok": True})


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


@app.route("/api/canvas/course-mappings/<path:code>", methods=["DELETE"])
def delete_course_mapping(code):
    db = get_db()
    row = db.execute(
        "SELECT course_name FROM course_mappings WHERE user_id=? AND course_code=?",
        (current_user.id, code)
    ).fetchone()
    db.execute("DELETE FROM course_mappings WHERE user_id=? AND course_code=?",
               (current_user.id, code))
    if row:
        nm = row["course_name"].lower().strip()
        db.execute("DELETE FROM tasks WHERE LOWER(project)=? AND user_id=?", (nm, current_user.id))
        db.execute("DELETE FROM projects WHERE LOWER(name)=? AND user_id=?", (nm, current_user.id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/canvas/ical/sync", methods=["POST"])
def sync_canvas_ical_now():
    try:
        from ical_sync import fetch_ical_events
        db = get_db()
        row = db.execute(
            "SELECT url FROM canvas_feeds WHERE user_id=?", (current_user.id,)
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "No Canvas feed configured"}), 400
        mappings = _get_user_mappings(db)
        events = fetch_ical_events(row["url"], mappings=mappings)
        uid = current_user.id
        existing = {t["title"] for t in db.execute(
            "SELECT title FROM tasks WHERE user_id=?", (uid,)
        ).fetchall()}
        SKIP_PATTERNS = ("class session", "class meeting", "lecture", "office hours")
        added = 0
        skipped_session = 0
        skipped_dupe = 0
        for e in events:
            title = e["title"]
            if any(title.lower().startswith(p) for p in SKIP_PATTERNS):
                skipped_session += 1
                continue
            if title in existing:
                skipped_dupe += 1
                continue
            color = get_or_create_project(e["course"], "school", db=db, uid=uid)
            db.execute(
                "INSERT INTO tasks (title,status,project,family,color,created_at,due_date,description,user_id)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (title, "todo", e["course"].lower(), "school", color,
                 datetime.now().isoformat(), e["start"], e.get("description"), uid)
            )
            existing.add(title)
            added += 1
        db.commit()
        return jsonify({"ok": True, "added": added,
                        "total": len(events),
                        "skipped_session": skipped_session,
                        "skipped_dupe": skipped_dupe})
    except Exception as e:
        print(f"[sync error] {traceback.format_exc()}", flush=True)
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


# ── Voice ─────────────────────────────────────────────────────────────────────

@app.route("/api/voice", methods=["POST"])
def voice_command():
    d = request.json
    text = (d.get("text") or "").strip()
    if not text:
        return jsonify({"reply": "", "action": {}})

    try:
        sid = session.get("chat_id")
        if not sid:
            sid = secrets.token_hex(8)
            session["chat_id"] = sid

        chat = get_chat(sid)
        response = chat.send_message(text)
        reply, action = handle_voice_reply(response.text.strip(), request.host_url, uid=current_user.id)
        return jsonify({"reply": reply, "action": action})
    except Exception as e:
        print(f"[voice error] {traceback.format_exc()}", flush=True)
        return jsonify({"reply": f"Sorry, something went wrong: {type(e).__name__}: {e}", "action": {}, "error": True})


# ── Calendar accounts ─────────────────────────────────────────────────────────

@app.route("/api/calendar/accounts")
def list_calendar_accounts():
    rows = get_db().execute(
        "SELECT id, type, label, active FROM calendar_accounts WHERE user_id=? ORDER BY created_at",
        (current_user.id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/calendar/accounts/<int:aid>", methods=["DELETE"])
def delete_calendar_account(aid):
    db = get_db()
    db.execute("DELETE FROM calendar_accounts WHERE id=? AND user_id=?", (aid, current_user.id))
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
        "INSERT INTO calendar_accounts (type,label,credentials,active,created_at,user_id) VALUES (?,?,?,1,?,?)",
        ("google", email, creds.to_json(), datetime.now().isoformat(), current_user.id)
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
        "INSERT INTO calendar_accounts (type,label,credentials,active,created_at,user_id) VALUES (?,?,?,1,?,?)",
        ("outlook", email, json.dumps(result), datetime.now().isoformat(), current_user.id)
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
        "INSERT INTO calendar_accounts (type,label,credentials,active,created_at,user_id) VALUES (?,?,?,1,?,?)",
        ("icloud", username, json.dumps({"username": username, "password": password}),
         datetime.now().isoformat(), current_user.id)
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
    sched.every(15).minutes.do(job)
    while True:
        sched.run_pending()
        _time.sleep(30)


init_db()
threading.Thread(target=canvas_auto_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
