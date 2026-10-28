# MillieBeanCounter: your copy

*Your own guide, with your accounts in it. Like My Setup.command, it stays off GitHub.
README.md is the public version for everyone else.*

Every 15 minutes this checks millie.agentic.ai@gmail.com for Goddard's Kaymbu emails:

* **Daily sheets:** the bottles, naps and diapers go into Millie in Huckleberry, and you
  get a short summary email.
* **Shared Moments:** the photos go into the Millie Google Drive, a folder per month,
  named like `October 2026/10/27 Millie 2m 28d Photo 1.jpg`.

It also keeps a full copy of everything in Huckleberry (yours, Tina's and the sync's
entries, every tracker) as an Excel workbook, CSV files for pandas, a SQLite database
for SQL, and optionally a Google Sheet. Every entry gets a permanent audit key like
`102226SD1`.

## Upgrading from millie-sync (do this once)

This version is renamed from millie-sync to MillieBeanCounter. Your settings, history
and audit keys carry over. Only the sync's key letter changes, from A to S
(`101226AD1` becomes `101226SD1`).

### 1. Forward the photo emails too (2 minutes)

Your Gmail filter only forwards emails with "Daily Sheet" in the subject, so the
"Shared Moments" photo emails never reach the Millie inbox. In **your own** Gmail, in a
computer browser:

1. Gear icon > **See all settings** > **Filters and Blocked Addresses**.
2. Find the filter for `from:(kaymbu.com) subject:(Daily Sheet)` and click **edit**.
3. Empty the **Subject** box, click **Continue**, keep **Forward it to**
   millie.agentic.ai@gmail.com ticked, and click **Update filter**.

### 2. Set up Google Drive for the photos (about 10 minutes)

Do all of this signed in as **millie.agentic.ai@gmail.com**, so the photos land in the
Millie Drive. You can share the folder with yourself and Tina afterwards.

1. Go to <https://console.cloud.google.com>. If you made a project for the Google Sheet
   copy, pick it from the project list at the top. Otherwise create one (any name, for
   example `MillieBeanCounter`). No billing is needed.
2. **APIs & Services > Library**, search for **Google Drive API**, click **Enable**.
3. Open **Google Auth Platform** (also listed as **APIs & Services > OAuth consent
   screen**) and click **Get started**. App name `MillieBeanCounter`, the Millie email
   as the support email, **Audience: External**, the Millie email as the contact, agree,
   **Create**. Then **Data Access > Add or remove scopes**, search for `drive.file`, tick
   `.../auth/drive.file`, click **Update**, then **Save**.
4. **Audience > Publish app > Confirm.** Left in "Testing", Google would sign the app
   out of Drive every 7 days. It only asks for the narrowest Drive permission, so
   publishing needs no review from Google.
5. **Clients > Create client**, type **Desktop app**, name `MillieBeanCounter`,
   **Create**, then **Download JSON**. Keep the file in Downloads for the next step.

### 3. Run My Setup.command in the new folder

1. Unzip the new version somewhere (it can replace the old folder; see "GitHub" below
   for your repository copy). Open Docker Desktop and wait until it's running.
2. Double-click **My Setup.command**. It stops the old millie-sync container, copies
   your settings and history across, and builds the new version.
3. Press Return to keep each saved answer. The new questions:
   * **Name, as you'd like it in photo names:** Millie.
   * **Birthday:** it suggests 7/29/2026 (from your "10/27 Millie 2m 28d" example).
     Fix it at the prompt if that's off.
   * **Save the daycare's photos to Google Drive?** yes. **Folder:** Millie Daycare
     Photos (or whatever you like).
   * **Drag in the OAuth client file:** the `.json` from step 2.5.
4. Your browser opens to Google. Pick **millie.agentic.ai@gmail.com** and allow access
   to "only the specific Google Drive files you use with this app". If Google shows
   "Google hasn't verified this app", click **Advanced**, then **Go to
   MillieBeanCounter**. It's your own app. The Terminal then says
   "✓ Google Drive: signed in as millie.agentic.ai@gmail.com".
5. It checks every connection and shows what it would do. Choose **Y**.

In Docker Desktop you'll see the old stopped millie-sync container and its volume
`millie-sync-data`. After a week or so of everything working, you can delete both there.

### 4. Send the older photos (optional)

The photo emails from before today are in your own inbox, not Millie's. To file them
too, in your Gmail on a computer: search `from:kaymbu.com -"Daily Sheet"`, select the
emails (the checkbox at the top selects a whole page), click **More** (the three dots)
> **Forward as attachment**, and send it to millie.agentic.ai@gmail.com. Each attached
email is saved on its own date, and anything already saved is skipped.

### 5. GitHub

1. On github.com, open the repository > **Settings** and rename it to
   `MillieBeanCounter`. GitHub keeps the old address working.
2. In GitHub Desktop, **Repository > Show in Finder**. In that Finder window press
   **Command-Shift-.** (period) to show hidden files.
3. Delete everything in the folder **except** `.git`. Then copy everything from the
   new unzipped folder in, including `.gitignore` and `.dockerignore` (hidden files
   show as grey once Command-Shift-. is on). Press Command-Shift-. again to hide them.
4. Back in GitHub Desktop the changes show up: the `millie_sync` folder gone,
   `millie_bean_counter` added, and so on. `My Setup.command` and `START_HERE.md`
   don't appear, because `.gitignore` keeps them out. Commit, then **Push origin**.

## Everyday use

Nothing to do. It runs whenever Docker Desktop is open, and catches up after the Mac
has been asleep (it looks back 7 days). In Docker Desktop, Settings > General >
"Start Docker Desktop when you sign in" keeps it going after a restart.

| Double-click | What it does |
| --- | --- |
| Status.command | What it has done lately (sheets and photos), and any problem |
| Get Data.command | Fresh export of everything into Documents/MillieBeanCounter Data (opens the folder) |
| Verify.command | Checks the export against the CSV Huckleberry emails you |
| Run Now.command | Check Gmail right now instead of waiting |
| Preview.command | Show what it would do right now, without writing anything |
| Stop.command / Start.command | Pause / resume (nothing is lost) |
| My Setup.command | Change settings, or install an updated version of this folder |

There are two setup files. **My Setup.command** is yours: it asks the same questions as
Setup.command but suggests your answers (the Millie Gmail, your email, Millie, Emilia,
Eastern time, photos on). **Setup.command** suggests nothing, so anyone who downloads
this from GitHub can enter their own accounts. Only Setup.command goes to GitHub.

## The photos

```
Millie Daycare Photos/
  October 2026/
    10/27 Millie 2m 28d Photo 1.jpg
    10/27 Millie 2m 28d Photo 2.jpg
    10/28 Millie 2m 29d Photo 1.jpg
```

* The date is the one printed in the email. The age is months and days on that date.
  The number counts up through the day, across all of that day's emails.
* Each moment's **Download** link is tried first, for the full-size picture. If it
  doesn't lead straight to a picture, the copy shown in the email is saved instead.
  The log (Status.command) says "full size" or "email size" after each one.
* Nothing is saved twice, even if a moment is shared again or forwarded again.
* Each file's description names the email it came from, and its "modified" time is
  when the daycare sent it.
* The app can only see what it created in that Drive. You can move the folder, rename
  it, or share it with Tina; it keeps working. If you delete it, a new one is made for
  the next photos.

## The summary email

```
Subject: Millie Mon Oct 12: 9 added to Huckleberry

From "Fwd: Emilia's Daily Sheet for Monday, October 12th".

Emilia, Monday, October 12, 2026
 Added to Huckleberry (audit key in brackets):
  11:36 AM  Diaper  wet + BM                               [101226SD1]
  12:23 PM  Nap     until 12:55 PM (32 min)                [101226SN1]
   1:00 PM  Bottle  5 oz breast milk (offered 5.25 oz)     [101226SB1]
   1:54 PM  Diaper  wet + BM                               [101226SD2]
   2:40 PM  Nap     until 3:26 PM (46 min)                 [101226SN2]
   3:30 PM  Bottle  4 oz breast milk (offered 5.25 oz)     [101226SB2]
   3:45 PM  Diaper  wet                                    [101226SD3]
   4:28 PM  Diaper  wet                                    [101226SD4]
   4:35 PM  Nap     until 5:24 PM (49 min)                 [101226SN3]
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
(a password stopped working, say), you get one email about it per day. Photos don't
send summary emails; Status.command lists the latest ones.

## Your data

The export holds **everything in Huckleberry**: entries you and Tina make in the app
and the ones the sync adds, for every tracker. It's refreshed after each daycare
import and at least once a day, and whenever you run Get Data.command.

| Sheet | What's in it |
| --- | --- |
| All entries | The master list: one row per entry, every kind, with key, source, times, amount (and amount in ml), details and notes |
| Daycare diapers / Daycare naps / Daycare bottles | Everything the sync added from daycare sheets: the email it came from, when it was added, the teacher's comment, and whether it's still in Huckleberry |
| Diapers, Sleep, Bottles, Breastfeeding, Pumping, Medicine, Solids, Temperature, Growth, Activities, Potty | One sheet per kind, with that kind's own columns (pee/poo sizes, left/right minutes, pumped amounts, medicine names...) |
| Daily totals | Per day: bottles and ml, breastfeeds and minutes, wet and dirty diapers, sleep minutes, pumps and ml, medicine doses |
| Legend | What every key letter and column means |

The "Agent" tabs in your Google Sheet are replaced by the "Daycare" ones at the next
refresh.

**Audit keys**: `MMDDYY` + who + what + number. `102226SD1` is the first diaper the
**S**ync logged for October 22, 2026; entries made in the app get **U** instead
(`101226UB2` = the second bottle you entered for October 12). Type letters: D diaper,
N nap/sleep, B bottle, F breastfeeding, P pumping, M medicine, S solids, T temperature,
G growth, A activity, O potty. A key never changes once given; an entry added later for
an earlier day gets the next free number.

**On your Mac**: Get Data.command writes `Documents/MillieBeanCounter Data` with
`millie_data.xlsx`, a `csv` folder (one file per sheet), `millie.db` (SQLite) and
`huckleberry_format.csv`. (Earlier exports stay in `Documents/Millie Data` until you
delete them.)

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
email, save the CSV, then double-click **Verify.command** and drag the CSV in.

## What it reads from the daily sheet

* **Bottles**: "Offered 5.25 ounces of breastmilk and took 5 ounces at 1:00pm" becomes a
  5 oz breast milk bottle at 1:00 PM. The amount she *took* is what's logged.
* **Diapers**: Wet = pee, BM = poo, BM/Wet = both, Dry = dry.
* **Naps**: logged as sleep with the exact start and end.
* Check-in/out, meals and anything else on the sheet are left out. The date comes from
  the sheet itself, not from when it was emailed or forwarded.
* Diapers get the note "From daycare" (plus any teacher comment). Bottles get it too
  once py-huckleberry-api 0.4.9 is installed.

It never guesses, never edits or deletes anything in Huckleberry, and skips anything
already there (a diaper or bottle within 15 minutes, or a nap that overlaps).

## Huckleberry login

The sync signs in with the email and password you use in the Huckleberry app. Because
the connection is unofficial, an app update on their side could break it; you'd get a
problem email, and the usual fix is a newer py-huckleberry-api in `requirements.txt`
followed by My Setup.command. When 0.4.9 shows up on PyPI, changing `requirements.txt`
to `huckleberry-api==0.4.9` adds the "offered 5.25 oz" note to bottles.

## Privacy

* Gmail is opened read-only. Nothing is marked read, moved or deleted.
* Settings, passwords, the Google keys and sign-in, and the journal live in the Docker
  volume `millie_bean_counter_data`, not in this folder.
* Google Drive access is limited to what the app itself creates. You can remove it any
  time at <https://myaccount.google.com/permissions> (signed in as the Millie account).
* Putting this folder on GitHub: this guide and `My Setup.command` mention your
  accounts and family, so `.gitignore` keeps both out when you use git or GitHub
  Desktop. If you upload files on the GitHub website instead, leave those two out
  yourself. Everything else is safe to publish: the tests use made-up emails (a child
  called Josephine, "Josie" in Huckleberry), and setup asks every user for their own
  child's name and birthday. The one personal thing left is the project's name.

## If something seems off

* **Nothing showed up for a day:** run Status.command. If it says there were no
  daily-sheet emails, look in the Millie inbox (including Spam). If it's in Spam, mark
  it "Not spam". Then Run Now.command.
* **No photos:** check that the photo emails reach the Millie inbox (step 1 of the
  upgrade) and that Status.command shows no Google Drive problem.
* **"Google Drive's sign-in has expired or was removed":** run My Setup.command and sign
  in again. If it happens about a week after each sign-in, the app is still in Testing:
  publish it (upgrade step 2.4).
* **"Gmail didn't accept the login":** make a new app password for the Millie account
  and run My Setup.command again.
* **"The Google Sheet isn't shared with ...":** share the sheet with that address as an
  Editor.
* **A line "needs a look":** daycare used wording the sync hasn't seen. Add that entry
  by hand. If it keeps happening, the sync can be taught the new wording.

## What has been tested

* The photo saver was built from your real "Shared Moments" email of October 27. The
  tests use a made-up copy with Kaymbu's exact layout (every account, school and link
  detail replaced), plus the same email as Gmail forwards it and as "Forward as
  attachment" sends it. They check the names (your example comes out exactly as
  "10/27 Millie 2m 28d Photo 1" with a 7/29/2026 birthday), folders, numbering,
  duplicates, dead links, interrupted uploads and the Google sign-in.
* Your real October 12 daily sheet and your real Huckleberry export (1,220 entries)
  were checked in earlier versions; those tests still pass, now with S keys.
* Not tested before handing over: the live Gmail, Huckleberry and Google logins (they
  need your passwords), Google's real responses, the Download links (Kaymbu's servers
  aren't reachable from where this was built) and the Docker build on your Mac.
  My Setup.command checks every connection first and stops with a plain explanation if
  one fails.
