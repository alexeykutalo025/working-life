# Recall

**Find every Outlook file on this computer, read what is in them, and search
fifty years of correspondence in one place.**

Everything happens on this computer. Nothing is uploaded, nothing is sent
anywhere, and **your original Outlook files are never changed, moved, renamed or
deleted.** Recall only ever reads them.

---

## Contents

1. [Setting it up, once](#setting-it-up-once)
2. [Using it](#using-it)
3. [The seven screens](#the-seven-screens)
4. [The Problems screen, and what each problem means](#the-problems-screen)
5. [When something goes wrong](#when-something-goes-wrong)
6. [Where your data lives](#where-your-data-lives)
7. [Settings you might want to change](#settings)
8. [Typing commands instead of clicking](#the-command-line)
9. [What is best-effort, and what is certain](#what-is-best-effort)
10. [Decisions made while building this](#decisions-made-while-building-this)

---

## Setting it up, once

**You need Python.** If you do not have it:

1. Go to <https://www.python.org/downloads/windows/>
2. Download the latest **Windows installer (64-bit)**.
3. Run it. On the **first** screen, tick **"Add python.exe to PATH"**. This
   matters; if you miss it, run the installer again and tick it.
4. Finish the installation.

**Then set up Recall.** Double-click **`setup.bat`**.

It takes a few minutes. It creates a private Python environment inside this
folder and installs what Recall needs. It changes nothing else on your
computer.

At the end it prints a table. Read it. Every line says PASS, WARN or FAIL:

- **PASS** — that piece works.
- **WARN** — that piece is missing and the line says exactly what you lose.
  Recall will still run.
- **FAIL** — something is genuinely broken and the line says what to do about
  it. Fix those, then run `setup.bat` again.

One WARN you may see is about **libpff-python**, which reads `.pst` and `.ost`
files quickly. It is published as source code rather than as a ready-made
package, so it sometimes will not install. Recall works without it: Microsoft
Outlook itself is used to read those files instead. That is slower, and it
actually reads *more* file types correctly.

---

## Using it

Double-click **`start.bat`**.

A black window opens and your web browser opens on Recall's Home screen. **Leave
the black window open** while you use Recall — closing it stops the program.
Nothing is lost when you close it: everything is saved as it goes.

### The three things you do, in order

**1. Find your files.**
Go to **Files found** and press **"Find Outlook files on this computer"**. You
will be shown every drive on the computer with its size, all ticked. Untick
anything you want to leave out, then press **Start looking**.

> A first full search of every drive can take **10 to 60 minutes**. That is
> normal. It reads file names, sizes and dates — it does not open anything. You
> can stop it at any time and start it again later; it carries on where it left
> off and never loses what it already found.

**2. Read them.**
When the search finishes you have a list. Press the button to read them into
the archive.

> **Do this first:** read a sample. In the black window, type
> `recall extract --sample 50` and press Enter. That reads the first 50 records
> out of each file, in seconds, so you can check everything looks right before
> committing to a run that may take all night.

A full read of a large collection can run for hours. It is designed to run
overnight without anybody watching. If the computer restarts, start it again
and it picks up from the file it was on.

**3. Look at what you have.**
Now Search, Timeline, People and Problems all have something in them.

---

## The seven screens

### Home
The archive at a glance: how many records, over what years, how many files were
found and read, how much space it uses. Across the top is the **health banner**,
which is always there and cannot be dismissed. It says, in plain words, what is
wrong — *"2 files could not be read in full · 3 unexplained gaps · 6 people to
confirm"*. Clicking it takes you to Problems.

> The button at the top right sets how bright the screen is: **match my
> computer**, **light**, or **dark**. Press it to move between the three.

### Files found
Every Outlook file on this computer, biggest first. For each one: its size, its
date, and its condition — whether it is a duplicate of another file, whether it
is stored in OneDrive and not actually on this computer, whether Outlook is
holding it open, and whether it has been read yet.

This is also where you start a search, start reading, and download cloud-only
files.

**"Find Outlook files on this computer"** offers every drive, ticked to start
with. Untick anything you want left out — searching one drive is much faster
than searching all of them. If you already know where your old mail is, press
**"Choose a specific folder…"** and open folders until you reach it; pointing
Recall straight at one folder takes seconds instead of an hour. Folders you
choose are offered again next time.

A folder that Windows normally skips — `Windows`, `Program Files` and a few
others — *is* searched when you choose it by hand, and the chooser says so.
Those folders are skipped during a whole-drive search only because they waste
time, not because they are forbidden.

### Search
One large box. Type anything you remember.

- Put words in `"quotation marks"` to find that exact phrase.
- Use `AND`, `OR` and `NOT` **in capitals** to combine words. In lower case
  they are just words, because "and" appears in a great many sentences.
- Use `NEAR` to find two words close together: `margaret NEAR invoice`.
- Narrow by a field: `from:margaret`, `subject:invoice`, `inside:contract`
  (that last one searches the text inside attachments).
- Press the **`/` key** at any time to jump back to the box. The up and down
  arrows move through the results and Enter opens one.

Recall searches the subject, the message text, everyone on it, attachment file
names, **and the text inside PDF, Word, Excel and PowerPoint attachments**.

A search will never show you an error message. If what you typed is not a valid
search, Recall looks for the whole thing as a phrase instead and tells you it
did that.

**Readable list or Table.** The list is easier to read: one result at a time,
with the words you searched for marked. The table shows every column, and it is
exactly the table you get in the spreadsheet — same columns, same headings, so
you can check one against the other. Messages, calendar entries and contacts do
not share columns, so the table shows one kind at a time and gives you buttons
to switch.

**"Download these results as Excel"** hands you one workbook. Inside it: a sheet
called **Integrity** first, saying what is missing or uncertain in those exact
records, then a sheet for each kind — Messages, Calendar, Contacts, Tasks,
Notes. A copy is kept in Recall's own exports folder too, so nothing is lost if
you cannot find the download. The other formats — CSV, a readable document, and
JSON — are under "Other ways to save these results".

### Timeline
Everything by date. Year bars first; click a year for its months, a month for
its days. Click any bar to see those records in Search.

**A period with nothing in it is drawn hatched and labelled "no data".** Recall
never draws a line across a gap, never averages one out, and never lets an empty
month look like a small one.

Underneath is **Periods of your life**. You can mark out a stretch of time that
meant something — a job, a company, a move — and it is shaded behind the bars
with its name on it, which makes the picture far easier to read. Dates can be as
rough as you like: a year on its own means the whole year, so *1984* to *1997*
covers the start of 1984 to the end of 1997. Marking a period changes nothing in
the archive; removing one only removes the shading.

If you type a date Recall cannot read, it says so and asks again rather than
storing a blank — a period missing one end would shade the wrong stretch of the
chart. It will not accept a two-digit year either, because *84* could be 1984 or
2084 and this program does not guess dates.

### People
Everyone who appears anywhere in the archive, with when you first and last
corresponded and how much. Click a name for their whole history: every address
they used, a graph of correspondence over time, and who else was on those
records.

At the top is the **merge review queue**. When Recall thinks two entries are one
person it says so, shows you both side by side with the evidence, *and waits*.
It never merges anybody on its own. When you do merge two people, nothing is
deleted and you can separate them again at any time.

### Problems
See the next section. This is the most important screen.

### Item viewer
One record in full: everyone on it, the attachments (which you can save or read
the text of), the conversation it belongs to, and the technical headers.

At the bottom is the **provenance panel**: *"This record was found in 3
files"*, and which ones. Recall stores one copy of a message no matter how many
backups it appears in — and remembers every backup it appeared in.

---

## The Problems screen

**This is the point of the program.**

An archive that quietly lost 1998 is worse than useless, because you would trust
it. So Recall keeps track of everything it could not read, could not date, or
could not be sure about, and puts it all here in plain language.

At the top is the **coverage map**: one row per year, one square per month,
darker where there is more. **A hatched square means nothing at all was found
for that month.** It is the honest answer to *"what do I actually have?"*

Below it, every problem, worst first. Each one says what is wrong, what it
affects, and how many records are involved, with the technical detail behind a
click for when you need to send it to somebody.

### What each problem means

#### Files that could not be read

| What it says | What it means |
|---|---|
| **could not be opened, so nothing in it has been read** | Another program is holding the file — usually Outlook. Close Outlook completely and search again. |
| **about N records could not be read out of M** | The file says how much it holds; Recall got less. Data is missing. Try **Read that file again with the other reader**. |
| **is password-protected** | The file was locked when it was made. Recall will not try to break it. Open it in Outlook once, let Outlook remember the password, then retry. |
| **belongs to an email account this computer no longer has** | An `.ost` with no matching account. It may be the only copy of that mail left. Outlook is the only thing that reads these properly, and Recall tries it automatically. |
| **is not the kind of file its name says it is** | The name says `.pst`, the contents say otherwise. It may have been renamed by hand, or damaged at the start. |
| **is empty** or **too small to be a real ...** | Almost always a copy that failed part-way. |
| **has N folders but not one message came out** | Much more likely a reading failure than an empty mailbox. |
| **the two readers disagree about how much is in it** | At least one reader is missing part of the file. The larger result was kept. |

#### Periods with no data

| What it says | What it means |
|---|---|
| **Nothing at all from *(dates)*** | There are records before and after, and none in between. That can be real — a quiet year, a job change — or it can mean a mailbox has not been found. **Recall cannot tell which and will not guess.** |
| **appears to cover 1996–2004, but nothing from 2001 came out of it** | **Take this one seriously.** A mailbox that was in use either side of a year was almost certainly in use during it. This is a reading failure wearing the costume of a quiet year. Try the other reader. |
| **is much quieter than the year before it** | Not necessarily wrong. Listed so you can say which. |
| **The archive starts in X, but the oldest file dates from Y** | Either the early years were never read, or the file was copied at some point and Windows reset its date. A question, not a conclusion. |

**Explaining a gap.** Press **Explain what was happening** and write down what
you know — *"I wasn't using email yet"*, *"that was the Contract Marketing
server we lost in the move"*. Your note is kept permanently. The gap stops
appearing in the health banner and **stays on the timeline forever**. Explaining
something never deletes it.

#### People and accounts

| What it says | What it means |
|---|---|
| ***A* and *B* may be the same person** | A suggestion, with its evidence. Recall has not merged them. Go to People and decide. |
| **info@... is probably more than one person** | A shared office mailbox. Every count involving it is about a group, not an individual. Recall will **not** split it: there is no way to tell from the mail who wrote what, and guessing would put words in somebody's mouth. |
| **X is almost entirely inside Y** | The same mailbox saved twice. Nothing is deleted. The archive already holds each record once and notes every file it came from. |
| **Is *address* one of your addresses?** | An address sending a lot of mail that is not in your list. If it is yours, add it to `config.toml` (see [Settings](#settings)). |
| **An internal Exchange address never resolved** | Company mail servers identify people internally rather than by email address. Recall keeps the internal name exactly as stored and **does not invent an email address for it**. |
| **contains mail you sent to yourself at another of your addresses** | Ordinary, or a sign the mailbox belongs to a different account than you assumed. |

#### Records with something uncertain

| What it says | What it means |
|---|---|
| **N records have no date** | Not on the timeline, not in any date range, **no date invented**. They are in the Undated list and are searchable like anything else. |
| **dated impossibly** | Before 1970 or in the future. Kept **exactly as found** and never corrected — a corrected date would be an invention. |
| **do not say which timezone they are in** | The date is right; the time may be out by a few hours. No timezone has been assumed. |
| **had their text worked out rather than read** | The record did not say what character set it used, so Recall worked it out. Accented letters and quotation marks are where a guess goes wrong. |
| **replies answer messages not in the archive** | A few is normal. A lot is evidence that a mailbox is missing — probably a Sent Items store, or a different account. |
| **attachments are listed but their contents are gone** | The archive is wrong about itself. Re-read the files they came from. |
| **repeating entries have a rule Recall could not read** | Shown once, on the start date. No repeats invented — a wrong repeat rule puts meetings in your calendar that never happened. |

### Why numbers sometimes have a ⚠ beside them

Anywhere Recall shows a count that one of these problems affects, it shows the
problem too:

> **12,481 messages** ⚠ *about 8,000 more could not be read from 2 files*

The number is always **exactly what Recall could read**. It is never rounded,
and the estimate of what is missing is never added into it — adding a guess to
a total would make the total a guess.

---

## When something goes wrong

**The black window closed and Recall stopped.**
Nothing is lost. Double-click `start.bat` again.

**"Port 8765 is already being used."**
Recall is probably already running in another window. Close it, or start Recall
on a different port: in the black window type `recall serve --port 8766`.

**A file says "Close Outlook and scan again".**
Outlook holds its own mailbox open and Windows will not let anything else read
it. Close Outlook completely — check the system tray — and search again.

**Reading is taking hours.**
That is expected for a large collection. Leave it. If you need to stop, press
Ctrl and C together in the black window: it finishes what it is writing, saves,
and stops cleanly. Start it again later and it carries on.

**Outlook keeps popping up, or Recall seems stuck on one file.**
Outlook is sometimes waiting for you to click something on a window you cannot
see. Open Outlook yourself, answer whatever it asks, close it, and run Recall
again. Recall gives up on Outlook after two minutes of no progress and carries
on with the next file, so it will not hang forever.

When that happens Recall also closes the invisible copy of Outlook it started,
and tells you it has. This matters: a stuck copy holds your mail profile, so
without clearing it the next attempt would get stuck the same way and you could
not open Outlook yourself to fix it either. **Recall only ever closes an Outlook
it started itself and that has no window on screen** — if Outlook was already
running when Recall asked, it is left strictly alone, even if it is the thing in
the way. In that case the message tells you to end `OUTLOOK.EXE` in Task
Manager, because closing an Outlook you might be typing in is not a decision
this program will make for you.

**Searching does not find something I know is there.**
Check Home: if it says the search index is not complete, type `recall index` in
the black window. If it still does not find it, the record may be in a file that
could not be read — check Problems.

**Something else.**
There is a full record in `workdir\logs\`. The file is named by date. That, plus
the technical detail behind the disclosure on the Problems screen, is what
somebody helping you will need.

---

## Where your data lives

Everything Recall builds is in one folder, **`workdir`**, beside this README:

| | |
|---|---|
| `workdir\archive.db` | The archive itself — every message, event and contact. |
| `workdir\blobs\` | The attachments, stored once each however many messages they appear in. |
| `workdir\logs\` | A full record of every run. |
| `workdir\exports\` | Anything you save out. |

**Back up the `workdir` folder** the way you would back up anything else you
care about. Recall can always rebuild it from your original files, but that
takes hours; a copy of this folder takes minutes.

**Your original Outlook files are not in there.** They are wherever they always
were, unchanged. Recall opens them for reading only. It never writes into a
OneDrive folder, and it refuses to put `workdir` inside one.

---

## Settings

Open **`config.toml`** in Notepad. It is plain text, every setting is explained
in it, and every one has a sensible default.

The one worth filling in early:

```toml
[identity]
me = [
    "tim@thebusinessofgood.org",
    "tmccarthy@contractmktg.com",
]
```

**Every email address you have ever used.** It teaches Recall which messages you
sent, keeps you out of your own list of correspondents, and makes "who did I
write to most" mean something. If you leave it empty, Recall notices addresses
that send a lot and asks you about them on the Problems screen.

Others you might change: which folders to search, how big an attachment can be
before Recall stops looking inside it, and how quiet a month has to be before it
is worth mentioning.

---

## The command line

Everything on the screens can be done by typing, and a few things are easier
that way. In the black window:

```
recall doctor                  check this computer has what Recall needs
recall scan                    find Outlook files (add --roots D: for one drive)
recall extract --sample 50     read 50 records per file, to check it works
recall extract                 read everything (this is the long one)
recall extract --resume        carry on after stopping
recall index                   repair the search index
recall audit                   list everything wrong, worst first
recall audit --report health.md   ...and write it to a file
recall findings list           the same list, shorter
recall findings explain 12 "I wasn't using email yet"
recall people suggest          who might be listed twice
recall people merge --keep 3 --merge 7
recall export csv --kind calendar
recall serve --open            start the web page
recall reset --items           empty the archive, keep the file list
```

`recall audit` exits with an error code if anything critical is still open, so
it can be used to decide whether the archive is fit to rely on.

---

## What is best-effort

Recall is honest about the difference between what it knows and what it is
guessing at.

**Read properly and completely:**
`.pst`, `.ost`, `.msg`, `.eml`, `.mbox`, `.ics`, `.vcs`, `.vcf`, `.olm`,
`.dbx`, `.mbx`.

**Best-effort, and it tells you so on the record:**

- **`.wab`** (Windows Address Book) — the format has never been published and no
  library reads it. Recall searches the file for readable text and keeps the
  email addresses it finds with whatever name sits nearest each one. **That is
  salvage, not parsing**: some contacts may be missing, some names may be
  attached to the wrong address, and phone numbers are not recovered at all.
  Every contact recovered this way says so. For a complete result, open the file
  in Windows Contacts, export a `.vcf`, and let Recall read that.
- **`.pab`** (old Outlook address book) — Microsoft removed support for these
  from Outlook in 2010, and nothing can open them any more. Recall reports what
  the file is and what to do about it, rather than half-reading it into
  plausible-looking rubbish. The contacts were almost certainly copied into
  Outlook's own Contacts when the format was retired, so look in a `.pst`.
- **Reading text out of scanned images** (OCR) — off by default. It needs
  Tesseract installed separately and is slow.
- **Repeating calendar entries from `.pst`/`.ost`** — Outlook stores the repeat
  rule in a packed form only Outlook reads. The fast reader shows the first
  occurrence and says so. Reading the file with Outlook recovers the full rule.

---

## Decisions made while building this

Points where the specification left a choice, and what was chosen.

**A missing timezone does not throw away a known date.** The specification says
never to assume a timezone, and also that undated records go in a visible
Undated bucket. A 1998 calendar entry saying "7:30pm" with no zone has a
*known date* and an *unknown instant*. Recall keeps the date, records the
timezone as unknown, and marks the record wherever its time is shown. A record
goes to Undated only when there is no usable timestamp at all. Nothing is
inferred; the uncertainty is recorded rather than resolved.

**A contact has no date, and that is not a problem.** A contact card records a
person, not something that happened. Flagging every entry in an address book as
"undated" would put thousands of non-problems on the Problems screen and teach
you to ignore it.

**An impossible date does not define the archive's span.** One message dated
1961 would otherwise make every month from 1961 to the real start of the
archive a "gap" — 418 of them, when this was tested. Such records are still
counted, still searchable, and still flagged; they are simply not allowed to
stretch the timeline.

**Deleted Items and Junk are not counted as lost.** They are skipped
deliberately, so counting them as unread would report deleted mail as missing
mail.

**`--sample 50` is not a data loss.** A file read deliberately in part is
recorded as such, so sampling does not report every large mailbox as
catastrophically incomplete.

**Outlook gets a stall timeout, not a total one.** Reading a 40 GB mailbox
through Outlook legitimately takes hours; a corrupt file that makes Outlook put
up a repair dialog never finishes at all. Recall measures the gap between signs
of progress, so a wedged call is caught in two minutes and a slow one runs as
long as it keeps moving.

**One extra database index.** The specification puts a `UNIQUE` constraint on
the `findings` table intending re-runs to be idempotent. SQLite treats NULLs as
distinct in a `UNIQUE` constraint, and nearly every finding has NULLs in those
columns, so that constraint alone would let every `recall audit` pile up another
copy of the same finding. The specified constraint is kept exactly as written
and a second index added beside it so the intent actually holds.

**Web pages are never rendered, only shown as text.** An HTML email is displayed
as its text. Rendering it would make your computer fetch whatever it points at,
and Recall never goes online.

**The project lives at the top of this folder.** The specification's layout
roots at `recall/`; `requirements.txt` and this README were already here, so
this folder is the project and the Python package sits inside it.

---

## What this program will not do

- Modify, move, rename or delete any file it finds.
- Write anything into a OneDrive folder.
- Download a cloud-only file without you saying so, having been shown the size.
- Make any network request, ever. There is no link to the internet anywhere in
  it.
- Guess a date, a timezone, a sender or a recipient.
- Merge two people, or split one, without you deciding.
- Draw a line across a gap in any chart.
- Show a total as complete when something is known to be missing from it.
- Quietly resolve, hide or delete a problem it has reported.
- Let an export leave without a note saying what is missing from it.
- Try to repair one of your files.
