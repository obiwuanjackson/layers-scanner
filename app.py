# app.py - Streamlit UI for Layers Scanner
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import streamlit as st

# Push secrets into env BEFORE importing scanner_core (it reads env at import time)
for k in ("TRONGRID_API_KEY", "MISTTRACK_API_KEY"):
    if k not in os.environ and k in st.secrets:
        os.environ[k] = st.secrets[k]

import scanner_core as sc

st.set_page_config(
    page_title="Layers Scanner",
    page_icon="📡",
    layout="centered",
)
st.title("Layers Scanner")
st.caption("TRON wallet graph + MistTrack risk scoring")

# One-time init of SQLite writer and in-memory cache
if "started" not in st.session_state:
    sc._db_start_writer()
    sc._load_from_db_into_memory()
    st.session_state.started = True

with st.form("scan"):
    col1, col2 = st.columns([2, 1])
    with col1:
        wallet = st.text_input(
            "Seed wallet (TRON address)",
            help="Leave empty to use the Google Sheet seed list",
        )
    with col2:
        depth = st.slider("Depth", 1, 5, sc.MAX_LAYERS)
    submit = st.form_submit_button("Run scan", use_container_width=True)

log_box = st.empty()
prog = st.progress(0.0)
logs: list[str] = []


def log(msg: str):
    logs.append(str(msg))
    log_box.code("\n".join(logs[-300:]), language="text")


if submit:
    # Apply depth override
    sc.MAX_LAYERS = depth

    # Build seed list
    if wallet.strip():
        seed_wallets = {wallet.strip(): "seed"}
    else:
        log("Loading seed wallets from Google Sheet...")
        db = sc.load_google_sheet()
        seed_wallets = {}
        for _, row in db.iterrows():
            w = str(row.get("WALLET", "")).strip()
            n = str(row.get("NAME", "")).strip()
            if w:
                seed_wallets[w] = n
        log(f"{len(seed_wallets)} seed wallets loaded.")

    if not seed_wallets:
        st.error("No seed wallets to scan.")
        st.stop()

    log(f"Starting {depth}-layer scan over {len(seed_wallets)} seeds...")

    # Interleaved MistTrack scan per layer (mirrors full_scan_worker logic)
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

    # Reset in-memory caches like full_scan_worker does
    with sc._cache_lock:
        sc._activity_cache.clear()
        sc._cex_cache.clear()
        sc._account_tag_cache.clear()
        sc._mistrack_cache.clear()
    sc._load_from_db_into_memory()

    wallet_meta, edges, skipped = sc.build_wallet_graph(
        seed_wallets,
        log_fn=log,
        scan_layer_fn=scan_layer_fn,
    )
    log(f"Graph: {len(wallet_meta)} wallets, {len(edges)} edges, {len(skipped)} skipped.")

    # Mop-up: scan any wallet not yet covered (mostly layer-0 seeds)
    all_results = []
    already = set(interleaved.keys())
    for addr, meta in wallet_meta.items():
        if addr in already:
            mt = interleaved[addr].copy()
        else:
            mt, _ = sc.fetch_mistrack_cached(addr, log_fn=log)
        mt["name"] = meta.get("name", "")
        mt["layer"] = meta["layer"]
        mt["sent_to"] = meta.get("sent_to", "")
        mt["last_tx"] = meta.get("last_tx", "-")
        mt["amount_usdt"] = meta.get("amount_usdt")
        all_results.append(mt)

    sc.save_xlsx(all_results, skipped, log)
    log("Done.")
    prog.progress(1.0)

    # Show summary table
    df = pd.DataFrame(
        [
            {
                "WALLET": r.get("address") or r.get("wallet") or "",
                "LAYER": r.get("layer"),
                "RISK": r.get("risk_level", r.get("error", "")),
                "AMOUNT_USDT": r.get("amount_usdt"),
                "LAST_TX": r.get("last_tx", ""),
                "NAME": r.get("name", ""),
            }
            for r in all_results
        ]
    )
    st.subheader(f"Results ({len(df)})")
    st.dataframe(df, use_container_width=True)

    # Download XLSX
    try:
        with open(sc.OUTPUT_XLSX, "rb") as f:
            st.download_button(
                "Download XLSX",
                f,
                file_name=os.path.basename(sc.OUTPUT_XLSX),
                use_container_width=True,
            )
    except FileNotFoundError:
        st.warning("XLSX file not produced.")
