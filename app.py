import requests
import pandas as pd
import streamlit as st

BASE_URL = "https://api.india.delta.exchange/v2"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "streamlit-option-chain-app"
}

PRICE_MODE = "Default"

st.set_page_config(page_title="BTCUSD Parity", layout="wide")
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

def build_ratio_matrix(df, option_type, base_strikes, width_ratio_pairs, price_mode):
    """
    Builds a Strike x Width matrix (rows = base_strikes used as the long leg,
    columns = Mark Price then each (width, ratio) pair). Each ratio cell is the
    Net Credit for Buy 1 lot @ row strike / Sell `ratio` lots @ nearest strike
    (row strike +/- width).
    """
    sub = df[df["contract_type"] == option_type].copy()
    sub["strike_price"] = pd.to_numeric(sub["strike_price"], errors="coerce")
    sub = sub.dropna(subset=["strike_price"]).sort_values("strike_price").reset_index(drop=True)
    if sub.empty or not base_strikes or not width_ratio_pairs:
        return pd.DataFrame()

    col_tuples = [("Mark Price", "")] + [(f"{int(w)}", f"1:{int(r)}") for w, r in width_ratio_pairs]
    columns = pd.MultiIndex.from_tuples(col_tuples, names=["Width", "Ratio"])

    data_rows = []
    for base in base_strikes:
        long_matches = sub[sub["strike_price"] == base]
        if long_matches.empty:
            data_rows.append([None] + [None] * len(width_ratio_pairs))
            continue
        long_row = long_matches.iloc[0]
        buy_price = premium_buy(long_row, price_mode)
        mark_price = long_row.get("mark_price")
        candidates = sub[sub["strike_price"] > base] if option_type == "call_options" else sub[sub["strike_price"] < base]
        row_vals = [round(float(mark_price), 2) if pd.notna(mark_price) else None]
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

try:
    products = fetch_all_products()
    expiries = get_btc_expiries(products)
    if not expiries:
        st.error("No BTC option expiries found.")
        st.stop()

    selected_expiry = st.selectbox("Select expiry", expiries, index=0)
    option_rows = fetch_option_chain("BTC", selected_expiry)
    option_df = enrich_option_rows(option_rows)

    spot_candidates = pd.to_numeric(pd.DataFrame(option_rows).get("spot_price"), errors="coerce").dropna()
    spot_value = float(spot_candidates.iloc[0]) if not spot_candidates.empty else None

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
            call_matrix = build_ratio_matrix(option_df, "call_options", strikes_from_atm, call_column_defs, PRICE_MODE)
            if call_matrix.empty:
                st.warning("No data available to build the matrix.")
            else:
                st.dataframe(call_matrix, use_container_width=True, height=560)

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
            put_matrix = build_ratio_matrix(option_df, "put_options", strikes_from_atm_put, put_column_defs, PRICE_MODE)
            if put_matrix.empty:
                st.warning("No data available to build the matrix.")
            else:
                st.dataframe(put_matrix, use_container_width=True, height=560)

except requests.HTTPError as e:
    st.error(f"HTTP error: {e}")
except Exception as e:
    st.error(f"Error: {e}")
