"""
Email client to fetch verification codes from Gmail/IMAP.
"""
import imaplib
import email
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

_CODE_PATTERNS = [
    re.compile(r"\b([A-Za-z0-9]{4}[-\s]?[A-Za-z0-9]{4})\b"),
]

def _extract_code(text: str) -> Optional[str]:
    if not text:
        return None
    compact = re.sub(r"\s+", " ", text)
    lower = compact.lower()
    anchors = [
        "copy and paste this code",
        "copy and paste the code",
        "security code field",
        "enter the security code",
        "your security code",
    ]
    stop_phrases = [
        "after you enter the code",
        "after you enter your code",
        "after you enter the security code",
    ]
    for anchor in anchors:
        start_idx = 0
        while True:
            idx = lower.find(anchor, start_idx)
            if idx == -1:
                break
            window = compact[idx:idx + 240]
            window_lower = window.lower()
            for stop in stop_phrases:
                stop_idx = window_lower.find(stop)
                if stop_idx != -1:
                    window = window[:stop_idx]
                    break
            for pat in _CODE_PATTERNS:
                for match in pat.finditer(window):
                    raw = match.group(1)
                    code = re.sub(r"[-\s]", "", raw)
                    if len(code) == 8:
                        return code
            start_idx = idx + len(anchor)
    return None

def _clean_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("=\r\n", "")
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"&nbsp;|&#160;", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

def _select_folder(mail, folder: str) -> bool:
    try:
        status, _ = mail.select(folder)
        return status == "OK"
    except Exception:
        return False

def fetch_greenhouse_code(user: str, password: str, timeout: int = 60, after_time: Optional[datetime] = None) -> Optional[str]:
    """
    Search for the latest 8-character verification code from Greenhouse.
    """
    if not user or not password:
        return None

    password = "".join(password.split())
    mail = None
    try:
        start_time = time.time()
        since_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%d-%b-%Y")
        while time.time() - start_time < timeout:
            try:
                # 1. Connect and login
                mail = imaplib.IMAP4_SSL("imap.gmail.com")
                mail.login(user, password)
                # 2. Search for emails from Greenhouse
                # Note: RFC2060 SEARCH strings. Prefer UNSEEN, fallback to recent seen.
                search_queries = [
                    '(UNSEEN FROM "no-reply@greenhouse.io")',
                    '(UNSEEN FROM "no-reply@greenhouse-mail.io")',
                    '(UNSEEN FROM "no-reply@us.greenhouse-mail.io")',
                    f'(FROM "no-reply@greenhouse.io" SINCE "{since_date}")',
                    f'(FROM "no-reply@greenhouse-mail.io" SINCE "{since_date}")',
                    f'(FROM "no-reply@us.greenhouse-mail.io" SINCE "{since_date}")',
                    f'(FROM "greenhouse.io" SINCE "{since_date}")',
                    f'(FROM "greenhouse-mail.io" SINCE "{since_date}")',
                ]
                gm_raw_queries = [
                    "from:greenhouse.io newer_than:2d",
                    "from:greenhouse-mail.io newer_than:2d",
                    "subject:\"verification code\" newer_than:2d",
                    "subject:\"security code\" newer_than:2d",
                ]

                ids = []
                folders = ["INBOX", '"[Gmail]/All Mail"', '"[Google Mail]/All Mail"']
                for folder in folders:
                    if not _select_folder(mail, folder):
                        continue
                    for q in search_queries:
                        _, data = mail.search(None, q)
                        cand = data[0].split() if data and data[0] else []
                        if cand:
                            ids = cand
                            break
                    if ids:
                        break
                    for q in gm_raw_queries:
                        try:
                            _, data = mail.search(None, "X-GM-RAW", q)
                        except Exception:
                            continue
                        cand = data[0].split() if data and data[0] else []
                        if cand:
                            ids = cand
                            break
                    if ids:
                        break

                if not ids:
                    mail.logout()
                    mail = None
                    time.sleep(5) # Poll every 5s
                    continue

                # 3. Scan newest messages for a code after submit time
                for msg_id in reversed(ids[-30:]):
                    _, msg_data = mail.fetch(msg_id, "(RFC822 INTERNALDATE)")
                    msg = None
                    internal_dt = None
                
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            meta = response_part[0].decode(errors="ignore") if isinstance(response_part[0], bytes) else str(response_part[0])
                            try:
                                internal_tuple = imaplib.Internaldate2tuple(meta)
                            except Exception:
                                internal_tuple = None
                            if internal_tuple:
                                internal_dt = datetime.fromtimestamp(time.mktime(internal_tuple), timezone.utc)
                            msg = email.message_from_bytes(response_part[1])
                        
                    if msg is None:
                        continue

                    if after_time:
                        if internal_dt:
                            if internal_dt < after_time:
                                continue
                        else:
                            try:
                                date_hdr = msg.get("Date")
                                parsed = parsedate_to_datetime(date_hdr) if date_hdr else None
                                if parsed and parsed.tzinfo is None:
                                    parsed = parsed.replace(tzinfo=timezone.utc)
                                if parsed and parsed < after_time:
                                    continue
                            except Exception:
                                continue

                    subject_raw = msg.get("Subject", "") or ""
                    try:
                        subject = str(make_header(decode_header(subject_raw)))
                    except Exception:
                        subject = subject_raw

                    # Extract body
                    body_text = ""
                    body_html = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            ctype = part.get_content_type()
                            if ctype == "text/plain" and not body_text:
                                body_text = part.get_payload(decode=True).decode(errors="ignore")
                            elif ctype == "text/html" and not body_html:
                                body_html = part.get_payload(decode=True).decode(errors="ignore")
                    else:
                        body_text = msg.get_payload(decode=True).decode(errors="ignore")

                    candidates = []
                    if body_text and body_text.strip():
                        candidates.append(body_text)
                    if body_html and body_html.strip():
                        candidates.append(body_html)

                    code = None
                    for candidate in candidates:
                        body_clean = _clean_text(candidate)
                        code = _extract_code(body_clean)
                        if code:
                            break
                    if code:
                        # Mark as seen
                        mail.store(msg_id, '+FLAGS', '\\Seen')
                        mail.logout()
                        return code

                mail.logout()
                mail = None
            except Exception:
                if mail:
                    try:
                        mail.logout()
                    except:
                        pass
                    mail = None
            
            time.sleep(5)
    except Exception:
        pass
    finally:
        if mail:
            try:
                mail.logout()
            except:
                pass

    return None
