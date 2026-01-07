# ============================================================
# VELLA V8 — app.py (AWS READY / ERROR 0)
# STEP 1 ~ STEP 16 (ALL PRESENT, IN ORDER)
# ENGINE INDEPENDENT / LIVE CONTRACT
# ------------------------------------------------------------
# 설계-구현 100% 일치 원칙:
# - CFG(01~40)에 있는 항목은 모두 "집행 로직"이 존재해야 한다.
# - OFF 옵션도 '집행 경로가 존재'해야 한다 (단, OFF면 통과).
# - 후보(candidate)는 이벤트 기록이며, gate/entry/exit과 분리된다.
# ============================================================


import os
import time
import threading
from decimal import Decimal, ROUND_DOWN

import pandas as pd



# ============================================================
# CFG (01 ~ 40 FULL) — VELLA V8 BASELINE (FREEZE)
# ============================================================

CFG = {
    # =====================================================
    # [ STEP 1 ] 거래 대상 · 자본 · 손실 한계
    # =====================================================
    "01_TRADE_SYMBOL": "POLYXUSDT",
    "02_CAPITAL_BASE_USDT": 60,
    "03_CAPITAL_USE_FIXED": True,
    "04_CAPITAL_MAX_LOSS_PCT": 100.0,

    # =====================================================
    # [ STEP 2 ] 엔진 / 실행 스위치
    # =====================================================
    "05_ENGINE_ENABLE": True,          # 🔒 엔진 보호: False면 엔진 즉시 정지
    "06_ENTRY_CANDIDATE_ENABLE": True,
    "07_ENTRY_EXEC_ENABLE": True,      # 🔒 엔진 보호: 실주문 ON/OFF 단일 스위치

    # =====================================================
    # [ STEP 3 ] 후보 생성
    # =====================================================
    "08_CAND_BODY_BELOW_EMA": True,

    # =====================================================
    # [ STEP 4 ] BTC SESSION BIAS
    # =====================================================
    "09_BTC_SESSION_BIAS_ENABLE": False,

    # =====================================================
    # [ STEP 5 ] EMA SLOPE
    # =====================================================
    "10_EMA_SLOPE_MIN_PCT": -100.0,
    "11_EMA_SLOPE_LOOKBACK_BARS": 0,

    # =====================================================
    # [ STEP 6 ] PRICE CONFIRM
    # =====================================================
    "12_EXECUTION_MIN_PRICE_MOVE_PCT": 0.0,
    "13_EXECUTION_ONLY_ON_NEW_LOW": False,

    # =====================================================
    # [ STEP 6-A ] EMA PROXIMITY
    # =====================================================
    "38_EMA_TOL_PCT": 5.00,
    "39_EMA_EPS_PCT": 1.00,

    # =====================================================
    # [ STEP 7 ] 실행 속도 제어
    # =====================================================
    "14_STATE_COOLDOWN_ENABLE": False,
    "15_COOLDOWN_RANGE_BARS": 0,
    "16_COOLDOWN_TREND_BARS": 0,

    # =====================================================
    # [ STEP 8 ] 실행 안전장치
    # =====================================================
    "17_ENTRY_MAX_PER_CYCLE": 100,
    "18_MAX_ENTRIES_PER_DAY": 100,
    "19_DATA_STALE_BLOCK": False,
    "20_EXECUTION_SPREAD_GUARD_ENABLE": False,
    "40_EXECUTION_SPREAD_MAX_PCT": 0.50,

    # =====================================================
    # [ STEP 9 ] 재진입 관리
    # =====================================================
    "21_ENTRY_COOLDOWN_BARS": 0,
    "22_ENTRY_COOLDOWN_AFTER_EXIT": 0,
    "23_REENTRY_SAME_REASON_BLOCK": False,
    "24_ENTRY_LOOKBACK_BARS": 100,
    "25_REENTRY_PRICE_TOL_PCT": 100,
    "26_CAND_POOL_TTL_BARS": 1000,
    "27_CAND_POOL_MAX_SIZE": 1000,
    "28_CAND_MIN_GAP_BARS": 0,

    # =====================================================
    # [ STEP 10 ] 변동성 보호
    # =====================================================
    "29_VOLATILITY_BLOCK_ENABLE": False,
    "30_VOLATILITY_MAX_PCT": 20,

    # =====================================================
    # [ STEP 11 ] 로그
    # =====================================================
    "31_LOG_CANDIDATES": True,
    "32_LOG_EXECUTIONS": True,

    # =====================================================
    # [ STEP 12 ] FAIL-SAFE (엔진 보호 핵심)
    # =====================================================
    "33_ENGINE_FAIL_FAST_ENABLE": True,   # 🔒 엔진 보호: 손실 초과 시 엔진 중단
    "34_ENGINE_FAIL_NOTIFY_ONLY": True,   # 🔒 엔진 보호: 중단 대신 알림만

    # =====================================================
    # [ STEP 14 ] EXIT CORE PARAMS
    # =====================================================
    "35_SL_PCT": 0.60,
    "36_TP_PCT": 0.80,
    "37_TRAILING_PCT": 0.40,

    # =====================================================
    # [ STEP 41 ] ENGINE LOOP IDLE CONTROL (OPERATION ONLY)
    # - BAR / ENTRY / EXIT 시간축과 무관
    # - 매매 확인 단계 기본값 = 0.0 (대기 없음)
    # =====================================================
    "41_LOOP_IDLE_SLEEP_SEC": 0.0,

}



# ============================================================
# CFG FREEZE DECLARATION
# ------------------------------------------------------------
# 기준선 고정:
# - 본 파일에서 허용되는 변경은 CFG 값(숫자/불리언)만.
# - STEP/로직/순서 변경 금지.
# ============================================================


# ============================================================
# STATE CONTRACT (설계 문장 고정 / 엔진독립)
# ------------------------------------------------------------
# 1) state는 계약이다. 암묵 키 생성 금지.
# 2) ENTRY와 EXIT는 같은 bar에 존재할 수 없다.
# 3) position은 "주문정보"가 아니라 "시간축 상태"다.
#    - None : 포지션 없음
#    - "OPEN": 포지션 존재(Record Only 단계)
# ============================================================

def init_state():
    return {
        "ticks": 0,
        "bars": 0,


        # ✅ LIVE BAR TRACKING (STATE CONTRACT: explicit key)
        "_last_bar_time": None,

        # candidate
        "has_candidate": False,
        "candidates": [],
        "last_candidate_bar": None,

        # gate (READY)
        "gate_ok": False,
        "gate_reason": None,

        # entry (LIVE CONTRACT)
        "entry_ready": False,
        "entry_bar": None,
        "entry_reason": None,

        # position (TIME AXIS STATE)
        "position": None,
        "position_open_bar": None,

        # ---- LIVE / EXIT CONTRACT (EXPLICIT) ----
        "capital_usdt": None,          # STEP1 sets
        "initial_equity": None,        # STEP1 sets
        "equity": None,                # updated on simulated exit
        "realized_pnl": 0.0,           # 누적

        "entry_price": None,
        "sl_price": None,
        "tp_price": None,

        "tp_touched": False,
        "trailing_active": False,

        "trailing_anchor": None,
        "trailing_stop": None,
        "exit_ready": False,
        "exit_reason": None,
        "order_inflight": False,

        # ---- EXIT CONFIRM (3-BAR / CLOSE BASIS) ----
        "exit_signal": None,         # None / "SL" / "TP" / "TRAIL"
        "exit_confirm_count": 0,     # 같은 signal 연속 충족 횟수 (>=3 이면 EXIT)

        # ---- EXIT FIRE LOCK (STATE CONTRACT) ----
        "exit_fired_bar": None,
        "exit_fired_signal": None,

        # ---- LIMITS / TIME AXIS ----
        "cycle_id": 0,
        "entries_in_cycle": 0,
        "entries_today": 0,
        "day_key": None,
        "last_entry_bar": None,
        "last_exit_bar": None,
        "last_entry_reason": None,
        "last_entry_price": None,

        # EMA series cache (LIVE data only)
        "_ema9_series_live": [],

        # records
        "execution_records": [],      # STEP 13
        "exit_records": [],           # STEP 15 confirm
        "sl_tp_trailing_records": [], # STEP 14 calc snapshots
    }


# ============================================================
# Numeric helpers
# ============================================================

def q(x, p=6):
    return float(Decimal(str(x)).quantize(Decimal("1." + "0"*p), rounding=ROUND_DOWN))

def _safe_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None

def _ms_to_daykey_utc(ms):
    # UTC day key: ms since epoch -> days since epoch
    try:
        return int(int(ms) // 86400000)
    except Exception:
        return None


# ============================================================
# [ STEP 41 ] ENGINE LOOP IDLE CONTROL
# - 운영 안정용 STEP
# - 매매 판단 ❌
# - state 변경 ❌
# - bar / entry / exit 시간축 ❌
# ============================================================

def step_41_loop_idle(cfg):
    """
    STEP 41은 '엔진 루프 안정화'를 위한 운영 STEP이다.
    CFG에 정의된 idle sleep만 집행하며,
    판단·상태·시장 데이터에는 일절 관여하지 않는다.
    """
    sec = cfg.get("41_LOOP_IDLE_SLEEP_SEC")
    try:
        sec = float(sec)
    except Exception:
        sec = 0.0

    if sec > 0:
        time.sleep(sec)



# ============================================================
# DATA LOADER (REPLAY)
# ============================================================

def load_sui_binance_ema9_csv(path):
    df = pd.read_csv(path)
    df.rename(columns={c: c.strip() for c in df.columns}, inplace=True)

    rows = []
    for _, r in df.iterrows():
        ema = None
        if "ema9" in df.columns:
            ema = r.get("ema9")
        elif "EMA9" in df.columns:
            ema = r.get("EMA9")

        rows.append({
            "time": r.get("time"),
            "open": r.get("open"),
            "high": r.get("high"),
            "low": r.get("low"),
            "close": r.get("close"),
            "ema9": ema,
        })
    return rows

def build_market_ctx(row):
    return {
        "time": row.get("time"),
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "close": row.get("close"),
        "ema9": row.get("ema9"),
    }


# ============================================================
# [ STEP 1 ] ENGINE LIMIT (READY)
# - CAPITAL_USE_FIXED 집행
# - CAPITAL_MAX_LOSS_PCT 집행(STATE 초기화)
# ============================================================

def step_1_engine_limit(cfg, state, capital_ctx=None, logger=print):
    required = ["01_TRADE_SYMBOL", "02_CAPITAL_BASE_USDT", "03_CAPITAL_USE_FIXED", "04_CAPITAL_MAX_LOSS_PCT"]
    for k in required:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP1: {k}")

    if not isinstance(cfg["01_TRADE_SYMBOL"], str) or not cfg["01_TRADE_SYMBOL"]:
        raise RuntimeError("STEP1_INVALID_TRADE_SYMBOL")

    base = cfg["02_CAPITAL_BASE_USDT"]
    if not isinstance(base, (int, float)) or float(base) <= 0:
        raise RuntimeError("STEP1_INVALID_CAPITAL_BASE_USDT")

    if not isinstance(cfg["03_CAPITAL_USE_FIXED"], bool):
        raise RuntimeError("STEP1_INVALID_BOOL: 03_CAPITAL_USE_FIXED")

    max_loss_pct = cfg["04_CAPITAL_MAX_LOSS_PCT"]
    if not isinstance(max_loss_pct, (int, float)) or float(max_loss_pct) < 0:
        raise RuntimeError("STEP1_INVALID_CAPITAL_MAX_LOSS_PCT")

    # CAPITAL USE (fixed / dynamic)
    capital_usdt = float(base)
    if not cfg["03_CAPITAL_USE_FIXED"]:
        # dynamic capital (live only): capital_ctx["available_usdt"] if provided
        if capital_ctx and isinstance(capital_ctx.get("available_usdt"), (int, float)):
            capital_usdt = max(0.0, float(capital_ctx["available_usdt"]))

    state["capital_usdt"] = capital_usdt

    # 최초 1회: equity 초기화
    if state.get("initial_equity") is None:
        state["initial_equity"] = capital_usdt
        state["equity"] = capital_usdt
        state["realized_pnl"] = 0.0

    logger("STEP1_PASS")
    return True


# ============================================================
# [ STEP 2 ] ENGINE SWITCH (READY)
# ============================================================

def step_2_engine_switch(cfg, logger=print):
    for k in ["05_ENGINE_ENABLE", "06_ENTRY_CANDIDATE_ENABLE", "07_ENTRY_EXEC_ENABLE"]:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP2: {k}")
        if not isinstance(cfg[k], bool):
            raise RuntimeError(f"STEP2_INVALID_BOOL: {k}")

    if not cfg["05_ENGINE_ENABLE"]:
        logger("STEP2_DENY: ENGINE_ENABLE=False")
        return False

    logger("STEP2_PASS")
    return True


# ============================================================
# [ STEP 3 ] CANDIDATE GENERATOR (SINGLE SOURCE)
# 계약:
# - [04~18] 체크리스트 준수
# - 한 봉에서 중복 생성 금지
# - BTC/gate/position 무관 (오직 이벤트 기록)
# - 수량/SLTPTRAIL/entry_ready 설정 금지
# - 후보 TTL/POOL 관리는 STEP 9에서만
# ============================================================

def step_3_generate_candidates(cfg, market, state, logger=print):
    if not cfg.get("06_ENTRY_CANDIDATE_ENABLE", True):
        return

    if "08_CAND_BODY_BELOW_EMA" not in cfg:
        raise RuntimeError("CFG_MISSING_KEY_STEP3: 08_CAND_BODY_BELOW_EMA")

    if not cfg["08_CAND_BODY_BELOW_EMA"]:
        return

    if market is None:
        return

    low = _safe_float(market.get("low"))
    ema9 = _safe_float(market.get("ema9"))
    t = market.get("time")

    if low is None or ema9 is None:
        return

    # ✅ 한 봉(bar) 중복 생성 금지 — bar는 WS close 기준
    if state.get("last_candidate_bar") == state.get("bars"):
        return

    # ✅ MIN GAP (bars) 집행: 28_CAND_MIN_GAP_BARS
    gap = int(cfg.get("28_CAND_MIN_GAP_BARS", 0) or 0)
    last_bar = state.get("last_candidate_bar")
    if last_bar is not None and gap > 0:
        if (state.get("bars", 0) - int(last_bar)) < gap:
            return

    # ✅ 침범(low < ema9) 즉시 후보 생성
    if low < ema9:
        state["has_candidate"] = True
        state["last_candidate_bar"] = state.get("bars")
        cand = {
            "bar": state.get("bars"),
            "time": t,
            "trigger_price": low,
            "ema9": ema9,
            "reason": "EMA9_PENETRATION",
        }
        state["candidates"].append(cand)
        if cfg.get("31_LOG_CANDIDATES", True):
            logger(f"STEP3_NEW_CANDIDATE: bar={state['bars']} t={t} low={low} ema9={ema9}")



# ============================================================
# [ STEP 4 ] BTC SESSION BIAS (ENTRY GATE ONLY) — LOG ENHANCED
# ------------------------------------------------------------
# ⚠️ 로직 동일 / 판단 동일
# ✅ 차단 사유를 AWS 로그에서 즉시 식별 가능
# ============================================================

def step_4_btc_session_bias(cfg, btc_ctx, state, logger=print):
    if "09_BTC_SESSION_BIAS_ENABLE" not in cfg:
        raise RuntimeError("CFG_MISSING_KEY_STEP4: 09_BTC_SESSION_BIAS_ENABLE")

    if not cfg["09_BTC_SESSION_BIAS_ENABLE"]:
        # gate_ok를 여기서 True로 강제하지 않는다 (다른 gate가 쓸 수 있음)
        return True

    if btc_ctx is None or btc_ctx.get("daily_open") is None or btc_ctx.get("price") is None:
        state["gate_ok"] = False
        state["gate_reason"] = "BTC_CTX_MISSING"
        logger("STEP4_BLOCK: BTC_CTX_MISSING")
        return False

    daily_open = _safe_float(btc_ctx.get("daily_open"))
    price = _safe_float(btc_ctx.get("price"))
    if daily_open is None or price is None:
        state["gate_ok"] = False
        state["gate_reason"] = "BTC_CTX_INVALID"
        logger("STEP4_BLOCK: BTC_CTX_INVALID")
        return False

    if price < daily_open:
        # OK (통과만)
        return True

    state["gate_ok"] = False
    state["gate_reason"] = f"BTC_BIAS_BLOCK (price={q(price,4)} >= open={q(daily_open,4)})"
    logger(f"STEP4_BLOCK: {state['gate_reason']}")
    return False



# ============================================================
# [ STEP 5 ] EMA SLOPE GATE (GATE PRODUCER / TRUE PULSE)
# ------------------------------ LOG ENHANCED -----------------
# ⚠️ 로직 동일 / 판정식 동일
# ✅ gate 생산 결과를 AWS 로그에서 즉시 식별
# ============================================================

def step_5_ema_slope_gate(cfg, ema_ctx, state, logger=print):
    for k in ["10_EMA_SLOPE_MIN_PCT", "11_EMA_SLOPE_LOOKBACK_BARS"]:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP5: {k}")

    min_pct = float(cfg["10_EMA_SLOPE_MIN_PCT"])
    lb = int(cfg["11_EMA_SLOPE_LOOKBACK_BARS"] or 0)

    # ========================================================
    # EMA SLOPE GATE OFF (CFG 기준)
    # ========================================================
    if min_pct == 0 and lb == 0:
        state["gate_ok"] = True
        state["gate_reason"] = "EMA_SLOPE_GATE_OFF"
        logger("STEP5_PASS: EMA_SLOPE_GATE_OFF")
        return True

    # ========================================================
    # EMA CONTEXT 검증 (ON 상태)
    # ========================================================
    if ema_ctx is None or not ema_ctx.get("ema9_series"):
        state["gate_ok"] = False
        state["gate_reason"] = "EMA_CTX_MISSING"
        logger("STEP5_BLOCK: EMA_CTX_MISSING")
        return False

    series = ema_ctx["ema9_series"]
    if len(series) <= lb:
        state["gate_ok"] = False
        state["gate_reason"] = "EMA_SERIES_TOO_SHORT"
        logger(f"STEP5_BLOCK: EMA_SERIES_TOO_SHORT len={len(series)} lb={lb}")
        return False

    ema_now = _safe_float(series[-1])
    ema_prev = _safe_float(series[-1 - lb])
    if ema_now is None or ema_prev is None or ema_prev == 0:
        state["gate_ok"] = False
        state["gate_reason"] = "EMA_INVALID"
        logger("STEP5_BLOCK: EMA_INVALID")
        return False

    slope_pct = (ema_now - ema_prev) / ema_prev * 100.0

    # ========================================================
    # SHORT 기준 slope 판정
    # ========================================================
    if min_pct <= 0:
        ok = (slope_pct <= 0)
    else:
        ok = (slope_pct <= -abs(min_pct))

    state["gate_ok"] = bool(ok)
    state["gate_reason"] = f"EMA_SLOPE_OK={ok} slope_pct={q(slope_pct,4)}"

    if ok:
        logger(f"STEP5_PASS: slope_pct={q(slope_pct,4)}")
    else:
        logger(f"STEP5_BLOCK: slope_pct={q(slope_pct,4)} min_pct={min_pct}")

    return bool(ok)



# ============================================================
# [ STEP 6 ] ENTRY JUDGEMENT — LOG ENHANCED (NO LOGIC CHANGE)
# ------------------------------------------------------------
# ⚠️ 기준선 로직/출력/시간선 동일
# ✅ ENTRY 거부 사유를 AWS 로그에서 즉시 식별
# ============================================================

def step_6_entry_judge(cfg, market, state, logger=print):

    # 🔒 EXIT 우선
    if state.get("exit_ready") or state.get("exit_confirm_count", 0) > 0:
        state["entry_ready"] = False
        state["entry_bar"] = None
        state["entry_reason"] = "EXIT_IN_PROGRESS"
        logger("STEP6_DENY: EXIT_IN_PROGRESS")
        return False

    # ---- GATE ----
    gate_ok = bool(state.get("gate_ok", False))
    if not gate_ok:
        state["entry_ready"] = False
        state["entry_bar"] = None
        state["entry_reason"] = state.get("gate_reason") or "GATE_BLOCK"
        logger(f"STEP6_DENY: GATE_BLOCK reason={state['entry_reason']}")
        return False

    # ---- CANDIDATE (존재만 확인) ----
    candidates = state.get("candidates", []) or []
    if not (state.get("has_candidate") or len(candidates) > 0):
        state["entry_ready"] = False
        state["entry_bar"] = None
        state["entry_reason"] = "NO_CANDIDATE"
        logger("STEP6_DENY: NO_CANDIDATE")
        return False

    # ---- MARKET ----
    if market is None:
        state["entry_ready"] = False
        state["entry_bar"] = None
        state["entry_reason"] = "MARKET_MISSING"
        logger("STEP6_DENY: MARKET_MISSING")
        return False

    close = _safe_float(market.get("close"))
    ema9  = _safe_float(market.get("ema9"))
    if close is None or ema9 is None:
        state["entry_ready"] = False
        state["entry_bar"] = None
        state["entry_reason"] = "PRICE_OR_EMA_MISSING"
        logger("STEP6_DENY: PRICE_OR_EMA_MISSING")
        return False

    # ========================================================
    # EMA 근접 허용 (CFG 38/39)
    # ========================================================
    if "38_EMA_TOL_PCT" not in cfg or "39_EMA_EPS_PCT" not in cfg:
        raise RuntimeError("CFG_MISSING_KEY_STEP6_EMA_PROXIMITY")

    tol = ema9 * (float(cfg["38_EMA_TOL_PCT"]) / 100.0)
    eps = ema9 * float(cfg["39_EMA_EPS_PCT"])
    band = tol + eps

    if abs(q(close,6) - q(ema9,6)) > q(band,6):
        state["entry_ready"] = False
        state["entry_bar"] = None
        state["entry_reason"] = "EMA_DISTANCE_EXCEEDED"
        logger(
            f"STEP6_DENY: EMA_DISTANCE_EXCEEDED "
            f"close={q(close,6)} ema9={q(ema9,6)} band={q(band,6)}"
        )
        return False

    # ========================================================
    # CFG 12 — MIN PRICE MOVE
    # ========================================================
    min_move_pct = float(cfg.get("12_EXECUTION_MIN_PRICE_MOVE_PCT", 0.0))
    if min_move_pct > 0:
        last_cand = candidates[-1]
        ref = _safe_float(last_cand.get("trigger_price"))
        if ref and ref > 0:
            move_pct = abs(close - ref) / ref * 100.0
            if move_pct < min_move_pct:
                state["entry_ready"] = False
                state["entry_bar"] = None
                state["entry_reason"] = "MIN_PRICE_MOVE_BLOCK"
                logger(
                    f"STEP6_DENY: MIN_PRICE_MOVE_BLOCK "
                    f"move_pct={q(move_pct,4)} < min={q(min_move_pct,4)}"
                )
                return False

    # ========================================================
    # CFG 13 — ONLY ON NEW LOW
    # ========================================================
    if cfg.get("13_EXECUTION_ONLY_ON_NEW_LOW", False):
        last_cand = candidates[-1]
        trigger = _safe_float(last_cand.get("trigger_price"))
        if trigger is not None and close > trigger:
            state["entry_ready"] = False
            state["entry_bar"] = None
            state["entry_reason"] = "ONLY_ON_NEW_LOW_BLOCK"
            logger(
                f"STEP6_DENY: ONLY_ON_NEW_LOW_BLOCK "
                f"close={q(close,6)} trigger={q(trigger,6)}"
            )
            return False

    # ========================================================
    # ENTRY 허가
    # ========================================================
    if state.get("position") is None:
        state["entry_ready"] = True
        state["entry_bar"] = state.get("bars")
        state["entry_reason"] = "STEP6_PASS"
        logger(f"STEP6_PASS: entry_bar={state['entry_bar']}")
        return True

    state["entry_ready"] = False
    state["entry_bar"] = None
    state["entry_reason"] = "POSITION_EXISTS"
    logger("STEP6_DENY: POSITION_EXISTS")
    return False



# ============================================================
# [ STEP 7 ] EXECUTION TEMPO CONTROL — LOG ENHANCED
# ------------------------------------------------------------
# ⚠️ 로직 동일 / 시간선 동일 / gate 계약 유지
# ✅ 쿨다운 차단 원인을 AWS 로그에서 즉시 식별
# ============================================================

def step_7_execution_tempo_control(cfg, state, logger=print):
    for k in ["14_STATE_COOLDOWN_ENABLE", "15_COOLDOWN_RANGE_BARS", "16_COOLDOWN_TREND_BARS"]:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP7: {k}")

    if not cfg["14_STATE_COOLDOWN_ENABLE"]:
        return True

    range_bars = int(cfg.get("15_COOLDOWN_RANGE_BARS", 0) or 0)
    trend_bars = int(cfg.get("16_COOLDOWN_TREND_BARS", 0) or 0)
    cd = max(range_bars, trend_bars)

    if cd <= 0:
        return True

    last_exit_bar = state.get("last_exit_bar")
    if last_exit_bar is None:
        return True

    cur_bar = int(state.get("bars", 0))
    last_exit_bar = int(last_exit_bar)

    if (cur_bar - last_exit_bar) < cd:
        remaining = cd - (cur_bar - last_exit_bar)
        state["gate_ok"] = False
        state["gate_reason"] = f"COOLDOWN_BLOCK remaining={remaining}"
        logger(
            f"STEP7_BLOCK: COOLDOWN_BLOCK "
            f"cur_bar={cur_bar} last_exit_bar={last_exit_bar} "
            f"remaining={remaining}"
        )
        return False

    return True



# ============================================================
# [ STEP 8 ] EXECUTION SAFETY GUARD — LOG ENHANCED
# ------------------------------------------------------------
# ⚠️ 로직 동일 / 차단 조건 동일
# ✅ 어떤 SAFETY에 막혔는지 AWS 로그에서 즉시 식별
# ============================================================

def step_8_execution_safety_guard(cfg, safety_ctx, state, logger=print):
    required = [
        "17_ENTRY_MAX_PER_CYCLE",
        "18_MAX_ENTRIES_PER_DAY",
        "19_DATA_STALE_BLOCK",
        "20_EXECUTION_SPREAD_GUARD_ENABLE",
        "40_EXECUTION_SPREAD_MAX_PCT",
    ]
    for k in required:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP8: {k}")

    # --------------------------------------------------------
    # entry limit per cycle
    # --------------------------------------------------------
    max_cycle = int(cfg.get("17_ENTRY_MAX_PER_CYCLE", 1) or 0)
    if (
        max_cycle > 0
        and int(state.get("entries_in_cycle", 0)) >= max_cycle
        and state.get("position") is None
    ):
        state["gate_ok"] = False
        state["gate_reason"] = f"MAX_ENTRY_PER_CYCLE_BLOCK limit={max_cycle}"
        logger(f"STEP8_BLOCK: {state['gate_reason']}")
        return False

    # --------------------------------------------------------
    # entry limit per day (UTC)
    # --------------------------------------------------------
    max_day = int(cfg.get("18_MAX_ENTRIES_PER_DAY", 0) or 0)
    if max_day > 0:
        ms = None
        if safety_ctx and safety_ctx.get("market_time_ms") is not None:
            ms = safety_ctx.get("market_time_ms")

        dk = _ms_to_daykey_utc(ms) if ms is not None else None
        if dk is not None:
            if state.get("day_key") != dk:
                state["day_key"] = dk
                state["entries_today"] = 0
                logger(f"STEP8_INFO: DAY_KEY_RESET dk={dk}")

        if (
            int(state.get("entries_today", 0)) >= max_day
            and state.get("position") is None
        ):
            state["gate_ok"] = False
            state["gate_reason"] = f"MAX_ENTRIES_PER_DAY_BLOCK limit={max_day}"
            logger(f"STEP8_BLOCK: {state['gate_reason']}")
            return False

    if safety_ctx is None:
        return True

    # --------------------------------------------------------
    # stale data block
    # --------------------------------------------------------
    if cfg["19_DATA_STALE_BLOCK"]:
        if safety_ctx.get("is_stale"):
            state["gate_ok"] = False
            state["gate_reason"] = f"DATA_STALE_BLOCK age_ms={safety_ctx.get('age_ms')}"
            logger(f"STEP8_BLOCK: {state['gate_reason']}")
            return False

    # --------------------------------------------------------
    # spread guard
    # --------------------------------------------------------
    if cfg["20_EXECUTION_SPREAD_GUARD_ENABLE"]:
        spread_pct = safety_ctx.get("spread_pct")
        max_spread = float(cfg["40_EXECUTION_SPREAD_MAX_PCT"])

        if spread_pct is None:
            state["gate_ok"] = False
            state["gate_reason"] = "SPREAD_CTX_MISSING"
            logger("STEP8_BLOCK: SPREAD_CTX_MISSING")
            return False

        if float(spread_pct) > max_spread:
            state["gate_ok"] = False
            state["gate_reason"] = (
                f"SPREAD_BLOCK spread_pct={q(spread_pct,4)} "
                f"> max={q(max_spread,4)}"
            )
            logger(f"STEP8_BLOCK: {state['gate_reason']}")
            return False

    return True



# ============================================================
# [ STEP 9 ] REENTRY / CANDIDATE HYGIENE — LOG ENHANCED
# ------------------------------------------------------------
# ⚠️ 로직 동일 / 후보 정리·재진입 차단 조건 동일
# ✅ TTL / POOL / REENTRY 중 차단 지점 즉시 식별
# ============================================================

def step_9_reentry_candidate_hygiene(cfg, market, state, logger=print):
    required = ["21_ENTRY_COOLDOWN_BARS", "22_ENTRY_COOLDOWN_AFTER_EXIT", "23_REENTRY_SAME_REASON_BLOCK",
                "24_ENTRY_LOOKBACK_BARS", "25_REENTRY_PRICE_TOL_PCT", "26_CAND_POOL_TTL_BARS",
                "27_CAND_POOL_MAX_SIZE", "28_CAND_MIN_GAP_BARS"]
    for k in required:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP9: {k}")

    # ---- candidate TTL ----
    ttl = int(cfg.get("26_CAND_POOL_TTL_BARS", 0) or 0)
    if ttl > 0 and state.get("candidates"):
        now_bar = int(state.get("bars", 0))
        before = len(state.get("candidates"))
        state["candidates"] = [c for c in state["candidates"] if (now_bar - int(c.get("bar", now_bar))) <= ttl]
        state["has_candidate"] = len(state["candidates"]) > 0
        after = len(state.get("candidates"))
        if before != after:
            logger(f"STEP9_INFO: CAND_TTL_TRIM before={before} after={after} ttl={ttl}")

    # ---- candidate max size ----
    mx = int(cfg.get("27_CAND_POOL_MAX_SIZE", 0) or 0)
    if mx > 0 and len(state.get("candidates", [])) > mx:
        before = len(state.get("candidates"))
        state["candidates"] = state["candidates"][-mx:]
        state["has_candidate"] = len(state["candidates"]) > 0
        logger(f"STEP9_INFO: CAND_POOL_TRIM before={before} after={len(state['candidates'])} max={mx}")

    # ---- reentry cooldown (bars since last entry) ----
    cd_entry = int(cfg.get("21_ENTRY_COOLDOWN_BARS", 0) or 0)
    last_entry_bar = state.get("last_entry_bar")
    if cd_entry > 0 and last_entry_bar is not None and state.get("position") is None:
        gap = int(state.get("bars", 0)) - int(last_entry_bar)
        if gap < cd_entry:
            state["gate_ok"] = False
            state["gate_reason"] = f"REENTRY_ENTRY_COOLDOWN_BLOCK bars={cd_entry}"
            logger(f"STEP9_BLOCK: {state['gate_reason']} gap={gap}")
            return False

    # ---- cooldown after exit ----
    cd_exit = int(cfg.get("22_ENTRY_COOLDOWN_AFTER_EXIT", 0) or 0)
    last_exit_bar = state.get("last_exit_bar")
    if cd_exit > 0 and last_exit_bar is not None and state.get("position") is None:
        gap = int(state.get("bars", 0)) - int(last_exit_bar)
        if gap < cd_exit:
            state["gate_ok"] = False
            state["gate_reason"] = f"REENTRY_AFTER_EXIT_COOLDOWN_BLOCK bars={cd_exit}"
            logger(f"STEP9_BLOCK: {state['gate_reason']} gap={gap}")
            return False

    # ---- same reason block ----
    if bool(cfg.get("23_REENTRY_SAME_REASON_BLOCK", False)) and state.get("position") is None:
        if state.get("last_entry_reason") and state.get("entry_reason") == state.get("last_entry_reason"):
            state["gate_ok"] = False
            state["gate_reason"] = "REENTRY_SAME_REASON_BLOCK"
            logger("STEP9_BLOCK: REENTRY_SAME_REASON_BLOCK")
            return False

    # ---- reentry price tolerance ----
    tol_pct = float(cfg.get("25_REENTRY_PRICE_TOL_PCT", 100.0) or 0.0)
    if tol_pct >= 0 and state.get("position") is None:
        last_price = _safe_float(state.get("last_entry_price"))
        cur_price = _safe_float(market.get("close")) if market else None
        if last_price and cur_price:
            diff_pct = abs(cur_price - last_price) / last_price * 100.0
            if diff_pct > tol_pct:
                state["gate_ok"] = False
                state["gate_reason"] = f"REENTRY_PRICE_TOL_BLOCK diff_pct={q(diff_pct,4)} > tol={q(tol_pct,4)}"
                logger(f"STEP9_BLOCK: {state['gate_reason']}")
                return False

    return True



# ============================================================
# [ STEP 10 ] VOLATILITY PROTECTION — LOG ENHANCED
# ------------------------------------------------------------
# ⚠️ 로직 동일 / 차단 기준 동일
# ✅ 변동성 차단 사유를 AWS 로그에서 즉시 식별
# ============================================================

def step_10_volatility_protection(cfg, vol_ctx, state, logger=print):
    for k in ["29_VOLATILITY_BLOCK_ENABLE", "30_VOLATILITY_MAX_PCT"]:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP10: {k}")

    if not cfg["29_VOLATILITY_BLOCK_ENABLE"]:
        return True

    if vol_ctx is None or vol_ctx.get("volatility_pct") is None:
        state["gate_ok"] = False
        state["gate_reason"] = "VOL_CTX_MISSING"
        logger("STEP10_BLOCK: VOL_CTX_MISSING")
        return False

    v = float(vol_ctx["volatility_pct"])
    max_v = float(cfg["30_VOLATILITY_MAX_PCT"])

    if v > max_v:
        state["gate_ok"] = False
        state["gate_reason"] = f"VOL_BLOCK v={q(v,4)} > max={q(max_v,4)}"
        logger(f"STEP10_BLOCK: {state['gate_reason']}")
        return False

    return True



# ============================================================
# [ STEP 11 ] OBSERVABILITY — LOG SNAPSHOT ONLY
# ------------------------------------------------------------
# ⚠️ 상태 변경 ❌ / 판단 개입 ❌ / gate 영향 ❌
# ✅ 현재 엔진 상태를 “관찰용”으로만 출력
# ============================================================

def step_11_observability(cfg, state, logger=print):
    for k in ["31_LOG_CANDIDATES", "32_LOG_EXECUTIONS"]:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP11: {k}")

    # --------------------------------------------------------
    # SNAPSHOT (READ ONLY)
    # --------------------------------------------------------
    if cfg.get("31_LOG_CANDIDATES", False):
        logger(
            "STEP11_SNAPSHOT: "
            f"bar={state.get('bars')} "
            f"cands={len(state.get('candidates', []))} "
            f"has_candidate={state.get('has_candidate')} "
            f"gate_ok={state.get('gate_ok')} "
            f"entry_ready={state.get('entry_ready')} "
            f"position={state.get('position')}"
        )

    return True


# ============================================================
# [ STEP 12 ] FAIL-SAFE (CAPITAL MAX LOSS)
# - 33/34 집행
# - 04_CAPITAL_MAX_LOSS_PCT 집행
# ============================================================

def step_12_fail_safe(cfg, state, logger=print):
    for k in ["33_ENGINE_FAIL_FAST_ENABLE", "34_ENGINE_FAIL_NOTIFY_ONLY", "04_CAPITAL_MAX_LOSS_PCT"]:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP12: {k}")

    max_loss_pct = float(cfg.get("04_CAPITAL_MAX_LOSS_PCT", 100.0))
    if state.get("initial_equity") is None or state.get("equity") is None:
        return True

    initial = float(state["initial_equity"])
    equity = float(state["equity"])
    loss = max(0.0, initial - equity)
    limit = initial * (max_loss_pct / 100.0)

    if loss > limit:
        msg = f"FAIL_SAFE_MAX_LOSS: loss={q(loss,4)} > limit={q(limit,4)} (pct={q(max_loss_pct,2)})"
        if cfg.get("33_ENGINE_FAIL_FAST_ENABLE", True):
            logger(msg)
            return False
        else:
            if not cfg.get("34_ENGINE_FAIL_NOTIFY_ONLY", True):
                logger(msg)
            return True

    return True



# ============================================================
# [ STEP 13 ] EXECUTION — REST ALIGNED VERSION (RECORD ONLY)
# - entry_ready는 1 bar 유효
# - OPEN은 entry_bar (REST 완료봉) 에서 즉시 허용
# - REST 5분봉 시간축과 1:1 정합
# ============================================================

def step_13_execution_record_only(cfg, market, state, logger=print):

    # --------------------------------------------------------
    # BASIC GUARD
    # --------------------------------------------------------
    if not state.get("entry_ready", False):
        return False
    if market is None:
        return False
    if state.get("entry_bar") is None:
        return False
    if state.get("position") is not None:
        return False

    current_bar = int(state.get("bars", 0))
    entry_bar = int(state["entry_bar"])

    # --------------------------------------------------------
    # REST TIME AXIS CONTRACT
    #
    # 1) current_bar < entry_bar  → 방어
    # 2) current_bar == entry_bar → 즉시 OPEN
    # 3) current_bar > entry_bar  → ENTRY 만료
    # --------------------------------------------------------

    if current_bar < entry_bar:
        return False

    if current_bar > entry_bar:
        state["entry_ready"] = False
        state["entry_bar"] = None
        state["entry_reason"] = "ENTRY_EXPIRED_REST_AXIS"
        return False

    # current_bar == entry_bar → OPEN
    state["position"] = "OPEN"
    state["position_open_bar"] = current_bar
    state["entry_price"] = market.get("close")

    # --------------------------------------------------------
    # COUNTERS / TIME AXIS UPDATE
    # --------------------------------------------------------
    state["entries_in_cycle"] = int(state.get("entries_in_cycle", 0)) + 1
    state["entries_today"] = int(state.get("entries_today", 0)) + 1
    state["last_entry_bar"] = current_bar
    state["last_entry_reason"] = state.get("entry_reason")
    state["last_entry_price"] = market.get("close")

    # --------------------------------------------------------
    # ENTRY STATE CLEANUP (CRITICAL)
    # --------------------------------------------------------
    state["entry_ready"] = False
    state["entry_bar"] = None
    state["entry_reason"] = None

    # --------------------------------------------------------
    # RECORD (OPEN 성공 시에만)
    # --------------------------------------------------------
    record = {
        "bar": current_bar,
        "time": market.get("time"),
        "price": market.get("close"),
        "capital_usdt": state.get("capital_usdt", cfg["02_CAPITAL_BASE_USDT"]),
        "reason": state.get("last_entry_reason", "RECORD_ONLY"),
        "type": "EXECUTION_RECORD_ONLY_REST_ALIGNED",
    }
    state["execution_records"].append(record)

    if cfg.get("32_LOG_EXECUTIONS", True):
        logger(
            f"STEP13_EXEC_RECORD_REST: bar={record['bar']} "
            f"price={record['price']} capital={record['capital_usdt']}"
        )

    return True





# ============================================================
# [ STEP 14 ] EXIT CORE CALC (SL/TP/TRAIL)
# ============================================================

def step_14_exit_core_calc(cfg, state, market, logger=print):
    if state.get("position") != "OPEN":
        return False
    if state.get("entry_price") is None:
        return False
    if market is None:
        return False

    entry = _safe_float(state.get("entry_price"))
    if entry is None or entry <= 0:
        return False

    # SL/TP는 포지션당 1회 계산 후 고정
    if state.get("sl_price") is None or state.get("tp_price") is None:
        sl = entry * (1 + float(cfg["35_SL_PCT"]) / 100.0)  # SHORT: 위로 가면 손절
        tp = entry * (1 - float(cfg["36_TP_PCT"]) / 100.0)  # SHORT: 아래로 가면 익절
        state["sl_price"] = q(sl, 6)
        state["tp_price"] = q(tp, 6)

    # TRAILING은 계속 갱신
    low = _safe_float(market.get("low"))
    anchor = _safe_float(state.get("trailing_anchor"))

    if anchor is None:
        anchor = entry
    if low is not None:
        anchor = min(anchor, low)

    trailing_stop = anchor * (1 + float(cfg["37_TRAILING_PCT"]) / 100.0)

    state["trailing_anchor"] = q(anchor, 6)
    state["trailing_stop"] = q(trailing_stop, 6)

    # snapshot
    state["sl_tp_trailing_records"].append({
        "bar": state.get("bars"),
        "time": market.get("time"),
        "entry": entry,
        "sl": state.get("sl_price"),
        "tp": state.get("tp_price"),
        "anchor": state.get("trailing_anchor"),
        "trailing_stop": state.get("trailing_stop"),
        "type": "EXIT_CORE_CALC",
    })
    return True


# ============================================================
# [ STEP 15 ] EXIT JUDGE — 3 BAR CONFIRM (CLOSE)
# ============================================================

def step_15_exit_judge(cfg, state, market, logger=print):

    if state.get("position") != "OPEN":
        return False
    if market is None:
        return False

    # 동일 bar entry/exit 금지 (OPEN bar에서는 EXIT 판정 금지)
    pob = state.get("position_open_bar")
    if pob is not None and state.get("bars", 0) <= int(pob):
        state["exit_ready"] = False
        state["exit_reason"] = None
        state["exit_signal"] = None
        state["exit_confirm_count"] = 0
        state["exit_fired_bar"] = None
        state["exit_fired_signal"] = None
        return False

    price = _safe_float(market.get("close"))
    if price is None:
        return False

    sl = _safe_float(state.get("sl_price"))
    tp = _safe_float(state.get("tp_price"))
    tr = _safe_float(state.get("trailing_stop"))

    signal = None

    # 1) SL (SHORT): close가 sl 이상이면 손절 신호
    if sl is not None and price >= sl:
        signal = "SL"

    # 2) TP (SHORT): close가 tp 이하이면 익절 신호
    elif tp is not None and price <= tp:
        signal = "TP"
        state["tp_touched"] = True
        state["trailing_active"] = True

    # 3) TRAIL (SHORT): TP 터치 이후에만 적용
    elif state.get("trailing_active", False):
        if tr is not None and price >= tr:
            signal = "TRAIL"

    # 신호 없음: 리셋
    if signal is None:
        state["exit_signal"] = None
        state["exit_confirm_count"] = 0
        state["exit_ready"] = False
        state["exit_reason"] = None
        state["exit_fired_bar"] = None
        state["exit_fired_signal"] = None
        return False

    # 3 BAR CONFIRM
    if state.get("exit_signal") == signal:
        state["exit_confirm_count"] = int(state.get("exit_confirm_count", 0)) + 1
    else:
        state["exit_signal"] = signal
        state["exit_confirm_count"] = 1
        state["exit_fired_bar"] = None
        state["exit_fired_signal"] = None

    if int(state.get("exit_confirm_count", 0)) >= 3:
        state["exit_ready"] = True
        state["exit_reason"] = f"{signal}_3BAR_CONFIRM_CLOSE"

        # 1회만 기록/락
        if state.get("exit_fired_bar") is None:
            state["exit_fired_bar"] = state.get("bars")
            state["exit_fired_signal"] = signal
            state["exit_records"].append({
                "bar": state.get("bars"),
                "time": market.get("time"),
                "close": price,
                "signal": signal,
                "reason": state.get("exit_reason"),
                "sl": state.get("sl_price"),
                "tp": state.get("tp_price"),
                "trailing_stop": state.get("trailing_stop"),
                "type": "EXIT_CONFIRM_3BAR",
            })
        return True

    state["exit_ready"] = False
    state["exit_reason"] = None
    return False


# ============================================================
# [ STEP 16 ] EXIT EXECUTION
# - "3봉 확정 → 청산 실행 → 상태 리셋"
# - 07_ENTRY_EXEC_ENABLE=False면 실주문 ❌, 대신 SIM_EXIT로 상태/손익 갱신은 수행
# ============================================================

# Binance enums (optional)
try:
    from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
except Exception:
    SIDE_BUY = "BUY"
    SIDE_SELL = "SELL"
    ORDER_TYPE_MARKET = "MARKET"

def order_adapter_send(symbol, side, quantity, reason, logger=print):
    logger(f"ORDER_ADAPTER_SEND: symbol={symbol} side={side} qty={quantity} reason={reason}")
    return True

def _simulate_pnl_short(entry_price, exit_price, capital_usdt):
    ep = _safe_float(entry_price)
    xp = _safe_float(exit_price)
    cap = _safe_float(capital_usdt)
    if ep is None or xp is None or cap is None or ep <= 0 or cap <= 0:
        return 0.0
    # 단순 비율 PnL (레버리지/수수료/수량 계산은 V8에서)
    ret = (ep - xp) / ep
    return cap * ret

def step_16_real_order(cfg, state, market, client, logger=print):
    if not state.get("exit_ready", False):
        return False
    if state.get("order_inflight"):
        return False
    if market is None:
        return False

    # ✅ EXIT 실행은 "항상" 1회 수행 (실주문 OFF라도 SIM_EXIT로 수행)
    state["order_inflight"] = True
    try:
        if cfg.get("07_ENTRY_EXEC_ENABLE", False):
            # REAL ORDER PATH (외부 어댑터 호출)
            order_adapter_send(
                symbol=cfg["01_TRADE_SYMBOL"],
                side=SIDE_BUY,  # SHORT 청산 = BUY (기본)
                quantity=1,
                reason=state.get("exit_reason"),
                logger=logger
            )
        else:
            logger(f"SIM_EXIT: reason={state.get('exit_reason')}")
    finally:
        state["order_inflight"] = False

    # ---- PnL / equity update (record-only simulation) ----
    exit_price = market.get("close")
    pnl = _simulate_pnl_short(state.get("entry_price"), exit_price, state.get("capital_usdt", cfg["02_CAPITAL_BASE_USDT"]))
    state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + float(pnl)
    if state.get("equity") is not None:
        state["equity"] = float(state["equity"]) + float(pnl)

    # ---- TIME AXIS reset ----
    state["position"] = None
    state["position_open_bar"] = None
    state["last_exit_bar"] = state.get("bars")

    # cycle reset
    state["cycle_id"] = int(state.get("cycle_id", 0)) + 1
    state["entries_in_cycle"] = 0

    # ENTRY reset
    state["entry_ready"] = False
    state["entry_bar"] = None
    state["entry_reason"] = None

    # candidate reset (유령 후보 방지)
    state["has_candidate"] = False
    state["candidates"] = []
    state["last_candidate_bar"] = None

    # EXIT reset
    state["exit_ready"] = False
    state["exit_reason"] = None
    state["exit_signal"] = None
    state["exit_confirm_count"] = 0
    state["exit_fired_bar"] = None
    state["exit_fired_signal"] = None

    # SL/TP/TRAIL reset
    state["entry_price"] = None
    state["sl_price"] = None
    state["tp_price"] = None
    state["tp_touched"] = False
    state["trailing_active"] = False
    state["trailing_anchor"] = None
    state["trailing_stop"] = None

    return True


# ============================================================
# LIVE DATA CONNECTION (BINANCE SPOT / REST POLLING)
# ✔️ SINGLE SOURCE: BINANCE REST KLINES (SPOT)
# ✔️ V3 SUCCESS PATH (PROVEN)
# ✔️ BAR = REST 완료봉 (kline[-2])
# ============================================================

import time
import requests

EMA9_PERIOD = 9
KLINE_INTERVAL = "5m"
BTC_SYMBOL = "BTCUSDT"

# ------------------------------------------------------------
# REST MARKET CACHE (STATE CONTRACT)
# ------------------------------------------------------------
# 계약:
# - 이 캐시는 "REST 폴링 결과 버퍼"다
# - 엔진 state / bar / gate / entry / position ❌
# - 판단 로직은 STEP 내부에서만 수행
# ------------------------------------------------------------
_rest_market_cache = {
    "kline": None,          # last completed 5m kline (dict)
    "ema9": None,           # last ema9 value
    "ema9_series": [],      # ema9 history (for slope gate)
    "closes": [],           # close buffer (volatility only)
}

BINANCE_SPOT = "https://api.binance.com/api/v3/klines"


# ------------------------------------------------------------
# REST KLINES FETCH (SPOT / COMPLETED BAR ONLY)
# ------------------------------------------------------------
def fetch_spot_klines(symbol, interval, limit=100):
    try:
        r = requests.get(
            BINANCE_SPOT,
            params={
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
            },
            timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ------------------------------------------------------------
# REST POLLING UPDATE (V3 SUCCESS CORE)
# ------------------------------------------------------------
def poll_rest_kline(symbol, logger=print):
    kl = fetch_spot_klines(symbol, KLINE_INTERVAL, limit=EMA9_PERIOD + 5)
    if not kl or len(kl) < EMA9_PERIOD + 2:
        return None

    # ✅ 완료봉만 사용 (마지막 -1 은 진행중)
    k = kl[-2]

    t     = int(k[6])                 # close time ms
    open_ = float(k[1])
    high  = float(k[2])
    low   = float(k[3])
    close = float(k[4])

    # --------------------------------------------------------
    # EMA9 계산 (BR3 방식: REST 완료봉 누적)
    # --------------------------------------------------------
    series = _rest_market_cache["ema9_series"]
    if not series:
        ema = close
    else:
        kf = 2 / (EMA9_PERIOD + 1)
        ema = close * kf + series[-1] * (1 - kf)

    series.append(ema)
    if len(series) > 50:
        series[:] = series[-50:]

    # --------------------------------------------------------
    # CLOSE BUFFER (VOLATILITY ONLY)
    # --------------------------------------------------------
    closes = _rest_market_cache["closes"]
    closes.append(close)
    if len(closes) > 50:
        closes[:] = closes[-50:]

    # --------------------------------------------------------
    # REST MARKET CACHE (SINGLE SOURCE)
    # --------------------------------------------------------
    _rest_market_cache["kline"] = {
        "time": t,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "ema9": ema,
    }
    _rest_market_cache["ema9"] = ema
    _rest_market_cache["ema9_series"] = series[:]

    logger(f"REST_KLINE_CLOSE: t={t} close={close} ema9={round(ema,6)}")
    return _rest_market_cache["kline"]


# ------------------------------------------------------------
# BR3 STYLE POLLING LOOP HELPER
# ------------------------------------------------------------
def get_rest_market_snapshot(symbol, logger=print):
    return poll_rest_kline(symbol, logger=logger)


# ------------------------------------------------------------
# BINANCE CLIENT (REST)
# - 용도: BTC daily open / orderbook / balance / order
# - LIVE PRICE / BAR 판단에는 사용 ❌
# ------------------------------------------------------------
try:
    from binance.client import Client
except Exception:
    Client = None

def init_binance_client():
    if Client is None:
        raise RuntimeError("python-binance not installed (Client missing)")
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("BINANCE_API_KEY / BINANCE_API_SECRET NOT SET")
    return Client(api_key, api_secret)




# ------------------------------------------------------------
# BTC DAILY OPEN (REST / FILTER ONLY)
# ------------------------------------------------------------
def fetch_btc_daily_open(client):
    try:
        kl = client.futures_klines(symbol=BTC_SYMBOL, interval="1d", limit=2)
        if not kl:
            return None
        open_price = _safe_float(kl[-1][1])
        open_time = int(kl[-1][0])
        return {"open": open_price, "open_time": open_time}
    except Exception:
        return None


# ------------------------------------------------------------
# ORDERBOOK SPREAD (SAFETY ONLY)
# ------------------------------------------------------------
def fetch_orderbook_spread_pct(client, symbol):
    try:
        ob = client.futures_order_book(symbol=symbol, limit=5)
        bid = _safe_float(ob["bids"][0][0]) if ob.get("bids") else None
        ask = _safe_float(ob["asks"][0][0]) if ob.get("asks") else None
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None, None, None
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100.0
        return spread_pct, bid, ask
    except Exception:
        return None, None, None


# ============================================================
# LIVE DATA CONNECTION (BINANCE SPOT REST POLLING)
# ✔️ SINGLE SOURCE: BINANCE SPOT REST /api/v3/klines
# ✔️ BR3 SUCCESS PATH (NO WEBSOCKET)
# ✔️ BAR = REST kline CLOSE (-2 index)
# ✔️ BTC / SUI BOTH POLLED VIA REST
# ============================================================

def app_run_live(logger=print):
    client = init_binance_client()
    state = init_state()

    # ❌ WS INIT 제거 (BR3: WebSocket 사용 안 함)
    # twm = start_ws_kline(...)

    if not step_2_engine_switch(CFG, logger=logger):
        logger("ENGINE_STOP: STEP2")
        return state

    logger("LIVE_START (REST POLLING MODE / BR3)")

    # ========================================================
    # BTC DAILY OPEN (FUTURES API)
    # --------------------------------------------------------
    # ⚠️ 기준선 설명:
    # - 현재 CFG["09_BTC_SESSION_BIAS_ENABLE"] == False
    # - 따라서 BTC SESSION BIAS 기능은 사용하지 않음
    # - FUTURES API 호출이 엔진 생존 경로에 걸리지 않도록
    #   아래 초기 호출을 "주석으로 봉인"
    # - 나중에 ENABLE=True 전환 시, 주석 해제하여 사용
    # ========================================================

    # btc_daily = fetch_btc_daily_open(client)
    # btc_daily_open = btc_daily["open"] if btc_daily else None

    btc_daily = None
    btc_daily_open = None

    while True:
        try:
            # ====================================================
            # refresh BTC daily open (FUTURES API)
            # ----------------------------------------------------
            # ⚠️ 현재:
            # - 09_BTC_SESSION_BIAS_ENABLE = False
            # - 아래 FUTURES API 호출은 사용하지 않으므로
            #   기준선 규칙에 따라 "주석 봉인"
            #
            # ❗ 주의:
            # - 삭제 ❌
            # - 로직 변경 ❌
            # - ENABLE=True 전환 시 주석 해제하여 즉시 사용
            # ====================================================

            # if btc_daily_open is None or (state["ticks"] % 200 == 0):
            #     btc_daily = fetch_btc_daily_open(client)
            #     btc_daily_open = btc_daily["open"] if btc_daily else btc_daily_open

            # ====================================================
            # LIVE MARKET — REST POLLING (SPOT /api/v3/klines)
            # - BR3 성공 경로
            # - 완료봉(-2)만 사용
            # ====================================================
            market = poll_rest_kline(CFG["01_TRADE_SYMBOL"], logger=logger)
            if market is None:
                step_41_loop_idle(CFG)
                continue

            market_core = {
                "time": market.get("time"),
                "open": market.get("open"),
                "high": market.get("high"),
                "low": market.get("low"),
                "close": market.get("close"),
                "ema9": market.get("ema9"),
            }


            # ====================================================
            # BAR ADVANCE — REST KLINE CLOSE ONLY (1 BAR = 1 CLOSE)
            # ====================================================
            bar_time = market_core.get("time")
            if bar_time is not None and state.get("_last_bar_time") != bar_time:
                state["_last_bar_time"] = bar_time
                state["bars"] += 1

            # ====================================================
            # STEP 1: capital (FIXED CAPITAL — V8 BASELINE)
            # ====================================================
            # ❌ fetch_usdt_available 제거 (미정의 즉사 원인)
            available = None

            step_1_engine_limit(
                CFG,
                state,
                capital_ctx={"available_usdt": available},
                logger=logger
            )

            # ====================================================
            # STEP 3: candidate
            # ====================================================
            step_3_generate_candidates(
                CFG,
                market_core,
                state,
                logger=logger
            )

            # ====================================================
            # STEP 4: BTC ctx (FILTER ONLY / BR3 SAFE)
            # ----------------------------------------------------
            # BTC SESSION BIAS OFF 상태:
            # - daily_open = None 이어도
            # - step_4 내부에서 즉시 통과
            # ====================================================
            btc_ctx = {
                "daily_open": _safe_float(btc_daily_open),
                "price": market_core.get("close"),
            }

            # ====================================================
            # STEP 5: EMA ctx (REST 누적 EMA9 series)   # ✅ BR3
            # ====================================================
            ema_ctx = {
                "ema9_series": _rest_market_cache.get("ema9_series") or []
            }

            # ====================================================
            # STEP 8: safety ctx (REST 기준 stale 판정)
            # ====================================================
            now_ms = int(time.time() * 1000)
            age_ms = max(0, now_ms - int(market_core["time"]))
            is_stale = age_ms > 2 * 60 * 1000

            spread_pct, bid, ask = fetch_orderbook_spread_pct(
                client,
                CFG["01_TRADE_SYMBOL"]
            )
            safety_ctx = {
                "market_time_ms": market_core.get("time"),
                "age_ms": age_ms,
                "is_stale": is_stale,
                "spread_pct": spread_pct,
                "bid": bid,
                "ask": ask,
            }

            # ====================================================
            # STEP 10: volatility ctx (BR3 close buffer)
            # ====================================================
            vol_ctx = {"volatility_pct": None}
            if CFG.get("29_VOLATILITY_BLOCK_ENABLE", False):
                closes = _rest_market_cache.get("closes") or []
                if len(closes) >= 2:
                    hi = max(closes)
                    lo = min(closes)
                    close = market_core["close"]
                    if close > 0:
                        vol_ctx["volatility_pct"] = (hi - lo) / close * 100.0

            # ====================================================
            # GATES — ALL BLOCKERS FIRST (BR3 ORDER)
            # ====================================================
            if not step_4_btc_session_bias(CFG, btc_ctx, state, logger): continue
            if not step_5_ema_slope_gate(CFG, ema_ctx, state, logger): continue
            if not step_7_execution_tempo_control(CFG, state, logger): continue
            if not step_8_execution_safety_guard(CFG, safety_ctx, state, logger): continue
            if not step_9_reentry_candidate_hygiene(CFG, market_core, state, logger): continue
            if not step_10_volatility_protection(CFG, vol_ctx, state, logger): continue

            # ENTRY JUDGE — FINAL (모든 게이트 통과 후 단 1회)
            step_6_entry_judge(CFG, market_core, state, logger)

            step_11_observability(CFG, state, logger)

            if not step_12_fail_safe(CFG, state, logger):
                logger("ENGINE_STOP: STEP12_FAIL_SAFE")
                break

            step_13_execution_record_only(CFG, market_core, state, logger)
            step_14_exit_core_calc(CFG, state, market_core, logger)
            step_15_exit_judge(CFG, state, market_core, logger)
            step_16_real_order(CFG, state, market_core, client, logger)

            state["ticks"] += 1
            step_41_loop_idle(CFG)

        except KeyboardInterrupt:
            logger("LIVE_STOP")
            break
        except Exception as e:
            logger(f"LIVE_ERROR: {e}")
            step_41_loop_idle(CFG)


    return state




# ============================================================
# MAIN (SINGLE)
# ============================================================

if __name__ == "__main__":
    _ = app_run_live(logger=print)
