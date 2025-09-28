#!/usr/bin/env python3
# mail_scrape_send.py — lightweight version for Actions
import os, re, imaplib, email, tempfile, zipfile, base64, sys, asyncio, hashlib, urllib.parse
from email.header import decode_header, make_header
from pathlib import Path
from bs4 import BeautifulSoup
import aiohttp, async_timeout
from urllib import robotparser
import resend

# ENV / secrets
IMAP_HOST = os.getenv("IMAP_HOST", "outlook.office365.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", 993))
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
RESEND_FROM = os.getenv("RESEND_FROM")

# Basic checks
if not (EMAIL_USER and EMAIL_PASS and RESEND_API_KEY and RESEND_FROM):
    print("Missing required env vars: EMAIL_USER, EMAIL_PASS, RESEND_API_KEY, RESEND_FROM", file=sys.stderr)
    sys.exit(1)

resend.api_key = RESEND_API_KEY

USER_AGENT = "GitHubImageScraperBot/1.0"
URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.I)
MIN_BYTES = 1024
MAX_IMAGES = 200
POLITE_DELAY = 0.2

def decode_subject(msg):
    return str(make_header(decode_header(msg.get("Subject",""))))

def extract_sender(field):
    m = re.search(r"[\w\.-]+@[\w\.-]+", field or "")
    return m.group(0) if m else None

def first_url_from_msg(msg):
    parts = []
    if msg.is_multipart():
        for p in msg.walk():
            ctype = p.get_content_type()
            disp = str(p.get("Content-Disposition") or "")
            if ctype in ("text/plain","text/html") and "attachment" not in disp:
                try:
                    parts.append(p.get_payload(decode=True).decode(errors="ignore"))
                except: pass
    else:
        try: parts.append(msg.get_payload(decode=True).decode(errors="ignore"))
        except: pass
    for txt in parts:
        m = URL_RE.search(txt)
        if m: return m.group(0)
    return None

def robots_allow(url):
    try:
        parsed = urllib.parse.urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        rp = robotparser.RobotFileParser()
        rp.set_url(urllib.parse.urljoin(base, "/robots.txt"))
        rp.read()
        return rp.can_fetch(USER_AGENT, url)
    except:
        return True

async def fetch_text(session, url):
    try:
        with async_timeout.timeout(20):
            async with session.get(url, headers={"User-Agent": USER_AGENT}) as r:
                if r.status==200:
                    return await r.text(errors="ignore")
    except: pass
    return None

def extract_image_urls(base, html):
    soup = BeautifulSoup(html, "html.parser")
    urls = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src: urls.add(urllib.parse.urljoin(base, src))
    for tag in soup.select("[style]"):
        style = tag.get("style","")
        for m in re.findall(r'url\(([^)]+)\)', style):
            u = m.strip('\'" ')
            urls.add(urllib.parse.urljoin(base, u))
    return urls

async def download_one(session, url, out_dir, seen):
    if not robots_allow(url): 
        print("robots blocked", url); return None
    try:
        with async_timeout.timeout(30):
            async with session.get(url, headers={"User-Agent": USER_AGENT}) as r:
                if r.status!=200: return None
                data = await r.read()
                if len(data) < MIN_BYTES: return None
                h = hashlib.sha1(data).hexdigest()
                if h in seen: return None
                seen.add(h)
                name = Path(urllib.parse.urlparse(url).path).name or f"img_{h[:8]}"
                safe = re.sub(r'[^a-zA-Z0-9_.-]', '_', name)
                out = out_dir / f"{h[:10]}_{safe}"
                out.write_bytes(data)
                return out
    except:
        return None

async def scrape_images(url, tmpdir):
    if not robots_allow(url):
        print("robots blocked page"); return None
    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(limit_per_host=6)
    out_dir = Path(tmpdir)/"images"
    out_dir.mkdir(parents=True, exist_ok=True)
    seen = set()
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as s:
        html = await fetch_text(s, url)
        if not html: return None
        img_urls = list(extract_image_urls(url, html))
        tasks = [download_one(s, u, out_dir, seen) for u in img_urls[:MAX_IMAGES]]
        res = await asyncio.gather(*tasks)
    saved = [r for r in res if r]
    if not saved: return None
    zip_path = Path(tmpdir)/"images.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out_dir.iterdir()):
            z.write(f, arcname=f.name)
    return zip_path

def fetch_unseen():
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.login(EMAIL_USER, EMAIL_PASS)
    M.select("INBOX")
    typ, data = M.search(None, '(UNSEEN)')
    msgs=[]
    if typ=="OK":
        for num in data[0].split():
            t, m = M.fetch(num, '(RFC822)')
            if t=="OK":
                msgs.append((num, email.message_from_bytes(m[0][1])))
    return M, msgs

def mark_seen(M, num):
    M.store(num, '+FLAGS', '\\Seen')

def send_via_resend(to, subject, text, attachment_path):
    if not attachment_path or not attachment_path.exists(): return False
    b64 = base64.b64encode(attachment_path.read_bytes()).decode()
    att = [{"filename": attachment_path.name, "content": b64}]
    payload = {"from": RESEND_FROM, "to":[to], "subject": subject, "html": f"<pre>{text}</pre>", "attachments": att}
    try:
        resp = resend.Emails.send(payload)
        print("Resend OK", resp)
        return True
    except Exception as e:
        print("Resend send failed:", e)
        return False

def main():
    M, msgs = fetch_unseen()
    if not msgs:
        print("No unseen messages"); M.logout(); return
    for num, msg in msgs:
        subj = decode_subject(msg)
        fr = msg.get("From","")
        sender = extract_sender(fr)
        print("Processing", sender, subj)
        url = first_url_from_msg(msg)
        if not url:
            print("No URL found; marking seen"); mark_seen(M, num); continue
        with tempfile.TemporaryDirectory() as td:
            zipf = asyncio.run(scrape_images(url, td))
            if zipf and zipf.exists():
                body = f"Images scraped from {url}"
                ok = send_via_resend(sender, "Scraped images", body, zipf)
                print("Sent?", ok)
            else:
                print("No images or zip.")
        mark_seen(M, num)
    M.logout()

if __name__=="__main__":
    main()
