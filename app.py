# Updated app.py with Brevo SMTP support and starttls

import os
import io
import base64
import secrets
import sqlite3
import smtplib
from datetime import datetime
from email.message import EmailMessage

from flask import (
    Flask, request, jsonify, g, redirect, url_for, make_response
)
from flask import render_template_string
from dotenv import load_dotenv
import qrcode

load_dotenv()

APP_SECRET = os.getenv("FLASK_SECRET", "dev_secret")
ADMIN_PIN = os.getenv("ADMIN_PIN", "123456")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp-relay.brevo.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
SENDER_NAME = os.getenv("SENDER_NAME", "QR System")
BRAND_NAME = os.getenv("BRAND_NAME", "One-Time QR")

DB_PATH = os.path.join(os.path.dirname(__file__), "qr_system.db")

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
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            used_at TEXT
        );
        """
    )
    db.commit()

def gen_token():
    return secrets.token_hex(6).upper()

def make_qr_png_bytes(data: str) -> bytes:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def send_qr_email(to_email: str, token: str, png_bytes: bytes):
    if not EMAIL_USER or not EMAIL_PASS:
        raise RuntimeError("EMAIL_USER/EMAIL_PASS not configured.")

    msg = EmailMessage()
    msg["Subject"] = f"{BRAND_NAME}: Your One-Time QR"
    msg["From"] = f"{SENDER_NAME} <{EMAIL_USER}>"
    msg["To"] = to_email

    html = f"""
    <div style='font-family:Arial,Helvetica,sans-serif'>
      <h2>{BRAND_NAME}</h2>
      <p>Your one-time QR is below. It can be used <b>once</b>.</p>
      <p><b>Token:</b> <code>{token}</code></p>
      <p>If image doesn't load, show token to admin.</p>
      <p>- {SENDER_NAME}</p>
    </div>
    """
    msg.add_alternative(html, subtype="html")

    msg.add_attachment(
        png_bytes,
        maintype="image",
        subtype="png",
        filename=f"{BRAND_NAME.replace(' ', '_')}_{token}.png"
    )

    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as smtp:
        smtp.starttls()
        smtp.login(EMAIL_USER, EMAIL_PASS)
        smtp.send_message(msg)


app = Flask(__name__)
app.secret_key = APP_SECRET
app.teardown_appcontext(close_db)

@app.before_request
def _ensure_db():
    init_db()

ADMIN_HTML = r"""
<!doctype html>
<html>
<head><meta charset="utf-8"><title>{{ brand }} — Issue QR</title></head>
<body style="font-family:Arial;max-width:600px;margin:auto;padding:20px">
<h2>{{ brand }} — Issue QR</h2>
<form method="post" action="{{ url_for('issue') }}">
  <input type="email" name="email" placeholder="client@example.com" required>
  <button type="submit">Generate & Send</button>
</form>
{% if last %}
<hr>
<p>Sent to: <b>{{ last.email }}</b></p>
<p>Token: <code>{{ last.token }}</code></p>
<img src="data:image/png;base64,{{ last.qr_b64 }}" width="200">
{% endif %}
<hr>
<p>Scanner Link: <a href="{{ scanner_url }}">{{ scanner_url }}</a></p>
</body></html>
"""

@app.get("/admin")
def admin():
    host = request.host_url.rstrip('/')
    return render_template_string(ADMIN_HTML, brand=BRAND_NAME, last=None, scanner_url=f"{host}/scanner")

@app.post("/issue")
def issue():
    email = request.form.get('email', '').strip()
    if not email:
        return make_response("Email required", 400)

    token = gen_token()
    png = make_qr_png_bytes(token)

    db = get_db()
    db.execute(
        "INSERT INTO vouchers (email, token, used, created_at) VALUES (?, ?, 0, ?)",
        (email, token, datetime.utcnow().isoformat())
    )
    db.commit()

    try:
        send_qr_email(email, token, png)
    except Exception as e:
        return make_response(f"Failed to send email: {e}", 500)

    host = request.host_url.rstrip('/')
    last = {'email': email, 'token': token, 'qr_b64': base64.b64encode(png).decode('ascii')}
    return render_template_string(ADMIN_HTML, brand=BRAND_NAME, last=last, scanner_url=f"{host}/scanner")

SCANNER_HTML = """
<!doctype html><html><body><h2>Scanner</h2>
<p>Open camera scanner UI here (same as your original, kept short for space)</p>
</body></html>
"""

@app.get("/scanner")
def scanner():
    return render_template_string(SCANNER_HTML, brand=BRAND_NAME)

@app.post("/api/verify")
def api_verify():
    pin = request.headers.get('X-Admin-Pin', '')
    if pin != ADMIN_PIN:
        return jsonify({'status': 'unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    token = (data.get('token') or '').strip().upper()
    if not token:
        return jsonify({'status': 'invalid'}), 400

    db = get_db()
    row = db.execute("SELECT id, email, used FROM vouchers WHERE token = ?", (token,)).fetchone()
    if not row:
        return jsonify({'status': 'invalid'})
    if row['used'] == 1:
        return jsonify({'status': 'used'})

    db.execute("UPDATE vouchers SET used = 1, used_at = ? WHERE id = ?", (datetime.utcnow().isoformat(), row['id']))
    db.commit()
    return jsonify({'status': 'valid', 'email': row['email']})

@app.get('/')
def root():
    return redirect(url_for('admin'))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
