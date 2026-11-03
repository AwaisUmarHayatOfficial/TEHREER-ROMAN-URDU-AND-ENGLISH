"""
Roman Urdu Notes - web backend (FastAPI)

Each job runs yt_to_roman_urdu.py in its own subprocess:
  - the user's Gemini key goes only into that subprocess's environment (it is NOT saved to disk)
  - the script's logs are parsed line by line -> percentage / chunk status
  - the .docx / .txt output is created in the job's own folder and served for download
  - old jobs are deleted automatically after the TTL

Accounts: visitors sign in with an e-mailed code (see auth.py); users, sessions and job history live in MySQL (tehreer.sql).

NOTE: running-job state lives in memory, so run the server with a SINGLE worker process.
"""
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, db
from .common import PROXY_IP_HEADER, TRUST_PROXY, client_ip  # noqa: F401

log = logging.getLogger("tehreer")

BASE = Path(__file__).resolve().parent
# One converter script per notes language (each job runs the chosen one in its own subprocess)
SCRIPTS = {
    "roman_urdu": Path(os.getenv("CONVERTER_SCRIPT_ROMAN_URDU") or os.getenv("CONVERTER_SCRIPT") or BASE / "yt_to_roman_urdu.py"),
    "english": Path(os.getenv("CONVERTER_SCRIPT_ENGLISH") or BASE / "yt_to_english.py"),
}
LANGUAGES = {"english": "English", "roman_urdu": "Roman Urdu"}
FRONTEND = Path(os.getenv("FRONTEND_DIR", BASE.parent / "frontend"))
JOBS_DIR = Path(os.getenv("JOBS_DIR", BASE / "jobs"))

MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
MAX_ACTIVE_PER_IP = int(os.getenv("MAX_ACTIVE_PER_IP", "3"))
MAX_ACTIVE_PER_USER = int(os.getenv("MAX_ACTIVE_PER_USER", "1"))
MAX_JOBS_PER_DAY_PER_USER = int(os.getenv("MAX_JOBS_PER_DAY_PER_USER", "20"))
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "90"))
MAX_JOBS_PER_HOUR_PER_IP = int(os.getenv("MAX_JOBS_PER_HOUR_PER_IP", "30"))
JOB_TTL_HOURS = float(os.getenv("JOB_TTL_HOURS", "24"))
MAX_JOB_MINUTES = int(os.getenv("MAX_JOB_MINUTES", "180"))
ALLOWED_ORIGINS = {o.strip().lower().rstrip("/") for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()}
MAX_LOG_LINES = 5000

JOBS_DIR.mkdir(parents=True, exist_ok=True)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
VIDEO_ID_RE = re.compile(r"(?:v=|/|be/|embed/|shorts/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
YT_HOST_RE = re.compile(r"^https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", re.I)
KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]{20,200}$")
BAD_KEY_RE = re.compile(r"API_KEY_INVALID|API key not valid|API key expired|UNAUTHENTICATED", re.I)

SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
JOBS: dict = {}
JOBS_LOCK = threading.Lock()
IP_HISTORY: dict = defaultdict(deque)


class Job:
    def __init__(self, job_id: str, ip: str, video_id: str, user_id: int, language: str = "roman_urdu"):
        self.id = job_id
        self.language = language
        self.ip = ip
        self.user_id = user_id
        self.video_url = f"https://www.youtube.com/watch?v={video_id}"
        self.dir = JOBS_DIR / job_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.status = "queued"  # queued | running | done | failed | cancelled
        self.stage = "Waiting in the queue..."
        self.percent = 0.0
        self.title = ""
        self.chunk_total = 0
        self.chunk_state: dict = {}
        self.logs: list = []
        self.error = ""
        self.warning = ""
        self.files: dict = {}
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.cancel_requested = False
        self.proc: Optional[subprocess.Popen] = None
        self.no_subs = False
        self.bad_key = False
        self.timed_out = False

    def _bump(self, pct, stage=None):
        self.percent = max(self.percent, min(pct, 99.0))
        if stage:
            self.stage = stage

    def _chunk_pct(self):
        if not self.chunk_total:
            return 12.0
        done = sum(1 for s in self.chunk_state.values() if s in ("done", "failed"))
        return 12.0 + 84.0 * done / self.chunk_total

    def feed(self, line: str):
        line = ANSI_RE.sub("", line.rstrip("\r\n"))
        if not line.strip():
            return
        with self.lock:
            if len(self.logs) < MAX_LOG_LINES:
                self.logs.append(line)

            if "Processing Video ID" in line:
                self._bump(2, "Fetching video info...")
                return
            m = re.search(r"Title Detected: '(.*)'", line)
            if m:
                self.title = m.group(1)
                self._bump(5, "Downloading subtitles...")
                return
            m = re.search(r"Total Video Length: ~(\d+)", line)
            if m:
                self._bump(9, f"Video is ~{m.group(1)} minutes long. Getting Gemini ready...")
                return
            if re.search(r"Detecting (?:the )?document header", line):
                self._bump(10, "Detecting the notes header...")
                return
            m = re.search(r"Converting (?:the )?video in (\d+) chunks", line)
            if m:
                self.chunk_total = int(m.group(1))
                self._bump(12, "Starting conversion...")
                return
            m = re.search(r"Processing Chunk (\d+)/(\d+)", line)
            if m:
                n, t = int(m.group(1)), int(m.group(2))
                self.chunk_total = t
                self.chunk_state[n] = "active"
                self._bump(self._chunk_pct(), f"Converting chunk {n}/{t}...")
                return
            m = re.search(r"Chunk (\d+) completed", line)
            if m:
                self.chunk_state[int(m.group(1))] = "done"
                self._bump(self._chunk_pct())
                return
            m = re.search(r"Error in Chunk (\d+)", line)
            if m:
                self.chunk_state[int(m.group(1))] = "failed"
                self._bump(self._chunk_pct())
                if BAD_KEY_RE.search(line):
                    self.bad_key = True
                return
            if "PROCESS COMPLETE" in line:
                self._bump(98, "Building the Word document...")
                return
            if "Could not fetch subtitles" in line:
                self.no_subs = True

    def to_dict(self, since: int = 0) -> dict:
        pos = queue_position(self)
        with self.lock:
            total = self.chunk_total
            states = [self.chunk_state.get(i, "pending") for i in range(1, total + 1)]
            end = self.finished or time.time()
            elapsed = int(end - self.started) if self.started else 0
            return {
                "id": self.id,
                "language": self.language,
                "status": self.status,
                "stage": self.stage,
                "percent": round(self.percent, 1),
                "title": self.title,
                "chunk_total": total,
                "chunks": states,
                "queue_position": pos,
                "elapsed": elapsed,
                "error": self.error,
                "warning": self.warning,
                "files": {k: f"/api/jobs/{self.id}/download/{k}" for k in self.files},
                "logs": self.logs[since:],
                "next": len(self.logs),
            }


def queue_position(job: Job) -> int:
    if job.status != "queued":
        return 0
    with JOBS_LOCK:
        ahead = [j for j in JOBS.values() if j.status == "queued" and j.created < job.created]
    return len(ahead) + 1


def kill_job(job: Job):
    p = job.proc
    if p and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass


def _finalize_state(job: Job, returncode: Optional[int]):
    files = {}
    for p in job.dir.iterdir():
        if p.suffix == ".docx":
            files["docx"] = p.name
        elif p.suffix == ".txt":
            files["txt"] = p.name
    failed = sum(1 for s in job.chunk_state.values() if s == "failed")

    with job.lock:
        job.finished = time.time()
        if job.cancel_requested:
            job.status, job.stage = "cancelled", "Job cancelled."
        elif job.bad_key:
            job.status, job.stage = "failed", "API key was rejected."
            job.error = "Gemini rejected your API key. Create a new one at aistudio.google.com/apikey"
        elif job.timed_out:
            job.status, job.stage = "failed", "Time limit reached."
            job.error = f"The job ran longer than {MAX_JOB_MINUTES} minutes and was stopped."
        elif job.no_subs:
            job.status, job.stage = "failed", "No subtitles found."
            job.error = ("Could not get subtitles for this video. It may be a live stream, captions may be disabled, "
                         "or YouTube may have blocked the server's request. Try another video.")
        elif "docx" in files and returncode == 0 and not (job.chunk_total and failed >= job.chunk_total):
            job.status, job.stage, job.percent, job.files = "done", "Done! You can download your files.", 100.0, files
            if failed:
                job.warning = (f"{failed} chunk(s) could not be converted (Gemini rate limit or error). "
                               "The rest of the notes are ready; you can try again a little later if you like.")
        else:
            job.status, job.stage = "failed", "Conversion failed."
            job.error = ("The job could not finish. Common causes: Gemini quota / rate limit or a subtitles problem. "
                         "Please try again in a little while.")


def persist_running(job: Job):
    try:
        db.execute("UPDATE jobs SET status = 'running', started_at = UTC_TIMESTAMP() WHERE id = %s", (job.id,))
    except Exception:
        log.exception("could not store job start")


def persist_final(job: Job):
    try:
        with job.lock:
            vals = (job.status, job.percent, (job.title or "")[:500] or None, job.chunk_total,
                    (job.error or "")[:500] or None, (job.warning or "")[:500] or None,
                    job.files.get("docx"), job.files.get("txt"), job.id)
        db.execute(
            "UPDATE jobs SET status = %s, percent = %s, title = %s, chunk_total = %s, error = %s, warning = %s, "
            "docx_name = %s, txt_name = %s, finished_at = UTC_TIMESTAMP() WHERE id = %s", vals)
    except Exception:
        log.exception("could not store job result")


def finalize(job: Job, returncode: Optional[int]):
    _finalize_state(job, returncode)
    persist_final(job)


def run_job(job: Job, api_key: str):
    while not SLOTS.acquire(timeout=1):
        if job.cancel_requested:
            finalize(job, None)
            return
    returncode = None
    timer = None
    try:
        if job.cancel_requested:
            return
        with job.lock:
            job.status, job.started = "running", time.time()
            job.stage, job.percent = "Starting...", 1.0
        persist_running(job)

        env = dict(os.environ)
        env.update(
            GEMINI_API_KEY=api_key,
            GOOGLE_API_KEY=api_key,
            PYTHONUNBUFFERED="1",
            PYTHONIOENCODING="utf-8",
            PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", str(SCRIPTS[job.language]), job.video_url],
            cwd=str(job.dir), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            start_new_session=True,
        )
        job.proc = proc

        def _timeout():
            job.timed_out = True
            kill_job(job)

        timer = threading.Timer(MAX_JOB_MINUTES * 60, _timeout)
        timer.daemon = True
        timer.start()

        for line in proc.stdout:
            job.feed(line.replace(api_key, "***"))
            if job.bad_key:
                kill_job(job)
        returncode = proc.wait()
    except Exception as e:
        job.feed(f"[X] Server error: {type(e).__name__}")
    finally:
        if timer:
            timer.cancel()
        SLOTS.release()
        finalize(job, returncode)


def check_gemini_key(key: str) -> Optional[bool]:
    """True = valid, False = rejected by Google, None = unknown (network/quota)"""
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
            headers={"x-goog-api-key": key}, timeout=10,
        )
    except requests.RequestException:
        return None
    if r.status_code == 200:
        return True
    if r.status_code in (400, 401, 403):
        return False
    return None


# ---------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------
def cleanup_once():
    cutoff_s = int(JOB_TTL_HOURS * 3600)
    # 1) generated files older than the retention time
    for r in db.query(
        "SELECT id FROM jobs WHERE files_deleted = 0 AND finished_at IS NOT NULL "
        "AND finished_at < UTC_TIMESTAMP() - INTERVAL %s SECOND", (cutoff_s,)):
        shutil.rmtree(JOBS_DIR / r["id"], ignore_errors=True)
        db.execute("UPDATE jobs SET files_deleted = 1 WHERE id = %s", (r["id"],))
    # in-memory state of finished jobs
    mem_cutoff = time.time() - JOB_TTL_HOURS * 3600
    with JOBS_LOCK:
        for jid in [j.id for j in JOBS.values() if j.finished and j.finished < mem_cutoff]:
            JOBS.pop(jid, None)
    # 2) history rows older than HISTORY_DAYS
    for r in db.query(
        "SELECT id FROM jobs WHERE created_at < UTC_TIMESTAMP() - INTERVAL %s DAY AND status NOT IN ('queued','running')",
        (HISTORY_DAYS,)):
        shutil.rmtree(JOBS_DIR / r["id"], ignore_errors=True)
        db.execute("DELETE FROM jobs WHERE id = %s", (r["id"],))
    # 3) unfinished registrations, expired sessions / codes, old audit rows
    db.execute("DELETE FROM users WHERE status = 'pending' AND created_at < UTC_TIMESTAMP() - INTERVAL 1 DAY")
    db.execute("DELETE FROM sessions WHERE expires_at < UTC_TIMESTAMP()")
    db.execute("DELETE FROM login_codes WHERE expires_at < UTC_TIMESTAMP() - INTERVAL 1 DAY")
    db.execute("DELETE FROM auth_events WHERE created_at < UTC_TIMESTAMP() - INTERVAL 180 DAY")
    # 4) directories that have no database row any more
    known = {r["id"] for r in db.query("SELECT id FROM jobs")}
    for d in JOBS_DIR.iterdir():
        try:
            if d.is_dir() and d.name not in known and d.stat().st_mtime < time.time() - cutoff_s:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def cleanup_loop():
    while True:
        time.sleep(600)
        try:
            cleanup_once()
        except Exception:
            log.exception("cleanup failed")


app = FastAPI(title="Roman Urdu Notes", docs_url=None, redoc_url=None)
app.include_router(auth.router)


@app.on_event("startup")
def _startup():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_schema()
    db.execute("UPDATE jobs SET status = 'failed', error = 'The server restarted while this job was running.', "
               "finished_at = UTC_TIMESTAMP() WHERE status IN ('queued', 'running')")
    cleanup_once()
    threading.Thread(target=cleanup_loop, daemon=True).start()


def frontend_build() -> str:
    """Short fingerprint of the frontend files currently being served (to verify a deployment)."""
    import hashlib
    h = hashlib.md5()
    for name in ("index.html", "site.js", "style.css"):
        try:
            h.update((FRONTEND / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:8]


@app.get("/api/health")
def health():
    with JOBS_LOCK:
        running = sum(1 for j in JOBS.values() if j.status == "running")
        queued = sum(1 for j in JOBS.values() if j.status == "queued")
    try:
        db_ok = bool(db.one("SELECT 1 AS ok"))
    except Exception:
        db_ok = False
    return {"ok": True, "db": db_ok, "running": running, "queued": queued, "max_concurrent": MAX_CONCURRENT_JOBS,
            "frontend_build": frontend_build()}


# ---------------------------------------------------------------------
# Conversion jobs (signed-in users only)
# ---------------------------------------------------------------------
class NewJob(BaseModel):
    api_key: str
    url: str
    language: str = ""


def iso(dt):
    return dt.isoformat() + "Z" if dt else None


def job_files(row: dict) -> dict:
    files = {}
    if row["status"] == "done" and not row["files_deleted"]:
        for kind, col in (("docx", "docx_name"), ("txt", "txt_name")):
            if row[col] and (JOBS_DIR / row["id"] / row[col]).is_file():
                files[kind] = f"/api/jobs/{row['id']}/download/{kind}"
    return files


def job_summary(row: dict) -> dict:
    return {
        "id": row["id"], "title": row["title"] or "", "video_url": row["video_url"], "status": row["status"],
        "language": row["language"], "language_label": LANGUAGES.get(row["language"], row["language"]),
        "percent": float(row["percent"]), "error": row["error"] or "", "warning": row["warning"] or "",
        "created_at": iso(row["created_at"]), "finished_at": iso(row["finished_at"]),
        "files": job_files(row), "expired": bool(row["files_deleted"]) and row["status"] == "done",
    }


STAGE_BY_STATUS = {"done": "Done! You can download your files.", "failed": "Conversion failed.",
                   "cancelled": "Job cancelled.", "running": "Working...", "queued": "Waiting in the queue..."}


@app.post("/api/jobs")
def create_job(body: NewJob, request: Request):
    user = auth.require_user(request)
    key = re.sub(r"\s+", "", body.api_key or "")
    url = (body.url or "").strip()
    language = (body.language or "").strip().lower()
    if language not in LANGUAGES:
        raise HTTPException(400, "Choose the notes language first: English or Roman Urdu.")

    if not KEY_RE.match(key):
        raise HTTPException(400, "That API key does not look right. Paste the full key (AIza... or AQ....).")
    if len(url) > 300 or not YT_HOST_RE.match(url):
        raise HTTPException(400, "That does not look like a YouTube link. Example: https://www.youtube.com/watch?v=XXXXXXXXXXX")
    m = VIDEO_ID_RE.search(url)
    if not m:
        raise HTTPException(400, "No video ID found in the link. Paste the full video link.")

    ip = client_ip(request)
    now = time.time()
    with JOBS_LOCK:
        hist = IP_HISTORY[ip]
        while hist and hist[0] < now - 3600:
            hist.popleft()
        if len(hist) >= MAX_JOBS_PER_HOUR_PER_IP:
            raise HTTPException(429, "Too many jobs were started from your network in the last hour. Please try again later.")
        if sum(1 for j in JOBS.values() if j.user_id == user["id"] and j.status in ("queued", "running")) >= MAX_ACTIVE_PER_USER:
            raise HTTPException(429, "You already have a job running. Let it finish or cancel it.")
        if sum(1 for j in JOBS.values() if j.ip == ip and j.status in ("queued", "running")) >= MAX_ACTIVE_PER_IP:
            raise HTTPException(429, "Too many jobs are running from your network. Please wait for one to finish.")
    day = db.one("SELECT COUNT(*) AS c FROM jobs WHERE user_id = %s AND created_at > UTC_TIMESTAMP() - INTERVAL 1 DAY", (user["id"],))
    if day["c"] >= MAX_JOBS_PER_DAY_PER_USER:
        raise HTTPException(429, f"Daily limit reached ({MAX_JOBS_PER_DAY_PER_USER} conversions per day). Please try again tomorrow.")

    if check_gemini_key(key) is False:
        raise HTTPException(400, "Gemini did not accept this API key. Create a new one at aistudio.google.com/apikey")

    job = Job(secrets.token_urlsafe(16), ip, m.group(1), user["id"], language)
    try:
        db.execute("INSERT INTO jobs (id, user_id, video_id, video_url, language) VALUES (%s, %s, %s, %s, %s)",
                   (job.id, user["id"], m.group(1), job.video_url, language))
    except Exception:
        shutil.rmtree(job.dir, ignore_errors=True)
        log.exception("could not store job")
        raise HTTPException(503, "The database is not available right now. Please try again shortly.")
    with JOBS_LOCK:
        JOBS[job.id] = job
        IP_HISTORY[ip].append(now)
    threading.Thread(target=run_job, args=(job, key), daemon=True).start()
    return {"id": job.id}


def own_row(job_id: str, user: dict) -> dict:
    row = db.one("SELECT * FROM jobs WHERE id = %s AND user_id = %s", (job_id, user["id"]))
    if not row:
        raise HTTPException(404, "Job not found (it may have expired and been deleted).")
    return row


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, request: Request, since: int = 0):
    user = auth.require_user(request)
    job = JOBS.get(job_id)
    if job:
        if job.user_id != user["id"]:
            raise HTTPException(404, "Job not found (it may have expired and been deleted).")
        return job.to_dict(max(since, 0))
    row = own_row(job_id, user)  # finished earlier / server restarted: answer from the database
    s = job_summary(row)
    return {"id": row["id"], "language": row["language"], "status": row["status"], "stage": STAGE_BY_STATUS.get(row["status"], ""),
            "percent": s["percent"], "title": s["title"], "chunk_total": row["chunk_total"], "chunks": [],
            "queue_position": 0, "elapsed": 0, "error": s["error"], "warning": s["warning"],
            "files": s["files"], "logs": [], "next": max(since, 0)}


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str, request: Request):
    user = auth.require_user(request)
    job = JOBS.get(job_id)
    if not job or job.user_id != user["id"]:
        raise HTTPException(404, "Job not found.")
    if job.status in ("queued", "running"):
        job.cancel_requested = True
        kill_job(job)
    return {"ok": True}


MEDIA = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain; charset=utf-8",
}


@app.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str, request: Request):
    user = auth.require_user(request)
    if kind not in MEDIA:
        raise HTTPException(404, "File not found.")
    row = own_row(job_id, user)
    name = row["docx_name"] if kind == "docx" else row["txt_name"]
    if row["status"] != "done" or not name:
        raise HTTPException(404, "The file is not ready.")
    base = (JOBS_DIR / job_id).resolve()
    path = (base / name).resolve()
    if base not in path.parents or not path.is_file():
        raise HTTPException(410, "This file has expired and was deleted. Please convert the video again.")
    return FileResponse(path, media_type=MEDIA[kind], filename=name)


# ---------------------------------------------------------------------
# My account: history, profile, deletion
# ---------------------------------------------------------------------
@app.get("/api/my/jobs")
def my_jobs(request: Request):
    user = auth.require_user(request)
    rows = db.query("SELECT * FROM jobs WHERE user_id = %s ORDER BY created_at DESC LIMIT 50", (user["id"],))
    return {"jobs": [job_summary(r) for r in rows], "retention_hours": JOB_TTL_HOURS, "history_days": HISTORY_DAYS}


@app.delete("/api/my/jobs/{job_id}")
def delete_my_job(job_id: str, request: Request):
    user = auth.require_user(request)
    row = own_row(job_id, user)
    if row["status"] in ("queued", "running"):
        raise HTTPException(409, "Cancel the job first.")
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
    db.execute("DELETE FROM jobs WHERE id = %s AND user_id = %s", (job_id, user["id"]))
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return {"ok": True}


class ProfileBody(BaseModel):
    first_name: str
    last_name: str
    phone: str


@app.patch("/api/me")
def update_profile(body: ProfileBody, request: Request):
    user = auth.require_user(request)
    try:
        first = auth.clean_name(body.first_name, "First name")
        last = auth.clean_name(body.last_name, "Last name") if (body.last_name or "").strip() else None
        phone = auth.clean_phone(body.phone) if (body.phone or "").strip() else None  # optional here (Google accounts start without one)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.execute("UPDATE users SET first_name = %s, last_name = %s, phone = %s, display_name = %s WHERE id = %s",
               (first, last, phone, f"{first} {last or ''}".strip(), user["id"]))
    user = db.one("SELECT * FROM users WHERE id = %s", (user["id"],))
    return {"user": auth.public_user(user)}


class DeleteAccountBody(BaseModel):
    confirm: str
    password: str = ""


@app.delete("/api/me")
def delete_account(body: DeleteAccountBody, request: Request, response: Response):
    user = auth.require_user(request)
    if (body.confirm or "").strip() != "DELETE":
        raise HTTPException(400, "Type DELETE to confirm.")
    if user.get("password_hash") and not auth.verify_user_password(user, body.password):
        raise HTTPException(400, "Your password is incorrect.")
    with JOBS_LOCK:
        mine = [j for j in JOBS.values() if j.user_id == user["id"]]
    for j in mine:
        j.cancel_requested = True
        kill_job(j)
    for r in db.query("SELECT id FROM jobs WHERE user_id = %s", (user["id"],)):
        shutil.rmtree(JOBS_DIR / r["id"], ignore_errors=True)
        with JOBS_LOCK:
            JOBS.pop(r["id"], None)
    db.execute("DELETE FROM auth_events WHERE user_id = %s OR email_normalized = %s", (user["id"], user["email_normalized"]))
    db.execute("DELETE FROM login_codes WHERE email_normalized = %s", (user["email_normalized"],))
    db.execute("DELETE FROM users WHERE id = %s", (user["id"],))  # sessions + jobs rows cascade
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


# ---------------------------------------------------------------------
# Website pages
# ---------------------------------------------------------------------
PAGES = {
    "/": "index.html",
    "/app": "app.html",
    "/features": "features.html",
    "/how-it-works": "how-it-works.html",
    "/solutions": "solutions.html",
    "/security": "security.html",
    "/faq": "faq.html",
    "/about": "about.html",
    "/contact": "contact.html",
    "/privacy": "privacy.html",
    "/terms": "terms.html",
    "/login": "login.html",
    "/register": "register.html",
    "/forgot-password": "forgot-password.html",
    "/account": "account.html",
}
NOT_IN_SITEMAP = {"/app", "/login", "/register", "/forgot-password", "/account"}


def _page_handler(filename: str):
    def handler():
        return FileResponse(FRONTEND / filename)
    return handler


for _path, _file in PAGES.items():
    app.add_api_route(_path, _page_handler(_file), methods=["GET"], include_in_schema=False)


def public_base(request: Request) -> str:
    configured = os.getenv("PUBLIC_URL", "").rstrip("/")
    return configured or str(request.base_url).rstrip("/")


@app.get("/robots.txt", include_in_schema=False)
def robots(request: Request):
    body = f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /login\nDisallow: /register\nDisallow: /forgot-password\nDisallow: /account\nSitemap: {public_base(request)}/sitemap.xml\n"
    return Response(body, media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap(request: Request):
    base = public_base(request)
    urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in PAGES if p not in NOT_IN_SITEMAP)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, media_type="application/xml")


@app.exception_handler(StarletteHTTPException)
async def custom_http_exception(request: Request, exc: StarletteHTTPException):
    path = request.url.path
    if exc.status_code == 404 and not path.startswith(("/api/", "/static/")):
        return FileResponse(FRONTEND / "404.html", status_code=404)
    return await http_exception_handler(request, exc)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    path = request.url.path
    # block cross-site state-changing API calls (the session cookie is also SameSite=Lax)
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "").lower()
            if urlparse(origin).netloc.lower() != host and origin.lower().rstrip("/") not in ALLOWED_ORIGINS:
                return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if path.startswith("/api/") or path in ("/login", "/register", "/forgot-password", "/account", "/app"):
        response.headers["Cache-Control"] = "no-store"
    elif path in PAGES or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache"  # always revalidate (cheap 304 when unchanged)
    elif path.startswith("/static/img/"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    return response


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
