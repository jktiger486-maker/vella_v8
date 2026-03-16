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
    "14_SLOPE_THRESHOLD": 0.002,
    "15_SWING_LOOKBACK": 4,

    "23_ENTRY2_ENABLE": True,

    # ---- EXIT EMA (기존 유지) ----
    "30_EXIT_FAST_EMA": 6,
    "31_EXIT_MID_EMA": 10,

    # ---- SL (최우선) ----
    "40_SL_ENABLE": True,
    "41_SL_PCT": 0.8,

    # ---- TIMEOUT (SYNC 포함 전체 적용) ----
    "50_TIMEOUT_EXIT_ENABLE": True,
    "51_TIMEOUT_BARS": 18,

    # ---- TP1 부분익절 ----
    "60_TP1_ENABLE": True,
    "61_TP1_PCT": 0.004,          # 진입가 대비 -0.4% (숏: 가격 하락) 시 1차 익절
    "62_TP1_PARTIAL_PCT": 0.50,   # 포지션의 50% 청산

    # ---- TRAILING (TP1 이후 잔량 전용 / non-SYNC 만) ----
    "70_TRAIL_ENABLE": True,
    "71_TRAIL_CALLBACK_PCT": 0.004,  # 최저가 대비 +0.4% 반등 시 잔량 전량청산

    "90_KLINE_LIMIT": 1500,
    "91_POLL_SEC": 5,
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
# EMA — incremental (옵티 동일 방식)
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
    # ---- TP1 / TRAILING 상태 ----
    # [규칙] tp1_done 변경은 engine()에서 주문 성공 + 재동기화 후에만 허용
    # [규칙] tp1_done / qty 변경은 engine()에서 주문 성공 + 재동기화 후에만 허용
    # [규칙] trail_low 갱신은 exit_signal() 내부 소유 (TRAILING 판단과 일체)
    tp1_done:    bool  = False
    trail_low:   float = float("inf")   # 숏: 포지션 보유 중 최저가 추적

@dataclass
class EngineState:
    bar:            int            = 0
    last_open_time: Optional[int]  = None
    position:       Optional[Position] = None

    close_history: Deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    high_history:  Deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    low_history:   Deque[float] = field(default_factory=lambda: deque(maxlen=2000))

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
# 거래소 포지션 재동기화 헬퍼
# ============================================================

def sync_short_position_state(
    client: "Client",
    symbol: str,
    lot: Dict[str, Decimal],
    st: EngineState,
) -> None:
    """
    거래소 실제 포지션을 조회하여 로컬 st.position 상태를 맞춘다.

    - positionAmt == 0  → st.position = None
    - positionAmt < 0   → 실제 SHORT 존재
        - 기존 st.position 있으면: qty / entry_price만 실제값으로 갱신
          (side / entry_type / tp1_done / trail_low / entry_bar 유지)
        - 기존 st.position 없으면: SYNC 포지션으로 신규 생성
    """
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
                        # 기존 포지션 유지 + qty/entry_price만 실제값으로 갱신
                        old_qty = st.position.qty
                        st.position.qty         = real_qty_str
                        st.position.entry_price = real_entry
                        log.info(
                            f"[RESYNC] SHORT qty {old_qty} → {real_qty_str} "
                            f"entry_price → {real_entry:.8f}"
                        )
                    else:
                        # 포지션 없던 상태에서 실제 SHORT 발견 → SYNC 포지션 생성
                        st.position = Position(
                            side="SHORT",
                            entry_price=real_entry,
                            qty=real_qty_str,
                            entry_bar=st.bar,
                            entry_type="SYNC",
                            tp1_done=True,       # TP1 재시도 금지
                            trail_low=float("inf"),
                        )
                        log.info(
                            f"[RESYNC] NEW SYNC SHORT detected qty={real_qty_str} "
                            f"entry={real_entry:.8f} tp1_done=True(safe_mode)"
                        )
                    return

        # symbol 없으면 포지션 없는 것으로 처리
        if st.position is not None:
            log.info(f"[RESYNC] symbol not found in positions → st.position=None")
        st.position = None

    except Exception as e:
        log.error(f"[RESYNC] sync_short_position_state failed: {e}")

# ============================================================
# ENTRY SIGNALS (FROZEN — 절대 수정 금지)
# ============================================================

def short_entry_signals(st: EngineState) -> str:
    if not _warmup_done(st):
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
    pullback  = st.high_history[-2] >= fast_prev * (1.0 - tolerance)
    reentry   = st.close_history[-1] < fast_now
    e2_signal = pullback and reentry

    if e1_signal:
        return "E1"
    if CFG["23_ENTRY2_ENABLE"] and e2_signal:
        return "E2"
    return ""

# ============================================================
# EXIT — 확장 구조
# ============================================================
# 반환값:
#   ("NONE",    None)             → 청산 없음
#   ("FULL",    reason_str)       → 전량청산
#   ("PARTIAL", partial_qty_str)  → 부분청산 (TP1)
#
# [규칙] exit_signal() 내부에서 pos.tp1_done / pos.qty 변경 금지
# [규칙] pos.trail_low 갱신은 exit_signal() 내부 소유 — TRAILING 판단과 일체로 처리
# [규칙] 상태 변경은 engine() 주문 성공 + 재동기화 후에만 허용

def exit_signal(st: EngineState, lot: Dict[str, Decimal]):
    pos = st.position
    if pos is None:
        return ("NONE", None)

    close = st.close_history[-1]
    low   = st.low_history[-1]

    # ----------------------------------------------------------
    # [1] SL — 최우선 전량청산
    # ----------------------------------------------------------
    if CFG["40_SL_ENABLE"]:
        sl = float(CFG["41_SL_PCT"]) / 100.0
        if close >= pos.entry_price * (1.0 + sl):
            log.info(f"[EXIT_SL] close={close:.8f} >= SL={pos.entry_price * (1.0 + sl):.8f}")
            return ("FULL", "SL")

    # ----------------------------------------------------------
    # [2] TIMEOUT — 전량청산 (SYNC 포함 모든 포지션)
    # ----------------------------------------------------------
    if CFG["50_TIMEOUT_EXIT_ENABLE"]:
        if (st.bar - pos.entry_bar) >= int(CFG["51_TIMEOUT_BARS"]):
            log.info(f"[EXIT_TIMEOUT] bars={st.bar - pos.entry_bar} entry_type={pos.entry_type}")
            return ("FULL", "TIMEOUT")

    # ----------------------------------------------------------
    # [3] EMA CROSS — 언제든 전량청산 (상태가 아닌 CROSS 이벤트)
    # ----------------------------------------------------------
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

    # ----------------------------------------------------------
    # [4] TP1 — 1차 부분익절 (non-SYNC + tp1_done=False 일 때만)
    # ----------------------------------------------------------
    if CFG["60_TP1_ENABLE"] and not pos.tp1_done and pos.entry_type != "SYNC":
        tp1_pct    = float(CFG["61_TP1_PCT"])
        tp1_target = pos.entry_price * (1.0 - tp1_pct)   # 숏: 가격 하락이 수익
        if close <= tp1_target:
            partial_ratio = float(CFG["62_TP1_PARTIAL_PCT"])
            qty_full      = Decimal(pos.qty)
            qty_partial   = qty_full * Decimal(str(partial_ratio))

            # ▶ partial_str 계산 (calculate_quantity 유지 — stepSize 규격 보장)
            partial_str = calculate_quantity(qty_partial, lot)
            if partial_str is None:
                # partial qty 자체가 minQty 미만 → FULL 승격 (dust 방지)
                log.warning(
                    f"[TP1_DUST_PREVENT] partial qty < minQty → FULL EXIT upgrade "
                    f"pos.qty={pos.qty} bar={st.bar}"
                )
                return ("FULL", "TP1_DUST_TO_FULL")

            # ▶ remaining 사전 계산 — PARTIAL 실행 전 dust 검증
            remaining     = qty_full - Decimal(partial_str)
            remaining_str = calculate_quantity(remaining, lot)
            if remaining_str is None:
                # 잔량 minQty 미만 → 거래소 dust 발생 방지 → FULL 승격
                log.warning(
                    f"[TP1_DUST_PREVENT] remaining < minQty → FULL EXIT upgrade "
                    f"pos.qty={pos.qty} partial={partial_str} bar={st.bar}"
                )
                return ("FULL", "TP1_DUST_TO_FULL")

            # ▶ 정상 PARTIAL 반환 (tp1_done 변경 금지 — engine()에서 처리)
            log.info(
                f"[EXIT_TP1] close={close:.8f} <= target={tp1_target:.8f} "
                f"partial={partial_str} remaining={remaining_str} full={pos.qty}"
            )
            return ("PARTIAL", partial_str)

    # ----------------------------------------------------------
    # [5] TRAILING — TP1 이후 잔량 전용 (non-SYNC 포지션만)
    # ----------------------------------------------------------
    if CFG["70_TRAIL_ENABLE"] and pos.tp1_done and pos.entry_type != "SYNC":
        # 최저가 갱신 (trail_low 소유권은 exit_signal 내부)
        if low < pos.trail_low:
            pos.trail_low = low
            log.debug(f"[TRAIL_LOW_UPDATE] trail_low={pos.trail_low:.8f}")

        trail_pct  = float(CFG["71_TRAIL_CALLBACK_PCT"])
        trail_stop = pos.trail_low * (1.0 + trail_pct)   # 최저가에서 +N% 반등 시 청산
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
    """
    sync_short_position_state() 를 최대 retries 회 재시도한다.
    체결 반영 지연(바이낸스 0.5~1초) 대비용.
    매 시도 사이 delay 초 대기.
    """
    for attempt in range(1, retries + 1):
        time.sleep(delay)
        sync_short_position_state(client, symbol, lot, st)
        log.debug(
            f"[SYNC_RETRY] attempt={attempt}/{retries} "
            f"position={'exists qty=' + st.position.qty if st.position else 'None'}"
        )


def place_short_entry(client: "Client", symbol: str, capital_usdt: float, lot: Dict[str, Decimal]) -> Optional[str]:
    """
    시장가 SHORT 진입 주문.
    반환값: 주문에 사용한 qty_str (성공 시) / None (실패 시)
    entry_price 는 주문 후 sync_short_position_state() 로 실제값 확정.
    """
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

    # ---- 시작 시 포지션 SYNC ----
    # SYNC 포지션 정책: TP1/TRAILING 없음, SL+EMA_CROSS+TIMEOUT 만
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

            # ▶ ARENA 로그
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
                        # 주문 성공 → retry sync 로 실제 entry_price / qty 확정
                        # (ticker 현재가가 아닌 실제 체결 평균가 사용)
                        # sleep(0.5) x 3회 retry — 바이낸스 체결 반영 지연 대비
                        _sync_with_retry(client, symbol, lot, st, retries=3, delay=0.5)
                        if st.position is not None:
                            # sync가 SYNC entry_type으로 생성하므로 실제 entry_type 복원
                            # tp1_done=False 초기화 필수 — sync가 True로 만들기 때문
                            st.position.entry_type = entry_type
                            st.position.entry_bar  = st.bar
                            st.position.tp1_done   = False
                            log.info(
                                f"[ENTRY] SHORT type={entry_type} qty={st.position.qty} "
                                f"entry={st.position.entry_price:.8f} bar={st.bar}"
                            )
                        else:
                            # sync 실패 — 거래소에 포지션 있을 수 있음
                            # 다음 바 루프 진입 시 st.position=None 상태이므로
                            # ENTRY 신호가 다시 발생할 수 있음 → 이중 진입 방지를 위해
                            # 한 번 더 즉시 재조회 시도
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

                # ----------------------------------------------------------
                # FULL EXIT
                # ----------------------------------------------------------
                if exit_type == "FULL":
                    ok = place_short_exit(client, symbol, st.position.qty, lot)
                    if ok:
                        log.info(
                            f"[EXIT_FULL] reason={exit_data} type={st.position.entry_type} "
                            f"close={st.close_history[-1]:.8f} entry={st.position.entry_price:.8f} bar={st.bar}"
                        )
                        # 주문 성공 후 retry sync — 부분체결 / 수량차 가능성 대비
                        _sync_with_retry(client, symbol, lot, st, retries=3, delay=0.5)
                    else:
                        log.error(f"[EXIT_FULL_FAIL] reason={exit_data} order failed → resync")
                        sync_short_position_state(client, symbol, lot, st)

                # ----------------------------------------------------------
                # PARTIAL EXIT (TP1)
                # ----------------------------------------------------------
                elif exit_type == "PARTIAL":
                    partial_qty = exit_data
                    ok = place_short_exit(client, symbol, partial_qty, lot)
                    if ok:
                        log.info(
                            f"[EXIT_TP1_ORDER_OK] partial={partial_qty} bar={st.bar} → resync"
                        )
                        # 주문 성공 후 retry sync — 실제 잔량으로 qty 확정
                        _sync_with_retry(client, symbol, lot, st, retries=3, delay=0.5)

                        if st.position is not None:
                            # 실제 잔량 존재 확인 후에만 tp1_done=True 설정
                            # [규칙] tp1_done=True: partial 성공 + 재동기화 후 실제 잔량 확인 시에만
                            st.position.tp1_done  = True
                            st.position.trail_low = st.low_history[-1]
                            log.info(
                                f"[EXIT_TP1] remaining={st.position.qty} "
                                f"trail_low_init={st.position.trail_low:.8f} bar={st.bar}"
                            )
                        else:
                            # 재조회 결과 포지션 없음 → 전량 청산 완료로 처리
                            log.info(f"[EXIT_TP1] position fully closed after resync bar={st.bar}")

                    else:
                        # TP1 partial 주문 실패
                        # [규칙] 주문 실패 시 tp1_done 절대 변경 금지
                        log.error(
                            f"[EXIT_TP1_FAIL] partial order failed partial={partial_qty} → resync + force FULL"
                        )
                        # 즉시 재조회 — 실제로 주문이 들어갔을 수도 있음
                        sync_short_position_state(client, symbol, lot, st)

                        if st.position is not None:
                            # 포지션 살아있음 → 전량청산 강제 시도
                            log.error(
                                f"[EXIT_TP1_FAIL] position still alive → force FULL exit qty={st.position.qty}"
                            )
                            ok2 = place_short_exit(client, symbol, st.position.qty, lot)
                            if ok2:
                                log.info(
                                    f"[EXIT_TP1_FORCE_FULL] qty={st.position.qty} "
                                    f"close={st.close_history[-1]:.8f} bar={st.bar}"
                                )
                                _sync_with_retry(client, symbol, lot, st, retries=3, delay=0.5)
                            else:
                                log.error(
                                    "[EXIT_TP1_FORCE_FULL_FAIL] force full also failed → resync, will retry next bar"
                                )
                                sync_short_position_state(client, symbol, lot, st)
                        else:
                            # 재조회 결과 포지션 없음 → partial이 실제로는 성공했던 것
                            log.info(
                                f"[EXIT_TP1_FAIL_BUT_CLOSED] position already closed after resync bar={st.bar}"
                            )

        except Exception as e:
            log.error(f"engine loop error: {e}")
            time.sleep(CFG["91_POLL_SEC"])

    log.info("STOP BR8 SHORT")

if __name__ == "__main__":
    engine()
