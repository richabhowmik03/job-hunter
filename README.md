# Job Hunter

Personal job bot. Runs on GitHub Actions twice a day, scrapes a handful of
sources, filters with rules + a Groq-hosted LLM fit-scorer that judges postings
against your resume, emails you the matches.

## Setup

1. From the **repository root** (the directory that contains `config.yaml` and the
   `job_hunter` Python package), install dependencies:
   ```
   pip install -r requirements.txt
   ```
   Optional but recommended if you use the Web UI or run tools from subfolders (e.g.
   `web/`): install the package in editable mode so `import job_hunter` works from
   any current directory:
   ```
   pip install -e .
   ```
2. Edit `profiles/richa.yaml` (titles, locations, companies, seniority,
   deal-breakers, `min_fit_score`).
3. Replace `profiles/richa_resume.md` with your actual resume in markdown.
4. Set env vars (or put them in a `.env` file next to `config.yaml`; the CLI loads
   that file and the project root `.env` when present):
   - `GROQ_API_KEY` — required (free tier at [console.groq.com](https://console.groq.com)).
   - `GROQ_MODEL` — optional; defaults to `llama-3.3-70b-versatile`.
   - `SERPAPI_KEY` — required if `serpapi` is in `enabled_sources`. The SerpAPI
     source **fails the run** if the API returns an error (including exhausted
     quota); there is no silent empty fallback.
   - `SMTP_USER`, `SMTP_APP_PASSWORD`, `SMTP_TO` — Gmail with an App Password
     works; `SMTP_HOST`/`SMTP_PORT` default to Gmail.
   - **`JOB_HUNTER_UI_OWNER_EMAIL`** (optional) — if you host the web UI, set this
     to **your** address. Only that address receives **SMTP email** from the UI;
     everyone else gets an **HTML download** of the same report (so you do not
     relay mail through your account to random addresses). GitHub Actions
     scheduled runs are unchanged and still use `SMTP_TO` from secrets.
5. Dry run (prints email HTML to stdout, no send, no state write):
   ```
   python -m job_hunter.main --dry-run
   ```
6. Real run:
   ```
   python -m job_hunter.main
   ```

## Web UI (local)

React + MUI form: you edit the **top-of-profile** fields (name, titles, locations,
seniority—the same idea as the first lines of `profiles/richa.yaml`). The server
merges that with the **rest** of `profiles/richa.yaml` (company lists,
deal-breakers, `min_fit_score`) and **builds `serpapi_queries`** from your target
titles (`"<title> India"` and `"<title> remote India"` per title). You upload a
**resume** as Markdown, plain text, or **PDF** (text-based PDFs; scanned images
are not OCR’d) and a **notification email** per run.

1. `cd web && npm install && npm run build`
2. Start the server (API + static `web/dist`) from the **repository root** (or use
   `pip install -e .` once so this works from any directory):
   ```
   uvicorn job_hunter.server:app --host 127.0.0.1 --port 8765
   ```
   Open `http://127.0.0.1:8765` in a browser.

**Dev:** run `uvicorn …` from the repo root (or after `pip install -e .`) and, in
another terminal, `cd web && npm run dev` (Vite proxies `/api` to port 8765).

**Note:** Running `uvicorn` from `web/` fails with `ModuleNotFoundError: job_hunter`
unless you ran `pip install -e .` from the repo root.

Each run writes only under `~/.job_hunter/ui-runs/<uuid>/` (isolated `state.db`).
UI runs use `discover=False` by default. Env vars are read from the project `.env`
(see step 4) so keys do not need to live in the temp workspace.

**On-demand email:** each run sends the digest to the **email address you type**
in the form (using `SMTP_*` from `.env`). **Scheduled twice-daily runs** use
GitHub Actions + repo `profiles/` and the `SMTP_TO` secret (one address per
workflow/repo unless you add more workflows or repos). See the in-app accordion
for details.

## Deploy on GitHub Actions

1. Push this repo to GitHub.
2. Add the same env vars as repo secrets
   (`Settings → Secrets and variables → Actions`). Use `GROQ_API_KEY` (not
   `ANTHROPIC_API_KEY`).
3. The workflow in `.github/workflows/run.yml` runs at 09:00 and 19:00 IST
   and commits the updated `state.db` back. Trigger it manually the first
   time via `Actions → job-hunter → Run workflow`.

**LinkedIn in CI:** the workflow runs a **scrape step** first (`python -m
job_hunter.cli_linkedin_scrape`) that hits LinkedIn’s **public guest** jobs API
with pagination and writes `data/linkedin_ingest.json`. The **main** step sets
`LINKEDIN_INGEST_PATH` to that file. The LinkedIn source uses **both** on-disk
ingest and the **same** guest API at run time: with the default
`LINKEDIN_PRIMARY=ingest`, it prefers a non-empty ingest file, then falls back
to live scraping; with `LINKEDIN_PRIMARY=live`, it tries live first and falls
back to the ingest file if live fails or returns no jobs. Omit
`LINKEDIN_INGEST_PATH` locally or in the Web UI to use **only** the live guest
API. Optional: `LINKEDIN_MAX_PAGES` (default `10`, 25 jobs per page per
title×location).

**Manual scrape** (writes JSON only):

```
python -m job_hunter.cli_linkedin_scrape --root . --out data/linkedin_ingest.json
```

## Making it yours (plug-and-play)

Copy `profiles/richa.yaml` → `profiles/<you>.yaml` and
`profiles/richa_resume.md` → `profiles/<you>_resume.md`. Change `profile:` in
`config.yaml` to `<you>`. No code changes.

## Enabling / disabling sources

Edit `enabled_sources` in `config.yaml`. When a source breaks (LinkedIn and
Naukri are the fragile ones), remove it from the list; the rest of the
pipeline keeps working. The email footer shows per-source health, and the
subject is prefixed `[ACTION NEEDED]` when a source has failed 3 runs in a
row.

## Adding a new source

Drop `job_hunter/sources/<name>.py` exposing:

```python
SOURCE_NAME = "<name>"

def fetch(profile: Profile) -> list[RawJob]: ...
```

Add `<name>` to `_SOURCE_MODULES` in `job_hunter/sources/__init__.py` and to
`enabled_sources` in `config.yaml`.

## Pipeline

```
sources → normalize → dedupe + 24h window → rules → LLM fit → email + state
```

- **Rules** cut 80–90% cheaply (title regex, seniority terms, locations,
  deal-breakers).
- **LLM fit** scores what survives against your resume; `min_fit_score` in
  your profile gates the final list.
- **State** (`state.db`, SQLite, committed by the workflow) dedupes across
  runs and tracks source health.
