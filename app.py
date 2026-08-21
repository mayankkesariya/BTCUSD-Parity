import io
import math
import time
import requests
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ==============================================================================\n# NIFTY Options Ratio Spread Matrix & Scanner - Kite Web ENCTOKEN version
# ==============================================================================\n# IMPORTANT:
# This version deliberately DOES NOT request NSE:NIFTY 50 or any NSE index
# symbol from the quote endpoint.  The live NIFTY reference price is estimated
# from the selected-expiry CE/PE option prices (synthetic spot = strike + CE - PE).
# This avoids the HTTP 400 that was occurring on the index quote request.
# ==============================================================================\n
st.set_page_config(
    page_title="NIFTY Options Ratio Spread Matrix & Scanner",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📈 NIFTY Options Ratio Spread Matrix & Scanner")

with st.sidebar:
    st.header("🔑 Authentication")
    raw_token = st.text_input(
        "Kite Web enctoken",
        type="password",
        help="Paste the active enctoken value from Kite Web cookies. Do not include 'enctoken '.",
    )
    kite_enctoken = raw_token.strip() if raw_token else ""

    st.header("⚙️ Configuration")
    underlying = st.selectbox(
        "Underlying Index", ["NIFTY", "BANKNIFTY", "FINNIFTY"], index=0
    )
    refresh_seconds = st.slider("Auto Refresh (seconds)", 5, 60, 10, 1)

    st.markdown("---")
    st.header("🔍 Scanner Controls")
    strategy_side = st.selectbox(
        "Strategy Side", ["Call Ratio Spread", "Put Ratio Spread"]
    )
    ratio_start = st.number_input(
        "Ratio From (1:N)", min_value=1, max_value=20, value=2, step=1
    )
    ratio_end = st.number_input(
        "Ratio Till (1:N)", min_value=1, max_value=20, value=10, step=1
    )
    price_mode = st.selectbox(
        "Price Mode", ["Bid/Ask (Live Depth)", "Last Price / Mark Price"], index=0
    )
    min_credit = st.number_input(
        "Minimum Net Credit (Pts)", min_value=0.0, value=10.0, step=1.0
    )
    width_min = st.number_input(
        "Minimum Farak (Width)", min_value=50, value=100, step=50
    )
    width_max = st.number_input(
        "Maximum Farak (Width)", min_value=50, value=1000, step=50
    )
    max_rows = st.slider("Top Opportunities", 5, 50, 20, 5)

st_autorefresh(interval=refresh_seconds * 1000, key="nifty_matrix_autorefresh")

# ==============================================================================\n# Instrument / Quote helpers
# ==============================================================================\n
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_nfo_instruments():
    url = "https://api.kite.trade/instruments/NFO"
    response = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    df = pd.read_csv(io.StringIO(response.text))
    if df.empty:
        raise RuntimeError("Kite NFO instrument master is empty.")

    required = {"name", "tradingsymbol", "expiry", "strike", "instrument_type", "instrument_token"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"NFO instrument master is missing columns: {sorted(missing)}")

    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["instrument_token"] = pd.to_numeric(df["instrument_token"], errors="coerce")
    return df


def kite_session(enctoken):
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"enctoken {enctoken.strip()}",
            "X-Kite-Version": "3",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://kite.zerodha.com/",
            "Origin": "https://kite.zerodha.com",
        }
    )
    return s


def _request_quote(session, symbols):
    # Use a list of repeated i parameters. This is the format used by the
    # Kite quote API and by working enctoken examples.
    params = [("i", str(symbol)) for symbol in symbols]
    return session.get(
        "https://api.kite.trade/quote",
        params=params,
        timeout=20,
    )


def fetch_kite_quotes(option_symbols, enctoken):
    """Fetch option quotes using Kite Web enctoken.

    We intentionally never request an NSE index symbol here. Only NFO option
    symbols are sent to the quote endpoint. If one option causes HTTP 400,
    the batch is split until the bad instrument can be isolated and skipped.
    """
    if not enctoken:
        raise RuntimeError("No enctoken supplied.")
    if not option_symbols:
        return {}, []

    session = kite_session(enctoken)
    quotes = {}
    bad_symbols = []

    # Kite's documented quote bulk endpoint supports up to 200 instruments.
    # Keep a conservative batch size.
    batch_size = 100

    def load_chunk(chunk):
        if not chunk:
            return

        try:
            resp = _request_quote(session, chunk)
        except requests.RequestException as exc:
            raise RuntimeError(f"Kite quote connection error: {exc}")

        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError:
                raise RuntimeError(
                    f"Kite returned non-JSON response: {resp.text[:500]}"
                )

            if payload.get("status") != "success":
                raise RuntimeError(f"Kite quote error: {payload}")

            data = payload.get("data") or {}
            if isinstance(data, dict):
                quotes.update(data)
            return

        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Kite rejected the enctoken (HTTP {resp.status_code}). "
                "Copy a fresh enctoken from Kite Web and paste only the token value."
            )

        if resp.status_code == 429:
            time.sleep(2.0)
            try:
                retry = _request_quote(session, chunk)
            except requests.RequestException as exc:
                raise RuntimeError(f"Kite quote retry connection error: {exc}")
            if retry.status_code == 200:
                try:
                    payload = retry.json()
                except ValueError:
                    payload = {}
                data = payload.get("data") or {}
                if isinstance(data, dict):
                    quotes.update(data)
                return
            if retry.status_code in (401, 403):
                raise RuntimeError(
                    f"Kite rejected the enctoken after retry (HTTP {retry.status_code})."
                )
            resp = retry

        # A 400 is an InputException. It can be caused by one bad instrument.
        # Do not recursively raise forever: isolate the bad symbol and continue.
        if resp.status_code == 400:
            if len(chunk) == 1:
                bad_symbols.append(str(chunk[0]))
                return
            mid = max(1, len(chunk) // 2)
            load_chunk(chunk[:mid])
            load_chunk(chunk[mid:])
            return

        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:500]
        raise RuntimeError(f"Kite quote HTTP {resp.status_code}: {detail}")

    for start in range(0, len(option_symbols), batch_size):
        load_chunk(option_symbols[start : start + batch_size])
        if start + batch_size < len(option_symbols):
            time.sleep(0.55)

    return quotes, bad_symbols


def quote_symbol(row):
    return f"NFO:{row['tradingsymbol']}"


def first_depth_price(quote, side):
    depth = quote.get("depth") or {}
    levels = depth.get(side) or []
    if levels and isinstance(levels[0], dict):
        try:
            p = float(levels[0].get("price", 0) or 0)
            return p if p > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def enrich_option_quotes(leg_df, quotes):
    rows = []
    for _, row in leg_df.iterrows():
        key = f"NFO:{row['tradingsymbol']}"
        q = quotes.get(key, {})
        if not q:
            # Some responses can be keyed by the bare instrument token.
            q = quotes.get(str(int(row["instrument_token"])), {})

        last_price = q.get("last_price", 0.0)
        try:
            last_price = float(last_price or 0.0)
        except (TypeError, ValueError):
            last_price = 0.0

        rows.append(
            {
                "tradingsymbol": row["tradingsymbol"],
                "instrument_token": int(row["instrument_token"]),
                "strike": float(row["strike"]),
                "instrument_type": row["instrument_type"],
                "last_price": last_price,
                "best_bid": first_depth_price(q, "buy"),
                "best_ask": first_depth_price(q, "sell"),
            }
        )
    return pd.DataFrame(rows)


def estimate_spot_from_options(option_df):
    """Estimate spot without querying NSE:NIFTY 50.

    For each strike with both CE and PE prices, synthetic spot is K + CE - PE.
    We choose the strike where CE and PE prices are closest, which is normally
    near ATM. This is the same put-call-parity style approach used in working
    enctoken examples.
    """
    if option_df.empty:
        return None, None

    ce = option_df[option_df["instrument_type"] == "CE"][["strike", "last_price"]].rename(
        columns={"last_price": "ce_price"}
    )
    pe = option_df[option_df["instrument_type"] == "PE"][["strike", "last_price"]].rename(
        columns={"last_price": "pe_price"}
    )

    merged = ce.merge(pe, on="strike", how="inner")
    merged = merged[(merged["ce_price"] > 0) & (merged["pe_price"] > 0)].copy()
    if merged.empty:
        return None, None

    merged["abs_diff"] = (merged["ce_price"] - merged["pe_price"]).abs()
    best = merged.sort_values("abs_diff").iloc[0]
    synthetic_spot = float(best["strike"] + best["ce_price"] - best["pe_price"])
    return synthetic_spot, float(best["strike"])


# ==============================================================================\n# Pricing helpers
# ==============================================================================\n
def premium_buy(row, mode):
    if mode == "Bid/Ask (Live Depth)" and pd.notna(row.get("best_ask")) and row.get("best_ask", 0) > 0:
        return float(row["best_ask"])
    return float(row.get("last_price", 0.0) or 0.0)


def premium_sell(row, mode):
    if mode == "Bid/Ask (Live Depth)" and pd.notna(row.get("best_bid")) and row.get("best_bid", 0) > 0:
        return float(row["best_bid"])
    return float(row.get("last_price", 0.0) or 0.0)


# ==============================================================================\n# Analytical core
# ==============================================================================\n
def find_ratio_spreads(df, option_type, qty_long, qty_short, min_credit, width_min, width_max, price_mode, spot_value):
    sub = df[df["instrument_type"] == option_type].copy()
    ascending = option_type == "CE"
    sub = sub.sort_values("strike", ascending=ascending).reset_index(drop=True)
    if sub.empty:
        return pd.DataFrame()

    rows = []
    for i in range(len(sub)):
        long_row = sub.iloc[i]
        long_k = float(long_row["strike"])

        for j in range(i + 1, len(sub)):
            short_row = sub.iloc[j]
            short_k = float(short_row["strike"])

            if option_type == "CE":
                if spot_value is not None and not (long_k >= spot_value and short_k > long_k):
                    continue
                width = short_k - long_k
            else:
                if spot_value is not None and not (long_k <= spot_value and short_k < long_k):
                    continue
                width = long_k - short_k

            if width < width_min or width > width_max:
                continue

            buy_p = premium_buy(long_row, price_mode)
            sell_p = premium_sell(short_row, price_mode)
            if buy_p <= 0 or sell_p <= 0:
                continue

            net_credit = qty_short * sell_p - qty_long * buy_p
            if net_credit < min_credit:
                continue

            max_profit = net_credit + width * qty_long
            denom = max(qty_short - qty_long, 1)
            breakeven = (
                short_k + max_profit / denom
                if option_type == "CE"
                else short_k - max_profit / denom
            )

            rows.append(
                {
                    "ratio": f"1:{qty_short}",
                    "long_strike": long_k,
                    "short_strike": short_k,
                    "width": width,
                    "buy_price": buy_p,
                    "sell_price": sell_p,
                    "net_credit": net_credit,
                    "max_profit": max_profit,
                    "breakeven": breakeven,
                }
            )

    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values(
            ["net_credit", "width"], ascending=[False, False]
        ).reset_index(drop=True)
    return result


def find_atm_width_ratios(df, option_type, atm_strike, qty_long, qty_short, widths, price_mode):
    sub = df[df["instrument_type"] == option_type].copy()
    sub["strike"] = pd.to_numeric(sub["strike"], errors="coerce")
    sub = sub.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)
    if sub.empty or atm_strike is None:
        return pd.DataFrame()

    exact = sub[sub["strike"] == atm_strike]
    long_row = exact.iloc[0] if not exact.empty else sub.iloc[(sub["strike"] - atm_strike).abs().idxmin()]
    long_strike = float(long_row["strike"])
    buy_p = premium_buy(long_row, price_mode)

    rows = []
    for w in widths:
        target = long_strike + w if option_type == "CE" else long_strike - w
        nearest_idx = (sub["strike"] - target).abs().idxmin()
        short_row = sub.iloc[nearest_idx]
        short_strike = float(short_row["strike"])
        if short_strike == long_strike:
            continue

        sell_p = premium_sell(short_row, price_mode)
        actual_width = (
            short_strike - long_strike
            if option_type == "CE"
            else long_strike - short_strike
        )
        net_credit = qty_short * sell_p - qty_long * buy_p

        rows.append(
            {
                "Width": actual_width,
                "Long Strike": long_strike,
                "Short Strike": short_strike,
                "Buy Price": round(buy_p, 2),
                "Sell Price": round(sell_p, 2),
                "Net Credit": round(net_credit, 2),
            }
        )
    return pd.DataFrame(rows)


def build_ratio_matrix(df, option_type, base_strikes, width_ratio_pairs, price_mode):
    sub = df[df["instrument_type"] == option_type].copy()
    sub["strike"] = pd.to_numeric(sub["strike"], errors="coerce")
    sub = sub.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)
    if sub.empty or not base_strikes or not width_ratio_pairs:
        return pd.DataFrame()

    col_tuples = [("Mark Price", "")] + [
        (f"{int(w)}", f"1:{int(r)}") for w, r in width_ratio_pairs
    ]
    columns = pd.MultiIndex.from_tuples(col_tuples, names=["Width", "Ratio"])

    data_rows = []
    for base in base_strikes:
        matches = sub[sub["strike"] == base]
        if matches.empty:
            data_rows.append([math.nan] + [math.nan] * len(width_ratio_pairs))
            continue

        long_row = matches.iloc[0]
        buy_p = premium_buy(long_row, price_mode)
        mark_p = long_row.get("last_price", math.nan)
        candidates = sub[sub["strike"] > base] if option_type == "CE" else sub[sub["strike"] < base]
        row_vals = [round(float(mark_p), 2) if pd.notna(mark_p) else math.nan]

        for w, r in width_ratio_pairs:
            if candidates.empty or pd.isna(buy_p):
                row_vals.append(math.nan)
                continue

            target = base + w if option_type == "CE" else base - w
            nearest_idx = (candidates["strike"] - target).abs().idxmin()
            short_row = candidates.loc[nearest_idx]
            sell_p = premium_sell(short_row, price_mode)
            if pd.isna(sell_p):
                row_vals.append(math.nan)
                continue

            net_credit = r * sell_p - buy_p
            row_vals.append(round(float(net_credit), 2))

        data_rows.append(row_vals)

    matrix = pd.DataFrame(
        data_rows,
        index=[f"{int(b)}" for b in base_strikes],
        columns=columns,
    )
    matrix.index.name = "Strike"
    return matrix


# ==============================================================================\n# Main application
# ==============================================================================\n
if not kite_enctoken:
    st.info("👈 Enter your Zerodha Kite Web enctoken in the sidebar to load live data.")
    st.stop()

try:
    instruments_df = fetch_nfo_instruments()

    today = pd.Timestamp.now(tz="Asia/Kolkata").date()
    underlying_inst = instruments_df[
        (instruments_df["name"] == underlying)
        & (instruments_df["instrument_type"].isin(["CE", "PE"]))
        & (instruments_df["expiry"].notna())
        & (instruments_df["expiry"] >= today)
    ].copy()

    expiries = sorted(underlying_inst["expiry"].unique())
    if not expiries:
        st.error(f"No active option expiries found for {underlying}.")
        st.stop()

    selected_expiry = st.selectbox(
        "Select Expiry",
        expiries,
        index=0,
        format_func=lambda x: x.strftime("%d-%b-%Y"),
    )

    leg_df = underlying_inst[underlying_inst["expiry"] == selected_expiry].copy()
    leg_df = leg_df.dropna(subset=["instrument_token", "strike", "tradingsymbol"])
    leg_df["instrument_token"] = pd.to_numeric(leg_df["instrument_token"], errors="coerce")
    leg_df = leg_df.dropna(subset=["instrument_token"])
    leg_df["instrument_token"] = leg_df["instrument_token"].astype(int)

    # Only NFO option symbols are sent. There is NO NSE:NIFTY 50 request.
    option_symbols = [quote_symbol(row) for _, row in leg_df.iterrows()]

    with st.spinner(f"Fetching live option quotes for {len(option_symbols)} instruments..."):
        quotes, bad_symbols = fetch_kite_quotes(option_symbols, kite_enctoken)

    if not quotes:
        st.error(
            "No live option quotes were returned. The enctoken may be expired, "
            "or Kite Web may not be accepting the session from this server."
        )
        if bad_symbols:
            st.warning(f"Skipped invalid quote symbols: {bad_symbols[:10]}")
        st.stop()

    option_df = enrich_option_quotes(leg_df, quotes)

    # Remove legs for which Kite returned no LTP. Keep valid live quotes only.
    option_df = option_df[option_df["last_price"] > 0].copy()
    if option_df.empty:
        st.error("Kite returned no usable option LTPs for the selected expiry.")
        st.stop()

    # --------------------------------------------------------------------------\n    # Estimate NIFTY reference price from options only.
    # This completely removes the failing NSE:NIFTY 50 quote call.
    # --------------------------------------------------------------------------\n    spot_value, atm_reference_strike = estimate_spot_from_options(option_df)
    if spot_value is None or spot_value <= 0:
        # Last-resort ATM reference: middle strike of available CE/PE overlap.
        common_strikes = sorted(
            set(option_df.loc[option_df["instrument_type"] == "CE", "strike"])
            & set(option_df.loc[option_df["instrument_type"] == "PE", "strike"])
        )
        if not common_strikes:
            st.error("Unable to determine an ATM reference from the option quotes.")
            st.stop()
        atm_reference_strike = float(common_strikes[len(common_strikes) // 2])
        spot_value = atm_reference_strike

    option_df["spot_price"] = float(spot_value)

    # Straddle around synthetic ATM/reference strike.
    straddle_price = None
    ce_sub = option_df[option_df["instrument_type"] == "CE"].copy()
    pe_sub = option_df[option_df["instrument_type"] == "PE"].copy()
    if not ce_sub.empty and not pe_sub.empty:
        atm_ce = ce_sub.loc[(ce_sub["strike"] - spot_value).abs().idxmin()]
        atm_pe = pe_sub.loc[(pe_sub["strike"] - spot_value).abs().idxmin()]
        straddle_price = premium_buy(atm_ce, price_mode) + premium_buy(atm_pe, price_mode)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("NIFTY Reference Price", f"{spot_value:,.2f}")
    m2.metric("ATM Straddle Premium", f"{straddle_price:,.2f}" if straddle_price else "N/A")
    m3.metric("Selected Expiry", selected_expiry.strftime("%d-%b-%Y"))
    m4.metric("Option Quotes Loaded", f"{len(quotes):,}")

    if bad_symbols:
        st.caption(f"Skipped {len(bad_symbols)} invalid/unavailable option symbols.")

    # ==========================================================================\n    # Section 1: Live Opportunities Scanner
    # ==========================================================================\n    st.markdown("---")
    st.subheader("🔍 Live Opportunities Scanner")

    opt_type_code = "CE" if strategy_side == "Call Ratio Spread" else "PE"
    s_start = min(ratio_start, ratio_end)
    s_end = max(ratio_start, ratio_end)

    scanner_frames = []
    for short_r in range(s_start, s_end + 1):
        frame = find_ratio_spreads(
            option_df,
            opt_type_code,
            1,
            short_r,
            min_credit,
            width_min,
            width_max,
            price_mode,
            spot_value,
        )
        if not frame.empty:
            scanner_frames.append(frame)

    opps = pd.concat(scanner_frames, ignore_index=True) if scanner_frames else pd.DataFrame()
    if opps.empty:
        st.warning("No ratio spread opportunities found matching the sidebar criteria.")
    else:
        display_opps = opps.head(max_rows).rename(
            columns={
                "ratio": "Ratio",
                "long_strike": "Long Strike",
                "short_strike": "Short Strike",
                "width": "Farak",
                "buy_price": "Buy Price",
                "sell_price": "Sell Price",
                "net_credit": "Net Credit",
                "max_profit": "Max Profit",
                "breakeven": "Breakeven",
            }
        )
        st.dataframe(display_opps, use_container_width=True, hide_index=True)

    # ==========================================================================\n    # Section 2: ATM Curve Finder
    # ==========================================================================\n    st.markdown("---")
    st.subheader("🎯 ATM Curve Finder - Call Side")

    call_sub = option_df[option_df["instrument_type"] == "CE"].sort_values("strike")
    if call_sub.empty:
        st.info("Call options data unavailable.")
    else:
        atm_call_strike = float(call_sub.loc[(call_sub["strike"] - spot_value).abs().idxmin(), "strike"])
        c1, c2, c3, c4 = st.columns(4)
        c1.number_input("ATM Long", value=1, min_value=1, max_value=1, key="call_atm_l")
        c_ratio = c2.number_input("Ratio (1:N)", value=10, min_value=1, key="call_atm_r")
        c_start = c3.number_input("Start Farak", value=200, step=50, key="call_atm_start")
        c_end = c4.number_input("End Farak", value=800, step=50, key="call_atm_end")
        f_min, f_max = min(c_start, c_end), max(c_start, c_end)
        call_widths = list(range(f_min, f_max + 50, 50))
        atm_call_table = find_atm_width_ratios(
            option_df, "CE", atm_call_strike, 1, int(c_ratio), call_widths, price_mode
        )
        st.dataframe(atm_call_table, use_container_width=True, hide_index=True)

    st.subheader("🎯 ATM Curve Finder - Put Side")
    put_sub = option_df[option_df["instrument_type"] == "PE"].sort_values("strike")
    if put_sub.empty:
        st.info("Put options data unavailable.")
    else:
        atm_put_strike = float(put_sub.loc[(put_sub["strike"] - spot_value).abs().idxmin(), "strike"])
        p1, p2, p3, p4 = st.columns(4)
        p1.number_input("ATM Long", value=1, min_value=1, max_value=1, key="put_atm_l")
        p_ratio = p2.number_input("Ratio (1:N)", value=10, min_value=1, key="put_atm_r")
        p_start = p3.number_input("Start Farak", value=200, step=50, key="put_atm_start")
        p_end = p4.number_input("End Farak", value=800, step=50, key="put_atm_end")
        pf_min, pf_max = min(p_start, p_end), max(p_start, p_end)
        put_widths = list(range(pf_min, pf_max + 50, 50))
        atm_put_table = find_atm_width_ratios(
            option_df, "PE", atm_put_strike, 1, int(p_ratio), put_widths, price_mode
        )
        st.dataframe(atm_put_table, use_container_width=True, hide_index=True)

    # ==========================================================================\n    # Section 3: Skew Curve Matrix
    # ==========================================================================\n    st.markdown("---")
    st.title("📊 NIFTY Skew Curve Matrix")

    col_slots = [str(i + 1) for i in range(6)]
    default_matrix_hdr = pd.DataFrame(
        [[100, 200, 300, 400, 500, 600], [10, 10, 10, 10, 10, 10]],
        index=["Farak", "Ratio"],
        columns=col_slots,
    )

    st.subheader("Call Side Matrix")
    if not call_sub.empty:
        atm_k_call = float(call_sub.loc[(call_sub["strike"] - spot_value).abs().idxmin(), "strike"])
        call_strikes_matrix = [
            s for s in sorted(call_sub["strike"].unique()) if s >= atm_k_call
        ]
        call_hdr_edited = st.data_editor(
            default_matrix_hdr.copy(),
            key="call_matrix_hdr",
            use_container_width=True,
        )
        call_pairs = [
            (float(call_hdr_edited.loc["Farak", c]), int(call_hdr_edited.loc["Ratio", c]))
            for c in col_slots
            if call_hdr_edited.loc["Farak", c] > 0 and call_hdr_edited.loc["Ratio", c] > 0
        ]
        c_matrix = build_ratio_matrix(
            option_df, "CE", call_strikes_matrix, call_pairs, price_mode
        )
        st.dataframe(c_matrix, use_container_width=True, height=450)

    st.subheader("Put Side Matrix")
    if not put_sub.empty:
        atm_k_put = float(put_sub.loc[(put_sub["strike"] - spot_value).abs().idxmin(), "strike"])
        put_strikes_matrix = [
            s for s in sorted(put_sub["strike"].unique(), reverse=True) if s <= atm_k_put
        ]
        put_hdr_edited = st.data_editor(
            default_matrix_hdr.copy(),
            key="put_matrix_hdr",
            use_container_width=True,
        )
        put_pairs = [
            (float(put_hdr_edited.loc["Farak", c]), int(put_hdr_edited.loc["Ratio", c]))
            for c in col_slots
            if put_hdr_edited.loc["Farak", c] > 0 and put_hdr_edited.loc["Ratio", c] > 0
        ]
        p_matrix = build_ratio_matrix(
            option_df, "PE", put_strikes_matrix, put_pairs, price_mode
        )
        st.dataframe(p_matrix, use_container_width=True, height=450)

except Exception as ex:
    st.error(f"Application Error: {ex}")
    with st.expander("Technical details"):
        import traceback
        st.code(traceback.format_exc())
