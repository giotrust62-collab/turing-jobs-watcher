"""
Turing jobs watcher.
Polls work.turing.com for new job posts and sends each new one to Telegram.

Env vars:
  TELEGRAM_BOT_TOKEN   required
  TELEGRAM_CHAT_ID     required
  POLL_MINUTES         optional, default 15. Set 0 to run once and exit (for cron).
  PAGE_SIZE            optional, default 50
  STATE_FILE           optional, default seen_jobs.json
  KEYWORDS             optional, comma separated. If set, only jobs whose title,
                       one-liner, role group or skills contain one of them are sent.
  SEND_EXISTING        optional, set to 1 to send current jobs on the very first run
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

BASE = "https://work.turing.com"
API = f"{BASE}/api/jobs/all"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
POLL_MINUTES = float(os.environ.get("POLL_MINUTES", "15"))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "50"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_jobs.json"))
KEYWORDS = [k.strip().lower() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]
SEND_EXISTING = os.environ.get("SEND_EXISTING") == "1"


def fetch_with_requests():
    """Fast path. Works if the site's bot protection lets plain requests through."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json", "Origin": BASE, "Referer": f"{BASE}/jobs"})
    s.get(f"{BASE}/jobs", timeout=15)
    r = s.post(API, json={"pageSize": PAGE_SIZE}, timeout=15)
    r.raise_for_status()
    return r.json()["jobs"]


def fetch_with_browser():
    """Fallback. Loads the page in headless Chromium so bot-protection cookies are set,
    then calls the same endpoint from inside the page."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        # networkidle never settles on this site (analytics pings), so only wait for the
        # document, then give the bot-protection script a few seconds to set its cookies.
        page.goto(f"{BASE}/jobs", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
        data = page.evaluate(
            """async (size) => {
                const r = await fetch('/api/jobs/all', {
                    method: 'POST',
                    headers: {'content-type': 'application/json'},
                    body: JSON.stringify({pageSize: size})
                });
                return await r.json();
            }""",
            PAGE_SIZE,
        )
        browser.close()
    return data["jobs"]


def fetch_jobs():
    try:
        return fetch_with_requests()
    except Exception as e:
        print(f"requests path failed ({e}), using browser fallback", file=sys.stderr)
        return fetch_with_browser()


def load_seen():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return None


def save_seen(ids):
    STATE_FILE.write_text(json.dumps(sorted(ids)))


def matches(job):
    if not KEYWORDS:
        return True
    skills = " ".join(s.get("skillName", "") for s in job.get("skills", []))
    hay = f"{job.get('title','')} {job.get('oneLinerDescription','')} {job.get('roleGroup','')} {skills}".lower()
    return any(k in hay for k in KEYWORDS)


def esc(t):
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(t or ""))


def format_job(job):
    skills = ", ".join(s["skillName"] for s in job.get("skills", [])[:6])
    link = f"{BASE}/jobs?jobId={job['id']}"
    lines = [
        f"*{esc(job.get('title'))}*",
        esc(job.get("oneLinerDescription")),
        f"Group: {esc(job.get('roleGroup'))} \\| {esc(job.get('locationType'))}",
    ]
    if skills:
        lines.append(f"Skills: {esc(skills)}")
    lines.append(f"Posted: {esc(job.get('createdDate'))}")
    lines.append(esc(link))
    return "\n".join(lines)


def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "MarkdownV2", "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        print(f"telegram error {r.status_code}: {r.text}", file=sys.stderr)
    r.raise_for_status()


def run_once():
    jobs = fetch_jobs()
    ids = {j["id"] for j in jobs}
    seen = load_seen()

    if seen is None:
        # First run: remember what exists so you are not flooded.
        save_seen(ids)
        print(f"first run, recorded {len(ids)} existing jobs")
        if not SEND_EXISTING:
            return
        seen = set()

    new = sorted((j for j in jobs if j["id"] not in seen), key=lambda j: j["id"])
    for job in new:
        if matches(job):
            send_telegram(format_job(job))
            time.sleep(1)
        seen.add(job["id"])
    save_seen(seen | ids)
    print(f"{len(new)} new jobs found")


def main():
    if not TOKEN or not CHAT_ID:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"run failed: {e}", file=sys.stderr)
            if POLL_MINUTES <= 0:
                sys.exit(1)
        if POLL_MINUTES <= 0:
            break
        time.sleep(POLL_MINUTES * 60)


if __name__ == "__main__":
    main()
