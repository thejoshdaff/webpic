#!/usr/bin/env python3
# mail_scrape_bot.py — minimal: reads unseen emails, scrapes first URL on page for images,
# zips them and replies attaching the zip. Configure via env vars.

import os, re, imaplib, email, tempfile, zipfile, smtplib, hashlib, time
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# Optional: loads .env if python-dotenv installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

EMAIL_USER = "thejoshdaff@outlook.com"
EMAIL_PASS = "oiuvuutgjexysypt"
IMAP_HOST  = "outlook.office365.com"
IMAP_PORT  = int("993")
SMTP_HOST  = "smtp.office365.com"
SMTP_PORT  = int("587")
if not EMAIL_USER or not EMAIL_PASS:
    print("ERROR: Set EMAIL_USER and EMAIL_PASS environment variables (or create a .env).")
    raise SystemExit(1)

URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.I)
USER_AGENT = "SimpleMailScrapeBot/1.0"
MIN_BYTES = 1024
MAX_IMAGES = 30

def decode_subject(msg):
    return str(make_header(decode_header(msg.get("Subject",""))))

def extract_sender(from_hdr):
    m = re.search(r"[\w\.-]+@[\w\.-]+", from_hdr or "")
    return m.group(0) if m else None

def get_text_parts(msg):
    texts=[]
    if msg.is_multipart():
        for p in msg.walk():
            c = p.get_content_type()
            d = str(p.get("Content-Disposition") or "")
            if c in ("text/plain","text/html") and "attachment" not in d:
                try:
                    payload = p.get_payload(decode=True)
                    if payload: texts.append(payload.decode(errors="ignore"))
                except: pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload: texts.append(payload.decode(errors="ignore"))
        except: pass
    return texts

def first_url_from_msg(msg):
    for t in get_text_parts(msg):
        m = URL_RE.search(t)
        if m: return m.group(0)
    return None

def fetch_html(url):
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code==200: return r.text
    except: pass
    return None

def extract_image_urls(base, html):
    soup = BeautifulSoup(html, "html.parser")
    urls=set()
    for img in soup.find_all("img"):
        s = img.get("src") or img.get("data-src")
        if s: urls.add(requests.compat.urljoin(base, s))
    for tag in soup.select("[style]"):
        for m in re.findall(r'url\(([^)]+)\)', tag.get("style","")):
            urls.add(requests.compat.urljoin(base, m.strip('\'" ')))
    return list(urls)

def download_images(urls, outdir, max_images=30):
    saved=[]
    seen=set()
    for u in urls:
        if len(saved)>=max_images: break
        try:
            r = requests.get(u, headers={"User-Agent": USER_AGENT}, timeout=30)
            if r.status_code!=200: continue
            data=r.content
            if len(data)<MIN_BYTES: continue
            h=hashlib.sha1(data).hexdigest()
            if h in seen: continue
            seen.add(h)
            name = Path(u).name or f"img_{h[:8]}"
            safe = re.sub(r'[^A-Za-z0-9_.-]','_',name)
            p = outdir / f"{h[:10]}_{safe}"
            p.write_bytes(data)
            saved.append(p)
            time.sleep(0.2)
        except: continue
    return saved

def zip_files(files, zip_path):
    with zipfile.ZipFile(zip_path,"w",zipfile.ZIP_DEFLATED) as z:
        for f in files: z.write(f, arcname=f.name)

def send_reply_with_zip(to_addr, subj, body, zip_path):
    em = EmailMessage()
    em["From"] = EMAIL_USER
    em["To"] = to_addr
    em["Subject"] = "Re: " + (subj or "")
    em.set_content(body)
    if zip_path and zip_path.exists():
        em.add_attachment(zip_path.read_bytes(), maintype="application", subtype="zip", filename=zip_path.name)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(EMAIL_USER, EMAIL_PASS)
            s.send_message(em)
        return True
    except Exception as e:
        print("SMTP send failed:", e); return False

def fetch_unseen():
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.login(EMAIL_USER, EMAIL_PASS)
    M.select("INBOX")
    typ,data = M.search(None,'(UNSEEN)')
    msgs=[]
    if typ=="OK":
        for num in data[0].split():
            t,m = M.fetch(num, '(RFC822)')
            if t=="OK":
                msgs.append((num, email.message_from_bytes(m[0][1])))
    return M,msgs

def mark_seen(mconn, num): 
    try: mconn.store(num, '+FLAGS', '\\Seen')
    except: pass

def main():
    M,msgs = fetch_unseen()
    if not msgs:
        print("No unseen messages"); M.logout(); return
    for num,msg in msgs:
        subj = decode_subject(msg); frm = msg.get("From",""); sender = extract_sender(frm)
        print("Processing:", sender, subj)
        url = first_url_from_msg(msg)
        if not url:
            print("No URL; skipping"); mark_seen(M,num); continue
        html = fetch_html(url)
        if not html:
            print("Failed to fetch page"); mark_seen(M,num); continue
        img_urls = extract_image_urls(url, html)
        if not img_urls:
            print("No images found"); mark_seen(M,num); continue
        with tempfile.TemporaryDirectory() as td:
            outdir = Path(td)/"images"; outdir.mkdir()
            saved = download_images(img_urls, outdir, MAX_IMAGES)
            if not saved:
                print("No images downloaded"); mark_seen(M,num); continue
            zip_path = Path(td)/"images.zip"; zip_files(saved, zip_path)
            ok = send_reply_with_zip(sender, subj, f"Images from {url}", zip_path)
            print("Sent:", ok)
        mark_seen(M,num)
    M.logout()

if __name__=="__main__":
    main()
