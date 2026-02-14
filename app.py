import os
import sys
import time
import signal
import logging
import requests
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

# ============================================================
# CFG V8_숏
# ============================================================
# 20260210_1730 : 클로드 엔트리 + 벨라 n봉 엑시트 기본 매매확인후 필터 추가
# 20260210_1940 : 매매확인 튜닝 13번 8>20, 14번 5>2 16번 30>5
# 20260211      : on_entry_fired 추가, 22번 OFF, EXIT후 entry_fired 리셋
# 20260211_v2   : 벨라 의견 — pullback EMA_MID 깊이 기준 강화
# 20260211_v3   : 벨라 의견 — slope_bars 2→3, spread 0.0004→0.0012, min_len 60→45
# 20260215_BASE : 브9 ENTRY/EXIT 이식 + 21_MAX_ENTRY_PER_TREND 구현
# 20260215_FIX  : entry_fired 리셋 로직 수정 + 21번 0으로 변경 + EMA 중복 계산 제거

CFG = {
    # ========================================================
    # BASIC
    # ========================================================
    "01_TRADE_SYMBOL": "SUIUSDT",
    "02_INTERVAL": "5m",
    "03_CAPITAL_BASE_USDT": 30.0,
    "04_LEVERAGE": 1,

    # ========================================================
    # ENTRY (EMA CROSS ONLY)
    # ========================================================
    # EMA_FAST ↓ EMA_MID + EMA_MID < EMA_SLOW
    "10_EMA_FAST": 9,
    "11_EMA_MID": 14,
    "12_EMA_SLOW": 20,

    # 아래 항목들은 현재 전략에서 사용하지 않음 (영향 0)
    "13_PULLBACK_N": 0,
    "14_SLOPE_BARS": 4,
    "15_SPREAD_MIN": 0.0,
    "16_PEAK_BARS": 0,

    # ========================================================
    # ENTRY MANAGEMENT
    # ========================================================
    "20_ENTRY_COOLDOWN_BARS": 0,
    "21_MAX_ENTRY_PER_TREND": 0,   # 0 = OFF
    "22_ENTRY_MAX_DROP_PCT": 0.0,

    # ========================================================
    # EXIT (EMA_MID BREAK ONLY)
    # ========================================================
    # close > EMA_MID → EXIT
    "40_SL_ENABLE": False,
    "41_SL_PCT": 2.0,

    "50_TIMEOUT_EXIT_ENABLE": False,
    "51_TIMEOUT_BARS": 60,

    # ========================================================
    # ENGINE
    # ========================================================
    "90_KLINE_LIMIT": 1500,
    "91_POLL_SEC": 7,
    "92_LOG_LEVEL": "INFO",
}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=getattr(logging, CFG["92_LOG_LEVEL"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("VELLA_v8_SHORT")

# ============================================================
# BINANCE
# ============================================================

try:
    from binance.client import Client
    from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
except Exception:
    Client = None
    SIDE_BUY = "BUY"
    SIDE_SELL = "SELL"
    ORDER_TYPE_MARKET = "MARKET"

BINANCE_FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"

def init_client():
    if Client is None:
        raise RuntimeError("python-binance missing. pip install python-binance")
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("Missing BINANCE_API_KEY / BINANCE_API_SECRET env vars.")
    return Client(api_key, api_secret)

def set_leverage(client, symbol, leverage):
    try:
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
    except Exception as e:
        log.error(f"set_leverage failed: {e}")

def fetch_klines_futures(symbol, interval, limit):
    try:
        r = requests.get(
            BINANCE_FUTURES_KLINES,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"fetch_klines_futures: {e}")
        return None

def get_futures_lot_size(client, symbol):
    try:
        info = client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        return {
                            "stepSize": Decimal(f["stepSize"]),
                            "minQty": Decimal(f["minQty"]),
                            "maxQty": Decimal(f["maxQty"]),
                        }
        return None
    except Exception as e:
        log.error(f"get_futures_lot_size: {e}")
        return None

def calculate_quantity(qty_raw, lot):
    if lot is None:
        return None
    qty_decimal = Decimal(str(qty_raw))
    step = lot["stepSize"]
    qty = (qty_decimal / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
    if qty < lot["minQty"]:
        return None
    if qty > lot["maxQty"]:
        qty = lot["maxQty"]
    precision = abs(step.as_tuple().exponent)
    return float(qty.quantize(Decimal(10) ** -precision))

# ============================================================
# INDICATORS
# ============================================================

def ema_series(values, period):
    if not values:
        return []
    if len(values) < period:
        return [values[0]] * len(values)
    k = 2 / (period + 1)
    out = [values[0]] * len(values)
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    prev = sma
    for i in range(period, len(values)):
        prev = (values[i] * k) + (prev * (1 - k))
        out[i] = prev
    for i in range(period - 1):
        out[i] = out[period - 1]
    return out

# ============================================================
# STATE
# ============================================================

@dataclass
class Position:
    side: str
    entry_price: float
    qty: float
    entry_bar: int

@dataclass
class ShortEntryState:
    entry_fired: bool = False

@dataclass
class EngineState:
    bar: int = 0
    last_open_time: Optional[int] = None
    cooldown_until_bar: int = 0
    entry_state: ShortEntryState = field(default_factory=ShortEntryState)
    position: Optional[Position] = None
    close_history: List[float] = None
    in_downtrend: bool = False
    entry_count_in_trend: int = 0
    ema_mid_now: float = 0.0

    def __post_init__(self):
        self.close_history = []

# ============================================================
# ENTRY
# ============================================================

def on_entry_fired(st: ShortEntryState):
    st.entry_fired = True

def short_entry_signal(closes, st, engine_st):
    if len(closes) < max(CFG["12_EMA_SLOW"], 60):
        return False

    ema_fast_s = ema_series(closes, CFG["10_EMA_FAST"])
    ema_mid_s  = ema_series(closes, CFG["11_EMA_MID"])
    ema_slow_s = ema_series(closes, CFG["12_EMA_SLOW"])

    ema_fast = ema_fast_s[-1]
    ema_mid  = ema_mid_s[-1]
    ema_slow = ema_slow_s[-1]

    stack_now = (ema_fast < ema_mid) and (ema_mid < ema_slow)
    if not stack_now:
        return False

    max_entry = int(CFG["21_MAX_ENTRY_PER_TREND"])
    if max_entry > 0 and engine_st.entry_count_in_trend >= max_entry:
        return False

    if st.entry_fired:
        return False

    raw_signal = (
        (ema_fast_s[-2] >= ema_mid_s[-2]) and
        (ema_fast < ema_mid) and
        (ema_mid < ema_slow)
    )
    
    if not raw_signal:
        st.entry_fired = False
        return False
    
    return raw_signal

# ============================================================
# EXIT
# ============================================================

def exit_option_sl(close, entry_price):
    if not CFG["40_SL_ENABLE"]:
        return False
    sl = float(CFG["41_SL_PCT"]) / 100.0
    return close >= entry_price * (1.0 + sl)

def exit_option_timeout(current_bar, entry_bar):
    if not CFG["50_TIMEOUT_EXIT_ENABLE"]:
        return False
    return (current_bar - entry_bar) >= int(CFG["51_TIMEOUT_BARS"])

def exit_signal(state):
    pos = state.position
    if pos is None:
        return False
    close = state.close_history[-1]
    if exit_option_sl(close, pos.entry_price):
        return True
    if exit_option_timeout(state.bar, pos.entry_bar):
        return True
    if close > state.ema_mid_now:
        return True
    return False

# ============================================================
# EXECUTION
# ============================================================

def place_short_entry(client, symbol, capital_usdt, lot):
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker["price"])
        leverage = int(CFG["04_LEVERAGE"])
        notional = float(capital_usdt) * float(leverage)
        qty_raw = notional / price
        qty = calculate_quantity(qty_raw, lot)
        if qty is None:
            log.error("entry: qty calculation failed")
            return None
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=qty,
        )
        return {"entry_price": price, "qty": qty}
    except Exception as e:
        log.error(f"place_short_entry: {e}")
        return None

def place_short_exit(client, symbol, qty, lot):
    try:
        qty_rounded = calculate_quantity(qty, lot)
        if qty_rounded is None:
            log.error("exit: qty too small — cannot close")
            return False
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=qty_rounded,
            reduceOnly=True,
        )
        return True
    except Exception as e:
        log.error(f"place_short_exit: {e}")
        return False

# ============================================================
# ENGINE
# ============================================================

STOP = False

def _sig_handler(_sig, _frame):
    global STOP
    STOP = True

signal.signal(signal.SIGINT, _sig_handler)
signal.signal(signal.SIGTERM, _sig_handler)

def engine():
    client = init_client()
    symbol   = CFG["01_TRADE_SYMBOL"]
    interval = CFG["02_INTERVAL"]
    capital  = float(CFG["03_CAPITAL_BASE_USDT"])

    set_leverage(client, symbol, int(CFG["04_LEVERAGE"]))

    lot = get_futures_lot_size(client, symbol)
    if lot is None:
        raise RuntimeError("lot_size retrieval failed")

    st = EngineState()

    log.info(f"START v8 SHORT | symbol={symbol} interval={interval} capital={capital} lev={CFG['04_LEVERAGE']}")

    while not STOP:
        try:
            kl = fetch_klines_futures(symbol, interval, int(CFG["90_KLINE_LIMIT"]))
            if not kl:
                time.sleep(CFG["91_POLL_SEC"])
                continue

            completed = kl[-2]
            open_time = int(completed[0])

            if st.last_open_time == open_time:
                time.sleep(CFG["91_POLL_SEC"])
                continue

            if not st.close_history:
                for k in kl[:-1]:
                    st.close_history.append(float(k[4]))
                st.bar = len(st.close_history)
                st.last_open_time = int(kl[-2][0])
                continue

            st.last_open_time = open_time
            st.bar += 1

            close = float(completed[4])
            st.close_history.append(close)

            if len(st.close_history) > 2000:
                st.close_history = st.close_history[-2000:]

            if len(st.close_history) >= max(CFG["12_EMA_SLOW"], 60):
                ema_fast_s = ema_series(st.close_history, CFG["10_EMA_FAST"])
                ema_mid_s  = ema_series(st.close_history, CFG["11_EMA_MID"])
                ema_slow_s = ema_series(st.close_history, CFG["12_EMA_SLOW"])
                
                ema_fast = ema_fast_s[-1]
                ema_mid  = ema_mid_s[-1]
                ema_slow = ema_slow_s[-1]
                
                st.ema_mid_now = ema_mid
                
                stack_now = (ema_fast < ema_mid) and (ema_mid < ema_slow)
                
                if stack_now and not st.in_downtrend:
                    st.in_downtrend = True
                    st.entry_count_in_trend = 0
                elif not stack_now and st.in_downtrend:
                    st.in_downtrend = False
                    st.entry_count_in_trend = 0

            if st.position is None:
                if st.bar < st.cooldown_until_bar:
                    continue

                if short_entry_signal(st.close_history, st.entry_state, st):
                    order = place_short_entry(client, symbol, capital, lot)
                    if order:
                        st.position = Position(
                            side="SHORT",
                            entry_price=float(order["entry_price"]),
                            qty=float(order["qty"]),
                            entry_bar=st.bar,
                        )
                        on_entry_fired(st.entry_state)
                        st.entry_count_in_trend += 1

                        cd = int(CFG["20_ENTRY_COOLDOWN_BARS"])
                        if cd > 0:
                            st.cooldown_until_bar = st.bar + cd

                        log.info(f"[ENTRY] SHORT qty={st.position.qty} entry={st.position.entry_price} bar={st.bar}")
                    else:
                        log.error("[ENTRY_FAIL] order failed")
            else:
                if st.position.entry_bar == st.bar:
                    continue

                if exit_signal(st):
                    ok = place_short_exit(client, symbol, st.position.qty, lot)
                    if ok:
                        log.info(f"[EXIT] SHORT close={close} entry={st.position.entry_price} bar={st.bar}")
                        st.position = None
                        st.entry_state.entry_fired = False

                        cd = int(CFG["20_ENTRY_COOLDOWN_BARS"])
                        if cd > 0:
                            st.cooldown_until_bar = st.bar + cd
                    else:
                        log.error("[EXIT_FAIL] order failed (kept position)")

        except Exception as e:
            log.error(f"engine loop error: {e}")
            time.sleep(CFG["91_POLL_SEC"])

    log.info("STOP v8 SHORT")

if __name__ == "__main__":
    engine()