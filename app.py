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
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET

# ============================================================
# CFG
# ============================================================
CFG = {
    # --------------------------------------------------------
    # [01~04] BASIC
    # --------------------------------------------------------
    "01_TRADE_SYMBOL": "RONINUSDT",
    "02_CAPITAL_BASE_USDT": 30,
    "03_CAPITAL_USE_FIXED": True,
    "04_ENGINE_ENABLE": True,

    # --------------------------------------------------------
    # [05~14] ENTRY / GATE (EXPANDABLE)
    # --------------------------------------------------------
    "05_EXECUTION_MIN_PRICE_MOVE_PCT": 0.08,  # 후보 대비 최소 하락 %
    # ▶ $$$ 엔진 돌리자 말자 포지션 잡는거 막음 $$$
    # ▶ 후보발생 후, 기준가격 대비 최소 % 이상 하락때만 ENTRY(실행) 허용
    #    (SHORT 기준: 하락 확인 게이트 / 노이즈 차단)
    # ▶ 추천값(시작): 0.20
    # ▶ 추천범위:
    #    - 0.10 ~ 0.15 : 약하게 닫기 (진입 조금만 줄고, 노이즈 일부만 제거)
    #    - 0.20 ~ 0.30 : 표준(권고) (의미 있는 하락만 남김, 데이터 해석 가장 깔끔)
    #    - 0.40 ~ 0.60 : 강하게 닫기 (진입 크게 감소, 추세 구간만 남음)
    # ▶ 이유: 실제 하방 움직임이 '숫자로 증명'된 뒤에만 들어가게" 만들어
    #         이후 성과 변화가 이 게이트 효과로만 해석되게 함.

    "06_EMA_BELOW_HOLD_SEC": 30,   
    # ★ 추가: EMA 아래 '연속 유지' ts 게이트(봉 없음)
    # - price < EMA 상태가 00초 연속 유지되어야 ENTRY 허용
    # - EMA 터치 즉시 진입(휩쏘) 차단 목적


    "07_CANDIDATE_MIN_SEC": 30,   # 후보(ref 갱신 이후 최소 대기 시간)
    # - 후보는 건드리지 않고, ENTRY 전에 최소 대기만 강제
    # - 반등에 자주 털린다 → 120 → 180
    # - 기회 놓친다 → 120 → 90

    # --------------------------------------------------------
    # [10~19] EXIT (SHORT)
    # --------------------------------------------------------
    "15_SL_PCT": 0.60,          # 손절
    "16_TP1_PCT": 0.70,         # TP1 트리거
    "17_TP_PARTIAL_PCT": 0.50,  # TP1 부분익절 비율
    "18_PULLBACK_PCT": 0.45,    # 단일 FINAL EXIT 되돌림 %

    # --------------------------------------------------------
    # [20~29] CONTROL / LOCK
    # --------------------------------------------------------
    "20_EXIT_COOLDOWN_SEC": 180,
    "21_MIN_HOLD_SEC": 300,


    # --------------------------------------------------------
    # [90~99] LOOP
    # --------------------------------------------------------
    "99_LOOP_SEC": 5,
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
def init_binance_client():
    k = os.getenv("BINANCE_API_KEY")
    s = os.getenv("BINANCE_API_SECRET")
    if not k or not s:
        raise RuntimeError("API KEY missing")
    return Client(k, s)

def get_realtime_price(client):
    t = client.futures_symbol_ticker(symbol=CFG["01_TRADE_SYMBOL"])
    return float(t["price"])

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
# STATE
# ============================================================
def init_state():
    return {
        "now_ts": None,

        # candidate
        "has_candidate": False,
        "candidate_ts": None,
        "candidate_ref_ts": None,      # ★ ref(최고가) 마지막 갱신 시점
        "candidate_ref_price": None,

        # 🔽 추가
        "ema_below_since": None,   # EMA 아래로 내려간 최초 ts    

        # position
        "position": None,
        "entry_ts": None,
        "entry_price": None,
        "position_qty": 0.0,
        "remain_qty": 0.0,

        # exit refs
        "sl_price": None,
        "tp_price": None,
        "anchor_low": None,
        "pullback_price": None,
        "tp_partial_done": False,

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

# ============================================================
# STEP 05 — EXECUTION MIN PRICE MOVE GATE
# ============================================================
def pass_min_price_move_gate(state, price):
    ref = state["candidate_ref_price"]
    if ref is None:
        return False
    move_pct = (ref - price) / ref * 100
    return move_pct >= CFG["05_EXECUTION_MIN_PRICE_MOVE_PCT"]


# ============================================================
# STEP 06 — CANDIDATE AGE GATE (시간 게이트)
# ============================================================
def pass_candidate_age_gate(state):
    # 후보 생성(or ref 갱신) 후 최소 시간 경과 요구
    ref_ts = state.get("candidate_ref_ts")
    if ref_ts is None:
        return False
    return (state["now_ts"] - ref_ts) >= CFG["07_CANDIDATE_MIN_SEC"]



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

    # ★ EMA 상태 (엔진 내부, 단일 ts)
    ema = None
    EMA_PERIOD = 9



    print("ENGINE START")

    while True:
        state["now_ts"] = now_ts()
        price = get_realtime_price(client)

        # ----------------------------
        # EMA UPDATE (REALTIME, TS SINGLE)
        # ----------------------------
        if ema is None:
            ema = price
        else:
            alpha = 2 / (EMA_PERIOD + 1)
            ema = ema + alpha * (price - ema)

        # ----------------------------
        # 1) CANDIDATE (단순 이벤트 + ref 고점 추적)
        # ----------------------------
        if not state["has_candidate"]:
            state["has_candidate"] = True
            state["candidate_ts"] = state["now_ts"]
            state["candidate_ref_price"] = price
            state["candidate_ref_ts"] = state["now_ts"]   # ★ 최초 ref 시점
            print(f"[CANDIDATE] ref_price={q(price)}")
        else:
            # 숏 기준: 후보가 살아있는 동안 최고가를 ref로 유지
            if price > state["candidate_ref_price"]:
                state["candidate_ref_price"] = price
                state["candidate_ref_ts"] = state["now_ts"]  # ★ ref 갱신 시점


        # ----------------------------
        # 2) ENTRY (GATE STACK)
        # ----------------------------
        if state["has_candidate"] and can_enter(state):

            # ① 가격 이동 게이트
            if not pass_min_price_move_gate(state, price):
                time.sleep(CFG["99_LOOP_SEC"])
                continue

            # ② 시간 게이트 (candidate age)
            if not pass_candidate_age_gate(state):
                time.sleep(CFG["99_LOOP_SEC"])
                continue

            # ③ EMA BELOW HOLD GATE
            if price < ema:
                if state["ema_below_since"] is None:
                    state["ema_below_since"] = state["now_ts"]
            else:
                # EMA 위로 올라오면 리셋
                state["ema_below_since"] = None
            if (
                state["ema_below_since"] is None or
                (state["now_ts"] - state["ema_below_since"]) < CFG["06_EMA_BELOW_HOLD_SEC"]
            ):
                time.sleep(CFG["99_LOOP_SEC"])
                continue



            # ▶▶ 여기부터가 "진짜 ENTRY" ◀◀


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
                    "sl_price": q(price * (1 + CFG["15_SL_PCT"] / 100)),
                    "tp_price": q(price * (1 - CFG["16_TP1_PCT"] / 100)),
                    "anchor_low": price,
                    "pullback_price": None,
                    "tp_partial_done": False,
                    "has_candidate": False,
                    "candidate_ts": None,
                    "candidate_ref_price": None,
                })
                print(f"[ENTRY] SELL price={q(price)} qty={qty}")

        # ----------------------------
        # 3) EXIT
        # ----------------------------
        if state["position"] == "SHORT":
            if not can_exit(state):
                time.sleep(CFG["99_LOOP_SEC"])
                continue

            state["anchor_low"] = min(state["anchor_low"], price)
            state["pullback_price"] = q(
                state["anchor_low"] * (1 + CFG["18_PULLBACK_PCT"] / 100)
            )

            if price >= state["sl_price"]:
                fx.order("BUY", state["remain_qty"], reduce_only=True)
                print("[EXIT] SL")
                state["last_exit_ts"] = state["now_ts"]
                state.clear(); state.update(init_state())

            elif (not state["tp_partial_done"]) and price <= state["tp_price"]:
                part = state["remain_qty"] * CFG["17_TP_PARTIAL_PCT"]
                closed = fx.order("BUY", part, reduce_only=True)
                state["remain_qty"] = q(state["remain_qty"] - closed, 10)
                state["tp_partial_done"] = True
                print(f"[TP_PARTIAL] qty={q(closed,8)}")

            elif price >= state["pullback_price"]:
                fx.order("BUY", state["remain_qty"], reduce_only=True)
                print("[EXIT] FINAL")
                state["last_exit_ts"] = state["now_ts"]
                state.clear(); state.update(init_state())

        time.sleep(CFG["99_LOOP_SEC"])

if __name__ == "__main__":
    run()    