import io
import math
import time
import requests
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ==============================================================================
# Page Configuration & Title
# ==============================================================================
st.set_page_config(
    page_title="NIFTY Options Ratio Spread Matrix & Scanner",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📈 NIFTY Options Ratio Spread Matrix & Scanner")

# ==============================================================================
# Sidebar Inputs & Controls
# ==============================================================================
with st.sidebar:
    st.header("🔑 Authentication")
    raw_token = st.text_input(
        "Kite Web enctoken",
        type="password",
        help="Paste your active Zerodha Kite Web enctoken from browser DevTools cookies.",
    )
    # Automatically clean up any accidental spaces copied with the token
    kite_enctoken = raw_token.strip() if raw_token else ""

    st.header("⚙️ Configuration")
    underlying = st.selectbox(
        "Underlying Index", ["NIFTY", "BANKNIFTY", "FINNIFTY"], index=0
    )
    # Increased default refresh time to prevent HTTP 429 rate limits
    refresh_seconds = st.slider("Auto Refresh (seconds)", 5, 60, 10, 1)

    st.markdown("---")
    st.header("🔍 Scanner Controls")
    strategy_side = st.selectbox("Strategy Side", ["Call Ratio Spread", "Put Ratio Spread"])
    ratio_start = st.number_input("Ratio From (1:N)", min_value=1, max_value=20, value=2, step=1)
    ratio_end = st.number_input("Ratio Till (1:N)", min_value=1, max_value=20, value=10, step=1)
    price_mode = st.selectbox("Price Mode", ["Bid/Ask (Live Depth)", "Last Price / Mark Price"], index=0)
    min_credit = st.number_input("Minimum Net Credit (Pts)", min_value=0.0, value=10.0, step=1.0)
    width_min = st.number_input("Minimum Farak (Width)", min_value=50, value=100, step=50)
    width_max = st.number_input("Maximum Farak (Width)", min_value=50, value=1000, step=50)
    max_rows = st.slider("Top Opportunities", 5, 50, 20, 5)

# Trigger Auto Refresh
refresh_count = st_autorefresh(interval=refresh_seconds * 1000, key="nifty_matrix_autorefresh")

# ==============================================================================
# Helper Data Fetching Functions
# ==============================================================================
@st.cache_data(ttl=3600)
def fetch_nfo_instruments():
    """Downloads NFO master instrument list from Zerodha."""
    url = "https://api.kite.trade/instruments/NFO"
    response = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    df = pd.read_csv(io.StringIO(response.text))
    if df.empty:
        raise RuntimeError("Instrument master is empty.")
    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date
    return df

def get_spot_symbol(symbol_name):
    mapping = {
        "NIFTY": "NSE:NIFTY 50",
        "BANKNIFTY": "NSE:NIFTY BANK",
        "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    }
    return mapping.get(symbol_name, "NSE:NIFTY 50")

def fetch_kite_quotes(symbols_list, enctoken):
    """Fetch live full quotes using the Kite quote endpoint with a Kite Web enctoken.

    IMPORTANT: enctoken authentication uses the normal Kite quote endpoint
    (api.kite.trade/quote) with the Authorization header. The /oms/quote
    endpoint was causing HTTP 400 InputException for this application.
    """
    if not enctoken or not symbols_list:
        return {}

    session = requests.Session()
    session.headers.update({
        "Authorization": f"enctoken {enctoken.strip()}",
        "X-Kite-Version": "3",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://kite.zerodha.com/",
    })

    quotes_data = {}
    # Kite quote API supports up to 500 instruments. Keep a smaller batch
    # so a single malformed symbol cannot prevent the complete matrix.
    chunk_size = 200

    def request_chunk(chunk):
        params = {"i": chunk}
        return session.get("https://api.kite.trade/quote", params=params, timeout=20)

    def load_chunk(chunk):
        if not chunk:
            return

        try:
            resp = request_chunk(chunk)
        except requests.RequestException as exc:
            raise RuntimeError(f"Kite quote connection error: {exc}")

        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError:
                raise RuntimeError(f"Kite returned invalid JSON: {resp.text[:500]}")
            if payload.get("status") != "success":
                raise RuntimeError(f"Kite quote error: {payload}")
            data = payload.get("data", {})
            if isinstance(data, dict):
                quotes_data.update(data)
            return

        if resp.status_code in (401, 403):
            raise RuntimeError("Kite enctoken is expired/invalid (HTTP %s). Copy a fresh enctoken from Kite Web." % resp.status_code)

        # HTTP 400 can be caused by one malformed instrument in a batch.
        # Split recursively until the valid symbols are recovered.
        if resp.status_code == 400 and len(chunk) > 1:
            mid = len(chunk) // 2
            load_chunk(chunk[:mid])
            load_chunk(chunk[mid:])
            return

        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:500]
        raise RuntimeError(f"Kite quote HTTP {resp.status_code}: {detail} | Symbol(s): {chunk[:3]}")

    for start_idx in range(0, len(symbols_list), chunk_size):
        load_chunk(symbols_list[start_idx:start_idx + chunk_size])
        # Quote requests are rate-limited. Do not hammer the endpoint.
        time.sleep(1.05)

    return quotes_data

def premium_buy(row, mode):
    if mode == "Bid/Ask (Live Depth)" and pd.notna(row.get("best_ask")) and row.get("best_ask") > 0:
        return float(row["best_ask"])
    return float(row.get("last_price", 0.0))

def premium_sell(row, mode):
    if mode == "Bid/Ask (Live Depth)" and pd.notna(row.get("best_bid")) and row.get("best_bid") > 0:
        return float(row["best_bid"])
    return float(row.get("last_price", 0.0))

# ==============================================================================
# Analytical Core Functions
# ==============================================================================
def find_ratio_spreads(df, option_type, qty_long, qty_short, min_credit, width_min, width_max, price_mode):
    sub = df[df["instrument_type"] == option_type].copy()
    ascending = True if option_type == "CE" else False
    sub = sub.sort_values("strike", ascending=ascending).reset_index(drop=True)
    if sub.empty:
        return pd.DataFrame()

    spot_value = float(sub["spot_price"].iloc[0]) if "spot_price" in sub.columns and not sub["spot_price"].empty else math.nan
    rows = []

    for i in range(len(sub)):
        long_row = sub.iloc[i]
        long_k = float(long_row["strike"])

        for j in range(i + 1, len(sub)):
            short_row = sub.iloc[j]
            short_k = float(short_row["strike"])

            if option_type == "CE":
                if pd.notna(spot_value) and not (long_k >= spot_value and short_k > long_k):
                    continue
                width = short_k - long_k
            else:
                if pd.notna(spot_value) and not (long_k <= spot_value and short_k < long_k):
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

            max_profit = net_credit + (width * qty_long)
            denom = max(qty_short - qty_long, 1)
            breakeven = short_k + (max_profit / denom) if option_type == "CE" else short_k - (max_profit / denom)

            rows.append({
                "ratio": f"1:{qty_short}",
                "long_strike": long_k,
                "short_strike": short_k,
                "width": width,
                "buy_price": buy_p,
                "sell_price": sell_p,
                "net_credit": net_credit,
                "max_profit": max_profit,
                "breakeven": breakeven,
            })

    res = pd.DataFrame(rows)
    if not res.empty:
        res = res.sort_values(["net_credit", "width"], ascending=[False, False]).reset_index(drop=True)
    return res

def find_atm_width_ratios(df, option_type, atm_strike, qty_long, qty_short, widths, price_mode):
    sub = df[df["instrument_type"] == option_type].copy()
    sub["strike"] = pd.to_numeric(sub["strike"], errors="coerce")
    sub = sub.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)

    if sub.empty or atm_strike is None:
        return pd.DataFrame()

    long_matches = sub[sub["strike"] == atm_strike]
    long_row = long_matches.iloc[0] if not long_matches.empty else sub.iloc[(sub["strike"] - atm_strike).abs().idxmin()]
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
        actual_width = (short_strike - long_strike) if option_type == "CE" else (long_strike - short_strike)
        net_credit = qty_short * sell_p - qty_long * buy_p

        rows.append({
            "Width": actual_width,
            "Long Strike": long_strike,
            "Short Strike": short_strike,
            "Buy Price": round(buy_p, 2),
            "Sell Price": round(sell_p, 2),
            "Net Credit": round(net_credit, 2),
        })
    return pd.DataFrame(rows)

def build_ratio_matrix(df, option_type, base_strikes, width_ratio_pairs, price_mode):
    sub = df[df["instrument_type"] == option_type].copy()
    sub["strike"] = pd.to_numeric(sub["strike"], errors="coerce")
    sub = sub.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)

    if sub.empty or not base_strikes or not width_ratio_pairs:
        return pd.DataFrame()

    col_tuples = [("Mark Price", "")] + [(f"{int(w)}", f"1:{int(r)}") for w, r in width_ratio_pairs]
    columns = pd.MultiIndex.from_tuples(col_tuples, names=["Width", "Ratio"])

    data_rows = []
    for base in base_strikes:
        long_matches = sub[sub["strike"] == base]
        if long_matches.empty:
            data_rows.append([math.nan] + [math.nan] * len(width_ratio_pairs))
            continue

        long_row = long_matches.iloc[0]
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

            net_credit = r * sell_p - 1 * buy_p
            row_vals.append(round(float(net_credit), 2))

        data_rows.append(row_vals)

    matrix = pd.DataFrame(data_rows, index=[f"{int(b)}" for b in base_strikes], columns=columns)
    matrix.index.name = "Strike"
    return matrix

# ==============================================================================
# Main Streamlit Application Pipeline
# ==============================================================================
if not kite_enctoken:
    st.info("👈 Please enter your Zerodha Kite Web **enctoken** in the sidebar to load live market data.")
    st.stop()

try:
    instruments_df = fetch_nfo_instruments()
    
    # Filter Expiries
    underlying_inst = instruments_df[
        (instruments_df["name"] == underlying) &
        (instruments_df["instrument_type"].isin(["CE", "PE"]))
    ]
    expiries = sorted(underlying_inst["expiry"].dropna().unique())

    if not expiries:
        st.error(f"No active option expiries found for {underlying}.")
        st.stop()

    selected_expiry = st.selectbox("Select Expiry", expiries, index=0, format_func=lambda x: x.strftime("%d-%b-%Y"))

    # Filter legs for selected expiry
    leg_df = underlying_inst[underlying_inst["expiry"] == selected_expiry].copy()
    leg_symbols = [f"NFO:{s}" for s in leg_df["tradingsymbol"].tolist()]
    spot_symbol = get_spot_symbol(underlying)

    # Fetch Quotes
    all_symbols = [spot_symbol] + leg_symbols
    with st.spinner(f"Fetching live quotes for {len(all_symbols)} instruments..."):
        quotes = fetch_kite_quotes(all_symbols, kite_enctoken)

    if not quotes:
        st.error("Data fetch returned no quotes. Please paste a fresh enctoken and retry.")
        st.stop()

    # Extract Spot Price
    spot_quote = quotes.get(spot_symbol, {})
    spot_value = spot_quote.get("last_price", None)
    if spot_value is None or float(spot_value) <= 0:
        st.error("Spot quote was not returned by Kite Web. The token may be invalid or the quote response changed.")
        with st.expander("Debug information"):
            st.write({"spot_symbol": spot_symbol, "quotes_received": len(quotes), "sample_keys": list(quotes.keys())[:10]})
        st.stop()

    # Populate Option Legs DataFrame
    enriched_rows = []
    for _, row in leg_df.iterrows():
        key = f"NFO:{row['tradingsymbol']}"
        q = quotes.get(key, {})
        depth = q.get("depth", {})
        buy_depth = depth.get("buy", [{}])[0]
        sell_depth = depth.get("sell", [{}])[0]

        enriched_rows.append({
            "tradingsymbol": row["tradingsymbol"],
            "strike": float(row["strike"]),
            "instrument_type": row["instrument_type"],
            "last_price": q.get("last_price", 0.0),
            "best_bid": buy_depth.get("price", 0.0),
            "best_ask": sell_depth.get("price", 0.0),
            "spot_price": spot_value,
        })

    option_df = pd.DataFrame(enriched_rows)

    # Calculate Straddle Price
    straddle_price = None
    if spot_value:
        ce_sub = option_df[option_df["instrument_type"] == "CE"]
        pe_sub = option_df[option_df["instrument_type"] == "PE"]
        if not ce_sub.empty and not pe_sub.empty:
            atm_ce = ce_sub.loc[(ce_sub["strike"] - spot_value).abs().idxmin()]
            atm_pe = pe_sub.loc[(pe_sub["strike"] - spot_value).abs().idxmin()]
            straddle_price = premium_buy(atm_ce, price_mode) + premium_buy(atm_pe, price_mode)

    # Top Metrics Banner
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Spot Price", f"{spot_value:,.2f}" if spot_value else "N/A")
    m2.metric("ATM Straddle Premium", f"{straddle_price:,.2f}" if straddle_price else "N/A")
    m3.metric("Selected Expiry", selected_expiry.strftime("%d-%b-%Y"))
    m4.metric("Quotes Loaded", f"{len(quotes):,}")

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
        frame = find_ratio_spreads(option_df, opt_type_code, 1, short_r, min_credit, width_min, width_max, price_mode)
        if not frame.empty:
            scanner_frames.append(frame)

    opps = pd.concat(scanner_frames, ignore_index=True) if scanner_frames else pd.DataFrame()

    if opps.empty:
        st.warning("No ratio spread opportunities found matching the sidebar criteria.")
    else:
        display_opps = opps.head(max_rows).rename(columns={
            "ratio": "Ratio",
            "long_strike": "Long Strike",
            "short_strike": "Short Strike",
            "width": "Farak",
            "buy_price": "Buy Price",
            "sell_price": "Sell Price",
            "net_credit": "Net Credit",
            "max_profit": "Max Profit",
            "breakeven": "Breakeven"
        })
        st.dataframe(display_opps, use_container_width=True, hide_index=True)

    # ==========================================================================
    # Section 2: ATM Curve Finder (Call & Put Side)
    # ==========================================================================
    st.markdown("---")
    st.subheader("🎯 ATM Curve Finder - Call Side")

    call_sub = option_df[option_df["instrument_type"] == "CE"].sort_values("strike")
    if call_sub.empty or spot_value is None:
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
        atm_call_table = find_atm_width_ratios(option_df, "CE", atm_call_strike, 1, int(c_ratio), call_widths, price_mode)
        st.dataframe(atm_call_table, use_container_width=True, hide_index=True)

    st.subheader("🎯 ATM Curve Finder - Put Side")
    put_sub = option_df[option_df["instrument_type"] == "PE"].sort_values("strike")
    if put_sub.empty or spot_value is None:
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
        atm_put_table = find_atm_width_ratios(option_df, "PE", atm_put_strike, 1, int(p_ratio), put_widths, price_mode)
        st.dataframe(atm_put_table, use_container_width=True, hide_index=True)

    # ==========================================================================
    # Section 3: Skew Curve Matrix (Call & Put Side)
    # ==========================================================================
    st.markdown("---")
    st.title("📊 NIFTY Skew Curve Matrix")

    col_slots = [str(i + 1) for i in range(6)]
    default_call_hdr = pd.DataFrame(
        [[100, 200, 300, 400, 500, 600], [10, 10, 10, 10, 10, 10]],
        index=["Farak", "Ratio"], columns=col_slots
    )

    st.subheader("Call Side Matrix")
    if not call_sub.empty and spot_value:
        atm_k_call = float(call_sub.loc[(call_sub["strike"] - spot_value).abs().idxmin(), "strike"])
        call_strikes_matrix = [s for s in sorted(call_sub["strike"].unique()) if s >= atm_k_call]

        call_hdr_edited = st.data_editor(default_call_hdr, key="call_matrix_hdr", use_container_width=True)

        call_pairs = [
            (float(call_hdr_edited.loc["Farak", c]), int(call_hdr_edited.loc["Ratio", c]))
            for c in col_slots if call_hdr_edited.loc["Farak", c] > 0 and call_hdr_edited.loc["Ratio", c] > 0
        ]

        c_matrix = build_ratio_matrix(option_df, "CE", call_strikes_matrix, call_pairs, price_mode)
        st.dataframe(c_matrix, use_container_width=True, height=450)

    st.subheader("Put Side Matrix")
    if not put_sub.empty and spot_value:
        atm_k_put = float(put_sub.loc[(put_sub["strike"] - spot_value).abs().idxmin(), "strike"])
        put_strikes_matrix = [s for s in sorted(put_sub["strike"].unique(), reverse=True) if s <= atm_k_put]

        put_hdr_edited = st.data_editor(default_call_hdr, key="put_matrix_hdr", use_container_width=True)
        put_pairs = [
            (float(put_hdr_edited.loc["Farak", c]), int(put_hdr_edited.loc["Ratio", c]))
            for c in col_slots if put_hdr_edited.loc["Farak", c] > 0 and put_hdr_edited.loc["Ratio", c] > 0
        ]

        p_matrix = build_ratio_matrix(option_df, "PE", put_strikes_matrix, put_pairs, price_mode)
        st.dataframe(p_matrix, use_container_width=True, height=450)

except Exception as ex:
    st.error(f"Application Error: {ex}")
    with st.expander("Technical details"):
        st.exception(ex)
