# app.py - Streamlit UI for Layers Scanner, mobile-first responsive
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape

import pandas as pd
import streamlit as st

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
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    "<h1 class='title'>WALLET <span class='accent'>GRAPH</span> SCANNER</h1>"
    "<div class='subcaption'>Multi-layer sender trace · MistTrack AML + TRC-20</div>",
    unsafe_allow_html=True,
)

# One-time init
if "started" not in st.session_state:
    sc._db_start_writer()
    sc._load_from_db_into_memory()
    st.session_state.started = True
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
    log_placeholder.code("\n".join(logs[-400:]), language="text")


# ---------- scan ----------
if submit:
    sc.MAX_LAYERS = depth
    sc.ACTIVITY_WINDOW_HOURS = int(window_h)
    sc.MIN_USDT_AMOUNT = float(min_usdt)

    if wallet.strip():
        seeds = {wallet.strip(): "seed"}
    else:
        log("Loading seed wallets from Google Sheet...")
        db = sc.load_google_sheet()
        seeds = {}
        for _, row in db.iterrows():
            w = str(row.get("WALLET", "")).strip()
            n = str(row.get("NAME", "")).strip()
            if w:
                seeds[w] = n
        log(f"{len(seeds)} seed wallets loaded.")

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
            prog.progress(min(1.0, idx / max(1, total)))

        with ThreadPoolExecutor(max_workers=sc.MISTRACK_WORKERS) as pool:
            futs = [pool.submit(_one, a, i) for i, a in enumerate(addresses, 1)]
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    log(f"  MistTrack worker error: {e}")
        return results

    with sc._cache_lock:
        sc._activity_cache.clear()
        sc._cex_cache.clear()
        sc._account_tag_cache.clear()
        sc._mistrack_cache.clear()
    sc._load_from_db_into_memory()

    wallet_meta, edges, skipped = sc.build_wallet_graph(
        seeds, log_fn=log, scan_layer_fn=scan_layer_fn,
    )
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
def _platforms(r):
    use_p = r.get("use_platform", {}) or {}
    parts = []
    for k, v in use_p.items():
        if isinstance(v, dict) and v.get("count", 0) > 0:
            names = v.get("list") or []
            tag = f"{k}:{','.join(names[:2])}" if names else f"{k}({v['count']})"
            parts.append(tag)
    return " | ".join(parts) if parts else "—"


def _events(r):
    mal = r.get("malicious_event", {}) or {}
    parts = []
    for k in ("phishing", "ransom", "stealing", "laundering"):
        v = mal.get(k, {}) or {}
        c = v.get("count", 0)
        if c > 0:
            parts.append(f"{k}({c})")
    return " | ".join(parts) if parts else "—"


def _aml(r):
    d = r.get("detail_list") or []
    if isinstance(d, list):
        return ", ".join(map(str, d)) if d else "—"
    return str(d) if d else "—"


def _build_df(rows):
    return pd.DataFrame([
        {
            "LYR": r.get("layer"),
            "NAME": r.get("name", ""),
            "WALLET": r.get("address", ""),
            "RISK": str(r.get("risk_level", r.get("error", ""))).title(),
            "SCORE": r.get("score", ""),
            "AML": _aml(r),
            "PLATFORMS": _platforms(r),
            "EVENTS": _events(r),
            "SENDS TO": r.get("sent_to", ""),
            "LAST_TX": r.get("last_tx", ""),
            "AMOUNT_USDT": r.get("amount_usdt"),
        }
        for r in rows
    ])


# Layer tabs + filters
layers_present = sorted({r.get("layer", 0) for r in results})
layer_tabs = ["All"] + [f"L{l}" for l in layers_present]
tab_objs = st.tabs(layer_tabs)


def _render_panel(rows, key_prefix):
    # Stack search + risk filter vertically on mobile via Streamlit columns
    # plus our CSS rule above that forces 100% width on small screens.
    col_s, col_f = st.columns([2, 1])
    with col_s:
        q = st.text_input("Search", key=f"{key_prefix}-search",
                          placeholder="wallet, name, platform, event")
    with col_f:
        rf = st.selectbox(
            "Risk",
            ["All", "Low", "Medium", "High", "Unknown", "Error"],
            key=f"{key_prefix}-risk",
        )

    filtered = rows
    if q.strip():
        ql = q.strip().lower()
        def _match(r):
            blob = " ".join([
                str(r.get("address", "")), str(r.get("name", "")),
                _platforms(r), _events(r), _aml(r),
            ]).lower()
            return ql in blob
        filtered = [r for r in filtered if _match(r)]
    if rf != "All":
        rfl = rf.lower()
        filtered = [
            r for r in filtered
            if str(r.get("risk_level", r.get("error", ""))).lower() == rfl
        ]

    st.caption(f"{len(filtered)} row(s)")
    df = _build_df(filtered)
    st.dataframe(df, use_container_width=True, hide_index=True, height=420)


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

st.markdown(
    f"<div class='lastupd'>Last updated: {escape(str(st.session_state.get('last_run', '—')))}</div>",
    unsafe_allow_html=True,
)
