# scanner_core.py - Flask stripped, API keys from env.
# Generated from scanner.py; preserves all helpers + build_wallet_graph.

import json
import pandas as pd
import requests, time, threading, sqlite3, queue, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import getcontext
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from tronpy import Tron
from tronpy.providers import HTTPProvider
from tronpy.contract import Contract

# ================================================================
# === CONFIGURATION ==============================================
# ================================================================

TRONPY_API_KEY        = os.environ.get("TRONGRID_API_KEY", "")
TRONGRID_API_KEY      = TRONPY_API_KEY
USDT_CONTRACT_ADDRESS = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

sheet_id = "1ROzeI5IrSnu2_p7BCDI4wiuI8lQNwlB8iyvGMNQ8OcM"
gid      = "826593804"
csv_url  = (
    f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    f"/gviz/tq?tqx=out:csv&gid={gid}"
)

MISTTRACK_API_KEY      = os.environ.get("MISTTRACK_API_KEY", "")
MISTTRACK_BASE_URL     = "https://openapi.misttrack.io/v2/risk_score"
MISTTRACK_PROFILE_URL  = "https://openapi.misttrack.io/v1/address_trace"
MISTTRACK_COIN         = "TRX"

TRONGRID_TXN_URL     = "https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
TRONGRID_ACCOUNT_URL = "https://api.trongrid.io/v1/accounts/{address}"

# ── Tunable parameters ───────────────────────────────────────────
# Tuned for Streamlit Community Cloud free tier (1 GB RAM, 1 CPU).
# Override any of these with env vars at deploy time.
MAX_LAYERS             = int(os.environ.get("MAX_LAYERS", "4"))
MAX_SENDERS_PER_WALLET = int(os.environ.get("MAX_SENDERS_PER_WALLET", "20"))
MAX_TOTAL_WALLETS      = int(os.environ.get("MAX_TOTAL_WALLETS", "800"))
ACTIVITY_WINDOW_HOURS  = int(os.environ.get("ACTIVITY_WINDOW_HOURS", "60"))
MIN_USDT_AMOUNT        = float(os.environ.get("MIN_USDT_AMOUNT", "5.0"))
GRAPH_WORKERS          = int(os.environ.get("GRAPH_WORKERS", "12"))    # 1 CPU box, lowered from 30
# [OPT-5RPS] Bumped 6→8 so the 5 req/s rate limiter (not the worker count)
# stays the binding constraint even when per-call latency is high.
MISTRACK_WORKERS       = int(os.environ.get("MISTRACK_WORKERS", "8"))  # rate limiter is the real floor

# [OPT-SESSION] Per-thread requests.Session reuses HTTP keep-alive connections,
# saving ~20-50 ms of TCP+TLS handshake overhead per TronGrid request.
_session_local = threading.local()

def _get_session() -> requests.Session:
    s = getattr(_session_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"TRON-PRO-API-KEY": TRONGRID_API_KEY})
        _session_local.session = s
    return s

# [OPT-6] Set True to skip re-scanning wallets whose cached risk
# level is already "Low". Set False to always re-scan every wallet.
SKIP_KNOWN_LOW_RISK    = True

# Floor for MistTrack interval recovery after 429 backoff [OPT-7]
# [OPT-5RPS] Raised 3.0 → 5.0 req/s for the MistTrack Compliance Plan
# (5 calls/s · 50,000 calls/day per key). If you ever downgrade the plan
# or see sustained 429s, lower this back toward 1.0–3.0.
# Math: each wallet costs 2 calls (risk_score + address_trace).
#   300 wallets × 2 = 600 calls. At 1/s = 600s. At 3/s = 200s. At 5/s = 120s.
MISTRACK_RATE_FLOOR    = 5.0   # req/s — Compliance Plan ceiling

# ── [FEAT-1] Alert-gated layer expansion ─────────────────────────
# From this layer number onwards, only wallets that carry at least
# one alert matching ALERT_GATE_KEYWORDS will be used as seeds for
# the next layer.  Wallets without alerts are kept in the results
# (they are real senders) but their senders are not explored.
# Set to a value > MAX_LAYERS to disable the gate entirely.
ALERT_GATE_FROM_LAYER  = 3

# Case-insensitive substrings matched against every string in
# MistTrack's detail_list.  Any single match qualifies the wallet.
# Add or remove entries to tune aggressiveness.
ALERT_GATE_KEYWORDS: set = {
    "illicit",
    "darknet",
    "sanctions",
    "ransomware",
    "scam",
    "phish",
    "hack",
    "stolen",
    "mixer",
    "tornado",
    "gambling",
    "fraud",
    "high-risk",
    "high risk",
    "medium-risk",
    "medium risk",
    "interact with",   # catches "Interact With Medium-risk Tag Addresses" etc.
    "involved in",     # catches "Involved Illicit Activity" etc.
}
# ─────────────────────────────────────────────────────────────────

OUTPUT_XLSX  = "wallet_graph_status.xlsx"
HTML_FILE    = "index.html"
CACHE_DB     = "wallet_cache.db"

getcontext().prec = 60

# ================================================================
# === KNOWN CEX HOT-WALLET ADDRESSES (TRC-20) ====================
# ================================================================
CEX_ADDRESSES = {
    "TJDENsfBJs4RFETt1X1W8wMDc8M5XnJhCe",
    "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8",
    "TFFAMQLSPQswt7FYNtVz1x1VcNfABcDMzk",
    "TKVTbh9BxBL6iMELzqGCUgXAYt2mDRRLjf",
    "TVj7RNVHy6thbM7BWdSe9G6gXwKhjhdNZS",
    "TQA9VaV7bCMfhAeATjrTbAGiFhPiCjPfE3",
    "TYASr5UV6HEcXatwdFyffSGZeHericALBq",
    "TM1zzNDZD2DPASbKcgdVoTYhfmYgtfwx9R",
    "TNaRAoLUyYEV2uF7GHBmNwrMNv1Rj5tR4w",
    "TThzxNRLrW2Brp9zAt69r18H1YSwUjNkHB",
    "TKWJdrQkqHisa1X8HUdHEfREvTzw4pMAaY",
    "TUEYcyPAqFBQi3gD8nqVK8GzGqJanP6gTQ",
    "TCqgAmMb5sJaJVEPCvKRLXPgJJhPhsMbFd",
    "TYsBtPHPzKKKN85V2m9Q3D4pkFqLjSmL56",
    "TE2RzoSV3wFK99w6J9UnnZ4vLfXYoxvRwP",
    "TBykAkTFJhRDVzGq5FRPbxMGfZEsVWbJkB",
    "TGfbHFPnHgCFhpqFjgMNfBmSFjAZHiMFxA",
    "TFuGMcwgMzHvSEpWxhWbMJmXJMkDSPVxEZ",
    "TAzsQ9Gx8eqFNFSgD65pqpVpYxNnH73aAH",
}

CEX_LABEL_KEYWORDS = {
    "binance", "okx", "okex", "huobi", "htx", "kucoin", "bybit",
    "gate.io", "gateio", "kraken", "crypto.com", "mexc", "bitfinex",
    "coinbase", "bitstamp", "poloniex", "bittrex", "gemini", "upbit",
    "bithumb", "whitebit", "lbank", "bitget", "cex", "exchange",
    "hot wallet", "hotwallet",
}

# ================================================================
# === SQLITE PERSISTENT CACHE ====================================
# ================================================================
# Single dedicated writer thread fed via a queue.
# Readers use short-lived read-only connections (WAL = safe).
# ================================================================

_db_write_queue: queue.Queue = queue.Queue()
_db_writer_thread: Optional[threading.Thread] = None

# [OPT-DB1] Persistent per-thread read connections — eliminates the overhead
# of opening/closing a new SQLite connection on every _db_get call.
# WAL mode allows concurrent readers alongside the single writer.
_db_read_local = threading.local()

def _get_read_conn() -> sqlite3.Connection:
    """Return (or create) a persistent read connection for this thread."""
    conn = getattr(_db_read_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True,
                               check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA cache_size=-8000")   # 8 MB page cache per reader
        _db_read_local.conn = conn
    return conn


def _db_writer_loop():
    """Dedicated thread that drains the write queue into SQLite.
    [OPT-DB2] Batches up to 200 rows per commit instead of 1-per-commit.
    """
    conn = sqlite3.connect(CACHE_DB, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-8000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            ts    INTEGER NOT NULL
        )
    """)
    conn.commit()

    BATCH_SIZE  = 200
    BATCH_TIMEOUT = 0.5   # seconds — flush even if batch not full

    batch = []
    deadline = time.monotonic() + BATCH_TIMEOUT

    while True:
        try:
            timeout = max(0.0, deadline - time.monotonic())
            item = _db_write_queue.get(timeout=timeout)
            if item is None:
                break
            batch.append(item)
        except queue.Empty:
            pass

        now = time.monotonic()
        if batch and (len(batch) >= BATCH_SIZE or now >= deadline):
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO cache(key, value, ts) VALUES (?,?,?)",
                    [(k, v, int(time.time())) for k, v in batch]
                )
                conn.commit()
            except Exception as e:
                print(f"⚠️  DB writer error: {e}")
            batch.clear()
            deadline = time.monotonic() + BATCH_TIMEOUT
        elif not batch:
            deadline = time.monotonic() + BATCH_TIMEOUT

    # Flush remainder on shutdown
    if batch:
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO cache(key, value, ts) VALUES (?,?,?)",
                [(k, v, int(time.time())) for k, v in batch]
            )
            conn.commit()
        except Exception:
            pass
    conn.close()


def _db_start_writer():
    global _db_writer_thread
    _db_writer_thread = threading.Thread(target=_db_writer_loop, daemon=True)
    _db_writer_thread.start()


def _db_get(key: str) -> Optional[str]:
    """[OPT-DB1] Uses a persistent per-thread read connection."""
    try:
        conn = _get_read_conn()
        row = conn.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    except Exception:
        # Connection may have gone stale — drop it so next call recreates
        _db_read_local.conn = None
        return None


def _db_put(key: str, value: str):
    _db_write_queue.put((key, value))


# ── Cache key helpers ────────────────────────────────────────────
def _cache_key_activity(addr: str) -> str:
    return f"activity:{ACTIVITY_WINDOW_HOURS}h:{addr}"

def _cache_key_cex(addr: str) -> str:
    return f"cex:{addr}"

def _cache_key_tag(addr: str) -> str:
    return f"tag:{addr}"

def _cache_key_mistrack(addr: str) -> str:   # [OPT-6]
    return f"mistrack:{addr}"


# ================================================================
# === RATE LIMITER ===============================================
# ================================================================
class RateLimiter:
    """
    [OPT-RL] Token-bucket rate limiter that releases the lock before
    sleeping, so waiting threads don't queue behind the sleeper.
    """
    def __init__(self, rate: int, per: float):
        self._rate      = rate
        self._per       = per
        self._allowance = float(rate)
        self._last      = time.monotonic()
        self._lock      = threading.Lock()

    def wait(self):
        while True:
            with self._lock:
                now     = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._allowance += elapsed * (self._rate / self._per)
                if self._allowance > self._rate:
                    self._allowance = float(self._rate)
                if self._allowance >= 1.0:
                    self._allowance -= 1.0
                    return          # token acquired — proceed
                # Calculate sleep needed, release lock, then sleep
                sleep_for = (1.0 - self._allowance) * (self._per / self._rate)
            time.sleep(sleep_for)   # lock released during sleep

trongrid_limiter = RateLimiter(rate=12, per=1.0)   # [OPT] raised from 6; TronGrid Pro allows 15/s
mistrack_limiter = RateLimiter(rate=5,  per=1.0)   # [OPT-5RPS] raised 3→5; matches MISTRACK_RATE_FLOOR (Compliance)

# ── TronGrid rate-limit state ────────────────────────────────────
_trongrid_lock         = threading.Lock()
_trongrid_rate         = 12.0
_trongrid_min_interval = 1.0 / 12.0
_trongrid_429_streak   = 0
_trongrid_last_call    = 0.0

# ── MistTrack rate-limit state ───────────────────────────────────
_mistrack_lock              = threading.Lock()
_mistrack_rate              = MISTRACK_RATE_FLOOR
_mistrack_min_interval      = 1.0 / MISTRACK_RATE_FLOOR
_mistrack_429_streak        = 0
_mistrack_last_call         = 0.0
_mistrack_success_streak    = 0   # [OPT-7] for gradual rate recovery

# ================================================================
# === IN-MEMORY CACHES (L1) — backed by SQLite (L2) ==============
# ================================================================
_cache_lock        = threading.Lock()
_activity_cache    = {}
_cex_cache         = {}
_account_tag_cache = {}
_mistrack_cache    = {}   # [OPT-6]


def _load_from_db_into_memory():
    """
    Called once at startup (and at the start of each scan run).
    Loads all persisted cache entries into memory for O(1) L1 hits.
    """
    try:
        # Use a short-lived connection here — this is a bulk read, not a hot path
        conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True,
                               check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute("SELECT key, value FROM cache").fetchall()
        conn.close()
        # Invalidate stale thread-local read connections so they reconnect fresh
        _db_read_local.conn = None
    except Exception:
        return

    loaded = {"activity": 0, "cex": 0, "tag": 0, "mistrack": 0}
    for key, value in rows:
        if key.startswith("activity:"):
            addr   = key.split(":", 2)[-1]
            parts  = value.split("|", 1)
            active = parts[0] == "True"
            last_tx = parts[1] if len(parts) > 1 else "—"
            _activity_cache[addr] = (active, last_tx)
            loaded["activity"] += 1
        elif key.startswith("cex:"):
            addr   = key[4:]
            parts  = value.split("|", 1)
            is_cex = parts[0] == "True"
            reason = parts[1] if len(parts) > 1 else ""
            _cex_cache[addr] = (is_cex, reason)
            loaded["cex"] += 1
        elif key.startswith("tag:"):
            addr = key[4:]
            _account_tag_cache[addr] = value
            loaded["tag"] += 1
        elif key.startswith("mistrack:"):   # [OPT-6]
            addr = key[9:]
            try:
                _mistrack_cache[addr] = json.loads(value)
                loaded["mistrack"] += 1
            except Exception:
                pass

    print(
        f"📦 SQLite cache loaded: "
        f"{loaded['activity']} activity, {loaded['cex']} CEX, "
        f"{loaded['tag']} tags, {loaded['mistrack']} MistTrack"
    )


# ================================================================
# === TRONPY CLIENT SETUP ========================================
# ================================================================
client = Tron(
    HTTPProvider(
        endpoint_uri="https://api.trongrid.io",
        api_key=TRONPY_API_KEY
    )
)

abi = [{
    "inputs": [{"name": "_maker", "type": "address"}],
    "name": "isBlackListed",
    "outputs": [{"name": "", "type": "bool"}],
    "stateMutability": "view",
    "type": "function"
}]

usdt_contract = Contract(
    addr=USDT_CONTRACT_ADDRESS,
    abi=abi,
    client=client
)

# ================================================================
# === HELPER: GOOGLE SHEET =======================================
# ================================================================

def load_google_sheet():
    """
    Always fetches a fresh copy of the sheet. Google's gviz CSV export is
    aggressively CDN-cached, so we:
      1) append a per-call cache-buster query param
      2) send Cache-Control: no-cache headers
      3) feed bytes straight into pandas (no on-disk pandas cache)
    """
    from io import StringIO
    bust = int(time.time() * 1000)
    url = f"{csv_url}&_cb={bust}"
    headers = {
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma":        "no-cache",
        "User-Agent":    f"layers-scanner/{bust}",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        db = pd.read_csv(StringIO(r.text))
        print(f"✅ Google Sheet loaded fresh ({len(db)} rows, cb={bust}).")
    except Exception as e:
        print(f"⚠️  Could not fetch Google Sheet ({e}), using fallback example.")
        db = pd.DataFrame({
            "WALLET": ["TXXX1", "TXXX2", "TXXX3"],
            "NAME":   ["Alice",  "Bob",   "Charlie"]
        })
    return db

# ================================================================
# === HELPER: BLACKLIST CHECK ====================================
# ================================================================

def check_blacklist(wallet_address: str):
    try:
        return usdt_contract.functions.isBlackListed(wallet_address)
    except Exception as e:
        print(f"⚠️  Blacklist check error for {wallet_address}: {e}")
        return None

def check_status(wallet: str) -> str:
    result = check_blacklist(wallet)
    if result is True:  return "BLOCKED"
    if result is False: return "GOOD STANDINGS"
    return "UNKNOWN"

# ================================================================
# === TRONGRID AUTO RATE-LIMIT HELPERS ===========================
# ================================================================

def _trongrid_wait():
    """[OPT-RL] Compute required sleep under lock, then sleep outside it."""
    global _trongrid_last_call
    while True:
        with _trongrid_lock:
            now     = time.monotonic()
            elapsed = now - _trongrid_last_call
            wait    = _trongrid_min_interval - elapsed
            if wait <= 0:
                _trongrid_last_call = now
                return
        time.sleep(wait)   # lock released during sleep


def _trongrid_on_429():
    global _trongrid_rate, _trongrid_min_interval, _trongrid_429_streak
    with _trongrid_lock:
        _trongrid_429_streak  += 1
        _trongrid_min_interval = min(_trongrid_min_interval * 2, 30.0)
        _trongrid_rate         = round(1.0 / _trongrid_min_interval, 4)
        streak   = _trongrid_429_streak
        interval = _trongrid_min_interval
        rate     = _trongrid_rate
    backoff = min(interval * streak, 60.0)
    print(
        f"  ⚠️  TronGrid 429 (streak={streak}) — "
        f"auto-adjusted to {rate} req/s (interval={interval:.2f}s). "
        f"Backing off {backoff:.1f}s..."
    )
    time.sleep(backoff)


def _trongrid_on_success():
    global _trongrid_429_streak
    with _trongrid_lock:
        _trongrid_429_streak = 0


def _trongrid_get(url, params=None, max_retries=6) -> dict:
    # [OPT-SESSION] reuse per-thread session — avoids TCP/TLS handshake per call
    session = _get_session()
    for attempt in range(1, max_retries + 1):
        _trongrid_wait()
        try:
            r = session.get(url, params=params, timeout=15)
            if r.status_code == 429:
                _trongrid_on_429()
                continue
            r.raise_for_status()
            _trongrid_on_success()
            return r.json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                _trongrid_on_429()
                continue
            raise
        except Exception:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            else:
                raise
    raise RuntimeError(f"TronGrid: failed after {max_retries} retries (rate limit)")


# ================================================================
# === HELPER: CEX DETECTION ======================================
# ================================================================

def get_account_tag(wallet_address: str) -> str:
    with _cache_lock:
        if wallet_address in _account_tag_cache:
            return _account_tag_cache[wallet_address]

    db_val = _db_get(_cache_key_tag(wallet_address))
    if db_val is not None:
        with _cache_lock:
            _account_tag_cache[wallet_address] = db_val
        return db_val

    tag = ""
    try:
        url  = TRONGRID_ACCOUNT_URL.format(address=wallet_address)
        data = _trongrid_get(url).get("data", [])
        if data:
            tag = (data[0].get("addressTag", "") or data[0].get("account_name", "")).lower()
    except Exception:
        pass

    with _cache_lock:
        _account_tag_cache[wallet_address] = tag
    _db_put(_cache_key_tag(wallet_address), tag)
    return tag


def is_cex_wallet(wallet_address: str) -> Tuple[bool, str]:
    if wallet_address in CEX_ADDRESSES:
        return True, "known CEX address"

    with _cache_lock:
        if wallet_address in _cex_cache:
            return _cex_cache[wallet_address]

    db_val = _db_get(_cache_key_cex(wallet_address))
    if db_val is not None:
        parts  = db_val.split("|", 1)
        result = (parts[0] == "True", parts[1] if len(parts) > 1 else "")
        with _cache_lock:
            _cex_cache[wallet_address] = result
        return result

    tag    = get_account_tag(wallet_address)
    result = (False, "")
    if tag:
        for keyword in CEX_LABEL_KEYWORDS:
            if keyword in tag:
                result = (True, f"CEX label: '{tag}'")
                break

    with _cache_lock:
        _cex_cache[wallet_address] = result
    _db_put(_cache_key_cex(wallet_address), f"{result[0]}|{result[1]}")
    return result

# ================================================================
# === HELPER: ACTIVITY CHECK =====================================
# ================================================================

def get_last_outgoing_tx_timestamp(wallet_address: str) -> Optional[datetime]:
    try:
        url    = TRONGRID_TXN_URL.format(address=wallet_address)
        params = {
            "limit": 5,
            "only_from": True,
            "contract_address": USDT_CONTRACT_ADDRESS,
        }
        data = _trongrid_get(url, params=params).get("data", [])
        for tx in data:
            ts_ms = tx.get("block_timestamp")
            if ts_ms:
                return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    except Exception as e:
        print(f"  ⚠️  Could not fetch last tx for {wallet_address}: {e}")
    return None


def is_recently_active(wallet_address: str) -> Tuple[bool, str]:
    with _cache_lock:
        if wallet_address in _activity_cache:
            return _activity_cache[wallet_address]

    db_val = _db_get(_cache_key_activity(wallet_address))
    if db_val is not None:
        parts   = db_val.split("|", 1)
        active  = parts[0] == "True"
        last_tx = parts[1] if len(parts) > 1 else "—"
        result  = (active, last_tx)
        with _cache_lock:
            _activity_cache[wallet_address] = result
        return result

    cutoff  = datetime.now(tz=timezone.utc) - timedelta(hours=ACTIVITY_WINDOW_HOURS)
    last_ts = get_last_outgoing_tx_timestamp(wallet_address)

    if last_ts is None:
        result = (False, "never")
    else:
        last_iso = last_ts.strftime("%Y-%m-%d %H:%M UTC")
        result   = (True, last_iso) if last_ts >= cutoff else (False, last_iso)

    with _cache_lock:
        _activity_cache[wallet_address] = result
    _db_put(_cache_key_activity(wallet_address), f"{result[0]}|{result[1]}")
    return result


def _prime_activity_from_txns(sender_addr: str, txns: list):
    """
    [OPT-PRIME] Pre-populate the activity cache for a sender using the
    outgoing-tx list already fetched as part of fetch_incoming_senders_and_prime.
    Avoids a separate TronGrid call in qualify_sender → is_recently_active.
    """
    with _cache_lock:
        if sender_addr in _activity_cache:
            return   # already known
    # txns are outgoing from sender_addr; pick the most recent timestamp
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=ACTIVITY_WINDOW_HOURS)
    last_ts: Optional[datetime] = None
    for tx in txns:
        ts_ms = tx.get("block_timestamp")
        if ts_ms:
            last_ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            break   # already sorted newest-first by TronGrid
    if last_ts is None:
        result = (False, "never")
    else:
        last_iso = last_ts.strftime("%Y-%m-%d %H:%M UTC")
        result = (True, last_iso) if last_ts >= cutoff else (False, last_iso)
    with _cache_lock:
        _activity_cache[sender_addr] = result
    _db_put(_cache_key_activity(sender_addr), f"{result[0]}|{result[1]}")

# ================================================================
# === HELPER: FETCH SENDERS WITH AMOUNTS =========================
# ================================================================

def fetch_incoming_senders(wallet_address: str, limit: int = MAX_SENDERS_PER_WALLET) -> list:
    """
    Fetch top senders AND [OPT-PRIME] pre-populate their activity cache
    in parallel, so qualify_sender never needs a separate TronGrid call
    for the activity check.
    """
    sender_max = {}
    _pow_cache: dict = {}
    try:
        url    = TRONGRID_TXN_URL.format(address=wallet_address)
        params = {
            "limit": limit,
            "only_to": True,
            "contract_address": USDT_CONTRACT_ADDRESS,
        }
        data = _trongrid_get(url, params=params).get("data", [])
        for tx in data:
            sender = tx.get("from", "")
            if not sender or sender == wallet_address:
                continue
            try:
                raw_value = int(tx.get("value", 0))
                decimals  = int(tx.get("token_info", {}).get("decimals", 6))
                divisor   = _pow_cache.get(decimals)
                if divisor is None:
                    divisor = _pow_cache[decimals] = 10 ** decimals
                amount = raw_value / divisor
            except Exception:
                amount = 0.0
            prev = sender_max.get(sender)
            if prev is None or amount > prev:
                sender_max[sender] = amount
            if len(sender_max) >= limit:
                break
    except Exception as e:
        print(f"  ⚠️  Could not fetch senders for {wallet_address}: {e}")
        return []

    senders = list(sender_max.items())

    # [OPT-PRIME] For each sender not yet in activity cache, fetch their
    # outgoing txns in parallel threads — they would otherwise be fetched
    # one-by-one and serially inside qualify_sender.
    uncached = []
    with _cache_lock:
        for addr, _ in senders:
            if addr not in _activity_cache:
                uncached.append(addr)

    if uncached:
        def _fetch_outgoing(addr: str):
            # Skip if a concurrent thread already populated it
            with _cache_lock:
                if addr in _activity_cache:
                    return
            try:
                url    = TRONGRID_TXN_URL.format(address=addr)
                params = {"limit": 5, "only_from": True,
                          "contract_address": USDT_CONTRACT_ADDRESS}
                txns = _trongrid_get(url, params=params).get("data", [])
                _prime_activity_from_txns(addr, txns)
            except Exception:
                pass   # qualify_sender will fall back to a live call if needed

        # Use a bounded pool so we don't explode TronGrid with 20 simultaneous calls
        with ThreadPoolExecutor(max_workers=min(len(uncached), 8)) as pool:
            futures = [pool.submit(_fetch_outgoing, addr) for addr in uncached]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass

    return senders

# ================================================================
# === HELPER: MISTRACK — with cache [OPT-6] + rate recovery [OPT-7]
# ================================================================

def _mistrack_wait():
    """[OPT-RL] Compute required sleep under lock, then sleep outside it."""
    global _mistrack_last_call
    while True:
        with _mistrack_lock:
            now     = time.monotonic()
            elapsed = now - _mistrack_last_call
            wait    = _mistrack_min_interval - elapsed
            if wait <= 0:
                _mistrack_last_call = now
                return
        time.sleep(wait)   # lock released during sleep


def _parse_retry_after(resp) -> Optional[float]:
    """
    [OPT-5RPS] Extract MistTrack's authoritative retry hint.
    MistTrack returns it in the JSON body ({"retry_after": <seconds>});
    fall back to the standard HTTP Retry-After header if present.
    """
    if resp is None:
        return None
    try:
        ra = resp.json().get("retry_after")
        if ra is not None:
            return float(ra)
    except Exception:
        pass
    try:
        return float(resp.headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None


def _mistrack_on_429(log_fn=print, retry_after: Optional[float] = None):
    global _mistrack_rate, _mistrack_min_interval, _mistrack_429_streak, _mistrack_success_streak
    with _mistrack_lock:
        _mistrack_429_streak     += 1
        _mistrack_success_streak  = 0
        _mistrack_min_interval    = min(_mistrack_min_interval * 2, 60.0)
        _mistrack_rate            = round(1.0 / _mistrack_min_interval, 4)
        streak   = _mistrack_429_streak
        interval = _mistrack_min_interval
        rate     = _mistrack_rate
    # [OPT-5RPS] Honour the server-supplied retry_after when present;
    # otherwise fall back to the previous interval×streak heuristic.
    if retry_after is not None and retry_after >= 0:
        backoff = min(float(retry_after), 120.0)
        hint = f" (server retry_after={retry_after:.1f}s)"
    else:
        backoff = min(interval * streak, 120.0)
        hint = ""
    log_fn(
        f"  ⚠️  MistTrack 429 (streak={streak}) — "
        f"auto-adjusted to {rate} req/s (interval={interval:.1f}s). "
        f"Backing off {backoff:.1f}s...{hint}"
    )
    time.sleep(backoff)


def _mistrack_on_success():
    """
    [OPT-7] After 10 consecutive successes, step the interval back down
    toward the configured floor, recovering throughput after a 429 burst.
    """
    global _mistrack_rate, _mistrack_min_interval, _mistrack_429_streak, _mistrack_success_streak
    with _mistrack_lock:
        _mistrack_429_streak      = 0
        _mistrack_success_streak += 1
        if _mistrack_success_streak >= 10 and _mistrack_min_interval > (1.0 / MISTRACK_RATE_FLOOR):
            _mistrack_min_interval = max(
                _mistrack_min_interval * 0.85,
                1.0 / MISTRACK_RATE_FLOOR
            )
            _mistrack_rate            = round(1.0 / _mistrack_min_interval, 4)
            _mistrack_success_streak  = 0


_mistrack_session_local = threading.local()

def _get_mistrack_session() -> requests.Session:
    s = getattr(_mistrack_session_local, "session", None)
    if s is None:
        s = requests.Session()
        _mistrack_session_local.session = s
    return s


def _mistrack_get(url: str, log_fn=print, max_retries: int = 6) -> Optional[dict]:
    """
    Shared GET helper for all MistTrack endpoints.
    Respects the shared rate limiter and 429 backoff.
    Returns the parsed JSON dict on success, None on unrecoverable failure.
    """
    session = _get_mistrack_session()   # [OPT-SESSION] reuse connection
    for attempt in range(1, max_retries + 1):
        _mistrack_wait()
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 429:
                _mistrack_on_429(log_fn=log_fn, retry_after=_parse_retry_after(r))
                continue
            r.raise_for_status()
            data = r.json()
            # [OPT-5RPS] MistTrack may also signal throttling with HTTP 200 +
            # {"success": false, "msg": "ExceededRateLimit"/"ExceededDailyRateLimit"}.
            # Treat those as rate limits (not data errors) and back off properly.
            if (
                isinstance(data, dict)
                and data.get("success") is False
                and "ratelimit" in str(data.get("msg", "")).replace(" ", "").lower()
            ):
                _mistrack_on_429(log_fn=log_fn, retry_after=data.get("retry_after"))
                continue
            _mistrack_on_success()
            return data
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                _mistrack_on_429(log_fn=log_fn, retry_after=_parse_retry_after(e.response))
                continue
            log_fn(f"  ⚠️  MistTrack HTTP error ({e}) for {url}")
            return None
        except Exception as e:
            if attempt < max_retries:
                wait = 2 ** attempt
                log_fn(f"  ⚠️  MistTrack error ({e}), retry {attempt}/{max_retries} in {wait}s...")
                time.sleep(wait)
            else:
                log_fn(f"  ⚠️  MistTrack gave up after {max_retries} retries: {e}")
                return None
    return None


def _parse_use_platform(raw: dict) -> dict:
    """
    Normalise the use_platform block from address_trace into a flat dict
    that is JSON-serialisable and easy to inspect.
    Each key holds {count, list}.
    """
    out = {}
    for key in ("exchange", "dex", "mixer", "nft"):
        block      = raw.get(key, {})
        count      = block.get("count", 0)
        items      = (
            block.get(f"{key}_list")
            or block.get("list", [])
        )
        out[key] = {"count": count, "list": items}
    return out


def _parse_malicious_event(raw: dict) -> dict:
    """
    Normalise the malicious_event block from address_trace.
    Each key holds {count, list}.
    """
    out = {}
    for key in ("phishing", "ransom", "stealing", "laundering"):
        block = raw.get(key, {})
        count = block.get("count", 0)
        items = (
            block.get(f"{key}_list")
            or block.get("list", [])
        )
        out[key] = {"count": count, "list": items}
    return out


def fetch_mistrack(wallet: str, log_fn=print, max_retries: int = 6) -> dict:
    """
    Calls BOTH MistTrack endpoints and merges the results:
      • v2/risk_score      → risk_level, score, detail_list (AML Risk Score)
      • v1/address_trace   → use_platform, malicious_event  (Address Profile)

    [OPT-CONCURRENT] Both calls now fire in parallel threads, cutting
    per-wallet latency from 2× round-trip to ~1× round-trip.
    Both calls still consume 2 rate-limiter tokens.
    """
    risk_url    = (
        f"{MISTTRACK_BASE_URL}"
        f"?coin={MISTTRACK_COIN}&address={wallet}&api_key={MISTTRACK_API_KEY}"
    )
    profile_url = (
        f"{MISTTRACK_PROFILE_URL}"
        f"?coin={MISTTRACK_COIN}&address={wallet}&api_key={MISTTRACK_API_KEY}"
    )

    risk_data_box    = [None]
    profile_data_box = [None]

    def _fetch_risk():
        risk_data_box[0] = _mistrack_get(risk_url, log_fn=log_fn, max_retries=max_retries)

    def _fetch_profile():
        profile_data_box[0] = _mistrack_get(profile_url, log_fn=log_fn, max_retries=max_retries)

    t_risk    = threading.Thread(target=_fetch_risk,    daemon=True)
    t_profile = threading.Thread(target=_fetch_profile, daemon=True)
    t_risk.start()
    t_profile.start()
    t_risk.join()
    t_profile.join()

    risk_data    = risk_data_box[0]
    profile_data = profile_data_box[0]

    if risk_data is None:
        return {"wallet": wallet, "error": f"risk_score call failed after {max_retries} retries"}
    if not risk_data.get("success") or not risk_data.get("data"):
        return {"wallet": wallet, "error": "risk_score: no data"}

    info   = risk_data["data"]
    result: dict = {
        "wallet":      wallet,
        "risk_level":  info.get("risk_level", "N/A"),
        "score":       info.get("score", "N/A"),
        "detail_list": info.get("detail_list", []),
    }

    if profile_data is None or not profile_data.get("success") or not profile_data.get("data"):
        result["use_platform"]    = {}
        result["malicious_event"] = {}
        result["profile_error"]   = "address_trace call failed or returned no data"
    else:
        pdata = profile_data["data"]
        result["use_platform"]    = _parse_use_platform(pdata.get("use_platform", {}))
        result["malicious_event"] = _parse_malicious_event(pdata.get("malicious_event", {}))

    return result


def fetch_mistrack_cached(wallet: str, log_fn=print) -> Tuple[dict, bool]:
    """
    [OPT-6] Returns (result_dict, from_cache).
    Checks L1 memory → L2 SQLite before making a live call.
    Successful results are persisted so re-runs skip already-scanned wallets.
    """
    # L1 memory
    with _cache_lock:
        if wallet in _mistrack_cache:
            return _mistrack_cache[wallet], True

    # L2 SQLite
    db_val = _db_get(_cache_key_mistrack(wallet))
    if db_val is not None:
        try:
            result = json.loads(db_val)
            # Honour SKIP_KNOWN_LOW_RISK: if cached as Low, return immediately
            if SKIP_KNOWN_LOW_RISK and result.get("risk_level") == "Low":
                with _cache_lock:
                    _mistrack_cache[wallet] = result
                return result, True
            # For non-Low cached results, still return from cache (re-scan only if you want fresh data)
            with _cache_lock:
                _mistrack_cache[wallet] = result
            return result, True
        except Exception:
            pass  # corrupted entry — fall through to live fetch

    # Live fetch
    result = fetch_mistrack(wallet, log_fn=log_fn)

    # Cache only successful responses (not errors/timeouts)
    if "error" not in result:
        with _cache_lock:
            _mistrack_cache[wallet] = result
        _db_put(_cache_key_mistrack(wallet), json.dumps(result))

    return result, False


# ================================================================
# === [FEAT-1] ALERT FLAG HELPER =================================
# ================================================================

def has_alert_flag(mistrack_result: dict) -> bool:
    """
    Returns True if the wallet carries any meaningful alert signal from
    EITHER MistTrack module:

    AML Risk Score (v2/risk_score):
      • detail_list contains a string matching ALERT_GATE_KEYWORDS
      • risk_level is not Low / N/A

    Address Profile (v1/address_trace):
      • malicious_event has a non-zero count for any category
        (phishing, ransom, stealing, laundering)
      • use_platform.mixer has a non-zero count (coin mixer interactions)
    """
    # ── AML Risk Score signals ───────────────────────────────────
    detail_list = mistrack_result.get("detail_list", [])
    for detail in detail_list:
        detail_lower = detail.lower()
        for kw in ALERT_GATE_KEYWORDS:
            if kw.lower() in detail_lower:
                return True

    risk = mistrack_result.get("risk_level", "").lower()
    if risk and risk not in ("low", "n/a", ""):
        return True

    # ── Address Profile signals ──────────────────────────────────
    malicious = mistrack_result.get("malicious_event", {})
    for category in ("phishing", "ransom", "stealing", "laundering"):
        if malicious.get(category, {}).get("count", 0) > 0:
            return True

    platform = mistrack_result.get("use_platform", {})
    if platform.get("mixer", {}).get("count", 0) > 0:
        return True

    return False

def _fmt_platform(use_platform: dict) -> str:
    """Compact string: 'exchange(Binance,OKX) mixer(Tornado.Cash)'"""
    parts = []
    for key in ("exchange", "dex", "mixer", "nft"):
        block = use_platform.get(key, {})
        if block.get("count", 0) > 0:
            names = ", ".join(block.get("list", []))
            parts.append(f"{key}({names})")
    return " | ".join(parts) if parts else ""


def _fmt_malicious(malicious_event: dict) -> str:
    """Compact string: 'stealing(MMFinance Exploiter) phishing(...)'"""
    parts = []
    for key in ("phishing", "ransom", "stealing", "laundering"):
        block = malicious_event.get(key, {})
        if block.get("count", 0) > 0:
            names = ", ".join(block.get("list", []))
            parts.append(f"{key}({names})")
    return " | ".join(parts) if parts else ""


def save_xlsx(all_results: list, skipped: list, log_fn=print):
    rows = []
    for r in all_results:
        rows.append({
            "LAYER":           r.get("layer", ""),
            "WALLET":          r.get("wallet", ""),
            "NAME":            r.get("name", ""),
            "LAST_TX":         r.get("last_tx", ""),
            "AMOUNT_USDT":     r.get("amount_usdt", ""),
            "RISK_LEVEL":      r.get("risk_level", r.get("error", "ERROR")),
            "SCORE":           r.get("score", ""),
            "AML_DETAILS":     ", ".join(r.get("detail_list", [])),
            "PLATFORMS":       _fmt_platform(r.get("use_platform", {})),
            "MALICIOUS_EVENTS":_fmt_malicious(r.get("malicious_event", {})),
            "SENT_TO":         ", ".join(r.get("sent_to", [])),
        })
    skip_rows = [{
        "LAYER":       s["layer"],
        "WALLET":      s["wallet"],
        "LAST_TX":     s["last_tx"],
        "AMOUNT_USDT": s.get("amount_usdt", ""),
        "REASON":      s["reason"],
    } for s in skipped]
    try:
        with pd.ExcelWriter(OUTPUT_XLSX, engine="openpyxl") as writer:
            pd.DataFrame(rows).sort_values(["LAYER", "WALLET"]).to_excel(
                writer, sheet_name="Active Wallets", index=False)
            pd.DataFrame(skip_rows).to_excel(
                writer, sheet_name="Skipped", index=False)
        log_fn(f"💾 XLSX saved ({len(rows)} active, {len(skip_rows)} skipped)")
    except Exception as e:
        log_fn(f"⚠️  XLSX save failed: {e}")

# ================================================================
# === QUALIFY A SINGLE SENDER ====================================
# ================================================================
# Filter order [OPT-3]:
#   1. Amount      (free — no network)
#   2. Hard CEX    (free — dict lookup)
#   3. Activity    (1 network call, cached — eliminates most wallets)
#   4. CEX label   (1 network call, cached — only if wallet is active)
# ================================================================

def qualify_sender(sender_addr: str, amount_usdt: float, layer: int) -> dict:
    # Filter 1 — amount (free)
    if amount_usdt < MIN_USDT_AMOUNT:
        return {
            "status":      "skip",
            "wallet":      sender_addr,
            "layer":       layer,
            "last_tx":     "—",
            "amount_usdt": round(amount_usdt, 4),
            "reason":      f"Amount {amount_usdt:.4f} USDT < min {MIN_USDT_AMOUNT} USDT",
            "icon":        "💸",
        }

    # Filter 2 — hard CEX address (free)
    if sender_addr in CEX_ADDRESSES:
        return {
            "status":      "skip",
            "wallet":      sender_addr,
            "layer":       layer,
            "last_tx":     "—",
            "amount_usdt": round(amount_usdt, 4),
            "reason":      "CEX: known CEX address",
            "icon":        "🏦",
        }

    # Filter 3 — activity window (cached)
    active, last_tx = is_recently_active(sender_addr)
    if not active:
        return {
            "status":      "skip",
            "wallet":      sender_addr,
            "layer":       layer,
            "last_tx":     last_tx,
            "amount_usdt": round(amount_usdt, 4),
            "reason":      f"Last tx {last_tx} — older than {ACTIVITY_WINDOW_HOURS}h",
            "icon":        "⏭",
        }

    # Filter 4 — CEX label (cached)
    cex, cex_reason = is_cex_wallet(sender_addr)
    if cex:
        return {
            "status":      "skip",
            "wallet":      sender_addr,
            "layer":       layer,
            "last_tx":     last_tx,
            "amount_usdt": round(amount_usdt, 4),
            "reason":      f"CEX: {cex_reason}",
            "icon":        "🏦",
        }

    return {
        "status":      "accept",
        "wallet":      sender_addr,
        "layer":       layer,
        "last_tx":     last_tx,
        "amount_usdt": round(amount_usdt, 4),
    }

# ================================================================
# === CORE: MULTI-LAYER GRAPH BUILDER ============================
# ================================================================
# [OPT-1] A single ThreadPoolExecutor handles BOTH fetch and qualify
# per receiver — eliminates the serial bottleneck at the start of
# every layer.
#
# [FEAT-1] Accepts an optional `scan_layer_fn` callback invoked
# immediately after each layer is built.  The callback receives
# the newly added wallet addresses for that layer and returns a
# dict {addr: mistrack_result} so the graph builder can decide
# which wallets to expand further (alert-gate logic).
# ================================================================

def _fetch_and_qualify_for_receiver(
    receiver_addr: str,
    seen_addrs: frozenset,   # [OPT] snapshot taken once per layer, not per worker
    layer: int
) -> list:
    """
    Fetches senders for one receiver AND qualifies each one.
    Runs entirely inside a worker thread — fetch + qualify in one shot.
    """
    raw     = fetch_incoming_senders(receiver_addr)
    results = []

    for sender_addr, amount_usdt in raw:
        if sender_addr in seen_addrs:
            continue
        result = qualify_sender(sender_addr, amount_usdt, layer)
        result["_receiver"] = receiver_addr
        results.append(result)

    return results


def build_wallet_graph(
    seed_wallets: dict,
    log_fn=print,
    all_results_ref=None,
    skipped_ref=None,
    scan_layer_fn=None,          # [FEAT-1] optional callback
) -> Tuple[dict, list, list]:
    """
    scan_layer_fn(addresses: list[str], layer: int) -> dict[str, dict]
        Called right after each layer's wallets are committed.
        Returns a mapping of address -> mistrack_result for every
        wallet in `addresses`.  Used to gate which wallets expand
        into the next layer when layer >= ALERT_GATE_FROM_LAYER.
    """

    wallet_meta = {}
    edges       = []
    skipped     = []

    # Layer 0 — seed wallets
    current_layer_wallets = {}
    for addr, name in seed_wallets.items():
        if addr not in wallet_meta:
            wallet_meta[addr] = {
                "name":        name,
                "layer":       0,
                "sent_to":     [],
                "last_tx":     "seed",
                "amount_usdt": None,
            }
            current_layer_wallets[addr] = name

    for layer in range(1, MAX_LAYERS + 1):

        if len(wallet_meta) >= MAX_TOTAL_WALLETS:
            log_fn(f"🛑 MAX_TOTAL_WALLETS ({MAX_TOTAL_WALLETS}) reached. Stopping expansion.")
            break

        log_fn(
            f"\n🔎 Layer {layer} — fetching+qualifying senders for "
            f"{len(current_layer_wallets)} wallet(s) in parallel "
            f"[{len(wallet_meta)}/{MAX_TOTAL_WALLETS} total so far]"
        )

        next_layer_wallets = {}
        accepted           = 0
        total_candidates   = 0
        pending: dict      = {}

        # [OPT] Snapshot known addresses once per layer as a frozenset
        # so worker threads share a single immutable object rather than
        # each copying the full dict.
        seen_snapshot = frozenset(wallet_meta.keys())

        with ThreadPoolExecutor(max_workers=GRAPH_WORKERS) as pool:
            futures = {
                pool.submit(
                    _fetch_and_qualify_for_receiver,
                    receiver_addr,
                    seen_snapshot,   # [OPT] frozenset instead of live dict
                    layer
                ): receiver_addr
                for receiver_addr in current_layer_wallets
            }

            for future in as_completed(futures):
                receiver_addr = futures[future]
                try:
                    results = future.result()
                except Exception as e:
                    log_fn(f"   ⚠️ Worker error for {receiver_addr}: {e}")
                    continue

                log_fn(f"   {receiver_addr} ← {len(results)} candidate(s)")
                total_candidates += len(results)

                for res in results:
                    sender = res["wallet"]
                    if sender in wallet_meta:
                        continue
                    if sender not in pending:
                        pending[sender] = {"result": res, "receivers": [receiver_addr]}
                    else:
                        if res.get("amount_usdt", 0) > pending[sender]["result"].get("amount_usdt", 0):
                            pending[sender]["result"] = res
                        pending[sender]["receivers"].append(receiver_addr)

        # Commit pending results
        newly_added = []
        for sender, entry in pending.items():
            if sender in wallet_meta:
                continue
            res       = entry["result"]
            receivers = entry["receivers"]
            icon      = res.get("icon", "")

            if res["status"] == "skip":
                log_fn(f"     {icon} SKIP {sender} — {res['reason']}")
                skipped.append({
                    "wallet":      sender,
                    "layer":       layer,
                    "last_tx":     res["last_tx"],
                    "amount_usdt": res["amount_usdt"],
                    "reason":      res["reason"],
                })
            else:
                for recv in receivers:
                    edges.append((sender, recv))
                wallet_meta[sender] = {
                    "name":        f"Layer{layer}-sender",
                    "layer":       layer,
                    "sent_to":     receivers,
                    "last_tx":     res["last_tx"],
                    "amount_usdt": res["amount_usdt"],
                }
                newly_added.append(sender)
                accepted += 1
                log_fn(
                    f"     ✅ ADD  {sender} — "
                    f"{res['amount_usdt']:.2f} USDT, last tx: {res['last_tx']}"
                )

            if len(wallet_meta) >= MAX_TOTAL_WALLETS:
                log_fn(f"🛑 MAX_TOTAL_WALLETS hit. Stopping further additions.")
                break

        log_fn(
            f"   → Layer {layer} done: {accepted} accepted, "
            f"{total_candidates - accepted} skipped"
        )

        # ── [FEAT-1] Interleaved MistTrack scan + alert gate ─────
        # Run MistTrack on every wallet just added in this layer so
        # results are ready before we decide which ones to expand.
        if scan_layer_fn is not None and newly_added:
            log_fn(
                f"\n   🔬 Scanning {len(newly_added)} layer-{layer} wallet(s) "
                f"with MistTrack (interleaved)..."
            )
            mt_results = scan_layer_fn(newly_added, layer)

            if layer >= ALERT_GATE_FROM_LAYER:
                # Only expand wallets that carry at least one alert flag
                flagged, clean = [], []
                for addr in newly_added:
                    mt = mt_results.get(addr, {})
                    if has_alert_flag(mt):
                        flagged.append(addr)
                    else:
                        clean.append(addr)

                log_fn(
                    f"   🚦 Alert gate (layer {layer} ≥ {ALERT_GATE_FROM_LAYER}): "
                    f"{len(flagged)} flagged (will expand) | "
                    f"{len(clean)} clean (kept, won't expand)"
                )
                # Expand only flagged wallets
                for addr in flagged:
                    next_layer_wallets[addr] = wallet_meta[addr]["name"]
            else:
                # Below the gate threshold — expand all newly added wallets
                for addr in newly_added:
                    next_layer_wallets[addr] = wallet_meta[addr]["name"]
        else:
            # No scan callback — expand all as before
            for addr in newly_added:
                next_layer_wallets[addr] = wallet_meta[addr]["name"]
        # ─────────────────────────────────────────────────────────

        # [OPT-XLSX] Removed per-layer incremental XLSX save — it dominated
        # wall-clock time at large wallet counts (O(n·layers) DataFrame work).
        # A single save happens at scan completion instead.

        current_layer_wallets = next_layer_wallets
        if not current_layer_wallets:
            log_fn(f"   ℹ️  No wallets to expand at Layer {layer}. Stopping early.")
            break

    return wallet_meta, edges, skipped
