import os
import time
import smtplib
import random
import queue
import threading
import uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import Flask, request, render_template_string, jsonify, abort

# ----------------------------------
# CONFIG (ENV)
# ----------------------------------
SMTP_HOST = "mail.privateemail.com"
SMTP_PORT = 465

SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")

FLASK_SECRET = os.getenv("FLASK_SECRET", "fallback-secret")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

DELAY_MIN = 60
DELAY_MAX = 70

MAX_ACTIVE_JOBS = 1
MAX_RECIPIENTS = 300
MAX_WORKERS = 3

if not SMTP_USER or not SMTP_PASS or not ADMIN_TOKEN:
    raise RuntimeError("Missing required ENV vars")

# ----------------------------------
# FLASK
# ----------------------------------
app = Flask(__name__)
app.secret_key = FLASK_SECRET

# ----------------------------------
# HTML TEMPLATE
# ----------------------------------
TEMPLATE = """
<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="utf-8">
<title>SMTP Bulk Sender</title>
<style>
body{font-family:Arial;max-width:900px;margin:20px}
textarea,input{width:100%}
textarea{height:120px}
label{display:block;margin-top:10px}
button{padding:10px;margin-top:10px}
.box{margin-top:12px;border:1px solid #ccc;background:#fafafa;padding:10px}
.good{color:green}
.bad{color:red}
.progress{width:100%;background:#ddd;height:22px;border-radius:5px;overflow:hidden}
.progress-bar{height:100%;width:0%;background:#4CAF50;transition:width .5s}
</style>
</head>

<body>
<h2>SMTP Bulk Sender</h2>

<form method="POST">
<label>Token acces</label>
<input name="token" required>

<label>Nume expeditor (opțional)</label>
<input name="sender_name">

<label>Destinatari (1 per linie sau separați prin virgulă)</label>
<textarea name="recipients" required></textarea>

<label>Subiect</label>
<input name="subject" required>

<label>Mesaj HTML</label>
<textarea name="html_body" required></textarea>

<label>Body text simplu (opțional)</label>
<textarea name="text_body"></textarea>

<label>Thread-uri</label>
<input name="workers" value="2" type="number" min="1" max="3">

<button>START CAMPANIE</button>
</form>

{% if job_id %}
<div class="box">
✅ Job pornit<br>
<a href="/status/{{ job_id }}?token={{ token }}">Vezi status</a>
</div>
{% endif %}

{% if show_status %}
<h3>Status job {{ job_id }}</h3>

<div class="box">
  <div class="progress">
    <div class="progress-bar" id="bar"></div>
  </div>
  <p id="progressText"></p>
  <p id="eta"></p>
</div>

<div class="box" id="log"></div>

<script>
function update() {
  fetch("/status_json/{{ job_id }}?token={{ token }}")
  .then(r => r.json())
  .then(d => {
    if (d.error) return;

    let percent = Math.round((d.sent / d.total) * 100);
    document.getElementById("bar").style.width = percent + "%";
    document.getElementById("progressText").innerText =
      d.sent + " / " + d.total + " (" + percent + "%)";

    document.getElementById("eta").innerText = d.finished
      ? "✅ Campanie finalizată"
      : "⏳ ETA: " + d.eta + " sec";

    let log = document.getElementById("log");
    log.innerHTML = "";
    d.results.forEach(r => {
      let div = document.createElement("div");
      div.innerText = r[1];
      div.className = r[0] ? "good" : "bad";
      log.appendChild(div);
    });
  });
}
setInterval(update, 3000);
update();
</script>
{% endif %}
</body>
</html>
"""

# ----------------------------------
# JOB STATE
# ----------------------------------
jobs = {}
lock = threading.Lock()

# ----------------------------------
# UTILS
# ----------------------------------
def check_token():
    token = request.values.get("token")
    if token != ADMIN_TOKEN:
        abort(401)

def normalize_html(text):
    return "".join(
        f"<p>{l}</p>" if l.strip() else "<br>"
        for l in text.splitlines()
    )

def build_msg(sender_name, to, subject, html, plain):
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{sender_name} <{SMTP_USER}>" if sender_name else SMTP_USER
    msg["To"] = to
    msg["Subject"] = subject
    if plain:
        msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg

def send_mail(msg):
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return True, "Trimis OK"
    except Exception as e:
        return False, str(e)

# ----------------------------------
# WORKER
# ----------------------------------
def worker(q, job):
    while True:
        try:
            r = q.get(False)
        except:
            return

        time.sleep(random.randint(DELAY_MIN, DELAY_MAX))

        msg = build_msg(
            job["sender_name"],
            r,
            job["subject"],
            normalize_html(job["html_body"]),
            job["text_body"]
        )

        ok, info = send_mail(msg)

        with lock:
            job["sent"] += 1
            job["results"].append((ok, f"{r} ➜ {info}"))

        q.task_done()

# ----------------------------------
# JOB RUNNER
# ----------------------------------
def run_job(job):
    q = queue.Queue()
    for r in job["recipients"]:
        q.put(r)

    for _ in range(job["workers"]):
        threading.Thread(target=worker, args=(q, job), daemon=True).start()

    q.join()
    job["finished"] = True

# ----------------------------------
# ROUTES
# ----------------------------------
@app.route("/", methods=["GET","POST"])
def index():
    check_token()

    if request.method == "POST":
        with lock:
            if len([j for j in jobs.values() if not j["finished"]]) >= MAX_ACTIVE_JOBS:
                return "Job activ deja"

        recipients = []
        for l in request.form["recipients"].splitlines():
            for x in l.split(","):
                if x.strip():
                    recipients.append(x.strip())

        if len(recipients) > MAX_RECIPIENTS:
            return "Prea multi destinatari"

        job_id = str(uuid.uuid4())[:8]
        job = {
            "finished": False,
            "results": [],
            "recipients": recipients,
            "total": len(recipients),
            "sent": 0,
            "start": time.time(),
            "sender_name": request.form.get("sender_name",""),
            "subject": request.form["subject"],
            "html_body": request.form["html_body"],
            "text_body": request.form.get("text_body",""),
            "workers": min(int(request.form.get("workers",2)), MAX_WORKERS)
        }

        jobs[job_id] = job
        threading.Thread(target=run_job, args=(job,), daemon=True).start()

        return render_template_string(
            TEMPLATE,
            job_id=job_id,
            token=ADMIN_TOKEN
        )

    return render_template_string(TEMPLATE, token=ADMIN_TOKEN)

@app.route("/status/<job_id>")
def status(job_id):
    check_token()
    return render_template_string(
        TEMPLATE,
        job_id=job_id,
        show_status=True,
        token=ADMIN_TOKEN
    )

@app.route("/status_json/<job_id>")
def status_json(job_id):
    check_token()

    job = jobs.get(job_id)
    if not job:
        return jsonify(error=True)

    elapsed = time.time() - job["start"]
    avg = elapsed / job["sent"] if job["sent"] else 0
    eta = int(avg * (job["total"] - job["sent"]))

    return jsonify(
        sent=job["sent"],
        total=job["total"],
        finished=job["finished"],
        eta=eta,
        results=job["results"][-5:]
    )

# ----------------------------------
# RUN
# ----------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
