# wallet_inspect.py - isolated wallet detail lookups for the UI.
# Additional lookups only: does not touch the MistTrack scanning workflow.
# Block status reuses the same isBlackListed contract call as
# resources/tron scan/tron_scan.py (check_blacklist/check_status,
# already present in scanner_core).
from datetime import datetime, timezone

import scanner_core as sc

TX_LOOKBACK = 50  # recent USDT transfers checked per wallet


def get_block_status(addr: str, cache: dict) -> str:
    """Returns BLOCKED / GOOD STANDINGS / UNKNOWN. Cached per scan session."""
    if addr not in cache:
        cache[addr] = sc.check_status(addr)
    return cache[addr]


def _is_malicious(cp: str, score_map: dict, block_cache: dict) -> bool:
    """Malicious = MistTrack score > 70 OR blocked (isBlackListed) in tron scan.
    Only wallets from the current scan results are considered; blocked status
    is checked lazily (cached contract call) when the score is not decisive."""
    if cp not in score_map:
        return False
    try:
        if float(score_map[cp] or 0) > 70:
            return True
    except (TypeError, ValueError):
        pass
    return get_block_status(cp, block_cache) == "BLOCKED"


def get_last_malicious_transfer(addr: str, score_map: dict,
                                block_cache: dict, cache: dict):
    """
    Most recent USDT transfer of `addr` whose counterparty is malicious
    (score > 70 or blocked) among the current scan's wallets.
    Returns dict {date, counterparty, direction, amount},
    None if no malicious transfer exists, or {"error": True} on failure.
    Cached per scan session; errors are not cached.
    """
    if addr in cache:
        return cache[addr]
    try:
        url = sc.TRONGRID_TXN_URL.format(address=addr)
        params = {"limit": TX_LOOKBACK, "contract_address": sc.USDT_CONTRACT_ADDRESS}
        txs = sc._trongrid_get(url, params=params).get("data", [])
    except Exception:
        return {"error": True}

    result = None
    for tx in txs:  # TronGrid returns newest first
        frm = tx.get("from", "")
        to = tx.get("to", "")
        counterparty = to if frm == addr else frm
        if counterparty and _is_malicious(counterparty, score_map, block_cache):
            ts_ms = tx.get("block_timestamp")
            if ts_ms:
                date = datetime.fromtimestamp(
                    ts_ms / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
            else:
                date = "unknown"
            try:
                raw = int(tx.get("value", 0))
                dec = int(tx.get("token_info", {}).get("decimals", 6))
                amount = raw / 10 ** dec
            except Exception:
                amount = None
            result = {
                "date": date,
                "counterparty": counterparty,
                "direction": "out" if frm == addr else "in",
                "amount": amount,
            }
            break

    cache[addr] = result
    return result
