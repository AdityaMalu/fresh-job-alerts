"""One-off: detect which ATS (Greenhouse/Lever/Ashby/SmartRecruiters) each candidate slug uses,
and how many India jobs it lists. Writes probe_result.json."""
import json, sys, concurrent.futures as cf, requests

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (job-scanner; personal use)"
INDIA = ("india", "bengaluru", "bangalore", "hyderabad", "pune", "gurgaon", "gurugram", "noida",
         "mumbai", "chennai", "delhi", "remote")

def gh(s):
    r = S.get(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs", timeout=15)
    if r.status_code != 200: return None
    j = r.json().get("jobs", [])
    return len(j), sum(any(k in (x.get("location") or {}).get("name", "").lower() for k in INDIA) for x in j)

def lever(s):
    r = S.get(f"https://api.lever.co/v0/postings/{s}?mode=json", timeout=15)
    if r.status_code != 200 or not isinstance(r.json(), list): return None
    j = r.json()
    return len(j), sum(any(k in ((x.get("categories") or {}).get("location") or "").lower() for k in INDIA) for x in j)

def ashby(s):
    r = S.get(f"https://api.ashbyhq.com/posting-api/job-board/{s}", timeout=15)
    if r.status_code != 200: return None
    j = r.json().get("jobs", [])
    return len(j), sum(any(k in (x.get("location") or "").lower() for k in INDIA) for x in j)

def sr(s):
    r = S.get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=100&country=in", timeout=15)
    if r.status_code != 200: return None
    d = r.json()
    if d.get("totalFound", 0) == 0: return None
    return d.get("totalFound"), d.get("totalFound")

FNS = {"greenhouse": gh, "lever": lever, "ashby": ashby, "smartrecruiters": sr}

def probe(slug):
    out = {}
    for name, fn in FNS.items():
        try:
            res = fn(slug)
            if res and res[0]: out[name] = {"total": res[0], "india": res[1]}
        except Exception:
            pass
    return slug, out

if __name__ == "__main__":
    slugs = sorted(set(l.strip().lower() for l in open(sys.argv[1]) if l.strip() and not l.startswith("#")))
    found = {}
    with cf.ThreadPoolExecutor(24) as ex:
        for slug, out in ex.map(probe, slugs):
            if out: found[slug] = out
    json.dump(found, open("probe_result.json", "w"), indent=1)
    print(f"{len(found)}/{len(slugs)} slugs matched")
    for s, o in sorted(found.items()):
        print(s, o)
