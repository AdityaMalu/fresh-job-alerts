"""Fresh-job scanner for SDE-1 roles in India.

Polls LinkedIn's public search (all companies, last ~2 hours), Amazon, and ~115 company
job boards (Greenhouse / Lever / Ashby / SmartRecruiters / Workday). New matching jobs are
appended to jobs_feed.csv and pushed to the phone via ntfy.sh within minutes of posting.

Usage:
  python scanner.py            # normal run (meant for every 15 minutes)
  python scanner.py --seed     # first run: mark everything currently open as seen, no pushes
  python scanner.py --dry-run  # print matches, change nothing
"""
import argparse, csv, html, json, os, random, re, sys, time
import concurrent.futures as cf
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

import notion_sync

HERE = Path(__file__).parent
if not (HERE / "config.json").exists():
    sys.exit("config.json not found: copy config.example.json to config.json and fill it in (see README).")
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
COMPANIES = json.loads((HERE / "companies.json").read_text(encoding="utf-8"))
STATE_FILE = HERE / "state" / "seen.json"
FEED_FILE = HERE / "jobs_feed.csv"
LOG_FILE = HERE / "state" / "scanner.log"
IST = timezone(timedelta(hours=5, minutes=30))
FEED_COLS = ["found_at_ist", "tier", "score", "company", "title", "location", "posted", "source", "url", "min_years", "status"]

S = requests.Session()
S.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

# ---------------------------------------------------------------- filters

INCLUDE = re.compile(CONFIG["title_include"], re.I)
EXCLUDE = re.compile(CONFIG["title_exclude"], re.I)
STRETCH = re.compile(CONFIG["title_stretch"], re.I)
LOC_OK = re.compile(CONFIG["location_include"], re.I)
BLOCKED = lambda company: any(b in company.lower() for b in CONFIG.get("company_block", []))
YEARS = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years|yrs)", re.I)


def location_ok(loc):
    return bool(loc) and bool(LOC_OK.search(loc))


def title_tier(title):
    """'A' = clean entry-level match, 'B' = stretch (e.g. Software Engineer II), None = drop."""
    if not INCLUDE.search(title) or EXCLUDE.search(title):
        return None
    return "B" if STRETCH.search(title) else "A"


PREFERRED = re.compile(r"prefer|nice to have|bonus|good to have|ideal|company|founded|customers", re.I)


def min_years(text):
    """Binding years-of-experience requirement: the largest lower bound among required (not
    'preferred') sentences, e.g. '3+ years dev ... 2+ years design' -> 3. None if not stated."""
    nums = []
    for sent in re.split(r"(?<=[.;\n•])\s+|\s-\s", text or ""):
        if PREFERRED.search(sent):
            continue
        nums += [int(m.group(1)) for m in YEARS.finditer(sent) if int(m.group(1)) <= 15]
    return max(nums) if nums else None


def score(job):
    text = f"{job['title']} {job.get('desc', '')}".lower()
    s = sum(w for kw, w in CONFIG["stack_weights"].items() if kw in text)
    if job["tier"] == "A":
        s += 5
    return s


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or ""))


# ---------------------------------------------------------------- sources
# Each source returns dicts: id, company, title, location, url, posted (str), desc (optional)

def src_linkedin(window_s):
    out = []
    for kw in CONFIG["linkedin_keywords"]:
        for loc in CONFIG["linkedin_locations"]:
            params = {"keywords": kw, "location": loc, "f_TPR": f"r{window_s}", "f_E": "2,3",
                      "f_JT": "F", "sortBy": "DD", "start": 0}
            if loc == "India":
                params["f_WT"] = "2"  # remote
            for start in (0, 25):
                params["start"] = start
                r = S.get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                          params=params, timeout=20)
                if r.status_code == 429:
                    log("linkedin: rate limited, stopping LinkedIn for this run")
                    return out
                if r.status_code != 200 or not r.text.strip():
                    break
                cards = r.text.split("<li")
                n = 0
                for c in cards:
                    m = re.search(r"urn:li:jobPosting:(\d+)", c)
                    if not m:
                        continue
                    n += 1
                    g = lambda pat: (re.search(pat, c, re.S) or [None, ""])[1]
                    out.append({
                        "id": m.group(1),
                        "title": strip_html(g(r'base-search-card__title[^>]*>(.*?)</h3>')).strip(),
                        "company": strip_html(g(r'base-search-card__subtitle[^>]*>(.*?)</h4>')).strip(),
                        "location": strip_html(g(r'job-search-card__location[^>]*>(.*?)</span>')).strip(),
                        "posted": g(r'datetime="([^"]+)"'),
                        "url": f"https://www.linkedin.com/jobs/view/{m.group(1)}/",
                    })
                time.sleep(random.uniform(1.0, 2.5))
                if n < 25:
                    break
    return out


def linkedin_desc(job_id):
    for attempt in range(2):
        r = S.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}", timeout=20)
        if r.status_code != 429:
            break
        time.sleep(5)
    if r.status_code != 200:
        return ""
    m = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', r.text, re.S)
    return strip_html(m.group(1)) if m else ""


def src_amazon(_):
    out = []
    for q in ("software development engineer", "software engineer"):
        r = S.get("https://www.amazon.jobs/en/search.json",
                  params={"base_query": q, "normalized_country_code[]": "IND", "sort": "recent",
                          "result_limit": 100}, timeout=30)
        for j in r.json().get("jobs", []):
            out.append({"id": j["id_icims"], "company": "Amazon", "title": j["title"],
                        "location": j.get("normalized_location") or j.get("location", ""),
                        "posted": j.get("posted_date", ""), "url": "https://www.amazon.jobs" + j["job_path"],
                        "desc": strip_html(j.get("basic_qualifications", ""))})
    return out


def src_greenhouse(slug):
    r = S.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", timeout=30)
    r.raise_for_status()
    return [{"id": j["id"], "company": slug, "title": j["title"], "location": (j.get("location") or {}).get("name", ""),
             "posted": j.get("first_published") or j.get("updated_at", ""), "url": j["absolute_url"],
             "desc": strip_html(html.unescape(j.get("content", "")))} for j in r.json().get("jobs", [])]


def src_lever(slug):
    r = S.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=30)
    r.raise_for_status()
    return [{"id": j["id"], "company": slug, "title": j["text"], "location": (j.get("categories") or {}).get("location") or "",
             "posted": datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).isoformat(), "url": j["hostedUrl"],
             "desc": j.get("descriptionPlain", "") + " " + " ".join(strip_html(l.get("content", "")) for l in j.get("lists", []))}
            for j in r.json()]


def src_ashby(slug):
    r = S.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations", [])]
        country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or ""
        if j.get("isRemote") and "india" in country.lower():
            locs.append("Remote India")
        out.append({"id": j["id"], "company": slug, "title": j["title"], "location": " / ".join(filter(None, locs)),
                    "posted": j.get("publishedAt", ""), "url": j["jobUrl"], "desc": j.get("descriptionPlain", "")})
    return out


def src_smartrecruiters(slug):
    r = S.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&country=in", timeout=30)
    r.raise_for_status()
    return [{"id": j["id"], "company": slug, "title": j["name"],
             "location": ", ".join(filter(None, [(j.get("location") or {}).get("city"), "India"])),
             "posted": j.get("releasedDate", ""), "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}"}
            for j in r.json().get("content", [])]


def src_workday(spec):
    tenant, n, site = spec
    base = f"https://{tenant}.wd{n}.myworkdayjobs.com"
    out = []
    for offset in (0, 20):
        r = S.post(f"{base}/wday/cxs/{tenant}/{site}/jobs", timeout=30,
                   json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "software engineer India"})
        r.raise_for_status()
        posts = r.json().get("jobPostings", [])
        for j in posts:
            if "externalPath" not in j:
                continue
            out.append({"id": j["externalPath"], "company": tenant, "title": j.get("title", ""),
                        "location": j.get("locationsText", ""), "posted": j.get("postedOn", ""),
                        "url": f"{base}/en-US/{site}{j['externalPath']}"})
        if len(posts) < 20:
            break
    return out


def workday_desc(job):
    m = re.match(r"(https://([^.]+)\.wd\d+\.myworkdayjobs\.com)/en-US/([^/]+)(/.*)", job["url"])
    if not m:
        return ""
    base, tenant, site, path = m.groups()
    r = S.get(f"{base}/wday/cxs/{tenant}/{site}{path}", timeout=20)
    return strip_html(r.json().get("jobPostingInfo", {}).get("jobDescription", "")) if r.ok else ""


# ---------------------------------------------------------------- state / output

def log(msg):
    line = f"{datetime.now(IST):%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    LOG_FILE.parent.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen": {}, "fingerprints": {}}


def save_state(state):
    # forget entries older than 45 days so the file stays small
    cutoff = (datetime.now(IST) - timedelta(days=45)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    state["fingerprints"] = {k: v for k, v in state["fingerprints"].items() if v >= cutoff}
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def fingerprint(job):
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    return norm(job["company"])[:20] + "|" + norm(job["title"])


def append_feed(jobs):
    new = not FEED_FILE.exists()
    with FEED_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FEED_COLS, extrasaction="ignore")
        if new:
            w.writeheader()
        for j in jobs:
            w.writerow(j)


def notify(jobs):
    topic = os.environ.get("NTFY_TOPIC") or CONFIG.get("ntfy_topic")
    if not topic or not jobs:
        return
    url = f"https://ntfy.sh/{topic}"
    if len(jobs) <= 4:
        for j in jobs:
            S.post(url, data=f"{j['location']} · {j['source']} · min {j['min_years'] or '?'} yrs".encode(),
                   headers={"Title": f"[{j['tier']}] {j['company']}: {j['title']}"[:200].encode("utf-8"),
                            "Click": j["url"], "Priority": "high" if j["tier"] == "A" else "default",
                            "Tags": "briefcase"}, timeout=15)
    else:
        body = "\n".join(f"[{j['tier']}] {j['company']} - {j['title']} ({j['location']})" for j in jobs[:15])
        S.post(url, data=body.encode(), headers={"Title": f"{len(jobs)} new SDE-1 jobs", "Priority": "high",
                                                 "Tags": "briefcase"}, timeout=15)


# ---------------------------------------------------------------- main

def collect(window_s):
    tasks = [("linkedin", src_linkedin, window_s), ("amazon", src_amazon, None)]
    for kind, fn in (("greenhouse", src_greenhouse), ("lever", src_lever), ("ashby", src_ashby),
                     ("smartrecruiters", src_smartrecruiters), ("workday", src_workday)):
        tasks += [(kind, fn, slug) for slug in COMPANIES[kind]]
    jobs, errors = [], []

    def run(t):
        kind, fn, arg = t
        try:
            return kind, arg, fn(arg), None
        except Exception as e:
            return kind, arg, [], f"{kind}:{arg if isinstance(arg, str) else ''} {type(e).__name__} {str(e)[:80]}"

    with cf.ThreadPoolExecutor(16) as ex:
        for kind, arg, res, err in ex.map(run, tasks):
            if err:
                errors.append(err)
            for j in res:
                j["source"] = kind
                j["key"] = f"{kind}:{j['company'] if kind != 'linkedin' else ''}:{j['id']}"
                jobs.append(j)
    return jobs, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--window", type=int, default=CONFIG.get("linkedin_window_seconds", 7200))
    a = ap.parse_args()

    t0 = time.time()
    state = load_state()
    now = datetime.now(IST).isoformat(timespec="seconds")
    jobs, errors = collect(86400 if a.seed else a.window)

    fresh, run_keys, run_fps = [], set(), set()
    for j in jobs:
        if j["key"] in state["seen"] or j["key"] in run_keys:
            continue
        run_keys.add(j["key"])
        if not a.dry_run:
            state["seen"][j["key"]] = now
        tier = title_tier(j["title"])
        if not tier or not location_ok(j["location"]) or BLOCKED(j["company"]):
            continue
        fp = fingerprint(j)
        if fp in state["fingerprints"] or fp in run_fps:  # same role already seen via another source
            continue
        run_fps.add(fp)
        if not a.dry_run:
            state["fingerprints"][fp] = now
        j["tier"] = tier
        fresh.append(j)

    # fetch descriptions only for the few new matches, then apply the years-of-experience rule
    keep = []
    for j in fresh:
        if not j.get("desc") and not a.seed:
            try:
                if j["source"] == "linkedin":
                    j["desc"] = linkedin_desc(j["id"])
                    time.sleep(random.uniform(0.8, 1.6))
                elif j["source"] == "workday":
                    j["desc"] = workday_desc(j)
            except Exception:
                j["desc"] = ""
        y = min_years(j["title"] + ". " + j.get("desc", ""))
        j["min_years"] = "" if y is None else y
        if y is not None and y >= CONFIG["max_min_years_drop"]:
            continue
        if y is not None and y >= 2 and j["tier"] == "A":
            j["tier"] = "B"
        j["score"] = score(j)
        j["found_at_ist"] = now
        j["company"] = COMPANIES["display_names"].get(j["company"].lower().replace(" ", ""),
                                                       j["company"].title() if j["company"].islower() else j["company"])
        j["status"] = "seeded" if a.seed else "new"
        keep.append(j)
    keep.sort(key=lambda j: (j["tier"], -j["score"]))

    if a.dry_run:
        for j in keep:
            print(f"[{j['tier']} {j['score']:>2}] {j['company']} | {j['title']} | {j['location']} | {j['posted']} | {j['url']}")
    else:
        append_feed(keep)
        save_state(state)
        if not a.seed:
            notify(keep)
            try:
                notion_sync.push(keep, log)
            except Exception as e:
                log(f"notion: sync crashed {type(e).__name__} {str(e)[:120]}")
    log(f"run{' seed' if a.seed else ''}{' dry' if a.dry_run else ''}: {len(jobs)} listings, {len(keep)} new matches "
        f"(A={sum(j['tier'] == 'A' for j in keep)}), {len(errors)} source errors, {time.time() - t0:.0f}s")
    for e in errors[:10]:
        log("  err " + e)


if __name__ == "__main__":
    sys.exit(main())
