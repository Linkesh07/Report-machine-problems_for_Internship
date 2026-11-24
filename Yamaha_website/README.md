# Bike Factory Machine Issue Portal

A private website for a bike factory. Workers report machine failures. The manager sees every report, and the system automatically tells the manager when a new report describes the same problem as an earlier one.

Built with Python (Flask), SQLite, and scikit-learn. Optionally uses sentence-transformers for smarter matching.

## What it does

| Who | What they can do |
|-----|------------------|
| **Worker** (own login page) | Report a machine problem with a photo, see the status of their own reports. Cannot see other workers' reports. |
| **Manager** (own login page) | See all reports with NEW ISSUE / DUPLICATE labels, compare two reports side by side, confirm or reject a duplicate, merge reports, change status and priority, write notes, add / disable / delete workers, delete checked reports, get a live alert badge for new reports. |

Pages: home, worker login, manager login, worker dashboard, report form, my reports, report details, manager dashboard, all reports (with filters), report comparison, issues and notes, manage workers, change password, and friendly error pages (403, 404, and others).

## 1. Install (Windows, PowerShell)

```powershell
cd D:\Yamaha_website
py -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Open `.env` and change `SECRET_KEY` to a long random text. To make one:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

> `sentence-transformers` is large and may not install on very new Python versions. That is fine: the portal falls back to keyword matching (TF-IDF) automatically. To skip it, delete that line from `requirements.txt`, or add `USE_EMBEDDINGS=0` to `.env`.

## 2. Create the first accounts and machines

```powershell
python seed.py
```

| Role | Employee ID | Password |
|------|-------------|----------|
| Manager | `MGR001` | `Manager@123` |
| Workers | `WRK001` (Ravi), `WRK002` (Kumar), `WRK003` (Anu) | `Worker@123` |

**Change these passwords before real use** (use "Change password" in the menu).

## 3. Run

```powershell
python run.py
```

Open http://127.0.0.1:5000 and choose Worker Login or Manager Login.

## 4. Try the duplicate detection

1. Log in as **WRK001** (Ravi). Report on *Welding Robot*: "Welding robot stops suddenly" / "Welding robot stops suddenly and shows red light."
2. Log out. Log in as **WRK002** (Kumar). Report on *Welding Robot*: "Welding machine shuts down" / "The welding machine is shutting down by itself with a red warning light."
3. Log out. Log in as **WRK003** (Anu). Report on *Paint Booth*: "Paint booth fan making loud noise."
4. Log in as **MGR001** on the manager page. Result:
   - Ravi's report: **NEW ISSUE**
   - Kumar's report: **DUPLICATE, about 80% similar to Report #1**, and the issue shows "2 workers reported this problem"
   - Anu's report: **NEW ISSUE** (a different machine)

## 5. How duplicate detection works (simple words)

1. A new report is compared only with **open** reports for the **same machine** sent in the **last 7 days**.
2. Both texts are cleaned. Filler words are removed, and similar words become one ("stops", "halts", "shutting down" all count as "stop").
3. A similarity score from 0% to 100% is calculated (keyword match, plus meaning match if sentence-transformers is installed).
4. The score decides the label:
   - **75% or more**: DUPLICATE. It joins the original issue and the "reported by X workers" counter goes up.
   - **55% to 75%**: POSSIBLE DUPLICATE. The manager must confirm or reject it.
   - **Below 55%**: NEW ISSUE, with its own notes area.
5. If 3 or more workers report the same problem, the priority goes up automatically. The manager can override it.
6. **Short vs long reports:** one worker may write 5 detailed lines and another only "engine overheating". Plain keyword matching scores these low because their lengths differ. So when almost all key words of the shorter report appear in the longer one **and** both describe the same kind of problem (overheating, leak, noise, vibration, smoke, jam, stopping, slipping...), the score is raised. Different kinds of problem (for example overheating vs oil leak) are never merged this way.

7. **Different words, same meaning:** every word is turned into a concept before comparing. "temperature / heat / hot" become `heat`, "reducing / decreased / dropped" become `decrease`, "rapidly / suddenly / quickly" become `sudden`, and "noise / sound / rattle", "smell / smoke / burning", "leak / drip / seep", "vibrate / shake / wobble", "stop / halt / shut down", "jam / stuck / blocked" are each one concept too. So "the temperature is reducing rapidly" and "the heat is suddenly decreased" are the same problem.
8. **Opposites and different problems are kept apart:** "temperature rising" vs "temperature dropping", or "pump leak" vs "pump noise", get a lower score even if they share words like "pump".

Duplicates are only searched among **open reports for the same machine** from the **last 7 days**. If two workers pick different machines in the form, the reports are not compared.

You can change the limits in `.env`: `DUPLICATE_THRESHOLD`, `POSSIBLE_THRESHOLD`, `DUPLICATE_WINDOW_DAYS`.

## 6. Deleting

- **Reports:** a **Delete** button appears in *All reports* next to every report the manager has already checked (opened, or "Mark all as seen"). It is also on the report page. If the deleted report was the main report of an issue, the next report takes over. If it was the only one, the issue and its notes are deleted too. The photo file is removed.
- **Workers:** *Delete* in *Workers* removes the account **and all of that worker's reports**. The confirm box says how many. If you want to keep the history, use *Disable* instead.

If the report's issue is **not solved yet** (status Pending or In Progress), the confirm box starts with "WARNING: Report #N is NOT solved yet". Once the issue is Resolved, the normal message is shown.

Deleting cannot be undone, so back up `instance/yamaha_portal.db` regularly.

## 7. Teaching the matcher new words

The matcher knows a long list of words (see `GROUPS` in `app/duplicate_detection.py`). If your factory uses other words, add them **without touching the code**: open `synonyms.json`, add a line, save, and restart the server.

```json
{
  "leak": "weep seepage",
  "coolant": "antifreeze radiator",
  "_symptoms": ["coolant"]
}
```

- A key is a problem type, and its value lists words that mean the same thing. Using an existing key (like `leak`) adds words to it. A new key creates a new concept.
- `_symptoms` is optional. List new concepts there if they name a *kind of problem*, so reports about different problem types are not merged.

To see how any two reports are understood, run:

```powershell
python check_similarity.py "the temperature is reducing rapidly" "the heat is suddenly decreased"
```

It prints the concepts it found, the similarity, and the result (duplicate, possible duplicate, or new issue). This is a word-list method, not a trained neural network. Installing `sentence-transformers` adds an AI model that also understands meaning for words that are not in the list.

## 8. Check that everything works

```powershell
python -m unittest tests.test_flow -v
```

This runs 17 automatic checks on a temporary database (your data is not touched): both logins, wrong-door login blocked, role protection, the Ravi/Kumar/Anu scenario, the long-vs-short overheating case, different words for the same problem, deleting reports and workers (with the unsolved warning), your own synonyms, privacy between workers, every page opens, manager actions, photo rules, and the 5-wrong-passwords lockout.

## 9. Project structure

```text
Yamaha_website/
  app/
    __init__.py            app setup, security headers, error pages
    auth.py                two login pages, logout, change password, lockout
    models.py              database tables
    worker.py              worker pages
    manager.py             manager pages and actions
    duplicate_detection.py the duplicate-matching logic
    services.py            create / merge / split issues
    main.py                home page and private photo downloads
    templates/             all HTML pages
    static/css/style.css   design
    static/js/app.js       menu, alerts, confirm boxes, show-password
  tests/test_flow.py       automatic checks
  synonyms.json            your own words for the matcher
  check_similarity.py      test how two reports are compared
  run.py   seed.py   requirements.txt   .env.example   README.md
```

## 10. Security that is already built in

- Passwords are hashed, never stored as plain text.
- Separate login pages. A worker cannot log in on the manager page, and the reverse.
- Pages are protected by role (a worker opening a manager page sees "Access Denied").
- Lock for 5 minutes after 5 wrong passwords. Automatic logout after 30 idle minutes.
- CSRF protection on every form, and safe database queries (no SQL injection).
- Photos: only real images, shrunk and renamed, 5 MB limit, visible only to the manager and the worker who sent them.
- Security headers, and the back button cannot reopen private pages after logout.

## 11. Before using it for real

This is a local starter. For a real company network: run it behind HTTPS with a production server (for example Waitress on Windows), set `SECURE_COOKIES=1` in `.env`, use a strong `SECRET_KEY`, change all demo passwords, back up the database file, and move to PostgreSQL or MySQL if many people will use it at the same time.

## 12. Ideas for later

Email or SMS alerts for critical issues, charts per month, machine maintenance history, a Tamil language option, and a "forgot password" request flow.

---
The name and logo are placeholders. Replace them with your company's own branding. Do not use the official Yamaha logo without permission.
