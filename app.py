import os
import sqlite3
from flask import Flask, request, render_template, redirect, url_for, flash, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from pathlib import Path
from zoneinfo import ZoneInfo
import praw

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

# Uploads
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/data/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024  # MB

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# Timezone for interpreting user input times
APP_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "UTC"))

# Database
DB_FILE = os.getenv("DB_FILE", "/data/posts.db")

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS scheduled_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subreddit TEXT NOT NULL,
        title TEXT NOT NULL,
        post_type TEXT NOT NULL, -- link | text | image
        content TEXT NOT NULL,   -- url, selftext, or image path on disk
        post_time TEXT NOT NULL, -- UTC "YYYY-MM-DD HH:MM"
        posted INTEGER DEFAULT 0,
        last_error TEXT DEFAULT NULL,
        created_at TEXT NOT NULL,
        flair_id TEXT DEFAULT NULL,  -- New column for flair ID
        flair_text TEXT DEFAULT NULL -- New column for flair display text
    )
    """)
    # Best-effort schema upgrades
    try:
        c.execute("ALTER TABLE scheduled_posts ADD COLUMN last_error TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE scheduled_posts ADD COLUMN created_at TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE scheduled_posts ADD COLUMN flair_id TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE scheduled_posts ADD COLUMN flair_text TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

init_db()

# Reddit API
reddit = praw.Reddit(
    client_id=os.getenv("REDDIT_CLIENT_ID"),
    client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
    username=os.getenv("REDDIT_USERNAME"),
    password=os.getenv("REDDIT_PASSWORD"),
    user_agent=os.getenv("REDDIT_USER_AGENT", "reddit-scheduler/0.3 by u/yourusername")
)

def get_subreddit_flairs(subreddit_name):
    """Fetch available flairs for a subreddit"""
    try:
        sub = reddit.subreddit(subreddit_name)
        flairs = []
        for flair in sub.flair.link_templates:
            flairs.append({
                'id': flair['id'],
                'text': flair['text'],
                'editable': flair.get('text_editable', False)
            })
        return flairs, None
    except Exception as e:
        return None, str(e)

def post_to_reddit(subreddit, title, post_type, content, flair_id=None):
    try:
        sub = reddit.subreddit(subreddit)
        if post_type == "link":
            submission = sub.submit(title=title, url=content, flair_id=flair_id)
        elif post_type == "text":
            submission = sub.submit(title=title, selftext=content, flair_id=flair_id)
        elif post_type == "image":
            submission = sub.submit_image(title=title, image_path=content, flair_id=flair_id)
        else:
            raise ValueError(f"Unknown post_type: {post_type}")
        return None  # success
    except Exception as e:
        return str(e)

def check_scheduled_posts():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    c.execute("SELECT id, subreddit, title, post_type, content, flair_id FROM scheduled_posts WHERE post_time<=? AND posted=0", (now_utc,))
    rows = c.fetchall()
    for row in rows:
        post_id, subreddit, title, post_type, content, flair_id = row
        err = post_to_reddit(subreddit, title, post_type, content, flair_id)
        if err is None:
            c.execute("UPDATE scheduled_posts SET posted=1, last_error=NULL WHERE id=?", (post_id,))
        else:
            c.execute("UPDATE scheduled_posts SET last_error=? WHERE id=?", (err, post_id))
    conn.commit()
    conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(func=check_scheduled_posts, trigger="interval", minutes=1)
scheduler.start()

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        subreddit = request.form.get("subreddit", "").strip().lstrip("r/")
        title = request.form.get("title", "").strip()
        post_type = request.form.get("post_type")
        post_time_str = request.form.get("post_time", "").strip()
        flair_id = request.form.get("flair_id", "").strip()
        flair_text = request.form.get("flair_text", "").strip()

        if not subreddit or not title or not post_type or not post_time_str:
            flash("All fields are required.")
            return redirect(url_for("index"))

        # Parse local time and convert to UTC (stored as minute precision)
        try:
            local_naive = datetime.strptime(post_time_str, "%Y-%m-%d %H:%M")
            local_dt = local_naive.replace(tzinfo=APP_TZ)
            post_time_utc = local_dt.astimezone(timezone.utc)
            post_time_store = post_time_utc.strftime("%Y-%m-%d %H:%M")
        except Exception:
            flash("Invalid date/time format. Use YYYY-MM-DD HH:MM")
            return redirect(url_for("index"))

        content_value = ""
        if post_type == "link":
            content_value = request.form.get("content", "").strip()
            if not content_value:
                flash("URL is required for link posts.")
                return redirect(url_for("index"))
        elif post_type == "text":
            content_value = request.form.get("content", "").strip()
            if not content_value:
                flash("Text body is required for text posts.")
                return redirect(url_for("index"))
        elif post_type == "image":
            # Prefer uploaded file; fallback to provided path text if any
            file = request.files.get("image_file")
            if file and file.filename:
                if not allowed_file(file.filename):
                    flash("Only PNG/JPG/JPEG images are allowed.")
                    return redirect(url_for("index"))
                safe = secure_filename(file.filename)
                save_path = UPLOAD_DIR / f"{int(datetime.now().timestamp())}_{safe}"
                file.save(save_path)
                content_value = str(save_path)
            else:
                content_value = request.form.get("content", "").strip()
                if not content_value:
                    flash("Image file (or a valid image path) is required for image posts.")
                    return redirect(url_for("index"))
        else:
            flash("Invalid post type.")
            return redirect(url_for("index"))

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""INSERT INTO scheduled_posts
            (subreddit, title, post_type, content, post_time, posted, last_error, created_at, flair_id, flair_text)
            VALUES (?, ?, ?, ?, ?, 0, NULL, ?, ?, ?)""",
            (subreddit, title, post_type, content_value, post_time_store, 
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), flair_id, flair_text)
        )
        conn.commit()
        conn.close()
        flash("Scheduled!")
        return redirect(url_for("index"))

    # GET
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, subreddit, title, post_type, content, post_time, posted, last_error, flair_id, flair_text FROM scheduled_posts ORDER BY post_time")
    posts = c.fetchall()
    conn.close()
    
    # Get flairs for the first subreddit in the list (if any) for initial display
    initial_flairs = []
    if posts:
        initial_flairs, _ = get_subreddit_flairs(posts[0][1])
    
    return render_template("index.html", posts=posts, app_tz=str(APP_TZ), flairs=initial_flairs)

@app.route("/get_flairs/<subreddit>")
def get_flairs(subreddit):
    flairs, error = get_subreddit_flairs(subreddit)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"flairs": flairs})

@app.post("/delete/<int:post_id>")
def delete_post(post_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM scheduled_posts WHERE id=?", (post_id,))
    conn.commit()
    conn.close()
    flash("Deleted.")
    return redirect(url_for("index"))

if __name__ == "__main__":
    # Bind to all interfaces for Docker
    app.run(host="0.0.0.0", port=5000, debug=False)