import streamlit as st
import pandas as pd
import requests
import urllib.parse

# --- 1. CONFIGURATION ---
st.set_page_config(page_title="Zerodha Option Chain", layout="wide")

SPOT_MAPPING = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT"
}

# --- 2. DATA FETCHING ---
@st.cache_data(ttl=43200) # Cache for 12 hours
def get_nfo_instruments():
    try:
        df = pd.read_csv("https://api.kite.trade/instruments/NFO")
        return df[df['instrument_type'].isin(['CE', 'PE'])]
    except Exception as e:
        st.error(f"Failed to fetch NFO instruments: {e}")
        return pd.DataFrame()

def fetch_kite_quotes(symbols_list, enctoken):
    url = "https://kite.zerodha.com/oms/quote"
    headers = {"Authorization": f"enctoken {enctoken}"}
    quotes_data = {}
    chunk_size = 200 
    
    def load_chunk(chunk):
        if not chunk: return
        
        # THE FIX: Force %20 encoding for spaces to prevent HTTP 400
        query_string = "&".join([f"i={urllib.parse.quote(sym)}" for sym in chunk])
        req_url = f"{url}?{query_string}"
        
        resp = requests.get(req_url, headers=headers)
        
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            quotes_data.update(data)
        elif resp.status_code == 400:
            if len(chunk) == 1:
                return 
            mid = len(chunk) // 2
            load_chunk(chunk[:mid])
            load_chunk(chunk[mid:])
        else:
            raise RuntimeError(f"Kite quote HTTP {resp.status_code}: {resp.text} | Symbol(s): {chunk[:3]}")

    for start_idx in range(0, len(symbols_list), chunk_size):
        load_chunk(symbols_list[start_idx:start_idx + chunk_size])
        
    return quotes_data

# --- 3. UI DASHBOARD ---
st.title("📈 Live Option Chain")

st.sidebar.header("🔑 Authentication")
kite_enctoken = st.sidebar.text_input("Kite Enctoken", type="password")

st.sidebar.header("⚙️ Settings")
symbol = st.sidebar.selectbox("Select Index", list(SPOT_MAPPING.keys()))
spot_symbol = SPOT_MAPPING[symbol]

df_nfo = get_nfo_instruments()
if df_nfo.empty:
    st.stop()

df_symbol = df_nfo[df_nfo['name'] == symbol]
expiries = sorted(df_symbol['expiry'].unique())
selected_expiry = st.sidebar.selectbox("Select Expiry", expiries)

if not kite_enctoken:
    st.info("👈 Enter your Kite enctoken in the sidebar to load live data.")
    st.stop()

# --- 4. PROCESSING ---
with st.spinner("Fetching market data..."):
    df_expiry = df_symbol[df_symbol['expiry'] == selected_expiry]
    option_symbols = ["NFO:" + ts for ts in df_expiry['tradingsymbol'].tolist()]
    all_symbols = [spot_symbol] + option_symbols
    
    quotes = fetch_kite_quotes(all_symbols, kite_enctoken)

    if spot_symbol not in quotes:
        st.error("Invalid Enctoken or unable to fetch spot price.")
        st.stop()

    spot_price = quotes[spot_symbol]['last_price']
    st.subheader(f"**{symbol}** Spot: `{spot_price}`")

    # Build chain
    chain_data = []
    strikes = sorted(df_expiry['strike'].unique())
    
    for strike in strikes:
        ce_row = df_expiry[(df_expiry['strike'] == strike) & (df_expiry['instrument_type'] == 'CE')]
        pe_row = df_expiry[(df_expiry['strike'] == strike) & (df_expiry['instrument_type'] == 'PE')]
        
        ce_sym = "NFO:" + ce_row['tradingsymbol'].values[0] if not ce_row.empty else None
        pe_sym = "NFO:" + pe_row['tradingsymbol'].values[0] if not pe_row.empty else None
        
        ce_quote = quotes.get(ce_sym, {})
        pe_quote = quotes.get(pe_sym, {})
        
        chain_data.append({
            "CE OI": ce_quote.get('oi', 0),
            "CE LTP": ce_quote.get('last_price', 0),
            "Strike": strike,
            "PE LTP": pe_quote.get('last_price', 0),
            "PE OI": pe_quote.get('oi', 0),
        })
        
    df_chain = pd.DataFrame(chain_data)
    
    # Filter 20 strikes above and below ATM
    atm_strike = min(strikes, key=lambda x: abs(x - spot_price))
    atm_idx = df_chain[df_chain['Strike'] == atm_strike].index[0]
    start_idx = max(0, atm_idx - 20)
    end_idx = min(len(df_chain), atm_idx + 21)
    
    df_filtered = df_chain.iloc[start_idx:end_idx].reset_index(drop=True)
    
    # Highlight ATM row
    def highlight_atm(row):
        if row['Strike'] == atm_strike:
            return ['background-color: rgba(255, 255, 255, 0.1)'] * len(row)
        return [''] * len(row)
        
    st.dataframe(df_filtered.style.apply(highlight_atm, axis=1), use_container_width=True, height=600)
