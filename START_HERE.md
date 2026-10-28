# Millie daycare sync

Every 15 minutes this checks millie.agentic.ai@gmail.com for Goddard "Daily Sheet"
emails, reads the bottles, naps and diapers, and adds them to Millie in Huckleberry.
After each sheet it emails you a short summary.

It also keeps a full copy of everything in Huckleberry (yours and the agent's
entries, every tracker) as an Excel workbook, CSV files for pandas, a SQLite
database for SQL, and optionally a Google Sheet. Every entry gets a permanent audit
key like `102226AD1`.

It replaces the Android-emulator version. There's no emulator, ADB or screen reading
any more. Entries go straight into Huckleberry through
[py-huckleberry-api](https://github.com/Woyken/py-huckleberry-api), an unofficial,
reverse-engineered client.

## One-time setup (about 10 minutes, plus 10 for the optional Google Sheet)

### 1. Make an app password for the Millie Gmail inbox

1. In a browser, sign in to **millie.agentic.ai@gmail.com**.
2. Turn on 2-Step Verification at <https://myaccount.google.com/security>
   (Google requires it before it allows app passwords).
3. Go to <https://myaccount.google.com/apppasswords>, type a name like `Millie sync`,
   click **Create**, and keep the 16-letter password handy for step 4.

### 2. Optional: auto-forward the daily sheets

Forwarding by hand works fine. To make it hands-off, do this in **your own** Gmail
(the one Goddard emails), in a computer browser:

1. Gear icon > **See all settings** > **Forwarding and POP/IMAP** >
   **Add a forwarding address** > `millie.agentic.ai@gmail.com`.
2. Google sends a confirmation to the Millie inbox. Open it there and confirm.
3. Back in your Gmail, leave "Disable forwarding" selected (that setting would forward
   *everything*). Instead, open the search options (the sliders icon in the search bar),
   put `kaymbu.com` in **From** and `Daily Sheet` in **Subject**, click
   **Create filter**, tick **Forward it to**, pick the Millie address, and save.

### 3. Optional: a Google Sheet copy of the data

Skip this if the files on your Mac are enough; you can add it later by running
Setup.command again. Everything here happens in the **Millie** Google account.

1. Go to <https://console.cloud.google.com>, create a project (any name, e.g.
   `Millie sync`). No billing is needed.
2. **APIs & Services > Library**, search for **Google Sheets API**, click **Enable**.
3. **IAM & Admin > Service Accounts > Create service account**. Name it
   `millie-sheets`, click **Done** (no roles needed). Copy its email address; it ends
   in `.iam.gserviceaccount.com`.
4. Click the new account > **Keys > Add key > Create new key > JSON**. A `.json` file
   downloads. Keep it private; it's the robot's password.
5. In Google Sheets, create a blank spreadsheet called `Millie data`. Click **Share**,
   paste the service account's email, choose **Editor**, and share.
6. Also share the sheet with your own Gmail (and Tina's) to see it on your phones.

If Google says key creation is blocked by an organization policy, you're signed in to a
work or school account rather than the Millie Gmail account.

### 4. Run Setup.command

1. Open Docker Desktop and wait for it to say the engine is running.
2. Double-click **Setup.command**. If macOS refuses to open it, right-click it, choose
   **Open**, then **Open** again. (Or open Terminal, type `bash `, drag the file into the
   window and press Return.)
3. It builds the Docker image (a few minutes the first time), then asks for:
   * the Millie Gmail address and the app password from step 1
   * the email and password you use to sign in to Huckleberry
   * Millie's name in Huckleberry and on the daycare sheet ("Emilia")
   * who's allowed to send sheets (your address, plus kaymbu.com for auto-forwarding)
   * where to email summaries
   * the Google Sheet link and the `.json` key from step 3 (drag the file into the
     window), or `none`
4. It tests every connection, sends you a test email, and shows exactly what it would
   add right now (nothing is written yet). Then you choose: start and add those, start
   but skip them, or don't start.

Passwords and the key are stored only inside Docker, never in this folder.

## Everyday use

Nothing to do. It runs whenever Docker Desktop is open, and catches up after the Mac
has been asleep (it looks back 7 days). In Docker Desktop, Settings > General >
"Start Docker Desktop when you sign in" keeps it going after a restart.

| Double-click | What it does |
| --- | --- |
| Status.command | What it has done lately, and any problem |
| Get Data.command | Fresh export of everything into Documents/Millie Data (opens the folder) |
| Verify.command | Checks the export against the CSV Huckleberry emails you |
| Run Now.command | Check Gmail right now instead of waiting |
| Preview.command | Show what it would add right now, without writing anything |
| Stop.command / Start.command | Pause / resume (nothing is lost) |
| Setup.command | Change settings, or install an updated version of this folder |

## The summary email

```
Subject: Millie Mon Oct 12: 9 added to Huckleberry

From "Fwd: Emilia's Daily Sheet for Monday, October 12th".

Emilia, Monday, October 12, 2026
 Added to Huckleberry (audit key in brackets):
  11:36 AM  Diaper  wet + BM                               [101226AD1]
  12:23 PM  Nap     until 12:55 PM (32 min)                [101226AN1]
   1:00 PM  Bottle  5 oz breast milk (offered 5.25 oz)     [101226AB1]
   1:54 PM  Diaper  wet + BM                               [101226AD2]
   2:40 PM  Nap     until 3:26 PM (46 min)                 [101226AN2]
   3:30 PM  Bottle  4 oz breast milk (offered 5.25 oz)     [101226AB2]
   3:45 PM  Diaper  wet                                    [101226AD3]
   4:28 PM  Diaper  wet                                    [101226AD4]
   4:35 PM  Nap     until 5:24 PM (49 min)                 [101226AN3]
 Teacher comments:
  - 3:30 PM bottle: And Mrs T.
 Day totals from the sheet:
  Bottles: 2, 9 oz total
  Naps: 3, 2h 7m total
  Diapers: 4 (2 wet + BM, 2 wet)
```

(That's a day with nothing logged yet. For October 12 itself, where you had already
entered the diapers and bottles by hand, it adds just the three naps.)

If something on a sheet couldn't be read with certainty, it's listed under
"Needs a look (not added)" so you can add it by hand. If the sync hits a problem
(a password stopped working, say), you get one email about it per day.

## Your data

The export holds **everything in Huckleberry**: entries you and Tina make in the app
and the ones the agent adds, for every tracker. It's refreshed after each daycare
import and at least once a day, and whenever you run Get Data.command.

**Sheets** (the same in the Excel workbook, the Google Sheet, the CSVs and SQLite):

| Sheet | What's in it |
| --- | --- |
| All entries | The master list: one row per entry, every kind, with key, source, times, amount (and amount in ml), details and notes |
| Agent diapers / Agent naps / Agent bottles | Everything the agent added: the daycare email it came from, when it was added, the teacher's comment, and whether it's still in Huckleberry |
| Diapers, Sleep, Bottles, Breastfeeding, Pumping, Medicine, Solids, Temperature, Growth, Activities, Potty | One sheet per kind, with that kind's own columns (pee/poo sizes, left/right minutes, pumped amounts, medicine names...) |
| Daily totals | Per day: bottles and ml, breastfeeds and minutes, wet and dirty diapers, sleep minutes, pumps and ml, medicine doses |
| Legend | What every key letter and column means |

**Audit keys**: `MMDDYY` + who + what + number. `102226AD1` is the first diaper the
**A**gent logged for October 22, 2026; entries made in the app get **U** instead
(`101226UB2` = the second bottle you entered for October 12). Type letters: D diaper,
N nap/sleep, B bottle, F breastfeeding, P pumping, M medicine, S solids, T temperature,
G growth, A activity, O potty. A key never changes once given; an entry added later for
an earlier day gets the next free number.

**On your Mac**: Get Data.command writes `Documents/Millie Data` with
`millie_data.xlsx`, a `csv` folder (one file per sheet), `millie.db` (SQLite) and
`huckleberry_format.csv`. For example:

```python
import pandas as pd

df = pd.read_csv("csv/all_entries.csv", parse_dates=["start", "end"])
daily = pd.read_csv("csv/daily_totals.csv", parse_dates=["date"])
```

```sql
-- sqlite3 millie.db
SELECT date, wet_diapers, dirty_diapers, bottle_ml, breastfeeding_minutes
FROM daily_totals ORDER BY date DESC LIMIT 14;
```

**Checking it against Huckleberry**: in the Huckleberry app, request the data export
email, save the CSV, then double-click **Verify.command** and drag the CSV in. It lines
up every entry by type and time and reports anything that differs (the full list goes
to `verify_report.txt`). `huckleberry_format.csv` is this export laid out exactly like
Huckleberry's file, so you can also compare them yourself.

## What it reads from the daily sheet

* **Bottles**: "Offered 5.25 ounces of breastmilk and took 5 ounces at 1:00pm" becomes a
  5 oz breast milk bottle at 1:00 PM. The amount she *took* is what's logged. Formula
  works the same way. A bottle she didn't drink from isn't logged.
* **Diapers**: Wet = pee, BM = poo, BM/Wet = both, Dry = dry.
* **Naps**: logged as sleep with the exact start and end.
* Check-in/out, meals and anything else on the sheet are left out.
* The date comes from the sheet itself, not from when it was emailed or forwarded.
* Diapers get the note "From daycare" (plus any teacher comment) so you can tell them
  apart in Huckleberry. Bottles get it too once py-huckleberry-api 0.4.9 is installed
  (see below); until then bottles have no note.

It never guesses. A line in a format it hasn't seen is reported, not logged.

## How it avoids duplicates

* It remembers every email and entry it has handled.
* Before adding anything it looks at what Huckleberry already has. A diaper or bottle
  within 15 minutes of the sheet's time, or a nap that overlaps, counts as already
  there and is left alone. Each existing entry can only stand in for one daycare entry,
  so one hand-entered diaper can't hide two daycare diapers. (Your October 12 entries
  were 0 to 10 minutes off the sheet's times; all six are recognised.)
* It never edits or deletes anything in Huckleberry. If daycare re-sends a corrected
  sheet, or what you entered by hand disagrees with the sheet (wet vs BM, 4 oz vs 5 oz),
  the summary says so and you decide.

## Huckleberry login

The sync signs in with the email and password you use in the Huckleberry app
(Huckleberry shares a child between caregivers by everyone signing in to the same
account). If you sign in with Google or Apple instead, there's no password for it to
use; you could try "Forgot password" on Huckleberry's email sign-in screen to set one,
but that isn't guaranteed to work for those accounts.

Because the Huckleberry connection is unofficial, an app update on their side could
break it. You'd get a problem email. The usual fix is a newer py-huckleberry-api:
change the version in `requirements.txt` and run Setup.command again.

Note: py-huckleberry-api 0.4.8 is the newest on PyPI today. Version 0.4.9 (already on
GitHub) also saves the "offered 5.25 oz" note on bottles. When it shows up on PyPI,
change `requirements.txt` to `huckleberry-api==0.4.9` and run Setup.command.

## Privacy

* Gmail is opened read-only. Nothing is marked read, moved or deleted.
* Settings, passwords, the Google key and the journal live in the Docker volume
  `millie-sync-data`, not in this folder. Your data goes to `Documents/Millie Data`
  and the Google Sheet, not here.
* Before putting this folder on GitHub: `Setup.command` suggests your Gmail address as
  a default answer, and `tests/fixtures` holds the October 12 sheet with the teacher's
  name and email addresses removed but Emilia's first name and that day's entries
  still in it. Swap those for placeholders, or keep the repository private.
* Summary emails are sent from the Millie Gmail account to the address you chose.

## If something seems off

* **Nothing showed up for a day:** run Status.command. If it says there were no
  daily-sheet emails, look in the Millie inbox (including Spam) to see whether the sheet
  arrived. If it's in Spam, mark it "Not spam". Then Run Now.command.
* **"Gmail didn't accept the login":** the app password was mistyped, or the Millie
  account's password was changed (which cancels app passwords). Make a new app password
  and run Setup.command again.
* **"Huckleberry didn't accept that email and password":** see "Huckleberry login" above.
* **"The Google Sheet isn't shared with ...":** share the sheet with that address as an
  Editor. **"Google Sheets API is turned off":** redo step 3.2.
* **A line "needs a look":** daycare used wording the sync hasn't seen. Add that entry
  by hand. If it keeps happening, the sync can be taught the new wording.

## Advanced

Extra settings (Terminal, from this folder):

```bash
docker compose run --rm -T sync save-settings <<< "POLL_MINUTES=30"
```

| Setting | Default | Meaning |
| --- | --- | --- |
| POLL_MINUTES | 15 | How often to check Gmail |
| LOOKBACK_DAYS | 7 | How far back in Gmail to look |
| IMPORT_SINCE | (blank) | Ignore sheets dated before this day, e.g. 2026-10-20 |
| EXPORT_EVERY_HOURS | 24 | Refresh the data export at least this often |
| TIMEZONE | America/New_York | Time zone of the daycare times |
| IMAP_FOLDER | INBOX | Gmail folder to read |

Other commands: `docker compose run --rm -T sync check` (test the connections),
`... sync export` (refresh the export and the Google Sheet), `... sync parse sheet.eml`
(read a saved email without logging in), and `docker compose logs -f sync` (live log).

## The old emulator version

Setup.command stops the old container for you. Once you're happy with this one, the
Android emulator isn't needed for this any more, and the old data can be removed with
`docker volume rm millie-daycare-data`.

## What has been tested

* A copy of the real October 12 email (personal details removed) reads as the nine
  entries above, from both the HTML and plain-text versions of the email, and also as
  Kaymbu's original email (what auto-forwarding delivers).
* Your real Huckleberry export (1,220 entries since July 29) was rebuilt as a stand-in
  Huckleberry database and exported again: all 1,220 entries came back exactly as in
  Huckleberry's own file, field by field. With your October 12 hand entries in place,
  the daycare sheet adds only the three naps.
* The real py-huckleberry-api code (0.4.8 and 0.4.9) was run against a stand-in
  database to check exactly what gets written: times, EDT/EST offsets, amounts, diaper
  types, nap lengths and notes. Duplicate handling, retries after a failed write,
  preview mode, daylight-saving changes, audit keys, every export format and the Google
  Sheet requests are covered too. The tests run again inside every Docker build.
* Not tested before handing over: the live Gmail, Huckleberry and Google logins (they
  need your passwords) and the Docker build on your Mac. Setup.command checks every
  connection first and stops with a plain explanation if one fails.
