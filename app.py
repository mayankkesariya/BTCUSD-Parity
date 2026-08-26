import io
import math
import time
import requests
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

try:
    import pyotp
except ImportError:
    pyotp = None

# ==============================================================================
# NIFTY Options Ratio Spread Matrix & Scanner - Angel One SmartAPI version
# ==============================================================================
# AUTH:
# Angel One's SmartAPI does not offer a standalone "just paste an API key"
# market-data mode - even a Market Data-only app still authenticates through
# the same session login endpoint, it's just that the key is *scoped* to
# read-only market data (no order placement rights). So this app needs:
#   1. api_key      -> from your Angel One SmartAPI app (Market Data type)
#   2. client_code  -> your Angel One client / login ID
#   3. pin          -> your 4-digit MPIN (not your trading password)
#   4. totp_secret  -> the TOTP base32 secret shown once when you enabled
#                       2FA for SmartAPI (NOT the 6-digit code itself - the
#                       app generates fresh codes locally with pyotp so it
#                       can silently re-login on every auto-refresh without
#                       you retyping a code each time)
#
# The app calls loginByPassword once to get a jwtToken + feedToken, caches
# it in the session, and re-authenticates automatically if a call ever
# returns 401 (expired token) or on first load.
#
# The live NIFTY reference price is still estimated synthetically from
# CE/PE option prices (spot = strike + CE - PE) rather than requesting a
# separate index quote.
# ==============================================================================

st.set_page_config(
    page_title="NIFTY Options Ratio Spread Matrix & Scanner",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📈 NIFTY Options Ratio Spread Matrix & Scanner")

ANGEL_BASE_URL = "https://apiconnect.angelbroking.com"
SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

DEFAULT_HEADERS_STATIC = {
    "X-ClientLocalIP": "127.0.0.1",
    "X-ClientPublicIP": "127.0.0.1",
    "X-MACAddress": "00:00:00:00:00:00",
    "X-UserType": "USER",
    "X-SourceID": "WEB",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

with st.sidebar:
    st.header("🔑 ENC Token")
    api_key = st.text_input(
        "ENC Token",
        type="password",
        "codes automatically instead of you retyping one every refresh."
        ),
    ).strip()

    is_authenticated = bool(api_key and client_code and pin and totp_secret)

    if is_authenticated and pyotp is None:
        st.error("pyotp is not installed. Run: pip install pyotp")
        is_authenticated = False

    st.header("⚙️ Configuration")
    underlying = st.selectbox(
        "Underlying Index", ["NIFTY", "BANKNIFTY", "FINNIFTY"], index=0
    )
    refresh_seconds = st.slider("Auto Refresh (seconds)", 5, 60, 15, 1)

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
# Auth helpers
# ==============================================================================


def angel_login(api_key, client_code, pin, totp_secret):
    """Log in via loginByPassword and return (jwt_token, feed_token)."""
    totp_code = pyotp.TOTP(totp_secret).now()
    url = f"{ANGEL_BASE_URL}/rest/auth/angelbroking/user/v1/loginByPassword"
    headers = dict(DEFAULT_HEADERS_STATIC)
    headers["X-PrivateKey"] = api_key

    try:
        resp = requests.post(
            url,
            json={"clientcode": client_code, "password": pin, "totp": totp_code},
            headers=headers,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Angel One login connection error: {exc}")

    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError(f"Angel One login returned non-JSON response: {resp.text[:500]}")

    if not payload.get("status") or resp.status_code != 200:
        message = payload.get("message", payload)
        raise RuntimeError(
            f"Angel One login failed: {message}. Check client code, MPIN, and "
            "TOTP secret - if the TOTP secret is wrong every generated code "
            "will be rejected even though it 'looks' valid."
        )

    data = payload.get("data") or {}
    jwt_token = data.get("jwtToken")
    feed_token = data.get("feedToken")
    if not jwt_token:
        raise RuntimeError(f"Angel One login response missing jwtToken: {payload}")

    return jwt_token, feed_token


def get_session_tokens(api_key, client_code, pin, totp_secret, force=False):
    """Cache jwt/feed tokens in session_state; re-login when missing/forced."""
    cache_key = "angel_auth"
    cached = st.session_state.get(cache_key)

    if not force and cached and cached.get("api_key") == api_key:
        # Re-login roughly every 6 hours as a safety margin even without a 401.
        if time.time() - cached.get("ts", 0) < 6 * 3600:
            return cached["jwt_token"], cached["feed_token"]

    jwt_token, feed_token = angel_login(api_key, client_code, pin, totp_secret)
    st.session_state[cache_key] = {
        "api_key": api_key,
        "jwt_token": jwt_token,
        "feed_token": feed_token,
        "ts": time.time(),
    }
    return jwt_token, feed_token


def angel_headers(api_key, jwt_token):
    headers = dict(DEFAULT_HEADERS_STATIC)
    headers["Authorization"] = f"Bearer {jwt_token}"
    headers["X-PrivateKey"] = api_key
    return headers


# ==============================================================================
# Instrument / Quote helpers
# ==============================================================================


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_nfo_instruments():
    resp = requests.get(SCRIP_MASTER_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    raw = resp.json()
    df = pd.DataFrame(raw)
    if df.empty:
        raise RuntimeError("Angel One scrip master is empty.")

    required = {"token", "symbol", "name", "expiry", "strike", "instrumenttype", "exch_seg"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Scrip master is missing columns: {sorted(missing)}")

    df = df[df["exch_seg"] == "NFO"].copy()
    df = df[df["instrumenttype"].isin(["OPTIDX", "OPTSTK"])].copy()

    # Strike is stored in paise (rupees * 100) in Angel's scrip master.
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce") / 100.0
    df["token"] = pd.to_numeric(df["token"], errors="coerce")
    df["expiry"] = pd.to_datetime(df["expiry"], format="%d%b%Y", errors="coerce").dt.date

    # instrument_type (CE/PE) isn't a separate column - derive it from the
    # trading symbol suffix, e.g. NIFTY28AUG25000CE.
    df["instrument_type"] = df["symbol"].str[-2:]
    df = df[df["instrument_type"].isin(["CE", "PE"])].copy()

    df = df.rename(columns={"token": "instrument_token", "symbol": "tradingsymbol"})
    return df


def _request_quote(jwt_token, api_key, tokens):
    url = f"{ANGEL_BASE_URL}/rest/secure/angelbroking/market/v1/quote"
    body = {"mode": "FULL", "exchangeTokens": {"NFO": [str(t) for t in tokens]}}
    return requests.post(url, json=body, headers=angel_headers(api_key, jwt_token), timeout=20)


def fetch_angel_quotes(instrument_tokens, api_key, client_code, pin, totp_secret):
    """Fetch FULL-mode quotes (ltp + market depth) for a list of NFO tokens.

    SmartAPI's FULL mode quote endpoint caps out around 50 instruments per
    call, so requests are batched. A 401 triggers exactly one forced
    re-login+retry per batch before giving up, so an expired session doesn't
    silently return empty data.
    """
    if not instrument_tokens:
        return {}, []

    jwt_token, _ = get_session_tokens(api_key, client_code, pin, totp_secret)

    quotes = {}
    bad_tokens = []
    batch_size = 50

    def load_chunk(chunk, retried=False):
        if not chunk:
            return

        nonlocal jwt_token
        try:
            resp = _request_quote(jwt_token, api_key, chunk)
        except requests.RequestException as exc:
            raise RuntimeError(f"Angel One quote connection error: {exc}")

        if resp.status_code == 401 and not retried:
            jwt_token, _ = get_session_tokens(api_key, client_code, pin, totp_secret, force=True)
            load_chunk(chunk, retried=True)
            return

        if resp.status_code == 429:
            time.sleep(1.5)
            load_chunk(chunk, retried=retried)
            return

        try:
            payload = resp.json()
        except ValueError:
            raise RuntimeError(f"Angel One quote returned non-JSON response: {resp.text[:500]}")

        if resp.status_code != 200 or not payload.get("status"):
            if len(chunk) == 1:
                bad_tokens.append(str(chunk[0]))
                return
            mid = max(1, len(chunk) // 2)
            load_chunk(chunk[:mid], retried=retried)
            load_chunk(chunk[mid:], retried=retried)
            return

        data = payload.get("data") or {}
        for item in data.get("fetched", []) or []:
            token = str(item.get("symbolToken"))
            quotes[token] = item

        for failed in data.get("unfetched", []) or []:
            bad_tokens.append(str(failed.get("symbolToken", failed)))

    for start in range(0, len(instrument_tokens), batch_size):
        load_chunk(instrument_tokens[start : start + batch_size])
        if start + batch_size < len(instrument_tokens):
            time.sleep(0.35)

    return quotes, bad_tokens


def depth_top(item, side):
    depth = (item.get("depth") or {}).get(side) or []
    if depth and isinstance(depth[0], dict):
        try:
            p = float(depth[0].get("price", 0) or 0)
            return p if p > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def enrich_option_quotes(leg_df, quotes):
    rows = []
    for _, row in leg_df.iterrows():
        q = quotes.get(str(int(row["instrument_token"])), {})

        last_price = q.get("ltp", 0.0)
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
                "best_bid": depth_top(q, "buy"),
                "best_ask": depth_top(q, "sell"),
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
        "👈 Enter your Angel One SmartAPI **API Key** in the sidebar to load live data. "
        "A Market Data-type app key is enough - no order-placement "
        "permissions are needed for this dashboard."
    )
    st.stop()

try:
    instruments_df = fetch_nfo_instruments()

    today = pd.Timestamp.now(tz="Asia/Kolkata").date()
    underlying_inst = instruments_df[
        (instruments_df["name"] == underlying)
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

    instrument_tokens = leg_df["instrument_token"].tolist()

    with st.spinner(f"Fetching live option quotes for {len(instrument_tokens)} instruments..."):
        quotes, bad_tokens = fetch_angel_quotes(
            instrument_tokens, api_key, client_code, pin, totp_secret
        )

    if not quotes:
        st.error(
            "No live option quotes were returned. Check that the MPIN and "
            "TOTP secret are correct, and that this API key is entitled to "
            "market data for NFO."
        )
        if bad_tokens:
            st.warning(f"Skipped invalid/unfetched tokens: {bad_tokens[:10]}")
        st.stop()

    option_df = enrich_option_quotes(leg_df, quotes)
    option_df = option_df[option_df["last_price"] > 0].copy()
    if option_df.empty:
        st.error("Angel One returned no usable option LTPs for the selected expiry.")
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

    if bad_tokens:
        st.caption(f"Skipped {len(bad_tokens)} invalid/unfetched option tokens.")

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
