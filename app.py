# ============================================================
# VELLA_v8 — SHORT ENGINE (Binance Futures)
# - EXECUTION CORE: based on v7 proven trade plumbing (lotSize/qty/order/reduceOnly/closed-bar loop)
# - ENTRY: EMA_FAST < EMA_MID < EMA_SLOW (하방 정렬) + close < EMA_FAST (1-shot per trend cycle)
# - EXIT: Bella rule SHORT (close > avg(prev N closed closes))  [default N=2, CFG adjustable]
# - TIME AXIS: REST closed-bar only (kline[-2])
# ============================================================

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
# CFG (ALL CONTROL HERE)
# ============================================================
# 20260210_1730 : 클롣드 엔트리 + 벨라 n봉 엑시트 기본 매매확인후 필터 추가
# 20260210_1940 : 매매확인 튜닝 13번 8>20, 14번 5>2 16번 30>5

CFG = {
    # -------------------------
    # BASIC
    # -------------------------
    "01_TRADE_SYMBOL": "POLYXUSDT",
    "02_INTERVAL": "5m",
    "03_CAPITAL_BASE_USDT": 30.0,
    "04_LEVERAGE": 1,

    # -------------------------
    # ENTRY (v8 SHORT)
    # -------------------------
    "10_EMA_FAST": 10,
    "11_EMA_MID": 15,
    "12_EMA_SLOW": 20,

    "13_PULLBACK_N": 20,
    "14_SLOPE_BARS": 2,
    "15_SPREAD_MIN": 0.0004,
    "16_PEAK_BARS": 5,

    # -------------------------
    # ENTRY MANAGEMENT FILTERS (plug-in slots)
    # -------------------------
    "20_ENTRY_COOLDOWN_BARS": 0,
    "21_MAX_ENTRY_PER_TREND": 1,

    # -------------------------
    # EXIT (Bella SHORT)
    # -------------------------
    "30_EXIT_AVG_N": 2,
    "31_EXIT_USE_PREV_N_ONLY": True,

    # -------------------------
    # EXIT OPTIONS (plug-in slots; default OFF)
    # -------------------------
    "40_SL_ENABLE": False,
    "41_SL_PCT": 2.0,

    "50_TIMEOUT_EXIT_ENABLE": False,
    "51_TIMEOUT_BARS": 60,

    # -------------------------
    # ENGINE
    # -------------------------
    "90_KLINE_LIMIT": 240,
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
# BINANCE (v7 style)
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

def init_client() -> "Client":
    if Client is None:
        raise RuntimeError("python-binance missing. pip install python-binance")
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("Missing BINANCE_API_KEY / BINANCE_API_SECRET env vars.")
    return Client(api_key, api_secret)

def set_leverage(client: "Client", symbol: str, leverage: int) -> None:
    try:
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
    except Exception as e:
        log.error(f"set_leverage failed: {e}")

def fetch_klines_futures(symbol: str, interval: str, limit: int) -> Optional[List[Any]]:
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

def get_futures_lot_size(client: "Client", symbol: str) -> Optional[Dict[str, Decimal]]:
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

def calculate_quantity(qty_raw: float, lot: Dict[str, Decimal]) -> Optional[float]:
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

def ema_series(values: List[float], period: int) -> List[float]:
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
    side: str               # "SHORT"
    entry_price: float
    qty: float
    entry_bar: int

@dataclass
class ShortEntryState:
    entry_fired: bool = False

from dataclasses import dataclass, field

@dataclass
class EngineState:
    bar: int = 0
    last_open_time: Optional[int] = None
    cooldown_until_bar: int = 0
    entry_state: ShortEntryState = field(default_factory=ShortEntryState)
    position: Optional[Position] = None
    close_history: List[float] = None

    def __post_init__(self):
        self.close_history = []

# ============================================================
# ENTRY (v8 SHORT)
# ============================================================

def short_entry_signal(
    closes: List[float],
    st: ShortEntryState
) -> bool:
    """
    v8 SHORT entry:
      - stack: EMA_FAST < EMA_MID < EMA_SLOW (하방 정렬)
      - close < EMA_FAST
      - 1-shot per trend cycle (entry_fired)
    """
    if len(closes) < max(CFG["12_EMA_SLOW"], 60):
        return False

    ema_fast_s = ema_series(closes, CFG["10_EMA_FAST"])
    ema_mid_s  = ema_series(closes, CFG["11_EMA_MID"])
    ema_slow_s = ema_series(closes, CFG["12_EMA_SLOW"])

    ema_fast = ema_fast_s[-1]
    ema_mid  = ema_mid_s[-1]
    ema_slow = ema_slow_s[-1]
    close    = closes[-1]

    stack_now = (ema_fast < ema_mid) and (ema_mid < ema_slow)
    if not stack_now:
        st.entry_fired = False
        return False

    if st.entry_fired:
        return False

    return close < ema_fast

def on_entry_fired(st: ShortEntryState) -> None:
    st.entry_fired = True

# ============================================================
# EXIT (Bella SHORT)
# ============================================================

def bella_exit_core_avg_break(closes: List[float], n: int) -> bool:
    """
    Bella SHORT:
      - avg = mean(prev N closed closes)
      - exit if current_close > avg
    """
    n = int(n)
    if n <= 0:
        return False
    if len(closes) < n + 1:
        return False

    current = closes[-1]
    if CFG["31_EXIT_USE_PREV_N_ONLY"]:
        prev = closes[-(n + 1):-1]
        avg = sum(prev) / n
    else:
        avg = sum(closes[-n:]) / n

    return current > avg

def exit_option_sl(close: float, entry_price: float) -> bool:
    if not CFG["40_SL_ENABLE"]:
        return False
    sl = float(CFG["41_SL_PCT"]) / 100.0
    return close >= entry_price * (1.0 + sl)

def exit_option_timeout(current_bar: int, entry_bar: int) -> bool:
    if not CFG["50_TIMEOUT_EXIT_ENABLE"]:
        return False
    return (current_bar - entry_bar) >= int(CFG["51_TIMEOUT_BARS"])

def exit_signal(state: EngineState) -> bool:
    pos = state.position
    if pos is None:
        return False

    close = state.close_history[-1]

    if exit_option_sl(close, pos.entry_price):
        return True
    if exit_option_timeout(state.bar, pos.entry_bar):
        return True

    n = int(CFG["30_EXIT_AVG_N"])
    if bella_exit_core_avg_break(state.close_history, n):
        return True

    return False

# ============================================================
# EXECUTION (v7-style order plumbing)
# ============================================================

def place_short_entry(client: "Client", symbol: str, capital_usdt: float, lot: Dict[str, Decimal]) -> Optional[Dict[str, Any]]:
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker["price"])

        leverage = int(CFG["04_LEVERAGE"])
        notional = float(capital_usdt) * float(leverage)

        qty_raw = notional / price
        qty = calculate_quantity(qty_raw, lot)
        if qty is None:
            log.error("entry: qty calculation failed (minQty/stepSize)")
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

def place_short_exit(client: "Client", symbol: str, qty: float, lot: Dict[str, Decimal]) -> bool:
    """
    Short close = BUY with reduceOnly=True
    """
    try:
        qty_rounded = calculate_quantity(qty, lot)
        if qty_rounded is None:
            log.error("exit: qty too small (minQty) — cannot close")
            return False

        client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=qty_rounded,
            reduceOnly=True
        )
        return True
    except Exception as e:
        log.error(f"place_short_exit: {e}")
        return False

# ============================================================
# ENGINE LOOP (closed bar only)
# ============================================================

STOP = False
def _sig_handler(_sig, _frame):
    global STOP
    STOP = True
signal.signal(signal.SIGINT, _sig_handler)
signal.signal(signal.SIGTERM, _sig_handler)

def engine():
    client = init_client()
    symbol = CFG["01_TRADE_SYMBOL"]
    interval = CFG["02_INTERVAL"]
    capital = float(CFG["03_CAPITAL_BASE_USDT"])

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

            completed = kl[-2]                  # closed bar
            open_time = int(completed[0])       # ms

            if st.last_open_time == open_time:
                time.sleep(CFG["91_POLL_SEC"])
                continue

            # ===============================
            # COLD START SEED (RUN ONCE)
            # ===============================
            if not st.close_history:
                for k in kl[:-1]:  # 마지막(현재 미완성봉) 제외, 완료봉만
                    st.close_history.append(float(k[4]))
                st.bar = len(st.close_history)
                st.last_open_time = int(kl[-2][0])
                continue

            st.last_open_time = open_time
            st.bar += 1

            close = float(completed[4])
            st.close_history.append(close)

            # keep history bounded
            if len(st.close_history) > 2000:
                st.close_history = st.close_history[-2000:]

            # -------------------------
            # ENTRY MANAGEMENT FILTERS (cooldown)
            # -------------------------
            if st.bar < st.cooldown_until_bar:
                pass

            # -------------------------
            # POSITION LOGIC
            # -------------------------
            if st.position is None:
                # cooldown check
                if st.bar < st.cooldown_until_bar:
                    continue

                # ENTRY SIGNAL
                sig_entry = short_entry_signal(st.close_history, st.entry_state)
                if sig_entry:
                    order = place_short_entry(client, symbol, capital, lot)
                    if order:
                        st.position = Position(
                            side="SHORT",
                            entry_price=float(order["entry_price"]),
                            qty=float(order["qty"]),
                            entry_bar=st.bar,
                        )
                        on_entry_fired(st.entry_state)

                        cd = int(CFG["20_ENTRY_COOLDOWN_BARS"])
                        if cd > 0:
                            st.cooldown_until_bar = st.bar + cd

                        log.info(f"[ENTRY] SHORT qty={st.position.qty} entry={st.position.entry_price} bar={st.bar}")
                    else:
                        log.error("[ENTRY_FAIL] order failed")
            else:
                # avoid same-bar entry-exit
                if st.position.entry_bar == st.bar:
                    continue

                if exit_signal(st):
                    ok = place_short_exit(client, symbol, st.position.qty, lot)
                    if ok:
                        log.info(f"[EXIT] SHORT close={close} entry={st.position.entry_price} bar={st.bar}")
                        st.position = None

                        # cooldown after exit
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
