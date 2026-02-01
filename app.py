# ============================================================
# VELLA V8 — app_min.py
# REALTIME ENGINE FIXED / TP PARTIAL / SINGLE EXIT RULE
# ============================================================
# ✅ 합의 사항 반영 요약
# 1) 엔진 시간축 = 100% 실시간(timestamp)
# 2) 봉(bar) 개념은 엔진 판단에서 제거 (시각화/리서치 입력만)
# 3) ENTRY 직후 연쇄 EXIT 방지 → MIN_HOLD_SEC
# 4) EXIT 후 재진입 방지 → EXIT_COOLDOWN_SEC
# 5) 체결(N회) ≠ 엔진 이벤트(1회)
# 6) reduceOnly = 숏 청산(BUY)에만 사용
# 7) 숏 의미 고정: SELL=숏 진입 / BUY=숏 청산
# 8) TP_PARTIAL → 동일 EXIT 규칙 → FINAL EXIT
# 9) BASE/TRAIL 분리 금지 → pullback 단일 규칙
# ============================================================
# ============================================================

import os
import time
import requests
from decimal import Decimal, ROUND_DOWN

# ============================================================
# CFG
# ============================================================
CFG = {
    # --------------------------------------------------------
    # [01~09] BASIC
    # --------------------------------------------------------
    "01_TRADE_SYMBOL": "RONINUSDT",
    "02_CAPITAL_BASE_USDT": 30,
    "03_CAPITAL_USE_FIXED": True,
    "04_ENGINE_ENABLE": True,

    # --------------------------------------------------------
    # [10~19] EXIT (SHORT)
    # --------------------------------------------------------
    "10_SL_PCT": 0.60,          # 손절
    "11_TP1_PCT": 0.70,         # TP1 트리거
    "12_PULLBACK_PCT": 0.45,    # 단일 FINAL EXIT 되돌림 %
    "13_TP_PARTIAL_PCT": 0.5,   # TP1 부분익절 비율

    # --------------------------------------------------------
    # [20~29] CONTROL / LOCK
    # --------------------------------------------------------
    "20_EXIT_COOLDOWN_SEC": 300, # EXIT 후 재진입 금지
    "21_MIN_HOLD_SEC": 300,      # ENTRY 직후 즉시 EXIT 금지 (엔진 안정 락)

    # --------------------------------------------------------
    # [90~99] LOOP
    # --------------------------------------------------------
    "99_LOOP_SEC": 5,           # 엔진 루프 주기 (초)
}

# ============================================================
# UTIL
# ============================================================
def q(x, p=6):
    return float(
        Decimal(str(x)).quantize(
            Decimal("1." + "0" * p),
            rounding=ROUND_DOWN
        )
    )

def now_ts():
    return int(time.time())

# ============================================================
# BINANCE
# ============================================================
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET

def init_binance_client():
    k = os.getenv("BINANCE_API_KEY")
    s = os.getenv("BINANCE_API_SECRET")
    if not k or not s:
        raise RuntimeError("API KEY missing")
    return Client(k, s)

class FX:
    def __init__(self, client):
        self.client = client
        info = client.futures_exchange_info()
        sym = next(s for s in info["symbols"] if s["symbol"] == CFG["01_TRADE_SYMBOL"])
        lot = next(f for f in sym["filters"] if f["filterType"] == "LOT_SIZE")
        self.step = Decimal(lot["stepSize"])
        self.minq = Decimal(lot["minQty"])

    def _norm_qty(self, qty):
        qd = (Decimal(str(qty)) / self.step).to_integral_value(rounding=ROUND_DOWN) * self.step
        if qd < self.minq:
            return None
        d = len(str(self.step).split(".")[1].rstrip("0"))
        return f"{qd:.{d}f}"

    def order(self, side, qty, reduce_only=False):
        qs = self._norm_qty(qty)
        if qs is None:
            return 0.0
        self.client.futures_create_order(
            symbol=CFG["01_TRADE_SYMBOL"],
            side=SIDE_SELL if side == "SELL" else SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=qs,
            reduceOnly=reduce_only
        )
        return float(qs)

# ============================================================
# MARKET (시각화/리서치 입력 전용)
# ============================================================
BINANCE_SPOT = "https://api.binance.com/api/v3/klines"
INTERVAL = "5m"
EMA_PERIOD = 9

_rest = {"ema": []}

def poll_kline(symbol):
    kl = requests.get(
        BINANCE_SPOT,
        params={"symbol": symbol, "interval": INTERVAL, "limit": EMA_PERIOD + 5},
        timeout=5
    ).json()

    k = kl[-2]  # 완료봉 (참고용)
    bar_ts = int(k[6])
    close = float(k[4])
    low = float(k[3])

    if not _rest["ema"]:
        ema = close
    else:
        alpha = 2 / (EMA_PERIOD + 1)
        ema = close * alpha + _rest["ema"][-1] * (1 - alpha)

    _rest["ema"].append(ema)
    _rest["ema"] = _rest["ema"][-50:]

    return {
        "bar_ts": bar_ts,
        "close": close,
        "low": low,
        "ema": ema,
    }

# ============================================================
# STATE
# ============================================================
def init_state():
    return {
        "now_ts": None,

        # candidate
        "has_candidate": False,
        "candidate_ts": None,
        "candidate_ref_bar_ts": None,

        # position
        "position": None,            # "SHORT"
        "entry_ts": None,
        "entry_price": None,
        "position_qty": 0.0,
        "remain_qty": 0.0,

        # exit refs
        "sl_price": None,
        "tp_price": None,

        # tracking
        "tp_partial_done": False,
        "anchor_low": None,
        "pullback_price": None,

        # locks
        "last_exit_ts": None,
    }

# ============================================================
# RULES
# ============================================================
def in_exit_cooldown(state):
    if not state["last_exit_ts"]:
        return False
    return (state["now_ts"] - state["last_exit_ts"]) < CFG["20_EXIT_COOLDOWN_SEC"]

def can_enter(state):
    if state["position"] is not None:
        return False
    if in_exit_cooldown(state):
        return False
    return True

def can_exit(state):
    if not state["entry_ts"]:
        return False
    return (state["now_ts"] - state["entry_ts"]) >= CFG["21_MIN_HOLD_SEC"]

def update_anchor_and_pullback(state, mkt):
    if state["anchor_low"] is None:
        state["anchor_low"] = state["entry_price"]
    state["anchor_low"] = q(min(state["anchor_low"], mkt["low"]))
    state["pullback_price"] = q(
        state["anchor_low"] * (1 + CFG["12_PULLBACK_PCT"] / 100)
    )

# ============================================================
# ENGINE
# ============================================================
def run():
    if not CFG["04_ENGINE_ENABLE"]:
        print("ENGINE DISABLED")
        return

    client = init_binance_client()
    fx = FX(client)
    state = init_state()

    print("ENGINE START")

    while True:
        state["now_ts"] = now_ts()
        mkt = poll_kline(CFG["01_TRADE_SYMBOL"])
        price = mkt["close"]

        # ----------------------------
        # 1) CANDIDATE
        # ----------------------------
        if (not state["has_candidate"]) and (mkt["low"] < mkt["ema"]):
            state["has_candidate"] = True
            state["candidate_ts"] = state["now_ts"]
            state["candidate_ref_bar_ts"] = mkt["bar_ts"]
            print(f"[CANDIDATE] ts={state['candidate_ts']}")

        # ----------------------------
        # 2) ENTRY
        # ----------------------------
        if state["has_candidate"] and can_enter(state):
            qty = fx.order(
                "SELL",
                (CFG["02_CAPITAL_BASE_USDT"] * 0.95) / price,
                reduce_only=False
            )
            if qty > 0:
                state.update({
                    "position": "SHORT",
                    "entry_ts": state["now_ts"],
                    "entry_price": price,
                    "position_qty": qty,
                    "remain_qty": qty,
                    "sl_price": q(price * (1 + CFG["10_SL_PCT"] / 100)),
                    "tp_price": q(price * (1 - CFG["11_TP1_PCT"] / 100)),
                    "tp_partial_done": False,
                    "anchor_low": price,
                    "pullback_price": None,
                    "has_candidate": False,
                    "candidate_ts": None,
                    "candidate_ref_bar_ts": None,
                })
                print(f"[ENTRY] SELL price={q(price)} qty={qty}")

        # ----------------------------
        # 3) EXIT
        # ----------------------------
        if state["position"] == "SHORT":
            if not can_exit(state):
                time.sleep(CFG["99_LOOP_SEC"])
                continue

            update_anchor_and_pullback(state, mkt)

            # SL
            if price >= state["sl_price"]:
                fx.order("BUY", state["remain_qty"], reduce_only=True)
                print(f"[EXIT] SL price={q(price)}")
                state["last_exit_ts"] = state["now_ts"]
                state.clear(); state.update(init_state())

            # TP PARTIAL
            elif (not state["tp_partial_done"]) and (price <= state["tp_price"]):
                part = state["remain_qty"] * CFG["13_TP_PARTIAL_PCT"]
                closed = fx.order("BUY", part, reduce_only=True)
                state["remain_qty"] = q(state["remain_qty"] - closed, 10)
                state["tp_partial_done"] = True
                print(f"[TP_PARTIAL] qty={q(closed,8)} remain={q(state['remain_qty'],8)}")

            # FINAL EXIT (pullback)
            elif price >= state["pullback_price"]:
                fx.order("BUY", state["remain_qty"], reduce_only=True)
                print(
                    f"[EXIT] FINAL price={q(price)} "
                    f"anchor={state['anchor_low']} pullback={state['pullback_price']}"
                )
                state["last_exit_ts"] = state["now_ts"]
                state.clear(); state.update(init_state())

        time.sleep(CFG["99_LOOP_SEC"])

if __name__ == "__main__":
    run()
