"""Push scanner matches into the Notion "SDE I Job Tracker" database.

Needs a Notion internal-integration token in the NOTION_TOKEN environment variable (or in
state/notion_token.txt), and the database shared with that integration. Jobs that fail to sync
are kept in state/notion_pending.json and retried on the next run.

  python notion_sync.py --check            # verify token + database access
  python notion_sync.py --backfill 7       # push feed rows found in the last 7 days
"""
import argparse, csv, json, os, re, sys, time
from datetime import datetime, timedelta
from pathlib import Path

import requests

HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
DATA_SOURCE = CONFIG.get("notion_data_source", "")
# Tier A jobs at or above this score go in as "Interested" (the shortlist); set to null to turn off
SHORTLIST_MIN_SCORE = CONFIG.get("shortlist_min_score", 5)
PENDING = HERE / "state" / "notion_pending.json"
API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def token():
    t = os.environ.get("NOTION_TOKEN", "").strip()
    f = HERE / "state" / "notion_token.txt"
    if not t and f.exists():
        t = f.read_text(encoding="utf-8").strip()
    return t


def session():
    global DATA_SOURCE
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token()}", "Notion-Version": VERSION,
                      "Content-Type": "application/json"})
    # config may hold a database ID (from the database's link) instead of a data source ID: resolve it
    r = s.get(f"{API}/databases/{DATA_SOURCE}", timeout=30)
    if r.ok and r.json().get("data_sources"):
        DATA_SOURCE = r.json()["data_sources"][0]["id"]
    return s


def job_id_from_url(url):
    for pat in (r"/jobs/view/(\d+)", r"amazon\.jobs/\w+/jobs/(\d+)", r"gh_jid=(\d+)", r"/jobs/(\d+)",
                r"_([A-Z]*R?-?\d[\w-]*)$", r"/([0-9a-f-]{36})", r"/(\d{6,})$"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return url


def posted_date(posted, found_at):
    """Normalise the many 'posted' formats to YYYY-MM-DD (falls back to the found date)."""
    found = datetime.fromisoformat(found_at[:19]) if found_at else datetime.now()
    p = (posted or "").strip().lower()
    if m := re.match(r"(\d{4}-\d{2}-\d{2})", p):
        return m.group(1)
    if m := re.match(r"([a-z]{3})[a-z]*\s+(\d{1,2}),\s*(\d{4})", p):  # "October 21, 2026"
        return f"{m.group(3)}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    if "today" in p:
        return found.date().isoformat()
    if "yesterday" in p:
        return (found - timedelta(days=1)).date().isoformat()
    if m := re.search(r"(\d+)\+?\s*days? ago", p):  # Workday "Posted 3 Days Ago"
        return (found - timedelta(days=int(m.group(1)))).date().isoformat()
    return found.date().isoformat()


def shortlisted(job):
    if SHORTLIST_MIN_SCORE is None or job.get("tier") != "A":
        return False
    try:
        return float(job.get("score")) >= SHORTLIST_MIN_SCORE
    except (TypeError, ValueError):
        return False


def properties(job):
    title = job["title"].strip()
    stretch = re.search(r"\bii\b|\b2\b|sde-?2", title, re.I)
    exp = f"{job['min_years']}+ yrs" if str(job.get("min_years", "")).strip() != "" else ""
    company = re.sub(r"[,]", "", job["company"]).strip()[:100] or "Unknown"
    props = {
        "Title": {"title": [{"text": {"content": title[:200]}}]},
        "Company": {"select": {"name": company}},
        "Location": {"rich_text": [{"text": {"content": (job.get("location") or "")[:200]}}]},
        "Level": {"select": {"name": "SDE II" if stretch else "SDE I"}},
        "Experience": {"rich_text": [{"text": {"content": exp}}]},
        "Posted Date": {"date": {"start": posted_date(job.get("posted"), job.get("found_at_ist"))}},
        "Link": {"url": job["url"]},
        "Job ID": {"rich_text": [{"text": {"content": str(job.get("id") or job_id_from_url(job["url"]))[:200]}}]},
        "Status": {"status": {"name": "Interested" if shortlisted(job) else "New"}},
        "Source": {"select": {"name": job.get("source", "linkedin")}},
    }
    if job.get("tier") in ("A", "B"):
        props["Tier"] = {"select": {"name": job["tier"]}}
    if str(job.get("score", "")).strip() != "":
        props["Score"] = {"number": float(job["score"])}
    return props


def exists(s, job):
    jid = str(job.get("id") or job_id_from_url(job["url"]))
    r = s.post(f"{API}/data_sources/{DATA_SOURCE}/query", timeout=30, json={"page_size": 1, "filter": {"or": [
        {"property": "Job ID", "rich_text": {"equals": jid}},
        {"property": "Link", "url": {"equals": job["url"]}}]}})
    r.raise_for_status()
    return bool(r.json().get("results"))


def push(jobs, log=print):
    """Add jobs (plus anything left from earlier failed runs) to Notion. Returns count added."""
    pending = json.loads(PENDING.read_text(encoding="utf-8")) if PENDING.exists() else []
    queue = pending + [{k: j.get(k, "") for k in ("id", "company", "title", "location", "posted", "url", "source",
                                                   "tier", "score", "min_years", "found_at_ist")} for j in jobs]
    if not queue:
        return 0
    if not token() or not DATA_SOURCE:
        PENDING.parent.mkdir(exist_ok=True)
        PENDING.write_text(json.dumps(queue[-500:]), encoding="utf-8")
        log(f"notion: token or notion_data_source not set, {len(queue)} jobs queued")
        return 0
    s, added, failed = session(), 0, []
    for job in queue:
        try:
            if not exists(s, job):
                r = s.post(f"{API}/pages", timeout=30,
                           json={"parent": {"type": "data_source_id", "data_source_id": DATA_SOURCE},
                                 "properties": properties(job)})
                if r.status_code == 400:  # bad data for this one row: log it and drop it
                    log(f"notion: rejected {job['company']} / {job['title']}: {r.text[:150]}")
                    continue
                r.raise_for_status()
                added += 1
            time.sleep(0.35)  # Notion allows ~3 requests/second
        except Exception as e:
            failed.append(job)
            if len(failed) == 1:
                log(f"notion: {type(e).__name__} {str(e)[:120]}")
    PENDING.parent.mkdir(exist_ok=True)
    PENDING.write_text(json.dumps(failed[-500:]), encoding="utf-8")
    log(f"notion: added {added}, already there {len(queue) - added - len(failed)}, retry later {len(failed)}")
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--backfill", type=int, metavar="DAYS")
    ap.add_argument("--tier", default="AB", help="tiers to backfill, e.g. A")
    a = ap.parse_args()
    if not token():
        sys.exit("NOTION_TOKEN is not set. See README.md > Notion sync.")
    if a.check:
        r = session().post(f"{API}/data_sources/{DATA_SOURCE}/query", json={"page_size": 1}, timeout=30)
        print("OK: database reachable" if r.ok else f"FAILED {r.status_code}: {r.text[:300]}")
        return
    if a.backfill:
        cutoff = (datetime.now() - timedelta(days=a.backfill)).isoformat()
        rows = [r for r in csv.DictReader(open(HERE / "jobs_feed.csv", encoding="utf-8"))
                if r["found_at_ist"] >= cutoff and r["tier"] in a.tier]
        print(f"backfilling {len(rows)} rows")
        push(rows)


if __name__ == "__main__":
    main()
