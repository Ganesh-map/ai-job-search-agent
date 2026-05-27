# AI Job Search Agent

This project runs a fully cloud-based Python job search agent every Monday at
7:00 AM IST through GitHub Actions. It searches Google-indexed public job pages,
filters for India-based internship, fresher, entry-level, and early-career
analyst/data roles, scores the best matches, creates a brand-new Google Sheet,
and uploads an execution log to Google Drive.

## What It Uses

- Python 3.10+
- Google Programmable Search API for public Google-indexed results
- Google Sheets API and Drive API
- GitHub Actions cron scheduler
- Environment variables and GitHub Secrets only

No runtime output is written to local disk. The spreadsheet and log file are
created directly in Google Drive.

## Required Cloud Setup

1. Create a Google Cloud project.
2. Enable these APIs:
   - Google Sheets API
   - Google Drive API
   - Custom Search API
3. Create a Google service account and download its JSON key.
4. Create or choose a Google Drive folder for the reports.
5. Share that Drive folder with the service account email as Editor.
6. Create a Programmable Search Engine at <https://programmablesearchengine.google.com/>.
   Configure it to search the entire web, then copy its Search engine ID.
7. Add these GitHub repository secrets:
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: the full service account JSON
   - `GOOGLE_SEARCH_API_KEY`: Google API key with Custom Search API access
   - `GOOGLE_CSE_ID`: Programmable Search Engine ID
   - `GOOGLE_DRIVE_FOLDER_ID`: target Drive folder ID

If your secret store has trouble with multi-line JSON, use base64 instead:
set `GOOGLE_SERVICE_ACCOUNT_B64` in the workflow and remove
`GOOGLE_SERVICE_ACCOUNT_JSON`.

## Schedule

The workflow file [`.github/workflows/job-search.yml`](.github/workflows/job-search.yml)
uses:

```yaml
cron: "30 1 * * 1"
```

That is Monday 01:30 UTC, which equals Monday 07:00 IST.

## Output

Each run creates a new spreadsheet named:

```text
JobSearch_YYYY-MM-DD
```

Columns are generated in this exact order:

```text
#, Job Title, Company, Location, Experience Required, Work Mode, Match Score,
Apply Link, Source Platform, Date Posted, Job Summary (2-3 lines)
```

The sheet freezes and bolds the header, auto-resizes columns, and color-codes
the Match Score column:

- Green: 8-10
- Yellow: 5-7
- Red: 1-4

## Filtering

The agent discards:

- Jobs requiring more than 3 years of experience
- Titles containing Senior, Lead, Manager, Director, Head of, VP, or Principal
- Duplicate title/company pairs
- Broken or inaccessible apply links
- Non-India roles
- Jobs outside the configured recency window
- Roles that are not relevant to the configured analyst/data target list

If fewer than 10 jobs are found in the 6-day window, it automatically retries
with an 8-day window.

## Match Score

Scores are calculated on a 1-10 scale:

- Title relevance: 40%
- Seniority fit: 30%
- Recency: 20%
- Work mode: 10%

Remote roles receive a small work-mode boost.

## Local Dry Checks

You can run unit tests without any cloud credentials:

```bash
python -m unittest discover -s tests
```

Running the real agent requires the same environment variables as GitHub
Actions:

```bash
python job_search_agent.py
```

## Runtime Limits

The GitHub Actions job has a 15-minute timeout. To keep the run bounded, the
agent caps Google search calls with `MAX_SEARCH_QUERIES`, which defaults to 80.
You can tune the following environment variables in the workflow:

- `MAX_SEARCH_QUERIES`
- `SEARCH_RESULTS_PER_QUERY`
- `MAX_RESULTS`
- `MIN_RELEVANT_JOBS`
- `PRIMARY_LOOKBACK_DAYS`
- `FALLBACK_LOOKBACK_DAYS`
- `ROLE_GROUP_SIZE`
