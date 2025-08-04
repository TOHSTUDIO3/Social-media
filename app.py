import sqlite3
import os
from flask import (
    Flask, g, render_template, request, redirect,
    session, url_for, flash, jsonify, send_from_directory
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timezone

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

# --- إعدادات ---
DB_PATH = "db.sqlite3"
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp4', 'mov', 'avi'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

def init_db():
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        # جدول المستخدمين (بدون تغيير)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        # جدول المنشورات (بدون تغيير)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT,
            media_path TEXT,
            media_type TEXT,
            created_at TEXT NOT NULL,
            likes INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """)
        # جدول اللايكات (بدون تغيير)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, post_id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(post_id) REFERENCES posts(id)
        )
        """)
        # --- جدول جديد للتعليقات ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(post_id) REFERENCES posts(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """)
        db.commit()

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_database", None)
    if db:
        db.close()

def current_user():
    if "user_id" in session:
        db = get_db()
        u = db.execute("SELECT id, username FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        return u
    return None

@app.route("/")
def home():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    
    db = get_db()
    
    # جلب المنشورات مع التعليقات الخاصة بكل منشور
    posts_query = db.execute("""
        SELECT posts.*, users.username FROM posts
        JOIN users ON posts.user_id = users.id
        ORDER BY posts.created_at DESC
    """).fetchall()

    posts = []
    for post in posts_query:
        post_dict = dict(post)
        comments = db.execute("""
            SELECT comments.*, users.username FROM comments
            JOIN users ON comments.user_id = users.id
            WHERE comments.post_id = ?
            ORDER BY comments.created_at ASC
        """, (post['id'],)).fetchall()
        post_dict['comments'] = comments
        posts.append(post_dict)

    user_likes = {r["post_id"] for r in db.execute("SELECT post_id FROM likes WHERE user_id = ?", (user["id"],)).fetchall()}

    return render_template("home.html", user=user, posts=posts, user_likes=user_likes)

# --- (جديد) صفحة الملف الشخصي للمستخدم ---
@app.route("/profile/<username>")
def profile(username):
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    db = get_db()
    profile_user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not profile_user:
        flash("المستخدم غير موجود")
        return redirect(url_for("home"))

    posts = db.execute("""
        SELECT * FROM posts WHERE user_id = ? ORDER BY created_at DESC
    """, (profile_user["id"],)).fetchall()

    return render_template("profile.html", user=user, profile_user=profile_user, posts=posts)

# --- (جديد) مسار لحذف المنشور ---
@app.route("/post/delete/<int:post_id>", methods=["POST"])
def delete_post(post_id):
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    db = get_db()
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()

    if not post:
        flash("المنشور غير موجود")
        return redirect(request.referrer or url_for('home'))

    if post["user_id"] != user["id"]:
        flash("ليس لديك صلاحية لحذف هذا المنشور")
        return redirect(request.referrer or url_for('home'))

    # حذف الملف المرفق إذا كان موجوداً
    if post["media_path"]:
        try:
            os.remove(os.path.join(app.config['UPLOAD_FOLDER'], post["media_path"]))
        except OSError as e:
            print(f"Error deleting file {post['media_path']}: {e}")

    # حذف اللايكات والتعليقات المتعلقة بالمنشور أولاً
    db.execute("DELETE FROM likes WHERE post_id = ?", (post_id,))
    db.execute("DELETE FROM comments WHERE post_id = ?", (post_id,))
    # ثم حذف المنشور نفسه
    db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    db.commit()

    flash("تم حذف المنشور بنجاح")
    return redirect(url_for("profile", username=user["username"]))

# --- (جديد) مسار لإضافة تعليق ---
@app.route("/comment/add/<int:post_id>", methods=["POST"])
def add_comment(post_id):
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    content = request.form.get("content", "").strip()
    if not content:
        flash("لا يمكن إضافة تعليق فارغ")
        return redirect(request.referrer or url_for('home'))

    db = get_db()
    db.execute(
        "INSERT INTO comments (post_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
        (post_id, user["id"], content, datetime.now(timezone.utc).isoformat())
    )
    db.commit()

    return redirect(request.referrer or url_for('home'))


# --- باقي الدوال تبقى كما هي مع بعض التعديلات الطفيفة ---
# ... (register, login, logout, post, like, uploaded_file) ...
# (الكود هنا لم يتغير، يمكنك نسخه من ردودي السابقة أو من الأسفل)

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route("/post", methods=["POST"])
def post():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    content = request.form.get("content", "").strip()
    file = request.files.get('media_file')
    if not content and not file:
        flash("يجب إضافة نص أو رفع ملف لإنشاء منشور!")
        return redirect(url_for("home"))
    media_path, media_type = None, None
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        unique_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
        media_path = unique_filename
        media_type = 'image' if filename.rsplit('.', 1)[1].lower() in {'png', 'jpg', 'jpeg', 'gif'} else 'video'
    db = get_db()
    db.execute(
        "INSERT INTO posts (user_id, content, media_path, media_type, created_at) VALUES (?, ?, ?, ?, ?)",
        (user["id"], content, media_path, media_type, datetime.now(timezone.utc).isoformat())
    )
    db.commit()
    return redirect(url_for("home"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash("كل الحقول لازمة")
            return redirect(url_for("register"))
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "INSERT INTO users (username, password, created_at) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), datetime.now(timezone.utc).isoformat())
            )
            session["user_id"] = cur.lastrowid
            db.commit()
            return redirect(url_for("home"))
        except sqlite3.IntegrityError:
            flash("الاسم مستخدم من قبل")
            return redirect(url_for("register"))
    return render_template("register.html", user=current_user())

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            return redirect(url_for("home"))
        else:
            flash("اسم المستخدم أو كلمة المرور خطأ")
    return render_template("login.html", user=current_user())

@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("login"))

@app.route("/like/<int:post_id>", methods=["POST"])
def like(post_id):
    user = current_user()
    if not user:
        return jsonify({"error": "لازم تسجل دخول"}), 401
    db = get_db()
    existing = db.execute("SELECT * FROM likes WHERE user_id = ? AND post_id = ?", (user["id"], post_id)).fetchone()
    if existing:
        db.execute("DELETE FROM likes WHERE id = ?", (existing["id"],))
        db.execute("UPDATE posts SET likes = likes - 1 WHERE id = ?", (post_id,))
        liked_now = False
    else:
        db.execute(
            "INSERT OR IGNORE INTO likes (user_id, post_id, created_at) VALUES (?, ?, ?)",
            (user["id"], post_id, datetime.now(timezone.utc).isoformat())
        )
        db.execute("UPDATE posts SET likes = likes + 1 WHERE id = ?", (post_id,))
        liked_now = True
    db.commit()
    new_count = db.execute("SELECT likes FROM posts WHERE id = ?", (post_id,)).fetchone()["likes"]
    return jsonify({"likes": new_count, "liked": liked_now})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", debug=True)
