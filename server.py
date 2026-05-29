import imaplib
import email
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from flask import Flask, jsonify, request, send_from_directory

from pymongo import MongoClient

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── MongoDB init ──────────────────────────────────────────
MONGODB_URI           = os.environ.get("MONGODB_URI", "mongodb+srv://amazonshiftgraber_db_user:ps4acPydaJEorEte@amazonca.gjcebw.mongodb.net/")
MONGODB_DB_NAME       = os.environ.get("MONGODB_DB_NAME", "amazon_shift_new")
MONGODB_MAX_POOL_SIZE = int(os.environ.get("MONGODB_MAX_POOL_SIZE", "100"))

_mongo_client = MongoClient(MONGODB_URI, maxPoolSize=MONGODB_MAX_POOL_SIZE)
db = _mongo_client[MONGODB_DB_NAME]


def fetch_accounts_from_mongo():
    """Fetch every doc from 'customers' collection fresh every call."""
    accounts = []
    for doc in db["customers"].find({}):
        email_addr = (doc.get("email_lower") or "").strip().lower()
        password   = (doc.get("gmail_imap_app_password") or "").strip()
        if email_addr:
            accounts.append({
                "doc_id":   str(doc["_id"]),
                "email":    email_addr,
                "password": password,
                "label":    doc.get("label") or doc.get("name") or email_addr.split("@")[0],
            })
    return accounts


# ── Email parsing helpers ─────────────────────────────────
def decode_str(value):
    if value is None:
        return ""
    parts = decode_header(value)
    result = []
    for raw, charset in parts:
        if isinstance(raw, bytes):
            result.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(raw)
    return "".join(result)


def get_body(msg):
    """Returns (plain_text, html) tuple. html is None if no HTML part found."""
    plain, html = None, None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd:
                continue
            if ct == "text/plain" and plain is None:
                charset = part.get_content_charset() or "utf-8"
                plain = part.get_payload(decode=True).decode(charset, errors="replace")
            elif ct == "text/html" and html is None:
                charset = part.get_content_charset() or "utf-8"
                html = part.get_payload(decode=True).decode(charset, errors="replace")
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            ct = msg.get_content_type()
            decoded = payload.decode(charset, errors="replace")
            if ct == "text/html":
                html = decoded
            else:
                plain = decoded
    return plain or "", html


def build_imap_or(senders):
    """Nest IMAP OR clauses for any number of senders."""
    if len(senders) == 1:
        return f'FROM "{senders[0]}"'
    if len(senders) == 2:
        return f'(OR FROM "{senders[0]}" FROM "{senders[1]}")'
    return f'(OR FROM "{senders[0]}" {build_imap_or(senders[1:])})'


# ── Routes ────────────────────────────────────────────────
SETTINGS_COLLECTION = "gmail_panel_setting"
SETTINGS_DOC        = "senders"


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/settings", methods=["GET"])
def get_settings():
    try:
        doc = db[SETTINGS_COLLECTION].find_one({"_id": SETTINGS_DOC})
        if doc:
            doc.pop("_id", None)
            return jsonify(doc)
        return jsonify({"watched_senders": []})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/settings", methods=["POST"])
def save_settings():
    try:
        data = request.json or {}
        data["_id"] = SETTINGS_DOC
        db[SETTINGS_COLLECTION].replace_one({"_id": SETTINGS_DOC}, data, upsert=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/accounts")
def get_accounts():
    """Fetch accounts fresh from MongoDB every time — no caching."""
    try:
        accounts = fetch_accounts_from_mongo()
        # Never expose passwords to frontend
        safe = [{"email": a["email"], "label": a["label"]} for a in accounts]
        safe.sort(key=lambda x: x["email"])
        return jsonify(safe)
    except Exception as e:
        return jsonify({"error": f"MongoDB error: {str(e)}"}), 503


@app.route("/fetch", methods=["POST"])
def fetch_emails():
    data       = request.json or {}
    acct_email = data.get("email", "").strip().lower()
    days       = max(1, int(data.get("days", 3)))
    senders    = data.get("senders", [])

    if not acct_email:
        return jsonify({"error": "No account email provided"}), 400
    if not senders:
        return jsonify({"error": "No sender filters configured"}), 400

    # Fetch credentials fresh from MongoDB
    try:
        accounts = fetch_accounts_from_mongo()
    except Exception as e:
        return jsonify({"error": f"MongoDB error: {str(e)}"}), 503

    acct = next((a for a in accounts if a["email"] == acct_email), None)
    if not acct:
        return jsonify({"error": f"Account '{acct_email}' not found in MongoDB."}), 404

    password = acct["password"]
    if not password:
        return jsonify({"error": f"No IMAP password stored for {acct_email}."}), 400

    # Strip invisible/non-ASCII characters that break IMAP ASCII requirement
    clean_senders = [re.sub(r'[^\x20-\x7E]', '', s).strip() for s in senders if s]
    clean_senders = [s for s in clean_senders if s]
    if not clean_senders:
        return jsonify({"error": "Sender emails contain only invalid characters after cleaning."}), 400

    since_date   = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    from_clause  = build_imap_or(clean_senders)
    search_str   = f'({from_clause} SINCE "{since_date}")'

    try:
        # ── Connect, search, disconnect ──
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        imap.login(acct_email, password)
        imap.select("INBOX")

        status, data_raw = imap.search(None, search_str)
        imap.logout()

        if status != "OK":
            return jsonify([])

        uid_list = data_raw[0].split()
        if not uid_list:
            return jsonify([])

        # ── Connect, fetch, disconnect ──
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        imap.login(acct_email, password)
        imap.select("INBOX")

        results = []
        for uid in uid_list[-100:]:   # latest 100 max
            try:
                st, msg_data = imap.fetch(uid, "(RFC822 FLAGS)")
                if st != "OK":
                    continue

                raw_email = msg_data[0][1]
                flags     = str(msg_data[0][0])
                msg       = email.message_from_bytes(raw_email)

                subject   = decode_str(msg.get("Subject", "(no subject)"))
                from_raw  = decode_str(msg.get("From", ""))
                date_raw  = msg.get("Date", "")
                body, body_html = get_body(msg)
                text_for_preview = body or re.sub(r"<[^>]+>", " ", body_html or "")
                preview   = " ".join(text_for_preview.split())[:120]

                # Parse sender name + email
                m = re.match(r"^(.*?)\s*<([^>]+)>$", from_raw.strip())
                if m:
                    from_name  = m.group(1).strip().strip('"')
                    from_email = m.group(2).strip().lower()
                else:
                    from_name  = from_raw.strip()
                    from_email = from_raw.strip().lower()

                # Parse date
                try:
                    dt           = parsedate_to_datetime(date_raw).astimezone()
                    date_iso     = dt.isoformat()
                    now          = datetime.now().astimezone()
                    if dt.date() == now.date():
                        display_time = dt.strftime("%-I:%M %p")
                    elif (now - dt).days < 7:
                        display_time = dt.strftime("%b %d, %-I:%M %p")
                    else:
                        display_time = dt.strftime("%b %d %Y, %-I:%M %p")
                except Exception:
                    date_iso     = date_raw
                    display_time = date_raw

                results.append({
                    "id":           uid.decode(),
                    "from":         from_email,
                    "from_name":    from_name,
                    "subject":      subject,
                    "preview":      preview,
                    "body":         body,
                    "body_html":    body_html,
                    "date":         date_iso,
                    "display_time": display_time,
                    "unread":       "\\Seen" not in flags,
                    "starred":      "\\Flagged" in flags,
                })
            except Exception:
                continue

        imap.logout()
        results.sort(key=lambda x: x["date"], reverse=True)
        return jsonify(results)

    except imaplib.IMAP4.error as e:
        msg = str(e)
        if "AUTHENTICATIONFAILED" in msg or "Invalid credentials" in msg:
            return jsonify({"error": "Authentication failed. Check the Gmail App Password in MongoDB."}), 401
        return jsonify({"error": f"IMAP error: {msg}"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 503


if __name__ == "__main__":
    print("Starting MyMailbox server on http://localhost:4242")
    app.run(host="0.0.0.0", port=4242, debug=False)
