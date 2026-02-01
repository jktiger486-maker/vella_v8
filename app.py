# ============================================================
# VELLA V8 — app_min.py (REALTIME ENGINE FIXED / TP PARTIAL)
# ------------------------------------------------------------
# ✅ 결정 반영 (이 대화창 합의 사항)
# 1) 엔진은 100% 실시간(timestamp) 기준
# 2) "봉(bar)" 개념은 엔진 판단에서 제거 (시각화/리서치용 데이터만)
# 3) ENTRY/EXIT 같은 봉/같은틱 연쇄 방지 → "MIN_HOLD_SEC"로 차단
# 4) EXIT 직후 재진입 방지 → EXIT_COOLDOWN_SEC
# 5) 체결(N회) vs 이벤트(1회) 분리 → 엔진 이벤트는 1회, 체결은 Binance가 쪼개도 OK
# 6) reduceOnly 역할 고정 → BUY(숏 청산)에서만 사용
# 7) 숏 의미 고정 → SELL=숏 진입 / BUY=숏 청산
# 8) TP_PARTIAL → (same EXIT rule) → FINAL EXIT 유지
# 9) BASE/TRAIL "조건 따로" 금지 → 되돌림(리바운드) 1조건으로 통일
# ============================================================

import os
import time
import requests
from decimal import Decimal, ROUND_DOWN

# ---------------- CFG ----------------
CFG = {
    # =====================================================
    # [01~04] 기본
    # =====================================================
    "01_TRADE_SYMBOL": "RONINUSDT",
    "02_CAPITAL_BASE_USDT": 30,
    "03_CAPITAL_USE_FIXED": True,
    "04_ENGINE_ENABLE": True,

    # =====================================================
    # [10~19] EXIT (SHORT 기준)
    # =====================================================
    "10_SL_PCT": 0.60,          # 손절 (price >= entry * (1 + SL))
    "11_TP1_PCT": 0.70,         # TP1 트리거 (price <= entry * (1 - TP1))
    "12_PULLBACK_PCT": 0.20,    # 단일 FINAL EXIT 되돌림 %
    "13_TP_PARTIAL_PCT": 0.5,   # TP1 부분익절 비율

    # =====================================================
    # [20~29] CONTROL / LOCK
    # =====================================================
    "20_EXIT_COOLDOWN_SEC": 60, # EXIT 후 재진입 금지(60~180)
    "21_MIN_HOLD_SEC": 10,      # ENTRY 직후 즉시 EXIT 금지

    # =====================================================
    # [90~99] LOOP
    # =====================================================
    "99_LOOP_SEC": 5,    # 5초마다 한 번씩 바이낸스에서 데이터를 가져와서 판단
}

# ---------------- UTIL ----------------
def q(x, p=6):
    # ✅ 반올림 금지 / 삭감(버림) 고정
    return float(Decimal(str(x)).quantize(Decimal("1." + "0"*p), rounding=ROUND_DOWN))

def now_ts():
    return int(time.time())

# ---------------- BINANCE ----------------
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
        """
        ✅ 숏 기준 의미 고정
        - SELL = 숏 진입
        - BUY  = 숏 청산(부분/전체)
        ✅ reduceOnly 역할 고정: 청산(BUY)에서만 True 사용
        """
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

# ---------------- MARKET (시각화/리서치 데이터 공급만) ----------------
BINANCE_SPOT = "https://api.binance.com/api/v3/klines"
INTERVAL = "5m"
EMA_PERIOD = 9

_rest = {"ema": []}

def poll_kline(symbol):
    # 완료봉(kl[-2])로 EMA 계산 → "후보 판정/시각화용 입력" (엔진 시간축은 now_ts)
    kl = requests.get(
        BINANCE_SPOT,
        params={"symbol": symbol, "interval": INTERVAL, "limit": EMA_PERIOD + 5},
        timeout=5
    ).json()

    k = kl[-2]  # 완료봉
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

# ---------------- STATE ----------------
def init_state():
    return {
        # time
        "now_ts": None,

        # candidate (이벤트)
        "has_candidate": False,
        "candidate_ts": None,      # ✅ 실시간 기준 후보 생성 시각
        "candidate_ref_bar_ts": None,  # 참고용 (시각화용) - 엔진 판단엔 사용 금지

        # position
        "position": None,          # "SHORT" or None
        "entry_ts": None,          # ✅ 실시간 엔트리 시각
        "entry_price": None,
        "position_qty": 0.0,
        "remain_qty": 0.0,

        # exit prices
        "sl_price": None,
        "tp_price": None,

        # tp/tracking
        "tp_partial_done": False,
        "anchor_low": None,        # ✅ 숏 기준: 유리했던 최저가 anchor (실시간 업데이트)
        "pullback_price": None,    # anchor_low에서 되돌림 트리거 가격

        # locks
        "last_exit_ts": None,      # ✅ EXIT cooldown 기준
    }

# ---------------- RULES ----------------
def in_exit_cooldown(state):
    if not state["last_exit_ts"]:
        return False
    return (state["now_ts"] - state["last_exit_ts"]) < CFG["90_EXIT_COOLDOWN_SEC"]

def can_enter(state):
    if state["position"] is not None:
        return False
    if in_exit_cooldown(state):
        return False
    return True

def can_exit(state):
    # ✅ ENTRY 직후 즉시 EXIT(연쇄) 금지
    if not state["entry_ts"]:
        return False
    return (state["now_ts"] - state["entry_ts"]) >= CFG["91_MIN_HOLD_SEC"]

def update_anchor_and_pullback(state, mkt):
    """
    ✅ BASE/TRAIL 조건 분리 금지
    - "유리했던 순간(anchor_low)"에서
    - "의미있게 되돌림(PULLBACK_PCT)" 나오면
    - 무조건 FINAL EXIT
    """
    if state["anchor_low"] is None:
        state["anchor_low"] = state["entry_price"]

    # 숏 유리 anchor = 더 낮은 low가 나오면 갱신
    state["anchor_low"] = q(min(state["anchor_low"], mkt["low"]))
    state["pullback_price"] = q(state["anchor_low"] * (1 + CFG["37_PULLBACK_PCT"] / 100))

# ---------------- ENGINE ----------------
def run():
    if not CFG["05_ENGINE_ENABLE"]:
        print("ENGINE DISABLED"); return

    client = init_binance_client()
    fx = FX(client)
    state = init_state()

    print("ENGINE START")

    while True:
        state["now_ts"] = now_ts()
        mkt = poll_kline(CFG["01_TRADE_SYMBOL"])
        price = mkt["close"]

        # =====================================================
        # 1) CANDIDATE (이벤트 1회)
        # -----------------------------------------------------
        # "조건 충족 + 후보 없음" → 후보 생성 (candidate_ts = now_ts)
        # =====================================================
        if (not state["has_candidate"]) and (mkt["low"] < mkt["ema"]):
            state["has_candidate"] = True
            state["candidate_ts"] = state["now_ts"]
            state["candidate_ref_bar_ts"] = mkt["bar_ts"]  # 참고용
            print(f"[CANDIDATE] ts={state['candidate_ts']} ref_bar={state['candidate_ref_bar_ts']}")

        # =====================================================
        # 2) ENTRY (이벤트 1회)
        # -----------------------------------------------------
        # 후보 존재 + can_enter → SELL(숏 진입)
        # =====================================================
        if state["has_candidate"] and can_enter(state):
            entry_price = price
            qty = fx.order("SELL", (CFG["02_CAPITAL_BASE_USDT"] * 0.95) / entry_price, reduce_only=False)
            if qty > 0:
                state.update({
                    "position": "SHORT",
                    "entry_ts": state["now_ts"],
                    "entry_price": entry_price,
                    "position_qty": qty,
                    "remain_qty": qty,

                    "sl_price": q(entry_price * (1 + CFG["35_SL_PCT"] / 100)),
                    "tp_price": q(entry_price * (1 - CFG["36_TP_PCT"] / 100)),

                    "tp_partial_done": False,
                    "anchor_low": entry_price,   # 시작 anchor
                    "pullback_price": None,

                    "has_candidate": False,
                    "candidate_ts": None,
                    "candidate_ref_bar_ts": None,
                })
                print(f"[ENTRY] SELL price={q(entry_price)} qty={qty} sl={state['sl_price']} tp={state['tp_price']}")

        # =====================================================
        # 3) EXIT (SL / TP_PARTIAL / FINAL EXIT)
        # -----------------------------------------------------
        # 우선순위(고정):
        # 1) SL (즉시 전량 청산)
        # 2) TP_PARTIAL (1회만 부분 청산, reduceOnly=True)
        # 3) FINAL EXIT (되돌림 단일 조건, reduceOnly=True)
        # =====================================================
        if state["position"] == "SHORT":
            # 연쇄 EXIT 락
            if not can_exit(state):
                time.sleep(CFG["99_LOOP_SEC"])
                continue

            # anchor/pullback 업데이트는 "항상"
            update_anchor_and_pullback(state, mkt)

            # 1) SL
            if price >= state["sl_price"]:
                fx.order("BUY", state["remain_qty"], reduce_only=True)
                print(f"[EXIT] SL price={q(price)} remain={q(state['remain_qty'], 8)}")
                state["last_exit_ts"] = state["now_ts"]
                state.clear(); state.update(init_state())

            # 2) TP_PARTIAL (1회)
            elif (not state["tp_partial_done"]) and (price <= state["tp_price"]):
                part_qty = state["remain_qty"] * CFG["38_TP_PARTIAL_PCT"]
                closed = fx.order("BUY", part_qty, reduce_only=True)  # ✅ reduceOnly 고정
                state["remain_qty"] = q(state["remain_qty"] - closed, 10)
                state["tp_partial_done"] = True
                print(f"[TP_PARTIAL] BUY qty={q(closed, 8)} remain={q(state['remain_qty'], 8)} price={q(price)}")

            # 3) FINAL EXIT (되돌림 단일 조건)
            # anchor_low에서 PULLBACK_PCT 만큼 되돌아오면 종료
            elif price >= state["pullback_price"]:
                fx.order("BUY", state["remain_qty"], reduce_only=True)
                print(
                    f"[EXIT] FINAL(pullback) price={q(price)} "
                    f"anchor={state['anchor_low']} pullback={state['pullback_price']} "
                    f"remain={q(state['remain_qty'], 8)}"
                )
                state["last_exit_ts"] = state["now_ts"]
                state.clear(); state.update(init_state())

        time.sleep(CFG["99_LOOP_SEC"])

if __name__ == "__main__":
    run()
