# FinCrime KYC/KYB Operations Hub — demo

A Straive demo of a unified back-office case-management hub for individual and
business KYC/KYB, built to be shown to any client. It reproduces the client's as-is screens — the internal case
manager and a simulated World-Check console — driven by the SOP's L1 → L2 →
L3 workflow, and adds the "Unified Back-Office Case Management" layer the
SOP proposes (§5.2): automated skill-based routing, case on-demand with
capacity, a live tier pipeline with hand-off lag, tracker views that replace
the spreadsheets, an investigation file per escalation, a global audit log,
QC sampling with a scorecard, and a four-eyes Checker 2 on business approvals.

Screening runs in two workflows. **Auto-verified**: no match on any list, or
every watchlist record cleared by the *AI screening rules*, so the screening
case closes itself and a Individuals case completes with no analyst step.
**Human-in-the-loop**: at least one record the rules could not clear, so an
analyst handles only what is left. The console shows *Auto-verified* only on a
case that is closed with nothing more asked of the customer: one the rules
cleared that still waits on the customer's reply to an RFI or a resubmission
shows **Waiting on customer** (badge, list filter, analytics) until it closes.
The "AI" is a deterministic rule book, not
a model — date-of-birth mismatch, gender mismatch, an optional nationality
rule and a sanctions guard, each switchable by the team lead on the AI
Screening page — because a compliance team must be able to read and audit
why a record was cleared.

**The case book.** By default the console opens on the client's
reconciliation log, a 1,000-row file
(`fincrime/filebook/data/kyc_wc_recon_log.csv`): every row is an open onboarding
case at L1 Data Confirming with its World-Check hit, carrying the file's own
case ids, passport numbers, names as the CRM captured them (swapped,
nicknamed or garbled: the confirming task) and World-Check records.
Individuals are Individuals cases (943); companies are companies on the Businesses tenant with
their UBO (57). The AI screening rules clear 592 hits at intake, every one
recorded in the file as a false positive, and leave 408 for analysts, among
them every exact or day/month-transposed date of birth and every company.
Each case page ends with what the file's analyst recorded, for comparison.
`CONSOLE_CASES=demo` brings back the nineteen scripted scenarios (the
walkthrough further down, and what the test suite runs on).

**The case list opens on the open cases.** Sign-in lands on the case list,
and with no filter set it lists only the cases still open (human-in-the-loop
and those waiting on the customer), never a Verified or Rejected one; sort,
page size and Priority Review keep that view. Any filter (a case id, a name,
Status = Verified, Workflow = Auto-verified) searches the whole book, and
Clear goes back to the open cases.

**The AI Agent** (`fincrime/engines/ai_agent.py`) does Data Confirming on
every individual's case. It starts on its own when the server takes its
first request. Claude (`claude-opus-4-8` through Straive's LLM Foundry,
`LLM_FOUNDRY_KEY`) reads every document on the case from its image, rendered
to PNG with resvg: the passport's data page and back page, the secondary ID
and the proof of address. Fixed checks, not the model, compare that read
with the case: readable, name (the file book's name rules: nicknames, word
order, initials, a garbled CRM encoding; not the English first name a
naturalised citizen goes by, which only the naturalisation certificate
settles), date of birth, nationality, ID
number, expiry, the vendor's result, an endorsement on the passport's back
page, the secondary ID (readable, the same person as the passport or the
record, the same date of birth; in a surname the passport does not print it
needs the marriage certificate, even when the record keeps both surnames;
its address is shown, not checked), and the
proof of address (readable, holder's name, address, age, type, country).
Every check passes: the agent confirms the data through the analysts' own
path, and corrects the record's postal code or old place names to the proof
of address's (never a code another document prints differently). A case whose World-Check alerts the AI screening rules cleared is then
Verified with nobody touching it; a case with an alert left goes to Data
Screening, where an analyst clears the alert, with the agent's **watchlist
finding** beside each open record (*likely true match*: the passport's name
is the listed name and its date of birth the listed one, or the same with
day and month swapped; *possible match*; *likely a different person*). The
finding shows on the case, the screening case and the case list's filter;
it decides nothing. Some failures the agent settles itself, when every
other check passed, no alert is left and nobody holds the case:

- *The record's own slip, where the documents agree with one another*: the
  agent corrects the record, writes the change to the change log and the
  audit, confirms, and the screening re-runs on the corrected record.
  Claude's verdict is asked again on it. This covers a record that kept the
  father's name and dropped the holder's own (PRAKASH MORE for the
  passport's MORE, MANASI PRAKASH), a surname no document prints beside the
  holder's given name (a married name written on the form), an English first
  name no document prints when the secondary ID is the naturalisation
  certificate (KEVIN NGUYEN for the passport's NGUYEN, KHOA QUOC: the record
  takes the legal name and keeps the typed one as the *preferred name*, as
  the file's analysts did), and a PO Box where the proof of address and the
  secondary ID give the same home.
- *What only the customer can give*: the agent asks for it through the
  analysts' own steps. Resubmit POA covers a proof of address older than
  six months. An RFI covers the marriage certificate that links the
  passport's name to the record's or the secondary ID's (or the
  naturalisation certificate, when the case does not already hold it), or
  which of two homes is the
  main one. A case waiting on the agent's RFI leaves the queue until
  someone takes it. The list's *AI Agent* filter shows these as *Asked the
  customer*, and a request approves nothing.

Anything blurred, cut off,
expired, missing or different, a declined ID, or a model call that fails:
the case stays with an analyst, labelled human-in-the-loop, and its page
shows the reasons and what the agent read. An unreadable or covered ID stays
with a person rather than being asked for again (what it hides may be the
point), and so does a spelling the documents themselves disagree on.
Companies, and cases a person already holds or has worked,
are the analysts'. Two cases at a time (the gateway refuses more concurrent
calls), about 940 cases and three to four hours for the file; each read is
stored, so a restart does not read a case twice (a case read before the
agent read every document, that nobody has touched, starts again and is read
again). A team lead can pause and resume it on the AI Screening page.
Auto-verification also refuses an expired or vendor-declined ID now,
whoever confirmed the data.

**Bad photos of the ID.** The planted capture defects copy what real
uploads that needed a person looked like: a phone lying on the passport,
a page shot at an angle with a phone over its corner, a cracked and worn
card, a page cut off by a shot taken too close, a dark blurred shot. They
are drawn as phone photos on a cloth (`fincrime/filebook/documents.py`), and
each hides at least a date or the number, so the fixed checks fail even if
the model names nothing. The ID vendor passes the first three, as it did on
the real uploads; only the AI Agent's look at the picture stops them. Claude
names the problem (covered, angled, damaged, cut off, blurred, too dark) and
says whether all four edges and the MRZ are in shot (a slight tilt alone
stops nothing: good photos have one); the case goes to a
person with a suggested Resubmit ID reason (for example *Please resubmit a
clear, good quality valid proof of identity and ensure all information on
the document is visible*), which the Resubmit dialog arrives filled in
with. The analyst still decides and clicks OK. A stored case whose ID image
was redrawn since it was read starts again from the file, so only those
are read a second time.

**Upload cases** (the button on the case list, `/kyc/upload`) adds new
onboarding cases from a CSV in the reconciliation log's layout; the columns
that record what an analyst decided may be left out. Each row goes through
intake like the file's: the rules screen it, an auto-verified one is read by
the AI Agent at once, the rest go to analysts. Existing case ids are
skipped, up to 500 rows a file. *Download a sample* gives five new cases
(two the agent should verify, a CRM surname spelt another way, an exact
date-of-birth hit, a company). Uploads are stored and rebuilt on restart
until Reset demo.

The console's names and colours are client-neutral: the two onboarding
lines are **Individuals** and **Businesses**, customer tiers are Tier 1–4,
the top tabs are Monitoring / Reports / Administration and the accent is a
petrol teal. Internally the tenants keep their old keys (`ttlive`, `ttshop`)
because behaviour hangs on them; `vocab.TENANTS` holds every label, and
records stored under the old names are relabelled on load
(`loader.relabel`, `vocab.RELABEL`).

Built the same way as the ChargeShield demo: one Flask `app.py`, Jinja
templates, a single stylesheet, a SQLite state store, no JS framework.

All people, companies, documents and screening records are fictional. The
identity documents are drawn as SVGs from the seed data and carry a
"SPECIMEN" watermark. Vendor names appear as text labels only.

Links that open the World-Check console, from the in-house CMS case, its
Alert Outcome dialog and the Audit Log, open it in a new tab marked with a
small arrow-box icon, so the screening case stays where the analyst left it.
Every other link stays in the same tab.

## Run

```
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

Open http://127.0.0.1:8010 — sign in with any analyst profile and the demo
password shown on the card (`1234`). Always run through the project `.venv`:
the Anaconda Python on this machine has a broken `_sqlite3` and the plain
Python 3.13 has no Flask.

The startup line names the console's case book. Set `CONSOLE_CASES=demo`
(in the environment or `.env`) for the scripted scenarios instead of the
reconciliation log; switching books clears the console's stored state.

## Deploy — a link anyone can open

**Permanent, free (Render).** Push the repo to GitHub, then in Render choose
*New → Blueprint*, connect the repo and create: `render.yaml` sets it up
(one gunicorn worker, debug off, a generated secret key). The free plan
sleeps after 15 minutes idle, so the first visit after a pause takes about
a minute, and its disk is wiped on every deploy or restart, which only
brings the seeded case book back. `Procfile` covers Railway and similar
hosts the same way. Keep the repository private: the demo copies the
client's internal screens.

**Right now, from this laptop.** Run a production copy (debug off, its own
state file) and open a Cloudflare quick tunnel to it. No account needed;
the link lives while both commands run, and changes on every restart.

```
set DEBUG=0 & set SECRET_KEY=<any long random string> & set FCC_STATE_DB=%TEMP%\fcc_public.db
.venv\Scripts\python.exe -m waitress --listen=127.0.0.1:8020 --threads=8 app:app
cloudflared tunnel --url http://127.0.0.1:8020
```

Either way the sign-in card shows the demo password. Set
`STRAIVE_DEMO_PASSWORD` (see `.env.example`) to hide it behind a password of
your own.

**Model assessment (a real model).** Set `LLM_FOUNDRY_KEY` (and, if
needed, `LLM_FOUNDRY_URL` and `LLM_MODEL`; see `.env.example`) and every
screening case gains an **AI assessment** button: Claude, reached through
Straive's LLM Foundry gateway, reads the client record and the watchlist
record side by side and writes a verdict, a confidence, its reasons and the
data it found missing on the ticked alerts (or every pending one), with an
audit row on each case. It is advisory: it resolves nothing, never runs at
intake, and the AI Screening page shows its status and a switch. On Render
the blueprint asks for the key; leave it blank to run without the feature.
The case data is fictional, so nothing sensitive leaves the demo.

## Tests

```
.venv\Scripts\python.exe -W error::ResourceWarning -m unittest -v
```

163 tests: `tests/test_upload.py` (the sample, an upload through intake, the
agent started, duplicates and a wrong file refused, uploads surviving a
restart), `tests/test_ai_agent.py` (the AI Agent with the model call stood
in for: the clean path to Verified, a held alert to Data Screening, every
reason a case goes to a person, an old proof of address asked for again and
what is never asked for, the record's slips corrected (the father's name, a
married surname on the form, an anglicised first name beside the
naturalisation certificate, a PO Box) and the RFIs (a second surname beside
the passport's among them), the bad ID photos and the suggested resubmit
reason, the filled-in Resubmit dialog, a redrawn case read again, a case
confirmed under older name or address rules, or asked for a certificate it
holds, decided again on its stored read, a postal code another document disputes, a gateway
error, a rate-limit or dropped-connection retry, the name rules, the worker
on an auto-verified case, Pause/Resume for the team lead),
`tests/test_console_book.py` (the console on the reconciliation
log: the book's shape, the rules at intake against what the file records,
the documents, Data Confirm on cleared, held, garbled-name, old-POA and
company cases, the paged lists, routing and search), `tests/test_filebook.py`
(the engine under the file book: the parsers, the name and address rules
on example rows from the log, the documents),
plus the console's 47 end-to-end tests on the Flask test client, run on the
scripted book: the golden path, resubmit,
escalate, segregation of duties, L2/EDD decisions, Skopenow, Businesses risk
recalculation, routing by skill and capacity, case on-demand, immediate
hand-offs, capacity overrides, trackers and CSV, the investigation-file ZIP,
the global audit log, QC sampling, Checker 2, role enforcement at EDD, POA
checks, fraud typology, World-Check links and the screening Details
button opening in a new tab, the document viewer's controls, the greyed Review button's reasons, refused
actions staying on the page with the error as a notification, the AI
screening rules (the rule book, what it did at intake, auto-verification on
Data Confirm, businesses keeping the human close, rule switches that persist),
the workflow labels across the screens, one outcome written to several
ticked alerts at once, the case list and screening queue filtered and paged
on the server, a routing run that passes over a full tier, a day/month-swapped
date of birth held for an analyst, and the model assessment (with the model
stood in for; the suite never calls the gateway).

## Analyst profiles

| Profile | Roles | Tenants | Skill book | Capacity |
|---|---|---|---|---|
| Amara Iyer | L1 | Individuals | latin, cyrillic | 6 |
| Jon Miranda | L1 | Individuals, Businesses | latin, arabic, KYB | 6 |
| Mario Kim | L1 + L2 | both | latin, KYB | 5 |
| Nisha Rao | L2 | both | latin, cyrillic, arabic, KYB | 4 |
| Ryan Lee | EDD (L3 / PIC) | both | all scripts, KYB | 3 |
| Leah Fernandez | QC / team lead | both | — (takes no cases) | 0 |

The header's analyst switcher jumps between profiles without signing out.
**Change Status** (moving a case by hand) is for L2 and QC; the moved case
still needs the stage's level to Review — Data Confirming is an L1 step —
and a greyed Review button says so in text beside it. Mario holds both L1
and L2.

## A demo script on the reconciliation log (the default book)

1. **Amara** → *Due Diligence*: 943 Individuals cases, 100 a page. The filters,
   the sort and the page run on the server and live in the URL; *Review
   workflow* = Human-in-the-loop leaves the 351 the rules could not clear.
2. *My Work* → **KYC-2026-3011** (Nguyen Ngoc Phuong). The header says
   *Auto-verified*: World-Check record WC-554473 lists a 1967 birth date
   against her 2001. Preview the passport, the CCCD and the CT07 residence
   confirmation: the file's printed names, MRZ line and addresses. The
   *Recorded Outcome* section at the foot shows what the file's analyst
   decided (False Positive - Cleared, M. Chen). Reserve → Review → OK → the
   case verifies itself, signed by System User.
3. **KYC-2026-3146**: the CRM captured the name garbled; the vendor read
   VŨ CÔNG KHÁNH off the MRZ. Correct the name in Data Confirm: the Change
   Log keeps the edit and the case verifies itself.
4. **KYC-2026-3012** (Upadhyay Ashish): an exact date of birth on an
   adverse-media record, so the rules hold it. Confirm → the screening case
   → World-Check record WC-675768 → resolve; the file recorded *True
   Positive - Application Rejected*.
5. **Jon**: **KYC-2026-3046**, a record listing the client's date with day
   and month swapped, is held (the file recorded 14 of its 22 such cases as
   true matches). **KYC-2026-3037**: the proof of address is 223 days old,
   so Data Confirm refuses it and Resubmit POA is the move. Tenant Businesses →
   **KYC-2026-3029** Ramasamy Agro Exports Pvt Ltd: the certificate of
   incorporation and the UBO's passport; a business goes through Checker 1
   and Checker 2.
6. **Mario**: **KYC-2026-3118** is a flat scan the vendor declined (Resubmit
   ID); **KYC-2026-3040**'s passport fails its MRZ check digit (escalate as a
   fraud ID); **KYC-2026-3043** lists a year of birth only and is held.
7. *AI Agent*: a few minutes after the server starts, **KYC-2026-3011** is
   Verified by itself; its page shows the AI Agent card with every check
   passed. A bad photo of the passport stays in Data Confirming with the
   reasons and a suggested Resubmit ID reason: **KYC-2026-3088** has a phone
   lying on it, **KYC-2026-3027** is cracked and worn, **KYC-2026-3832** is
   shot at an angle, **KYC-2026-3217** is cut off, **KYC-2026-3198** is
   blurred. *AI Screening* shows the agent's
   progress; *Due Diligence* → filter *AI Agent* shows what it handled.
8. *AI Screening* → *Against the reconciliation log*: 592 cleared by the
   rules, 592 recorded as false positives; the 351 left for analysts hold
   every true match.
9. **Nisha** → *Routing*: 935 unassigned, the 100 most urgent with the
   router's pick; **Route selected now** fills the free slots and stops.
10. **Reset demo** reloads the file book.

The document images are drawn from the file's data (it carries none); the
capture defects on about 9% of cases are planted, and the case page says
so. They are synthetic drawings of the kinds of failure real
uploads show; no real customer's document is in the repository.

## A demo script on the scripted book (`CONSOLE_CASES=demo`)

0. The case list's filter panel folds behind the arrow at its top right and
   stays folded through live refreshes; the buttons row keeps working and a
   folded panel says how many filters are active.
1. **Amara** → *Operations Hub › My Work* → **Get next case**: the router picks
   the most urgent case her skill book allows and opens it, reserved. The
   header shows the routing chip and the reason.
2. Open **Dmitri Volkov** (Russian passport, Russian proof of address) →
   *Review*: the Data Confirm modal shows the POA checks; two fail, OK stays
   off, **Escalate** preselects the sanctioned-country reason. Escalate with
   a fraud typology → the case is handed to L2 at once; the flash names who.
   The document panel here, and the **Preview** dialog on the case page,
   zoom with the mouse wheel, pan by drag, and have zoom, fit and rotate
   buttons.
3. Open **Michael James Taylor** for the auto-verified workflow: the header
   already says *Auto-verified* — the AI screening rules cleared his two
   namesakes at intake (a 23-year date-of-birth gap, a female record) and
   the screening case closed itself. Reserve → confirm the three fields →
   the case is Verified with no analyst step, signed by System User, the
   export attached. Then open **Daniel Mercer** for the human-in-the-loop
   golden path: six of seven records cleared by the rules, one record with
   no listed date of birth left for Amara → Reserve → confirm → screening
   case → World-Check resolve + export → alert and case outcome → EDD
   Review → Verified.
4. **Nisha** → *My Work* shows the hand-off; *Pipeline* shows the L2 column
   and the hand-off feed with lag 0 s against the bot-era history.
5. **Jon**, tenant Businesses → a business in Data Screening → Recalculate Risk
   Rating → EDD Review "No match - Approve" → the business waits for
   **Checker 2**; Jon cannot sign it off. **Nisha** → L2 / EDD Queue → the
   Checker 2 row → Review → Approve → Verified.
6. *Trackers*: the Productivity / Time, Fraud and EDD / Businesses Escalation
   trackers, filled by the system, with CSV. Open an escalated case's
   **Investigation File** and download the ZIP.
7. **Leah** → *QC Sampling* → fail one sampled case with a reason → the
   closing analyst's My Work shows the feedback to acknowledge. *Routing*:
   lower an L2's capacity to 1, escalate another case, watch it wait for
   capacity, raise it, **Route selected now**. *AI Screening*: switch the
   nationality rule on, then confirm Mercer's data as Amara — the seventh
   record clears and the case auto-verifies; the change sits in the history.
8. *Analytics*: tier pipeline, backlog ageing, shift progress, hand-off lag,
   quality scorecard, auto-verified vs human-in-the-loop, rules fired.
   *Audit Log*: one stream across every record; the rules sign their own.
9. **Reset demo** in the header brings the seeded case book back.

## Layout

| Path | What lives there |
|---|---|
| `app.py` | Flask app: sign-in, tenant switch, thin routes, state glue, the Operations Hub routes |
| `fincrime/data/` | vocabularies, seed builders, the 19 scenarios, the reconciliation-log book (`recon_book.py`), loader, SQLite state store |
| `fincrime/engines/` | KYC workflow state machine, AI screening rules, routing (skill book, capacity, hand-offs), work log, name-screening, World-Check logic, SLA, segregation, QC sampling, investigation file, PDF export |
| `fincrime/documents/` | synthetic ID / proof-of-address SVG cards |
| `fincrime/analytics/` | view-model builders: case list, queues, My Work, pipeline, trackers, audit log, routing console, AI screening, dashboard |
| `fincrime/adapters/` | mock IDV vendor, in-house CMS and Skopenow |
| `templates/` | flat Jinja templates; `_name.html` are partials/macros |
| `static/css/style.css` | the one stylesheet (light console + dark `body.wc-theme`) |
| `tests/` | end-to-end flow tests on the Flask test client |

## How the hub maps to the SOP

| SOP | Where |
|---|---|
| §2.1 QC sampling, scorecards, coaching | QC Sampling page, scorecard on Analytics, feedback on My Work |
| §3.1 SLAs 48 h / 14 d | SLA chips, breach KPIs, pipeline flags |
| §4.1.2 Data Confirming, POA checks, resubmit reasons | Data Confirm modal |
| §4.1.3 screening, WC export, Skopenow, escalation, fraud typology, EDD tracker, investigation folder | screening case, World-Check console, EDD Review, Escalate dialog, Trackers, Investigation File |
| §5.1 current automation: rule-based disposition of screening hits; the auto-verified and human-in-the-loop workflows | AI screening rules at intake and at the re-screen, auto-verification on Data Confirm, AI Screening page, workflow badge and filter |
| §4.2 Businesses Checker 1 / Checker 2, risk rating, Pending EDD | Agency panel, EDD Review, Checker 2 sign-off, L2 / EDD queue |
| §5.2.1 automated routing, no spreadsheets | routing engine on every hand-off, Routing console, Trackers |
| §5.2.2 dynamic capacity, case on-demand | My Work (Get next case / Request cases), capacity per analyst |
| §5.2.3 L1 → L2 → L3, real-time pipeline, no hand-off lag | Pipeline page, hand-off feed, lag metric |
| §5.2.4 live dashboards, audit logs | Analytics, Audit Log, live refresh on every page |

The state store keeps every action across restarts; a change to the seed's
shape bumps `SEED_VERSION` and clears it. Uploads are never stored — only
the file name is echoed into the audit trail.
