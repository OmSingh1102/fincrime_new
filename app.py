"""FinCrime KYC/KYB Operations Hub — demo.

One Flask module, thin routes, all logic in the fincrime/ package. Built the
way the ChargeShield demo is built: no factory, no blueprints, demo data
loaded at import so the reloader's two processes agree, a SQLite state store
that survives a restart, one stylesheet, no JS framework.

Sections, in order: Modular imports · App Setup · Sign-in, analysts and roles
· Tenant · Nav tables · State · Context processor · Auth routes · KYC case
management · Documents · Name screening · World-Check (simulated) · Queues ·
Operations hub (My Work, Pipeline, Trackers, Investigation file, Audit log,
QC, Routing) · Analytics · Demo affordances · Main.
"""
import hmac
import os
import re
import threading
import time
from functools import wraps
from urllib.parse import urlsplit

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)

try:  # A deployed copy reads .env (see .env.example); a dev checkout has none.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# ─── Modular imports ───────────────────────────────────────────────────────────
from fincrime.adapters import idv_vendor
from fincrime.analytics.audit_log import AuditLogConsole
from fincrime.analytics.case_list import PAGE_SIZES, PER_PAGE, CaseListConsole, read_query as read_case_query
from fincrime.analytics.ai_console import AiScreeningConsole
from fincrime.analytics.dashboard import AnalyticsConsole
from fincrime.analytics.pipeline import PipelineConsole
from fincrime.analytics.queues import QueueConsole
from fincrime.analytics.routing_console import RoutingConsole
from fincrime.analytics.trackers import TrackerConsole, to_csv
from fincrime.analytics.work import WorkConsole
from fincrime.data import vocab
from fincrime.data import recon_book
from fincrime.data.loader import SEED_VERSION, book_from, index_by_id, load_seed, relabel
from fincrime.data.state_store import StateStore
from fincrime.documents import svg_cards
from fincrime.engines import (ai_agent, ai_review, ai_screening, investigation, qc, recommendation, routing, screening, segregation, sla,
                              worldcheck, workflow)
from fincrime.engines.errors import WorkflowError
from fincrime.engines.wc_report_pdf import render_wc_export
from fincrime.utils import dates
from fincrime.utils.text import display_name, initials, translit

# ─── App Setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "fincrime-dev-key")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

# The state store must never raise — its restore runs at import, so an
# unwritable or locked database would take the app down rather than merely
# losing a decision. It reports here instead.
StateStore.on_error = app.logger.warning

DEBUG = os.environ.get("DEBUG", "1") == "1"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8010"))


def _who(value):
    """An email as a display name; anything else (system, FCC Router) as is."""
    return display_name(value) if "@" in (value or "") else (value or "-")


# Jinja *globals*, not context-processor values: a macro imported with
# {% from "_badges.html" import ... %} does not see the render context, but it
# does see globals, and the badge macros need the status colour map.
app.jinja_env.globals.update(status_tone=vocab.STATUS_TONE, vocab=vocab,
                             icon=app.jinja_env.get_template("_icon.html").module.icon,
                             ai_summary=ai_screening.summary, shown_workflow=ai_screening.shown_workflow)
app.jinja_env.filters.update(wc_date=dates.wc_date, wc_dt=dates.wc_datetime, who=_who, translit=translit)

# ─── Sign-in, analysts and roles ───────────────────────────────────────────────
# Demo gate only: one shared password, a profile picker, the session cookie
# signed with the dev secret above. It decides which analyst the app records
# as the actor — it is not a security control.
DEMO_PASSWORD = os.environ.get("STRAIVE_DEMO_PASSWORD", "1234")
DEMO_MODE = os.environ.get("DEMO_MODE", "1") == "1"
# The console's case book: "file" (the default) is the 1,000 cases of the
# reconciliation log the client sent; "demo" is the nineteen scripted
# scenarios and their filler, which the test suite and the original
# walkthrough use. Read once; /reset reloads the same book.
CASE_BOOK = book_from(os.environ.get("CONSOLE_CASES"))
CASE_BOOK_LABEL = {"file": "reconciliation log (1,000 cases)", "demo": "scripted demo (19 scenarios + filler)"}
# The records the console owns in the state store; a new seed stamp clears
# only these.
CONSOLE_KINDS = ("kyc", "alert", "wc", "analyst", "settings", "upload")
# Only advertise the password on the card while it is the built-in one.
_SHOW_DEMO_HINT = "STRAIVE_DEMO_PASSWORD" not in os.environ

ROLE_LABEL = {"L1": "L1 Analyst", "L2": "L2 Analyst", "EDD": "EDD Analyst", "QC": "QC Lead"}
PUBLIC_ENDPOINTS = {"portal", "login", "logout", "static"}

ANALYSTS = []            # filled by _load_seed
ANALYST_BY_KEY = {}
ANALYST_BY_EMAIL = {}


def _analyst():
    return ANALYST_BY_KEY.get(session.get("user"))


def _actor() -> str:
    return session.get("email", "")


def _levels() -> set:
    return set(session.get("roles") or [])


def _analysts_with(levels, tenant) -> str:
    """'Amara Iyer, Jon Miranda or Mario Kim': who holds one of `levels` on
    `tenant`, for the hints that say who can act."""
    names = [a["name"] for a in ANALYSTS if set(levels) & set(a["roles"]) and tenant in a["tenants"]]
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


def _check_password(password: str) -> bool:
    return hmac.compare_digest((password or "").encode("utf-8"), DEMO_PASSWORD.encode("utf-8"))


def _safe_next(target: str, fallback=None) -> str:
    """Only follow same-site paths. Rejects absolute and protocol-relative URLs."""
    if target and target.startswith("/") and target[1:2] not in ("/", "\\"):
        return target
    return fallback or url_for("kyc_list")


def _back(fallback=None) -> str:
    """The page the request came from, when it is one of ours; else home. A
    refused action leaves the analyst where they were, with the reason shown
    as a notification (see _flashes.html)."""
    ref = urlsplit(request.referrer or "")
    if ref.scheme in ("http", "https") and ref.netloc == request.host and ref.path.startswith("/"):
        return ref.path + (f"?{ref.query}" if ref.query else "")
    return fallback or url_for("kyc_list")


def role_required(*levels):
    """Restrict a view to analysts holding one of the given levels. A
    signed-in analyst without the level is sent back to the page they came
    from (home when unknown) with the reason as a notification — the nav
    never offered them the link, but a shared button such as Reserve can
    still be pressed."""
    allowed = set(levels)

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("user"):
                return redirect(url_for("login", next=request.path))
            if allowed and not (_levels() & allowed):
                flash(f"That action needs {' or '.join(sorted(allowed))} access.", "error")
                return redirect(_back())
            return view(*args, **kwargs)
        return wrapper
    return decorator


@app.before_request
def _require_sign_in():
    """Everything outside PUBLIC_ENDPOINTS needs a session. One hook rather
    than a decorator on each route: easier to audit, impossible to forget."""
    if request.endpoint is None or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not session.get("user") or session.get("user") not in ANALYST_BY_KEY:
        for key in ("user", "email", "name", "role", "roles", "tenant"):
            session.pop(key, None)
        return redirect(url_for("portal"))
    # A deep link may name the tenant, the way the real tool's URLs do.
    wanted = request.args.get("tenant")
    if wanted in vocab.TENANTS and wanted in (_analyst() or {}).get("tenants", []):
        session["tenant"] = wanted
    return None


# ─── Tenant ────────────────────────────────────────────────────────────────────
def _tenant() -> str:
    a = _analyst() or {}
    allowed = a.get("tenants") or ["ttlive"]
    t = session.get("tenant")
    return t if t in allowed else allowed[0]


# ─── Nav tables ────────────────────────────────────────────────────────────────
TOP_TABS = [
    {"label": "Case Management", "endpoint": "kyc_list"},
]

# Which side-nav entry a page belongs to, by endpoint prefix. Order matters:
# `kyc_` catches kyc_file / kyc_qc_review before the hub prefixes.
_NAV_OWNER = (("kyc_", "kyc_list"), ("document", "kyc_list"), ("ns_", "ns_queue"), ("wc_", "ns_queue"),
              ("queue_", "queue_l2"), ("analytics", "analytics"),
              ("work", "work"), ("pipeline", "pipeline"), ("trackers", "trackers"), ("audit_log", "audit_log"),
              ("ai_", "ai_screening"), ("qc_", "qc_sampling"), ("routing_", "routing_console"))


def _nav_owner(endpoint: str) -> str:
    for prefix, owner in _NAV_OWNER:
        if (endpoint or "").startswith(prefix):
            return owner
    return ""


def _qc_pending_count():
    """Memoised per request: the nav and the QC page both ask."""
    if not hasattr(g, "qc_pending"):
        g.qc_pending = len(qc.pending(KYC_CASES, _tenant()))
    return g.qc_pending


def _side_nav():
    """The left menu in sections: the cases an analyst works, the screening,
    the hub SOP 5.2 asks for on top of the as-is screens, the reports."""
    owner = _nav_owner(request.endpoint)
    levels = _levels()
    tenant = _tenant()
    actor = _actor()
    open_l1 = sum(1 for c in KYC_CASES if c["tenant"] == tenant and c["status"] in ("DataConfirming", "DataScreening"))
    open_l2 = sum(1 for c in KYC_CASES if c["tenant"] == tenant and c["status"] == "DataEscalatingL2"
                  and not segregation.hidden_from(c, actor, "L2"))
    cases = [
        {"label": "Due Diligence", "title": "Due Diligence (KYC/KYB)", "icon": "user-check", "endpoint": "kyc_list",
         "active": owner == "kyc_list", "count": open_l1},
        {"label": "My Work", "icon": "clipboard", "endpoint": "work", "active": owner == "work",
         "count": routing.load(actor, KYC_CASES) if actor else 0},
    ]
    if levels & {"L2", "EDD", "QC"}:
        cases.append({"label": "L2 / EDD Queue", "icon": "layers", "endpoint": "queue_l2", "active": owner == "queue_l2", "count": open_l2})
    screening = [
        {"label": "Name Screening", "icon": "search", "endpoint": "ns_queue", "active": owner == "ns_queue"},
        {"label": "AI Screening", "icon": "check-circle", "endpoint": "ai_screening_page", "active": owner == "ai_screening"},
    ]
    hub = [
        {"label": "Pipeline", "icon": "trending-up", "endpoint": "pipeline", "active": owner == "pipeline"},
        {"label": "Trackers", "icon": "list", "endpoint": "trackers", "active": owner == "trackers"},
        {"label": "Audit Log", "icon": "book", "endpoint": "audit_log", "active": owner == "audit_log"},
    ]
    if "QC" in levels:
        hub.append({"label": "QC Sampling", "icon": "shield-check", "endpoint": "qc_sampling", "active": owner == "qc_sampling",
                    "count": _qc_pending_count()})
    if levels & {"L2", "QC"}:
        hub.append({"label": "Routing", "icon": "sliders", "endpoint": "routing_console", "active": owner == "routing_console",
                    "count": len(routing.unassigned(tenant, KYC_CASES))})
    return [
        {"title": "Cases", "items": cases},
        {"title": "Screening", "items": screening},
        {"title": "Operations Hub", "items": hub},
        {"title": "Insights", "items": [
            {"label": "Analytics", "icon": "bar-chart", "endpoint": "analytics", "active": owner == "analytics"}]},
    ]


# ─── State ─────────────────────────────────────────────────────────────────────
# Bumped on every write; polled by _live_refresh.html so a second screen
# notices a change made on the first.
STATE_VERSION = 0

KYC_CASES, ALERT_CASES, WC_CASES = [], [], []
KYC_BY_ID, ALERT_BY_ID, WC_BY_ID = {}, {}, {}
AI_SETTINGS = {}         # the AI screening rules' switches; one stored record, kind "settings"


def _bump_state():
    global STATE_VERSION
    STATE_VERSION += 1


def _rebuild_indexes():
    KYC_BY_ID.clear(); KYC_BY_ID.update(index_by_id(KYC_CASES))
    ALERT_BY_ID.clear(); ALERT_BY_ID.update(index_by_id(ALERT_CASES))
    WC_BY_ID.clear(); WC_BY_ID.update(index_by_id(WC_CASES))
    ANALYST_BY_KEY.clear(); ANALYST_BY_KEY.update({a["key"]: a for a in ANALYSTS})
    ANALYST_BY_EMAIL.clear(); ANALYST_BY_EMAIL.update({a["email"]: a for a in ANALYSTS})


def _load_seed():
    """Rebuild the case lists from the seed, then lay stored actions back on
    top. Rebinds the lists in place so every reference stays valid — including
    the router's, which is bound to the list objects.

    A stored seed version that differs from the code's means the record shape
    may have moved; the store is cleared and the log says so. Done only in
    the reloader's child (or outside debug) so the parent process never
    wipes the database on its own boot."""
    started = time.perf_counter()
    analysts, kyc, alerts, wc = load_seed(CASE_BOOK)
    app.logger.info("Console case book: %s; %d cases built in %.2f s.", CASE_BOOK_LABEL[CASE_BOOK], len(kyc),
                    time.perf_counter() - started)
    ANALYSTS[:] = analysts
    KYC_CASES[:] = kyc
    ALERT_CASES[:] = alerts
    WC_CASES[:] = wc
    _rebuild_indexes()
    routing.configure(ANALYSTS, KYC_CASES)
    AI_SETTINGS.clear()
    AI_SETTINGS.update(ai_screening.default_settings())
    ai_screening.configure(AI_SETTINGS)

    in_serving_process = (not DEBUG) or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    stamp = f"{SEED_VERSION}/{CASE_BOOK}"
    stored = StateStore.seed_version()
    if stored and stored != stamp and in_serving_process:
        app.logger.warning("Seed version or case book changed (%s -> %s); clearing the console's stored state.", stored, stamp)
        StateStore.clear(kinds=CONSOLE_KINDS)
        stored = ""
    if not stored and in_serving_process:
        StateStore.set_seed_version(stamp)

    # cases uploaded in the console: rebuilt from their stored rows, oldest
    # batch first, so the actions stored on them below have a case to land on
    if CASE_BOOK == "file":
        for batch in sorted(StateStore.records("upload"), key=lambda b: b.get("at", "")):
            if batch.get("removed_at"):
                continue                # removed on the Upload page: kept as history, no cases
            _add_uploaded([r for r in batch.get("rows", []) if r.get("kyc_case_id") not in KYC_BY_ID])

    redrawn = _redrawn_cases()
    if redrawn:
        app.logger.info("%d stored record(s) were read from an older drawing of their documents, without the proof of "
                        "address's name, or before the agent read every document; they start again.", len(redrawn))
    redecided = _redecided_cases(redrawn)
    skip = set(redrawn)
    for case_id in redecided:
        skip.add(case_id)
        skip.update(r["id"] for r in _linked(KYC_BY_ID[case_id]) if r)
    restored = (StateStore.restore("kyc", KYC_BY_ID, skip=skip) + StateStore.restore("alert", ALERT_BY_ID, skip=skip)
                + StateStore.restore("wc", WC_BY_ID, skip=skip) + StateStore.restore("analyst", ANALYST_BY_KEY)
                + StateStore.restore("settings", {AI_SETTINGS["id"]: AI_SETTINGS}))
    if restored:
        app.logger.info("Restored %d stored record(s) onto the seed.", restored)
        relabelled = relabel(KYC_CASES, ALERT_CASES, WC_CASES, ANALYSTS)
        if relabelled:
            app.logger.info("Relabelled %d stored value(s) to the console's current names.", relabelled)
        resexed = _sync_sex()
        if resexed:
            app.logger.info("%d stored case(s) take their sex from the file's names now.", resexed)
        replaced = _sync_wc_places()
        if replaced:
            app.logger.info("%d stored watchlist record(s) list every place the file gives now.", replaced)
    if redecided:
        app.logger.info("%d case(s) confirmed on a name or an address the checks now refuse, or asked about a name the "
                        "rules now settle, were decided again on their stored read.", _redecide(redecided))


def _sync_sex():
    """A file case's sex comes from what the file says about its holder
    (filebook documents.holder_sex); records stored before that rule carry a coin toss.
    Bring the case, its screening alert and its watchlist case in line with
    the drawn passport. No one can edit the sex, so nothing a person set is
    overwritten. Returns how many cases changed."""
    alerts = {a.get("kyc_case_id"): a for a in ALERT_CASES}
    changed = 0
    for case in KYC_CASES:
        v = recon_book.VIEWS.get((case.get("recon") or {}).get("id", ""))
        if not v or case.get("agency") or case["client"].get("gender") == v["synth"]["sex"]:
            continue
        case["client"]["gender"] = v["synth"]["sex"]
        label = {"M": "Male", "F": "Female"}[v["synth"]["sex"]]
        alert = alerts.get(case["id"])
        if alert:
            alert.setdefault("screening_info", {})["gender"] = label
            if alert["id"] in WC_BY_ID:
                WC_BY_ID[alert["id"]]["gender"] = label
        changed += 1
    return changed


def _sync_wc_places():
    """A file case's watchlist match lists every place the record gives
    (recon_book.wc_places); matches stored before kept only the first. Bring
    them in line: no one edits a match's places, so nothing a person set is
    overwritten. Returns how many matches gained a place."""
    changed = 0
    for case in KYC_CASES:
        v = recon_book.VIEWS.get((case.get("recon") or {}).get("id", ""))
        wc = _linked(case)[1] if v else None
        if not wc:
            continue
        country, locations = recon_book.wc_places(v)
        for m in wc.get("matches", []):
            if m.get("wc_id") == v["wc"]["uid"]:
                changed += m.get("country") != country
                m["country"] = country
                m["key_data"].update(location=locations[0], locations=locations)
    return changed


def _redrawn_cases():
    """Ids (KYC, screening and watchlist) of stored cases whose ID or proof-
    of-address image has been redrawn since they were stored: what was read or decided on them
    was about another picture, so they keep their seeded state and the AI
    Agent reads them again. So do the cases whose stored read lacks the
    proof of address's name (ai_agent.needs_reread), and those decided
    before the agent read every document (ai_agent.read_before_all_documents)."""
    out = set()
    for stored in StateStore.records("kyc"):
        case = KYC_BY_ID.get(stored.get("id", ""))
        if case is None or "recon" not in case:
            continue
        if ((stored.get("recon") or {}).get("drawn", "") != case["recon"].get("drawn", "") or ai_agent.needs_reread(stored)
                or ai_agent.read_before_all_documents(stored)):
            out.add(case["id"])
            alert, wc = _linked(case)
            out.update(r["id"] for r in (alert, wc) if r)
    return out


def _redecided_cases(redrawn):
    """{KYC id: the agent's stored record} of the cases it confirmed on a name
    the checks now refuse (ai_agent.confirmed_on_older_names), asked the
    customer about a name the rules now settle (ai_agent.asked_on_older_names)
    or confirmed with an address it would not write now
    (ai_agent.address_written_otherwise): they keep their seeded state, and
    the agent decides them again on the stored read, with no new look at the
    images. A case read again (`redrawn`) is not decided again."""
    out = {}
    for stored in StateStore.records("kyc"):
        case = KYC_BY_ID.get(stored.get("id", ""))
        if case is None or "recon" not in case or case["id"] in redrawn:
            continue
        if (ai_agent.confirmed_on_older_names(stored, case) or ai_agent.asked_on_older_names(stored, case)
                or ai_agent.address_written_otherwise(stored, case)):
            out[case["id"]] = stored["ai_agent"]
    return out


def _redecide(stored):
    """Decide again the cases _redecided_cases chose, each on its seed and its
    stored read, as the worker would have (a case a person holds on the seed
    stays theirs, as it would for a read again); save them. Returns how many."""
    done = 0
    for case_id, record in stored.items():
        case = KYC_BY_ID[case_id]
        if not ai_agent.eligible(case):
            continue
        reviewer = case.get("current_reviewer", "")
        alert, wc = _linked(case)
        ai_agent.redecide(case, alert, wc, record)
        if reviewer and case["status"] == "Verified":
            workflow.worklog.close_entry(case, "Verified by the AI Agent", email=reviewer)
        _save("kyc", case)
        if alert:
            _save("alert", alert)
            _save("wc", wc)
        done += 1
    return done


def _add_uploaded(rows):
    """Uploaded rows → cases on the live lists, screened at intake. Returns
    (built cases, [(line, reason)] for rows that could not be built)."""
    built, failed = recon_book.add_rows(rows, len(KYC_CASES) + 1)
    for kyc, alert, wc in built:
        KYC_CASES.append(kyc)
        ALERT_CASES.append(alert)
        WC_CASES.append(wc)
    _rebuild_indexes()
    return [k for k, _, _ in built], failed


def _remove_uploaded(ids):
    """Take uploaded cases off the live lists and out of the store, with the
    screening and watchlist records paired with them. The lists are trimmed
    in place, so the router's binding stays valid. Returns how many went."""
    ids = set(ids)
    paired = {a["id"] for a in ALERT_CASES if a.get("kyc_case_id") in ids}   # a watchlist case shares its alert's id
    gone = sum(1 for c in KYC_CASES if c["id"] in ids)
    KYC_CASES[:] = [c for c in KYC_CASES if c["id"] not in ids]
    ALERT_CASES[:] = [a for a in ALERT_CASES if a["id"] not in paired]
    WC_CASES[:] = [w for w in WC_CASES if w["id"] not in paired]
    _rebuild_indexes()
    StateStore.delete("kyc", ids)
    StateStore.delete("alert", paired)
    StateStore.delete("wc", paired)
    for cid in ids:
        recon_book.VIEWS.pop(cid, None)
    _bump_state()
    return gone


def _save(kind: str, record: dict):
    """Persist one touched record and tell the live-refresh poll about it."""
    _bump_state()
    StateStore.save(kind, record)


def _kyc(case_id: str) -> dict:
    return KYC_BY_ID.get(case_id) or abort(404)


def _alert(alert_case_id: str) -> dict:
    return ALERT_BY_ID.get(alert_case_id) or abort(404)


def _wc(wc_case_id: str) -> dict:
    return WC_BY_ID.get(wc_case_id) or abort(404)


def _run_action(fn, *args, **kwargs):
    """Call an engine action; a WorkflowError becomes an error flash and the
    caller redirects. Returns (ok, result)."""
    try:
        return True, fn(*args, **kwargs)
    except WorkflowError as exc:
        flash(str(exc), "error")
        return False, None


def _csv_response(filename, columns, rows):
    return Response(to_csv(columns, rows), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _routing_note(case):
    """One clause for a flash: where the router sent the case."""
    r = case.get("routing") or {}
    if r.get("status") == "routed" and case.get("current_reviewer"):
        return f"Routed to {display_name(case['current_reviewer'])} at once."
    if r.get("status") == "queued":
        return "No analyst has free capacity right now; the case waits in the pipeline."
    return ""


# ─── Context processor ─────────────────────────────────────────────────────────
@app.context_processor
def _inject_nav():
    a = _analyst() or {}
    signed_in = bool(a)
    return {
        "tenants": vocab.TENANTS,
        "tenant": _tenant() if signed_in else "ttlive",
        "analyst_tenants": a.get("tenants") or ["ttlive"],
        "top_tabs": [dict(t, active=(t.get("endpoint") == "kyc_list")) for t in TOP_TABS],
        "side_nav": _side_nav() if signed_in else [],
        "role_label": " / ".join(a.get("roles", [])),
        "user_initials": initials(a.get("name", "")),
        "home_url": url_for("kyc_list") if signed_in else url_for("portal"),
        "demo_mode": DEMO_MODE,
        "switch_users": ANALYSTS if (DEMO_MODE and signed_in) else [],
        "all_analysts": ANALYSTS,
        "status_tone": vocab.STATUS_TONE,
        "state_version": STATE_VERSION,
        # The line-icon macro, delivered to every template including the
        # standalone auth pages. Fetched per render so editing _icon.html
        # shows up without a restart, like any other template.
        "icon": app.jinja_env.get_template("_icon.html").module.icon,
    }


# ─── Auth routes ───────────────────────────────────────────────────────────────
@app.route("/state/version")
def state_version():
    """Cheap staleness probe polled by every open console."""
    return jsonify({"v": STATE_VERSION})


@app.route("/")
def portal():
    """The console's front door: the case list when signed in, else sign-in."""
    return redirect(url_for("kyc_list" if session.get("user") in ANALYST_BY_KEY else "login"))


def _login_page(error="", selected="", next_url="", status=200):
    return render_template("login.html", error=error, selected=selected or ANALYSTS[0]["key"], next=next_url,
                           analysts=ANALYSTS, demo_password=DEMO_PASSWORD if _SHOW_DEMO_HINT else ""), status


def _sign_in(analyst: dict):
    session.clear()
    session["user"] = analyst["key"]
    session["email"] = analyst["email"]
    session["name"] = analyst["name"]
    session["role"] = analyst["role"]
    session["roles"] = list(analyst["roles"])
    session["tenant"] = analyst["tenants"][0]


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "")
    if request.method == "POST":
        key = request.form.get("analyst", "")
        analyst = ANALYST_BY_KEY.get(key)
        if not analyst:
            return _login_page(error="Choose an analyst profile to sign in as.", next_url=next_url, status=400)
        if not _check_password(request.form.get("password", "")):
            return _login_page(error="Incorrect password.", selected=key, next_url=next_url, status=401)
        _sign_in(analyst)
        return redirect(_safe_next(next_url))
    if session.get("user") in ANALYST_BY_KEY:
        return redirect(_safe_next(next_url))
    return _login_page(next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("portal"))


@app.route("/tenant", methods=["POST"])
def set_tenant():
    wanted = request.form.get("tenant", "")
    if wanted in (_analyst() or {}).get("tenants", []):
        session["tenant"] = wanted
    return redirect(_safe_next(request.form.get("next", "")))


@app.route("/switch-user", methods=["POST"])
def switch_user():
    """Demo shortcut: jump between analyst profiles without the login page,
    so the presenter can go from L1 to L2 in one click. Off unless DEMO_MODE."""
    if not DEMO_MODE:
        abort(404)
    analyst = ANALYST_BY_KEY.get(request.form.get("analyst", ""))
    if analyst:
        keep_tenant = session.get("tenant")
        _sign_in(analyst)
        if keep_tenant in analyst["tenants"]:
            session["tenant"] = keep_tenant
        flash(f"Now working as {analyst['name']} ({' / '.join(analyst['roles'])}).", "success")
    return redirect(_safe_next(request.form.get("next", "")))


# ─── KYC case management ───────────────────────────────────────────────────────
def _pager(page, pages):
    """Links for templates/_pager.html: first, last and two either side of
    the current page, each keeping the request's query string."""
    args = request.args.to_dict(flat=False)

    def link(n):
        return url_for(request.endpoint, **dict(request.view_args or {}, **dict(args, page=[str(n)])))

    shown = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    items, last = [], 0
    for n in shown:
        if n - last > 1:
            items.append({"gap": True})
        items.append({"n": n, "url": link(n), "on": n == page})
        last = n
    return {"page": page, "pages": pages, "links": items,
            "prev": link(page - 1) if page > 1 else "", "next": link(page + 1) if page < pages else ""}


def _case_page_size():
    """Rows a page on the case list: ?per_page= (one of PAGE_SIZES) chooses
    it, and the choice holds for the rest of the session, through Search,
    Clear and the menu link."""
    asked = request.args.get("per_page", "")
    if asked.isdigit() and int(asked) in PAGE_SIZES:
        session["case_per_page"] = int(asked)
    size = session.get("case_per_page", PER_PAGE)
    return size if size in PAGE_SIZES else PER_PAGE


@app.route("/kyc/list")
def kyc_list():
    vm = CaseListConsole.compute(KYC_CASES, _tenant(), _actor(), query=read_case_query(request.args),
                                 per_page=_case_page_size())
    return render_template("kyc_list.html", rows=vm["rows"], total=vm["total"], shown=vm["shown"], filters=vm["filters"],
                           query=vm["query"], per_page=vm["per_page"], page_sizes=PAGE_SIZES, first=vm["first"], last=vm["last"],
                           pages=_pager(vm["page"], vm["pages"]), open_only=vm["open_only"], can_upload=CASE_BOOK == "file",
                           can_assign=bool(_levels() & {"L2", "QC"}))      # kyc_bulk_assign's roles


def _linked(case):
    """(screening case, watchlist case) for a KYC case with a hit, else (None, None)."""
    alert = ALERT_BY_ID.get(case["screening"]["alert_case_id"]) if case["screening"]["hit"] else None
    wc = WC_BY_ID.get(alert["id"]) if alert else None
    return alert, wc


_POA_RESUBMIT = {
    "Issued within the last 6 months": "The proof of address is older than 6 months",
    "Accepted document type (internal library)": "The document type is not an accepted proof of address",
    "Not from a sanctioned or prohibited country": "The proof of address is from a prohibited country",
}


@app.route("/kyc/detail/<case_id>")
def kyc_detail(case_id):
    case = _kyc(case_id)
    alert, wc = _linked(case)
    levels, actor = _levels(), _actor()
    l2_level = "EDD" if case["pending_reason"] == "Pending EDD" else "L2"
    stage, needed = workflow.review_needs(case)
    hit = case["screening"]["hit"]
    checks = workflow.poa_checks(case)
    failed = [c for c in checks if not c["ok"]]
    return render_template(
        "kyc_detail.html", case=case, alert=alert, wc=wc, levels=levels, actor=actor,
        points=workflow.verification_points(case), review=workflow.review_action(case, levels, actor),
        is_mine=case["current_reviewer"] == actor, closed=case["status"] in vocab.CLOSED_STATUSES,
        review_needs=(stage, needed), level_names=_analysts_with(needed, case["tenant"]),
        sla=sla.chip(case), vendor=idv_vendor.details(case), confirm_fields=workflow.confirm_fields(case),
        pending_alerts=sum(1 for a in (alert or {}).get("alerts", []) if a["outcome"] == "Pending"),
        exports=(wc or {}).get("exports", []), l2_level=l2_level,
        segregated=segregation.hidden_from(case, actor, l2_level) if case["status"] == "DataEscalatingL2" else False,
        edd_decisions=(list(vocab.EDD_DECISIONS) if hit else ["No match - Approve"] + list(vocab.EDD_DECISIONS)),
        analysts=ANALYSTS,
        poa_checks=checks, poa_failed=bool(failed),
        poa_sanction_fail=any(c["label"].startswith("Sanctioned-country") for c in failed),
        poa_resubmit_reason=next((_POA_RESUBMIT[c["label"]] for c in failed if c["label"] in _POA_RESUBMIT), ""),
        id_resubmit_reason=(case.get("ai_agent") or {}).get("resubmit", "") if case["status"] == "DataConfirming" else "",
        file_items=investigation.items(case, alert, wc) if case.get("investigation_file") else [],
        agent=_agent_view(case, actor, levels))


@app.route("/kyc/detail/<case_id>/audit")
def kyc_audit(case_id):
    return render_template("kyc_audit.html", case=_kyc(case_id), view=request.args.get("view", "audit"))


def _case_action(case_id, fn, *args, success="", open_modal="", extra=(), **kwargs):
    """Run one workflow action on a case as the signed-in analyst; persist on
    success (plus any `extra` (kind, record) pairs the action touched); go
    back to the case, reopening the modal on a validation flash. `success`
    may be a callable taking the case, for messages that depend on the
    outcome (where the router sent it)."""
    case = _kyc(case_id)
    ok, result = _run_action(fn, case, _actor(), *args, **kwargs)
    if ok:
        _save("kyc", case)
        for kind, record in extra:
            if record is not None:
                _save(kind, record)
        text = success(case) if callable(success) else success
        if text:
            flash(text, "success")
    target = url_for("kyc_detail", case_id=case_id)
    if not ok and open_modal:
        target += f"?open={open_modal}"
    return redirect(target)


@app.route("/kyc/<case_id>/reserve", methods=["POST"])
@role_required("L1", "L2", "EDD")
def kyc_reserve(case_id):
    return _case_action(case_id, workflow.reserve, success="Case reserved. You are the current reviewer.")


@app.route("/kyc/<case_id>/release", methods=["POST"])
@role_required("L1", "L2", "EDD")
def kyc_release(case_id):
    return _case_action(case_id, workflow.release, success="Case released to the queue.")


@app.route("/kyc/<case_id>/assign", methods=["POST"])
@role_required("L1", "L2", "EDD", "QC")
def kyc_assign(case_id):
    return _case_action(case_id, workflow.assign, request.form.get("assignee", ""), success="Case assigned.")


@app.route("/kyc/<case_id>/tag", methods=["POST"])
def kyc_tag(case_id):
    return _case_action(case_id, workflow.add_tag, request.form.get("tag", ""))


@app.route("/kyc/<case_id>/modify", methods=["POST"])
@role_required("L1", "L2", "EDD", "QC")
def kyc_modify(case_id):
    return _case_action(case_id, workflow.modify_user_info, request.form, success="User info updated; see the Change Log.",
                        open_modal="modify-modal")


@app.route("/kyc/<case_id>/status", methods=["POST"])
@role_required("L2", "QC")
def kyc_status(case_id):
    return _case_action(case_id, workflow.change_status, request.form.get("status", ""), request.form.get("comment", ""),
                        success="Status changed.", open_modal="status-modal")


def _confirmed_message(case):
    """What Data Confirm led to, now that the AI screening rules run on it."""
    screening = case["screening"]
    if case["status"] == "Verified":
        return "Data confirmed — the AI screening rules left nothing for an analyst, so the case is auto-verified."
    if screening.get("workflow") == "auto-verified" and screening["hit"]:
        return ("Data confirmed — the AI screening rules cleared every alert and the screening case closed itself; "
                "recalculate the risk rating and run the EDD Review to approve the business.")
    if screening.get("workflow") == "auto-verified":
        return "Data confirmed — no screening hit; recalculate the risk rating and run the EDD Review to approve the business."
    left = (screening.get("ai") or {}).get("for_review", 0)
    return f"Data confirmed — the case moved to Data Screening and was re-screened; {left} alert(s) are left for you to review."


@app.route("/kyc/<case_id>/data_confirm", methods=["POST"])
@role_required("L1")
def kyc_data_confirm(case_id):
    alert, wc = _linked(_kyc(case_id))
    return _case_action(case_id, workflow.data_confirm, request.form, alert_case=alert, wc=wc,
                        extra=(("alert", alert), ("wc", wc)), success=_confirmed_message, open_modal="dc-modal")


@app.route("/kyc/<case_id>/resubmit", methods=["POST"])
@role_required("L1")
def kyc_resubmit(case_id):
    return _case_action(case_id, workflow.resubmit, request.form.getlist("decision"), request.form.get("id_reason", ""),
                        request.form.get("poa_reason", ""), request.form.get("note", ""),
                        success="Resubmission requested. The case leaves your queue until the user uploads again.",
                        open_modal="dc-modal")


@app.route("/kyc/<case_id>/escalate", methods=["POST"])
@role_required("L1")
def kyc_escalate(case_id):
    case = _kyc(case_id)
    alert, _wc = _linked(case)
    back = "dc-modal" if case["status"] == "DataConfirming" else "edd-modal"
    return _case_action(case_id, workflow.escalate, request.form.get("reason", ""), request.form.get("comment", ""),
                        request.form.get("fraud_typology", ""), alert_case=alert, extra=(("alert", alert),),
                        success=lambda c: f"Escalated to L2. {_routing_note(c)} The 14-day EDD clock has started.".replace("  ", " "),
                        open_modal=back)


@app.route("/kyc/<case_id>/rfi", methods=["POST"])
@role_required("L1", "L2", "EDD")
def kyc_rfi(case_id):
    return _case_action(case_id, workflow.rfi, request.form.get("message", ""), success="RFI sent to the user.",
                        open_modal="rfi-modal")


@app.route("/kyc/<case_id>/edd_review", methods=["POST"])
@role_required("L1", "L2", "EDD")
def kyc_edd_review(case_id):
    case = _kyc(case_id)
    alert, _wc = _linked(case)
    attachment = request.form.get("attachment", "") or request.form.get("attachment_name", "")
    level = "L1" if "L1" in _levels() else "L2"

    def done(c):
        if c.get("pending_reason") == "Pending Checker 2":
            return f"Checker 1 approval recorded — the business now waits for Checker 2. {_routing_note(c)}".strip()
        if c["status"] == "DataEscalatingL2":
            return f"Escalated to L2. {_routing_note(c)} The 14-day EDD clock has started.".replace("  ", " ")
        return "Review recorded."
    return _case_action(case_id, workflow.edd_review, request.form.get("decision", ""), request.form.get("summary", ""),
                        attachment, alert_case=alert, level=level, extra=(("alert", alert),),
                        success=done, open_modal="edd-modal")


@app.route("/kyc/<case_id>/l2_decision", methods=["POST"])
@role_required("L2", "EDD")
def kyc_l2_decision(case_id):
    case = _kyc(case_id)
    level = "EDD" if case["pending_reason"] == "Pending EDD" else "L2"
    return _case_action(case_id, workflow.l2_decision, level, request.form.get("decision", ""), request.form.get("comment", ""),
                        levels=_levels(),
                        success=lambda c: (f"Escalated to EDD. {_routing_note(c)}".strip() if c.get("pending_reason") == "Pending EDD"
                                           else f"{level} decision recorded."),
                        open_modal="l2-modal")


@app.route("/kyc/<case_id>/checker2", methods=["POST"])
@role_required("L1", "L2")
def kyc_checker2(case_id):
    return _case_action(case_id, workflow.checker2_signoff, request.form.get("decision", ""), request.form.get("comment", ""),
                        success=lambda c: ("Checker 2 sign-off recorded — the business is verified." if c["status"] == "Verified"
                                           else f"Checker 2 decision recorded. {_routing_note(c)}".strip()),
                        open_modal="checker2-modal")


@app.route("/kyc/<case_id>/skopenow", methods=["GET", "POST"])
@role_required("L1", "L2", "EDD", "QC")
def kyc_skopenow(case_id):
    case = _kyc(case_id)
    if request.method == "POST":
        ok, _ = _run_action(workflow.skopenow_clear, case, _actor(), request.form.get("outcome", ""), request.form.get("notes", ""))
        if ok:
            _save("kyc", case)
            flash("Skopenow outcome recorded.", "success")
            return redirect(url_for("kyc_detail", case_id=case_id))
        return redirect(url_for("kyc_skopenow", case_id=case_id))
    return render_template("skopenow.html", case=case, is_mine=case["current_reviewer"] == _actor())


@app.route("/kyc/<case_id>/recalculate_risk", methods=["POST"])
@role_required("L1", "L2", "EDD")
def kyc_recalculate_risk(case_id):
    case = _kyc(case_id)
    ok, rating = _run_action(workflow.recalculate_risk, case, _actor())
    if ok:
        _save("kyc", case)
        flash(f"Risk rating recalculated: {rating}.", "success")
    return redirect(url_for("kyc_detail", case_id=case_id))


@app.route("/kyc/bulk_assign", methods=["POST"])
@role_required("L2", "QC")
def kyc_bulk_assign():
    assignee = request.form.get("assignee", "")
    done = 0
    for case_id in request.form.getlist("case_id"):
        case = KYC_BY_ID.get(case_id)
        if not case:
            continue
        ok, _ = _run_action(workflow.assign, case, _actor(), assignee)
        if ok:
            _save("kyc", case)
            done += 1
    flash(f"Assigned {done} case(s) to {assignee}.", "success" if done else "error")
    return redirect(url_for("kyc_list"))


# ─── Documents ─────────────────────────────────────────────────────────────────
@app.route("/documents/<case_id>/<doc_id>.svg")
def document_svg(case_id, doc_id):
    """A synthetic document drawn from the case data (see documents/svg_cards)."""
    case = _kyc(case_id)
    doc = next((d for d in case["documents"] if d["id"] == doc_id), None) or abort(404)
    return Response(_render_document(case, doc), mimetype="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


def _render_document(case, doc):
    """A reconciliation-log case's passport, secondary ID and proof of
    address are the file book's images (the file's printed names, MRZ and
    addresses); everything else is drawn by svg_cards from the case."""
    if doc.get("hub_file"):
        svg = recon_book.render(case, doc)
        if svg:
            return svg
    return svg_cards.render(case, doc)


# ─── Name screening ────────────────────────────────────────────────────────────
NS_PER_PAGE = 100


def _tenant_alerts():
    t = _tenant()
    return [a for a in ALERT_CASES if (KYC_BY_ID.get(a["kyc_case_id"]) or {}).get("tenant") == t]


@app.route("/name_screening/queue")
def ns_queue():
    level = request.args.get("level", "L1")
    mine = request.args.get("mine") == "1"
    # Row copies, not the records: "pending" is a view field and must not be
    # persisted into the store the next time the record is saved.
    alerts = [dict(a, pending=sum(1 for x in a["alerts"] if x["outcome"] == "Pending"),
                   auto=bool(a["case_outcome"] and a["case_outcome"].get("by") == vocab.SYSTEM_USER)) for a in _tenant_alerts()]
    queues = []
    for lv in ("L1", "L2"):
        rows = [a for a in alerts if a["case_level"] == lv]
        auto_closed = sum(1 for a in rows if a["auto"])
        queues.append({"level": lv, "name": f"{vocab.TENANTS[_tenant()]['cms_queue']}_{lv}", "total": len(rows),
                       "pending": sum(1 for a in rows if a["status"] != "Closed"),
                       "completed": sum(1 for a in rows if a["status"] == "Closed"),
                       "auto_closed": auto_closed, "human": len(rows) - auto_closed})
    rows = [a for a in alerts if a["case_level"] == level and (not mine or a["assigned_to"] == _actor())]
    rows.sort(key=lambda a: (a["status"] == "Closed", a["created_at"]), reverse=False)
    # Paged: a file-backed book puts a thousand screening cases in one queue.
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    pages = max(1, -(-len(rows) // NS_PER_PAGE))
    page = min(page, pages)
    shown = rows[(page - 1) * NS_PER_PAGE: page * NS_PER_PAGE]
    return render_template("ns_queue.html", level=level, mine_only=mine, queues=queues, rows=shown, total=len(rows),
                           first=(page - 1) * NS_PER_PAGE + 1 if rows else 0, last=(page - 1) * NS_PER_PAGE + len(shown),
                           pages=_pager(page, pages))


@app.route("/name_screening/search")
def ns_search():
    q = (request.args.get("q") or "").strip().lower()
    if q:
        # Ids compare without case: the reconciliation log's are KYC-2026-3011 and WC-554473.
        for a in ALERT_CASES:
            k = KYC_BY_ID.get(a["kyc_case_id"]) or {}
            if q in (a["id"].lower(), a["merchant_uid"].lower(), a["kyc_case_id"].lower())                     or q in k.get("client", {}).get("legal_name", "").lower():
                return redirect(url_for("ns_case", alert_case_id=a["id"]))
        for wc in WC_CASES:
            mid = next((m["id"] for m in wc["matches"] if str(m["wc_id"]).lower() == q), None)
            if mid:
                return redirect(url_for("wc_match", wc_case_id=wc["id"], match_id=mid))
    flash(f"No screening case or record matches '{q}'." if q else "Enter a case ID, merchant UID, KYC case ID or name.", "error")
    return redirect(request.referrer or url_for("ns_queue"))


@app.route("/name_screening/case/<alert_case_id>")
def ns_case(alert_case_id):
    alert = _alert(alert_case_id)
    wc = WC_BY_ID.get(alert["id"])
    types = sorted({a["type"] for a in alert["alerts"] if a["type"]})
    kyc = KYC_BY_ID.get(alert.get("kyc_case_id", ""))
    return render_template("ns_case.html", alert=alert, wc=wc, actor=_actor(), types=types,
                           kyc=kyc, watch_labels=ai_agent.WATCH_LABELS, details=_alert_details(alert, wc, kyc),
                           counts=screening.tab_counts(alert["alerts"]), strategies=[r["label"] for r in ai_screening.RULES],
                           recommendation=recommendation.recommend_case_outcome(alert, wc),
                           ai_ready=ai_review.ready(), ai_why=ai_review.why_not(), ai_model=ai_review.MODEL,
                           ai_tone=ai_review.VERDICT_TONE)


def _alert_details(alert, wc, kyc):
    """{alert id: what its row opens to}: the World-Check record behind the
    alert (Overview, Others), and Smart Review Results: the rules' verdicts
    on it, submitted against matched data, the AI Agent's finding from the
    documents."""
    client = (kyc or {}).get("client") or {}
    findings = {f["wc_id"]: f for f in ((((kyc or {}).get("ai_agent") or {}).get("watchlist") or {}).get("matches") or [])}
    out = {}
    for a in alert["alerts"]:
        m = worldcheck.find_match(wc, a["match_id"]) if wc else None
        out[a["id"]] = {"m": m, "rules": ai_screening.review_results(m, client) if m else [],
                        "compare": worldcheck.comparison_rows(wc, m, client) if m else [],
                        "finding": findings.get((m or {}).get("wc_id", ""))}
    return out


def _alert_action(alert_case_id, fn, *args, success="", open_modal="", **kwargs):
    alert = _alert(alert_case_id)
    ok, _ = _run_action(fn, alert, _actor(), *args, **kwargs)
    if ok:
        _save("alert", alert)
        if success:
            flash(success, "success")
    target = url_for("ns_case", alert_case_id=alert_case_id)
    return redirect(target + (f"?open={open_modal}" if not ok and open_modal else ""))


@app.route("/name_screening/<alert_case_id>/handle", methods=["POST"])
@role_required("L1", "L2")
def ns_handle(alert_case_id):
    return _alert_action(alert_case_id, screening.handle, success="You are handling this screening case.")


@app.route("/name_screening/<alert_case_id>/alert_outcome", methods=["POST"])
@role_required("L1", "L2")
def ns_alert_outcome(alert_case_id):
    n = len([i for i in request.form.getlist("alert_id") if i])
    return _alert_action(alert_case_id, screening.alert_outcome, request.form,
                         success="Alert outcome recorded." if n < 2 else f"Alert outcome recorded for {n} alerts.",
                         open_modal="alert-outcome")


@app.route("/name_screening/<alert_case_id>/ai_assess", methods=["POST"])
@role_required("L1", "L2")
def ns_ai_assess(alert_case_id):
    """Ask the model about the ticked alerts (every pending one when none
    are ticked). Advisory: nothing is resolved. What was assessed is saved
    even when a later call fails."""
    alert = _alert(alert_case_id)
    kyc, wc = KYC_BY_ID.get(alert["kyc_case_id"]), WC_BY_ID.get(alert["id"])
    ids = [i for i in request.form.getlist("alert_id") if i]
    ok, res = _run_action(ai_review.assess_alerts, kyc, alert, wc, _actor(), ids)
    if ok:
        done, error = res
        if done:
            _save("alert", alert)
            for kind, record in (("wc", wc), ("kyc", kyc)):
                if record is not None:
                    _save(kind, record)
            flash(f"AI assessment recorded for {len(done)} alert(s) ({ai_review.MODEL}).", "success")
        if error:
            flash(f"Assessment stopped after {len(done)} alert(s): {error}", "error")
    return redirect(url_for("ns_case", alert_case_id=alert_case_id))


@app.route("/name_screening/<alert_case_id>/case_outcome", methods=["POST"])
@role_required("L1", "L2")
def ns_case_outcome(alert_case_id):
    alert = _alert(alert_case_id)
    kyc = KYC_BY_ID.get(alert["kyc_case_id"])
    ok, _ = _run_action(screening.case_outcome, alert, _actor(), request.form, kyc=kyc)
    if ok:
        _save("alert", alert)
        if kyc:
            _save("kyc", kyc)
        flash(f"Case outcome recorded: {alert['case_outcome']['value']}. Back on the KYC case, run the EDD Review.", "success")
        return redirect(url_for("ns_case", alert_case_id=alert_case_id))
    return redirect(url_for("ns_case", alert_case_id=alert_case_id) + "?open=case-outcome")


# ─── World-Check (simulated) ───────────────────────────────────────────────────
def _wc_context(wc):
    alert = ALERT_BY_ID.get(wc["alert_case_id"])
    return {"wc": wc, "alert": alert, "back_url": url_for("ns_case", alert_case_id=wc["alert_case_id"]),
            "can_resolve": bool(alert) and alert["status"] == "Handling" and alert["assigned_to"] == _actor()}


@app.route("/wc/<wc_case_id>")
def wc_case(wc_case_id):
    wc = _wc(wc_case_id)
    status, mtype = request.args.get("status", ""), request.args.get("type", "")
    filtered = worldcheck.filter_matches(wc["matches"], status, mtype)
    page = worldcheck.paginate(filtered, int(request.args.get("page", "1") or 1))
    return render_template("wc_case.html", tab=request.args.get("tab", "worldcheck"), status=status, mtype=mtype,
                           facets=worldcheck.facets(wc["matches"]), page=page, **_wc_context(wc))


@app.route("/wc/<wc_case_id>/match/<match_id>")
def wc_match(wc_case_id, match_id):
    wc = _wc(wc_case_id)
    m = worldcheck.find_match(wc, match_id) or abort(404)
    status, mtype = request.args.get("status", ""), request.args.get("type", "")
    return render_template("wc_match.html", m=m, status=status, mtype=mtype,
                           nav=worldcheck.match_nav(wc, match_id, status, mtype),
                           comparison=worldcheck.comparison_rows(wc, m, (KYC_BY_ID.get(wc["kyc_case_id"]) or {}).get("client")),
                           unresolved=worldcheck.facets(wc["matches"])["status"]["Unresolved"], **_wc_context(wc))


@app.route("/wc/<wc_case_id>/match/<match_id>/resolve", methods=["POST"])
@role_required("L1", "L2")
def wc_resolve(wc_case_id, match_id):
    wc = _wc(wc_case_id)
    ctx = _wc_context(wc)
    f = request.form
    if not ctx["can_resolve"]:
        flash("Handle the screening case first — only its handler can resolve matches.", "error")
    else:
        ok, _ = _run_action(worldcheck.resolve_match, wc, _actor(), match_id, f.get("status", ""), f.get("risk", ""),
                            f.get("reason", ""), f.get("comment", ""))
        if ok:
            _save("wc", wc)
            flash("Match resolved. Record the matching alert outcome on the screening case.", "success")
    return redirect(url_for("wc_match", wc_case_id=wc_case_id, match_id=match_id, status=f.get("status_filter", ""),
                            type=f.get("type_filter", "")))


@app.route("/wc/<wc_case_id>/export", methods=["POST"])
def wc_export(wc_case_id):
    wc = _wc(wc_case_id)
    export = worldcheck.record_export(wc, _actor())
    _save("wc", wc)
    flash(f"Exported {export['filename']} — it is now available to attach in the EDD Review.", "success")
    return redirect(url_for("wc_case", wc_case_id=wc_case_id, tab="summary"))


@app.route("/wc/<wc_case_id>/export.pdf")
def wc_export_pdf(wc_case_id):
    wc = _wc(wc_case_id)
    alert = ALERT_BY_ID.get(wc["alert_case_id"])
    pdf = render_wc_export(wc, alert, KYC_BY_ID.get(wc["kyc_case_id"]), _actor())
    name = (wc["exports"][-1]["filename"] if wc["exports"] else f"WorldCheck_{wc['id'][:8]}.pdf")
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


# ─── Queues ────────────────────────────────────────────────────────────────────
@app.route("/queue/l2")
@role_required("L2", "EDD", "QC")
def queue_l2():
    vm = QueueConsole.compute(KYC_CASES, _tenant(), _actor(), _levels())
    return render_template("queue_l2.html", vm=vm)


# ─── Operations hub: My Work and case on-demand (SOP 5.2.2) ────────────────────
@app.route("/work")
def work():
    return render_template("work.html", vm=WorkConsole.compute(KYC_CASES, ALERT_CASES, _analyst()))


@app.route("/work/next", methods=["POST"])
@role_required("L1", "L2", "EDD")
def work_next():
    ok, routed = _run_action(routing.request_cases, _analyst(), 1, tenant=_tenant(), trigger="next")
    if not ok:
        return redirect(url_for("work"))
    case = routed[0]
    _save("kyc", case)
    flash(f"Routed to you: {case['client']['legal_name']} — {case['routing']['reason']}.", "success")
    return redirect(url_for("kyc_detail", case_id=case["id"]))


@app.route("/work/request", methods=["POST"])
@role_required("L1", "L2", "EDD")
def work_request():
    ok, routed = _run_action(routing.request_cases, _analyst(), request.form.get("n", "1"), tenant=_tenant())
    if ok:
        for c in routed:
            _save("kyc", c)
        flash(f"Routed {len(routed)} case(s) to you: " + ", ".join(c["client"]["legal_name"] for c in routed) + ".", "success")
    return redirect(url_for("work"))


@app.route("/kyc/<case_id>/qc_ack", methods=["POST"])
def kyc_qc_ack(case_id):
    case = _kyc(case_id)
    ok, _ = _run_action(workflow.qc_ack, case, _actor())
    if ok:
        _save("kyc", case)
        flash("QC feedback acknowledged.", "success")
    return redirect(url_for("work"))


# ─── Operations hub: pipeline, trackers, investigation file, audit log (SOP 5.2.3–5.2.4)
@app.route("/pipeline")
def pipeline():
    return render_template("pipeline.html", vm=PipelineConsole.compute(KYC_CASES, _tenant()))


@app.route("/trackers")
def trackers():
    vm = TrackerConsole.compute(KYC_CASES, ALERT_CASES, WC_CASES, _tenant(), request.args.get("view", "productivity"))
    if request.args.get("format") == "csv":
        return _csv_response(f"tracker_{vm['view']}_{_tenant()}.csv", vm["columns"], vm["rows"])
    return render_template("trackers.html", vm=vm)


@app.route("/kyc/detail/<case_id>/file")
def kyc_file(case_id):
    case = _kyc(case_id)
    alert, wc = _linked(case)
    return render_template("investigation_file.html", case=case, alert=alert, wc=wc,
                           items=investigation.items(case, alert, wc))


@app.route("/kyc/<case_id>/investigation_file.zip")
def kyc_file_zip(case_id):
    case = _kyc(case_id)
    alert, wc = _linked(case)
    actor = _actor()
    name, data = investigation.zip_bytes(case, alert, wc,
                                        render_pdf=lambda w, a, k: render_wc_export(w, a, k, actor),
                                        render_doc=_render_document)
    return Response(data, mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.route("/audit")
def audit_log():
    vm = AuditLogConsole.compute(KYC_CASES, ALERT_CASES, WC_CASES, _tenant())
    if request.args.get("format") == "csv":
        rows = [[r["at"], r["actor"], r["kind_label"], r["record_id"], r["label"], r["action"], r["detail"]] for r in vm["rows"]]
        return _csv_response(f"audit_log_{_tenant()}.csv", ["At", "Actor", "Record", "Record ID", "Name", "Action", "Detail"], rows)
    return render_template("audit_log.html", vm=vm)


# ─── Operations hub: QC sampling (SOP 2.1) ─────────────────────────────────────
@app.route("/qc")
@role_required("QC")
def qc_sampling():
    tenant = _tenant()
    sample = qc.sample(KYC_CASES, tenant)
    return render_template("qc.html", sample=[{"case": c, "sla": sla.chip(c)} for c in sample],
                           scorecard=qc.scorecard(KYC_CASES, tenant),
                           pending=sum(1 for c in sample if not c.get("qc_result")), day=dates.now_str()[:10])


@app.route("/kyc/<case_id>/qc_review", methods=["POST"])
@role_required("QC")
def kyc_qc_review(case_id):
    case = _kyc(case_id)
    ok, _ = _run_action(workflow.qc_review, case, _actor(), request.form.get("result", ""),
                        request.form.get("reason", ""), request.form.get("comment", ""))
    if ok:
        _save("kyc", case)
        flash(f"QC review recorded: {case['qc_result']} — {case['client']['legal_name']}.", "success")
    return redirect(url_for("qc_sampling"))


# ─── Operations hub: routing console (SOP 5.2.1) ───────────────────────────────
@app.route("/routing")
@role_required("L2", "QC")
def routing_console():
    return render_template("routing.html", vm=RoutingConsole.compute(KYC_CASES, ANALYSTS, _tenant()),
                           can_edit="QC" in _levels())


@app.route("/routing/run", methods=["POST"])
@role_required("L2", "QC")
def routing_run():
    ids = request.form.getlist("case_id")
    wanted = len(ids) if ids else len(routing.unassigned(_tenant(), KYC_CASES))
    results = routing.route_backlog(_tenant(), only_ids=ids if ids else None)
    for c, _a, _r in results:                    # only the cases the run touched
        _save("kyc", c)
    routed = [(c, a) for c, a, _ in results if a]
    waiting = wanted - len(routed)
    if not wanted:
        flash("Nothing to route.", "error")
    else:
        detail = ", ".join(f"{c['client']['legal_name']} → {display_name(a['email'])}" for c, a in routed[:6])
        flash(f"Routed {len(routed)} case(s){' — ' + detail if detail else ''}."
              + (f" {waiting} still waiting for capacity." if waiting else ""), "success" if routed else "error")
    return redirect(url_for("routing_console"))


# ─── AI Screening (the preset automation and its switches) ──────────────────
@app.route("/ai-screening")
def ai_screening_page():
    vm = AiScreeningConsole.compute(KYC_CASES, ALERT_CASES, WC_CASES, _tenant(), AI_SETTINGS,
                                    agent=dict(AI_AGENT, workers=AI_AGENT_WORKERS))
    return render_template("ai_screening.html", vm=vm, can_edit="QC" in _levels(), agent_ready=ai_review.configured(),
                           agent_labels=AGENT_LABELS)


@app.route("/ai-screening/rules", methods=["POST"])
@role_required("QC")
def ai_screening_rules():
    key = request.form.get("key", "")
    ok, res = _run_action(ai_screening.set_rule, AI_SETTINGS, _actor(), key, request.form.get("enabled", ""))
    if ok:
        _save("settings", AI_SETTINGS)
        rule = (ai_screening.RULE_BY_KEY.get(key) or ai_screening.GUARD_BY_KEY.get(key)
                or ai_screening.SWITCH_BY_KEY.get(key) or {})
        when = "Applies at once." if key in ai_screening.SWITCH_BY_KEY else "Applies to the next screening run."
        flash(f"{rule.get('label', key)}: {'on' if res[0] else 'off'} → {'on' if res[1] else 'off'}. {when}", "success")
    return redirect(url_for("ai_screening_page"))


# ─── AI Agent (engines/ai_agent): Claude reads the documents, checks decide ─
# It starts by itself on the first request the serving process handles and
# reads every document of an individual's case in Data Confirming: one the
# AI screening rules cleared (auto-verified) verifies itself when every check
# passes; one with an alert left that no person holds is confirmed and goes
# to Data Screening with the agent's watchlist finding, for an analyst to
# clear the alert; anything that fails goes to an analyst with the reasons.
# A team lead can pause it on the AI
# Screening page. Workers read outside the lock (the slow model call) and
# decide under it, re-checking the case is still theirs to take. A read is
# stored on the case, so a restart does not read a case twice.
AI_AGENT_WORKERS = 2          # the gateway refuses more concurrent connections
AI_AGENT_LOCK = threading.Lock()
AI_AGENT = {"running": False, "busy": set(), "tried": set(), "threads": [], "done": 0, "errors": 0, "verdicts": 0, "rechecked": 0, "started_by": "",
            "started_at": "", "stopped": "", "autostarted": False}
AGENT_LABELS = {"verified": "Handled by AI", "screening": "Confirmed by AI · alert left for an analyst",
                "human": "Sent to a person", "resubmit": "Asked the customer · new proof of address",
                "rfi": "Asked the customer · information"}


def _agent_view(case, actor, levels):
    """What the case page shows: the agent's read and checks, once it has
    worked the case, and Claude's own verdict beside them."""
    return {"show": bool(case.get("ai_agent")), "labels": AGENT_LABELS, "verdict_labels": ai_agent.VERDICT_LABELS,
            "watch_labels": ai_agent.WATCH_LABELS, "verdict_watch": ai_agent.VERDICT_WATCHLIST}


def _agent_finish(case, perceived):
    """Under the lock: decide on a read and save what changed."""
    reviewer = case.get("current_reviewer", "")
    alert, wc = _linked(case)
    record = ai_agent.decide(case, alert, wc, perceived)
    if reviewer and case["status"] == "Verified":
        # an analyst had it reserved; their turn ends with the agent's close
        workflow.worklog.close_entry(case, "Verified by the AI Agent", email=reviewer)
    AI_AGENT["done"] += 1
    AI_AGENT["errors"] += 1 if record.get("error") else 0
    _save("kyc", case)
    if alert:
        _save("alert", alert)
        _save("wc", wc)
    return record


def _agent_recheck(case):
    """Under the lock: the agent's current checks on a stored read (no model
    call); save what changed."""
    alert, wc = _linked(case)
    ai_agent.recheck(case, alert, wc)
    AI_AGENT["rechecked"] += 1
    _save("kyc", case)
    if alert:
        _save("alert", alert)
        _save("wc", wc)


def _agent_address(case):
    """Under the lock: a confirmed case's record address corrected to its
    proof of address's (no model call); save it."""
    ai_agent.update_address(case)
    AI_AGENT["addresses"] = AI_AGENT.get("addresses", 0) + 1
    _save("kyc", case)


def _agent_verdict(case):
    """A case read before verdicts existed: Claude's verdict from the stored
    read (one text call, no images), stored beside the decision."""
    verdict = ai_agent.opinion(case, case["ai_agent"]["read"], wc=_linked(case)[1])
    with AI_AGENT_LOCK:
        if ai_agent.needs_verdict(case):
            ai_agent.attach_verdict(case["ai_agent"], verdict)
            AI_AGENT["verdicts"] = AI_AGENT.get("verdicts", 0) + 1
            _save("kyc", case)


def _agent_worker():
    while AI_AGENT["running"]:
        backfill = False
        with AI_AGENT_LOCK:
            case = next((c for c in KYC_CASES if ai_agent.eligible(c) and not c.get("ai_agent")
                         and c["id"] not in AI_AGENT["busy"]), None)
            if case is None:            # new cases first; then cases changed checks may now pass (no model call)
                case = next((c for c in KYC_CASES if ai_agent.needs_recheck(c) and c["id"] not in AI_AGENT["busy"]), None)
                if case is not None:
                    _agent_recheck(case)
                    continue
                case = next((c for c in KYC_CASES if ai_agent.needs_address_update(c) and c["id"] not in AI_AGENT["busy"]), None)
                if case is not None:        # then addresses confirmed before the agent wrote them back
                    _agent_address(case)
                    continue
            if case is None:            # then verdicts for earlier reads, one try per case per run
                case = next((c for c in KYC_CASES if ai_agent.needs_verdict(c) and c["id"] not in AI_AGENT["busy"]
                             and c["id"] not in AI_AGENT["tried"]), None)
                backfill = case is not None
                if backfill:
                    AI_AGENT["tried"].add(case["id"])
            if case is None:
                break
            AI_AGENT["busy"].add(case["id"])
        if backfill:
            try:
                _agent_verdict(case)
            except Exception:        # one bad case must not stop the queue
                app.logger.exception("AI Agent verdict failed on %s", case["id"])
                case["ai_agent"]["verdict"] = {"error": "internal error", "at": dates.now_str(), "model": ai_agent.MODEL}
            finally:
                AI_AGENT["busy"].discard(case["id"])
            continue
        try:
            perceived = ai_agent.perceive(case, _render_document, wc=_linked(case)[1])
            with AI_AGENT_LOCK:
                if ai_agent.eligible(case) and not case.get("ai_agent"):
                    _agent_finish(case, perceived)
        except Exception:            # one bad case must not stop the queue
            app.logger.exception("AI Agent failed on %s", case["id"])
            case["ai_agent"] = {"decision": "human", "error": "internal error", "reasons": ["AI Agent failed on this case"],
                                "checks": [], "read": {}, "at": dates.now_str(), "model": ai_agent.MODEL, "seconds": 0}
        finally:
            AI_AGENT["busy"].discard(case["id"])
    with AI_AGENT_LOCK:
        AI_AGENT["threads"] = [t for t in AI_AGENT["threads"] if t is not threading.current_thread() and t.is_alive()]
        if not AI_AGENT["threads"] and AI_AGENT["running"]:
            AI_AGENT["running"] = False
            AI_AGENT["stopped"] = "Queue finished"
            AI_AGENT["tried"] = set()       # the next run tries a failed verdict once more
            _bump_state()


def _agent_start(by):
    with AI_AGENT_LOCK:
        if AI_AGENT["running"]:
            return
        AI_AGENT.update(running=True, started_by=by, started_at=dates.now_str(), stopped="", tried=set())
        AI_AGENT["threads"] = [threading.Thread(target=_agent_worker, name=f"ai-agent-{i}", daemon=True)
                               for i in range(AI_AGENT_WORKERS)]
        for t in AI_AGENT["threads"]:
            t.start()


@app.before_request
def _agent_autostart():
    """The agent starts by itself once per serving process, when the model
    is configured (never under the test client)."""
    if AI_AGENT["autostarted"] or app.testing:
        return
    AI_AGENT["autostarted"] = True
    if ai_review.configured():
        _agent_start("System")


@app.route("/ai-agent/start", methods=["POST"])
@role_required("QC")
def ai_agent_start():
    if not ai_review.configured():
        flash("The AI Agent needs LLM_FOUNDRY_KEY (see .env.example).", "error")
        return redirect(url_for("ai_screening_page"))
    _agent_start(_actor())
    flash(f"AI Agent running: {AI_AGENT_WORKERS} cases at a time.", "success")
    return redirect(url_for("ai_screening_page"))


@app.route("/ai-agent/pause", methods=["POST"])
@role_required("QC")
def ai_agent_pause():
    AI_AGENT.update(running=False, stopped=f"Paused by {display_name(_actor())}")
    flash("AI Agent pausing: the cases it is reading now finish, then it stops.", "success")
    return redirect(url_for("ai_screening_page"))


# ─── Upload cases: new onboarding rows in the reconciliation log's layout ────
@app.route("/kyc/upload", methods=["GET", "POST"])
@role_required("L1", "L2", "EDD", "QC")
def kyc_upload():
    if CASE_BOOK != "file":
        flash("Uploading cases works on the reconciliation-log book (CONSOLE_CASES=file).", "error")
        return redirect(url_for("kyc_list"))
    if request.method == "GET":
        batches = sorted(StateStore.records("upload"), key=lambda b: b.get("at", ""), reverse=True)
        return render_template("kyc_upload.html", columns=recon_book.UPLOAD_COLUMNS, optional=recon_book.HISTORY_COLUMNS,
                               max_rows=recon_book.UPLOAD_MAX_ROWS, batches=batches, agent_ready=ai_review.configured())
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a CSV file to upload.", "error")
        return redirect(url_for("kyc_upload"))
    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("The file is not UTF-8 text; save it as CSV UTF-8 and try again.", "error")
        return redirect(url_for("kyc_upload"))
    ok, rows = _run_action(recon_book.read_upload, text)
    if not ok:
        return redirect(url_for("kyc_upload"))
    seen, fresh, skipped = set(KYC_BY_ID), [], []
    for i, r in enumerate(rows, start=2):
        cid = r["kyc_case_id"]
        if not cid:
            skipped.append(f"line {i}: no case id")
        elif cid in seen:
            skipped.append(f"line {i}: {cid} already exists")
        else:
            seen.add(cid)
            fresh.append(r)
    cases, failed = _add_uploaded(fresh) if fresh else ([], [])
    bad = {line for line, _ in failed}
    skipped += [f"line {line}: {why}" for line, why in failed]
    kept = [r for i, r in enumerate(fresh, start=2) if i not in bad] if failed else fresh
    if cases:
        now = dates.now_str()
        _save("upload", {"id": f"upload-{now}-{len(StateStore.records('upload')) + 1}", "file": f.filename, "by": _actor(),
                         "at": now, "rows": kept, "cases": [c["id"] for c in cases]})
        auto = sum(1 for c in cases if c["screening"].get("workflow") == "auto-verified")
        if auto and ai_review.configured():
            _agent_start("upload")
        flash(f"{len(cases)} case(s) added from {f.filename}: {auto} auto-verified"
              + (" — the AI Agent is reading them now" if auto and ai_review.configured() else "")
              + f", {len(cases) - auto} for analysts." + (f" Skipped {len(skipped)}: {'; '.join(skipped[:5])}" if skipped else ""),
              "success")
        return redirect(url_for("kyc_list", tags="Uploaded"))
    flash("No case was added. " + "; ".join(skipped[:8]), "error")
    return redirect(url_for("kyc_upload"))


@app.route("/kyc/upload/<batch_id>/remove", methods=["POST"])
@role_required("L1", "L2", "EDD", "QC")
def kyc_upload_remove(batch_id):
    """Remove one upload: its cases go, with everything done on them; the
    batch stays on the Upload page marked removed, by whom and when."""
    batch = next((b for b in StateStore.records("upload") if b.get("id") == batch_id and not b.get("removed_at")), None)
    if batch is None:
        flash("That upload is not there any more; it may have been removed already.", "error")
        return redirect(url_for("kyc_upload"))
    with AI_AGENT_LOCK:
        reading = next((cid for cid in batch["cases"] if cid in AI_AGENT["busy"]), None)
        if reading:
            flash(f"The AI Agent is reading {reading} right now. Remove the upload again in a minute.", "error")
            return redirect(url_for("kyc_upload"))
        gone = _remove_uploaded(batch["cases"])
    batch.update(removed_at=dates.now_str(), removed_by=_actor())
    _save("upload", batch)
    app.logger.info("Upload %s (%s) removed by %s: %d case(s).", batch_id, batch.get("file"), _actor(), gone)
    flash(f"Removed the upload {batch.get('file', '')}: {gone} case(s) and their screening records.", "success")
    return redirect(url_for("kyc_upload"))


@app.route("/kyc/upload/sample.csv")
def kyc_upload_sample():
    nums = [int(m.group(1)) for c in KYC_CASES for m in [re.search(r"(\d+)$", c["id"])] if m]
    start = max(nums, default=4010) + 1
    rows = recon_book.sample_rows(start, taken=set(KYC_BY_ID))
    columns = list(recon_book.UPLOAD_COLUMNS + recon_book.HISTORY_COLUMNS)
    return _csv_response("new_cases_sample.csv", columns, [[r[c] for c in columns] for r in rows])


@app.route("/routing/capacity", methods=["POST"])
@role_required("QC")
def routing_capacity():
    a = ANALYST_BY_KEY.get(request.form.get("key", ""))
    if not a:
        flash("Unknown analyst.", "error")
        return redirect(url_for("routing_console"))
    ok, res = _run_action(routing.set_capacity, a, request.form.get("capacity", ""))
    if ok:
        _save("analyst", a)
        flash(f"{a['name']}: capacity {res[0]} → {res[1]}.", "success")
    return redirect(url_for("routing_console"))


# ─── Analytics ─────────────────────────────────────────────────────────────────
@app.route("/analytics")
def analytics():
    vm = AnalyticsConsole.compute(KYC_CASES, ALERT_CASES, WC_CASES, _tenant(), analysts=ANALYSTS)
    return render_template("analytics.html", vm=vm)


# ─── Demo affordances ──────────────────────────────────────────────────────────
@app.route("/reset", methods=["POST"])
def reset_demo():
    """Back to the seeded case book: drop every stored action and reload."""
    if not DEMO_MODE:
        abort(404)
    cleared = StateStore.clear()
    _load_seed()
    _bump_state()
    flash("Demo reset — the seeded case book is back." if cleared else "Could not clear the stored state.",
          "success" if cleared else "error")
    return redirect(_safe_next(request.form.get("next", "")) if request.form.get("next") else url_for("kyc_list"))


# ─── Main ──────────────────────────────────────────────────────────────────────
_load_seed()

if __name__ == "__main__":
    print(f" * FinCrime Operations Hub on http://{HOST}:{PORT}  (demo password: {DEMO_PASSWORD})")
    print(f" * Console case book: {CASE_BOOK_LABEL[CASE_BOOK]}  (CONSOLE_CASES=file|demo)")
    app.run(debug=DEBUG, host=HOST, port=PORT)
