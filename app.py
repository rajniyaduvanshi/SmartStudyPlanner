
from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "smart-study-planner-secret-key"

DATABASE = Path(__file__).resolve().parent / "study.db"


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    # Create users table for a new database
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password_hash TEXT,
            email TEXT,
            password TEXT
        )
    """)

    # Create tasks table for a new database
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            subject TEXT,
            due_date TEXT,
            deadline TEXT,
            completed INTEGER DEFAULT 0
        )
    """)

    # Add missing columns to an existing tasks table
    task_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
    }

    task_definitions = {
        "user_id": "INTEGER",
        "title": "TEXT",
        "subject": "TEXT",
        "due_date": "TEXT",
        "deadline": "TEXT",
        "completed": "INTEGER NOT NULL DEFAULT 0"
    }

    for name, definition in task_definitions.items():
        if name not in task_columns:
            conn.execute(
                f'ALTER TABLE tasks ADD COLUMN "{name}" {definition}'
            )

    # Add missing user columns without deleting existing records
    user_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }

    user_definitions = {
        "username": "TEXT",
        "password_hash": "TEXT",
        "email": "TEXT",
        "password": "TEXT"
    }

    for name, definition in user_definitions.items():
        if name not in user_columns:
            conn.execute(
                f'ALTER TABLE users ADD COLUMN "{name}" {definition}'
            )

    # Migrate old plain-text passwords to password hashes
    users = conn.execute(
        "SELECT id, password, password_hash FROM users"
    ).fetchall()

    for user in users:
        old_password = user["password"]
        old_hash = user["password_hash"]

        if old_password and not old_hash:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(old_password), user["id"])
            )

    # Copy older task dates into the new date column where possible
    conn.execute("""
        UPDATE tasks
        SET due_date = deadline
        WHERE (due_date IS NULL OR due_date = '')
          AND deadline IS NOT NULL
          AND deadline != ''
    """)

    conn.commit()
    conn.close()


@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        email = request.form.get("email", "").strip()

        if not username or not password:
            flash("Please enter username and password.")
            return redirect(url_for("register"))

        conn = get_db()

        try:
            password_hash = generate_password_hash(password)

            conn.execute("""
                INSERT INTO users (username, password_hash, email, password)
                VALUES (?, ?, ?, ?)
            """, (username, password_hash, email, None))

            conn.commit()
            flash("Registration successful. Please log in.")
            return redirect(url_for("login"))

        except sqlite3.IntegrityError:
            flash("This username already exists. Please log in.")

        finally:
            conn.close()

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,)
        ).fetchone()
        conn.close()

        if user:
            stored_hash = user["password_hash"]
            valid_password = False

            if stored_hash:
                try:
                    valid_password = check_password_hash(
                        stored_hash, password
                    )
                except (ValueError, TypeError):
                    valid_password = False

            # Allow old plain-text passwords to be upgraded at login
            if not valid_password and user["password"]:
                if user["password"] == password:
                    valid_password = True

                    conn = get_db()
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (generate_password_hash(password), user["id"])
                    )
                    conn.commit()
                    conn.close()

            if valid_password:
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                return redirect(url_for("dashboard"))

        flash("Invalid username or password.")

    return render_template("login.html")


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()

    tasks = conn.execute("""
        SELECT id, user_id, title, subject,
               due_date, deadline, completed
        FROM tasks
        WHERE user_id = ?
        ORDER BY completed ASC, id DESC
    """, (session["user_id"],)).fetchall()

    conn.close()

    return render_template("dashboard.html", tasks=tasks)


@app.route("/add-task", methods=["POST"])
def add_task():
    if "user_id" not in session:
        return redirect(url_for("login"))

    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip()

    due_date = (
        request.form.get("due_date", "").strip()
        or request.form.get("deadline", "").strip()
        or None
    )

    if not title or not subject:
        flash("Please enter both task name and subject.")
        return redirect(url_for("dashboard"))

    conn = get_db()

    # Read the real schema so older required columns are also filled
    table_info = conn.execute("PRAGMA table_info(tasks)").fetchall()
    columns = {column["name"] for column in table_info}

    values = {
        "user_id": session["user_id"],
        "title": title,
        "subject": subject,
        "due_date": due_date,
        "deadline": due_date,
        "completed": 0
    }

    insert_data = {}

    for column in table_info:
        name = column["name"]

        if name in values:
            insert_data[name] = values[name]
        elif column["notnull"] and column["dflt_value"] is None:
            # Do not explicitly insert an autogenerated integer primary key
            if column["pk"] and "INT" in column["type"].upper():
                continue
            insert_data[name] = ""

    # Ensure the task belongs to the current user
    if "user_id" in columns:
        insert_data["user_id"] = session["user_id"]

    names = list(insert_data.keys())
    placeholders = ", ".join("?" for _ in names)
    column_names = ", ".join(f'"{name}"' for name in names)

    sql = (
        f"INSERT INTO tasks ({column_names}) "
        f"VALUES ({placeholders})"
    )

    try:
        conn.execute(sql, [insert_data[name] for name in names])
        conn.commit()
        flash("Study task added successfully.")

    except sqlite3.Error as error:
        conn.rollback()
        print("Task save error:", error)
        flash("Task save nahi hua. Terminal mein error check karo.")

    finally:
        conn.close()

    return redirect(url_for("dashboard"))


@app.route("/complete/<int:task_id>", methods=["POST"])
def complete_task(task_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    conn.execute("""
        UPDATE tasks
        SET completed = CASE
            WHEN completed = 0 THEN 1
            ELSE 0
        END
        WHERE id = ? AND user_id = ?
    """, (task_id, session["user_id"]))

    conn.commit()
    conn.close()

    return redirect(url_for("dashboard"))


@app.route("/delete/<int:task_id>", methods=["POST"])
def delete_task(task_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    conn.execute(
        "DELETE FROM tasks WHERE id = ? AND user_id = ?",
        (task_id, session["user_id"])
    )

    conn.commit()
    conn.close()

    flash("Task deleted successfully.")
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# Create or repair database tables on startup
init_db()


if __name__ == "__main__":
    app.run(debug=True)
