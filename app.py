"""
One-Time QR System — Flask + SQLite + Gmail (App Password) + Phone Camera Scanner
---------------------------------------------------------------------------------
Single-file app you can run locally. Generates unique, one-time-use QR codes,
sends them to clients by email (Gmail SMTP), and provides an admin scanner
webpage that uses your phone camera to validate and burn (mark used) the code.

Quick Start
-----------
1) Python 3.10+ recommended
2) pip install -r requirements.txt  (see REQUIREMENTS below)
3) Create a .env file (see ENV below)
4) python app.py
5) Open http://127.0.0.1:5000/admin on your laptop to issue QR codes
6) Open http://<your-laptop-LAN-IP>:5000/scanner on your phone for scanning

Security notes
--------------
- Verification API requires an ADMIN PIN (set ADMIN_PIN in .env). The scanner
  page prompts for it and sends it with each verify request.
- QR content is only the random token (no PII in the code itself).
- This is an MVP. For production, add HTTPS, auth, audit logs, and stronger ACLs.

REQUIREMENTS (pip)
------------------
Flask==3.0.3
python-dotenv==1.0.1
qrcode==7.4.2
pillow==10.4.0

ENV (.env example)
------------------
FLASK_SECRET=change_me
ADMIN_PIN=123456
SMTP_USER=yourgmail@gmail.com
SMTP_PASS=your_app_password   # Requires Gmail 2FA + App Password
SENDER_NAME=Your Org Name

# Optional branding
BRAND_NAME=My Event Gate

"""

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
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
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
    # 12 hex chars: 48 bits entropy, uppercase for easy reading
    return secrets.token_hex(6).upper()


def make_qr_png_bytes(data: str) -> bytes:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def send_qr_email(to_email: str, token: str, png_bytes: bytes):
    if not SMTP_USER or not SMTP_PASS:
        raise RuntimeError("SMTP_USER/SMTP_PASS not configured. Set in .env")

    msg = EmailMessage()
    msg["Subject"] = f"{BRAND_NAME}: Your One-Time QR"
    msg["From"] = f"{SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = to_email

    html = f"""
    <div style='font-family:Arial,Helvetica,sans-serif'>
      <h2>{BRAND_NAME}</h2>
      <p>Hello,</p>
      <p>Your one-time QR is below. Please keep it safe. It can be used <b>once</b>.</p>
      <p><b>Token:</b> <code>{token}</code></p>
      <p>If the image doesn't load, show this token to the admin.</p>
      <p>— {SENDER_NAME}</p>
    </div>
    """
    msg.add_alternative(html, subtype="html")

    msg.add_attachment(
        png_bytes,
        maintype="image",
        subtype="png",
        filename=f"{BRAND_NAME.replace(' ','_')}_{token}.png"
    )

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


app = Flask(__name__)
app.secret_key = APP_SECRET
app.teardown_appcontext(close_db)

@app.before_request
def _ensure_db():
    init_db()


# ---------------------- Admin Issue Page ----------------------
ADMIN_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ brand }} — Issue QR</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;padding:24px;max-width:800px;margin:auto}
    .card{border:1px solid #ddd;border-radius:12px;padding:16px;margin:12px 0}
    .row{display:flex;gap:12px;flex-wrap:wrap}
    label{display:block;margin-bottom:6px;font-weight:600}
    input,button{padding:10px;border-radius:8px;border:1px solid #ccc;font-size:16px}
    button{cursor:pointer}
    .success{background:#e8f8ef;border:1px solid #a3e4c9}
    .error{background:#fdeaea;border:1px solid #f5b7b1}
    img{max-width:220px;display:block}
    code{background:#f6f8fa;padding:3px 6px;border-radius:6px}
  </style>
</head>
<body>
  <h1>{{ brand }} — Issue One-Time QR</h1>
  <div class="card">
    <form method="post" action="{{ url_for('issue') }}">
      <label>Email address (client)</label>
      <div class="row">
        <input type="email" name="email" placeholder="client@example.com" required style="flex:1">
        <button type="submit">Generate & Send</button>
      </div>
      <p style="font-size:14px;color:#666">This will email a unique, one-time QR to the client.</p>
    </form>
  </div>

  {% if last %}
  <div class="card success">
    <h3>Sent!</h3>
    <p><b>To:</b> {{ last.email }}<br>
       <b>Token:</b> <code>{{ last.token }}</code></p>
    <p>QR preview:</p>
    <img src="data:image/png;base64,{{ last.qr_b64 }}" alt="QR">
  </div>
  {% endif %}

  <div class="card">
    <h3>Scanner</h3>
    <p>Open this on your phone: <a href="{{ scanner_url }}">{{ scanner_url }}</a></p>
    <p>Admin PIN required: <code>set in .env (ADMIN_PIN)</code></p>
  </div>
</body>
</html>
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

    # Save to DB
    db = get_db()
    db.execute(
        "INSERT INTO vouchers (email, token, used, created_at) VALUES (?, ?, 0, ?)",
        (email, token, datetime.utcnow().isoformat())
    )
    db.commit()

    # Send email
    try:
        send_qr_email(email, token, png)
    except Exception as e:
        # On email failure, rollback the voucher? Here we keep it but warn admin.
        return make_response(f"Failed to send email: {e}", 500)

    host = request.host_url.rstrip('/')
    last = {
        'email': email,
        'token': token,
        'qr_b64': base64.b64encode(png).decode('ascii')
    }
    return render_template_string(ADMIN_HTML, brand=BRAND_NAME, last=last, scanner_url=f"{host}/scanner")


# ---------------------- Scanner Page ----------------------
SCANNER_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ brand }} — Scanner</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;margin:0}
    header{padding:16px;border-bottom:1px solid #eee;display:flex;gap:12px;align-items:center}
    main{padding:16px}
    .row{display:flex;gap:12px;align-items:center}
    input,button{padding:10px;border-radius:8px;border:1px solid #ccc;font-size:16px}
    button{cursor:pointer}
    video{width:100%;max-width:520px;border-radius:12px;border:1px solid #ddd}
    canvas{display:none}
    .status{margin-top:12px;padding:12px;border-radius:12px;font-size:18px}
    .ok{background:#e8f8ef;border:1px solid #a3e4c9}
    .bad{background:#fdeaea;border:1px solid #f5b7b1}
    code{background:#f6f8fa;padding:3px 6px;border-radius:6px}
  </style>
</head>
<body>
  <header>
    <h2 style="margin:0">{{ brand }} — Scanner</h2>
  </header>
  <main>
    <div class="row">
      <label for="pin">Admin PIN:</label>
      <input id="pin" type="password" placeholder="Enter PIN" style="flex:1">
      <button id="savePin">Save</button>
    </div>

    <p style="color:#666;font-size:14px">PIN is stored locally on your device until you close the page.</p>

    <video id="preview" playsinline></video>
    <canvas id="canvas"></canvas>

    <div id="status" class="status">Ready</div>
  </main>

  <!-- jsQR from CDN -->
  <script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.js"></script>
  <script>
    const video = document.getElementById('preview');
    const canvas = document.getElementById('canvas');
    const ctx = canvas.getContext('2d');
    const statusBox = document.getElementById('status');
    const pinInput = document.getElementById('pin');
    const savePinBtn = document.getElementById('savePin');

    savePinBtn.onclick = () => {
      localStorage.setItem('admin_pin', pinInput.value.trim());
      status('PIN saved. Start scanning...');
    };

    function status(msg, ok=false){
      statusBox.textContent = msg;
      statusBox.className = 'status ' + (ok ? 'ok' : 'bad');
      if(!ok && msg === 'Ready'){ statusBox.className = 'status'; }
    }

    async function start(){
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
      video.srcObject = stream;
      await video.play();
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      scanLoop();
    }

    let lastToken = '';
    let cooling = false;

    async function scanLoop(){
      if(video.readyState === video.HAVE_ENOUGH_DATA){
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const code = jsQR(img.data, img.width, img.height);
        if(code){
          const token = (code.data || '').trim();
          if(token && token !== lastToken && !cooling){
            lastToken = token;
            cooling = true;
            verify(token).finally(()=>{
              setTimeout(()=>{ cooling = false; }, 1500);
            });
          }
        }
      }
      requestAnimationFrame(scanLoop);
    }

    async function verify(token){
      const pin = localStorage.getItem('admin_pin') || pinInput.value.trim();
      if(!pin){ status('Enter PIN first'); return; }
      status('Verifying...');
      try{
        const res = await fetch('/api/verify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Admin-Pin': pin },
          body: JSON.stringify({ token })
        });
        const data = await res.json();
        if(data.status === 'valid'){
          status(`✅ VALID — ${data.email} (marked used)`, true);
        } else if(data.status === 'used'){
          status('⚠️ Already used');
        } else if(data.status === 'invalid'){
          status('❌ Invalid code');
        } else if(data.status === 'unauthorized'){
          status('❌ Wrong PIN');
        } else {
          status('❌ Error');
        }
      } catch(e){
        status('❌ Network error');
      }
    }

    start().catch(err=>{
      status('Camera error: ' + err.message);
    });
  </script>
</body>
</html>
"""


@app.get("/scanner")
def scanner():
    return render_template_string(SCANNER_HTML, brand=BRAND_NAME)


# ---------------------- Verify API ----------------------
@app.post("/api/verify")
def api_verify():
    pin = request.headers.get('X-Admin-Pin', '')
    if pin != ADMIN_PIN:
        return jsonify({ 'status': 'unauthorized' }), 401

    data = request.get_json(silent=True) or {}
    token = (data.get('token') or '').strip().upper()
    if not token:
        return jsonify({ 'status': 'invalid' }), 400

    db = get_db()

    # Read current state
    row = db.execute("SELECT id, email, used FROM vouchers WHERE token = ?", (token,)).fetchone()
    if not row:
        return jsonify({ 'status': 'invalid' })

    if row['used'] == 1:
        return jsonify({ 'status': 'used' })

    # Mark used now (atomic enough for SQLite + single process)
    db.execute("UPDATE vouchers SET used = 1, used_at = ? WHERE id = ?", (datetime.utcnow().isoformat(), row['id']))
    db.commit()
    return jsonify({ 'status': 'valid', 'email': row['email'] })


# ---------------------- Root helper ----------------------
@app.get('/')
def root():
    return redirect(url_for('admin'))


if __name__ == "__main__":
    print("\n== One-Time QR System ==\n")
    print("Admin UI:", "http://127.0.0.1:5000/admin")
    print("Scanner:", "http://127.0.0.1:5000/scanner")
    app.run(host="0.0.0.0", port=5000, debug=True)

