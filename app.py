import os
import io
import base64
import secrets
import sqlite3
import smtplib
from datetime import datetime
from email.message import EmailMessage

from flask import (
    Flask, request, jsonify, g, redirect, url_for, make_response, render_template_string
)
from dotenv import load_dotenv
import qrcode

load_dotenv()

# ------------------ ENV ------------------
APP_SECRET = os.getenv("FLASK_SECRET", "dev_secret")
ADMIN_PIN = os.getenv("ADMIN_PIN", "123456")
EMAIL_HOST = os.getenv("EMAIL_HOST")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
SENDER_NAME = os.getenv("SENDER_NAME", "QR System")
BRAND_NAME = os.getenv("BRAND_NAME", "One-Time QR")

DB_PATH = os.path.join(os.path.dirname(__file__), "qr_system.db")

# ------------------ DB ------------------
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            used_at TEXT
        );
    """)
    db.commit()

# ------------------ QR ------------------
def gen_token():
    return secrets.token_hex(6).upper()

def make_qr_png_bytes(data: str) -> bytes:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ------------------ EMAIL (BREVO SMTP) ------------------
def send_qr_email(to_email: str, token: str, png_bytes: bytes):
    if not EMAIL_USER or not EMAIL_PASS:
        raise RuntimeError("EMAIL_USER/EMAIL_PASS missing. Set correctly in Render Environment.")

    msg = EmailMessage()
    msg["Subject"] = f"{BRAND_NAME}: Your One-Time QR"
    msg["From"] = f"{SENDER_NAME} <{EMAIL_USER}>"
    msg["To"] = to_email

    html = f"""
    <h2>{BRAND_NAME}</h2>
    <p>Your one-time QR code is attached.</p>
    <p><b>Token:</b> {token}</p>
    """
    msg.add_alternative(html, subtype="html")
    msg.add_attachment(png_bytes, maintype="image", subtype="png", filename=f"{token}.png")

    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as smtp:
        smtp.starttls()
        smtp.login(EMAIL_USER, EMAIL_PASS)
        smtp.send_message(msg)

# ------------------ FLASK APP ------------------
app = Flask(__name__)
app.secret_key = APP_SECRET
app.teardown_appcontext(close_db)

@app.before_request
def _ensure_db():
    init_db()

ADMIN_HTML = r"""
<h1>{{ brand }} — Issue QR</h1>
<form method="post" action="{{ url_for('issue') }}">
  <input type="email" name="email" placeholder="client@example.com" required>
  <button type="submit">Generate & Send</button>
</form>

{% if last %}
<h3>Sent to: {{ last.email }}</h3>
<p>Token: {{ last.token }}</p>
<img src="data:image/png;base64,{{ last.qr_b64 }}">
{% endif %}

<hr>
<h3>Scanner (open on phone)</h3>
<a href="{{ scanner_url }}">{{ scanner_url }}</a>
<p>PIN: {{ pin }}</p>
"""

@app.get("/admin")
def admin():
    host = request.host_url.rstrip('/')
    return render_template_string(ADMIN_HTML, brand=BRAND_NAME, last=None, scanner_url=f"{host}/scanner", pin=ADMIN_PIN)

@app.post("/issue")
def issue():
    email = request.form.get('email', '').strip()
    token = gen_token()
    png = make_qr_png_bytes(token)

    db = get_db()
    db.execute("INSERT INTO vouchers (email, token, used, created_at) VALUES (?, ?, 0, ?)",
               (email, token, datetime.utcnow().isoformat()))
    db.commit()

    send_qr_email(email, token, png)

    host = request.host_url.rstrip('/')
    last = {'email': email, 'token': token, 'qr_b64': base64.b64encode(png).decode('ascii')}
    return render_template_string(ADMIN_HTML, brand=BRAND_NAME, last=last, scanner_url=f"{host}/scanner", pin=ADMIN_PIN)

# ------------------ SCANNER PAGE ------------------
SCANNER_HTML = r"""
<h1>{{ brand }} Scanner</h1>
<p>Use your phone camera to scan QR codes.</p>
"""

@app.get("/scanner")
def scanner():
    return render_template_string(SCANNER_HTML, brand=BRAND_NAME)

# ------------------ VERIFY API ------------------
@app.post("/api/verify")
def api_verify():
    pin = request.headers.get('X-Admin-Pin', '')
    if pin != ADMIN_PIN:
        return jsonify({'status': 'unauthorized'})
    data = request.get_json(silent=True) or {}
    token = data.get('token', '').strip().upper()
    db = get_db()
    row = db.execute("SELECT id, email, used FROM vouchers WHERE token=?", (token,)).fetchone()
    if not row:
        return jsonify({'status': 'invalid'})
    if row['used']:
        return jsonify({'status': 'used'})
    db.execute("UPDATE vouchers SET used=1, used_at=? WHERE id=?", (datetime.utcnow().isoformat(), row['id']))
    db.commit()
    return jsonify({'status': 'valid', 'email': row['email']})

@app.get("/")
def root():
    return redirect(url_for('admin'))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
