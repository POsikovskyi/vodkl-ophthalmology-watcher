# VODKL pediatric ophthalmology schedule watcher

Watches the public booking page at
https://portal-doctor.eleks.com/web/vodkl/registration.html for the
"Лікар офтальмолог дитячий" (pediatric ophthalmologist) doctors — including
Маковська Ольга Миколаївна — and emails you when something changes.

It calls the site's own public, unauthenticated JSON API (the same one the
registration page itself calls) read-only: it never books, cancels, or
submits anything.

## Files

- `spz_client.py` - talks to the hospital's booking API, walks each doctor's
  published calendar, and classifies each day as fully booked or a possible
  opening.
- `run_check.py` - the script that actually runs. Scans, rewrites
  `report.html`, and emails you per the policy below.
- `report_builder.py` / `report.html` - a self-contained local web page
  (just a file, open it in any browser - no server needed) showing every
  scanned doctor's calendar, busy and open days both, color-coded.
- `emailer.py` - plain SMTP sender.
- `config.json` - your settings (see below).
- `state/` - snapshot + log the script keeps between runs; safe to delete
  if you want it to "forget" history (it'll just treat the next run as the
  first one).
- `register_task.ps1` - registers the Windows Scheduled Task that runs
  `run_check.py` every 2 hours.

## One-time setup

### 1. Get a Gmail app password

(Needed because Gmail blocks plain-password SMTP login.)

1. Go to https://myaccount.google.com/apppasswords (requires 2-Step
   Verification to be turned on).
2. Create an app password for "Mail" / "Other (custom name)".
3. Copy the 16-character code it gives you.

### 2. Edit `config.json`

Open `config.json` and fill in:

```json
"smtp": {
  "username": "your_gmail_address@gmail.com",
  "password": "the 16-character app password, no spaces",
  "from_addr": "your_gmail_address@gmail.com"
}
```

`recipient_email` already defaults to your address; change it if you want
the reports to go elsewhere. `speciality_filter` and `horizon_days` can be
tuned too (`horizon_days` is how far ahead it tries to scan; the hospital
currently only publishes about 2 months ahead anyway, so scanning further
just costs a few extra, harmless requests).

### 3. Test it manually once

```
python3 run_check.py
```

Check `state\run_log.txt` for a line confirming the email sent (or a clear
error if the SMTP login failed), and open `report.html` in a browser.

### 4. Register the recurring task

In PowerShell, from this folder:

```
.\register_task.ps1
```

This runs `run_check.py` every 2 hours from now on, including after you
restart your PC (it catches up once you're logged back in).

To stop it:
```
Unregister-ScheduledTask -TaskName "VodklOphthalmologyWatcher" -Confirm:$false
```

## Email policy

- If a doctor's calendar shows a **new possible opening** (see heuristic
  below) or a scan starts/stops failing, you get an email **immediately**.
- If nothing changed, you get **one quiet "still nothing" email per day**
  at most (the first check that runs on a given day).
- Otherwise it stays completely silent.

## How "possible opening" is detected (important caveat)

The server only ever reports the **busy** blocks for a day — it never sends
an explicit "this slot is free" marker. So a day counts as a possible
opening here if either:
- it has **no busy blocks reported at all**, or
- there's a **gap of 15+ minutes** between two busy blocks.

This is a reasonable inference, not a guarantee — **always double check on
the actual booking page** before assuming a slot is truly bookable, and open
`report.html` for the full picture (it also shows the "Зайнято"/fully-booked
days, so you can see at a glance how far each doctor's schedule is
published and how booked they are).
