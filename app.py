import os
import sqlite3
from flask import Flask, request, render_template, redirect, url_for, flash, jsonify, send_file
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from pathlib import Path
from zoneinfo import ZoneInfo
import praw
from urllib.parse import urlparse
import requests
from io import BytesIO

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

# Uploads
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/data/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024  # MB

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# Timezone for interpreting user input times
APP_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "UTC"))
# Timezone for displaying scheduled posts
DISPLAY_TZ = ZoneInfo("America/Chicago")  # Central Time Zone

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
        flair_id TEXT DEFAULT NULL,
        flair_text TEXT DEFAULT NULL,
        destination_type TEXT DEFAULT 'subreddit'
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
    try:
        c.execute("ALTER TABLE scheduled_posts ADD COLUMN destination_type TEXT")
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

def post_to_reddit(subreddit, title, post_type, content, flair_id=None, destination_type="subreddit"):
    try:
        if destination_type == "profile":
            # Post to user's profile
            if post_type == "link":
                submission = reddit.subreddit("u_" + reddit.user.me().name).submit(title=title, url=content)
            elif post_type == "text":
                submission = reddit.subreddit("u_" + reddit.user.me().name).submit(title=title, selftext=content)
            elif post_type == "image":
                submission = reddit.subreddit("u_" + reddit.user.me().name).submit_image(title=title, image_path=content)
            else:
                raise ValueError(f"Unknown post_type: {post_type}")
        else:
            # Post to subreddit
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
    c.execute("SELECT id, subreddit, title, post_type, content, flair_id, destination_type FROM scheduled_posts WHERE post_time<=? AND posted=0", (now_utc,))
    rows = c.fetchall()
    for row in rows:
        post_id, subreddit, title, post_type, content, flair_id, destination_type = row
        err = post_to_reddit(subreddit, title, post_type, content, flair_id, destination_type)
        if err is None:
            c.execute("UPDATE scheduled_posts SET posted=1, last_error=NULL WHERE id=?", (post_id,))
        else:
            c.execute("UPDATE scheduled_posts SET last_error=? WHERE id=?", (err, post_id))
    conn.commit()
    conn.close()

def convert_utc_to_central(utc_time_str):
    """Convert UTC time string to Central Time string"""
    try:
        utc_dt = datetime.strptime(utc_time_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        central_dt = utc_dt.astimezone(DISPLAY_TZ)
        return central_dt.strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return utc_time_str  # Return original if conversion fails

def is_valid_image_path(path):
    """Check if the path is a valid image file that exists"""
    try:
        path_obj = Path(path)
        return path_obj.exists() and path_obj.is_file() and path_obj.suffix.lower()[1:] in ALLOWED_EXTENSIONS
    except:
        return False

def get_image_preview_url(content, post_type):
    """Generate appropriate preview URL or path for different post types"""
    if post_type == "image":
        if content.startswith(('http://', 'https://')):
            # External image URL
            return content
        elif is_valid_image_path(content):
            # Local file path that exists - use the filename only for security
            return f"/image-preview/{os.path.basename(content)}"
        else:
            # Invalid or non-existent image
            return None
    elif post_type == "link":
        # For link posts, return the URL for display
        return content
    return None

@app.route("/image-preview/<filename>")
def serve_image_preview(filename):
    """Serve image files for preview"""
    try:
        # Security check to prevent directory traversal
        safe_filename = secure_filename(filename)
        image_path = UPLOAD_DIR / safe_filename
        
        if image_path.exists() and image_path.is_file():
            return send_file(image_path)
        else:
            # Try to find the file by matching the end of the filename
            # (since we store with timestamp prefix)
            for file in UPLOAD_DIR.glob(f"*{safe_filename}"):
                if file.exists() and file.is_file():
                    return send_file(file)
            return "Image not found", 404
    except Exception as e:
        app.logger.error(f"Error serving image: {e}")
        return "Error serving image", 500

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        subreddit = request.form.get("subreddit", "").strip().lstrip("r/")
        title = request.form.get("title", "").strip()
        post_type = request.form.get("post_type")
        post_time_str = request.form.get("post_time", "").strip()
        flair_id = request.form.get("flair_id", "").strip()
        flair_text = request.form.get("flair_text", "").strip()
        destination_type = request.form.get("destination_type", "subreddit")

        if not title or not post_type or not post_time_str:
            flash("Title, post type, and time are required.")
            return redirect(url_for("index"))

        # Validate subreddit if posting to subreddit
        if destination_type == "subreddit" and not subreddit:
            flash("Subreddit is required when posting to a subreddit.")
            return redirect(url_for("index"))

        # Parse datetime from the datetime-local input
        try:
            # Convert from datetime-local format to datetime object
            local_naive = datetime.fromisoformat(post_time_str.replace("T", " "))
            local_dt = local_naive.replace(tzinfo=APP_TZ)
            post_time_utc = local_dt.astimezone(timezone.utc)
            post_time_store = post_time_utc.strftime("%Y-%m-%d %H:%M")
        except Exception:
            flash("Invalid date/time format. Please use the datetime picker.")
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
                    flash("Only PNG/JPG/JPEG/GIF images are allowed.")
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
            (subreddit, title, post_type, content, post_time, posted, last_error, created_at, flair_id, flair_text, destination_type)
            VALUES (?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?)""",
            (subreddit, title, post_type, content_value, post_time_store, 
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), flair_id, flair_text, destination_type)
        )
        conn.commit()
        conn.close()
        flash("Scheduled!")
        return redirect(url_for("index"))

    # GET
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, subreddit, title, post_type, content, post_time, posted, last_error, flair_id, flair_text, destination_type FROM scheduled_posts ORDER BY post_time")
    posts = c.fetchall()
    conn.close()
    
    # Convert UTC times to Central Time for display and add preview info
    posts_with_ct = []
    for post in posts:
        post_list = list(post)
        post_list[5] = convert_utc_to_central(post[5])  # Convert post_time to CT
        
        # Add preview URL information
        preview_url = get_image_preview_url(post[4], post[3])
        post_list.append(preview_url)  # Add preview URL as additional element
        
        posts_with_ct.append(tuple(post_list))
    
    # Get flairs for the first subreddit in the list (if any) for initial display
    initial_flairs = []
    if posts_with_ct and posts_with_ct[0][1]:  # Only if there's a subreddit
        initial_flairs, _ = get_subreddit_flairs(posts_with_ct[0][1])
    
    return render_template("index.html", posts=posts_with_ct, app_tz=str(APP_TZ), flairs=initial_flairs, display_tz=str(DISPLAY_TZ))

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

scheduler = BackgroundScheduler()
scheduler.add_job(func=check_scheduled_posts, trigger="interval", minutes=1)
scheduler.start()

if __name__ == "__main__":
    # Bind to all interfaces for Docker
    app.run(host="0.0.0.0", port=5000, debug=False)