import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import ccxt
import time
import os
from datetime import datetime, timedelta

# ==========================================
# 0. 页面配置与 CSS
# ==========================================
st.set_page_config(
    page_title="QuantPro | 多空双向交易系统",
    layout="wide",
    page_icon="⚖️",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .stApp { background-color: #0e1117; }
    section[data-testid="stSidebar"] { background-color: #161b22; border-right: 1px solid #30363d; }
    div[data-testid="stMetric"] { background-color: #21262d; border: 1px solid #30363d; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
    div[data-testid="stMetric"]:hover { border-color: #58a6ff; }
    div[data-testid="stMetricLabel"] { color: #8b949e; }
    div[data-testid="stMetricValue"] { color: #fff; font-weight: 600; }
    .stButton>button { background-color: #238636; color: white; border: none; font-weight: bold; }
    .stButton>button:hover { background-color: #2ea043; }
    h1, h2, h3 { color: #f0f6fc !important; font-family: 'Segoe UI', sans-serif; }
    .stTabs [data-baseweb="tab"] { background-color: #21262d; color: #c9d1d9; }
    .stTabs [aria-selected="true"] { background-color: #1f6feb !important; color: white !important; }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 1. 核心算法区
# ==========================================

SUPPORTED_COINS = ['BTC', 'ETH', 'SOL', 'ADA', 'BNB', 'DOGE', 'XRP', 'AVAX', 'LINK']


def fetch_binance_data(symbol, progress_bar, status_text):
    exchange = ccxt.binance({'enableRateLimit': True, 'options': {'adjustForTimeDifference': True}})
    all_data = []
    # 获取最近4年的数据
    since = exchange.parse8601('2020-01-01T00:00:00Z')

    while True:
        try:
            data = exchange.fetch_ohlcv(symbol, '1d', since)
            if not data: break
            all_data.extend(data)
            since = data[-1][0] + 86400000
            last_date = pd.to_datetime(data[-1][0], unit='ms').strftime('%Y-%m-%d')
            status_text.markdown(f"<span style='color:#58a6ff'>同步 {symbol}... {last_date}</span>",
                                 unsafe_allow_html=True)
            if since > exchange.milliseconds(): break
            time.sleep(exchange.rateLimit / 1000)
        except Exception as e:
            st.error(f"获取 {symbol} 失败: {str(e)}")
            break

    df = pd.DataFrame(all_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    # 简单的时区处理，假设为UTC
    df.set_index('datetime', inplace=True)
    return df[['open', 'close']]


def update_market_data():
    progress_bar = st.progress(0)
    status_text = st.empty()
    try:
        data_frames = {}
        for idx, coin in enumerate(SUPPORTED_COINS):
            df = fetch_binance_data(f"{coin}/USDT", progress_bar, status_text)
            df = df.reset_index().rename(columns={'open': f'{coin}_open', 'close': f'{coin}_close'})
            data_frames[coin] = df
            progress_bar.progress((idx + 1) / len(SUPPORTED_COINS))

        with pd.ExcelWriter('market_data.xlsx', engine='openpyxl') as writer:
            for coin, df in data_frames.items():
                df.to_excel(writer, sheet_name=coin, index=False)

        status_text.success("✅ 数据同步完成")
        time.sleep(1)
        status_text.empty()
        progress_bar.empty()
        return True
    except Exception as e:
        st.error(f"同步错误: {str(e)}")
        return False


@st.cache_data(ttl=3600)
def load_and_preprocess(alt_coin):
    if not os.path.exists('market_data.xlsx'): return None
    try:
        btc = pd.read_excel('market_data.xlsx', sheet_name='BTC', parse_dates=['datetime'], index_col='datetime')
        alt = pd.read_excel('market_data.xlsx', sheet_name=alt_coin, parse_dates=['datetime'], index_col='datetime')
    except ValueError:
        return None
    except Exception:
        return None

    # 合并数据
    merged = pd.concat({
        'BTC': btc[[f'BTC_open', f'BTC_close']],
        'ALT': alt[[f'{alt_coin}_open', f'{alt_coin}_close']]
    }, axis=1)
    
    # 展平列名
    merged.columns = ['BTC_open', 'BTC_close', f'{alt_coin}_open', f'{alt_coin}_close']

    # 计算指标
    target_symbols = ['BTC', alt_coin]
    for symbol in target_symbols:
        close = merged[f'{symbol}_close']
        # 经典 V1 指标
        merged[f'{symbol}_MA40'] = close.rolling(40).mean()
        merged[f'{symbol}_MA40_up'] = merged[f'{symbol}_MA40'].diff() > 0
        merged[f'{symbol}_20d_ret'] = close.pct_change(20)

    return merged.dropna()


def run_strategy(df, alt_coin, initial_capital, fee, start_date, end_date, allow_short):
    # 确保索引是 datetime 类型以便比较
    df.index = pd.to_datetime(df.index)
    start_ts = pd.to_datetime(start_date)
    end_ts = pd.to_datetime(end_date)
    
    mask = (df.index >= start_ts) & (df.index <= end_ts)
    df_slice = df.loc[mask].copy()
    if len(df_slice) < 2: return None, None, "选定范围内数据不足"

    portfolio = pd.Series(index=df_slice.index, dtype=float)
    trades = []

    # 持仓状态
    position_symbol = None
    position_side = None
    cash = initial_capital

    short_entry_price = 0
    short_amount = 0
    long_amount = 0

    for i in range(len(df_slice)):
        current_date = df_slice.index[i]
        
        # 获取在完整df中的位置，用于获取前一天的数据
        if current_date not in df.index: continue
        full_idx = df.index.get_loc(current_date)
        
        if full_idx < 1: 
            portfolio.iloc[i] = cash
            continue
            
        prev_date = df.index[full_idx - 1]

        # 目标信号
        target_symbol = None
        target_side = None

        # ====================
        # 1. 信号判断
        # ====================
        btc_price = df.at[prev_date, 'BTC_close']
        btc_ma = df.at[prev_date, 'BTC_MA40']
        btc_ma_up = df.at[prev_date, 'BTC_MA40_up']

        is_bull = btc_price > btc_ma and btc_ma_up
        is_bear = btc_price < btc_ma and (not btc_ma_up)

        if is_bull:
            target_side = 'LONG'
            alt_price = df.at[prev_date, f'{alt_coin}_close']
            alt_ma = df.at[prev_date, f'{alt_coin}_MA40']
            alt_ma_up = df.at[prev_date, f'{alt_coin}_MA40_up']

            if alt_price > alt_ma and alt_ma_up:
                btc_ret = df.at[prev_date, 'BTC_20d_ret']
                alt_ret = df.at[prev_date, f'{alt_coin}_20d_ret']
                target_symbol = 'BTC' if btc_ret > alt_ret else alt_coin
            else:
                target_symbol = 'BTC'

        elif is_bear and allow_short:
            target_side = 'SHORT'
            alt_price = df.at[prev_date, f'{alt_coin}_close']
            alt_ma = df.at[prev_date, f'{alt_coin}_MA40']
            alt_ma_up = df.at[prev_date, f'{alt_coin}_MA40_up']

            if alt_price < alt_ma and (not alt_ma_up):
                btc_ret = df.at[prev_date, 'BTC_20d_ret']
                alt_ret = df.at[prev_date, f'{alt_coin}_20d_ret']
                target_symbol = 'BTC' if btc_ret < alt_ret else alt_coin
            else:
                target_symbol = 'BTC'
        else:
            target_symbol = None
            target_side = None

        # ====================
        # 2. 交易执行
        # ====================
        
        # 平仓逻辑
        if position_symbol:
            change_needed = (position_symbol != target_symbol) or (position_side != target_side)

            if change_needed:
                price = df_slice.at[current_date, f'{position_symbol}_open']

                if position_side == 'LONG':
                    cash = long_amount * price * (1 - fee)
                    trades.append({
                        'Date': current_date, 'Action': 'CLOSE_LONG', 
                        'Symbol': position_symbol, 'Price': price, 'Value': cash
                    })
                    long_amount = 0

                elif position_side == 'SHORT':
                    gross_pnl = (short_entry_price - price) * short_amount
                    buy_back_cost = price * short_amount
                    fee_cost = buy_back_cost * fee
                    cash = (short_amount * short_entry_price) + gross_pnl - fee_cost
                    trades.append({
                        'Date': current_date, 'Action': 'CLOSE_SHORT', 
                        'Symbol': position_symbol, 'Price': price, 'Value': cash
                    })
                    short_amount = 0
                    short_entry_price = 0

                position_symbol = None
                position_side = None

        # 开仓逻辑
        if target_symbol and not position_symbol:
            if cash > 0:
                price = df_slice.at[current_date, f'{target_symbol}_open']

                if target_side == 'LONG':
                    long_amount = cash * (1 - fee) / price
                    cash = 0
                    trades.append({
                        'Date': current_date, 'Action': 'OPEN_LONG', 
                        'Symbol': target_symbol, 'Price': price, 'Value': initial_capital # approximate
                    })
                    position_symbol = target_symbol
                    position_side = 'LONG'

                elif target_side == 'SHORT':
                    available_cash = cash * (1 - fee)
                    short_entry_price = price
                    short_amount = available_cash / price
                    cash = 0
                    trades.append({
                        'Date': current_date, 'Action': 'OPEN_SHORT', 
                        'Symbol': target_symbol, 'Price': price, 'Value': initial_capital
                    })
                    position_symbol = target_symbol
                    position_side = 'SHORT'

        # ====================
        # 3. 净值计算
        # ====================
        if position_side == 'LONG':
            current_price = df_slice.at[current_date, f'{position_symbol}_close']
            current_val = long_amount * current_price
        elif position_side == 'SHORT':
            current_price = df_slice.at[current_date, f'{position_symbol}_close']
            locked_val = short_amount * short_entry_price
            pnl = (short_entry_price - current_price) * short_amount
            current_val = locked_val + pnl
        else:
            current_val = cash

        portfolio.iloc[i] = current_val
        
        # 修正交易记录中的净值
        if trades and trades[-1]['Date'] == current_date:
            trades[-1]['Value'] = current_val

    return portfolio, trades, None


# ==========================================
# 2. UI 逻辑
# ==========================================

st.sidebar.markdown("### 🎛️ 控制台")
if st.sidebar.button("🔄 同步行情数据", use_container_width=True):
    if update_market_data(): st.cache_data.clear()

st.sidebar.markdown("---")
target_coin = st.sidebar.selectbox("轮动标的", SUPPORTED_COINS[1:], index=1)

# 加载数据
data = load_and_preprocess(target_coin)

# === 关键修正：数据判空与初始化检查 ===
if data is not None and not data.empty:
    # 强制确保索引是时间格式
    data.index = pd.to_datetime(data.index)
    
    min_date = data.index.min().date()
    max_date = data.index.max().date()

    # 初始化 Session State
    if 'global_start_date' not in st.session_state:
        default_start_str = '2021-01-01'
        init_start = pd.to_datetime(default_start_str).date()
        # 确保默认时间在范围内
        if init_start < min_date: init_start = min_date
        if init_start > max_date: init_start = min_date
        st.session_state['global_start_date'] = init_start
        
    if 'global_end_date' not in st.session_state: 
        st.session_state['global_end_date'] = max_date

    # 状态范围纠偏
    if st.session_state['global_start_date'] < min_date:
        st.session_state['global_start_date'] = min_date
    if st.session_state['global_start_date'] > max_date:
        st.session_state['global_start_date'] = min_date
    if st.session_state['global_end_date'] > max_date:
        st.session_state['global_end_date'] = max_date
    if st.session_state['global_end_date'] < min_date:
        st.session_state['global_end_date'] = max_date

    st.sidebar.subheader("策略配置")

    allow_short = st.sidebar.checkbox("启用做空机制 (Bear Mode)", value=True,
                                      help="勾选后，当趋势向下时会进行不加杠杆的做空（1x Short），从下跌中获利。")

    col_date1, col_date2 = st.sidebar.columns(2)
    start_date = col_date1.date_input("开始", min_value=min_date, max_value=max_date, key='global_start_date')
    end_date = col_date2.date_input("结束", min_value=min_date, max_value=max_date, key='global_end_date')
    capital = st.sidebar.number_input("本金", 10000, step=1000)
    fee = st.sidebar.number_input("费率", 0.001, format="%.4f")

    st.title(f"⚖️ 多空双向回测: BTC vs {target_coin}")

    if allow_short:
        st.success("✅ **多空全天候模式**: 牛市做多强者，熊市做空弱者。旨在实现穿越牛熊的绝对收益。")
    else:
        st.info("🛡️ **纯多头模式**: 仅在牛市持有，熊市空仓 (USDT)。")

    if start_date < end_date:
        with st.spinner('计算中...'):
            port, trades, err = run_strategy(data, target_coin, capital, fee, start_date, end_date, allow_short)

        if err:
            st.error(err)
        else:
            mask = (data.index >= pd.to_datetime(start_date)) & (data.index <= pd.to_datetime(end_date))
            
            # 再次检查 mask 是否有数据
            if mask.sum() > 0:
                btc_hold_series = data.loc[mask, 'BTC_close']
                btc_hold = btc_hold_series / btc_hold_series.iloc[0] * capital

                final = port.iloc[-1]
                ret = (final / capital) - 1
                dd = ((port - port.cummax()) / port.cummax()).min()

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("策略收益", f"{ret * 100:+.1f}%", f"${final - capital:,.0f}")
                c2.metric("最大回撤", f"{dd * 100:.1f}%")
                c3.metric(f"跑赢BTC", f"{(final / btc_hold.iloc[-1] - 1) * 100:+.1f}%")
                c4.metric(f"交易次数", len(trades))

                tab1, tab2 = st.tabs(["曲线对比", "详细交易"])
                with tab1:
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=port.index, y=port, name='策略净值', line=dict(color='#00e676', width=2),
                                             fill='tozeroy', fillcolor='rgba(0,230,118,0.1)'))
                    fig.add_trace(
                        go.Scatter(x=btc_hold.index, y=btc_hold, name='BTC持有', line=dict(color='gray', dash='dot')))

                    df_t = pd.DataFrame(trades)
                    if not df_t.empty:
                        longs = df_t[df_t['Action'] == 'OPEN_LONG']
                        if not longs.empty:
                            fig.add_trace(go.Scatter(x=longs['Date'], y=longs['Value'], mode='markers', name='开多',
                                                     marker=dict(symbol='triangle-up', color='#00e676', size=10,
                                                                 line=dict(width=1, color='black'))))

                        shorts = df_t[df_t['Action'] == 'OPEN_SHORT']
                        if not shorts.empty:
                            fig.add_trace(go.Scatter(x=shorts['Date'], y=shorts['Value'], mode='markers', name='开空',
                                                     marker=dict(symbol='triangle-down', color='#9c27b0', size=10,
                                                                 line=dict(width=1, color='white'))))

                        closes = df_t[df_t['Action'].str.contains('CLOSE')]
                        if not closes.empty:
                            fig.add_trace(go.Scatter(x=closes['Date'], y=closes['Value'], mode='markers', name='平仓',
                                                     marker=dict(symbol='circle', color='#808080', size=6, opacity=0.7)))

                    fig.update_layout(template='plotly_dark', height=500, margin=dict(t=30, b=0, l=0, r=0))
                    st.plotly_chart(fig, use_container_width=True)

                with tab2:
                    if not df_t.empty:
                        df_t['Date'] = df_t['Date'].dt.strftime('%Y-%m-%d')
                        def color_action(val):
                            if 'LONG' in val and 'OPEN' in val: return 'color: #00e676; font-weight: bold'
                            if 'SHORT' in val and 'OPEN' in val: return 'color: #ce93d8; font-weight: bold'
                            if 'CLOSE' in val: return 'color: #b0bec5'
                            return ''
                        st.dataframe(df_t.style.map(color_action, subset=['Action']), use_container_width=True)
            else:
                st.warning("该日期范围内无有效市场数据。")
    else:
        st.error("结束日期必须晚于开始日期")

else:
    # === 空数据状态下的欢迎界面 ===
    st.title("⚖️ QuantPro 量化交易系统")
    st.info("👋 欢迎！这是您的首次运行，或者本地数据已过期。")
    st.markdown("""
    ### 🚀 快速开始
    1. 请点击左侧边栏顶部的 **'🔄 同步行情数据'** 按钮。
    2. 等待进度条走完（约需 1-2 分钟，从币安服务器下载数据）。
    3. 数据准备好后，系统将自动显示回测界面。
    """)
    st.warning("提示：如果点击同步后长时间无反应，请刷新页面重试。")
