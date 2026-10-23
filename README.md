# Auto-Apply

A job-search copilot. It watches company job boards and your LinkedIn job alerts, uses Claude to
score every posting against your profile, and for the good fits prepares a full draft: a tailored
resume, a cover letter when the form asks for one, and an answer to every question on the form.
**It never submits anything.** A Chrome extension fills the draft into the real application in
your own browser, and you read it over and press Submit.

It runs on a small always-on Fly.io machine with a password-protected dashboard you can use from
your phone.

```
discover ─► free filter ─► AI title screen ─► fit score ─► draft ─────────► you, in Chrome
 company     location, age,  Haiku reads       Haiku, full   Sonnet: resume,   extension fills
 boards +    excluded co.,   titles, keeps     posting vs.   cover letter,     the form, you
 LinkedIn    never-consider  anything          your profile  every answer      review and submit
 alerts      titles          plausible         0–100
```

## Why it's built this way

- **You submit.** Every application goes out from your own browser after you've seen it. Sites'
  bot checks, emailed security codes, and CAPTCHAs are yours to do; the app never tries to get
  around them.
- **Nothing is made up.** Your profile is the only source of facts. Tailoring can only pick,
  reorder, and rephrase your existing bullets, and every rewritten bullet must cite the bullets it
  came from. A second pass fact-checks the result, and a code check rejects any number that isn't
  in the source.
- **High-stakes answers are double-checked.** Work authorization, sponsorship, salary, contact
  details, and demographic questions are cross-checked in code against your profile. A conflict
  stops the draft at **Needs you** instead of guessing.
- **LinkedIn is never automated.** The app only reads the alert emails LinkedIn sends you, then
  looks for the same posting on the employer's own hiring system.
- **One role per company.** The best-scoring role at a company is prepared; the others are held,
  side by side, so you can swap.
- **Spend is capped and visible.** A daily cap on new drafts, cheap models for the high-volume
  steps, and an Impact page with what it cost and the hours it saved you.

## What's on the dashboard

| Page | What it's for |
|---|---|
| **Overview** | Hours saved, the current run with live progress, and what needs you: drafts ready to fill, drafts waiting on a question, LinkedIn-only jobs |
| **Ready** | Prepared drafts. **Fill in Chrome ↗** opens the form for the extension |
| **Applications** | Everything you've applied to, with stages that update from your email (confirmations, rejections, interview requests) |
| **Impact** | Hours saved per day, your measured minutes per application, the funnel from found to interviewing, replies by fit score, how fresh your applications were, and Claude spend |
| **Jobs** | Every job the app knows about, by stage |
| **LinkedIn** | Jobs from your alerts that aren't on a supported board; look them up on a budget |
| **Companies**, **Filters** | What's watched and what's filtered out |
| **Profile** | Everything the app knows about you, versioned, plus your writing samples and style guide |

Each job's page shows the six-part fit score (role family, required and preferred skills,
level, location, pay) with its history, the tailored resume and cover letter, and every answer.
**I've done this** next to a gap adds what you actually did to your profile and re-scores the job.

## Run it locally

Needs Python 3.12 and an Anthropic API key.

```bash
git clone <this repo> autoapply && cd autoapply
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/playwright install chromium
```

```bash
cp .env.example .env                                 # then add ANTHROPIC_API_KEY and a DASHBOARD_PASSWORD
cp config.example.yaml config.yaml                   # what you're looking for: titles, locations, preferences
cp profile/profile.example.yaml profile/profile.yaml # or: .venv/bin/autoapply import-resume your_resume.pdf
```

Your `.env`, `config.yaml`, `profile/profile.yaml`, and the `data/` folder stay out of git.

```bash
.venv/bin/autoapply serve        # dashboard and scheduler at http://localhost:8080
```

Other commands:

```bash
.venv/bin/autoapply discover        # fetch and filter jobs, no Claude calls, no cost
.venv/bin/autoapply run-once        # one full cycle now
.venv/bin/autoapply build-boards    # companies.txt → boards.yaml (which boards are watched)
.venv/bin/autoapply check-boards    # does every watched board resolve?
.venv/bin/pytest -q -m "not network"
```

## The Chrome extension

The extension fills a prepared draft into the application in your own browser and records the
application when you submit.

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and choose the
   `extension/` folder.
2. In the extension's settings, enter your dashboard address, username, and password, then
   **Save**. It should say **Connected ✓**.
3. On any draft, click **Fill in Chrome ↗**. The form fills in about a minute; review it, change
   anything you like, and press Submit. The job moves to Applications on its own, including when
   the thank-you page opens in a new tab or on the company's own site.

It talks to dashboards on `*.fly.dev` and localhost. If you host somewhere else, add your address
to `host_permissions` in `extension/manifest.json`.

## Deploy to Fly.io

1. Install flyctl and sign in: `curl -L https://fly.io/install.sh | sh`, then `fly auth login`.
2. `cp fly.example.toml fly.toml` and pick a unique `app = "..."` name. Then create the app and
   its volume:

   ```bash
   fly launch --no-deploy --copy-config
   ```

   ```bash
   fly volumes create autoapply_data --size 1 --region ewr
   ```

3. Set the secrets yourself (they never go in files):

   ```bash
   fly secrets set ANTHROPIC_API_KEY=... DASHBOARD_PASSWORD='a-long-password'
   ```

4. Deploy. This uploads your local `config.yaml` and `profile/profile.yaml` with the code:

   ```bash
   fly deploy
   ```

5. Open `https://<app-name>.fly.dev`, sign in as `admin`, and press **Run now**.

The first run reads a lot of postings, so it's the most expensive one. After that, a run every
four hours only reads what's new. Before redeploying, wait for a run to finish (the Overview shows
when one is going): a deploy restarts the machine.

## Optional extras

- **Phone notifications:** install the ntfy app, subscribe to a long random topic name, and set
  `NOTIFY_URL=https://ntfy.sh/<your-topic>` (Slack and Discord webhooks work too). You hear about
  runs that prepared drafts, things that need you, and replies from companies.
- **LinkedIn alerts and reply tracking:** read-only Gmail access, either an IMAP app password
  (`IMAP_USER`, `IMAP_PASSWORD`) or the Gmail API (`autoapply google-auth gmail`). The app reads
  LinkedIn alert emails and replies about your applications, and moves stages forward.
- **Copies in Google Drive:** `autoapply google-auth drive` lets the app keep a folder per
  application with the exact resume, cover letter, and answers you sent, plus nightly database
  backups. It uses Google's `drive.file` permission, so it can only see files it created.

## Costs

- **Fly:** about $10–12 a month for a shared-CPU machine with 2 GB of memory and a 1 GB volume.
- **Claude:** scoring uses Haiku, at well under a cent per posting. A full draft (tailored resume,
  cover letter, answers) uses Sonnet, at roughly 5–15¢. The daily cap on new drafts is in
  Settings, and the Impact page shows actual spend.

## Project layout

| Path | What |
|---|---|
| `autoapply/sources/` | Greenhouse, Lever, and Ashby boards; six other hiring systems for looking up LinkedIn jobs; LinkedIn alert parsing |
| `autoapply/llm.py` | Every Claude prompt and its guardrails |
| `autoapply/apply/` | Reading application forms, planning and checking answers |
| `autoapply/pipeline.py` | The run: discover, filter, screen, score, draft |
| `autoapply/impact.py` | Time saved and the Impact page numbers |
| `autoapply/web/` | The dashboard |
| `extension/` | The Chrome extension |
| `config.example.yaml`, `profile/profile.example.yaml` | Starting points for your own settings and profile |

## Known limits

- Drafts can be prepared for Greenhouse, Lever, and Ashby forms. Jobs on other systems (Workday,
  iCIMS, custom sites) come through LinkedIn alerts as "apply yourself", with a tailored resume
  and cover letter on request.
- Questions your profile can't answer truthfully stop at **Needs you**. Answer them on the job's
  page and tick **Remember** to reuse the answer on future applications.

## License

MIT. See [LICENSE](LICENSE).
