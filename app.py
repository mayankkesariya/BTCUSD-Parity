# -*- coding: utf-8 -*-
"""
FAST Ratio Spread Scanner + Dashboard  -  single-file Streamlit app
====================================================================
Merges (1) RatioSpreadScanner_FAST_v5_..._PARALLEL_3.py   (Kite live scanner)
       (2) RatioSpreadDashboard_FAST_v5_..._PARALLEL_3.py (Streamlit dashboard)
       (3) the launcher .bat (ban list + results calendar inputs)

The scanner now runs as a background thread inside this Streamlit process and
writes the same CSV files the dashboard reads (in DATA_DIR, a temp folder).

Streamlit Cloud secrets (App settings -> Secrets):
    KITE_API_KEY      = "..."
    KITE_API_SECRET   = "..."          # needed for the daily token login
    KITE_ACCESS_TOKEN = "..."          # optional, expires daily
    APP_PASSWORD      = "choose-one"   # strongly recommended on a public URL
"""
import hmac
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import copy
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

# Cloud servers run in UTC. The dashboard uses local "now" for market hours and result dates,
# so pin the process to India time.
os.environ["TZ"] = "Asia/Kolkata"
if hasattr(time, "tzset"):
    time.tzset()

import pandas as pd
import pytz
import requests
import streamlit as st
from kiteconnect import KiteConnect
from kiteconnect import exceptions as kite_exceptions
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="FAST Ratio Spread Opportunities & Portfolio", layout="wide")

# Kite's instrument list uses different names for a few stocks; the dashboard and results use NSE names.
NAME_ALIASES = {"M": "M&M"}
IST = pytz.timezone("Asia/Kolkata")
QUOTE_CHUNK = 500

# ----------------------------------------------------------------------------------------------
# PATHS  (Cloud disk is temporary: everything here is rebuilt by the scanner after a restart)
# ----------------------------------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("RATIO_DATA_DIR") or os.path.join(tempfile.gettempdir(), "ratio_out")
os.makedirs(DATA_DIR, exist_ok=True)


def P(name: str) -> str:
    return os.path.join(DATA_DIR, name)


RATIO_CSV_PATH = P("ratio_spread_opportunities_fast.csv")
PARALLEL_CSV_PATH = P("ratio_spread_parallel_opportunities_fast.csv")
NEXT_RATIO_CSV_PATH = P("next_month_ratio_spread_opportunities_fast.csv")
RATIO35_CSV_PATH = P("ratio_spread_opportunities_fast_1x35.csv")
NEXT_RATIO35_CSV_PATH = P("next_month_ratio_spread_opportunities_fast_1x35.csv")
RATIO3_CSV_PATH = P("ratio_spread_opportunities_fast_1x3.csv")
NEXT_RATIO3_CSV_PATH = P("next_month_ratio_spread_opportunities_fast_1x3.csv")
RATIO25_CSV_PATH = P("ratio_spread_opportunities_fast_1x25.csv")
NEXT_RATIO25_CSV_PATH = P("next_month_ratio_spread_opportunities_fast_1x25.csv")
RATIO2_CSV_PATH = P("ratio_spread_opportunities_fast_1x2.csv")
NEXT_RATIO2_CSV_PATH = P("next_month_ratio_spread_opportunities_fast_1x2.csv")
STATUS_CSV_PATH = P("ratio_spread_fast_status.csv")
MARGIN_CACHE_PATH = P("ratio_margin_cache_fast.csv")
BAN_CSV_PATH = P("ban_list.csv")
RESULTS_CSV_PATH = P("results_calendar.csv")
DECLARED_CSV_PATH = P("results_declared.csv")
RATIO_DIR = DATA_DIR
PORTFOLIO_SYMBOLS_PATH = P("portfolio_symbols.json")
SPOT_PRICES_PATH = P("spot_prices.csv")
ALERT_STATE_PATH = P("portfolio_alert_state.json")

# Optional data files committed next to this script in the GitHub repo are copied in once.
for _fn in ("results_calendar.csv", "results_declared.csv", "ban_list.csv"):
    _src = os.path.join(APP_DIR, _fn)
    if os.path.exists(_src) and not os.path.exists(P(_fn)):
        try:
            shutil.copy(_src, P(_fn))
        except Exception:
            pass


def _secret(name: str, default: str = "") -> str:
    """Streamlit secret first, then environment variable."""
    try:
        v = st.secrets.get(name)
    except Exception:
        v = None
    if v not in (None, ""):
        return str(v)
    return os.environ.get(name, default)


def require_password():
    """Public URL protection. Active only when APP_PASSWORD is set in secrets."""
    pw = _secret("APP_PASSWORD")
    if not pw or st.session_state.get("_auth_ok"):
        return
    st.title("🔒 FAST Ratio Spread Dashboard")
    entered = st.text_input("Password", type="password")
    if entered:
        if hmac.compare_digest(entered.encode(), pw.encode()):
            st.session_state["_auth_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()


# ==============================================================================================
# SCANNER ENGINE  (logic taken unchanged from RatioSpreadScanner_FAST_v5_...)
# ==============================================================================================
@dataclass(frozen=True)
class OptInfo:
    token: int
    symbol: str
    tradingsymbol: str
    strike: float
    opt_type: str
    expiry: datetime
    lot_size: int


@dataclass(frozen=True)
class FutInfo:
    token: int
    symbol: str
    tradingsymbol: str
    expiry: datetime
    lot_size: int


def now_ist() -> datetime:
    return datetime.now(IST)


INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def get_exchange_monthly_expiries(nfo: pd.DataFrame) -> List[datetime.date]:
    """Monthly expiries of STOCK options (the universe this scanner trades).

    NIFTY options also list weekly expiries, so using them made "next" resolve to
    next week's date (no stock options exist on it -> empty CSV). Here we take the
    stock-option expiries and keep the last expiry of each calendar month.
    """
    today = now_ist().date()
    opts = nfo[
        (nfo["segment"] == "NFO-OPT") &
        (nfo["instrument_type"].isin(["CE", "PE"])) &
        (~nfo["name"].isin(INDEX_NAMES))
    ]
    dates = sorted({d for d in opts["expiry"].dt.date.unique() if d >= today})
    by_month: Dict[Tuple[int, int], datetime.date] = {}
    for d in dates:
        key = (d.year, d.month)
        if key not in by_month or d > by_month[key]:
            by_month[key] = d
    return sorted(by_month.values())


def _to_dt(d) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=IST)


def compute_target_expiry(expiry_arg: str, nfo: pd.DataFrame = None) -> datetime:
    arg = (expiry_arg or "current").strip().lower()
    if nfo is not None:
        exp = get_exchange_monthly_expiries(nfo)
        if exp:
            if arg == "current":
                return _to_dt(exp[0])
            if arg == "next":
                return _to_dt(exp[1] if len(exp) > 1 else exp[0])
            if len(arg) == 7 and arg[4] == "-":  # YYYY-MM
                y, m = int(arg[:4]), int(arg[5:])
                match = [d for d in exp if d.year == y and d.month == m]
                if match:
                    return _to_dt(match[0])
    raise RuntimeError(f"[ERROR] Could not resolve expiry '{expiry_arg}' from the NFO instrument list.")


def compute_next_expiry_after(target_expiry: datetime, nfo: pd.DataFrame = None) -> datetime:
    target_date = target_expiry.date()
    if nfo is not None:
        later = [e for e in get_exchange_monthly_expiries(nfo) if e > target_date]
        if later:
            return _to_dt(later[0])
    raise RuntimeError("[ERROR] No later monthly expiry found for the next-month scan.")




def load_result_dates_mapping() -> Dict[str, str]:
    """Symbol -> result date (DD-Mon-YYYY) from results_calendar.csv (written by update_results_calendar.py)."""
    if not os.path.exists(RESULTS_CSV_PATH):
        return {}
    try:
        df = pd.read_csv(RESULTS_CSV_PATH)
        if df.empty or "Symbol" not in df.columns or "Date" not in df.columns:
            return {}
        return {
            str(sym).strip().upper(): str(dt).strip()
            for sym, dt in zip(df["Symbol"], df["Date"])
            if str(dt).strip() not in ("", "nan")
        }
    except Exception as e:
        print(f"[WARN] Could not read results calendar: {e}")
        return {}


def atomic_overwrite_csv(path: str, df: pd.DataFrame, retries: int = 8, delay: float = 0.10):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    df.to_csv(tmp, index=False)
    last_err = None
    for _ in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(delay)
    try:
        os.replace(tmp, path)
    except Exception as e:
        last_err = e
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise last_err


def write_keep_last_good(path: str, df: pd.DataFrame):
    """Weekends / market closed produce no live quotes, so the result is empty.
    Keep the last session's saved opportunities instead of overwriting them with nothing."""
    if df is None or df.empty and os.path.exists(path):
        try:
            if pd.read_csv(path, nrows=1).shape[0] > 0:
                return False  # keep yesterday's rows on disk
        except Exception:
            pass
    atomic_overwrite_csv(path, df)
    return True


def fetch_quotes_batched(
    kite: KiteConnect,
    keys: List[Union[int, str]],
    pause_sec: float = 0.02,
) -> Dict[Union[int, str], dict]:
    out: Dict[Union[int, str], dict] = {}
    keys = list(dict.fromkeys(k for k in keys if k is not None))
    for i in range(0, len(keys), QUOTE_CHUNK):
        chunk = keys[i:i + QUOTE_CHUNK]
        if not chunk:
            continue
        q = kite.quote(chunk)
        for k, v in q.items():
            tok = v.get("instrument_token")
            if isinstance(k, str) and ":" in k:
                out[k] = v
            elif tok is not None:
                out[int(tok)] = v
        if pause_sec > 0 and i + QUOTE_CHUNK < len(keys):
            time.sleep(pause_sec)
    return out


def q_ltp(q: dict) -> float:
    try:
        return float(q.get("last_price") or 0.0)
    except Exception:
        return 0.0


def q_oi(q: dict) -> int:
    try:
        return int(q.get("oi") or 0)
    except Exception:
        return 0


def q_volume(q: dict) -> int:
    try:
        return int(q.get("volume") or 0)
    except Exception:
        return 0


def best_bid(q: dict) -> float:
    try:
        buys = (q.get("depth") or {}).get("buy") or []
        return float(buys[0].get("price") or 0.0) if buys else 0.0
    except Exception:
        return 0.0


def best_ask(q: dict) -> float:
    try:
        sells = (q.get("depth") or {}).get("sell") or []
        return float(sells[0].get("price") or 0.0) if sells else 0.0
    except Exception:
        return 0.0


def best_bid_qty(q: dict) -> int:
    try:
        buys = (q.get("depth") or {}).get("buy") or []
        return int(buys[0].get("quantity") or 0) if buys else 0
    except Exception:
        return 0


def best_ask_qty(q: dict) -> int:
    try:
        sells = (q.get("depth") or {}).get("sell") or []
        return int(sells[0].get("quantity") or 0) if sells else 0
    except Exception:
        return 0


def load_instruments(kite: KiteConnect) -> Tuple[pd.DataFrame, pd.DataFrame]:
    nfo = pd.DataFrame(kite.instruments("NFO"))
    nse = pd.DataFrame(kite.instruments("NSE"))

    nfo["expiry"] = pd.to_datetime(nfo["expiry"], utc=True, errors="coerce").dt.tz_convert(IST)
    nfo = nfo.dropna(subset=["expiry"])
    nfo["instrument_token"] = nfo["instrument_token"].astype(int)
    nfo["strike"] = nfo["strike"].astype(float)
    nfo["lot_size"] = nfo["lot_size"].astype(int)
    nfo["instrument_type"] = nfo["instrument_type"].astype(str)
    nfo["segment"] = nfo["segment"].astype(str)
    nfo["name"] = nfo["name"].astype(str).replace(NAME_ALIASES)   # Kite gives "M" for M&M
    nfo["tradingsymbol"] = nfo["tradingsymbol"].astype(str)

    nse["instrument_token"] = nse["instrument_token"].astype(int)
    return nfo, nse


def build_universe(nfo: pd.DataFrame, target_expiry: datetime):
    excluded = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}
    target_date = target_expiry.date()

    opt_df = nfo[
        (nfo["segment"] == "NFO-OPT") &
        (nfo["instrument_type"].isin(["CE", "PE"])) &
        (nfo["expiry"].dt.date == target_date) &
        (~nfo["name"].isin(excluded)) &
        (nfo["strike"] > 0)
    ].copy()

    target_ts = pd.Timestamp(target_expiry).tz_convert(IST)
    fut_df = nfo[
        (nfo["segment"] == "NFO-FUT") &
        (~nfo["name"].isin(excluded)) &
        (nfo["expiry"] >= pd.Timestamp(now_ist()).tz_convert(IST))
    ].sort_values(["name", "expiry"])

    opts_by_symbol = defaultdict(list)
    for r in opt_df.itertuples(index=False):
        opts_by_symbol[str(r.name)].append(
            OptInfo(
                token=int(r.instrument_token),
                symbol=str(r.name),
                tradingsymbol=str(r.tradingsymbol),
                strike=float(r.strike),
                opt_type=str(r.instrument_type),
                expiry=r.expiry.to_pydatetime(),
                lot_size=int(r.lot_size),
            )
        )

    fut_by_symbol: Dict[str, FutInfo] = {}
    for sym, g in fut_df.groupby("name"):
        g = g.sort_values("expiry")
        exact = g[g["expiry"].dt.normalize() == target_ts.normalize()]
        if not exact.empty:
            row = exact.iloc[0]
        else:
            on_or_after = g[g["expiry"] >= target_ts]
            if on_or_after.empty:
                continue
            row = on_or_after.iloc[0]
        fut_by_symbol[str(sym)] = FutInfo(
            token=int(row["instrument_token"]),
            symbol=str(sym),
            tradingsymbol=str(row["tradingsymbol"]),
            expiry=row["expiry"].to_pydatetime(),
            lot_size=int(row["lot_size"]),
        )

    side_chains: Dict[str, Dict[str, List[OptInfo]]] = {}
    for sym, opts in opts_by_symbol.items():
        side_chains[sym] = {
            "CE": sorted((o for o in opts if o.opt_type == "CE"), key=lambda x: x.strike),
            "PE": sorted((o for o in opts if o.opt_type == "PE"), key=lambda x: x.strike),
        }

    return side_chains, fut_by_symbol


def build_spot_tokens(nse: pd.DataFrame, symbols: List[str]) -> Dict[str, int]:
    eq = nse[
        (nse["segment"].astype(str).str.contains("NSE", case=False, na=False)) &
        (nse["instrument_type"].astype(str).str.upper() == "EQ")
    ]
    token_map = dict(zip(eq["tradingsymbol"].astype(str), eq["instrument_token"].astype(int)))
    return {sym: token_map[sym] for sym in symbols if sym in token_map}


def find_atm_index(chain: List[OptInfo], px: float) -> int:
    return min(range(len(chain)), key=lambda i: abs(chain[i].strike - px))


def relevant_otm_tokens(
    chain: List[OptInfo],
    underlying_price: float,
    side: str,
    quote_otm_steps: int,
) -> List[int]:
    if not chain or underlying_price <= 0:
        return []
    atm_idx = find_atm_index(chain, underlying_price)
    if side == "CE":
        end = len(chain) if quote_otm_steps <= 0 else min(len(chain), atm_idx + quote_otm_steps + 1)
        selected = chain[atm_idx:end]
    else:
        start = 0 if quote_otm_steps <= 0 else max(0, atm_idx - quote_otm_steps)
        selected = chain[start:atm_idx + 1]
    return [o.token for o in selected]


def determine_strike_gap_from_atm(
    chain: List[OptInfo],
    option_quotes: Dict[int, dict],
    atm_idx: int,
    side: str,
    ratio: float,
    min_far_bid: float,
) -> Optional[float]:
    atm_q = option_quotes.get(chain[atm_idx].token, {})
    atm_bid = best_bid(atm_q)
    if atm_bid <= 0:
        return None

    target_far_ask = atm_bid / ratio
    search = range(atm_idx + 1, len(chain)) if side == "CE" else range(atm_idx - 1, -1, -1)
    for j in search:
        far_q = option_quotes.get(chain[j].token, {})
        far_ask = best_ask(far_q)
        far_bid = best_bid(far_q)
        if far_ask <= 0 or far_bid < min_far_bid:
            continue
        if far_ask <= target_far_ask:
            return abs(chain[j].strike - chain[atm_idx].strike)
    return None


def create_ratio_candidates_for_chain(
    sym: str,
    chain: List[OptInfo],
    option_quotes: Dict[int, dict],
    underlying_price: float,
    side: str,
    ratio: float,
    scan_otm_steps: int,
    min_far_bid: float,
    min_credit_per_share: float,
    min_bid_qty_lots: int,
    min_ask_qty_lots: int,
    result_mapping: Dict[str, str],
) -> List[dict]:
    if len(chain) < 3 or underlying_price <= 0:
        return []

    by_strike = {o.strike: o for o in chain}
    atm_idx = find_atm_index(chain, underlying_price)
    strike_gap = determine_strike_gap_from_atm(
        chain, option_quotes, atm_idx, side, ratio, min_far_bid
    )
    if not strike_gap:
        return []

    if side == "CE":
        near_indices = range(atm_idx, min(len(chain), atm_idx + scan_otm_steps + 1))
    else:
        near_indices = range(atm_idx, max(-1, atm_idx - scan_otm_steps - 1), -1)

    rows = []
    for i in near_indices:
        near = chain[i]
        far_strike = near.strike + strike_gap if side == "CE" else near.strike - strike_gap
        far = by_strike.get(far_strike)
        if far is None:
            continue

        near_q = option_quotes.get(near.token, {})
        far_q = option_quotes.get(far.token, {})
        near_ask = best_ask(near_q)
        far_bid = best_bid(far_q)
        if near_ask <= 0 or far_bid <= 0 or far_bid < min_far_bid:
            continue

        lot_size = near.lot_size
        if best_ask_qty(near_q) < lot_size * min_ask_qty_lots:
            continue
        if best_bid_qty(far_q) < lot_size * ratio * min_bid_qty_lots:
            continue

        credit_per_share = far_bid * ratio - near_ask
        if credit_per_share < min_credit_per_share:
            continue

        rows.append({
            "symbol": sym,
            "result_date": result_mapping.get(sym.upper(), "TBA"),
            "expiry_date": near.expiry.strftime("%Y-%m-%d"),
            "option_type": side,
            "underlying_price": underlying_price,
            "near_strike": near.strike,
            "far_strike": far.strike,
            "strike_gap": strike_gap,
            "near_tradingsymbol": near.tradingsymbol,
            "far_tradingsymbol": far.tradingsymbol,
            "near_buy_ask": near_ask,
            "far_sell_bid": far_bid,
            "near_ltp": q_ltp(near_q),
            "far_ltp": q_ltp(far_q),
            "near_bid": best_bid(near_q),
            "near_ask": near_ask,
            "far_bid": far_bid,
            "far_ask": best_ask(far_q),
            "near_ask_qty": best_ask_qty(near_q),
            "far_bid_qty": best_bid_qty(far_q),
            "ratio": ratio,
            "lot_size": lot_size,
            "credit_per_share": credit_per_share,
            "credit_amount": credit_per_share * lot_size,
            "near_oi": q_oi(near_q),
            "far_oi": q_oi(far_q),
            "near_volume": q_volume(near_q),
            "far_volume": q_volume(far_q),
        })
    return rows


def _find_strike_index(chain: List[OptInfo], strike: float) -> Optional[int]:
    for i, opt in enumerate(chain):
        if abs(float(opt.strike) - float(strike)) < 1e-9:
            return i
    return None


def local_strike_step_for_normal_gap(
    chain: List[OptInfo],
    atm_idx: int,
    normal_gap: float,
    side: str,
) -> Optional[float]:
    if not chain or normal_gap <= 0:
        return None
    atm_strike = float(chain[atm_idx].strike)
    normal_far_strike = atm_strike + normal_gap if side == "CE" else atm_strike - normal_gap
    far_idx = _find_strike_index(chain, normal_far_strike)
    if far_idx is None or far_idx == atm_idx:
        return None

    closer_idx = far_idx - 1 if side == "CE" else far_idx + 1
    if closer_idx < 0 or closer_idx >= len(chain) or closer_idx == atm_idx:
        return None
    if side == "CE" and not (atm_idx < closer_idx < far_idx):
        return None
    if side == "PE" and not (far_idx < closer_idx < atm_idx):
        return None

    step = abs(float(chain[far_idx].strike) - float(chain[closer_idx].strike))
    return step if step > 0 else None


def create_parallel_incremental_candidates_for_chain(
    sym: str,
    chain: List[OptInfo],
    option_quotes: Dict[int, dict],
    underlying_price: float,
    side: str,
    ratio: float,
    scan_otm_steps: int,
    min_far_bid: float,
    min_incremental_per_share: float,
    min_bid_qty_lots: int,
    min_ask_qty_lots: int,
    result_mapping: Dict[str, str],
) -> List[dict]:
    if len(chain) < 4 or underlying_price <= 0:
        return []

    by_strike = {float(o.strike): o for o in chain}
    atm_idx = find_atm_index(chain, underlying_price)
    normal_gap = determine_strike_gap_from_atm(
        chain, option_quotes, atm_idx, side, ratio, min_far_bid
    )
    if not normal_gap:
        return []

    strike_step = local_strike_step_for_normal_gap(chain, atm_idx, normal_gap, side)
    if not strike_step or strike_step <= 0:
        return []

    parallel_gap = float(normal_gap) - float(strike_step)
    if parallel_gap <= 0:
        return []

    atm_near = chain[atm_idx]
    normal_atm_far_strike = (
        float(atm_near.strike) + float(normal_gap)
        if side == "CE"
        else float(atm_near.strike) - float(normal_gap)
    )
    parallel_atm_far_strike = (
        float(atm_near.strike) + parallel_gap
        if side == "CE"
        else float(atm_near.strike) - parallel_gap
    )
    normal_atm_far = by_strike.get(normal_atm_far_strike)
    parallel_atm_far = by_strike.get(parallel_atm_far_strike)
    if normal_atm_far is None or parallel_atm_far is None:
        return []

    atm_near_q = option_quotes.get(atm_near.token, {})
    normal_atm_q = option_quotes.get(normal_atm_far.token, {})
    parallel_atm_q = option_quotes.get(parallel_atm_far.token, {})
    atm_near_ask = best_ask(atm_near_q)
    normal_atm_bid = best_bid(normal_atm_q)
    parallel_atm_bid = best_bid(parallel_atm_q)
    if atm_near_ask <= 0 or parallel_atm_bid <= 0:
        return []

    # ATM base = the SAME spread structure at the ATM strike, at real entry prices:
    # buy ATM near at ask, sell ratio x ATM parallel far at bid.
    # Incremental = this strike's parallel credit minus this ATM base.
    atm_curve_premium_per_share = float(ratio) * parallel_atm_bid - atm_near_ask

    if side == "CE":
        near_indices = range(atm_idx, min(len(chain), atm_idx + scan_otm_steps + 1))
    else:
        near_indices = range(atm_idx, max(-1, atm_idx - scan_otm_steps - 1), -1)

    rows = []
    for i in near_indices:
        near = chain[i]
        normal_far_strike = (
            float(near.strike) + float(normal_gap)
            if side == "CE"
            else float(near.strike) - float(normal_gap)
        )
        parallel_far_strike = (
            float(near.strike) + parallel_gap
            if side == "CE"
            else float(near.strike) - parallel_gap
        )
        normal_far = by_strike.get(normal_far_strike)
        parallel_far = by_strike.get(parallel_far_strike)
        if normal_far is None or parallel_far is None:
            continue

        near_q = option_quotes.get(near.token, {})
        normal_far_q = option_quotes.get(normal_far.token, {})
        parallel_far_q = option_quotes.get(parallel_far.token, {})

        near_ask = best_ask(near_q)
        normal_far_bid = best_bid(normal_far_q)
        parallel_far_bid = best_bid(parallel_far_q)
        if near_ask <= 0 or parallel_far_bid <= 0 or parallel_far_bid < min_far_bid:
            continue

        lot_size = int(near.lot_size)
        if best_ask_qty(near_q) < lot_size * min_ask_qty_lots:
            continue
        if best_bid_qty(parallel_far_q) < lot_size * ratio * min_bid_qty_lots:
            continue

        normal_credit_per_share = (
            float(ratio) * normal_far_bid - near_ask if normal_far_bid > 0 else float("nan")
        )
        parallel_credit_per_share = float(ratio) * parallel_far_bid - near_ask

        actual_parallel_credit_per_share = parallel_credit_per_share
        incremental_per_share = actual_parallel_credit_per_share - atm_curve_premium_per_share

        if incremental_per_share < min_incremental_per_share:
            continue

        incremental_amount = incremental_per_share * lot_size
        rows.append({
            "symbol": sym,
            "result_date": result_mapping.get(sym.upper(), "TBA"),
            "expiry_date": near.expiry.strftime("%Y-%m-%d"),
            "option_type": side,
            "underlying_price": underlying_price,
            "near_strike": float(near.strike),
            "normal_far_strike": float(normal_far.strike),
            "parallel_far_strike": float(parallel_far.strike),
            "normal_gap": float(normal_gap),
            "parallel_gap": float(parallel_gap),
            "strike_step": float(strike_step),
            "near_tradingsymbol": near.tradingsymbol,
            "far_tradingsymbol": parallel_far.tradingsymbol,
            "normal_far_tradingsymbol": normal_far.tradingsymbol,
            "parallel_far_tradingsymbol": parallel_far.tradingsymbol,
            "near_buy_ask": near_ask,
            "normal_far_sell_bid": normal_far_bid,
            "parallel_far_sell_bid": parallel_far_bid,
            "atm_near_ask": atm_near_ask,
            "atm_normal_far_bid": normal_atm_bid,
            "atm_parallel_far_bid": parallel_atm_bid,
            "atm_curve_premium_per_share": atm_curve_premium_per_share,
            "actual_parallel_credit_per_share": actual_parallel_credit_per_share,
            "incremental_per_share": incremental_per_share,
            "incremental_amount": incremental_amount,
            "normal_credit_per_share": normal_credit_per_share,
            "parallel_credit_per_share": parallel_credit_per_share,
            "normal_credit_amount": normal_credit_per_share * lot_size if normal_far_bid > 0 else float("nan"),
            "parallel_credit_amount": parallel_credit_per_share * lot_size,
            "ratio": ratio,
            "lot_size": lot_size,
            "near_ask_qty": best_ask_qty(near_q),
            "normal_far_bid_qty": best_bid_qty(normal_far_q),
            "parallel_far_bid_qty": best_bid_qty(parallel_far_q),
            "near_oi": q_oi(near_q),
            "normal_far_oi": q_oi(normal_far_q),
            "parallel_far_oi": q_oi(parallel_far_q),
            "near_volume": q_volume(near_q),
            "normal_far_volume": q_volume(normal_far_q),
            "parallel_far_volume": q_volume(parallel_far_q),
        })
    return rows


def margin_cache_key(row: dict, product: str) -> str:
    return "|".join([
        product,
        str(row.get("expiry_date", "")),
        str(row.get("near_tradingsymbol", "")),
        str(row.get("far_tradingsymbol", "")),
        str(int(row.get("lot_size", 0))),
        str(float(row.get("ratio", 0))),
    ])


def margin_for_ratio_spread(kite: KiteConnect, row: dict, product: str = "NRML") -> Optional[float]:
    lot = int(row["lot_size"])
    ratio = float(row["ratio"])
    
    if abs(ratio - 3.5) < 0.01:
        near_qty = lot * 2
        far_qty = lot * 7
        margin_divisor = 2.0
    else:
        near_qty = lot
        far_qty = int(lot * ratio)
        margin_divisor = 1.0

    basket = [
        {
            "exchange": "NFO",
            "tradingsymbol": row["near_tradingsymbol"],
            "transaction_type": "BUY",
            "variety": "regular",
            "product": product,
            "order_type": "MARKET",
            "quantity": near_qty,
        },
        {
            "exchange": "NFO",
            "tradingsymbol": row["far_tradingsymbol"],
            "transaction_type": "SELL",
            "variety": "regular",
            "product": product,
            "order_type": "MARKET",
            "quantity": far_qty,
        },
    ]
    try:
        m = kite.basket_order_margins(basket, consider_positions=False, mode="compact")
        total_margin = 0.0
        if isinstance(m, dict):
            if "final" in m and isinstance(m["final"], dict):
                total_margin = float(m["final"].get("total") or 0)
            else:
                total_margin = float(m.get("total") or 0)
        else:
            total_margin = float(m)
        
        return total_margin / margin_divisor if total_margin > 0 else 0.0
    except Exception as e:
        print(f"[WARN] basket margin failed {row.get('symbol')} {row.get('near_tradingsymbol')}-{row.get('far_tradingsymbol')}: {e}")
        return None


def load_margin_cache(path: str) -> Dict[str, dict]:
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path)
        out = {}
        for r in df.to_dict("records"):
            key = str(r.get("cache_key") or "")
            margin = pd.to_numeric(r.get("margin_required"), errors="coerce")
            fetched_epoch = pd.to_numeric(r.get("fetched_epoch"), errors="coerce")
            if key and pd.notna(margin) and float(margin) > 0 and pd.notna(fetched_epoch):
                out[key] = {"margin": float(margin), "fetched_epoch": float(fetched_epoch)}
        return out
    except Exception as e:
        return {}


def save_margin_cache(path: str, cache: Dict[str, dict], max_rows: int = 2000):
    if not cache:
        return
    rows = [
        {
            "cache_key": k,
            "margin_required": v["margin"],
            "fetched_epoch": v["fetched_epoch"],
        }
        for k, v in cache.items()
    ]
    df = pd.DataFrame(rows).sort_values("fetched_epoch", ascending=False).head(max_rows)
    atomic_overwrite_csv(path, df)


def select_balanced_candidates(candidates, top_credit: int) -> pd.DataFrame:
    """Keep the top_credit best-credit candidates for EACH side (CE and PE) and interleave them,
    so calls are never crowded out by puts and both sides get their share of the margin lookups."""
    cand = pd.DataFrame(candidates)
    if cand.empty or "option_type" not in cand.columns:
        return cand.nlargest(top_credit, "credit_amount").copy() if not cand.empty else cand
    parts = []
    for _, grp in cand.groupby("option_type"):
        g = grp.nlargest(top_credit, "credit_amount").copy()
        g["_side_rank"] = range(len(g))
        parts.append(g)
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values(["_side_rank", "credit_amount"], ascending=[True, False]).drop(columns="_side_rank").reset_index(drop=True)


def build_ranked_output(
    kite: KiteConnect,
    candidates: List[dict],
    ts: datetime,
    product: str,
    top_credit: int,
    top_output: int,
    margin_cache: Dict[str, dict],
    margin_cache_ttl: float,
    margin_stale_ttl: float,
    fresh_margin_limit: int,
    margin_pause_sec: float,
) -> Tuple[pd.DataFrame, dict]:
    empty_cols = [
        "scan_time", "return_on_margin_pct", "credit_amount", "symbol", "result_date", "expiry_date",
        "option_type", "near_strike", "far_strike", "strike_gap", "margin_required",
        "near_buy_ask", "far_sell_bid", "margin_status", "margin_age_sec",
    ]
    if not candidates:
        return pd.DataFrame(columns=empty_cols), {
            "margin_api_calls": 0, "margin_cache_hits": 0, "margin_stale_uses": 0,
            "margin_skipped": 0, "margin_checked": 0,
        }

    df0 = select_balanced_candidates(candidates, top_credit)
    now_epoch = time.time()
    final_rows = []
    api_calls = 0
    cache_hits = 0
    stale_uses = 0
    skipped = 0

    for r in df0.to_dict("records"):
        key = margin_cache_key(r, product)
        entry = margin_cache.get(key)
        margin = None
        status = ""
        age = None

        if entry:
            age = max(0.0, now_epoch - float(entry["fetched_epoch"]))
            if age <= margin_cache_ttl:
                margin = float(entry["margin"])
                status = "CACHE"
                cache_hits += 1

        if margin is None and api_calls < fresh_margin_limit:
            fresh = margin_for_ratio_spread(kite, r, product=product)
            api_calls += 1
            if fresh is not None and fresh > 0:
                margin = float(fresh)
                margin_cache[key] = {"margin": margin, "fetched_epoch": time.time()}
                status = "LIVE"
                age = 0.0
            if margin_pause_sec > 0:
                time.sleep(margin_pause_sec)

        if margin is None and entry:
            age = max(0.0, now_epoch - float(entry["fetched_epoch"]))
            if age <= margin_stale_ttl:
                margin = float(entry["margin"])
                status = "STALE"
                stale_uses += 1

        if margin is None or margin <= 0:
            skipped += 1
            continue

        r["margin_required"] = margin
        r["return_on_margin_pct"] = (float(r["credit_amount"]) / margin) * 100.0
        r["scan_time"] = ts.strftime("%Y-%m-%d %H:%M:%S%z")
        r["margin_status"] = status
        r["margin_age_sec"] = round(float(age or 0.0), 1)
        final_rows.append(r)

    df = pd.DataFrame(final_rows)
    if not df.empty:
        df = df.nlargest(top_output, "return_on_margin_pct").copy()
        first_cols = [
            "scan_time", "return_on_margin_pct", "credit_amount", "symbol", "result_date", "expiry_date",
            "option_type", "near_strike", "far_strike", "strike_gap", "margin_required",
            "margin_status", "margin_age_sec",
        ]
        other_cols = [c for c in df.columns if c not in first_cols]
        df = df[first_cols + other_cols]

    stats = {
        "margin_api_calls": api_calls,
        "margin_cache_hits": cache_hits,
        "margin_stale_uses": stale_uses,
        "margin_skipped": skipped,
        "margin_checked": len(df0),
    }
    return df, stats


def build_parallel_ranked_output(
    kite: KiteConnect,
    candidates: List[dict],
    ts: datetime,
    product: str,
    top_incremental: int,
    top_output: int,
    margin_cache: Dict[str, dict],
    margin_cache_ttl: float,
    margin_stale_ttl: float,
    fresh_margin_limit: int,
    margin_pause_sec: float,
) -> Tuple[pd.DataFrame, dict]:
    """
    Margin resolution and ranking for parallel candidates.

    RANKING: always by incremental ROM % (incremental_amount / margin). Rupee amount is never used
    to choose or rank rows. (top_incremental is kept only for CLI compatibility and is not used.)

    MARGIN, resolved in this order so rows do not drop out between refreshes:
      1. CACHE  - cache entry younger than margin_cache_ttl, no API call.
      2. LIVE   - fresh basket margin call, limited to fresh_margin_limit per cycle.
                  Spreads that already have a margin on record are refreshed first, so the
                  known rows stay current; new spreads are added as the call budget allows.
      3. STALE  - last known margin, used when the call budget is spent and the entry is younger
                  than margin_stale_ttl. This keeps a row visible for a few minutes between refreshes.
      4. skipped - no usable margin at all (never priced, or older than margin_stale_ttl).
    """
    empty_cols = [
        "scan_time", "parallel_return_on_margin_pct", "incremental_return_on_margin_pct",
        "incremental_amount", "incremental_per_share", "symbol", "result_date", "expiry_date", "option_type",
        "underlying_price", "near_strike", "parallel_far_strike", "normal_far_strike",
        "parallel_gap", "normal_gap", "strike_step", "margin_required",
        "atm_curve_premium_per_share", "actual_parallel_credit_per_share",
        "margin_status", "margin_age_sec",
    ]
    if not candidates:
        return pd.DataFrame(columns=empty_cols), {
            "margin_api_calls": 0, "margin_cache_hits": 0, "margin_stale_uses": 0,
            "margin_skipped": 0, "margin_checked": 0,
        }

    now_epoch = time.time()
    pool = pd.DataFrame(candidates).copy()

    # Split candidates into those with a fresh cache entry and those still needing a margin.
    resolved = []   # (row, margin, status, age)
    pending = []    # (row, entry_or_None)
    for r in pool.to_dict("records"):
        entry = margin_cache.get(margin_cache_key(r, product))
        if entry:
            age = max(0.0, now_epoch - float(entry["fetched_epoch"]))
            if age <= margin_cache_ttl:
                resolved.append((r, float(entry["margin"]), "CACHE", age))
                continue
        pending.append((r, entry))

    # Fresh calls: known spreads first (keeps them current), then by rupee amount as a
    # tie-breaker for the call order only. Ranking itself is still by ROM below.
    pending.sort(key=lambda x: (x[1] is None, -float(x[0]["incremental_amount"])))

    api_calls = 0
    stale_uses = 0
    skipped = 0
    for r, entry in pending:
        key = margin_cache_key(r, product)
        margin, status, age = None, "", 0.0

        if api_calls < fresh_margin_limit:
            fresh = margin_for_ratio_spread(kite, r, product=product)
            api_calls += 1
            if fresh is not None and fresh > 0:
                margin = float(fresh)
                margin_cache[key] = {"margin": margin, "fetched_epoch": time.time()}
                status = "LIVE"
            if margin_pause_sec > 0:
                time.sleep(margin_pause_sec)

        if margin is None and entry:
            age = max(0.0, now_epoch - float(entry["fetched_epoch"]))
            if age <= margin_stale_ttl:
                margin = float(entry["margin"])
                status = "STALE"
                stale_uses += 1

        if margin is None or margin <= 0:
            skipped += 1
            continue
        resolved.append((r, margin, status, age))

    final_rows = []
    for r, margin, status, age in resolved:
        r["margin_required"] = margin
        r["incremental_return_on_margin_pct"] = float(r["incremental_amount"]) / margin * 100.0
        r["parallel_return_on_margin_pct"] = float(r["parallel_credit_amount"]) / margin * 100.0
        r["scan_time"] = ts.strftime("%Y-%m-%d %H:%M:%S%z")
        r["margin_status"] = status
        r["margin_age_sec"] = round(float(age), 1)
        final_rows.append(r)

    cache_hits = sum(1 for x in resolved if x[2] == "CACHE")
    df = pd.DataFrame(final_rows)
    if not df.empty:
        # Ranked by incremental ROM % only.
        df = df.sort_values("incremental_return_on_margin_pct", ascending=False).head(top_output).copy()
        first_cols = [
            "scan_time", "parallel_return_on_margin_pct", "parallel_credit_amount",
            "incremental_return_on_margin_pct", "incremental_amount",
            "incremental_per_share", "symbol", "result_date", "expiry_date", "option_type",
            "underlying_price", "near_strike", "parallel_far_strike", "normal_far_strike",
            "parallel_gap", "normal_gap", "strike_step", "margin_required",
            "atm_curve_premium_per_share", "actual_parallel_credit_per_share",
            "margin_status", "margin_age_sec",
        ]
        other_cols = [c for c in df.columns if c not in first_cols]
        df = df[first_cols + other_cols]

    stats = {
        "margin_api_calls": api_calls,
        "margin_cache_hits": cache_hits,
        "margin_stale_uses": stale_uses,
        "margin_skipped": skipped,
        "margin_checked": len(pool),
    }
    return df, stats


# ==============================================================================================
# SCANNER SERVICE  (the old main() loop, now a background thread that survives Streamlit reruns)
# ==============================================================================================
@dataclass
class ScannerConfig:
    expiry: str = "current"          # current | next | YYYY-MM
    side: str = "BOTH"               # CE | PE | BOTH
    ratio: float = 4.0
    ratio35: float = 3.5
    ratio3: float = 3.0
    ratio25: float = 2.5
    ratio2: float = 2.0
    refresh_sec: float = 2.0
    scan_otm_steps: int = 25
    quote_otm_steps: int = 0
    min_far_bid: float = 0.05
    min_credit_per_share: float = 0.05
    min_incremental_per_share: float = 0.05
    min_bid_qty_lots: int = 1
    min_ask_qty_lots: int = 1
    product: str = "NRML"
    margin_cache_ttl: float = 30.0
    margin_stale_ttl: float = 180.0
    margin_pause_sec: float = 0.02
    quote_pause_sec: float = 0.02


# Same 11 outputs, file names, top-N and fresh-margin budgets as the original CLI defaults.
# prefix = prefix of that series' columns in the status CSV.
SERIES_SPECS = [
    dict(key="base",     month="current", kind="ratio",    ratio_attr="ratio",   file="ratio_spread_opportunities_fast.csv",                  top_credit=50, top_output=50, fresh=25, prefix=""),
    dict(key="parallel", month="current", kind="parallel", ratio_attr="ratio",   file="ratio_spread_parallel_opportunities_fast.csv",         top_credit=40, top_output=40, fresh=15, prefix="parallel_"),
    dict(key="next",     month="next",    kind="ratio",    ratio_attr="ratio",   file="next_month_ratio_spread_opportunities_fast.csv",       top_credit=50, top_output=50, fresh=15, prefix="next_"),
    dict(key="r35",      month="current", kind="ratio",    ratio_attr="ratio35", file="ratio_spread_opportunities_fast_1x35.csv",             top_credit=50, top_output=50, fresh=12, prefix="ratio35_"),
    dict(key="n35",      month="next",    kind="ratio",    ratio_attr="ratio35", file="next_month_ratio_spread_opportunities_fast_1x35.csv",  top_credit=50, top_output=50, fresh=12, prefix="next_ratio35_"),
    dict(key="r3",       month="current", kind="ratio",    ratio_attr="ratio3",  file="ratio_spread_opportunities_fast_1x3.csv",              top_credit=50, top_output=50, fresh=12, prefix="ratio3_"),
    dict(key="n3",       month="next",    kind="ratio",    ratio_attr="ratio3",  file="next_month_ratio_spread_opportunities_fast_1x3.csv",   top_credit=50, top_output=50, fresh=12, prefix="next_ratio3_"),
    dict(key="r25",      month="current", kind="ratio",    ratio_attr="ratio25", file="ratio_spread_opportunities_fast_1x25.csv",             top_credit=50, top_output=50, fresh=12, prefix="ratio25_"),
    dict(key="n25",      month="next",    kind="ratio",    ratio_attr="ratio25", file="next_month_ratio_spread_opportunities_fast_1x25.csv",  top_credit=50, top_output=50, fresh=12, prefix="next_ratio25_"),
    dict(key="r2",       month="current", kind="ratio",    ratio_attr="ratio2",  file="ratio_spread_opportunities_fast_1x2.csv",              top_credit=50, top_output=50, fresh=12, prefix="ratio2_"),
    dict(key="n2",       month="next",    kind="ratio",    ratio_attr="ratio2",  file="next_month_ratio_spread_opportunities_fast_1x2.csv",   top_credit=50, top_output=50, fresh=12, prefix="next_ratio2_"),
]


def _load_universe(kite: KiteConnect, cfg: ScannerConfig) -> dict:
    nfo, nse = load_instruments(kite)
    target = compute_target_expiry(cfg.expiry, nfo)
    next_target = compute_next_expiry_after(target, nfo)
    cur_chains, cur_fut = build_universe(nfo, target)
    nxt_chains, nxt_fut = build_universe(nfo, next_target)
    cur_syms, nxt_syms = sorted(cur_chains.keys()), sorted(nxt_chains.keys())
    spot_tokens = build_spot_tokens(nse, sorted(set(cur_syms) | set(nxt_syms)))
    return {
        "date": now_ist().date(), "target": target, "next_target": next_target,
        "cur_chains": cur_chains, "cur_fut": cur_fut, "cur_syms": cur_syms,
        "nxt_chains": nxt_chains, "nxt_fut": nxt_fut, "nxt_syms": nxt_syms,
        "spot_tokens": spot_tokens,
    }


def _resolve_underlying(symbols, chains, fut_by_symbol, spot_tokens, q_under, sides, quote_otm_steps):
    underlying, tokens = {}, []
    for sym in symbols:
        fut = fut_by_symbol.get(sym)
        px = q_ltp(q_under.get(fut.token, {})) if fut else 0.0
        if px <= 0:
            px = q_ltp(q_under.get(spot_tokens.get(sym), {}))
        if px <= 0:
            continue
        underlying[sym] = px
        for side in sides:
            tokens.extend(relevant_otm_tokens(chains[sym][side], px, side, quote_otm_steps))
    return underlying, tokens


def _run_cycle(kite, cfg: ScannerConfig, uni: dict, margin_cache: dict, ts, cycle_no: int, stop: threading.Event) -> dict:
    t0 = time.perf_counter()
    sides = ["CE", "PE"] if cfg.side == "BOTH" else [cfg.side]
    result_mapping = load_result_dates_mapping()

    underlying_keys = list(dict.fromkeys(
        [f.token for f in uni["cur_fut"].values()] +
        [f.token for f in uni["nxt_fut"].values()] +
        list(uni["spot_tokens"].values())
    ))
    q_under = fetch_quotes_batched(kite, underlying_keys, pause_sec=cfg.quote_pause_sec)

    cur_under, cur_tokens = _resolve_underlying(uni["cur_syms"], uni["cur_chains"], uni["cur_fut"], uni["spot_tokens"], q_under, sides, cfg.quote_otm_steps)
    nxt_under, nxt_tokens = _resolve_underlying(uni["nxt_syms"], uni["nxt_chains"], uni["nxt_fut"], uni["spot_tokens"], q_under, sides, cfg.quote_otm_steps)
    all_option_tokens = list(dict.fromkeys(cur_tokens + nxt_tokens))
    q_opt = fetch_quotes_batched(kite, all_option_tokens, pause_sec=cfg.quote_pause_sec)
    if stop.is_set():
        raise InterruptedError

    outputs: Dict[str, pd.DataFrame] = {}
    stats_row: Dict[str, object] = {}
    for spec in SERIES_SPECS:
        is_cur = spec["month"] == "current"
        chains = uni["cur_chains"] if is_cur else uni["nxt_chains"]
        under = cur_under if is_cur else nxt_under
        ratio = getattr(cfg, spec["ratio_attr"])

        cands: List[dict] = []
        for sym, px in under.items():
            for side in sides:
                if spec["kind"] == "parallel":
                    cands.extend(create_parallel_incremental_candidates_for_chain(
                        sym, chains[sym][side], q_opt, px, side, ratio, cfg.scan_otm_steps, cfg.min_far_bid,
                        cfg.min_incremental_per_share, cfg.min_bid_qty_lots, cfg.min_ask_qty_lots, result_mapping))
                else:
                    cands.extend(create_ratio_candidates_for_chain(
                        sym, chains[sym][side], q_opt, px, side, ratio, cfg.scan_otm_steps, cfg.min_far_bid,
                        cfg.min_credit_per_share, cfg.min_bid_qty_lots, cfg.min_ask_qty_lots, result_mapping))

        builder = build_parallel_ranked_output if spec["kind"] == "parallel" else build_ranked_output
        df, m = builder(kite, cands, ts, cfg.product, spec["top_credit"], spec["top_output"], margin_cache,
                        cfg.margin_cache_ttl, cfg.margin_stale_ttl, spec["fresh"], cfg.margin_pause_sec)
        outputs[spec["key"]] = df
        pre = spec["prefix"]
        stats_row[f"{pre}raw_candidates"] = len(cands)
        stats_row[f"{pre}margin_checked"] = m["margin_checked"]
        stats_row[f"{pre}margin_api_calls"] = m["margin_api_calls"]
        stats_row[f"{pre}margin_cache_hits"] = m["margin_cache_hits"]
        stats_row[f"{pre}margin_stale_uses"] = m["margin_stale_uses"]
        stats_row[f"{pre}margin_skipped"] = m["margin_skipped"]
        stats_row[f"{pre}output_rows"] = len(df)
        if stop.is_set():
            raise InterruptedError

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # Spot price of every scanned underlying (used by portfolio spike alerts on the dashboard)
    try:
        spot_rows = [{"Symbol": s_, "Spot": float(p_), "Scan Time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")}
                     for s_, p_ in cur_under.items()]
        atomic_overwrite_csv(SPOT_PRICES_PATH, pd.DataFrame(spot_rows, columns=["Symbol", "Spot", "Scan Time"]))
    except Exception:
        pass

    for spec in SERIES_SPECS:
        write_keep_last_good(P(spec["file"]), outputs[spec["key"]])
    save_margin_cache(MARGIN_CACHE_PATH, margin_cache)

    status_row = {
        "scan_time": ts.strftime("%Y-%m-%d %H:%M:%S%z"),
        "cycle": cycle_no,
        "scan_duration_ms": round(elapsed_ms, 1),
        "symbols": len(cur_under),
        "next_symbols": len(nxt_under),
        "quoted_option_tokens": len(all_option_tokens),
        "current_quoted_option_tokens": len(cur_tokens),
        "next_quoted_option_tokens": len(nxt_tokens),
        **stats_row,
        "side": cfg.side, "ratio": cfg.ratio, "ratio35": cfg.ratio35, "ratio3": cfg.ratio3,
        "ratio25": cfg.ratio25, "ratio2": cfg.ratio2,
        "expiry": uni["target"].strftime("%Y-%m-%d"),
        "next_expiry": uni["next_target"].strftime("%Y-%m-%d"),
    }
    try:
        atomic_overwrite_csv(STATUS_CSV_PATH, pd.DataFrame([status_row]))
    except Exception:
        pass

    return {
        "elapsed_ms": elapsed_ms,
        "symbols": len(cur_under), "next_symbols": len(nxt_under),
        "quoted_tokens": len(all_option_tokens),
        "expiry": status_row["expiry"], "next_expiry": status_row["next_expiry"],
        "rows": {s["key"]: len(outputs[s["key"]]) for s in SERIES_SPECS},
    }


def _scanner_thread(svc: "ScannerService", cfg: ScannerConfig, api_key: str, token: str, stop: threading.Event):
    try:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(token)
        margin_cache = load_margin_cache(MARGIN_CACHE_PATH)
        uni, cycle_no = None, 0
        while not stop.is_set():
            t0 = time.perf_counter()
            ts = now_ist()
            wait_extra = 0.0
            try:
                # (re)load instruments on first run and whenever the date changes (expiry roll-over)
                if uni is None or uni["date"] != ts.date():
                    svc._update(state="loading instruments")
                    uni = _load_universe(kite, cfg)
                cycle_no += 1
                info = _run_cycle(kite, cfg, uni, margin_cache, ts, cycle_no, stop)
                svc._update(state="running", cycle=cycle_no, last_ok=ts.strftime("%d-%b %H:%M:%S"), last_error="", **info)
            except InterruptedError:
                break
            except kite_exceptions.TokenException as e:
                svc._update(state="token expired", last_error=f"{e} - log in to Kite again.")
                wait_extra = 30.0
            except Exception as e:
                svc._update(state="error (retrying)", last_error=f"{type(e).__name__}: {e}")
                wait_extra = 5.0 if uni else 10.0
            stop.wait(max(wait_extra, cfg.refresh_sec - (time.perf_counter() - t0)))
    except Exception as e:  # never let the thread die silently
        svc._update(state="crashed", last_error=f"{type(e).__name__}: {e}")
    finally:
        with svc._lock:
            if svc._stop is stop and svc.status.get("state") != "crashed":
                svc.status["state"] = "stopped"


class ScannerService:
    """One per server process (see get_service). Holds credentials, config and the scanner thread."""

    def __init__(self):
        self._lock = threading.RLock()
        self.api_key = _secret("KITE_API_KEY")
        self.access_token = _secret("KITE_ACCESS_TOKEN")
        self.cfg = ScannerConfig()
        self._thread: Optional[threading.Thread] = None
        self._stop: Optional[threading.Event] = None
        self.user_stopped = False
        self.status = {
            "state": "stopped", "cycle": 0, "elapsed_ms": None, "last_ok": None, "last_error": "",
            "rows": {}, "symbols": 0, "next_symbols": 0, "quoted_tokens": 0, "expiry": "", "next_expiry": "",
        }

    # ---- helpers
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.access_token)

    def token_fingerprint(self) -> str:
        return f"{self.api_key}:{self.access_token[-6:]}"

    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def _update(self, **kw):
        with self._lock:
            self.status.update(kw)

    def snapshot(self) -> dict:
        with self._lock:
            snap = dict(self.status)
        snap["running"] = self.is_running()
        return snap

    # ---- control
    def start(self):
        with self._lock:
            if self.is_running():
                return
            if not self.has_credentials():
                self.status["state"] = "no credentials"
                return
            self.user_stopped = False
            self._stop = threading.Event()
            self.status.update(state="starting", last_error="")
            self._thread = threading.Thread(
                target=_scanner_thread, name="ratio-scanner", daemon=True,
                args=(self, copy.copy(self.cfg), self.api_key, self.access_token, self._stop))
            self._thread.start()

    def stop(self, user: bool = True):
        with self._lock:
            if user:
                self.user_stopped = True
            ev, t = self._stop, self._thread
        if ev is not None:
            ev.set()
        if t is not None and t.is_alive():
            t.join(timeout=3)
        self._update(state="stopped")

    def restart(self):
        self.stop(user=False)
        self.start()

    def ensure_running(self):
        """Called on every Streamlit run: starts the scanner after a (re)deploy / wake-up."""
        if not self.user_stopped and not self.is_running() and self.has_credentials():
            self.start()

    def set_credentials(self, api_key: str, access_token: str):
        changed = (api_key != self.api_key) or (access_token != self.access_token)
        self.api_key, self.access_token = api_key, access_token
        if changed:
            self.user_stopped = False
            self.restart()

    def set_config(self, cfg: ScannerConfig):
        self.cfg = cfg
        self.restart()


@st.cache_resource(show_spinner=False)
def get_service() -> ScannerService:
    return ScannerService()


def make_kite() -> KiteConnect:
    """Kite client for dashboard-side calls (portfolio margins, lot sizes)."""
    svc = get_service()
    if not svc.has_credentials():
        raise RuntimeError("Kite credentials are not set")
    kite = KiteConnect(api_key=svc.api_key)
    kite.set_access_token(svc.access_token)
    return kite


SERVICE = get_service()


# ==============================================================================================
# DASHBOARD  (logic taken unchanged from RatioSpreadDashboard_FAST_v5_...)
# ==============================================================================================
DETAIL_SERIES = [
    ("Current 1:4", RATIO_CSV_PATH, 4.0),
    ("Current 1:3.5", RATIO35_CSV_PATH, 3.5),
    ("Current 1:3", RATIO3_CSV_PATH, 3.0),
    ("Current 1:2.5", RATIO25_CSV_PATH, 2.5),
    ("Current 1:2", RATIO2_CSV_PATH, 2.0),
    ("Next Month 1:4", NEXT_RATIO_CSV_PATH, 4.0),
    ("Next Month 1:3.5", NEXT_RATIO35_CSV_PATH, 3.5),
    ("Next Month 1:3", NEXT_RATIO3_CSV_PATH, 3.0),
    ("Next Month 1:2.5", NEXT_RATIO25_CSV_PATH, 2.5),
    ("Next Month 1:2", NEXT_RATIO2_CSV_PATH, 2.0),
]


@st.cache_data
def _detail_read_csv(path, mtime_ns):
    return pd.read_csv(path)


def render_stock_detail_view(symbol):
    st.title(f"{symbol} - Strike Parity & All Combinations")
    st.markdown("[← Back to main dashboard](/)")

    refresh_on = st.checkbox(
        "Auto refresh this page every 5 seconds (only while this page is open)",
        value=False, key="detail_refresh_on",
    )
    if refresh_on:
        st_autorefresh(interval=5000, key="stock_detail_refresh")

    frames = []
    for label, path, ratio in DETAIL_SERIES:
        if not os.path.exists(path):
            continue
        try:
            df = _detail_read_csv(path, os.stat(path).st_mtime_ns).copy()
        except Exception:
            continue
        if df.empty or "symbol" not in df.columns:
            continue
        df = df[df["symbol"].astype(str).str.upper() == symbol].copy()
        if df.empty:
            continue
        df["series"] = label
        df["series_ratio"] = ratio
        frames.append(df)

    if not frames:
        st.info(f"No saved combinations for {symbol} in any ratio right now.")
        return

    combos = pd.concat(frames, ignore_index=True)
    for c in ["near_strike", "far_strike", "underlying_price", "return_on_margin_pct",
              "credit_amount", "credit_per_share", "margin_required",
              "near_buy_ask", "far_sell_bid", "strike_gap"]:
        if c in combos.columns:
            combos[c] = pd.to_numeric(combos[c], errors="coerce")

    # Distance from spot, same definition as the main table: (strike - spot) / spot * 100
    combos["near_dist_pct"] = (combos["near_strike"] - combos["underlying_price"]) / combos["underlying_price"] * 100.0
    combos["far_dist_pct"] = (combos["far_strike"] - combos["underlying_price"]) / combos["underlying_price"] * 100.0

    combos = apply_spread_quality_filters(combos)
    if combos.empty:
        st.info(f"No {symbol} spreads pass the no-trade and 1st-leg distance filters right now.")
        return

    spot_series = combos["underlying_price"].dropna()
    spot_val = float(spot_series.iloc[0]) if not spot_series.empty else float("nan")
    result_date = str(combos["result_date"].iloc[0]) if "result_date" in combos.columns else "TBA"
    expiry_val = str(combos["expiry_date"].iloc[0]) if "expiry_date" in combos.columns else "-"

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Spot / Fut", f"{spot_val:,.2f}")
    m2.metric("Combinations", len(combos))
    m3.metric("Best ROM %", f"{combos['return_on_margin_pct'].max():.2f}%")
    m4.metric("Expiry", expiry_val)
    m5.metric("Result Date", result_date)

    st.caption(
        "Combinations saved by the scanner after its filters (minimum credit, liquidity, bid/ask). "
        "Distance % = (strike - spot) / spot."
    )

    buy_label = "Buy Strike (1)"
    for label, _, ratio in DETAIL_SERIES:
        part = combos[combos["series"] == label]
        if part.empty:
            continue
        part = part.sort_values(["option_type", "near_strike", "far_strike"])
        sell_label = f"Sell Strike (x{ratio:g})"

        st.subheader(f"{label}  ·  {len(part)} combinations")
        view = pd.DataFrame({
            "Type": part["option_type"],
            "Expiry": part["expiry_date"],
            buy_label: part["near_strike"],
            sell_label: part["far_strike"],
            "Gap": part["strike_gap"],
            "Buy Dist %": part["near_dist_pct"],
            "Sell Dist %": part["far_dist_pct"],
            "Buy Ask ₹": part["near_buy_ask"],
            "Sell Bid ₹": part["far_sell_bid"],
            "Credit ₹/Share": part["credit_per_share"],
            "Credit ₹": part["credit_amount"],
            "ROM %": part["return_on_margin_pct"],
            "Margin ₹": part["margin_required"],
        })
        fmt = {
            buy_label: "{:.0f}", sell_label: "{:.0f}", "Gap": "{:.0f}",
            "Buy Dist %": "{:+.2f}%", "Sell Dist %": "{:+.2f}%",
            "Buy Ask ₹": "{:.2f}", "Sell Bid ₹": "{:.2f}",
            "Credit ₹/Share": "₹{:.2f}", "Credit ₹": "₹{:,.0f}",
            "ROM %": "{:.2f}%", "Margin ₹": "₹{:,.0f}",
        }
        st.dataframe(view.style.format(fmt, na_rep="-"), width="stretch", hide_index=True)


@st.cache_data(show_spinner=False, max_entries=30)
def _read_csv_cached(path: str, modified_ns: int, size_bytes: int):
    return pd.read_csv(path)

def safe_read_csv(path: str):
    if not os.path.exists(path):
        return None, f"CSV not found: {path}"
    try:
        stat = os.stat(path)
        return _read_csv_cached(path, stat.st_mtime_ns, stat.st_size).copy(), None
    except Exception as e:
        return None, f"Could not read CSV: {e}"

RESULT_RECENT_DAYS = 7   # results declared in the last N days stay visible (green)
RESULT_POPUP_DAYS = 1    # pop-up fires for results today or in the last N days
RESULT_CSS = {
    "today": "background-color:#b91c1c;color:#ffffff;font-weight:700",
    "upcoming": "background-color:#1d4ed8;color:#ffffff;font-weight:600",
    "recent": "background-color:#15803d;color:#ffffff;font-weight:600",
}


def parse_event_date(value):
    s = str(value).strip()
    if not s or s.lower() in ("nan", "tba", "none", "nat", "null"):
        return pd.NaT
    # Bulletproof parsing attempting common structural setups before fallback
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S"):
        try:
            dt = pd.to_datetime(s, format=fmt)
            return dt.normalize()
        except:
            continue
    dt = pd.to_datetime(s, errors="coerce", dayfirst=True)
    if pd.isna(dt):
        dt = pd.to_datetime(s, errors="coerce", dayfirst=False)
    return dt.normalize() if pd.notna(dt) else pd.NaT


def result_status_for(dt):
    if pd.isna(dt):
        return ""
    today = pd.Timestamp.now().normalize()
    if dt == today:
        return "today"
    if dt > today:
        return "upcoming"
    if (today - dt).days <= RESULT_RECENT_DAYS:
        return "recent"
    return ""


def result_cell_css(value):
    return RESULT_CSS.get(result_status_for(parse_event_date(value)), "")




def build_result_calendar():
    today = pd.Timestamp.now().normalize()
    rows = []
    if os.path.exists(RESULTS_CSV_PATH):
        try:
            rc = pd.read_csv(RESULTS_CSV_PATH)
            # Remove any trailing invisible spaces from column names that mess up the mapping
            rc.columns = [str(c).strip() for c in rc.columns]
            
            # Robust mapping if scraper saves column slightly differently
            sym_col = next((c for c in rc.columns if c.lower() in ['symbol', 'company']), 'Symbol')
            date_col = next((c for c in rc.columns if c.lower() in ['date', 'result_date', 'event_date']), 'Date')
            link_col = next((c for c in rc.columns if c.lower() in ['link', 'url', 'source']), 'Link')

            for _, r in rc.iterrows():
                sym_val = str(r.get(sym_col, "")).strip().upper()
                if not sym_val or sym_val == "NAN":
                    continue
                rows.append({
                    "Symbol": sym_val,
                    "Date": parse_event_date(r.get(date_col, "")),
                    "Link": str(r.get(link_col, "") or "") if link_col in rc.columns else "",
                })
        except Exception:
            pass
            
    cal = pd.DataFrame(rows, columns=["Symbol", "Date", "Link"])
    if cal.empty:
        return pd.DataFrame(columns=["Symbol", "Date", "Link", "Status"])
    cal = cal[(cal["Symbol"] != "") & cal["Date"].notna()]
    cal = cal[cal["Date"] >= today - pd.Timedelta(days=RESULT_RECENT_DAYS)].copy()
    cal["_dist"] = (cal["Date"] - today).dt.days.abs()
    cal = cal.sort_values(["Symbol", "_dist"]).drop_duplicates("Symbol")
    cal["Link"] = [
        l if (l and l.lower() != "nan") else f"https://www.nseindia.com/companies-listing/corporate-filings-announcements?symbol={s}"
        for s, l in zip(cal["Symbol"], cal["Link"])
    ]
    cal["Status"] = cal["Date"].apply(result_status_for)
    cal = cal[cal["Status"] != ""]
    return cal.drop(columns="_dist").reset_index(drop=True)


def load_events_database():
    """Reads both the upcoming calendar and the already declared results to ensure no data is lost."""
    mapping = {}
    
    # 1. Read Scheduled Calendar (No date cutoff so we don't lose past dates mapped in scanner)
    if os.path.exists(RESULTS_CSV_PATH):
        try:
            rc = pd.read_csv(RESULTS_CSV_PATH)
            rc.columns = [str(c).strip() for c in rc.columns]
            sym_col = next((c for c in rc.columns if c.lower() in ['symbol', 'company']), 'Symbol')
            date_col = next((c for c in rc.columns if c.lower() in ['date', 'result_date', 'event_date']), 'Date')
            for _, r in rc.iterrows():
                sym = str(r.get(sym_col, "")).strip().upper()
                if sym and sym != "NAN":
                    dt = parse_event_date(r.get(date_col, ""))
                    if pd.notna(dt):
                        mapping[sym] = dt.strftime("%d-%b-%Y")
        except Exception:
            pass
            
    # 2. Override with actually Declared Results (if any)
    if os.path.exists(DECLARED_CSV_PATH):
        try:
            dec = pd.read_csv(DECLARED_CSV_PATH)
            dec.columns = [str(c).strip() for c in dec.columns]
            sym_col = next((c for c in dec.columns if c.lower() in ['symbol', 'company']), 'Symbol')
            date_col = next((c for c in dec.columns if c.lower() in ['declared at', 'date', 'time']), 'Declared At')
            for _, r in dec.iterrows():
                sym = str(r.get(sym_col, "")).strip().upper()
                if sym and sym != "NAN":
                    dt = parse_event_date(r.get(date_col, ""))
                    if pd.notna(dt):
                        mapping[sym] = dt.strftime("%d-%b-%Y") + " (Declared)"
        except Exception:
            pass
            
    return mapping




RESULTS_ACK_PATH = os.path.join(os.path.dirname(RATIO_CSV_PATH), "results_acknowledged.json")


def _load_result_acks():
    try:
        with open(RESULTS_ACK_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_result_acks(acks):
    tmp = RESULTS_ACK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(acks), f)
    os.replace(tmp, RESULTS_ACK_PATH)


def _acknowledge_result(key):
    acks = _load_result_acks()
    acks.add(key)
    _save_result_acks(acks)


def _acknowledge_all_results(keys):
    acks = _load_result_acks()
    acks.update(keys)
    _save_result_acks(acks)


CATEGORY_HEADLINE = {
    "Results": "FINANCIAL RESULTS ANNOUNCED",
    "Board outcome": "OUTCOME OF BOARD MEETING",
    "Concall": "CONCALL / INVESTOR MEET",
    "Board meeting": "BOARD MEETING INTIMATION",
    "Dividend / corporate action": "DIVIDEND / CORPORATE ACTION",
}


def _show_result_dialog(r):
    """Modal pop-up, same layout as the ZeroVega alert. It stays open until OK is pressed,
    and OK marks the item acknowledged on disk."""
    sym = str(r["Symbol"])
    ex = str(r.get("Exchange", "NSE") or "NSE")
    cat = str(r.get("Category", "") or "")
    key = str(r["Key"])
    pub = pd.to_datetime(str(r.get("Published At", "")), errors="coerce")
    dec_t = pd.to_datetime(str(r.get("Declared At", "")), errors="coerce")
    lag = (dec_t - pub).total_seconds() if (pd.notna(pub) and pd.notna(dec_t)) else None
    link = str(r.get("Link") or "https://www.nseindia.com")

    @st.dialog(f"LIVE FINANCIAL RESULTS — {sym} ({ex})")
    def _dlg():
        st.info(f"**{sym} — {CATEGORY_HEADLINE.get(cat, cat.upper())}**")
        st.write(str(r.get("Desc", "")))
        if pd.notna(pub):
            st.caption(f"{ex} dissemination: {pub.strftime('%d-%b-%Y %I:%M:%S %p')} IST")
        if lag is not None and lag >= 0:
            st.caption(f"Detected {lag:.1f}s after dissemination")
        c1, c2 = st.columns(2)
        if c1.button("OK", key=f"dlg_ok_{key}"):
            _acknowledge_result(key)
            st.rerun()
        c2.link_button(f"Open {ex} filing", link)

    _dlg()


def check_result_popups():
    """Results and announcements recorded today. The oldest unacknowledged one opens as a
    pop-up that stays until OK is pressed. All unacknowledged items are listed below it."""
    dec, err = safe_read_csv(DECLARED_CSV_PATH)
    if err or dec is None or dec.empty or "Key" not in dec.columns:
        return
    declared = pd.to_datetime(dec["Declared At"].astype(str), errors="coerce")
    dec = dec[declared >= pd.Timestamp.now().normalize()].copy()
    acks = _load_result_acks()
    pending = dec[~dec["Key"].astype(str).isin(acks)].copy()
    if pending.empty:
        return

    queue = pending.sort_values("Declared At", ascending=True)
    if "dialog_sounded" not in st.session_state:
        st.session_state["dialog_sounded"] = set()
    first = queue.iloc[0]
    if str(first["Key"]) not in st.session_state["dialog_sounded"]:
        st.session_state["dialog_sounded"].add(str(first["Key"]))
        play_filing_alert_sound()
    if not _DIALOG_STATE["open"]:
        _DIALOG_STATE["open"] = True
        _show_result_dialog(first)

    st.markdown(f"### 📢 Results & announcements to acknowledge ({len(pending)})")
    st.button("✅ Acknowledge all", key="ack_all_results",
              on_click=_acknowledge_all_results, args=(pending["Key"].astype(str).tolist(),))
    for _, r in pending.sort_values("Published At", ascending=False).iterrows():
        k = str(r["Key"])
        c1, c2 = st.columns([6, 1])
        c1.error(
            f"**{r['Symbol']}** · {r.get('Exchange', 'NSE')} · {r.get('Category', '')}: {r.get('Desc', '')}. "
            f"Filed {r.get('Published At', '')}. [Open filing ↗]({r.get('Link', '')})"
        )
        c2.button("✅ Acknowledge", key=f"ack_res_{k}", on_click=_acknowledge_result, args=(k,))


def get_stock_sector(symbol: str) -> str:
    sector_mapping = {
        "ADANIENT": "Energy & Infra", "RELIANCE": "Energy & Oil", "POLICYBZR": "Fintech / Financials",
        "SBIN": "Banking", "HDFCBANK": "Banking", "SBICARD": "Financial Services",
        "TRENT": "Consumer & Retail", "HAL": "Defense & Aero", "JIOFIN": "Financial Services",
        "CDSL": "Financial Services", "COCHINSHIP": "Defense & Aero", "SOLARINDS": "Chemicals & Defense",
        "BDL": "Defense & Aero", "BLUESTARCO": "Consumer Durables", "COLPAL": "FMCG",
        "DLF": "Real Estate", "EICHERMOT": "Auto", "FORTIS": "Healthcare",
        "HAVELLS": "Consumer Durables", "IDFCFIRSTB": "Banking", "IOC": "Energy & Oil",
        "IREDA": "Green Energy", "KEI": "Capital Goods", "MAXHEALTH": "Healthcare",
        "MAZDOCK": "Defense & Aero", "ONGC": "Energy & Oil", "PAGEIND": "Consumer & Retail",
        "PIDILITIND": "Chemicals", "RADICO": "Consumer Goods", "RVNL": "Railways & Infra",
        "SUNPHARMA": "Pharma", "UNOMINDA": "Auto Ancillary", "ATHERENERG": "Auto",
        "DIXON": "Consumer Durables", "WAAREEENER": "Green Energy", "FEDERALBNK": "Banking",
        "NIFTY": "Index", "BANKNIFTY": "Index", "FINNIFTY": "Index", "MIDCPNIFTY": "Index", 
        "SENSEX": "Index", "BANKEX": "Index"
    }
    return sector_mapping.get(str(symbol).upper(), "Other / General")

def play_filing_alert_sound():
    audio_html = """
    <audio autoplay>
      <source src="https://assets.mixkit.co/active_storage/sfx/2869/2869-preview.mp3" type="audio/mpeg">
    </audio>
    """
    st.markdown(audio_html, unsafe_allow_html=True)

EMPTY_RATIO_COLS = [
    "symbol", "result_date", "expiry_date", "option_type", "underlying_price", "near_strike", "far_strike",
    "strike_gap", "near_buy_ask", "far_sell_bid", "near_ask_qty", "far_bid_qty", "near_oi", "far_oi",
    "return_on_margin_pct", "credit_amount", "credit_per_share", "margin_required", "margin_status",
    "margin_age_sec", "scan_time", "lot_size", "ratio",
]


def _empty_ratio_frame():
    """No made-up rows: an empty table with the real columns, so the UI shows only what the scanner saved."""
    return pd.DataFrame(columns=EMPTY_RATIO_COLS)


def get_preview_dataframe(ratio_val, is_parallel=False, is_next_month=False):
    return _empty_ratio_frame()



def apply_ce_pe_filter(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "option_type" not in df.columns:
        return df
    if ce_pe_filter == "📈 CE Spreads Only":
        return df[df["option_type"].astype(str).str.upper() == "CE"]
    elif ce_pe_filter == "📉 PE Spreads Only":
        return df[df["option_type"].astype(str).str.upper() == "PE"]
    return df

def add_distance_metrics(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "underlying_price" not in df.columns:
        return df
    if "near_strike" in df.columns:
        df["near_dist_pct"] = ((df["near_strike"] - df["underlying_price"]) / df["underlying_price"]) * 100.0
    if "far_strike" in df.columns:
        df["far_dist_pct"] = ((df["far_strike"] - df["underlying_price"]) / df["underlying_price"]) * 100.0
    elif "parallel_far_strike" in df.columns:
        df["far_dist_pct"] = ((df["parallel_far_strike"] - df["underlying_price"]) / df["underlying_price"]) * 100.0
    return df

def apply_spread_quality_filters(df: pd.DataFrame) -> pd.DataFrame:
    """
    Applied to every spread view (ratio tabs, parallel tab, best-per-stock table, stock detail page).
    1) NO-TRADE FILTER (always on): a spread is hidden if any leg it trades has zero volume today,
       i.e. that strike has not traded. Uses near_volume + far_volume (parallel rows: near_volume +
       parallel_far_volume). If the CSV has no volume columns yet, nothing can be proven to trade,
       so nothing is shown until the scanner is re-run.
    2) 1ST-LEG DISTANCE FILTER (sidebar toggle + threshold): keeps a spread only if its 1st (buy)
       leg is at least min_leg_dist_pct % away from spot (either side).
    """
    if df is None or df.empty:
        return df
    out = df.copy()

    if "parallel_far_strike" in out.columns:
        trade_legs = ["near_volume", "parallel_far_volume"]
    else:
        trade_legs = ["near_volume", "far_volume"]
    if not all(c in out.columns for c in trade_legs):
        st.warning("Traded volume is not recorded in this CSV yet, so no-trade strikes cannot be checked. "
                   "Re-run the scanner. Rows are hidden until then.")
        return out.iloc[0:0].copy()
    vol = out[trade_legs].apply(pd.to_numeric, errors="coerce").fillna(0)
    out = out[(vol > 0).all(axis=1)]

    if dist_filter_on and "near_strike" in out.columns and "underlying_price" in out.columns:
        near = pd.to_numeric(out["near_strike"], errors="coerce")
        spot = pd.to_numeric(out["underlying_price"], errors="coerce")
        dist_abs = ((near - spot) / spot * 100.0).abs()
        out = out[dist_abs >= min_leg_dist_pct]

    return out.copy()

def style_map_available(styler, func, subset_cols):
    subset_cols = [c for c in subset_cols if c in styler.data.columns]
    if subset_cols:
        if hasattr(styler, "map"):
            return styler.map(func, subset=subset_cols)
        else:
            return styler.applymap(func, subset=subset_cols)
    return styler

def highlight_rom(val):
    try:
        if isinstance(val, str):
            v = float(val.replace('%', ''))
        else:
            v = float(val)
        if v > 0.8:
            return "background-color: #198754; color: white; font-weight: bold"
        elif v > 0.4:
            return "background-color: #ffc107; color: black; font-weight: bold"
    except Exception:
        pass
    return ""

def highlight_remaining_rs(val):
    try:
        v = float(val)
        if v > 0:
            return "color: #ff6b6b; font-weight: bold"
        else:
            return "color: #51cf66; font-weight: bold"
    except Exception:
        pass
    return ""

def highlight_debit_excess(val):
    try:
        v = float(val)
        if v > 0:
            return "background-color: #198754; color: white; font-weight: bold"
    except Exception:
        pass
    return ""

def highlight_ban_rows(row):
    raw_sym = str(row.get("Symbol", row.get("symbol", ""))).strip()
    if "symbol=" in raw_sym:  # Symbol cell holds a detail-view link; extract the name
        raw_sym = raw_sym.split("symbol=")[1].split("&")[0]
    sym = raw_sym.upper()
    if sym in banned_symbols_set:
        return ["background-color: #7f1d1d; color: #fca5a5; font-weight: bold"] * len(row)
    return [""] * len(row)

def render_data_status(csv_path, label):
    df, err = safe_read_csv(csv_path)
    rows = 0 if (err or df is None) else len(df)
    if os.path.exists(csv_path):
        last = pd.Timestamp.fromtimestamp(os.path.getmtime(csv_path)).strftime("%d-%b %H:%M:%S")
    else:
        last = "file not found"
    st.caption(f"Data status · {label}: {rows} saved rows · last written {last} · {os.path.basename(csv_path)}")


def render_generic_ratio_tab(csv_path, title_prefix, ratio_val, status_key_prefix, is_next_month):
    target_name = title_prefix
    render_data_status(csv_path, target_name)
    
    # 1. Read the raw unfiltered CSV file so we have ALL combinations available
    raw_df, err = safe_read_csv(csv_path)
    if err or raw_df is None or raw_df.empty:
        raw_df = get_preview_dataframe(ratio_val, is_parallel=False, is_next_month=is_next_month)
    
    _scan_stamp = str(raw_df["scan_time"].dropna().iloc[0]) if ("scan_time" in raw_df.columns and raw_df["scan_time"].notna().any()) else "unknown"
    _now = pd.Timestamp.now()
    _mins = _now.hour * 60 + _now.minute
    _market_open = _now.weekday() < 5 and (9 * 60 + 15) <= _mins <= (15 * 60 + 30)
    _status = "Market open" if _market_open else "Market closed (weekend/holiday/after hours)"
    st.info(f"{target_name} 1:{ratio_val} · {_status}. Showing opportunities from the last live scan: {_scan_stamp}.")

    # Apply distance metrics and result dates to raw_df
    raw_df = add_distance_metrics(raw_df)
    raw_df = apply_spread_quality_filters(raw_df)
    events_dict = load_events_database()
    if "symbol" in raw_df.columns:
        raw_df["result_date"] = raw_df["symbol"].apply(lambda s: events_dict.get(str(s).upper(), "TBA"))

    numeric_cols = [
        "return_on_margin_pct", "credit_amount", "credit_per_share", "margin_required", "underlying_price",
        "near_strike", "far_strike", "strike_gap", "near_buy_ask", "far_sell_bid",
        "near_dist_pct", "far_dist_pct", "margin_age_sec", "scan_duration_ms",
    ]
    for col in numeric_cols:
        if col in raw_df.columns:
            raw_df[col] = pd.to_numeric(raw_df[col], errors="coerce")

    # 2. Filtered view for the main table (based on sidebar filters)
    filtered = apply_ce_pe_filter(raw_df)
    st.sidebar.markdown(f"### {target_name} Filters")
    min_return = st.sidebar.number_input(f"{target_name} Min Return on Margin %", value=0.0, step=0.1, key=f"min_ret_{ratio_val}_{target_name}")
    
    if "return_on_margin_pct" in filtered.columns:
        filtered = filtered[filtered["return_on_margin_pct"].fillna(0) >= min_return]
        filtered = filtered.sort_values("return_on_margin_pct", ascending=False)

    m1, m2, m3, m4 = st.columns(4)
    best_return = filtered['return_on_margin_pct'].max() if not filtered.empty else 0.0
    max_credit = filtered['credit_amount'].max() if not filtered.empty else 0.0
    expiry = str(filtered["expiry_date"].iloc[0]) if "expiry_date" in filtered.columns and not filtered.empty else "-"
    
    m1.metric("Opportunities", len(filtered))
    m2.metric("Best ROM %", f"{best_return:.2f}")
    m3.metric("Max Credit", f"{max_credit:,.0f}")
    m4.metric("Expiry", expiry)

    show_cols = [c for c in [
        "symbol", "result_date", "option_type", 
        "return_on_margin_pct", "credit_amount", "credit_per_share", 
        "underlying_price", "near_strike", "far_strike", 
        "near_buy_ask", "far_sell_bid", 
        "near_dist_pct", "far_dist_pct", 
        "strike_gap",
        "near_ask_qty", "far_bid_qty", "near_oi", "far_oi", 
        "margin_required", "margin_status", "margin_age_sec", "scan_time",
    ] if c in filtered.columns]

    rename_map = {
        "symbol": "Symbol",
        "result_date": "Result Date",
        "option_type": "Type",
        "return_on_margin_pct": "ROM %",
        "credit_amount": "Credit ₹",
        "credit_per_share": "Credit\n₹/Share",
        "underlying_price": "Spot/\nFut",
        "near_strike": "Near Strike\n(Buy)",
        "far_strike": "Far Strike\n(Sell)",
        "near_buy_ask": "Near Ask\n(Buy)",
        "far_sell_bid": "Far Bid\n(Sell)",
        "near_dist_pct": "Near\nDist %",
        "far_dist_pct": "Far\nDist %",
        "strike_gap": "Strike\nGap",
        "near_ask_qty": "Near Ask Qty",
        "far_bid_qty": "Far Bid Qty",
        "near_oi": "Near OI",
        "far_oi": "Far OI",
        "margin_required": "Margin\nRequired ₹",
        "margin_status": "Margin\nStatus",
        "margin_age_sec": "Age\n(s)",
        "scan_time": "Scan Time"
    }

    display_df = filtered[show_cols].rename(columns=rename_map)
    if "Symbol" in display_df.columns:
        display_df["Symbol"] = display_df["Symbol"].astype(str).apply(
            lambda s: f"/?view=stock&symbol={s}")

    styled = display_df.style.format({
        "ROM %": "{:.2f}%",
        "Credit ₹": "₹{:,.0f}",
        "Credit\n₹/Share": "₹{:.2f}",
        "Spot/\nFut": "{:.2f}",
        "Near Strike\n(Buy)": "{:.0f}",
        "Far Strike\n(Sell)": "{:.0f}",
        "Near Ask\n(Buy)": "{:.2f}",
        "Far Bid\n(Sell)": "{:.2f}",
        "Near\nDist %": "{:+.2f}%",
        "Far\nDist %": "{:+.2f}%",
        "Strike\nGap": "{:.0f}",
        "Margin\nRequired ₹": "₹{:,.0f}",
        "Age\n(s)": "{:.1f}s",
    })
    
    styled = style_map_available(styled, highlight_rom, ["ROM %"])
    styled = style_map_available(styled, result_cell_css, ["Result Date"])
    if hasattr(styled, "apply"):
        styled = styled.apply(highlight_ban_rows, axis=1)

    st.dataframe(
        styled, width="stretch", height=720, hide_index=True,
        column_config={"Symbol": st.column_config.LinkColumn(
            "Symbol", display_text=r"symbol=([^&]+)",
            help="Click to open this stock's strike parity & all combinations")},
    )

    # --- STOCK STRIKE PARITY & ALL COMBINATIONS DRILL-DOWN WINDOW (USING UNFILTERED RAW DF) ---
    st.markdown("---")
    st.subheader(f"🔍 Stock Strike Parity & All Combination Drill-Down ({target_name})")
    
    # Extract symbols from raw_df so ALL scanned stocks are available in the dropdown
    all_symbols_in_raw = sorted(raw_df["symbol"].dropna().unique().tolist()) if "symbol" in raw_df.columns and not raw_df.empty else []
    
    if all_symbols_in_raw:
        selected_drill_sym = st.selectbox(
            "Select Stock to Inspect All Strike Combinations & Parity", 
            ["-- Select Stock --"] + all_symbols_in_raw, 
            key=f"drill_{ratio_val}_{status_key_prefix}_{is_next_month}"
        )
        if selected_drill_sym != "-- Select Stock --":
            with st.expander(f"📌 Full Strike Parity & All Combinations for `{selected_drill_sym}` (Ratio 1:{ratio_val})", expanded=True):
                # Pulls ALL scanned combinations for this symbol from raw_df irrespective of minimum ROM / credit/debit filters
                stock_combos = raw_df[raw_df["symbol"].astype(str).str.upper() == selected_drill_sym.upper()]
                if not stock_combos.empty:
                    drill_show_cols = [c for c in [
                        "result_date", "option_type", "return_on_margin_pct", "credit_amount", "credit_per_share",
                        "underlying_price", "near_strike", "far_strike", "strike_gap", "near_buy_ask", "far_sell_bid",
                        "near_dist_pct", "far_dist_pct", "margin_required"
                    ] if c in stock_combos.columns]
                    
                    drill_rename_map = {
                        "result_date": "Result Date", "option_type": "Type", "return_on_margin_pct": "ROM %",
                        "credit_amount": "Credit ₹", "credit_per_share": "Credit ₹/Share", "underlying_price": "Spot/Fut",
                        "near_strike": "Near Strike (Buy)", "far_strike": "Far Strike (Sell)", "strike_gap": "Strike Gap",
                        "near_buy_ask": "Near Ask", "far_sell_bid": "Far Bid", "near_dist_pct": "Near Dist %",
                        "far_dist_pct": "Far Dist %", "margin_required": "Margin Req ₹"
                    }
                    drill_df = stock_combos[drill_show_cols].rename(columns=drill_rename_map)
                    st.dataframe(drill_df.style.format({
                        "ROM %": "{:.2f}%", "Credit ₹": "₹{:,.0f}", "Credit ₹/Share": "₹{:.2f}",
                        "Spot/Fut": "{:.2f}", "Near Strike (Buy)": "{:.0f}", "Far Strike (Sell)": "{:.0f}",
                        "Near Ask": "{:.2f}", "Far Bid": "{:.2f}", "Near Dist %": "{:+.2f}%",
                        "Far Dist %": "{:+.2f}%", "Margin Req ₹": "₹{:,.0f}"
                    }), width="stretch", hide_index=True)
                else:
                    st.info(f"No combination records found for {selected_drill_sym}.")

    csv_bytes = filtered.to_csv(index=False).encode('utf-8')
    st.download_button(
        label=f"📥 Download {target_name} Opportunities CSV",
        data=csv_bytes,
        file_name=f"{target_name.lower().replace(' ', '_')}_opportunities.csv",
        mime="text/csv"
    )

def render_parallel_ratio():
    target_name = "Parallel Incremental"
    
    df, err = safe_read_csv(PARALLEL_CSV_PATH)
    if err or df is None or df.empty:
        st.info("No saved parallel opportunities yet. The table fills after the scanner finds some during market hours.")
        df = _empty_ratio_frame()
    else:
        st.info(f"Displaying saved {target_name} opportunities.")

    df = apply_ce_pe_filter(df)
    df = add_distance_metrics(df)
    df = apply_spread_quality_filters(df)

    events_dict = load_events_database()
    if "symbol" in df.columns:
        df["result_date"] = df["symbol"].apply(lambda s: events_dict.get(str(s).upper(), "TBA"))

    numeric_cols = [
        "incremental_return_on_margin_pct", "incremental_amount", "incremental_per_share", 
        "margin_required", "parallel_return_on_margin_pct", "parallel_credit_amount",
        "near_dist_pct", "far_dist_pct"
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    st.sidebar.markdown(f"### {target_name} Filters")
    min_inc_rom = st.sidebar.number_input(f"Min Incremental ROM % ({target_name})", value=0.0, step=0.05, key="min_inc_rom_parallel")
    filtered = df
    if "incremental_return_on_margin_pct" in filtered.columns:
        filtered = filtered[filtered["incremental_return_on_margin_pct"].fillna(0) >= min_inc_rom]
        filtered = filtered.sort_values("incremental_return_on_margin_pct", ascending=False)

    if "margin_status" in filtered.columns and not filtered.empty:
        mc = filtered["margin_status"].value_counts()
        st.caption(
            f"Ranked by Inc. ROM %. Margin basis: {int(mc.get('LIVE', 0))} live · "
            f"{int(mc.get('CACHE', 0))} cached · {int(mc.get('STALE', 0))} last-known (up to 3 min old)."
        )
    m1, m2, m3 = st.columns(3)
    m1.metric("Opportunities", len(filtered))
    m2.metric("Best Incremental ROM %", f"{filtered['incremental_return_on_margin_pct'].max() if not filtered.empty else 0.0:.2f}")
    m3.metric("Best Incremental ₹", f"{filtered['incremental_amount'].max() if not filtered.empty else 0.0:,.0f}")
    
    show_cols = [c for c in [
        "symbol", "result_date", "option_type",
        "parallel_return_on_margin_pct", "parallel_credit_amount",
        "incremental_return_on_margin_pct", "incremental_amount",
        "underlying_price", "near_strike", "parallel_far_strike",
        "near_buy_ask", "parallel_far_sell_bid",
        "near_dist_pct", "far_dist_pct",
        "incremental_per_share", 
        "normal_far_strike", "normal_far_sell_bid",
        "parallel_gap", "normal_gap", "strike_step",
        "atm_curve_premium_per_share", "actual_parallel_credit_per_share",
        "atm_near_ask", "atm_parallel_far_bid",
        "parallel_credit_per_share", "normal_credit_per_share",
        "margin_required", "margin_status", "margin_age_sec", "scan_time",
    ] if c in filtered.columns]

    rename_map = {
        "symbol": "Symbol",
        "result_date": "Result Date",
        "option_type": "Type",
        "parallel_return_on_margin_pct": "Total\nROM %",
        "parallel_credit_amount": "Total\nCredit ₹",
        "incremental_return_on_margin_pct": "Inc.\nROM %",
        "incremental_amount": "Inc.\nAmt ₹",
        "incremental_per_share": "Inc.\n₹/Sh",
        "underlying_price": "Spot/\nFut",
        "near_strike": "Near Strike\n(Buy)",
        "parallel_far_strike": "Parallel Far\n(Sell)",
        "near_buy_ask": "Near Ask\n(Buy)",
        "parallel_far_sell_bid": "Parallel Bid\n(Sell)",
        "near_dist_pct": "Near\nDist %",
        "far_dist_pct": "Far\nDist %",
        "normal_far_strike": "Normal Far",
        "normal_far_sell_bid": "Normal Bid",
        "parallel_gap": "Parallel\nGap",
        "normal_gap": "Normal\nGap",
        "strike_step": "Strike\nStep",
        "atm_curve_premium_per_share": "ATM Base\n₹/Sh",
        "actual_parallel_credit_per_share": "Actual\nCr. ₹/Sh",
        "atm_near_ask": "ATM\nNear Ask (Buy)",
        "atm_parallel_far_bid": "ATM\nFar Bid (Sell)",
        "parallel_credit_per_share": "Parallel\nCr./Sh",
        "normal_credit_per_share": "Normal\nCr./Sh",
        "margin_required": "Margin\nRequired ₹",
        "margin_status": "Margin\nStatus",
        "margin_age_sec": "Age\n(s)",
        "scan_time": "Scan Time"
    }

    display_df = filtered[show_cols].rename(columns=rename_map)

    styled = display_df.style.format({
        "Total\nROM %": "{:.2f}%",
        "Total\nCredit ₹": "₹{:,.0f}",
        "Inc.\nROM %": "{:.3f}%",
        "Inc.\nAmt ₹": "₹{:,.0f}",
        "Inc.\n₹/Sh": "₹{:.2f}",
        "Spot/\nFut": "{:.2f}",
        "Near Strike\n(Buy)": "{:.0f}",
        "Parallel Far\n(Sell)": "{:.0f}",
        "Near Ask\n(Buy)": "{:.2f}",
        "Parallel Bid\n(Sell)": "{:.2f}",
        "Near\nDist %": "{:+.2f}%",
        "Far\nDist %": "{:+.2f}%",
        "Normal Far": "{:.0f}",
        "Normal Bid": "{:.2f}",
        "Parallel\nGap": "{:.0f}",
        "Normal\nGap": "{:.0f}",
        "Strike\nStep": "{:.0f}",
        "ATM Base\n₹/Sh": "₹{:.2f}",
        "Actual\nCr. ₹/Sh": "₹{:.2f}",
        "ATM\nNear Ask (Buy)": "{:.2f}",
        "ATM\nFar Bid (Sell)": "{:.2f}",
        "Parallel\nCr./Sh": "{:.2f}",
        "Normal\nCr./Sh": "{:.2f}",
        "Margin\nRequired ₹": "₹{:,.0f}",
        "Age\n(s)": "{:.1f}s",
    })
    
    styled = style_map_available(styled, highlight_rom, ["Total\nROM %", "Inc.\nROM %"])
    if hasattr(styled, "apply"):
        styled = styled.apply(highlight_ban_rows, axis=1)

    st.dataframe(styled, width="stretch", height=720, hide_index=True)

@st.cache_data(ttl=600)
def fetch_live_exchange_lot_sizes(token_key: str = ""):
    lot_sizes_map = {}
    try:
        kite = make_kite()
        instruments = kite.instruments("NFO")
        for inst in instruments:
            name = str(inst.get("name", "")).strip().upper()
            lot = int(inst.get("lot_size", 0))
            if name and lot > 0:
                lot_sizes_map[name] = lot
    except Exception:
        pass
    return lot_sizes_map

def fetch_live_portfolio_margins(df_port):
    margins_by_symbol = {}
    try:
        kite = make_kite()
        
        for symbol, group in df_port.groupby("Symbol"):
            basket = []
            for _, r in group.iterrows():
                net_qty = float(r.get("NetQty", 0))
                if net_qty == 0:
                    continue
                
                tradingsymbol = str(r.get("ScripName", "")).strip().replace(" ", "").upper()
                if not tradingsymbol or tradingsymbol == "NAN":
                    tradingsymbol = str(r.get("Symbol", "")).strip().replace(" ", "").upper()
                if not tradingsymbol:
                    continue
                
                trans_type = "BUY" if net_qty > 0 else "SELL"
                qty = int(abs(net_qty))
                
                exchange = "NFO"
                if "SENSEX" in tradingsymbol or "BANKEX" in tradingsymbol:
                    exchange = "BFO"
                elif str(r.get("Exchange", "")).strip().upper() == "BSE":
                    exchange = "BSE"
                
                if "EQ" in str(r.get("InstrumentName", "")).upper() and exchange != "BSE":
                    exchange = "NSE"

                product = str(r.get("ProductType", "NRML")).strip().upper()
                if product in ["DELIVERY", "CNC", "MIS"]:
                    product = "NRML"

                basket.append({
                    "exchange": exchange,
                    "tradingsymbol": tradingsymbol,
                    "transaction_type": trans_type,
                    "variety": "regular",
                    "product": product,
                    "order_type": "MARKET",
                    "quantity": qty,
                })
            
            if basket:
                try:
                    m = kite.basket_order_margins(basket, consider_positions=False, mode="compact")
                    total_margin = 0.0
                    if isinstance(m, dict):
                        if "final" in m and isinstance(m["final"], dict):
                            total_margin = float(m["final"].get("total") or 0)
                        else:
                            total_margin = float(m.get("total") or 0)
                    else:
                        total_margin = float(m)
                    
                    if total_margin > 0:
                        margins_by_symbol[str(symbol).upper()] = total_margin
                except Exception:
                    pass
                
                time.sleep(0.15)
                
    except Exception:
        pass
    return margins_by_symbol

def render_portfolio_churning_tab():
    st.header("📊 Portfolio Margin, Capital Churning & Position Intelligence")
    st.markdown("Upload your open positions trade sheet Excel to track multi-leg portfolio margins, fund-size concentration, and position intelligence matching Zerodha Zerovega style.")

    uploaded_file = st.file_uploader("Upload Open Trades Excel", type=["xlsx", "xls"])
    
    # Keep the sheet in this browser session (not a shared file on a public server)
    if uploaded_file is not None:
        st.session_state["portfolio_xlsx_bytes"] = uploaded_file.getvalue()
        st.success("File loaded. It stays loaded while you switch tabs in this session.")

    _xlsx_bytes = st.session_state.get("portfolio_xlsx_bytes")
    target_source = io.BytesIO(_xlsx_bytes) if _xlsx_bytes else None
    
    if target_source is None:
        st.info("Please upload your Excel trade sheet above.")
        return

    try:
        xl_file = pd.ExcelFile(target_source)
        sheet_to_use = xl_file.sheet_names[0] if xl_file.sheet_names else 0
        for sh in xl_file.sheet_names:
            if str(sh).lower() in ["stocks", "sheet1", "positions", "net"]:
                sheet_to_use = sh
                break

        df_port = xl_file.parse(sheet_name=sheet_to_use)
        df_port.columns = [str(c).strip() for c in df_port.columns]
        
        _syms = set()
        for _c in df_port.columns:
            if _c.lower() in ["symbol", "stock", "underlying"]:
                _syms.update(df_port[_c].dropna().astype(str).str.strip().str.upper().tolist())
        _syms = sorted(s_ for s_ in _syms if s_ and s_ != "NAN")
        if _syms:
            os.makedirs(RATIO_DIR, exist_ok=True)
            with open(PORTFOLIO_SYMBOLS_PATH, "w", encoding="utf-8") as _f:
                json.dump(_syms, _f)

        rename_map = {}
        for col in df_port.columns:
            col_lower = col.lower().strip()
            if col_lower in ["symbol", "stock"]:
                rename_map[col] = "Symbol"
            elif col_lower in ["scripname", "tradingsymbol", "scrip"]:
                rename_map[col] = "ScripName"
            elif col_lower in ["buyval", "buyvalue", "buy_val"]:
                rename_map[col] = "BuyVal"
            elif col_lower in ["sellval", "sellvalue", "sell_val"]:
                rename_map[col] = "SellVal"
            elif col_lower in ["mtmg_l", "mtm", "unrealised_mtm", "pnl"]:
                rename_map[col] = "MTMG_L"
            elif col_lower in ["netval", "netvalue", "expected_value"]:
                rename_map[col] = "NetVal"
            elif col_lower in ["ser_exp", "expiry", "expdate"]:
                rename_map[col] = "Ser_Exp"
            elif col_lower in ["netqty", "net_qty", "quantity", "qty"]:
                rename_map[col] = "NetQty"
            elif col_lower in ["marketprice", "ltp", "price"]:
                rename_map[col] = "MarketPrice"
            elif col_lower in ["strikeprice", "strike"]:
                rename_map[col] = "StrikePrice"
            elif col_lower in ["lotsize", "lot_size"]:
                rename_map[col] = "LotSize"

        if rename_map:
            df_port = df_port.rename(columns=rename_map)

        if "Symbol" not in df_port.columns and "ScripName" in df_port.columns:
            df_port["Symbol"] = df_port["ScripName"]
        elif "ScripName" not in df_port.columns and "Symbol" in df_port.columns:
            df_port["ScripName"] = df_port["Symbol"]

        if "Symbol" not in df_port.columns:
            text_cols = df_port.select_dtypes(include=['object']).columns
            if len(text_cols) > 0:
                df_port["Symbol"] = df_port[text_cols[0]]
            else:
                st.error("Error: Could not find a 'Symbol' or stock identifier column in the uploaded Excel file.")
                return

        numeric_portfolio_cols = ["BuyVal", "SellVal", "MTMG_L", "NetVal", "NetQty", "MarketPrice", "StrikePrice", "BuyQty", "SellQty", "LotSize"]
        for col in numeric_portfolio_cols:
            if col not in df_port.columns:
                df_port[col] = 0.0
            else:
                df_port[col] = pd.to_numeric(df_port[col], errors="coerce").fillna(0.0)

        if "Ser_Exp" not in df_port.columns:
            df_port["Ser_Exp"] = "-"

        df_port = df_port.dropna(subset=["Symbol"]).copy()
    except Exception as e:
        st.error(f"Could not read Excel file: {e}")
        return

    df_port = df_port[df_port["NetQty"] != 0].copy()
    net_qty_per_symbol = df_port.groupby("Symbol")["NetQty"].sum()
    active_symbols = net_qty_per_symbol[net_qty_per_symbol != 0].index.tolist()
    df_port = df_port[df_port["Symbol"].isin(active_symbols)].copy()

    df_port["MarketExitVal"] = df_port["NetQty"] * df_port["MarketPrice"]
    
    # Hash to detect changes and prevent stagnant margin data
    port_state_str = "".join(df_port["Symbol"].astype(str) + df_port["NetQty"].astype(str) + df_port["StrikePrice"].astype(str))
    port_hash = hash(port_state_str)
    
    if st.session_state.get("port_hash") != port_hash:
        st.session_state["live_margins_dict"] = {}
        st.session_state["port_hash"] = port_hash

    live_margins_dict = st.session_state.get("live_margins_dict", {})
    exchange_lot_sizes = fetch_live_exchange_lot_sizes(SERVICE.token_fingerprint())

    st.markdown("---")
    col_btn1, col_btn2 = st.columns([1, 3])
    with col_btn1:
        force_refresh = st.button("🔌 Force Refresh Live Kite Margins", use_container_width=True)
        
    # Auto-fetch live margins if empty (or file changed) or if user clicks force refresh
    if not live_margins_dict or force_refresh:
        with st.spinner("Querying exact Kite API margins (pacing requests to avoid rate limits)..."):
            live_margins_dict = fetch_live_portfolio_margins(df_port)
            st.session_state["live_margins_dict"] = live_margins_dict

    summary = df_port.groupby("Symbol").agg({
        "BuyVal": "sum",
        "SellVal": "sum",
        "MTMG_L": "sum",
        "NetVal": "sum",
        "Ser_Exp": "first",
        "LotSize": "first"
    }).reset_index()

    summary["Sector"] = summary["Symbol"].apply(get_stock_sector)
    events_dict = load_events_database()
    
    # Return "-" for Indices, else lookup TBA status
    summary["Upcoming Result Date"] = summary.apply(
        lambda row: "-" if row["Sector"] == "Index" else events_dict.get(str(row["Symbol"]).upper(), "TBA"), axis=1
    )

    today = pd.to_datetime("today").normalize()
    summary["Expiry Date"] = pd.to_datetime(summary["Ser_Exp"], errors="coerce")
    summary["DTE"] = (summary["Expiry Date"] - today).dt.days

    summary["Net Credit (₹)"] = summary["SellVal"] - summary["BuyVal"]
    summary["Current MTM (₹)"] = summary["MTMG_L"]
    summary["Total Expected Profit (₹)"] = summary["NetVal"].abs()
    summary["Remaining Profit (₹)"] = (summary["Total Expected Profit (₹)"] - summary["Current MTM (₹)"]).clip(lower=0.0)

    market_exit_val_per_symbol = df_port.groupby("Symbol")["MarketExitVal"].sum()
    summary["Market Exit Cost (₹)"] = summary["Symbol"].map(market_exit_val_per_symbol).fillna(0.0)
    
    def calc_debit_excess(row):
        expected_net_val = abs(row["NetVal"])
        mtm = row["Current MTM (₹)"]
        total_realized_plus_remaining = mtm + row["Remaining Profit (₹)"]
        excess = total_realized_plus_remaining - expected_net_val
        return max(0.0, excess)

    summary["Debit Exit Excess (₹)"] = summary.apply(calc_debit_excess, axis=1)
    summary["Remaining Rs (Decay/Profit)"] = summary["Remaining Profit (₹)"]

    def assign_margin(row):
        sym_upper = str(row["Symbol"]).upper()
        if sym_upper in live_margins_dict:
            return live_margins_dict[sym_upper]
            
        # If for any reason it wasn't pre-fetched, fetch it dynamically NOW from Kite API
        group_legs = df_port[df_port["Symbol"].astype(str).str.upper() == sym_upper]
        basket = []
        try:
            kite = make_kite()
            for _, r in group_legs.iterrows():
                net_qty = float(r.get("NetQty", 0))
                if net_qty == 0: continue
                tradingsymbol = str(r.get("ScripName", "")).strip().replace(" ", "").upper()
                if not tradingsymbol or tradingsymbol == "NAN":
                    tradingsymbol = str(r.get("Symbol", "")).strip().replace(" ", "").upper()
                
                trans_type = "BUY" if net_qty > 0 else "SELL"
                qty = int(abs(net_qty))
                
                exchange = "NFO"
                if "SENSEX" in tradingsymbol or "BANKEX" in tradingsymbol:
                    exchange = "BFO"
                elif str(r.get("Exchange", "")).strip().upper() == "BSE":
                    exchange = "BSE"
                
                if "EQ" in str(r.get("InstrumentName", "")).upper() and exchange != "BSE":
                    exchange = "NSE"
                    
                product = str(r.get("ProductType", "NRML")).strip().upper()
                if product in ["DELIVERY", "CNC", "MIS"]: product = "NRML"
                
                basket.append({
                    "exchange": exchange,
                    "tradingsymbol": tradingsymbol,
                    "transaction_type": trans_type,
                    "variety": "regular",
                    "product": product,
                    "order_type": "MARKET",
                    "quantity": qty,
                })
            
            if basket:
                for _ in range(3): # Retry up to 3 times for rate limits
                    try:
                        m = kite.basket_order_margins(basket, consider_positions=False, mode="compact")
                        total_margin = float(m["final"].get("total") if isinstance(m, dict) and "final" in m else m.get("total", 0))
                        if total_margin > 0:
                            live_margins_dict[sym_upper] = total_margin
                            st.session_state["live_margins_dict"] = live_margins_dict
                            return total_margin
                    except Exception:
                        time.sleep(0.3)
        except Exception:
            pass
            
        # If Kite API absolutely fails after retries, fallback to 0 to avoid inaccurate static data
        return 0.0

    summary["Est. Margin Allocated (₹)"] = summary.apply(assign_margin, axis=1)
    total_deployed_capital = summary["Est. Margin Allocated (₹)"].sum()
    
    summary["Fund Size %"] = (summary["Est. Margin Allocated (₹)"] / total_permitted_fund) * 100.0
    summary["Remaining ROI %"] = (summary["Remaining Profit (₹)"] / summary["Est. Margin Allocated (₹)"]).replace([float('inf'), float('-inf')], 0).fillna(0) * 100.0
    summary["Est. Daily Theta (₹/Day)"] = summary.apply(
        lambda row: row["Remaining Profit (₹)"] / max(1, row["DTE"]) if row["DTE"] > 0 else 0, axis=1
    )
    summary["Risk Status"] = summary["Fund Size %"].apply(
        lambda x: "🔴 RISKY (Over-Allocated)" if x > risky_threshold_pct else "🟢 Safe"
    )

    summary = summary.sort_values("Remaining ROI %", ascending=True)
    overall_utilization_pct = (total_deployed_capital / total_permitted_fund) * 100.0
    total_daily_theta = summary["Est. Daily Theta (₹/Day)"].sum()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Active Symbols", len(summary))
    m2.metric("Total Deployed Capital", f"₹{total_deployed_capital:,.0f}", f"{overall_utilization_pct:.1f}% Utilized")
    m3.metric("Est. Daily Theta (Decay)", f"₹{total_daily_theta:,.0f} / Day", "Time-Decay Velocity")
    m4.metric("Total MTM P&L", f"₹{summary['Current MTM (₹)'].sum():,.0f}")

    st.markdown("---")
    st.subheader("💡 Smart Churning & Reallocation Recommendation")
    
    if not summary.empty:
        worst_pos = summary.iloc[0]
        worst_sym = worst_pos["Symbol"]
        worst_roi = worst_pos["Remaining ROI %"]
        freed_margin = worst_pos["Est. Margin Allocated (₹)"]

        active_portfolio_symbols = summary["Symbol"].astype(str).str.upper().tolist()
        scan_df, scan_err = safe_read_csv(RATIO_CSV_PATH)
        
        if scan_err or scan_df is None or scan_df.empty:
            scan_df = get_preview_dataframe(4.0, is_parallel=False)

        available_scans = scan_df[~scan_df["symbol"].astype(str).str.upper().isin(active_portfolio_symbols)]
        
        if not available_scans.empty and "return_on_margin_pct" in available_scans.columns:
            best_new_opp = available_scans.sort_values("return_on_margin_pct", ascending=False).iloc[0]
            new_sym = best_new_opp.get("symbol", "-")
            new_rom = best_new_opp.get("return_on_margin_pct", 0.0)
            new_expiry = best_new_opp.get("expiry_date", "-")

            st.info(
                f"🎯 **Churn Recommendation**: Square off **{worst_sym}** (Remaining ROI: **{worst_roi:.2f}%**) "
                f"to release approx **₹{freed_margin:,.0f}**, and reallocate capital into fresh scan opportunity **{new_sym}** "
                f"(Scanner ROM: **{new_rom:.2f}%**, Expiry: {new_expiry}), which is **not currently in your portfolio**."
            )
        else:
            st.info(f"🎯 **Churn Recommendation**: Square off **{worst_sym}** (Remaining ROI: **{worst_roi:.2f}%**) to release approx **₹{freed_margin:,.0f}** for new deployment.")

    st.markdown("---")
    risky_count = len(summary[summary["Fund Size %"] > risky_threshold_pct])
    if risky_count > 0:
        st.warning(f"⚠️ **Risk Exposure Alert**: You have **{risky_count} stock(s)** exceeding your permitted risk threshold of {risky_threshold_pct}% of total fund size.")
    else:
        st.success(f"✅ **Portfolio Risk Check Passed**: All active stock allocations are safely below your {risky_threshold_pct}% threshold.")

    st.subheader("Per-Stock Capital Allocation, Position Intelligence & Theta Velocity Table")

    def format_dte(val):
        try:
            d = int(val)
            if d < 7:
                return f"⚠️ {d}d (Gamma Zone)"
            return f"{d}d"
        except Exception:
            return "-"

    summary["DTE_Display"] = summary["DTE"].apply(format_dte)

    enriched_rows = []
    for _, row in summary.iterrows():
        sym = str(row["Symbol"]).strip().upper()
        group_legs = df_port[df_port["Symbol"].astype(str).str.upper() == sym]
        
        buy_strikes = group_legs[group_legs["NetQty"] > 0]["StrikePrice"].tolist()
        sell_strikes = group_legs[group_legs["NetQty"] < 0]["StrikePrice"].tolist() if "NetQty" in group_legs.columns else []
        
        buy_strike_str = str(int(buy_strikes[0])) if len(buy_strikes) == 1 and buy_strikes[0] > 0 else ("Combined" if buy_strikes else "-")
        sell_strike_str = str(int(sell_strikes[0])) if len(sell_strikes) == 1 and sell_strikes[0] > 0 else ("Combined" if sell_strikes else "-")
        
        diff_val = (sell_strikes[0] - buy_strikes[0]) if (len(buy_strikes) == 1 and len(sell_strikes) == 1) else 0.0
        
        total_margin_val = summary["Est. Margin Allocated (₹)"].sum() or 1.0
        roi_impact_val = (row["Current MTM (₹)"] / total_margin_val) * 100.0

        raw_lot_size = exchange_lot_sizes.get(sym, 0)
        if raw_lot_size <= 0:
            raw_lot_size = row["LotSize"] if "LotSize" in row and pd.notna(row["LotSize"]) and row["LotSize"] > 0 else 0
        if raw_lot_size <= 0:
            fallback_mapping = {"NIFTY": 25, "BANKNIFTY": 15, "RELIANCE": 250, "SBIN": 750, "HDFCBANK": 550, "TRENT": 250, "M&M": 350, "TATAMOTORS": 500, "MARUTI": 100}
            raw_lot_size = fallback_mapping.get(sym, 1)

        buy_legs = group_legs[group_legs["NetQty"] > 0]
        qty_buy_val = int(buy_legs["NetQty"].sum()) if not buy_legs.empty else int(abs(group_legs["NetQty"].sum()))
        calculated_lots = round(qty_buy_val / raw_lot_size, 1) if raw_lot_size > 0 else 0.0

        enriched_rows.append({
            "Off": False,
            "Symbol": sym,
            "Debit Exit Excess (₹)": row["Debit Exit Excess (₹)"],
            "Remaining ROI %": row["Remaining ROI %"],
            "Est. Margin Allocated (₹)": row["Est. Margin Allocated (₹)"],
            "Lot(s)": calculated_lots,
            "Buy Strike": buy_strike_str,
            "Sell Strike": sell_strike_str,
            "Difference": int(diff_val) if diff_val != 0 else "-",
            "Exit Amount": round(group_legs["MarketExitVal"].sum(), 2),
            "ROI Impact %": round(roi_impact_val, 2),
            "Bid": "-",
            "Ask": "-",
            "Spread": "-",
            "Sector": row["Sector"],
            "Remaining Rs (Decay/Profit)": row["Remaining Rs (Decay/Profit)"],
            "Risk Status": row["Risk Status"],
            "Fund Size %": row["Fund Size %"],
            "Theta ₹/Day": row["Est. Daily Theta (₹/Day)"],
            "DTE (Expiry)": row["DTE_Display"],
            "Result Date": row["Upcoming Result Date"],
            "Remaining Profit (₹)": row["Remaining Profit (₹)"],
            "Net Credit (₹)": row["Net Credit (₹)"],
            "Current MTM (₹)": row["Current MTM (₹)"],
            "Buy Value (₹)": row["BuyVal"],
            "Sell Value (₹)": row["SellVal"],
            "Total Expected Profit (₹)": row["Total Expected Profit (₹)"]
        })

    display_summary = pd.DataFrame(enriched_rows)

    styled_port = display_summary.style.format({
        "Remaining ROI %": "{:.2f}%", "Debit Exit Excess (₹)": "+₹{:,.0f}",
        "Remaining Rs (Decay/Profit)": "₹{:,.0f}", "Fund Size %": "{:.2f}%",
        "Theta ₹/Day": "₹{:,.0f}", "Remaining Profit (₹)": "₹{:,.0f}",
        "Net Credit (₹)": "₹{:,.0f}", "Current MTM (₹)": "₹{:,.0f}",
        "Buy Value (₹)": "₹{:,.0f}", "Sell Value (₹)": "₹{:,.0f}",
        "Est. Margin Allocated (₹)": "₹{:,.0f}", "Total Expected Profit (₹)": "₹{:,.0f}",
        "Exit Amount": "₹{:,.2f}", "ROI Impact %": "{:+.2f}%", "Lot(s)": "{:.1f}",
    })
    
    styled_port = style_map_available(styled_port, highlight_remaining_rs, ["Remaining Rs (Decay/Profit)"])
    styled_port = style_map_available(styled_port, highlight_debit_excess, ["Debit Exit Excess (₹)"])
    if hasattr(styled_port, "apply"):
        styled_port = styled_port.apply(highlight_ban_rows, axis=1)

    st.dataframe(styled_port, width="stretch", height=600, hide_index=True)

    port_csv = summary.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="📥 Download Portfolio Churning Report CSV",
        data=port_csv,
        file_name="portfolio_churning_report.csv",
        mime="text/csv"
    )

    st.markdown("---")
    st.subheader("🌐 Sector-Level Exposure & Diversification Analysis")
    sector_summary = summary.groupby("Sector")["Est. Margin Allocated (₹)"].sum().reset_index()
    sector_summary["Sector Fund %"] = (sector_summary["Est. Margin Allocated (₹)"] / total_permitted_fund) * 100.0
    sector_summary = sector_summary.sort_values("Sector Fund %", ascending=False)

    col_sec1, col_sec2 = st.columns([1.5, 1])
    with col_sec1:
        st.dataframe(
            sector_summary.rename(columns={"Est. Margin Allocated (₹)": "Margin Allocated (₹)", "Sector Fund %": "Fund %"}).style.format({
                "Margin Allocated (₹)": "₹{:,.0f}",
                "Fund %": "{:.2f}%"
            }),
            width="stretch", hide_index=True
        )
    with col_sec2:
        over_sectors = sector_summary[sector_summary["Sector Fund %"] > sector_limit_pct]
        if not over_sectors.empty:
            sec_names = ", ".join(over_sectors["Sector"].tolist())
            st.warning(f"⚠️ **Sector Over-Concentration Alert**: **{sec_names}** exceed your {sector_limit_pct}% sector limit!")
        else:
            st.success(f"✅ **Sector Diversification Passed**: All sectors are safely under your {sector_limit_pct}% ceiling.")

    st.markdown("---")
    st.subheader("📅 Expiry Clustering & Rollover Matrix")
    expiry_summary = summary.groupby("Expiry Date").agg(
        Symbols_Count=("Symbol", "count"),
        Total_Margin=("Est. Margin Allocated (₹)", "sum")
    ).reset_index()
    expiry_summary["Expiry Date Str"] = expiry_summary["Expiry Date"].dt.strftime("%Y-%m-%d").fillna("Unknown")
    expiry_summary["Capital %"] = (expiry_summary["Total_Margin"] / total_permitted_fund) * 100.0

    st.bar_chart(expiry_summary.set_index("Expiry Date Str")["Total_Margin"])
    st.markdown("### 📈 Capital Concentration Breakdown by Stock")
    st.bar_chart(summary.set_index("Symbol")["Est. Margin Allocated (₹)"])






def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    import time as _t
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    for attempt in range(10):          # Windows can briefly lock the file while another reader has it open
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            _t.sleep(0.2)
    with open(path, "w", encoding="utf-8") as f:   # last resort: write in place
        json.dump(obj, f, indent=1)


def _empty_alert_state():
    return {"price_history": {}, "alerts": [], "last_alert_time": {}}


def _ack_alert(alert_id):
    state = _load_json(ALERT_STATE_PATH, _empty_alert_state())
    for a in state["alerts"]:
        if a["id"] == alert_id:
            a["status"] = "done"
    _save_json(ALERT_STATE_PATH, state)


def process_portfolio_alerts(spike_pct):
    """Returns alerts created in this run. Alerts stay open until acknowledged."""
    portfolio = _load_json(PORTFOLIO_SYMBOLS_PATH, [])
    if not portfolio:
        return []
    spot_df, err = safe_read_csv(SPOT_PRICES_PATH)
    if err or spot_df is None or spot_df.empty:
        return []
    spots = {}
    for s_, p_ in zip(spot_df["Symbol"], spot_df["Spot"]):
        if pd.notna(p_) and float(p_) > 0:
            spots[str(s_).strip().upper()] = float(p_)

    state = _load_json(ALERT_STATE_PATH, _empty_alert_state())
    
    if "price_history" not in state:
        state["price_history"] = {}
    if "last_alert_time" not in state:
        state["last_alert_time"] = {}
        
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    state["alerts"] = [a for a in state["alerts"] if str(a.get("time", ""))[:10] == today or a.get("status") == "open"]
    changed, new_alerts = False, []

    now_epoch = time.time()
    window_sec = 3 * 60  # 3 minutes

    for sym in portfolio:
        if sym not in spots:
            continue
        spot = spots[sym]
        
        history = [h for h in state["price_history"].get(sym, [])
                   if isinstance(h, (list, tuple)) and len(h) == 2
                   and isinstance(h[0], (int, float))]  # skip bad saved entries
        history.append([now_epoch, spot])
        
        history = [h for h in history if now_epoch - h[0] <= window_sec]
        state["price_history"][sym] = history
        changed = True
        
        if len(history) < 2:
            continue
            
        oldest_price = history[0][1]
        move = (spot / oldest_price - 1.0) * 100.0
        
        if abs(move) >= spike_pct:
            last_alert_time = state["last_alert_time"].get(sym, 0)
            if now_epoch - last_alert_time > window_sec:
                alert = {
                    "id": f"{sym}-{int(now_epoch)}",
                    "symbol": sym,
                    "move_pct": round(move, 2),
                    "baseline": oldest_price,
                    "spot": spot,
                    "time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "open",
                }
                state["alerts"].append(alert)
                state["last_alert_time"][sym] = now_epoch
                new_alerts.append(alert)

    if changed:
        _save_json(ALERT_STATE_PATH, state)
    return new_alerts


def render_portfolio_alert_panel():
    state = _load_json(ALERT_STATE_PATH, _empty_alert_state())
    open_alerts = [a for a in state.get("alerts", []) if a.get("status") == "open"]
    if not open_alerts:
        return
    st.markdown("### 🚨 Portfolio stocks moving: check the spreads")
    for a in open_alerts:
        c1, c2 = st.columns([6, 1])
        direction = "up" if a["move_pct"] > 0 else "down"
        c1.error(
            f"**{a['symbol']}** {direction} {a['move_pct']:+.2f}% in the last 3 minutes "
            f"({a['baseline']:.2f} → {a['spot']:.2f}, scan {a['time']}). "
            f"IV and the spread curve may have changed. [Open detail](/?view=stock&symbol={a['symbol']})"
        )
        c2.button("✅ Acknowledge", key=f"ack_{a['id']}", on_click=_ack_alert, args=(a["id"],))


def _render_best_block(frames, title, top_n):
    """One 'best ratio per stock' table for a set of ratio frames (CE only, one row per stock)."""
    with st.expander(title, expanded=True):
        if not frames:
            st.info("No saved opportunities yet.")
            return
        allp = pd.concat(frames, ignore_index=True)

        if "option_type" in allp.columns:
            allp = allp[allp["option_type"].astype(str).str.upper() == "CE"]
        if allp.empty:
            st.info("No CE opportunities available to rank.")
            return

        allp["return_on_margin_pct"] = pd.to_numeric(allp["return_on_margin_pct"], errors="coerce")
        allp["symbol"] = allp["symbol"].astype(str).str.upper()
        best = (allp.sort_values("return_on_margin_pct", ascending=False)
                    .drop_duplicates("symbol").head(top_n).reset_index(drop=True))
        view = pd.DataFrame({
            "Symbol": best["symbol"],
            "Best ratio": best["Ratio"],
            "ROM %": best["return_on_margin_pct"],
            "Credit ₹": best["credit_amount"],
            "Strikes": [f"{n:.0f} → {f:.0f}" for n, f in zip(best["near_strike"], best["far_strike"])],
            "Buy leg dist %": [(n - sp) / sp * 100 if sp else None for n, sp in zip(best["near_strike"], best["underlying_price"])],
            "Sell leg dist %": [(f - sp) / sp * 100 if sp else None for f, sp in zip(best["far_strike"], best["underlying_price"])],
            "Result Date": best["result_date"],
            "Detail": [f"/?view=stock&symbol={s_}" for s_ in best["symbol"]],
        })
        styled = view.style.format({"ROM %": "{:.2f}%", "Credit ₹": "₹{:,.0f}",
                                    "Buy leg dist %": "{:+.2f}%", "Sell leg dist %": "{:+.2f}%"}, na_rep="-")
        styled = style_map_available(styled, result_cell_css, ["Result Date"])
        st.dataframe(styled, width="stretch", hide_index=True,
                     column_config={"Detail": st.column_config.LinkColumn("Detail", display_text=r"symbol=([^&]+)")})


def render_best_across_ratios(top_n=15):
    """Best ratio per stock, shown as two separate tables: current month and next month."""
    current_frames, next_frames = [], []
    for label, path, _ratio in DETAIL_SERIES:
        df, err = safe_read_csv(path)
        if err or df is None or df.empty or "symbol" not in df.columns or "return_on_margin_pct" not in df.columns:
            continue
        df = apply_spread_quality_filters(df)
        if df.empty:
            continue
        if label.startswith("Next Month"):
            next_frames.append(df.assign(Ratio=label))
        else:
            current_frames.append(df.assign(Ratio=label))

    _render_best_block(current_frames, f"🏆 Current month: best ratio per stock (top {top_n})", top_n)
    _render_best_block(next_frames, f"🏆 Next month: best ratio per stock (top {top_n})", top_n)


# ==============================================================================================
# CLOUD GLUE: Kite login, scanner controls, ban list, results calendar upload
# (replaces the .bat steps: nse_watcher_v2_robust.py / update_results_calendar.py inputs)
# ==============================================================================================
_DIALOG_STATE = {"open": False}   # Streamlit allows only one dialog per run


def handle_kite_redirect():
    """If Kite's redirect URL points at this app, finish the login automatically."""
    rt = st.query_params.get("request_token")
    if not rt:
        return
    secret = _secret("KITE_API_SECRET")
    msg = None
    if SERVICE.api_key and secret:
        try:
            data = KiteConnect(api_key=SERVICE.api_key).generate_session(str(rt), api_secret=secret)
            SERVICE.set_credentials(SERVICE.api_key, data["access_token"])
            msg = "✅ Kite login successful - scanner restarted with the new access token."
        except Exception as e:
            msg = f"Kite login failed: {e}"
    else:
        msg = "Kite redirect received, but KITE_API_KEY / KITE_API_SECRET are not set in secrets."
    st.session_state["kite_msg"] = msg
    for k in ("request_token", "action", "type", "status"):
        try:
            if k in st.query_params:
                del st.query_params[k]
        except Exception:
            pass


@st.cache_data(ttl=30)
def load_local_ban_list():
    banned_set = set()
    if os.path.exists(BAN_CSV_PATH):
        try:
            with open(BAN_CSV_PATH, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
                for line in lines[1:]:
                    parts = line.split(",")
                    sym = parts[0].replace('"', '').strip().upper()
                    if sym and not sym.isdigit() and sym not in ["SECURITY", "SYMBOL", "BAN"]:
                        banned_set.add(sym)
        except Exception:
            pass
    return banned_set


@st.cache_data(ttl=900, show_spinner=False)
def refresh_nse_ban_list() -> bool:
    """Best-effort NSE F&O ban list download. NSE sometimes blocks cloud IPs; then the previous
    file / the manual list in the sidebar is used instead."""
    try:
        r = requests.get(
            "https://nsearchives.nseindia.com/content/fo/fo_secban.csv",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                     "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"},
            timeout=10)
        lines = r.text.splitlines()
        if r.status_code != 200 or not lines or "ban" not in lines[0].lower():
            return False
        syms = []
        for line in lines[1:]:
            parts = [p.strip().strip('"') for p in line.split(",")]
            cand = (parts[1] if (len(parts) > 1 and parts[0].isdigit()) else parts[0]).upper()
            if cand and not cand.isdigit() and re.fullmatch(r"[A-Z0-9&\-]+", cand):
                syms.append(cand)
        atomic_overwrite_csv(BAN_CSV_PATH, pd.DataFrame({"Symbol": sorted(set(syms))}))
        load_local_ban_list.clear()
        return True
    except Exception:
        return False


def get_banned_symbols() -> set:
    refresh_nse_ban_list()
    banned = set(load_local_ban_list())
    manual = st.session_state.get("manual_ban_text", "")
    banned |= {x.strip().upper() for x in re.split(r"[,\s]+", manual) if x.strip()}
    return banned


def render_dist_filter_sidebar():
    """1ST LEG DISTANCE FILTER (sidebar, switch any time)."""
    st.sidebar.subheader("📏 1st Leg Distance from Spot")
    on = st.sidebar.checkbox(
        "Show only spreads whose 1st (buy) leg is far from spot",
        value=True, key="dist_filter_on",
        help="Keeps a spread only if its buy strike is at least the chosen % away from spot (either side).",
    )
    pct = st.sidebar.slider(
        "Minimum 1st-leg distance from spot (%)", 0.0, 15.0, 5.0, 0.5, key="min_leg_dist_pct",
    )
    st.sidebar.caption("Always on: spreads where a traded strike has zero volume today are hidden.")
    st.sidebar.markdown("---")
    return on, pct


SERIES_LABELS = {
    "base": "Current 1:4", "parallel": "Parallel 1:4", "next": "Next 1:4",
    "r35": "Current 1:3.5", "n35": "Next 1:3.5", "r3": "Current 1:3", "n3": "Next 1:3",
    "r25": "Current 1:2.5", "n25": "Next 1:2.5", "r2": "Current 1:2", "n2": "Next 1:2",
}


def render_sidebar_connection():
    snap = SERVICE.snapshot()
    state = snap["state"]
    icon = "🟢" if (snap["running"] and state == "running") else ("🟡" if snap["running"] else "🔴")
    st.sidebar.caption(f"{icon} Scanner: **{state}**")
    with st.sidebar.expander("🔌 Kite login & scanner settings", expanded=not SERVICE.has_credentials()):
        msg = st.session_state.pop("kite_msg", None)
        if msg:
            st.info(msg)
        if snap["last_error"]:
            st.error(snap["last_error"])
        if snap["cycle"]:
            st.caption(
                f"Cycle {snap['cycle']} · {snap['elapsed_ms'] or 0:,.0f} ms · last OK {snap['last_ok']} · "
                f"{snap['symbols']} stocks (cur) / {snap['next_symbols']} (next) · "
                f"expiry {snap['expiry']} / {snap['next_expiry']}"
            )
            if snap["rows"]:
                st.caption(" · ".join(f"{SERIES_LABELS[k]}: {v}" for k, v in snap["rows"].items()))

        c1, c2, c3 = st.columns(3)
        if c1.button("▶ Start", key="sc_start", use_container_width=True):
            SERVICE.start()
            st.rerun()
        if c2.button("⏹ Stop", key="sc_stop", use_container_width=True):
            SERVICE.stop(user=True)
            st.rerun()
        if c3.button("🔄 Restart", key="sc_restart", use_container_width=True):
            SERVICE.user_stopped = False
            SERVICE.restart()
            st.rerun()

        st.markdown("**Kite credentials** (Zerodha access token expires every day)")
        st.caption("If typing gets interrupted, untick *Auto refresh* in the sidebar while you paste the token.")
        api_key_in = st.text_input("API key", value=SERVICE.api_key, key="kite_api_key_in")
        token_in = st.text_input("Access token (paste to override)", type="password", key="kite_token_in",
                                 placeholder="●●●●●● set" if SERVICE.access_token else "")
        if st.button("Use these credentials", key="kite_use"):
            SERVICE.set_credentials(api_key_in.strip(), token_in.strip() or SERVICE.access_token)
            st.rerun()

        secret = _secret("KITE_API_SECRET")
        if SERVICE.api_key:
            st.link_button("1️⃣ Open Kite login", KiteConnect(api_key=SERVICE.api_key).login_url())
            rt_in = st.text_input("2️⃣ request_token from the redirect URL", key="kite_rt_in")
            secret_in = "" if secret else st.text_input("API secret", type="password", key="kite_secret_in")
            if st.button("3️⃣ Generate access token", key="kite_gen"):
                try:
                    data = KiteConnect(api_key=SERVICE.api_key).generate_session(
                        rt_in.strip(), api_secret=(secret or secret_in.strip()))
                    SERVICE.set_credentials(SERVICE.api_key, data["access_token"])
                    st.session_state["kite_msg"] = "✅ Access token generated - scanner restarted."
                except Exception as e:
                    st.session_state["kite_msg"] = f"Could not generate token: {e}"
                st.rerun()
            st.caption("Tip: set your Kite app's redirect URL to this app's URL and the login completes by itself.")

        st.markdown("**Scanner settings**")
        cfg = SERVICE.cfg
        with st.form("scanner_cfg"):
            side = st.selectbox("Side", ["BOTH", "CE", "PE"], index=["BOTH", "CE", "PE"].index(cfg.side))
            expiry = st.text_input("Base expiry (current / next / YYYY-MM)", value=cfg.expiry)
            refresh = st.number_input("Scan refresh (sec)", 0.5, 120.0, float(cfg.refresh_sec), 0.5)
            r1, r2, r3 = st.columns(3)
            ratio = r1.number_input("Ratio A", 1.0, 10.0, float(cfg.ratio), 0.5)
            ratio35 = r2.number_input("Ratio B", 1.0, 10.0, float(cfg.ratio35), 0.5)
            ratio3 = r3.number_input("Ratio C", 1.0, 10.0, float(cfg.ratio3), 0.5)
            r4, r5, r6 = st.columns(3)
            ratio25 = r4.number_input("Ratio D", 1.0, 10.0, float(cfg.ratio25), 0.5)
            ratio2 = r5.number_input("Ratio E", 1.0, 10.0, float(cfg.ratio2), 0.5)
            product = r6.selectbox("Product", ["NRML", "MIS"], index=["NRML", "MIS"].index(cfg.product))
            o1, o2 = st.columns(2)
            scan_otm = o1.number_input("Scan OTM steps", 1, 200, int(cfg.scan_otm_steps))
            quote_otm = o2.number_input("Quote OTM steps (0 = all)", 0, 500, int(cfg.quote_otm_steps))
            m1, m2 = st.columns(2)
            min_far_bid = m1.number_input("Min far bid ₹", 0.0, 100.0, float(cfg.min_far_bid), 0.05)
            min_credit = m2.number_input("Min credit ₹/share", 0.0, 100.0, float(cfg.min_credit_per_share), 0.05)
            m3, m4 = st.columns(2)
            min_incr = m3.number_input("Min incremental ₹/share", 0.0, 100.0, float(cfg.min_incremental_per_share), 0.05)
            m5, m6 = st.columns(2)
            min_bid_lots = m5.number_input("Min bid qty (lots)", 0, 50, int(cfg.min_bid_qty_lots))
            min_ask_lots = m6.number_input("Min ask qty (lots)", 0, 50, int(cfg.min_ask_qty_lots))
            if st.form_submit_button("Apply & restart scanner"):
                new_cfg = copy.copy(cfg)
                new_cfg.side, new_cfg.expiry, new_cfg.refresh_sec = side, expiry.strip() or "current", float(refresh)
                new_cfg.ratio, new_cfg.ratio35, new_cfg.ratio3 = float(ratio), float(ratio35), float(ratio3)
                new_cfg.ratio25, new_cfg.ratio2, new_cfg.product = float(ratio25), float(ratio2), product
                new_cfg.scan_otm_steps, new_cfg.quote_otm_steps = int(scan_otm), int(quote_otm)
                new_cfg.min_far_bid, new_cfg.min_credit_per_share = float(min_far_bid), float(min_credit)
                new_cfg.min_incremental_per_share = float(min_incr)
                new_cfg.min_bid_qty_lots, new_cfg.min_ask_qty_lots = int(min_bid_lots), int(min_ask_lots)
                SERVICE.user_stopped = False
                SERVICE.set_config(new_cfg)
                st.rerun()
        st.caption("Tab titles are fixed to 1:4 / 1:3.5 / 1:3 / 1:2.5 / 1:2. Change ratios A–E only if you want those tabs to show other ratios.")

        st.markdown("**Data inputs**")
        up = st.file_uploader("Results calendar CSV (columns: Symbol, Date)", type=["csv"], key="up_results_cal")
        if up is not None:
            data = up.getvalue()
            try:
                with open(RESULTS_CSV_PATH, "rb") as f:
                    same = f.read() == data
            except Exception:
                same = False
            if not same:
                with open(RESULTS_CSV_PATH, "wb") as f:
                    f.write(data)
        st.text_area("Manual F&O ban list (comma separated, added to NSE list)", key="manual_ban_text", height=60)

        if not _secret("APP_PASSWORD"):
            st.warning("No APP_PASSWORD secret set: anyone with the URL can see this dashboard and use your Kite session.")


# ==============================================================================================
# MAIN FLOW
# ==============================================================================================
require_password()
handle_kite_redirect()
SERVICE.ensure_running()

# --- CUSTOM FINTECH UI / UX STYLING ---
st.markdown("""
<style>
    .stMetric {
        background-color: #1a2234;
        border: 1px solid #2d3748;
        padding: 14px;
        border-radius: 8px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
    }
    h1, h2, h3 {
        letter-spacing: -0.5px;
    }
</style>
""", unsafe_allow_html=True)

render_sidebar_connection()
banned_symbols_set = get_banned_symbols()

# =========================================================================
# STOCK DETAIL VIEW - opened in a new tab by clicking a stock name:
#   /?view=stock&symbol=ADANIPOWER
# The sidebar distance filter is created first because the detail tables use it.
# =========================================================================
_detail_symbol = str(st.query_params.get("symbol", "")).strip().upper()
if st.query_params.get("view") == "stock":
    dist_filter_on, min_leg_dist_pct = render_dist_filter_sidebar()
    if _detail_symbol:
        render_stock_detail_view(_detail_symbol)
    else:
        st.info("No stock selected. Go back to the main dashboard and click a stock name.")
    st.stop()

st.title("FAST Ratio Spread Opportunities & Portfolio Manager")
st.caption("Institutional-grade tracking for ratio spreads, capital churning, sector exposure, theta velocity, and corporate intelligence.")

refresh_sec = st.sidebar.slider("Dashboard refresh seconds", 1, 10, 4)
auto_refresh = st.sidebar.checkbox("Auto refresh", value=True)

if auto_refresh and SERVICE.has_credentials():   # paused until Kite credentials exist, so typing is not interrupted
    st_autorefresh(interval=refresh_sec * 1000, key="fast_ratio_parallel_refresh")

# --- BAN LIST POPUP: stays open until acknowledged (once per browser session) ---
if banned_symbols_set and not st.session_state.get("ban_ack"):
    _ban_list_str = ", ".join(sorted(banned_symbols_set))

    @st.dialog("🚫 Active NSE F&O Ban List Notice")
    def show_ban_dialog():
        st.warning(f"The following **{len(banned_symbols_set)} stock(s)** are currently under official NSE F&O ban:")
        st.markdown(f"### `{_ban_list_str}`")
        st.info("New position creation is restricted by the exchange. Existing positions can only be squared off.")
        if st.button("I Understand & Acknowledge", use_container_width=True):
            st.session_state["ban_ack"] = True
            st.rerun()

    _DIALOG_STATE["open"] = True
    show_ban_dialog()

# --- PORTFOLIO FUND SIZE & SECTOR LIMIT SETTINGS IN SIDEBAR ---
st.sidebar.markdown("---")
st.sidebar.subheader("💰 Portfolio Capital & Risk Settings")
total_permitted_fund = st.sidebar.number_input(
    "Total Permitted Fund Size (₹)", 
    value=80000000.0, 
    step=1000000.0, 
    format="%.0f"
)
risky_threshold_pct = st.sidebar.slider(
    "Stock Concentration Threshold (%)", 
    5.0, 25.0, 10.0, 0.5,
    help="Any single stock consuming more than this % of total fund size is marked as Risky."
)
sector_limit_pct = st.sidebar.slider(
    "Sector Concentration Limit (%)", 
    15.0, 50.0, 30.0, 1.0,
    help="Warns if a single sector consumes more than this % of permitted fund size."
)
st.sidebar.markdown("---")

# --- LIVE BAN LIST DISPLAY IN SIDEBAR ---
st.sidebar.subheader("🚫 Live NSE F&O Ban List")
if banned_symbols_set:
    for s in sorted(list(banned_symbols_set)):
        st.sidebar.markdown(f"🔴 **{s}**")
else:
    st.sidebar.success("No stocks currently in F&O ban.")
st.sidebar.markdown("---")

# --- CE / PE FILTER SELECTION IN SIDEBAR ---
st.sidebar.subheader("Directional Bias (CE / PE)")
ce_pe_filter = st.sidebar.radio(
    "Select Spread Type",
    [
        "🌐 All (CE & PE Combined)", 
        "📈 CE Spreads Only", 
        "📉 PE Spreads Only"
    ],
    label_visibility="collapsed"
)
st.sidebar.markdown("---")

dist_filter_on, min_leg_dist_pct = render_dist_filter_sidebar()

PAGE_NAMES = [
    "Current 1:4 Ratio Spread",
    "Parallel Incremental 1:4",
    "Current 1:3.5 Ratio Spread",
    "Current 1:3 Ratio Spread",
    "Current 1:2.5 Ratio Spread",
    "Current 1:2 Ratio Spread",
    "Next Month 1:4 Ratio Spread",
    "Next Month 1:3.5 Ratio Spread",
    "Next Month 1:3 Ratio Spread",
    "Next Month 1:2.5 Ratio Spread",
    "Next Month 1:2 Ratio Spread",
    "📊 Portfolio & Capital Churning",
]
active_page = st.radio(
    "Dashboard section",
    PAGE_NAMES,
    horizontal=True,
    label_visibility="collapsed",
    key="ratio_dashboard_section",
)

check_result_popups()

# ---------- PORTFOLIO SPIKE ALERTS (pinned until acknowledged) ----------
portfolio_spike_pct = st.sidebar.number_input(
    "Portfolio spike alert (% move in last 3 mins)", value=2.0, step=0.5, min_value=0.5,
    key="portfolio_spike_pct")

try:
    new_portfolio_alerts = process_portfolio_alerts(portfolio_spike_pct)
except Exception as _alert_err:
    new_portfolio_alerts = []
    st.warning(f"Portfolio spike check skipped this refresh: {_alert_err}")
for _a in new_portfolio_alerts:
    st.toast(f"🚨 {_a['symbol']} moved {_a['move_pct']:+.2f}% in the last 3 minutes", icon="🚨")
    play_filing_alert_sound()
render_portfolio_alert_panel()
if active_page and ("Ratio" in active_page or "Parallel" in active_page):
    render_best_across_ratios()

# --- MAIN ROUTING ---
if active_page == "Current 1:4 Ratio Spread":
    render_generic_ratio_tab(RATIO_CSV_PATH, "Current 1:4", 4, "", False)
elif active_page == "Parallel Incremental 1:4":
    render_parallel_ratio()
elif active_page == "Current 1:3.5 Ratio Spread":
    render_generic_ratio_tab(RATIO35_CSV_PATH, "Current 1:3.5", 3.5, "ratio35", False)
elif active_page == "Current 1:3 Ratio Spread":
    render_generic_ratio_tab(RATIO3_CSV_PATH, "Current 1:3", 3, "ratio3", False)
elif active_page == "Current 1:2.5 Ratio Spread":
    render_generic_ratio_tab(RATIO25_CSV_PATH, "Current 1:2.5", 2.5, "ratio25", False)
elif active_page == "Current 1:2 Ratio Spread":
    render_generic_ratio_tab(RATIO2_CSV_PATH, "Current 1:2", 2, "ratio2", False)
elif active_page == "Next Month 1:4 Ratio Spread":
    render_generic_ratio_tab(NEXT_RATIO_CSV_PATH, "Next Month 1:4", 4, "next", True)
elif active_page == "Next Month 1:3.5 Ratio Spread":
    render_generic_ratio_tab(NEXT_RATIO35_CSV_PATH, "Next Month 1:3.5", 3.5, "next_ratio35", True)
elif active_page == "Next Month 1:3 Ratio Spread":
    render_generic_ratio_tab(NEXT_RATIO3_CSV_PATH, "Next Month 1:3", 3, "next_ratio3", True)
elif active_page == "Next Month 1:2.5 Ratio Spread":
    render_generic_ratio_tab(NEXT_RATIO25_CSV_PATH, "Next Month 1:2.5", 2.5, "next_ratio25", True)
elif active_page == "Next Month 1:2 Ratio Spread":
    render_generic_ratio_tab(NEXT_RATIO2_CSV_PATH, "Next Month 1:2", 2, "next_ratio2", True)
elif active_page == "📊 Portfolio & Capital Churning":
    render_portfolio_churning_tab()
