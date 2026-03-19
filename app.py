# ============================================================
# VELLA_BR8 — SHORT ENGINE  (최종 안정화 / 재동기화 중심)
# ENTRY LOGIC FROZEN / EXIT EXPANDED + RESYNC
#
# EXIT 우선순위:
#   [1] SL       — 최우선 전량청산
#   [2] TIMEOUT  — 전량청산 (SYNC 포함)
#   [3] EMA CROSS — CROSS 이벤트 전량청산
#   [4] TP1      — 부분익절 (non-SYNC 포지션만, 1회)
#   [5] TRAILING — TP1 이후 잔량 전용 (non-SYNC 포지션만)
#
# 재동기화 원칙:
#   - ENTRY 주문 성공 후  → sync_short_position_state() 로 실제 entry_price/qty 확정
#   - FULL EXIT 성공 후   → sync 후 포지션 0이면 None, 잔량 있으면 반영
#   - PARTIAL EXIT 성공 후→ sync 후 실제 잔량으로 qty 확정, tp1_done=True 설정
#   - 주문 실패 후        → 즉시 sync 로 로컬-거래소 불일치 최소화
#
# SYNC 포지션 정책:
#   - TP1 없음 / TRAILING 없음
#   - SL + EMA_CROSS + TIMEOUT 만 허용
#   - tp1_done=True 고정 (TP1 재시도 방지)
#
# 상태 변경 원칙:
#   - exit_signal() 내부에서 tp1_done / qty 변경 금지
#   - trail_low 갱신은 exit_signal() 내부 소유 (TRAILING 판단과 일체)
#   - 상태 변경은 주문 성공 후 + 재동기화 결과 기준으로만
# ============================================================

import os
import sys
import time
import signal
import logging
import requests
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Deque
from collections import deque

# ============================================================
# CFG
# ============================================================

CFG = {
    "01_TRADE_SYMBOL": "TIAUSDT",
    "02_INTERVAL": "5m",
    "03_CAPITAL_BASE_USDT": 10.0,
    "04_LEVERAGE": 1,

    # ---- ENTRY EMA (FROZEN) ----
    "10_EMA_FAST": 8,
    "11_EMA_MID": 14,
    "12_EMA_ARENA": 30,

    # ---- 필터 강화 (FROZEN) ----
    "13_TOUCH_TOLERANCE": 0.002,
    "14_SLOPE_THRESHOLD": 0.0015,   # 0.006 → 0.003 → 0.002 → 0.0015
    "15_SWING_LOOKBACK": 4,

    # ---- ATR FILTER (횡보 차단 전용) ----
    "16_ATR_FILTER_ENABLE": True,
    "17_ATR_PERIOD": 14,
    "18_ATR_THRESHOLD_PCT": 0.002,   # 0.004 → 0.003 → 0.002

    "23_ENTRY2_ENABLE": True,

    # ---- EXIT EMA (기존 유지) ----
    "30_EXIT_FAST_EMA": 4,
    "31_EXIT_MID_EMA": 8,

    # ---- SL (최우선) ----
    "40_SL_ENABLE": True,
    "41_SL_PCT": 0.8,

    # ---- TIMEOUT (SYNC 포함 전체 적용) ----
    "50_TIMEOUT_EXIT_ENABLE": True,
    "51_TIMEOUT_BARS": 12,

    # ---- TP1 부분익절 ----
    "60_TP1_ENABLE": True,
    "61_TP1_PCT": 0.006,
    "62_TP1_PARTIAL_PCT": 0.50,

    # ---- TRAILING (TP1 이후 잔량 전용 / non-SYNC 만) ----
    "70_TRAIL_ENABLE": True,
    "71_TRAIL_CALLBACK_PCT": 0.006,

    "90_KLINE_LIMIT": 1500,
    "91_POLL_SEC": 5,
    "92_LOG_LEVEL": "DEBUG",   # INFO → DEBUG
}

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=getattr(logging, CFG["92_LOG_LEVEL"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("VELLA_BR8_SHORT")

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

def init_client() -> "Client":
    if Client is None:
        raise RuntimeError("python-binance missing")
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("Missing BINANCE_API_KEY / BINANCE_API_SECRET")
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
                            "minQty":   Decimal(f["minQty"]),
                            "maxQty":   Decimal(f["maxQty"]),
                        }
        return None
    except Exception as e:
        log.error(f"get_futures_lot_size: {e}")
        return None

# ============================================================
# QTY (str 통일)
# ============================================================

def calculate_quantity(qty_raw, lot: Dict[str, Decimal]) -> Optional[str]:
    if lot is None:
        return None
    qty_decimal = Decimal(str(qty_raw))
    step = lot["stepSize"]
    qty  = (qty_decimal / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
    if qty < lot["minQty"]:
        return None
    if qty > lot["maxQty"]:
        qty = lot["maxQty"]
    precision = abs(step.as_tuple().exponent)
    return f"{qty:.{precision}f}"

def normalize_qty_str(qty_str: str, lot: Dict[str, Decimal]) -> Optional[str]:
    if lot is None:
        return None
    qty_decimal = Decimal(qty_str)
    step = lot["stepSize"]
    qty  = (qty_decimal / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
    if qty < lot["minQty"]:
        return None
    if qty > lot["maxQty"]:
        qty = lot["maxQty"]
    precision = abs(step.as_tuple().exponent)
    return f"{qty:.{precision}f}"

# ============================================================
# EMA — incremental
# ============================================================

class IncrementalEMA:
    def __init__(self, period: int):
        self.period  = period
        self.k       = 2.0 / (period + 1)
        self.value   = None
        self.ready   = False
        self._buf: List[float] = []
        self._history: Deque[float] = deque()

    def update(self, price: float) -> None:
        if not self.ready:
            self._buf.append(price)
            if len(self._buf) >= self.period:
                self.value = sum(self._buf) / len(self._buf)
                self.ready = True
                self._buf  = []
        else:
            self.value = price * self.k + self.value * (1.0 - self.k)
        if self.ready:
            self._history.append(self.value)

    def get(self) -> Optional[float]:
        return self.value if self.ready else None

    def get_prev(self) -> Optional[float]:
        if len(self._history) >= 2:
            return self._history[-2]
        return None

    def get_lookback(self, n: int) -> Optional[float]:
        if len(self._history) > n:
            return self._history[-(n + 1)]
        return None

    def trim_history(self, maxlen: int = 2100) -> None:
        while len(self._history) > maxlen:
            self._history.popleft()

# ============================================================
# STATE
# ============================================================

@dataclass
class Position:
    side:        str
    entry_price: float
    qty:         str
    entry_bar:   int
    entry_type:  str   = "E1"
    tp1_done:     bool  = False
    trail_low:    float = float("inf")
    be_activated: bool  = False

@dataclass
class EngineState:
    bar:            int            = 0
    last_open_time: Optional[int]  = None
    position:       Optional[Position] = None

    close_history: Deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    high_history:  Deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    low_history:   Deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    atr_history:   Deque[float] = field(default_factory=lambda: deque(maxlen=200))

    ema_fast:      IncrementalEMA = field(default_factory=lambda: IncrementalEMA(CFG["10_EMA_FAST"]))
    ema_mid:       IncrementalEMA = field(default_factory=lambda: IncrementalEMA(CFG["11_EMA_MID"]))
    ema_arena:     IncrementalEMA = field(default_factory=lambda: IncrementalEMA(CFG["12_EMA_ARENA"]))
    ema_exit_fast: IncrementalEMA = field(default_factory=lambda: IncrementalEMA(CFG["30_EXIT_FAST_EMA"]))
    ema_exit_mid:  IncrementalEMA = field(default_factory=lambda: IncrementalEMA(CFG["31_EXIT_MID_EMA"]))

    prev_arena_state: Optional[bool] = None

# ============================================================
# WARMUP
# ============================================================

def _warmup_done(st: EngineState) -> bool:
    swing  = int(CFG["15_SWING_LOOKBACK"])
    needed = max(
        CFG["10_EMA_FAST"],
        CFG["11_EMA_MID"],
        CFG["12_EMA_ARENA"],
        CFG["30_EXIT_FAST_EMA"],
        CFG["31_EXIT_MID_EMA"],
        swing + 2,
        62,
    )
    return st.bar >= needed

# ============================================================
# 거래소 포지션 재동기화
# ============================================================

def sync_short_position_state(
    client: "Client",
    symbol: str,
    lot: Dict[str, Decimal],
    st: EngineState,
) -> None:
    try:
        positions = client.futures_position_information(symbol=symbol)
        for pos in positions:
            if pos["symbol"] == symbol:
                position_amt = float(pos["positionAmt"])
                real_entry   = float(pos["entryPrice"])

                if position_amt == 0:
                    if st.position is not None:
                        log.info(f"[RESYNC] positionAmt=0 → st.position=None")
                    st.position = None
                    return

                if position_amt < 0:
                    real_qty_str = calculate_quantity(abs(position_amt), lot)
                    if real_qty_str is None:
                        log.error(f"[RESYNC] qty calculation failed for positionAmt={position_amt}")
                        return

                    if st.position is not None:
                        old_qty = st.position.qty
                        st.position.qty         = real_qty_str
                        st.position.entry_price = real_entry
                        log.info(
                            f"[RESYNC] SHORT qty {old_qty} → {real_qty_str} "
                            f"entry_price → {real_entry:.8f}"
                        )
                    else:
                        st.position = Position(
                            side="SHORT",
                            entry_price=real_entry,
                            qty=real_qty_str,
                            entry_bar=st.bar,
                            entry_type="SYNC",
                            tp1_done=True,
                            trail_low=float("inf"),
                        )
                        log.info(
                            f"[RESYNC] NEW SYNC SHORT detected qty={real_qty_str} "
                            f"entry={real_entry:.8f} tp1_done=True(safe_mode)"
                        )
                    return

        if st.position is not None:
            log.info(f"[RESYNC] symbol not found in positions → st.position=None")
        st.position = None

    except Exception as e:
        log.error(f"[RESYNC] sync_short_position_state failed: {e}")

# ============================================================
# ENTRY SIGNALS (FROZEN)
# ============================================================

def short_entry_signals(st: EngineState) -> str:
    if not _warmup_done(st):
        return ""

    if CFG["16_ATR_FILTER_ENABLE"]:
        if len(st.atr_history) < CFG["17_ATR_PERIOD"]:
            return ""
        atr = sum(list(st.atr_history)[-CFG["17_ATR_PERIOD"]:]) / CFG["17_ATR_PERIOD"]
        atr_pct = atr / st.close_history[-1]
        if atr_pct < CFG["18_ATR_THRESHOLD_PCT"]:
            log.debug(f"[ATR_BLOCK] atr_pct={atr_pct:.4f}")
            return ""

    fast  = st.ema_fast
    mid   = st.ema_mid
    arena = st.ema_arena

    if not (fast.ready and mid.ready and arena.ready):
        return ""

    fast_now  = fast.get()
    fast_prev = fast.get_prev()
    mid_now   = mid.get()
    mid_prev  = mid.get_prev()
    arena_now = arena.get()

    if fast_prev is None or mid_prev is None:
        return ""

    close_now   = st.close_history[-1]
    short_arena = (
        (close_now < arena_now) and
        (fast_now  < arena_now) and
        (mid_now   < arena_now)
    )
    if not short_arena:
        log.debug(
            f"[ARENA_BLOCK] close={close_now:.8f} fast={fast_now:.8f} "
            f"mid={mid_now:.8f} arena={arena_now:.8f}"
        )
        return ""

    # DISTANCE FILTER — 0.005 → 0.015 → 0.010
    distance_from_arena = (arena_now - close_now) / arena_now
    if distance_from_arena > 0.010:
        log.debug(f"[DIST_BLOCK] distance={distance_from_arena:.4f}")
        return ""

    arena_prev = arena.get_prev()
    if arena_prev is None:
        return ""
    if arena_now > arena_prev:   # 상승 시만 차단, 평탄 허용
        log.debug(f"[TREND_BLOCK] arena not falling")
        return ""

    swing_lookback  = int(CFG["15_SWING_LOOKBACK"])
    slope_threshold = float(CFG["14_SLOPE_THRESHOLD"])
    ref = fast.get_lookback(swing_lookback)
    if ref is None or ref == 0:
        return ""
    slope_val = (fast_now - ref) / ref
    slope_ok  = slope_val <= -slope_threshold
    if not slope_ok:
        return ""

    e1_signal = (fast_prev >= mid_prev) and (fast_now < mid_now)

    tolerance = float(CFG["13_TOUCH_TOLERANCE"])
    if len(st.high_history) < 2 or len(st.close_history) < 1:
        return ""
    pullback  = (
        (st.high_history[-2] >= mid_prev) and
        (fast_now < mid_now) and
        (mid_now  < arena_now)
    )
    reentry   = st.close_history[-1] < fast_now
    e2_signal = pullback and reentry

    if e1_signal:
        return "E1"
    if CFG["23_ENTRY2_ENABLE"] and e2_signal:
        return "E2"
    return ""

# ============================================================
# EXIT
# ============================================================

def exit_signal(st: EngineState, lot: Dict[str, Decimal]):
    pos = st.position
    if pos is None:
        return ("NONE", None)

    close = st.close_history[-1]
    low   = st.low_history[-1]

    # [1] SL
    if CFG["40_SL_ENABLE"]:
        sl = float(CFG["41_SL_PCT"]) / 100.0
        if pos.be_activated:
            sl_price = pos.entry_price
        else:
            sl_price = pos.entry_price * (1.0 + sl)
        if close >= sl_price:
            log.info(f"[EXIT_SL] close={close:.8f} >= SL={sl_price:.8f} be={pos.be_activated}")
            return ("FULL", "SL")

    # [2] TIMEOUT
    if CFG["50_TIMEOUT_EXIT_ENABLE"]:
        if (st.bar - pos.entry_bar) >= int(CFG["51_TIMEOUT_BARS"]):
            log.info(f"[EXIT_TIMEOUT] bars={st.bar - pos.entry_bar} entry_type={pos.entry_type}")
            return ("FULL", "TIMEOUT")

    # [3] EMA CROSS
    ef = st.ema_exit_fast.get()
    em = st.ema_exit_mid.get()
    if ef is not None and em is not None:
        prev_fast = st.ema_exit_fast.get_prev()
        prev_mid  = st.ema_exit_mid.get_prev()
        if prev_fast is not None and prev_mid is not None:
            cross_up = (prev_fast <= prev_mid) and (ef > em)
            if cross_up:
                log.info(f"[EXIT_EMA_CROSS] ef={ef:.8f} em={em:.8f} close={close:.8f}")
                return ("FULL", "EMA_CROSS")

    # [4] TP1
    if CFG["60_TP1_ENABLE"] and not pos.tp1_done and pos.entry_type != "SYNC":
        tp1_pct    = float(CFG["61_TP1_PCT"])
        tp1_target = pos.entry_price * (1.0 - tp1_pct)
        if close <= tp1_target:
            partial_ratio = float(CFG["62_TP1_PARTIAL_PCT"])
            qty_full      = Decimal(pos.qty)
            qty_partial   = qty_full * Decimal(str(partial_ratio))

            partial_str = calculate_quantity(qty_partial, lot)
            if partial_str is None:
                log.warning(f"[TP1_DUST_PREVENT] partial qty < minQty → FULL EXIT upgrade")
                return ("FULL", "TP1_DUST_TO_FULL")

            remaining     = qty_full - Decimal(partial_str)
            remaining_str = calculate_quantity(remaining, lot)
            if remaining_str is None:
                log.warning(f"[TP1_DUST_PREVENT] remaining < minQty → FULL EXIT upgrade")
                return ("FULL", "TP1_DUST_TO_FULL")

            log.info(
                f"[EXIT_TP1] close={close:.8f} <= target={tp1_target:.8f} "
                f"partial={partial_str} remaining={remaining_str} full={pos.qty}"
            )
            return ("PARTIAL", partial_str)

    # BE ACTIVATE
    if pos.entry_type != "SYNC" and not pos.be_activated:
        be_trigger = pos.entry_price * (1.0 - 0.006)
        if close <= be_trigger:
            return ("BE_ACTIVATE", None)

    # [5] TRAILING
    if CFG["70_TRAIL_ENABLE"] and pos.tp1_done and pos.entry_type != "SYNC":
        if low < pos.trail_low:
            pos.trail_low = low
            log.debug(f"[TRAIL_LOW_UPDATE] trail_low={pos.trail_low:.8f}")

        trail_pct  = float(CFG["71_TRAIL_CALLBACK_PCT"])
        trail_stop = pos.trail_low * (1.0 + trail_pct)
        if close >= trail_stop:
            log.info(
                f"[EXIT_TRAIL] close={close:.8f} >= trail_stop={trail_stop:.8f} "
                f"(trail_low={pos.trail_low:.8f})"
            )
            return ("FULL", "TRAIL")

    return ("NONE", None)

# ============================================================
# EXECUTION
# ============================================================

def _sync_with_retry(
    client: "Client",
    symbol: str,
    lot: Dict[str, Decimal],
    st: EngineState,
    retries: int = 3,
    delay: float = 0.5,
) -> None:
    for attempt in range(1, retries + 1):
        time.sleep(delay)
        sync_short_position_state(client, symbol, lot, st)
        log.debug(
            f"[SYNC_RETRY] attempt={attempt}/{retries} "
            f"position={'exists qty=' + st.position.qty if st.position else 'None'}"
        )

def place_short_entry(client: "Client", symbol: str, capital_usdt: float, lot: Dict[str, Decimal]) -> Optional[str]:
    try:
        ticker   = client.futures_symbol_ticker(symbol=symbol)
        price    = float(ticker["price"])
        leverage = int(CFG["04_LEVERAGE"])
        notional = float(capital_usdt) * float(leverage)
        qty_raw  = notional / price
        qty_str  = calculate_quantity(qty_raw, lot)
        if qty_str is None:
            log.error("entry: qty calculation failed")
            return None
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=qty_str,
        )
        return qty_str
    except Exception as e:
        log.error(f"place_short_entry: {e}")
        return None

def place_short_exit(client: "Client", symbol: str, qty: str, lot: Dict[str, Decimal]) -> bool:
    try:
        qty2 = normalize_qty_str(qty, lot)
        if qty2 is None:
            log.error("exit: qty too small")
            return False
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=qty2,
            reduceOnly=True
        )
        return True
    except Exception as e:
        log.error(f"place_short_exit: {e}")
        return False

# ============================================================
# ENGINE LOOP
# ============================================================

STOP = False
def _sig_handler(_sig, _frame):
    global STOP
    STOP = True
signal.signal(signal.SIGINT, _sig_handler)
signal.signal(signal.SIGTERM, _sig_handler)

def _apply_bar(st: EngineState, close: float, high: float, low: float) -> None:
    st.close_history.append(close)
    st.high_history.append(high)
    st.low_history.append(low)

    st.ema_fast.update(close)
    st.ema_mid.update(close)
    st.ema_arena.update(close)
    st.ema_exit_fast.update(close)
    st.ema_exit_mid.update(close)

    st.ema_fast.trim_history()
    st.ema_mid.trim_history()
    st.ema_arena.trim_history()
    st.ema_exit_fast.trim_history()
    st.ema_exit_mid.trim_history()

    if len(st.close_history) >= 2:
        prev_close = st.close_history[-2]
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        st.atr_history.append(tr)

def engine():
    client   = init_client()
    symbol   = CFG["01_TRADE_SYMBOL"]
    interval = CFG["02_INTERVAL"]
    capital  = float(CFG["03_CAPITAL_BASE_USDT"])

    set_leverage(client, symbol, int(CFG["04_LEVERAGE"]))

    lot = get_futures_lot_size(client, symbol)
    if lot is None:
        raise RuntimeError("lot_size retrieval failed")

    st = EngineState()

    sync_short_position_state(client, symbol, lot, st)
    if st.position is not None:
        log.info(
            f"[BOOT_SYNC] SHORT position restored: qty={st.position.qty} "
            f"entry={st.position.entry_price:.8f} entry_type={st.position.entry_type}"
        )

    log.info(
        f"START BR8 SHORT | symbol={symbol} interval={interval} capital={capital} "
        f"lev={CFG['04_LEVERAGE']} "
        f"| ENTRY_EMA=({CFG['10_EMA_FAST']},{CFG['11_EMA_MID']},{CFG['12_EMA_ARENA']}) "
        f"| EXIT_EMA=({CFG['30_EXIT_FAST_EMA']},{CFG['31_EXIT_MID_EMA']}) "
        f"| SLOPE={CFG['14_SLOPE_THRESHOLD']} ATR={CFG['18_ATR_THRESHOLD_PCT']} "
        f"| TP1={CFG['61_TP1_PCT']*100:.2f}%x{CFG['62_TP1_PARTIAL_PCT']*100:.0f}% "
        f"| TRAIL={CFG['71_TRAIL_CALLBACK_PCT']*100:.2f}%"
    )

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
                    _apply_bar(st, float(k[4]), float(k[2]), float(k[3]))
                st.bar = len(st.close_history)
                st.last_open_time = int(kl[-2][0])
                log.info(f"[BOOT] {st.bar} bars loaded, EMA warm-up complete={_warmup_done(st)}")
                continue

            st.last_open_time = open_time
            st.bar += 1

            _apply_bar(st, float(completed[4]), float(completed[2]), float(completed[3]))

            if st.ema_fast.ready and st.ema_mid.ready and st.ema_arena.ready and st.close_history:
                close_now   = st.close_history[-1]
                arena_now   = st.ema_arena.get()
                short_arena_now = (
                    (close_now         < arena_now) and
                    (st.ema_fast.get() < arena_now) and
                    (st.ema_mid.get()  < arena_now)
                )
                if short_arena_now != st.prev_arena_state:
                    if short_arena_now:
                        log.info(f"[ARENA] 통과 close={close_now:.8f} < arena={arena_now:.8f}")
                    else:
                        log.info(f"[ARENA] 차단 close={close_now:.8f} arena={arena_now:.8f}")
                    st.prev_arena_state = short_arena_now

            # ============================================================
            # ENTRY
            # ============================================================
            if st.position is None:
                entry_type = short_entry_signals(st)
                if entry_type:
                    qty_str = place_short_entry(client, symbol, capital, lot)
                    if qty_str:
                        _sync_with_retry(client, symbol, lot, st, retries=3, delay=0.5)
                        if st.position is not None:
                            st.position.entry_type = entry_type
                            st.position.entry_bar  = st.bar
                            st.position.tp1_done   = False
                            log.info(
                                f"[ENTRY] SHORT type={entry_type} qty={st.position.qty} "
                                f"entry={st.position.entry_price:.8f} bar={st.bar}"
                            )
                        else:
                            log.error(
                                f"[ENTRY_SYNC_FAIL] position not found after 3 retries "
                                f"qty={qty_str} bar={st.bar} → immediate re-sync"
                            )
                            sync_short_position_state(client, symbol, lot, st)
                            if st.position is not None:
                                st.position.entry_type = entry_type
                                st.position.entry_bar  = st.bar
                                st.position.tp1_done   = False
                                log.info(
                                    f"[ENTRY_SYNC_RECOVERED] SHORT type={entry_type} "
                                    f"qty={st.position.qty} entry={st.position.entry_price:.8f} bar={st.bar}"
                                )
                            else:
                                log.error(
                                    f"[ENTRY_SYNC_FAIL_FINAL] position still not found. "
                                    f"Will re-check next bar. qty={qty_str} bar={st.bar}"
                                )
                    else:
                        log.error("[ENTRY_FAIL] order failed")

            # ============================================================
            # EXIT
            # ============================================================
            else:
                if st.position.entry_bar == st.bar:
                    continue

                exit_type, exit_data = exit_signal(st, lot)

                if exit_type == "BE_ACTIVATE":
                    if st.position is not None and not st.position.be_activated:
                        st.position.be_activated = True
                        log.info(
                            f"[BE_ACTIVATED] close={st.close_history[-1]:.8f} "
                            f"entry={st.position.entry_price:.8f} bar={st.bar}"
                        )

                elif exit_type == "FULL":
                    ok = place_short_exit(client, symbol, st.position.qty, lot)
                    if ok:
                        log.info(
                            f"[EXIT_FULL] reason={exit_data} type={st.position.entry_type} "
                            f"close={st.close_history[-1]:.8f} entry={st.position.entry_price:.8f} bar={st.bar}"
                        )
                        _sync_with_retry(client, symbol, lot, st, retries=3, delay=0.5)
                    else:
                        log.error(f"[EXIT_FULL_FAIL] reason={exit_data} order failed → resync")
                        sync_short_position_state(client, symbol, lot, st)

                elif exit_type == "PARTIAL":
                    partial_qty = exit_data
                    ok = place_short_exit(client, symbol, partial_qty, lot)
                    if ok:
                        log.info(f"[EXIT_TP1_ORDER_OK] partial={partial_qty} bar={st.bar} → resync")
                        _sync_with_retry(client, symbol, lot, st, retries=3, delay=0.5)
                        if st.position is not None:
                            st.position.tp1_done  = True
                            st.position.trail_low = st.low_history[-1]
                            log.info(
                                f"[EXIT_TP1] remaining={st.position.qty} "
                                f"trail_low_init={st.position.trail_low:.8f} bar={st.bar}"
                            )
                        else:
                            log.info(f"[EXIT_TP1] position fully closed after resync bar={st.bar}")
                    else:
                        log.error(f"[EXIT_TP1_FAIL] partial order failed partial={partial_qty} → resync + force FULL")
                        sync_short_position_state(client, symbol, lot, st)
                        if st.position is not None:
                            log.error(f"[EXIT_TP1_FAIL] position still alive → force FULL exit qty={st.position.qty}")
                            ok2 = place_short_exit(client, symbol, st.position.qty, lot)
                            if ok2:
                                log.info(f"[EXIT_TP1_FORCE_FULL] qty={st.position.qty} close={st.close_history[-1]:.8f} bar={st.bar}")
                                _sync_with_retry(client, symbol, lot, st, retries=3, delay=0.5)
                            else:
                                log.error("[EXIT_TP1_FORCE_FULL_FAIL] force full also failed → resync, will retry next bar")
                                sync_short_position_state(client, symbol, lot, st)
                        else:
                            log.info(f"[EXIT_TP1_FAIL_BUT_CLOSED] position already closed after resync bar={st.bar}")

        except Exception as e:
            log.error(f"engine loop error: {e}")
            time.sleep(CFG["91_POLL_SEC"])

    log.info("STOP BR8 SHORT")

if __name__ == "__main__":
    engine()