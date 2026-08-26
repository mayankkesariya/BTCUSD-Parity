import io
import math
import time
import requests
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ==============================================================================
# NIFTY Options Ratio Spread Matrix & Scanner - Kite Connect API version
# ==============================================================================
# AUTH:
# Uses the official Kite Connect API token flow instead of scraping the
# enctoken cookie from Kite Web. You need:
#   1. api_key       -> from your Kite Connect app (developers.kite.trade)
#   2. access_token   -> generated once per trading day via the login flow /
#                         KiteConnect.generate_session(request_token, api_secret)
# The Authorization header Kite expects is:
#   Authorization: token <api_key>:<access_token>
# This app never touches your api_secret directly; you paste the access_token
# you already generated (e.g. via the standard Kite Connect login redirect,
# or the `kiteconnect` Python SDK's generate_session() helper run once
# separately). This keeps the secret out of the Streamlit session.
#
# The live NIFTY reference price is still estimated synthetically from
# CE/PE option prices (spot = strike + CE - PE) rather than requesting the
# NSE:NIFTY 50 index quote, since that endpoint needs a separate market-data
# entitlement on some plans.
# ==============================================================================

st.set_page_config(
    page_title="NIFTY Options Ratio Spread Matrix & Scanner",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📈 NIFTY Options Ratio Spread Matrix & Scanner")

KITE_BASE_URL = "https://api.kite.trade"

with st.sidebar:
    st.header("🔑 Kite Connect API Authentication")
    api_key = st.text_input(
        "API Key",
        type="password",
        help="From your Kite Connect app at developers.kite.trade",
    ).strip()
    access_token = st.text_input(
        "Access Token",
        type="password",
        help=(
            "Generated once per trading day via KiteConnect.generate_session"
            "(request_token, api_secret) or your login callback flow. "
            "Do not paste your api_secret here."
        ),
    ).strip()

    is_authenticated = bool(api_key and access_token)

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

# ==============================================================================
# Instrument / Quote helpers
# ==============================================================================


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_nfo_instruments():
    url = f"{KITE_BASE_URL}/instruments/NFO"
    response = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    df = pd.read_csv(io.StringIO(response.text))
    if df.empty:
        raise RuntimeError("Kite NFO instrument master is empty.")

    required = {
        "name",
        "tradingsymbol",
        "expiry",
        "strike",
        "instrument_type",
        "instrument_token",
    }
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"NFO instrument master is missing columns: {sorted(missing)}")

    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["instrument_token"] = pd.to_numeric(df["instrument_token"], errors="coerce")
    return df


def kite_session(api_key, access_token):
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"token {api_key}:{access_token}",
            "X-Kite-Version": "3",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
        }
    )
    return s


def _request_quote(session, symbols):
    params = [("i", str(symbol)) for symbol in symbols]
    return session.get(f"{KITE_BASE_URL}/quote", params=params, timeout=20)


def fetch_kite_quotes(option_symbols, api_key, access_token):
    """Fetch option quotes using a Kite Connect api_key + access_token pair.

    Only NFO option symbols are sent here - no NSE index quote is requested.
    If a batch returns HTTP 400 (InputException), it is bisected until the
    single bad instrument can be isolated and skipped.
    """
    if not api_key or not access_token:
        raise RuntimeError("Missing API key or access token.")
    if not option_symbols:
        return {}, []

    session = kite_session(api_key, access_token)
    quotes = {}
    bad_symbols = []

    # Kite's documented quote bulk endpoint supports up to 500 instruments,
    # keep a conservative batch size to stay well under URL/query limits.
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
                raise RuntimeError(f"Kite returned non-JSON response: {resp.text[:500]}")

            if payload.get("status") != "success":
                raise RuntimeError(f"Kite quote error: {payload}")

            data = payload.get("data") or {}
            if isinstance(data, dict):
                quotes.update(data)
            return

        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Kite rejected the API credentials (HTTP {resp.status_code}). "
                "Your access_token may have expired - it is valid for one "
                "trading day only. Regenerate it and paste the new value."
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
                    f"Kite rejected the credentials after retry (HTTP {retry.status_code})."
                )
            resp = retry

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
            time.sleep(0.35)

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
    """Estimate spot via put-call parity: synthetic spot = K + CE - PE,
    picked at the strike where CE and PE prices are closest (normally ATM)."""
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


# ==============================================================================
# Pricing helpers
# ==============================================================================


def premium_buy(row, mode):
    if mode == "Bid/Ask (Live Depth)" and pd.notna(row.get("best_ask")) and row.get("best_ask", 0) > 0:
        return float(row["best_ask"])
    return float(row.get("last_price", 0.0) or 0.0)


def premium_sell(row, mode):
    if mode == "Bid/Ask (Live Depth)" and pd.notna(row.get("best_bid")) and row.get("best_bid", 0) > 0:
        return float(row["best_bid"])
    return float(row.get("last_price", 0.0) or 0.0)


# ==============================================================================
# Analytical core
# ==============================================================================


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
        result = result.sort_values(["net_credit", "width"], ascending=[False, False]).reset_index(drop=True)
    return result


def build_ratio_matrix(df, option_type, base_strikes, width_ratio_pairs, price_mode, qty_long=1):
    """Build a Strike x (Width, Ratio) net-credit matrix.

    Rows = long strikes. Each (width, ratio) column pair finds the strike
    nearest `base + width` (calls) / `base - width` (puts), sells `ratio`
    lots of it against `qty_long` lot(s) of the base strike, and reports the
    net credit. Columns are fully driven by the editable width_ratio_pairs
    list, so the matrix reshapes live with whatever the user edits.
    """
    sub = df[df["instrument_type"] == option_type].copy()
    sub["strike"] = pd.to_numeric(sub["strike"], errors="coerce")
    sub = sub.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)
    if sub.empty or not base_strikes or not width_ratio_pairs:
        return pd.DataFrame()

    col_tuples = [("Mark Price", "")] + [(f"{int(w)}", f"1:{int(r)}") for w, r in width_ratio_pairs]
    columns = pd.MultiIndex.from_tuples(col_tuples, names=["Width", "Ratio"])

    data_rows = []
    for base in base_strikes:
        matches = sub[sub["strike"] == base]
        if matches.empty:
            data_rows.append([math.nan] * (1 + len(width_ratio_pairs)))
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

            net_credit = r * sell_p - qty_long * buy_p
            row_vals.append(round(float(net_credit), 2))

        data_rows.append(row_vals)

    matrix = pd.DataFrame(
        data_rows,
        index=[f"{int(b)}" for b in base_strikes],
        columns=columns,
    )
    matrix.index.name = "Strike"
    return matrix


def style_matrix(matrix):
    """Colour net-credit cells green (>=0) / red (<0), leave Mark Price plain."""
    def _style(v):
        if pd.isna(v):
            return ""
        if v >= 0:
            return "color: #0a7a2f; font-weight: 600;"
        return "color: #b3261e; font-weight: 600;"

    credit_cols = [c for c in matrix.columns if c[0] != "Mark Price"]
    return matrix.style.applymap(_style, subset=credit_cols).format("{:.2f}", na_rep="—")


# ==============================================================================
# Main application
# ==============================================================================

if not is_authenticated:
    st.info(
        "👈 Enter your Kite Connect **API Key** and **Access Token** in the "
        "sidebar to load live data. Generate the access token once per day "
        "via the Kite Connect login flow / `KiteConnect.generate_session()`."
    )
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

    option_symbols = [quote_symbol(row) for _, row in leg_df.iterrows()]

    with st.spinner(f"Fetching live option quotes for {len(option_symbols)} instruments..."):
        quotes, bad_symbols = fetch_kite_quotes(option_symbols, api_key, access_token)

    if not quotes:
        st.error(
            "No live option quotes were returned. The access_token may have "
            "expired (valid for a single trading day), or the API key may "
            "not be entitled to this instrument set."
        )
        if bad_symbols:
            st.warning(f"Skipped invalid quote symbols: {bad_symbols[:10]}")
        st.stop()

    option_df = enrich_option_quotes(leg_df, quotes)
    option_df = option_df[option_df["last_price"] > 0].copy()
    if option_df.empty:
        st.error("Kite returned no usable option LTPs for the selected expiry.")
        st.stop()

    spot_value, atm_reference_strike = estimate_spot_from_options(option_df)
    if spot_value is None or spot_value <= 0:
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

    # ==========================================================================
    # Section 1: Live Opportunities Scanner
    # ==========================================================================
    st.markdown("---")
    st.subheader("🔍 Live Opportunities Scanner")

    opt_type_code = "CE" if strategy_side == "Call Ratio Spread" else "PE"
    s_start = min(ratio_start, ratio_end)
    s_end = max(ratio_start, ratio_end)

    scanner_frames = []
    for short_r in range(s_start, s_end + 1):
        frame = find_ratio_spreads(
            option_df, opt_type_code, 1, short_r, min_credit, width_min, width_max, price_mode, spot_value
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

    # ==========================================================================
    # Section 2: Editable Ratio Matrix
    # ==========================================================================
    st.markdown("---")
    st.title("📊 NIFTY Ratio Spread Matrix")
    st.caption(
        "Rows = long strikes around ATM. Columns = editable (Farak, Ratio) pairs — "
        "edit the header table below and the matrix recalculates from live quotes."
    )

    n_strikes = st.slider("Strikes around ATM to show", 3, 15, 7, key="matrix_n_strikes")
    n_cols = st.slider("Number of Width/Ratio columns", 2, 10, 6, key="matrix_n_cols")

    default_widths = [(i + 1) * 100 for i in range(n_cols)]
    default_ratios = [10] * n_cols
    col_slots = [str(i + 1) for i in range(n_cols)]
    default_matrix_hdr = pd.DataFrame(
        [default_widths, default_ratios],
        index=["Farak", "Ratio"],
        columns=col_slots,
    )

    tab_call, tab_put = st.tabs(["📈 Call Side Matrix", "📉 Put Side Matrix"])

    with tab_call:
        call_sub = option_df[option_df["instrument_type"] == "CE"].sort_values("strike")
        if call_sub.empty:
            st.info("Call options data unavailable.")
        else:
            atm_k_call = float(call_sub.loc[(call_sub["strike"] - spot_value).abs().idxmin(), "strike"])
            all_call_strikes = sorted(call_sub["strike"].unique())
            atm_idx = all_call_strikes.index(atm_k_call) if atm_k_call in all_call_strikes else 0
            call_strikes_matrix = all_call_strikes[atm_idx : atm_idx + n_strikes]

            st.write(f"ATM Call Strike: **{atm_k_call:,.0f}**")
            call_hdr_edited = st.data_editor(
                default_matrix_hdr.copy(),
                key="call_matrix_hdr",
                use_container_width=True,
                num_rows="fixed",
            )
            call_pairs = [
                (float(call_hdr_edited.loc["Farak", c]), int(call_hdr_edited.loc["Ratio", c]))
                for c in col_slots
                if pd.notna(call_hdr_edited.loc["Farak", c])
                and pd.notna(call_hdr_edited.loc["Ratio", c])
                and call_hdr_edited.loc["Farak", c] > 0
                and call_hdr_edited.loc["Ratio", c] > 0
            ]
            c_matrix = build_ratio_matrix(option_df, "CE", call_strikes_matrix, call_pairs, price_mode)
            if c_matrix.empty:
                st.info("No call matrix data for the current strikes/columns.")
            else:
                st.dataframe(style_matrix(c_matrix), use_container_width=True, height=450)

    with tab_put:
        put_sub = option_df[option_df["instrument_type"] == "PE"].sort_values("strike")
        if put_sub.empty:
            st.info("Put options data unavailable.")
        else:
            atm_k_put = float(put_sub.loc[(put_sub["strike"] - spot_value).abs().idxmin(), "strike"])
            all_put_strikes = sorted(put_sub["strike"].unique(), reverse=True)
            atm_idx = all_put_strikes.index(atm_k_put) if atm_k_put in all_put_strikes else 0
            put_strikes_matrix = all_put_strikes[atm_idx : atm_idx + n_strikes]

            st.write(f"ATM Put Strike: **{atm_k_put:,.0f}**")
            put_hdr_edited = st.data_editor(
                default_matrix_hdr.copy(),
                key="put_matrix_hdr",
                use_container_width=True,
                num_rows="fixed",
            )
            put_pairs = [
                (float(put_hdr_edited.loc["Farak", c]), int(put_hdr_edited.loc["Ratio", c]))
                for c in col_slots
                if pd.notna(put_hdr_edited.loc["Farak", c])
                and pd.notna(put_hdr_edited.loc["Ratio", c])
                and put_hdr_edited.loc["Farak", c] > 0
                and put_hdr_edited.loc["Ratio", c] > 0
            ]
            p_matrix = build_ratio_matrix(option_df, "PE", put_strikes_matrix, put_pairs, price_mode)
            if p_matrix.empty:
                st.info("No put matrix data for the current strikes/columns.")
            else:
                st.dataframe(style_matrix(p_matrix), use_container_width=True, height=450)

except Exception as ex:
    st.error(f"Application Error: {ex}")
    with st.expander("Technical details"):
        import traceback

        st.code(traceback.format_exc())
