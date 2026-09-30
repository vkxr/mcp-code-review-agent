"""30 pull-request diffs, each with one seeded bug.

Each case is a realistic small change: `before` is the file on the base branch,
`after` is the file on the PR branch. The PR adds working code *and* one bug.
The expected bug line is the line in `after` containing `bug_snippet`, so labels
stay correct if a case is edited. The model never sees the labels.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    id: str
    path: str
    before: str
    after: str
    bug_snippet: str
    category: str
    description: str

    @property
    def diff(self) -> str:
        return "".join(difflib.unified_diff(
            self.before.splitlines(keepends=True), self.after.splitlines(keepends=True),
            fromfile=f"a/{self.path}", tofile=f"b/{self.path}"))

    @property
    def bug_line(self) -> int:
        hits = [i for i, line in enumerate(self.after.splitlines(), 1) if self.bug_snippet in line]
        if len(hits) != 1:
            raise ValueError(f"{self.id}: bug_snippet must match exactly one line, matched {hits}")
        return hits[0]


def case(id, path, before, added, bug_snippet, category, description) -> Case:
    before = before.strip("\n") + "\n"
    after = before + "\n\n" + added.strip("\n") + "\n"
    return Case(id, path, before, after, bug_snippet, category, description)


PY_DB = '''
import sqlite3

from app.config import DB_PATH


def get_conn():
    return sqlite3.connect(DB_PATH)
'''

PY_API = '''
from flask import Blueprint, jsonify, request

from app.auth import current_user, login_required

bp = Blueprint("api", __name__)


@bp.get("/health")
def health():
    return jsonify(status="ok")
'''

TS_API = '''
import { Router } from "express";
import { db } from "../db";

export const router = Router();

router.get("/health", (_req, res) => res.json({ status: "ok" }));
'''

CASES: list[Case] = [
    case("01_sql_injection", "app/users.py", PY_DB, '''
def find_user_by_email(email: str):
    conn = get_conn()
    try:
        cur = conn.execute(f"SELECT id, name, email FROM users WHERE email = '{email}'")
        return cur.fetchone()
    finally:
        conn.close()
''', "WHERE email = '{email}'", "security", "SQL built with an f-string from caller input"),

    case("02_shell_injection", "app/images.py", '''
import os

UPLOAD_DIR = "/srv/uploads"
''', '''
import subprocess


def make_thumbnail(filename: str) -> str:
    src = os.path.join(UPLOAD_DIR, os.path.basename(filename))
    out = src + ".thumb.png"
    subprocess.run(f"convert {src} -resize 200x200 {out}", shell=True, check=True)
    return out
''', 'shell=True', "security", "User-controlled filename passed to a shell"),

    case("03_path_traversal", "app/files.py", PY_API, '''
import os

UPLOAD_DIR = "/srv/uploads"


@bp.get("/files/<path:name>")
@login_required
def download(name):
    with open(os.path.join(UPLOAD_DIR, name), "rb") as f:
        return f.read()
''', "os.path.join(UPLOAD_DIR, name)", "security", "Path traversal via ../ in the file name"),

    case("04_hardcoded_secret", "app/billing_client.py", '''
import httpx

BILLING_URL = "https://billing.internal.example.com"
''', '''
BILLING_API_TOKEN = "prod-7f3a9c1e5b2d4a6f8e0c"


def charge(customer_id: str, amount_paise: int) -> dict:
    r = httpx.post(f"{BILLING_URL}/charges",
                   headers={"Authorization": f"Bearer {BILLING_API_TOKEN}"},
                   json={"customer": customer_id, "amount": amount_paise}, timeout=10)
    r.raise_for_status()
    return r.json()
''', 'BILLING_API_TOKEN = "prod-', "security", "Production API token committed to source"),

    case("05_pickle_untrusted", "app/sessions.py", PY_API, '''
import base64
import pickle


@bp.post("/session/restore")
def restore_session():
    blob = base64.b64decode(request.cookies.get("state", ""))
    state = pickle.loads(blob)
    return jsonify(cart=state.get("cart", []))
''', "pickle.loads(blob)", "security", "Unpickling a client-controlled cookie allows code execution"),

    case("06_yaml_unsafe_load", "app/importer.py", PY_API, '''
import yaml


@bp.post("/workflows/import")
@login_required
def import_workflow():
    spec = yaml.load(request.get_data(as_text=True), Loader=yaml.UnsafeLoader)
    return jsonify(name=spec["name"], steps=len(spec["steps"]))
''', "yaml.UnsafeLoader", "security", "UnsafeLoader on request body allows object construction"),

    case("07_md5_passwords", "app/passwords.py", '''
import hashlib
import secrets
''', '''
def hash_password(password: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.md5((salt + password).encode()).hexdigest()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    salt, digest = stored.split("$", 1)
    return secrets.compare_digest(hashlib.md5((salt + password).encode()).hexdigest(), digest)
''', "digest = hashlib.md5", "security", "Fast unkeyed hash (MD5) used for password storage"),

    case("08_timing_unsafe_compare", "app/webhooks.py", '''
import hashlib
import hmac
import os

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"].encode()
''', '''
def verify_signature(body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    return header == expected
''', "return header == expected", "security", "Signature compared with == (timing attack)"),

    case("09_tls_verify_disabled", "app/kyc.py", '''
import os

import requests

KYC_URL = os.environ.get("KYC_URL", "https://kyc.partner.example.com")
''', '''
def verify_pan(pan: str) -> bool:
    resp = requests.post(f"{KYC_URL}/v1/pan", json={"pan": pan}, timeout=10, verify=False)
    resp.raise_for_status()
    return resp.json()["valid"]
''', "verify=False", "security", "TLS certificate verification disabled for a KYC call"),

    case("10_jwt_no_verify", "app/tokens.py", '''
import os

import jwt

JWT_SECRET = os.environ["JWT_SECRET"]
''', '''
def user_id_from_token(token: str) -> int:
    claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], options={"verify_signature": False})
    return int(claims["sub"])
''', '"verify_signature": False', "security", "JWT signature not verified, any token is accepted"),

    case("11_missing_authorization", "app/invoices.py", PY_API, '''
from app import db


@bp.delete("/invoices/<int:invoice_id>")
@login_required
def delete_invoice(invoice_id):
    invoice = db.get_invoice(invoice_id)
    if invoice is None:
        return jsonify(error="not found"), 404
    db.delete_invoice(invoice.id)
    return jsonify(deleted=invoice.id)
''', "db.delete_invoice(invoice.id)", "security",
         "Any logged-in user can delete any invoice (no ownership check)"),

    case("12_ssrf", "app/previews.py", PY_API, '''
import httpx


@bp.post("/link-preview")
@login_required
def link_preview():
    url = request.json["url"]
    resp = httpx.get(url, timeout=5, follow_redirects=True)
    title = resp.text.split("<title>")[1].split("</title>")[0] if "<title>" in resp.text else url
    return jsonify(title=title)
''', "resp = httpx.get(url", "security", "Server fetches arbitrary user URLs (SSRF to internal network)"),

    case("13_pagination_off_by_one", "app/listing.py", '''
from dataclasses import dataclass


@dataclass
class Page:
    items: list
    page: int
    total: int
''', '''
def paginate(items: list, page: int, page_size: int = 20) -> Page:
    """Return the requested page. `page` is 1-based: page=1 is the first page."""
    start = page * page_size
    return Page(items=items[start:start + page_size], page=page, total=len(items))
''', "start = page * page_size", "correctness", "1-based page with 0-based offset skips the first page"),

    case("14_range_off_by_one", "app/metrics.py", '''
from statistics import mean
''', '''
def total_revenue(daily: list[float]) -> float:
    total = 0.0
    for i in range(len(daily) - 1):
        total += daily[i]
    return total
''', "range(len(daily) - 1)", "correctness", "Loop drops the last day"),

    case("15_mutable_default", "app/tags.py", '''
import re

TAG_RE = re.compile(r"^[a-z0-9-]{1,32}$")
''', '''
def normalize_tags(raw: list[str], seen: list[str] = []) -> list[str]:
    for tag in raw:
        tag = tag.strip().lower()
        if TAG_RE.match(tag) and tag not in seen:
            seen.append(tag)
    return seen
''', "seen: list[str] = []", "correctness", "Mutable default list leaks tags across calls"),

    case("16_age_boundary", "app/eligibility.py", '''
from datetime import date
''', '''
def can_open_account(birth_date: date, today: date) -> bool:
    """Customers aged 18 or over can open an account."""
    age = today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
    return age > 18
''', "return age > 18", "correctness", "Uses > instead of >=, rejects 18-year-olds"),

    case("17_boolean_logic", "app/login.py", '''
from app.models import User
''', '''
def can_log_in(user: User) -> bool:
    """Only active users who are not banned may log in."""
    return user.is_active or not user.is_banned
''', "return user.is_active or not user.is_banned", "security",
         "`or` should be `and`: banned-but-active users can log in"),

    case("18_none_dereference", "app/profile.py", '''
from typing import Optional

from app.models import User


def find_user(email: str) -> Optional[User]:
    """Returns None when no user has this email."""
    return User.query.filter_by(email=email).first()
''', '''
def display_name(email: str) -> str:
    user = find_user(email)
    return user.first_name + " " + user.last_name
''', "return user.first_name", "correctness", "find_user can return None; AttributeError"),

    case("19_swallowed_exception", "app/payments.py", '''
import logging

from app.gateway import gateway

log = logging.getLogger(__name__)
''', '''
def capture_payment(order_id: str, amount_paise: int) -> bool:
    try:
        gateway.capture(order_id=order_id, amount=amount_paise)
    except Exception:
        pass
    return True
''', "        pass", "reliability", "Payment failures are swallowed and reported as success"),

    case("20_connection_leak", "app/reports.py", '''
from app.db import pool
''', '''
def monthly_totals(month: str) -> list[tuple]:
    conn = pool.acquire()
    rows = conn.execute("SELECT day, SUM(amount) FROM orders WHERE month = ? GROUP BY day", (month,)).fetchall()
    pool.release(conn)
    return rows
''', "conn = pool.acquire()", "reliability", "Connection never released if the query raises"),

    case("21_dict_mutation_during_iteration", "app/cache.py", '''
import time


class TTLCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._data: dict[str, tuple[float, object]] = {}

    def set(self, key: str, value: object) -> None:
        self._data[key] = (time.monotonic(), value)
''', '''
    def evict_expired(self) -> int:
        now = time.monotonic()
        removed = 0
        for key, (stored_at, _) in self._data.items():
            if now - stored_at > self.ttl:
                del self._data[key]
                removed += 1
        return removed
''', "del self._data[key]", "correctness", "Deleting from a dict while iterating raises RuntimeError"),

    case("22_toctou_race", "app/wallet.py", '''
import threading


class Wallet:
    def __init__(self, balance: int):
        self.balance = balance
        self.lock = threading.Lock()
''', '''
    def withdraw(self, amount: int) -> bool:
        """Called concurrently from many request threads."""
        if self.balance >= amount:
            self.balance -= amount
            return True
        return False
''', "if self.balance >= amount:", "concurrency",
         "Check-then-act without the lock allows overdrafts under concurrency"),

    case("23_missing_await", "src/routes/users.ts", TS_API, '''
router.get("/users/:id/email", async (req, res) => {
  const user = db.users.findOne({ id: req.params.id });
  if (!user) return res.status(404).json({ error: "not found" });
  res.json({ email: user.email });
});
''', "const user = db.users.findOne", "correctness",
         "Missing await: user is a Promise, email is always undefined"),

    case("24_numeric_sort", "src/leaderboard.ts", '''
export type Score = { player: string; points: number };
''', '''
export function topPoints(scores: Score[], n = 3): number[] {
  const points = scores.map((s) => s.points);
  points.sort();
  return points.reverse().slice(0, n);
}
''', "points.sort();", "correctness", "Default sort is lexicographic: 100 sorts before 9"),

    case("25_xss_innerhtml", "src/comments.ts", '''
export type Comment = { author: string; body: string };
''', '''
export function renderComment(list: HTMLElement, comment: Comment): void {
  const item = document.createElement("li");
  const author = document.createElement("strong");
  author.textContent = comment.author;
  const body = document.createElement("p");
  body.innerHTML = comment.body;
  item.append(author, body);
  list.append(item);
}
''', "body.innerHTML = comment.body", "security", "User comment rendered as HTML (stored XSS)"),

    case("26_redos", "app/validators.py", '''
import re
''', '''
USERNAME_RE = re.compile(r"^([a-zA-Z0-9]+)*_admin$")


def is_admin_username(name: str) -> bool:
    return bool(USERNAME_RE.match(name))
''', 'USERNAME_RE = re.compile', "security", "Nested quantifier: catastrophic backtracking (ReDoS)"),

    case("27_retry_never_increments", "app/sms.py", '''
import time

from app.providers import sms_provider
''', '''
def send_otp(phone: str, code: str, max_retries: int = 3) -> bool:
    attempt = 0
    while attempt < max_retries:
        try:
            sms_provider.send(phone, f"Your OTP is {code}")
            return True
        except ConnectionError:
            time.sleep(2 ** attempt)
    return False
''', "while attempt < max_retries:", "reliability",
         "attempt is never incremented: infinite retry loop on failure"),

    case("28_naive_datetime", "app/otp.py", '''
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class OtpRecord:
    code: str
    expires_at_utc: datetime  # naive datetime in UTC
''', '''
def is_expired(record: OtpRecord) -> bool:
    return datetime.now() > record.expires_at_utc
''', "return datetime.now() > record.expires_at_utc", "correctness",
         "Local time compared with UTC: on an IST server OTPs expire 5.5h early"),

    case("29_cache_key_collision", "app/feed.py", '''
from app.cache import cache
from app.db import load_feed
''', '''
def get_feed(user_id: int, page: int) -> list:
    key = f"feed:{user_id}{page}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    items = load_feed(user_id, page)
    cache.set(key, items, ttl=60)
    return items
''', 'key = f"feed:{user_id}{page}"', "security",
         "Ambiguous key: user 1 page 12 and user 11 page 2 share a cache entry (data leak)"),

    case("30_n_plus_one", "app/orders.py", '''
from app.db import session
from app.models import Customer, Order
''', '''
def orders_with_customers(limit: int = 500) -> list[dict]:
    orders = session.query(Order).order_by(Order.created_at.desc()).limit(limit).all()
    out = []
    for order in orders:
        customer = session.query(Customer).get(order.customer_id)
        out.append({"order": order.id, "customer": customer.name})
    return out
''', "customer = session.query(Customer).get(order.customer_id)", "performance",
         "One query per order (N+1): 501 queries for 500 orders"),
]
