# Fresh-job scanner

Get a phone notification within ~15 minutes of a matching job being posted, so you can apply before the first 200 applicants do.

Every run, the scanner:

1. Searches LinkedIn's public job search (**every company**) for roles posted in the last 2 hours.
2. Checks Amazon and ~115 company job boards directly (Greenhouse, Lever, Ashby, SmartRecruiters, Workday). These often list a job before LinkedIn does.
3. Filters by title, location and years of experience (it reads the job description), and drops anything it has already seen.
4. Sends each new match to your phone through [ntfy](https://ntfy.sh) (tap to open the apply page), adds it to `jobs_feed.csv`, and optionally adds it to a Notion database.

No login, API key or paid service is needed for the scanning itself. Out of the box it targets entry-level (0–2 years) backend roles in Bengaluru, Hyderabad, Pune and remote India; change `config.json` for your own search.

## Setup

Needs Python 3.10+.

```
git clone <this repo> job-scanner
cd job-scanner
pip install -r requirements.txt
copy config.example.json config.json      # macOS/Linux: cp config.example.json config.json
```

Edit `config.json`:

| Key | What to put |
|---|---|
| `ntfy_topic` | Any hard-to-guess name, e.g. `myjobs-7f3k29q`. Install the **ntfy** app and subscribe to the same topic. Anyone who knows the name can read it, so keep it private. |
| `linkedin_keywords`, `linkedin_locations` | What and where to search on LinkedIn. |
| `title_include` / `title_exclude` / `title_stretch` | Regexes on the job title. Stretch matches become tier **B**, others tier **A**. |
| `location_include` | Regex the location must match (for ATS boards). |
| `max_min_years_drop` | Drop jobs whose description asks for at least this many years. |
| `stack_weights` | Keywords that raise a job's **Score** (how well the stack fits you). |
| `company_block` | Company names to ignore (e.g. staffing agencies). |
| `notion_data_source` | Optional, see Notion below. |
| `shortlist_min_score` | Tier A jobs with at least this Score go into Notion as **Interested** instead of **New**, so the Interested view is your shortlist. Default 5; `null` turns it off. |

Then:

```
python scanner.py --dry-run   # see what would match; changes nothing
python scanner.py --seed      # mark everything currently open as seen, so only NEW jobs alert
python scanner.py             # a normal run
```

## Run it every 15 minutes

**Windows** (PowerShell, from the repo folder):

```powershell
$py = (Get-Command pythonw).Source
$action = New-ScheduledTaskAction -Execute $py -Argument "`"$PWD\scanner.py`"" -WorkingDirectory $PWD
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "Job Scanner" -Action $action -Trigger $trigger -Settings $settings
```

**macOS / Linux**: `crontab -e` and add
`*/15 * * * * cd /path/to/job-scanner && python3 scanner.py`

It only runs while the computer is on. Missed runs catch up when it wakes (Windows).

## Notion (optional)

After each run, new matches can be added to a Notion database.

1. Create a database with these properties: **Title** (title), **Company** (select), **Location** (text), **Level** (select), **Experience** (text), **Posted Date** (date), **Link** (URL), **Job ID** (text), **Status** (status, with `New` and `Interested` options), **Tier** (select), **Source** (select), **Score** (number).
2. At https://www.notion.so/profile/integrations, create an **internal** connection and give it access to that database (Content access → Edit access).
3. Copy the connection's secret into `state/notion_token.txt` (or set the `NOTION_TOKEN` environment variable).
4. Open the database → `•••` → **Copy link**. Put the 32-character ID from that link (the part before `?v=`) in `notion_data_source` in `config.json`. A data source ID works too.
5. Check it: `python notion_sync.py --check`. Optionally backfill today's matches with `python notion_sync.py --backfill 1 --tier A`.

Jobs that fail to sync wait in `state/notion_pending.json` and are retried next run. Jobs already in Notion (same Job ID or link) are skipped.

## Adding companies

`companies.json` lists the boards polled directly. To find which ATS a company uses, put candidate slugs (usually the lowercase company name) in `candidates.txt` and run:

```
python probe_ats.py candidates.txt
```

Copy matched slugs into the right list. Workday entries are `[tenant, wd-number, site]`, taken from a careers URL like `https://<tenant>.wd<N>.myworkdayjobs.com/<site>`.

## Files

| Path | |
|---|---|
| `scanner.py` | The scanner. |
| `notion_sync.py` | Notion push, pending queue, `--check` and `--backfill`. |
| `companies.json` | Company boards + display names. |
| `config.example.json` | Template for your `config.json` (which git ignores). |
| `state/` | Seen jobs (expire after 45 days), log, Notion queue and token. Ignored by git. |
| `jobs_feed.csv` | Every match found. Ignored by git. |

## Limits

- Naukri blocks scripts with a CAPTCHA. Use Naukri's own alerts.
- LinkedIn sometimes rate-limits (HTTP 429). The scanner skips LinkedIn for that run.
- Microsoft and Morgan Stanley career sites need a real browser and aren't covered.
- The scanner only finds and alerts. Applying is up to you.
- Use it for your own job search at a modest rate. Respect each site's terms.
