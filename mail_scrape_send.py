#!/usr/bin/env python3
import os, re, imaplib, email, smtplib, tempfile, zipfile, requests
from email.message import EmailMessage
from email.utils import parseaddr
from bs4 import BeautifulSoup
from pathlib import Path

# Configuration (use env vars to override)
EMAIL_USER = os.getenv("EMAIL_USER", "thejoshdaff@outlook.com")
EMAIL_PASS = os.getenv("EMAIL_PASS", "oiuvuutgjexysypt")
IMAP_HOST = os.getenv("IMAP_HOST", "outlook.office365.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.office365.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.I)

def first_url_from_msg(msg):
    if msg.is_multipart():
        parts = [p.get_payload(decode=True) for p in msg.walk() if p.get_content_type() in ("text/plain","text/html")]
        text  = " ".join([p.decode(errors="ignore") for p in parts if p])
    else:
        text = msg.get_payload(decode=True).decode(errors="ignore")
    m = URL_RE.search(text or "")
    return m.group(0) if m else None

def extract_image_urls(base_url, html):
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for img in soup.find_all("img"):
        src = img.get("src")
        if src:
            urls.append(requests.compat.urljoin(base_url, src))
    return urls

def download_and_zip(urls, zip_path, limit=10):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for i,u in enumerate(urls[:limit]):
            try:
                r = requests.get(u, timeout=10)
                if r.ok:
                    fname = f"img{i}.jpg"
                    z.writestr(fname, r.content)
            except Exception:
                pass

def send_reply(to_addr, subj, body, zip_path):
    em = EmailMessage()
    em["From"] = EMAIL_USER
    em["To"]   = to_addr
    em["Subject"] = "Re: " + (subj or "")
    em.set_content(body)
    with open(zip_path,"rb") as f:
        em.add_attachment(f.read(), maintype="application", subtype="zip", filename="images.zip")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(EMAIL_USER, EMAIL_PASS)
        s.send_message(em)

def main():
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.login(EMAIL_USER, EMAIL_PASS)
    M.select("INBOX")
    typ, data = M.search(None, '(UNSEEN)')
    if typ!="OK" or not data[0]:
        print("No unseen messages")
        return

    for num in data[0].split():
        typ, msg_data = M.fetch(num, '(RFC822)')
        if typ!="OK": continue
        msg = email.message_from_bytes(msg_data[0][1])
        sender = parseaddr(msg.get("From"))[1]
        subj   = msg.get("Subject","")
        url = first_url_from_msg(msg)
        if not url: continue
        r = requests.get(url, timeout=10)
        if not r.ok: continue
        img_urls = extract_image_urls(url, r.text)
        if not img_urls: continue
        with tempfile.TemporaryDirectory() as td:
            zip_path = Path(td)/"images.zip"
            download_and_zip(img_urls, zip_path)
            send_reply(sender, subj, f"Images from {url}", zip_path)
        M.store(num, '+FLAGS', '\\Seen')

    M.logout()

if __name__=="__main__":
    main()
