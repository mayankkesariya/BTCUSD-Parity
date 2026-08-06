import math
import requests
import pandas as pd
import streamlit as st

BASE_URL = "https://api.india.delta.exchange/v2"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "streamlit-option-chain-app"
}

st.set_page_config(page_title="Delta BTC Ratio Spread Scanner", layout="wide")
st.title("BTCUSD Parity")

@st.cache_data(ttl=30)
def fetch_all_products():
    all_rows = []
    after = None
    while True:
        params = {"contract_types": "call_options,put_options", "states": "live", "page_size": 100}
        if after:
            params["after"] = after
        r = requests.get(f"{BASE_URL}/products", params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        rows = data.get("result", [])
        all_rows.extend(rows)
        meta = data.get("meta", {}) or {}
        after = meta.get("after")
        if not after or not rows:
            break
    return all_rows

@st.cache_data(ttl=15)
def fetch_option_chain(underlying="BTC", expiry_date=None):
    params = {"contract_types": "call_options,put_options", "underlying_asset_symbols": underlying}
    if expiry_date:
        params["expiry_date"] = expiry_date
    r = requests.get(f"{BASE_URL}/tickers", params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json().get("result", [])

def get_btc_expiries(products):
    expiries = set()
    for p in products:
        if p.get("contract_type") not in ["call_options", "put_options"]:
            continue
        if "-BTC-" not in p.get("symbol", ""):
            continue
        settlement_time = p.get("settlement_time")
        if settlement_time:
            try:
                dt = pd.to_datetime(settlement_time, utc=True)
                expiries.add(dt.strftime("%d-%m-%Y"))
            except Exception:
                pass
    return sorted(expiries, key=lambda x: pd.to_datetime(x, format="%d-%m-%Y"))

def build_chain_table(option_rows):
    if not option_rows:
        return pd.DataFrame()
    df = pd.DataFrame(option_rows)
    df["strike_price"] = pd.to_numeric(df.get("strike_price"), errors="coerce")
    df["mark_price"] = pd.to_numeric(df.get("mark_price"), errors="coerce")
    df["spot_price"] = pd.to_numeric(df.get("spot_price"), errors="coerce")
    df["oi"] = pd.to_numeric(df.get("oi"), errors="coerce")
    df["volume"] = pd.to_numeric(df.get("volume"), errors="coerce")
    df["best_bid"] = pd.to_numeric(df["quotes"].apply(lambda x: (x or {}).get("best_bid")), errors="coerce")
    df["best_ask"] = pd.to_numeric(df["quotes"].apply(lambda x: (x or {}).get("best_ask")), errors="coerce")
    df["bid_iv"] = pd.to_numeric(df["quotes"].apply(lambda x: (x or {}).get("bid_iv")), errors="coerce")
    df["ask_iv"] = pd.to_numeric(df["quotes"].apply(lambda x: (x or {}).get("ask_iv")), errors="coerce")
    df["delta"] = pd.to_numeric(df["greeks"].apply(lambda x: (x or {}).get("delta")), errors="coerce")
    df["gamma"] = pd.to_numeric(df["greeks"].apply(lambda x: (x or {}).get("gamma")), errors="coerce")
    df["theta"] = pd.to_numeric(df["greeks"].apply(lambda x: (x or {}).get("theta")), errors="coerce")
    df["vega"] = pd.to_numeric(df["greeks"].apply(lambda x: (x or {}).get("vega")), errors="coerce")
    calls = df[df["contract_type"] == "call_options"].copy()
    puts = df[df["contract_type"] == "put_options"].copy()
    calls = calls.rename(columns={
        "symbol": "call_symbol", "best_bid": "call_bid", "best_ask": "call_ask", "mark_price": "call_mark",
        "oi": "call_oi", "volume": "call_volume", "delta": "call_delta", "gamma": "call_gamma",
        "theta": "call_theta", "vega": "call_vega", "bid_iv": "call_bid_iv", "ask_iv": "call_ask_iv"
    })
    puts = puts.rename(columns={
        "symbol": "put_symbol", "best_bid": "put_bid", "best_ask": "put_ask", "mark_price": "put_mark",
        "oi": "put_oi", "volume": "put_volume", "delta": "put_delta", "gamma": "put_gamma",
        "theta": "put_theta", "vega": "put_vega", "bid_iv": "put_bid_iv", "ask_iv": "put_ask_iv"
    })
    merged = pd.merge(
        calls[["strike_price", "spot_price", "call_symbol", "call_bid", "call_ask", "call_mark", "call_oi", "call_volume", "call_delta", "call_gamma", "call_theta", "call_vega", "call_bid_iv", "call_ask_iv"]],
        puts[["strike_price", "put_symbol", "put_bid", "put_ask", "put_mark", "put_oi", "put_volume", "put_delta", "put_gamma", "put_theta", "put_vega", "put_bid_iv", "put_ask_iv"]],
        on="strike_price", how="outer"
    ).sort_values("strike_price")
    col_order = [
        "call_symbol", "call_bid", "call_ask", "call_mark", "call_oi", "call_volume", "call_delta", "call_gamma",
        "call_theta", "call_vega", "call_bid_iv", "call_ask_iv", "strike_price", "put_bid", "put_ask", "put_mark",
        "put_oi", "put_volume", "put_delta", "put_gamma", "put_theta", "put_vega", "put_bid_iv", "put_ask_iv", "put_symbol"
    ]
    return merged[[c for c in col_order if c in merged.columns]]

def enrich_option_rows(option_rows):
    if not option_rows:
        return pd.DataFrame()
    df = pd.DataFrame(option_rows).copy()
    df["strike_price"] = pd.to_numeric(df.get("strike_price"), errors="coerce")
    df["mark_price"] = pd.to_numeric(df.get("mark_price"), errors="coerce")
    df["spot_price"] = pd.to_numeric(df.get("spot_price"), errors="coerce")
    df["oi"] = pd.to_numeric(df.get("oi"), errors="coerce")
    df["volume"] = pd.to_numeric(df.get("volume"), errors="coerce")
    df["best_bid"] = pd.to_numeric(df["quotes"].apply(lambda x: (x or {}).get("best_bid")), errors="coerce")
    df["best_ask"] = pd.to_numeric(df["quotes"].apply(lambda x: (x or {}).get("best_ask")), errors="coerce")
    df["bid_iv"] = pd.to_numeric(df["quotes"].apply(lambda x: (x or {}).get("bid_iv")), errors="coerce")
    df["ask_iv"] = pd.to_numeric(df["quotes"].apply(lambda x: (x or {}).get("ask_iv")), errors="coerce")
    df["delta"] = pd.to_numeric(df["greeks"].apply(lambda x: (x or {}).get("delta")), errors="coerce")
    return df

def premium_buy(row, mode):
    if mode == "mid" and pd.notna(row.get("best_bid")) and pd.notna(row.get("best_ask")):
        return (row.get("best_bid") + row.get("best_ask")) / 2
    return row.get("best_ask") if pd.notna(row.get("best_ask")) else row.get("mark_price")

def premium_sell(row, mode):
    if mode == "mid" and pd.notna(row.get("best_bid")) and pd.notna(row.get("best_ask")):
        return (row.get("best_bid") + row.get("best_ask")) / 2
    return row.get("best_bid") if pd.notna(row.get("best_bid")) else row.get("mark_price")

def find_ratio_spreads(df, option_type, qty_long, qty_short, min_credit, min_oi, min_volume, width_min, width_max, price_mode):
    sub = df[df["contract_type"] == option_type].copy()
    ascending = True if option_type == "call_options" else False
    sub = sub.sort_values("strike_price", ascending=ascending).reset_index(drop=True)
    if sub.empty:
        return pd.DataFrame()
    spot = pd.to_numeric(sub["spot_price"], errors="coerce").dropna()
    spot_value = float(spot.iloc[0]) if not spot.empty else math.nan
    rows = []
    for i in range(len(sub)):
        long_row = sub.iloc[i]
        if long_row.get("oi", 0) < min_oi or long_row.get("volume", 0) < min_volume:
            continue
        for j in range(i + 1, len(sub)):
            short_row = sub.iloc[j]
            if short_row.get("oi", 0) < min_oi or short_row.get("volume", 0) < min_volume:
                continue
            long_k = long_row["strike_price"]
            short_k = short_row["strike_price"]
            if option_type == "call_options":
                if pd.notna(spot_value) and not (long_k > spot_value and short_k > spot_value):
                    continue
                width = short_k - long_k
            else:
                if pd.notna(spot_value) and not (long_k < spot_value and short_k < spot_value):
                    continue
                width = long_k - short_k
            if pd.isna(long_k) or pd.isna(short_k) or width < width_min or width > width_max:
                continue
            buy_price = premium_buy(long_row, price_mode)
            sell_price = premium_sell(short_row, price_mode)
            if pd.isna(buy_price) or pd.isna(sell_price):
                continue
            net_credit = qty_short * sell_price - qty_long * buy_price
            if net_credit < min_credit:
                continue
            max_profit = net_credit + width * qty_long
            breakeven = short_k + max_profit / max(qty_short - qty_long, 1) if option_type == "call_options" else short_k - max_profit / max(qty_short - qty_long, 1)
            rows.append({
                "strategy": f"{qty_long}:{qty_short} | {qty_long}x{int(long_k)}{'C' if option_type == 'call_options' else 'P'} / -{qty_short}x{int(short_k)}{'C' if option_type == 'call_options' else 'P'}",
                "type": "Call Ratio Spread" if option_type == "call_options" else "Put Ratio Spread",
                "spot_price": spot_value,
                "long_symbol": long_row.get("symbol"),
                "short_symbol": short_row.get("symbol"),
                "long_strike": long_k,
                "short_strike": short_k,
                "width": width,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "net_credit": net_credit,
                "max_profit_at_short_strike": max_profit,
                "breakeven": breakeven,
                "long_oi": long_row.get("oi"),
                "short_oi": short_row.get("oi"),
                "long_volume": long_row.get("volume"),
                "short_volume": short_row.get("volume"),
                "long_delta": long_row.get("delta"),
                "short_delta": short_row.get("delta"),
                "long_strike_iv": ((long_row.get("ask_iv") if pd.notna(long_row.get("ask_iv")) else long_row.get("bid_iv")) * 100),
                "short_strike_iv": ((short_row.get("bid_iv") if pd.notna(short_row.get("bid_iv")) else short_row.get("ask_iv")) * 100),
                "iv_difference": (((long_row.get("ask_iv") if pd.notna(long_row.get("ask_iv")) else long_row.get("bid_iv")) - (short_row.get("bid_iv") if pd.notna(short_row.get("bid_iv")) else short_row.get("ask_iv"))) * 100),
                "risk_note": "Unlimited tail risk" if qty_short > qty_long else "Bounded",
            })
    res = pd.DataFrame(rows)
    if not res.empty:
        res = res.sort_values(["net_credit", "width"], ascending=[False, False]).reset_index(drop=True)
    return res

def find_atm_width_ratios(df, option_type, atm_strike, qty_long, qty_short, widths, price_mode):
    """
    Anchors the long leg at atm_strike (qty_long) and, for each requested width,
    finds the nearest available short strike (qty_short) at that distance from ATM.
    Returns one row per width. New function, does not alter find_ratio_spreads.
    """
    sub = df[df["contract_type"] == option_type].copy()
    sub["strike_price"] = pd.to_numeric(sub["strike_price"], errors="coerce")
    sub = sub.dropna(subset=["strike_price"]).sort_values("strike_price").reset_index(drop=True)
    if sub.empty or atm_strike is None:
        return pd.DataFrame()

    long_matches = sub[sub["strike_price"] == atm_strike]
    long_row = long_matches.iloc[0] if not long_matches.empty else sub.iloc[(sub["strike_price"] - atm_strike).abs().idxmin()]
    long_strike = long_row["strike_price"]
    buy_price = premium_buy(long_row, price_mode)

    available_strikes = sub["strike_price"]
    rows = []
    for w in widths:
        target = long_strike + w if option_type == "call_options" else long_strike - w
        short_strike = available_strikes.iloc[(available_strikes - target).abs().idxmin()]
        if short_strike == long_strike:
            continue
        short_row = sub[sub["strike_price"] == short_strike].iloc[0]
        sell_price = premium_sell(short_row, price_mode)
        if pd.isna(buy_price) or pd.isna(sell_price):
            continue
        actual_width = (short_strike - long_strike) if option_type == "call_options" else (long_strike - short_strike)
        net_credit = qty_short * sell_price - qty_long * buy_price
        max_profit = net_credit + actual_width * qty_long
        denom = max(qty_short - qty_long, 1)
        breakeven = short_strike + max_profit / denom if option_type == "call_options" else short_strike - max_profit / denom
        long_iv = long_row.get("ask_iv") if pd.notna(long_row.get("ask_iv")) else long_row.get("bid_iv")
        short_iv = short_row.get("bid_iv") if pd.notna(short_row.get("bid_iv")) else short_row.get("ask_iv")
        rows.append({
            "Requested Width": w,
            "Actual Width": actual_width,
            "Long Strike": long_strike,
            "Short Strike": short_strike,
            "Buy Price": buy_price,
            "Sell Price": sell_price,
            "Net Credit": net_credit,
            "Max Profit": max_profit,
            "Breakeven": breakeven,
            "Long IV%": long_iv * 100 if pd.notna(long_iv) else None,
            "Short IV%": short_iv * 100 if pd.notna(short_iv) else None,
            "IV Diff": (long_iv - short_iv) * 100 if pd.notna(long_iv) and pd.notna(short_iv) else None,
            "Long OI": long_row.get("oi"),
            "Short OI": short_row.get("oi"),
            "Long Vol": long_row.get("volume"),
            "Short Vol": short_row.get("volume"),
        })
    return pd.DataFrame(rows)

def build_ratio_matrix(df, option_type, base_strikes, width_ratio_pairs, price_mode):
    """
    Builds a Strike x Width matrix (rows = base_strikes used as the long leg,
    columns = each (width, ratio) pair). Each cell is the Net Credit for
    Buy 1 lot @ row strike / Sell `ratio` lots @ nearest strike (row strike +/- width).
    New function - does not alter find_ratio_spreads or find_atm_width_ratios.
    """
    sub = df[df["contract_type"] == option_type].copy()
    sub["strike_price"] = pd.to_numeric(sub["strike_price"], errors="coerce")
    sub = sub.dropna(subset=["strike_price"]).sort_values("strike_price").reset_index(drop=True)
    if sub.empty or not base_strikes or not width_ratio_pairs:
        return pd.DataFrame()

    col_tuples = [(f"{int(w)}", f"1:{int(r)}") for w, r in width_ratio_pairs]
    columns = pd.MultiIndex.from_tuples(col_tuples, names=["Width", "Ratio"])

    data_rows = []
    for base in base_strikes:
        long_matches = sub[sub["strike_price"] == base]
        if long_matches.empty:
            data_rows.append([None] * len(width_ratio_pairs))
            continue
        long_row = long_matches.iloc[0]
        buy_price = premium_buy(long_row, price_mode)
        candidates = sub[sub["strike_price"] > base] if option_type == "call_options" else sub[sub["strike_price"] < base]
        row_vals = []
        for w, r in width_ratio_pairs:
            if candidates.empty or pd.isna(buy_price):
                row_vals.append(None)
                continue
            target = base + w if option_type == "call_options" else base - w
            nearest_idx = (candidates["strike_price"] - target).abs().idxmin()
            short_row = candidates.loc[nearest_idx]
            sell_price = premium_sell(short_row, price_mode)
            if pd.isna(sell_price):
                row_vals.append(None)
                continue
            net_credit = r * sell_price - 1 * buy_price
            row_vals.append(round(float(net_credit), 2))
        data_rows.append(row_vals)

    matrix = pd.DataFrame(data_rows, index=[f"{int(b)}" for b in base_strikes], columns=columns)
    matrix.index.name = "Strike"
    return matrix

def format_numeric_columns(df):
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_numeric_dtype(out[col]):
            out[col] = out[col].round(4)
    return out



st.markdown(f"<script>setTimeout(function(){{window.location.reload();}}, {refresh_seconds * 1000});</script>", unsafe_allow_html=True)

if st.button("Refresh Now"):
    st.cache_data.clear()

try:
    products = fetch_all_products()
    expiries = get_btc_expiries(products)
    if not expiries:
        st.error("No BTC option expiries found.")
        st.stop()

    selected_expiry = st.selectbox("Select Expiry", expiries, index=0)
    option_rows = fetch_option_chain("BTC", selected_expiry)
    chain = build_chain_table(option_rows)
    option_df = enrich_option_rows(option_rows)

    c1, c2 = st.columns(2)
    spot_candidates = pd.to_numeric(pd.DataFrame(option_rows).get("spot_price"), errors="coerce").dropna()
    spot_value = float(spot_candidates.iloc[0]) if not spot_candidates.empty else None
    c1.metric("Spot Price", f"{spot_value:,.2f}" if spot_value is not None else "NA")
    c2.metric("Selected Expiry", selected_expiry)


    st.title("BTCUSD Skew Curve")
    
    st.subheader("Call Side")
    st.caption("You can manually edit the input values for Farak & Ratio")

    matrix_call_sub = option_df[option_df["contract_type"] == "call_options"].copy()
    matrix_call_sub["strike_price"] = pd.to_numeric(matrix_call_sub["strike_price"], errors="coerce")
    matrix_call_sub = matrix_call_sub.dropna(subset=["strike_price"])

    if matrix_call_sub.empty or spot_value is None:
        st.warning("No call strikes available to build the matrix.")
    else:
        atm_strike_matrix = float(matrix_call_sub.loc[(matrix_call_sub["strike_price"] - spot_value).abs().idxmin(), "strike_price"])
        all_call_strikes = sorted(matrix_call_sub["strike_price"].unique())
        strikes_from_atm = [s for s in all_call_strikes if s >= atm_strike_matrix]

        

        call_slot_labels = [str(i + 1) for i in range(6)]
        default_call_header = pd.DataFrame(
            [[800, 1000, 1200, 1400, 1600, 1800], [10, 10, 10, 10, 10, 10]],
            index=["Farak", "Ratio"],
            columns=call_slot_labels
        )
        call_header_edited = st.data_editor(
            default_call_header,
            use_container_width=True,
            key="call_ratio_matrix_header",
            column_config={c: st.column_config.NumberColumn(c, min_value=0, step=10) for c in call_slot_labels}
        )

        call_column_defs = [
            (float(call_header_edited.loc["Farak", c]), int(call_header_edited.loc["Ratio", c]))
            for c in call_slot_labels
            if pd.notna(call_header_edited.loc["Farak", c]) and pd.notna(call_header_edited.loc["Ratio", c])
            and call_header_edited.loc["Farak", c] > 0 and call_header_edited.loc["Ratio", c] > 0
        ]

        if not call_column_defs:
            st.info("Set at least one Width / Ratio pair above (both > 0) to build the matrix.")
        else:
            call_matrix = build_ratio_matrix(option_df, "call_options", strikes_from_atm, call_column_defs, price_mode)
            if call_matrix.empty:
                st.warning("No data available to build the matrix.")
            else:
                st.dataframe(call_matrix, use_container_width=True, height=560)
               

    # ==========================================================================
    # NEW SECTION (added below the Call matrix, does not modify anything above)
    # Strike x Width x Ratio matrix, Put side — mirrors the Call matrix above:
    # editable Width / Ratio rows sit on top of the strike grid, which runs
    # from ATM down to the last available put strike.
    # ==========================================================================
    st.markdown("---")
    st.subheader("Put Side")
    st.caption("You can manually edit the input values for Farak & Ratio")

    matrix_put_sub = option_df[option_df["contract_type"] == "put_options"].copy()
    matrix_put_sub["strike_price"] = pd.to_numeric(matrix_put_sub["strike_price"], errors="coerce")
    matrix_put_sub = matrix_put_sub.dropna(subset=["strike_price"])

    if matrix_put_sub.empty or spot_value is None:
        st.warning("No put strikes available to build the matrix.")
    else:
        atm_strike_matrix_put = float(matrix_put_sub.loc[(matrix_put_sub["strike_price"] - spot_value).abs().idxmin(), "strike_price"])
        all_put_strikes = sorted(matrix_put_sub["strike_price"].unique())
        strikes_from_atm_put = [s for s in reversed(all_put_strikes) if s <= atm_strike_matrix_put]

        

        put_slot_labels = [str(i + 1) for i in range(6)]
        default_put_header = pd.DataFrame(
            [[800, 1000, 1200, 1400, 1600, 1800], [10, 10, 10, 10, 10, 10]],
            index=["Farak", "Ratio"],
            columns=put_slot_labels
        )
        put_header_edited = st.data_editor(
            default_put_header,
            use_container_width=True,
            key="put_ratio_matrix_header",
            column_config={c: st.column_config.NumberColumn(c, min_value=0, step=10) for c in put_slot_labels}
        )

        put_column_defs = [
            (float(put_header_edited.loc["Farak", c]), int(put_header_edited.loc["Ratio", c]))
            for c in put_slot_labels
            if pd.notna(put_header_edited.loc["Farak", c]) and pd.notna(put_header_edited.loc["Ratio", c])
            and put_header_edited.loc["Farak", c] > 0 and put_header_edited.loc["Ratio", c] > 0
        ]

        if not put_column_defs:
            st.info("Set at least one Width / Ratio pair above (both > 0) to build the matrix.")
        else:
            put_matrix = build_ratio_matrix(option_df, "put_options", strikes_from_atm_put, put_column_defs, price_mode)
            if put_matrix.empty:
                st.warning("No data available to build the matrix.")
            else:
                st.dataframe(put_matrix, use_container_width=True, height=560)
                

except requests.HTTPError as e:
    st.error(f"HTTP error: {e}")
except Exception as e:
    st.error(f"Error: {e}")
