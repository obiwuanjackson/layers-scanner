# app.py - Streamlit UI for Layers Scanner, mobile-first responsive
import hmac
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape

import pandas as pd
import streamlit as st

# --- Resource-conscious limits for Streamlit Community Cloud free tier ---
# 1 GB RAM, 1 CPU, ephemeral disk. Tuned for ~5 scans/day x ~500 wallets.
HARD_WALLET_CAP = 800          # absolute ceiling per scan (overrides scanner_core if higher)
DAILY_SCAN_CAP  = 8            # soft cap; UI warns past this
LAST_SCAN_FILE  = "last_scan.json"
DAILY_FILE      = "daily_counter.json"
LOG_MAX_LINES   = 300          # keep memory bounded


# --- Persistence: last scan ----------------------------------------------
def _trim_for_persist(r):
    """Keep only fields the UI actually renders. Drops raw API payloads to save disk + RAM."""
    return {
        "address": r.get("address", ""),
        "name": r.get("name", ""),
        "layer": r.get("layer"),
        "risk_level": r.get("risk_level"),
        "score": r.get("score"),
        "error": r.get("error"),
        "detail_list": r.get("detail_list") or [],
        "use_platform": r.get("use_platform") or {},
        "malicious_event": r.get("malicious_event") or {},
        "sent_to": r.get("sent_to", ""),
        "last_tx": r.get("last_tx", "-"),
        "amount_usdt": r.get("amount_usdt"),
    }


def _save_last_scan(results, edges, skipped, last_run):
    try:
        slim_results = [_trim_for_persist(r) for r in results]
        # Trim edges/skipped to what we actually display
        slim_edges = edges[:5000]
        slim_skipped = skipped[:2000]
        with open(LAST_SCAN_FILE, "w") as f:
            json.dump(
                {
                    "results": slim_results,
                    "edges": slim_edges,
                    "skipped": slim_skipped,
                    "last_run": last_run,
                },
                f,
                default=str,
            )
    except Exception:
        pass


def _load_last_scan():
    try:
        with open(LAST_SCAN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# --- Daily scan counter ---------------------------------------------------
def _today_str():
    return pd.Timestamp.utcnow().strftime("%Y-%m-%d")


def _load_daily():
    try:
        with open(DAILY_FILE) as f:
            d = json.load(f)
        if d.get("date") != _today_str():
            return {"date": _today_str(), "scans": 0, "wallets": 0}
        return d
    except (FileNotFoundError, json.JSONDecodeError):
        return {"date": _today_str(), "scans": 0, "wallets": 0}


def _bump_daily(wallets_scanned):
    d = _load_daily()
    d["scans"] = d.get("scans", 0) + 1
    d["wallets"] = d.get("wallets", 0) + int(wallets_scanned or 0)
    try:
        with open(DAILY_FILE, "w") as f:
            json.dump(d, f)
    except Exception:
        pass
    return d

# Push secrets into env BEFORE importing scanner_core (it reads env at import time)
for k in ("TRONGRID_API_KEY", "MISTTRACK_API_KEY"):
    if k not in os.environ and k in st.secrets:
        os.environ[k] = st.secrets[k]

import scanner_core as sc

st.set_page_config(
    page_title="Wallet Graph Scanner",
    page_icon="📡",
    layout="centered",  # centered behaves better on phones
    initial_sidebar_state="collapsed",
)

# ---------- mobile-first styling ----------
st.markdown(
    """
    <style>
      /* Tight top padding so the header sits high on phones */
      .block-container { padding-top: .8rem; padding-left: .8rem; padding-right: .8rem; max-width: 980px; }

      /* Heading */
      h1.title { font-size: 1.35rem; letter-spacing: .06em; margin: 0 0 .1rem 0; }
      h1.title .accent { color: #00ffe0; }
      .subcaption { color: #6b7886; font-size: .72rem; margin-bottom: .8rem; }

      /* Stat grid: auto-fits 2 cols on phone, 4 on tablet, 7 on desktop */
      .stat-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
        gap: .5rem;
        margin: .4rem 0 1rem 0;
      }
      .stat-card {
        background: #0d1117; border: 1px solid #1f2730; border-radius: 10px;
        padding: .55rem .6rem; text-align: center;
        min-height: 64px;
        display: flex; flex-direction: column; justify-content: center;
      }
      .stat-label {
        color: #6b7886; font-size: .60rem; letter-spacing: .12em;
        text-transform: uppercase; line-height: 1.2;
      }
      .stat-value {
        color: #c9d8e3; font-size: 1.25rem; font-weight: 600;
        font-family: monospace; margin-top: .15rem;
      }
      .stat-value.danger { color: #ff5d6c; }
      .stat-value.warn   { color: #ffb454; }
      .stat-value.purple { color: #d985ff; }
      .stat-value.blue   { color: #60a5fa; }

      /* Buttons: large tap targets */
      .stButton button, .stDownloadButton button, .stFormSubmitButton button {
        min-height: 46px;
        font-size: .92rem;
        border-radius: 10px;
      }

      /* Form inputs: bigger on touch */
      .stTextInput input, .stNumberInput input, .stSelectbox div[data-baseweb="select"] > div {
        min-height: 42px;
        font-size: .95rem;
      }

      /* Tabs scroll horizontally on phones instead of cramming */
      .stTabs [data-baseweb="tab-list"] {
        overflow-x: auto;
        flex-wrap: nowrap;
        -webkit-overflow-scrolling: touch;
      }
      .stTabs [data-baseweb="tab"] { min-width: max-content; }

      /* Table: ensure horizontal scroll works on narrow screens */
      div[data-testid="stDataFrame"] {
        font-family: monospace;
        font-size: .78rem;
      }

      /* Log panel readable on phone */
      pre, code { font-size: .72rem !important; }

      /* Footer link styling */
      .lastupd { color: #6b7886; font-size: .72rem; text-align: right; padding-top: .4rem; }

      /* On wide screens, let the layout breathe */
      @media (min-width: 900px) {
        .block-container { max-width: 1100px; padding-left: 1.2rem; padding-right: 1.2rem; }
        h1.title { font-size: 1.6rem; }
        .stat-value { font-size: 1.4rem; }
      }

      /* On phones, force compact form: stack the columns */
      @media (max-width: 640px) {
        div[data-testid="column"] { width: 100% !important; flex: 1 1 100% !important; }
      }

      /* ---------- Result cards (mimicking scanner.html) ---------- */
      .wlist { display: flex; flex-direction: column; gap: .55rem; margin-top: .4rem; }
      .wcard {
        background: #0d1117; border: 1px solid #1f2730; border-left: 3px solid #1f2730;
        border-radius: 10px; padding: .65rem .75rem;
      }
      .wcard.has-mixer { border-left-color: #d985ff; background: linear-gradient(90deg,#1a0f23 0%,#0d1117 30%); }
      .wcard.has-event { border-left-color: #ff5d6c; background: linear-gradient(90deg,#23101a 0%,#0d1117 30%); }

      .wrow1 { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; }
      .layer-badge {
        font-family: monospace; font-size: .68rem; letter-spacing: .08em;
        padding: .12rem .45rem; border-radius: 999px;
        background: #1f2730; color: #c9d8e3; border: 1px solid #2a323b;
      }
      .layer-badge.l0 { background: #003a40; color: #00ffe0; border-color: #006a72; }
      .layer-badge.l1 { background: #1d2e44; color: #60a5fa; border-color: #2c4360; }
      .layer-badge.l2 { background: #1d3a2e; color: #4ade80; border-color: #2c5742; }
      .layer-badge.l3 { background: #3a311d; color: #ffb454; border-color: #5a4a2a; }
      .layer-badge.l4 { background: #3a1d2a; color: #ff8aa5; border-color: #5a2a3e; }
      .layer-badge.l5 { background: #3a1d3a; color: #d985ff; border-color: #5a2a5a; }

      .wname { color: #c9d8e3; font-weight: 600; font-size: .85rem; }
      .waddr {
        font-family: monospace; font-size: .76rem; color: #8aa0b3;
        background: #060a0f; padding: .12rem .45rem; border-radius: 6px;
        border: 1px solid #1f2730; word-break: break-all;
      }

      .risk-badge {
        font-family: monospace; font-size: .68rem; font-weight: 700; letter-spacing: .12em;
        padding: .14rem .55rem; border-radius: 999px; text-transform: uppercase;
      }
      .risk-low      { background: #0e2d1f; color: #4ade80; border: 1px solid #1f5a3c; }
      .risk-medium   { background: #3a311d; color: #ffb454; border: 1px solid #5a4a2a; }
      .risk-high     { background: #3a151c; color: #ff5d6c; border: 1px solid #6a232e; }
      .risk-unknown  { background: #1f2730; color: #8aa0b3; border: 1px solid #2a323b; }
      .risk-error    { background: #3a151c; color: #ff5d6c; border: 1px solid #6a232e; font-style: italic; }

      .score-wrap { display: flex; align-items: center; gap: .4rem; min-width: 90px; }
      .score-bar-bg { flex: 1; height: 6px; background: #1f2730; border-radius: 3px; overflow: hidden; min-width: 50px; }
      .score-bar-fill { height: 100%; border-radius: 3px; transition: width .2s; }
      .score-num { font-family: monospace; font-size: .78rem; color: #c9d8e3; min-width: 28px; text-align: right; }

      .wrow2 { display: flex; flex-wrap: wrap; gap: .3rem; margin-top: .5rem; }
      .chip {
        font-size: .65rem; padding: .15rem .45rem; border-radius: 6px;
        background: #1f2730; color: #c9d8e3; border: 1px solid #2a323b;
        line-height: 1.4;
      }
      .chip.aml      { background: #2a1a3a; color: #d985ff; border-color: #432a5a; }
      .chip.exchange { background: #16223a; color: #60a5fa; border-color: #2c4360; }
      .chip.dex      { background: #16322a; color: #4ade80; border-color: #1f5a3c; }
      .chip.mixer    { background: #2a1037; color: #d985ff; border-color: #5a1f7a; font-weight: 700; }
      .chip.nft      { background: #322a16; color: #ffb454; border-color: #5a4a2a; }
      .chip.phishing  { background: #3a1a2a; color: #ff8aa5; border-color: #5a2a3e; }
      .chip.ransom    { background: #3a151c; color: #ff5d6c; border-color: #6a232e; font-weight: 700; }
      .chip.stealing  { background: #3a221c; color: #ff8b4a; border-color: #6a3e23; }
      .chip.laundering{ background: #322a16; color: #ffb454; border-color: #5a4a2a; }
      .chip-muted { color: #6b7886; font-style: italic; }
      .chip-tiny  { font-size: .58rem; opacity: .8; margin-left: .25rem; }

      .wmeta { color: #6b7886; font-size: .68rem; margin-top: .35rem; }
      .wmeta b { color: #8aa0b3; font-weight: 600; }

      .sent-chip {
        font-family: monospace; font-size: .62rem; padding: .08rem .3rem;
        background: #060a0f; border: 1px solid #1f2730; border-radius: 4px;
        color: #8aa0b3; margin-right: .2rem;
      }

      .alert-pills {
        display: flex; flex-wrap: wrap; gap: .35rem;
        margin: .4rem 0 .2rem 0;
      }
      .alert-pill-label {
        color: #6b7886; font-size: .62rem; letter-spacing: .12em;
        text-transform: uppercase; align-self: center;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------- Auth gate ----------
def _check_login(user: str, pw: str) -> bool:
    want_user = st.secrets.get("APP_USER", "chimbi") if hasattr(st, "secrets") else "chimbi"
    want_pw   = st.secrets.get("APP_PASS", "113511") if hasattr(st, "secrets") else "113511"
    try:
        return (
            hmac.compare_digest(user.strip(), str(want_user))
            and hmac.compare_digest(pw, str(want_pw))
        )
    except Exception:
        return False

if not st.session_state.get("auth_ok"):
    st.markdown(
        "<h1 class='title'>WALLET <span class='accent'>GRAPH</span> SCANNER</h1>"
        "<div class='subcaption'>Sign in to continue</div>",
        unsafe_allow_html=True,
    )
    with st.form("login", clear_on_submit=False):
        u = st.text_input("User", autocomplete="username")
        p = st.text_input("Password", type="password", autocomplete="current-password")
        ok = st.form_submit_button("Sign in", use_container_width=True, type="primary")
    if ok:
        if _check_login(u, p):
            st.session_state.auth_ok = True
            st.rerun()
        else:
            st.error("Invalid credentials")
    st.stop()

with st.sidebar:
    if st.button("Sign out", use_container_width=True):
        st.session_state.clear()
        st.rerun()
# -------------------------------

st.markdown(
    "<h1 class='title'>WALLET <span class='accent'>GRAPH</span> SCANNER</h1>"
    "<div class='subcaption'>Multi-layer sender trace · MistTrack AML + TRC-20</div>",
    unsafe_allow_html=True,
)

# One-time init: load persisted last scan so reconnects show prior results
if "started" not in st.session_state:
    sc._db_start_writer()
    sc._load_from_db_into_memory()
    st.session_state.started = True
    prev = _load_last_scan()
    if prev:
        st.session_state.results = prev.get("results", [])
        st.session_state.skipped = prev.get("skipped", [])
        st.session_state.edges = prev.get("edges", [])
        st.session_state.last_run = prev.get("last_run")
    else:
        st.session_state.results = []
        st.session_state.skipped = []
        st.session_state.edges = []
        st.session_state.last_run = None

# ---------- controls ----------
with st.form("scan"):
    wallet = st.text_input(
        "Seed wallet",
        placeholder="TRON address (leave empty for Google Sheet seeds)",
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        depth = st.slider("Depth", 1, 6, sc.MAX_LAYERS)
    with c2:
        window_h = st.number_input("Window (h)", 1, 720, sc.ACTIVITY_WINDOW_HOURS)
    with c3:
        min_usdt = st.number_input("Min USDT", 0.0, 1000.0, float(sc.MIN_USDT_AMOUNT))
    submit = st.form_submit_button("▶  Start scan", use_container_width=True)

log_placeholder = st.empty()
prog = st.progress(0.0)
logs: list[str] = []


def log(msg: str):
    logs.append(str(msg))
    # Keep buffer bounded to limit memory
    if len(logs) > LOG_MAX_LINES * 2:
        del logs[: len(logs) - LOG_MAX_LINES]
    log_placeholder.code("\n".join(logs[-LOG_MAX_LINES:]), language="text")


# ---------- scan ----------
if submit:
    # Immediate feedback so the user sees the click landed
    st.toast("Scan started", icon="🚀")
    # Drop previous results from the UI so it does not look frozen
    st.session_state.results = []
    st.session_state.edges = []
    st.session_state.skipped = []
    st.session_state.last_run = None

    sc.MAX_LAYERS = depth
    sc.ACTIVITY_WINDOW_HOURS = int(window_h)
    sc.MIN_USDT_AMOUNT = float(min_usdt)
    # Enforce hard cap regardless of scanner_core default
    sc.MAX_TOTAL_WALLETS = min(sc.MAX_TOTAL_WALLETS, HARD_WALLET_CAP)

    # Reset rate-limiter state so a throttled prior run does not carry over
    try:
        with sc._trongrid_lock:
            sc._trongrid_rate         = 12.0
            sc._trongrid_min_interval = 1.0 / 12.0
            sc._trongrid_429_streak   = 0
            sc._trongrid_last_call    = 0.0
        with sc._mistrack_lock:
            sc._mistrack_rate            = sc.MISTRACK_RATE_FLOOR
            sc._mistrack_min_interval    = 1.0 / sc.MISTRACK_RATE_FLOOR
            sc._mistrack_429_streak      = 0
            sc._mistrack_success_streak  = 0
            sc._mistrack_last_call       = 0.0
    except Exception:
        pass

    # Ensure DB writer thread is alive (in case it died after a prior run)
    try:
        if sc._db_writer_thread is None or not sc._db_writer_thread.is_alive():
            sc._db_start_writer()
    except Exception:
        pass

    daily = _load_daily()
    if daily.get("scans", 0) >= DAILY_SCAN_CAP:
        st.warning(
            f"You have already run {daily['scans']} scans today on this app "
            f"(soft cap is {DAILY_SCAN_CAP}). The cache will keep new scans fast, "
            f"but heavy use could hit Streamlit's free-tier resource limits."
        )

    if wallet.strip():
        seeds = {wallet.strip(): "seed"}
        log(f"Using manual seed: {wallet.strip()}")
    else:
        log("Loading FRESH seed wallets from Google Sheet (CDN cache busted)...")
        db = sc.load_google_sheet()
        seeds = {}
        for _, row in db.iterrows():
            w = str(row.get("WALLET", "")).strip()
            n = str(row.get("NAME", "")).strip()
            if w:
                seeds[w] = n
        log(f"{len(seeds)} seed wallets loaded from sheet:")
        for i, (w, n) in enumerate(seeds.items(), 1):
            log(f"  {i:>3}. {w}  ({n})")

    if not seeds:
        st.error("No seed wallets to scan.")
        st.stop()

    log(f"Starting {depth}-layer scan over {len(seeds)} seed(s)...")

    interleaved: dict = {}
    interleaved_lock = threading.Lock()

    def scan_layer_fn(addresses: list, layer: int) -> dict:
        results: dict = {}
        rlock = threading.Lock()
        total = len(addresses)

        def _one(addr, idx):
            r, cached = sc.fetch_mistrack_cached(addr, log_fn=log)
            tag = " (cached)" if cached else ""
            log(f"  L{layer} [{idx}/{total}] {addr} -> {r.get('risk_level', r.get('error', '?'))}{tag}")
            with rlock:
                results[addr] = r
            with interleaved_lock:
                interleaved[addr] = r
            # NOTE: do NOT touch Streamlit widgets from worker threads.
            # Streamlit raises MissingScriptRunContext when it happens.

        with ThreadPoolExecutor(max_workers=sc.MISTRACK_WORKERS) as pool:
            futs = [pool.submit(_one, a, i) for i, a in enumerate(addresses, 1)]
            done = 0
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    msg = repr(e) if not str(e) else str(e)
                    log(f"  MistTrack worker error: {msg}")
                done += 1
                # Safe: progress update from main thread (the as_completed loop runs here)
                try:
                    prog.progress(min(1.0, done / max(1, total)))
                except Exception:
                    pass
        return results

    with sc._cache_lock:
        sc._activity_cache.clear()
        sc._cex_cache.clear()
        sc._account_tag_cache.clear()
        sc._mistrack_cache.clear()
    sc._load_from_db_into_memory()

    try:
        wallet_meta, edges, skipped = sc.build_wallet_graph(
            seeds, log_fn=log, scan_layer_fn=scan_layer_fn,
        )
    except Exception as e:
        st.error(f"Scan failed: {e!r}")
        log(f"Scan failed: {e!r}")
        st.stop()
    log(f"Graph: {len(wallet_meta)} wallets, {len(edges)} edges, {len(skipped)} skipped.")

    all_results = []
    already = set(interleaved.keys())
    for addr, meta in wallet_meta.items():
        if addr in already:
            mt = interleaved[addr].copy()
        else:
            mt, _ = sc.fetch_mistrack_cached(addr, log_fn=log)
        mt["address"] = mt.get("address") or addr
        mt["name"] = meta.get("name", "")
        mt["layer"] = meta["layer"]
        mt["sent_to"] = meta.get("sent_to", "")
        mt["last_tx"] = meta.get("last_tx", "-")
        mt["amount_usdt"] = meta.get("amount_usdt")
        all_results.append(mt)

    sc.save_xlsx(all_results, skipped, log)
    log("Scan complete.")
    prog.progress(1.0)

    st.session_state.results = all_results
    st.session_state.skipped = skipped
    st.session_state.edges = edges
    st.session_state.last_run = pd.Timestamp.utcnow().isoformat(timespec="seconds")
    # Persist so the page survives Safari backgrounding / tab kills
    _save_last_scan(all_results, edges, skipped, st.session_state.last_run)
    # Bump daily counter
    _bump_daily(len(all_results))
    st.toast(f"Scan complete: {len(all_results)} wallets", icon="✅")
    # Force a fresh rerun so the stats grid + cards repaint with new data
    st.rerun()

# ---------- render ----------
results = st.session_state.get("results", [])
edges = st.session_state.get("edges", [])
skipped = st.session_state.get("skipped", [])


def _count_alerts(r):
    use_p = r.get("use_platform", {}) or {}
    mal = r.get("malicious_event", {}) or {}
    mixer = (use_p.get("mixer", {}) or {}).get("count", 0)
    exchange = sum(
        (use_p.get(k, {}) or {}).get("count", 0)
        for k in ("exchange", "cex", "centralized exchange")
    )
    events = sum(
        (mal.get(k, {}) or {}).get("count", 0)
        for k in ("phishing", "ransom", "stealing", "laundering")
    )
    return mixer, exchange, events


total_w = len(results)
high_risk = sum(1 for r in results if str(r.get("risk_level", "")).lower() == "high")
scores = [r.get("score") for r in results if isinstance(r.get("score"), (int, float))]
avg_score = round(sum(scores) / len(scores), 2) if scores else "—"

mixer_total = exch_total = evt_total = 0
for r in results:
    m, e, ev = _count_alerts(r)
    mixer_total += m
    exch_total += e
    evt_total += ev

# Stats: rendered as one HTML grid so it wraps responsively on phones
def _stat(label, value, cls=""):
    return (
        f"<div class='stat-card'>"
        f"<div class='stat-label'>{escape(label)}</div>"
        f"<div class='stat-value {cls}'>{escape(str(value))}</div>"
        f"</div>"
    )

stats_html = (
    "<div class='stat-grid'>"
    + _stat("Total Wallets", total_w)
    + _stat("Edges", len(edges))
    + _stat("High Risk", high_risk, "danger")
    + _stat("Avg Score", avg_score)
    + _stat("Mixer Hits", mixer_total, "purple")
    + _stat("Mal Events", evt_total, "danger")
    + _stat("Exchange", exch_total, "blue")
    + "</div>"
)
st.markdown(stats_html, unsafe_allow_html=True)

if not results:
    st.info("No results yet. Run a scan above.")
    st.stop()


# Helpers for table cell contents
PLATFORM_ICONS = {"exchange": "⇄", "dex": "◎", "mixer": "⬡", "nft": "◆"}
EVENT_ICONS    = {"phishing": "🎣", "ransom": "💀", "stealing": "⚡", "laundering": "🌀"}


def _short_addr(a: str) -> str:
    if not a or len(a) <= 16:
        return a or "—"
    return f"{a[:8]}…{a[-6:]}"


def _platforms_summary(r):
    use_p = r.get("use_platform", {}) or {}
    parts = []
    for k, v in use_p.items():
        if isinstance(v, dict) and v.get("count", 0) > 0:
            names = v.get("list") or []
            parts.append(f"{k} {','.join(names[:2])}")
    return " | ".join(parts)


def _events_summary(r):
    mal = r.get("malicious_event", {}) or {}
    parts = []
    for k in ("phishing", "ransom", "stealing", "laundering"):
        v = mal.get(k, {}) or {}
        if v.get("count", 0) > 0:
            parts.append(f"{k}({v['count']})")
    return " | ".join(parts)


def _aml_summary(r):
    d = r.get("detail_list") or []
    return ", ".join(map(str, d)) if isinstance(d, list) and d else ""


def _build_df(rows):
    return pd.DataFrame([
        {
            "LYR": r.get("layer"),
            "NAME": r.get("name", ""),
            "WALLET": r.get("address", ""),
            "RISK": str(r.get("risk_level", r.get("error", ""))).title(),
            "SCORE": r.get("score", ""),
            "AML": _aml_summary(r) or "-",
            "PLATFORMS": _platforms_summary(r) or "-",
            "EVENTS": _events_summary(r) or "-",
            "SENDS TO": r.get("sent_to", ""),
            "LAST_TX": r.get("last_tx", ""),
            "AMOUNT_USDT": r.get("amount_usdt"),
        }
        for r in rows
    ])


def _build_card(r) -> str:
    """Render one wallet result as a rich HTML card (mimics scanner.html row)."""
    layer = r.get("layer", "?")
    addr = r.get("address", "") or "—"
    name = r.get("name", "") or "—"
    risk_raw = r.get("error") and "error" or (r.get("risk_level") or "unknown")
    risk_lo = str(risk_raw).lower()
    risk_display = "ERR" if r.get("error") else str(r.get("risk_level") or "N/A").upper()

    # Score
    try:
        score = float(r.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    score_pct = min(max(score, 0), 100)
    score_color = "#ff5d6c" if score >= 70 else ("#ffb454" if score >= 40 else "#4ade80")
    score_display = f"{score:g}" if score > 0 else "—"

    # Highlight stripe
    plat = r.get("use_platform", {}) or {}
    mal = r.get("malicious_event", {}) or {}
    has_mixer = (plat.get("mixer", {}) or {}).get("count", 0) > 0
    has_event = any((mal.get(k, {}) or {}).get("count", 0) > 0
                    for k in ("phishing", "ransom", "stealing", "laundering"))
    stripe_cls = "has-event" if has_event else ("has-mixer" if has_mixer else "")

    # AML detail tags
    aml_tags = "".join(
        f"<span class='chip aml'>{escape(str(d))}</span>"
        for d in (r.get("detail_list") or [])
    )

    # Platform chips
    plat_chips = ""
    for key in ("exchange", "dex", "mixer", "nft"):
        block = plat.get(key, {}) or {}
        c = block.get("count", 0)
        if not c:
            continue
        names = block.get("list") or []
        names_short = ", ".join(escape(str(n)) for n in names[:3])
        overflow = f" +{c - 3}" if c > 3 else ""
        names_html = f"<span class='chip-tiny'>{names_short}{overflow}</span>" if names_short else ""
        plat_chips += (
            f"<span class='chip {key}'>{PLATFORM_ICONS.get(key, '')} {key} ({c}){names_html}</span>"
        )

    # Event chips
    event_chips = ""
    for key in ("phishing", "ransom", "stealing", "laundering"):
        block = mal.get(key, {}) or {}
        c = block.get("count", 0)
        if not c:
            continue
        names = block.get("list") or []
        names_short = ", ".join(escape(str(n)) for n in names[:2])
        overflow = f" +{c - 2}" if c > 2 else ""
        names_html = f"<span class='chip-tiny'>{names_short}{overflow}</span>" if names_short else ""
        event_chips += (
            f"<span class='chip {key}'>{EVENT_ICONS.get(key, '')} {key} ({c}){names_html}</span>"
        )

    # sent_to (can be list of strings or string)
    sent_to = r.get("sent_to") or []
    if isinstance(sent_to, str):
        sent_to = [s for s in sent_to.split(",") if s.strip()]
    sent_chips = "".join(
        f"<span class='sent-chip' title='{escape(s)}'>{escape(_short_addr(s))}</span>"
        for s in sent_to[:6]
    )
    if len(sent_to) > 6:
        sent_chips += f"<span class='sent-chip'>+{len(sent_to) - 6}</span>"

    # Footer meta
    last_tx = r.get("last_tx") or "—"
    amount = r.get("amount_usdt")
    amount_str = f"{amount:.2f}" if isinstance(amount, (int, float)) else "—"

    chips_block = aml_tags + plat_chips + event_chips
    if not chips_block:
        chips_block = "<span class='chip chip-muted'>no alerts</span>"

    return f"""
<div class='wcard {stripe_cls}'>
  <div class='wrow1'>
    <span class='layer-badge l{layer}'>L{layer}</span>
    <span class='wname'>{escape(name)}</span>
    <span class='risk-badge risk-{risk_lo}'>{escape(risk_display)}</span>
    <div class='score-wrap'>
      <div class='score-bar-bg'><div class='score-bar-fill' style='width:{score_pct}%;background:{score_color}'></div></div>
      <span class='score-num'>{escape(score_display)}</span>
    </div>
  </div>
  <div style='margin-top:.35rem'><span class='waddr'>{escape(addr)}</span></div>
  <div class='wrow2'>{chips_block}</div>
  <div class='wmeta'>
    <b>Last tx:</b> {escape(str(last_tx))} &nbsp;·&nbsp;
    <b>Amount:</b> {amount_str} USDT
    {f" &nbsp;·&nbsp; <b>Sends to:</b> {sent_chips}" if sent_chips else ""}
  </div>
</div>
"""


# Alert pill state lives in session_state
if "active_alert" not in st.session_state:
    st.session_state.active_alert = None


def _matches_alert(r, active):
    if not active:
        return True
    plat = r.get("use_platform", {}) or {}
    mal = r.get("malicious_event", {}) or {}
    if active == "has_any":
        return ((plat.get("mixer", {}) or {}).get("count", 0) > 0
                or any((mal.get(k, {}) or {}).get("count", 0) > 0
                       for k in ("phishing", "ransom", "stealing", "laundering")))
    if active == "mixer":
        return (plat.get("mixer", {}) or {}).get("count", 0) > 0
    return (mal.get(active, {}) or {}).get("count", 0) > 0


# Alert filter pills (one row, persistent across tabs)
st.markdown("<div class='alert-pill-label'>Profile filter</div>", unsafe_allow_html=True)
ap_cols = st.columns(7)
pill_defs = [
    (None, "All"),
    ("has_any", "⚠ Any"),
    ("mixer", "⬡ Mixer"),
    ("phishing", "🎣 Phishing"),
    ("ransom", "💀 Ransom"),
    ("stealing", "⚡ Stealing"),
    ("laundering", "🌀 Launder"),
]
for col, (val, label) in zip(ap_cols, pill_defs):
    active = st.session_state.active_alert == val
    btn_label = f"● {label}" if active else label
    if col.button(btn_label, key=f"pill-{val or 'none'}", use_container_width=True):
        st.session_state.active_alert = val
        st.rerun()


# Layer tabs
layers_present = sorted({r.get("layer", 0) for r in results})
layer_tabs = ["All"] + [f"L{l}" for l in layers_present]
tab_objs = st.tabs(layer_tabs)


def _render_panel(rows, key_prefix):
    col_s, col_f, col_sort = st.columns([2, 1, 1])
    with col_s:
        q = st.text_input("Search", key=f"{key_prefix}-search",
                          placeholder="wallet, name, platform, event")
    with col_f:
        rf = st.selectbox(
            "Risk",
            ["All", "Low", "Medium", "High", "Unknown", "Error"],
            key=f"{key_prefix}-risk",
        )
    with col_sort:
        sort = st.selectbox(
            "Sort by",
            ["Layer", "Risk", "Score (high→low)", "Name"],
            key=f"{key_prefix}-sort",
        )

    filtered = rows
    if q.strip():
        ql = q.strip().lower()
        def _match(r):
            blob = " ".join([
                str(r.get("address", "")), str(r.get("name", "")),
                _platforms_summary(r), _events_summary(r), _aml_summary(r),
            ]).lower()
            return ql in blob
        filtered = [r for r in filtered if _match(r)]
    if rf != "All":
        rfl = rf.lower()
        filtered = [
            r for r in filtered
            if str(r.get("risk_level", r.get("error", ""))).lower() == rfl
        ]
    filtered = [r for r in filtered if _matches_alert(r, st.session_state.active_alert)]

    # Sort
    risk_order = {"high": 0, "medium": 1, "low": 2, "unknown": 3, "error": 4, "": 5}
    if sort == "Layer":
        filtered.sort(key=lambda r: (r.get("layer", 99), r.get("name", "")))
    elif sort == "Risk":
        filtered.sort(key=lambda r: risk_order.get(str(r.get("risk_level", "")).lower(), 9))
    elif sort.startswith("Score"):
        filtered.sort(key=lambda r: -(float(r.get("score") or 0)))
    elif sort == "Name":
        filtered.sort(key=lambda r: (str(r.get("name", "")).lower(), r.get("layer", 99)))

    st.caption(f"{len(filtered)} wallet(s)")
    if not filtered:
        st.info("No wallets match the current filters.")
        return

    # Render up to 200 cards on screen, paginate the rest with a button
    CHUNK = 200
    visible = filtered[:CHUNK]
    cards_html = "<div class='wlist'>" + "".join(_build_card(r) for r in visible) + "</div>"
    st.markdown(cards_html, unsafe_allow_html=True)
    if len(filtered) > CHUNK:
        st.caption(f"Showing first {CHUNK} of {len(filtered)}. Refine filters for more.")


with tab_objs[0]:
    _render_panel(results, "all")
for i, lyr in enumerate(layers_present, start=1):
    with tab_objs[i]:
        _render_panel([r for r in results if r.get("layer") == lyr], f"l{lyr}")

# Skipped panel
if skipped:
    with st.expander(f"Skipped ({len(skipped)})"):
        sdf = pd.DataFrame(skipped)
        st.dataframe(sdf, use_container_width=True, hide_index=True, height=280)

# Downloads + last updated
st.divider()
d1, d2 = st.columns(2)
try:
    with open(sc.OUTPUT_XLSX, "rb") as f:
        d1.download_button(
            "⬇ XLSX", f,
            file_name=os.path.basename(sc.OUTPUT_XLSX),
            use_container_width=True,
        )
except FileNotFoundError:
    d1.button("XLSX unavailable", disabled=True, use_container_width=True)

csv_bytes = _build_df(results).to_csv(index=False).encode("utf-8")
d2.download_button(
    "⬇ CSV", csv_bytes,
    file_name="wallet_graph_results.csv",
    mime="text/csv", use_container_width=True,
)

# --- Daily usage panel ---
daily = _load_daily()
cache_size_kb = 0
try:
    cache_size_kb = os.path.getsize(sc.CACHE_DB) // 1024
except FileNotFoundError:
    pass

with st.expander("Free-tier usage / health"):
    st.markdown(
        f"- Scans today (UTC): **{daily.get('scans', 0)} / {DAILY_SCAN_CAP}** soft cap\n"
        f"- Wallets scanned today: **{daily.get('wallets', 0)}**\n"
        f"- Per-scan wallet cap: **{HARD_WALLET_CAP}**\n"
        f"- Persistent cache file: **{cache_size_kb} KB**\n"
        f"- Workers in use: graph={sc.GRAPH_WORKERS}, mistrack={sc.MISTRACK_WORKERS}\n"
        f"- Cache wallets in memory: **{len(sc._mistrack_cache) if hasattr(sc, '_mistrack_cache') else 0}**\n"
        f"\nStreamlit Cloud free tier: ~1 GB RAM, 1 CPU, ephemeral disk."
        f" The cache file may be wiped on container restart; rerunning a scan rebuilds it."
    )
    if cache_size_kb > 50_000:  # 50 MB
        st.warning("Cache file is getting large. Consider clearing it to free disk.")
    cc1, cc2 = st.columns(2)
    if cc1.button("✕ Clear last scan", use_container_width=True):
        try:
            os.remove(LAST_SCAN_FILE)
        except FileNotFoundError:
            pass
        for k in ("results", "edges", "skipped", "last_run"):
            st.session_state.pop(k, None)
        st.rerun()
    if cc2.button("✕ Clear scanner cache (DB)", use_container_width=True):
        try:
            os.remove(sc.CACHE_DB)
        except FileNotFoundError:
            pass
        st.success("Cache DB removed. It will be recreated on next scan.")

st.markdown(
    f"<div class='lastupd'>Last updated: {escape(str(st.session_state.get('last_run', '—')))}"
    f" · Tip: keep this tab in the foreground while scanning. "
    f"On iOS, switching apps for &gt;30s will pause the connection and the scan stops.</div>",
    unsafe_allow_html=True,
)
