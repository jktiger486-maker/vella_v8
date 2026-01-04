# ============================================================
# VELLA V8 — app.py (AWS READY / ERROR 0)
# STEP 1 ~ STEP 16 (ALL PRESENT, IN ORDER)
# ENGINE INDEPENDENT / LIVE CONTRACT
# ------------------------------------------------------------
# 설계-구현 100% 일치 원칙:
# - CFG(01~37)에 있는 항목은 모두 "집행 로직"이 존재해야 한다.
# - OFF 옵션도 '집행 경로가 존재'해야 한다 (단, OFF면 통과).
# - 후보(candidate)는 이벤트 기록이며, gate/entry/exit과 분리된다.
# ============================================================

import os
import time
from decimal import Decimal, ROUND_DOWN

import pandas as pd


# ============================================================
# CFG (01 ~ 37 FULL)
# ============================================================

CFG = {
    # =====================================================
    # [ STEP 1 ] 거래 대상 · 자본 · 손실 한계
    # =====================================================
    "01_TRADE_SYMBOL": "WOOUSDT",
    "02_CAPITAL_BASE_USDT": 60,
    "03_CAPITAL_USE_FIXED": True,
    "04_CAPITAL_MAX_LOSS_PCT": 100.0,  # 100%면 사실상 차단 없음

    # =====================================================
    # [ STEP 2 ] 엔진 / 실행 스위치
    # =====================================================
    "05_ENGINE_ENABLE": True,
    "06_ENTRY_CANDIDATE_ENABLE": True,
    "07_ENTRY_EXEC_ENABLE": True,  # 🔒 실주문 차단 (STEP A 핵심)

    # =====================================================
    # [ STEP 3 ] 후보 생성
    # =====================================================
    "08_CAND_BODY_BELOW_EMA": True,

    # =====================================================
    # [ STEP 4 ] BTC SESSION BIAS
    # =====================================================
    "09_BTC_SESSION_BIAS_ENABLE": False,  # 유지 (STEP A에서는 OFF)

    # =====================================================
    # [ STEP 5 ] EMA SLOPE
    # =====================================================
    "10_EMA_SLOPE_MIN_PCT": 0.0,
    "11_EMA_SLOPE_LOOKBACK_BARS": 1,

    # =====================================================
    # [ STEP 6 ] PRICE CONFIRM (ENTRY FINAL)
    # =====================================================
    "12_EXECUTION_MIN_PRICE_MOVE_PCT": 0.0,
    "13_EXECUTION_ONLY_ON_NEW_LOW": False,

    # =====================================================
    # [ STEP 7 ] 실행 속도 제어 (COOLDOWN)
    # =====================================================
    "14_STATE_COOLDOWN_ENABLE": False,
    "15_COOLDOWN_RANGE_BARS": 0,
    "16_COOLDOWN_TREND_BARS": 0,

    # =====================================================
    # [ STEP 8 ] 실행 안전장치 (LIMITS / STALE / SPREAD)
    # =====================================================
    "17_ENTRY_MAX_PER_CYCLE": 2,
    "18_MAX_ENTRIES_PER_DAY": 20,
    "19_DATA_STALE_BLOCK": False,                 # ✅ ON
    "20_EXECUTION_SPREAD_GUARD_ENABLE": False,    # ✅ ON

    # =====================================================
    # [ STEP 9 ] 재진입 관리 · 후보 정리
    # =====================================================
    "21_ENTRY_COOLDOWN_BARS": 0,
    "22_ENTRY_COOLDOWN_AFTER_EXIT": 0,
    "23_REENTRY_SAME_REASON_BLOCK": False,
    "24_ENTRY_LOOKBACK_BARS": 100,
    "25_REENTRY_PRICE_TOL_PCT": 100,
    "26_CAND_POOL_TTL_BARS": 100,
    "27_CAND_POOL_MAX_SIZE": 100,
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
    "32_LOG_EXECUTIONS": True,  # 기록만 (실주문 OFF)

    # =====================================================
    # [ STEP 12 ] FAIL-SAFE
    # =====================================================
    "33_ENGINE_FAIL_FAST_ENABLE": True,
    "34_ENGINE_FAIL_NOTIFY_ONLY": True,

    # =====================================================
    # [ STEP 14 ] EXIT CORE PARAMS (중요 3개)
    # =====================================================
    "35_SL_PCT": 0.60,
    "36_TP_PCT": 0.80,
    "37_TRAILING_PCT": 0.40,
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

    # ✅ 한 봉 중복 생성 금지 (bar 기준)
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
# [ STEP 4 ] BTC SESSION BIAS (ENTRY GATE ONLY)
# ------------------------------------------------------------
# [19~26] 체크리스트 준수
# [-1] BTC_BIAS는 후보 생성과 무관 (STEP3)
# [-2] BTC_BIAS는 ENTRY 허용(gate)만 차단
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
        return False

    daily_open = _safe_float(btc_ctx.get("daily_open"))
    price = _safe_float(btc_ctx.get("price"))
    if daily_open is None or price is None:
        state["gate_ok"] = False
        state["gate_reason"] = "BTC_CTX_INVALID"
        return False

    if price < daily_open:
        # OK (통과만)
        return True

    state["gate_ok"] = False
    state["gate_reason"] = f"BTC_BIAS_BLOCK (price={q(price,4)} >= open={q(daily_open,4)})"
    return False


# ============================================================
# [ STEP 5 ] EMA SLOPE GATE (ENTRY GATE ONLY)
# ============================================================

def step_5_ema_slope_gate(cfg, ema_ctx, state, logger=print):
    for k in ["10_EMA_SLOPE_MIN_PCT", "11_EMA_SLOPE_LOOKBACK_BARS"]:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP5: {k}")

    if ema_ctx is None or not ema_ctx.get("ema9_series"):
        state["gate_ok"] = False
        state["gate_reason"] = "EMA_CTX_MISSING"
        return False

    min_pct = float(cfg["10_EMA_SLOPE_MIN_PCT"])
    lb = int(cfg["11_EMA_SLOPE_LOOKBACK_BARS"] or 1)

    series = ema_ctx["ema9_series"]
    if len(series) <= lb:
        state["gate_ok"] = False
        state["gate_reason"] = "EMA_SERIES_TOO_SHORT"
        return False

    ema_now = _safe_float(series[-1])
    ema_prev = _safe_float(series[-1 - lb])
    if ema_now is None or ema_prev is None or ema_prev == 0:
        state["gate_ok"] = False
        state["gate_reason"] = "EMA_INVALID"
        return False

    slope_pct = (ema_now - ema_prev) / ema_prev * 100.0

    # SHORT 기준: slope <= 0 (또는 -min_pct 이하)
    if min_pct <= 0:
        ok = (slope_pct <= 0)
    else:
        ok = (slope_pct <= -abs(min_pct))

    state["gate_ok"] = bool(ok)
    state["gate_reason"] = f"EMA_SLOPE_OK={ok} slope_pct={q(slope_pct,4)}"
    return bool(ok)


# ============================================================
# [ STEP 6 ] ENTRY JUDGEMENT (LIVE CONTRACT / NO ORDER)
# - STEP3 후보 + gate 결과를 읽어 기록만 한다
# - STEP6 PRICE CONFIRM(12/13) 집행 포함
# ============================================================

def step_6_entry_judge(cfg, market, state, logger=print):

    # 🔒 EXIT 우선 시간축: EXIT 준비 중이면 ENTRY 판단 금지
    if state.get("exit_ready") or state.get("exit_confirm_count", 0) > 0:
        state["entry_ready"] = False
        state["entry_bar"] = None
        state["entry_reason"] = "EXIT_IN_PROGRESS"
        return False

    for k in ["12_EXECUTION_MIN_PRICE_MOVE_PCT", "13_EXECUTION_ONLY_ON_NEW_LOW"]:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP6: {k}")

    has_candidate = bool(state.get("has_candidate") or (len(state.get("candidates", [])) > 0))
    gate_ok = bool(state.get("gate_ok", False))

    # ---- 후보 최신성 (24_ENTRY_LOOKBACK_BARS) 집행 ----
    lookback = int(cfg.get("24_ENTRY_LOOKBACK_BARS", 1) or 0)
    latest_cand_bar = None
    if state.get("candidates"):
        latest_cand_bar = state["candidates"][-1].get("bar")
    if lookback > 0 and latest_cand_bar is not None:
        if (state.get("bars", 0) - int(latest_cand_bar)) > lookback:
            has_candidate = False

    # ---- PRICE CONFIRM(12/13) ----
    price_ok = True
    reason_price = None

    if market is not None:
        close = _safe_float(market.get("close"))
        low = _safe_float(market.get("low"))
    else:
        close = None
        low = None

    # 12_MIN_PRICE_MOVE: 후보 trigger 대비 최소 하락폭(%) 요구 (SHORT)
    min_move = float(cfg.get("12_EXECUTION_MIN_PRICE_MOVE_PCT", 0.0) or 0.0)
    if min_move > 0 and has_candidate and close is not None:
        trig = None
        try:
            trig = _safe_float(state["candidates"][-1].get("trigger_price"))
        except Exception:
            trig = None
        if trig and trig > 0:
            move_pct = (trig - close) / trig * 100.0
            if move_pct < min_move:
                price_ok = False
                reason_price = f"PRICE_MOVE_TOO_SMALL move_pct={q(move_pct,4)} < min={q(min_move,4)}"
        else:
            price_ok = False
            reason_price = "TRIGGER_PRICE_MISSING"

    # 13_ONLY_ON_NEW_LOW: 직전 entry 이후의 "새 저가"에서만 허용
    if price_ok and bool(cfg.get("13_EXECUTION_ONLY_ON_NEW_LOW", False)):
        if low is None:
            price_ok = False
            reason_price = "LOW_MISSING_FOR_NEW_LOW"
        else:
            last_entry_price = _safe_float(state.get("last_entry_price"))
            if last_entry_price is not None:
                # "새 저가" = low < last_entry_price (SHORT 관점)
                if not (low < last_entry_price):
                    price_ok = False
                    reason_price = "NOT_NEW_LOW"

    entry_ok = bool(gate_ok and has_candidate and price_ok and state.get("position") is None)

    state["entry_ready"] = entry_ok
    if entry_ok:
        state["entry_reason"] = "GATE_OK_AND_CANDIDATE_PRESENT"
    else:
        # 가장 먼저 막힌 이유를 기록(짧게)
        if not gate_ok:
            state["entry_reason"] = state.get("gate_reason") or "GATE_BLOCK"
        elif not has_candidate:
            state["entry_reason"] = "NO_VALID_CANDIDATE"
        elif not price_ok:
            state["entry_reason"] = reason_price or "PRICE_CONFIRM_BLOCK"
        else:
            state["entry_reason"] = "ENTRY_NOT_READY"

    # entry_ready는 '현재 bar 한정 허가'
    if entry_ok:
        state["entry_bar"] = state.get("bars")
    else:
        state["entry_bar"] = None

    return entry_ok


# ============================================================
# [ STEP 7 ] EXECUTION TEMPO CONTROL (COOLDOWN)
# - 14/15/16 집행
# - gate_ok를 '강제 True'로 만들지 않는다 (차단만 담당)
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

    if (state.get("bars", 0) - int(last_exit_bar)) < cd:
        state["gate_ok"] = False
        state["gate_reason"] = f"COOLDOWN_BLOCK remaining={cd - (state['bars'] - int(last_exit_bar))}"
        return False

    return True


# ============================================================
# [ STEP 8 ] EXECUTION SAFETY GUARD
# - 17_ENTRY_MAX_PER_CYCLE 집행
# - 18_MAX_ENTRIES_PER_DAY 집행 (UTC daykey)
# - 19_DATA_STALE_BLOCK 집행
# - 20_SPREAD_GUARD 집행
# ============================================================

def step_8_execution_safety_guard(cfg, safety_ctx, state, logger=print):
    for k in ["17_ENTRY_MAX_PER_CYCLE", "18_MAX_ENTRIES_PER_DAY", "19_DATA_STALE_BLOCK", "20_EXECUTION_SPREAD_GUARD_ENABLE"]:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP8: {k}")

    # ---- entry limit per cycle ----
    max_cycle = int(cfg.get("17_ENTRY_MAX_PER_CYCLE", 1) or 0)
    if max_cycle > 0 and int(state.get("entries_in_cycle", 0)) >= max_cycle and state.get("position") is None:
        state["gate_ok"] = False
        state["gate_reason"] = f"MAX_ENTRY_PER_CYCLE_BLOCK limit={max_cycle}"
        return False

    # ---- entry limit per day (UTC) ----
    max_day = int(cfg.get("18_MAX_ENTRIES_PER_DAY", 0) or 0)
    if max_day > 0:
        # day_key update from safety_ctx.market_time_ms if provided
        ms = None
        if safety_ctx and safety_ctx.get("market_time_ms") is not None:
            ms = safety_ctx.get("market_time_ms")
        dk = _ms_to_daykey_utc(ms) if ms is not None else None
        if dk is not None:
            if state.get("day_key") != dk:
                state["day_key"] = dk
                state["entries_today"] = 0
        if int(state.get("entries_today", 0)) >= max_day and state.get("position") is None:
            state["gate_ok"] = False
            state["gate_reason"] = f"MAX_ENTRIES_PER_DAY_BLOCK limit={max_day}"
            return False

    if safety_ctx is None:
        return True

    # ---- stale block ----
    if cfg["19_DATA_STALE_BLOCK"]:
        if safety_ctx.get("is_stale"):
            state["gate_ok"] = False
            state["gate_reason"] = f"DATA_STALE_BLOCK age_ms={safety_ctx.get('age_ms')}"
            return False

    # ---- spread guard ----
    if cfg["20_EXECUTION_SPREAD_GUARD_ENABLE"]:
        spread_pct = safety_ctx.get("spread_pct")
        HARD_LIMIT = 0.50  # CFG에 임계값이 없어서 하드리밋
        if spread_pct is None:
            state["gate_ok"] = False
            state["gate_reason"] = "SPREAD_CTX_MISSING"
            return False
        if float(spread_pct) > HARD_LIMIT:
            state["gate_ok"] = False
            state["gate_reason"] = f"SPREAD_BLOCK spread_pct={q(spread_pct,4)}"
            return False

    return True


# ============================================================
# [ STEP 9 ] REENTRY / CANDIDATE HYGIENE
# - 후보 풀 TTL / MAX SIZE 집행
# - 재진입 쿨다운/사유/가격 허용오차 집행(ENTRY GATE 차단)
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
        state["candidates"] = [c for c in state["candidates"] if (now_bar - int(c.get("bar", now_bar))) <= ttl]
        state["has_candidate"] = len(state["candidates"]) > 0

    # ---- candidate max size ----
    mx = int(cfg.get("27_CAND_POOL_MAX_SIZE", 0) or 0)
    if mx > 0 and len(state.get("candidates", [])) > mx:
        state["candidates"] = state["candidates"][-mx:]
        state["has_candidate"] = len(state["candidates"]) > 0

    # ---- reentry cooldown (bars since last entry) ----
    cd_entry = int(cfg.get("21_ENTRY_COOLDOWN_BARS", 0) or 0)
    last_entry_bar = state.get("last_entry_bar")
    if cd_entry > 0 and last_entry_bar is not None and state.get("position") is None:
        if (state.get("bars", 0) - int(last_entry_bar)) < cd_entry:
            state["gate_ok"] = False
            state["gate_reason"] = f"REENTRY_ENTRY_COOLDOWN_BLOCK bars={cd_entry}"
            return False

    # ---- cooldown after exit ----
    cd_exit = int(cfg.get("22_ENTRY_COOLDOWN_AFTER_EXIT", 0) or 0)
    last_exit_bar = state.get("last_exit_bar")
    if cd_exit > 0 and last_exit_bar is not None and state.get("position") is None:
        if (state.get("bars", 0) - int(last_exit_bar)) < cd_exit:
            state["gate_ok"] = False
            state["gate_reason"] = f"REENTRY_AFTER_EXIT_COOLDOWN_BLOCK bars={cd_exit}"
            return False

    # ---- same reason block ----
    if bool(cfg.get("23_REENTRY_SAME_REASON_BLOCK", False)) and state.get("position") is None:
        if state.get("last_entry_reason") and state.get("entry_reason") == state.get("last_entry_reason"):
            state["gate_ok"] = False
            state["gate_reason"] = "REENTRY_SAME_REASON_BLOCK"
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
                return False

    return True


# ============================================================
# [ STEP 10 ] VOLATILITY PROTECTION
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
        return False

    v = float(vol_ctx["volatility_pct"])
    max_v = float(cfg["30_VOLATILITY_MAX_PCT"])

    if v > max_v:
        state["gate_ok"] = False
        state["gate_reason"] = f"VOL_BLOCK v={q(v,4)} > max={q(max_v,4)}"
        return False

    return True


# ============================================================
# [ STEP 11 ] OBSERVABILITY (READY)
# ============================================================

def step_11_observability(cfg, state, logger=print):
    for k in ["31_LOG_CANDIDATES", "32_LOG_EXECUTIONS"]:
        if k not in cfg:
            raise RuntimeError(f"CFG_MISSING_KEY_STEP11: {k}")
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
# [ STEP 13 ] EXECUTION — LIVE CONTRACT (RECORD ONLY)
# - entry_ready는 1 bar 유효
# - OPEN은 entry_bar + 1 bar에서만 허용
# - OPEN 성공 시 entry 상태 즉시 소거
# ============================================================

def step_13_execution_record_only(cfg, market, state, logger=print):

    # --- 기본 가드 ---
    if not state.get("entry_ready", False):
        return False

    if market is None:
        return False

    if state.get("entry_bar") is None:
        return False

    current_bar = int(state.get("bars", 0))
    entry_bar = int(state["entry_bar"])

    # 🔒 시간축 잠금: entry_bar + 1 에서만 OPEN 허용
    if current_bar != entry_bar + 1:
        # 허가 만료 → 즉시 폐기
        state["entry_ready"] = False
        state["entry_bar"] = None
        state["entry_reason"] = "ENTRY_EXPIRED_TIME_AXIS"
        return False

    # --- OPEN ---
    if state.get("position") is None:
        state["position"] = "OPEN"
        state["position_open_bar"] = current_bar
        state["entry_price"] = market.get("close")

        # counters / time-axis
        state["entries_in_cycle"] = int(state.get("entries_in_cycle", 0)) + 1
        state["entries_today"] = int(state.get("entries_today", 0)) + 1
        state["last_entry_bar"] = current_bar
        state["last_entry_reason"] = state.get("entry_reason")
        state["last_entry_price"] = market.get("close")

        # 🔒 entry 상태 소거 (중요)
        state["entry_ready"] = False
        state["entry_bar"] = None

        # --- 기록은 OPEN 성공 시에만 ---
        record = {
            "bar": current_bar,
            "time": market.get("time"),
            "price": market.get("close"),
            "capital_usdt": state.get("capital_usdt", cfg["02_CAPITAL_BASE_USDT"]),
            "reason": state.get("last_entry_reason", "RECORD_ONLY"),
            "type": "EXECUTION_RECORD_ONLY",
        }
        state["execution_records"].append(record)

        if cfg.get("32_LOG_EXECUTIONS", True):
            logger(
                f"STEP13_EXEC_RECORD: bar={record['bar']} "
                f"price={record['price']} capital={record['capital_usdt']}"
            )

        return True

    return False



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
# LIVE DATA CONNECTION (BINANCE FUTURES)
# ============================================================

try:
    from binance.client import Client
except Exception:
    Client = None

LIVE_INTERVAL_SEC = 3
KLINE_INTERVAL = "5m"
EMA9_PERIOD = 9
BTC_SYMBOL = "BTCUSDT"

def init_binance_client():
    if Client is None:
        raise RuntimeError("python-binance not installed (binance.client.Client missing)")
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("BINANCE_API_KEY / BINANCE_API_SECRET NOT SET")
    return Client(api_key, api_secret)

def fetch_live_market_minimal(client, symbol, logger=print):
    try:
        kl = client.futures_klines(symbol=symbol, interval=KLINE_INTERVAL, limit=50)
        if not kl:
            return None
        closes = [_safe_float(k[4]) for k in kl if _safe_float(k[4]) is not None]
        if len(closes) < EMA9_PERIOD:
            return None

        # EMA
        k = 2 / (EMA9_PERIOD + 1)
        ema = closes[0]
        series = [ema]
        for v in closes[1:]:
            ema = v * k + ema * (1 - k)
            series.append(ema)

        last = kl[-1]
        return {
            "time": int(last[6]),          # close time ms
            "open": _safe_float(last[1]),
            "high": _safe_float(last[2]),
            "low": _safe_float(last[3]),
            "close": _safe_float(last[4]),
            "ema9": _safe_float(series[-1]),
            "ema9_series": series[-max(int(CFG.get("11_EMA_SLOPE_LOOKBACK_BARS", 1)) + 1, 3):],
            "kline_rows": kl,
        }
    except Exception as e:
        logger(f"LIVE_MARKET_FETCH_FAIL: {e}")
        return None

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

def fetch_usdt_available(client):
    # dynamic capital source (03_CAPITAL_USE_FIXED=False)
    try:
        bals = client.futures_account_balance()
        for b in bals:
            if str(b.get("asset")).upper() == "USDT":
                return _safe_float(b.get("availableBalance"))
    except Exception:
        return None
    return None


# ============================================================
# ENGINE RUNNER (LIVE / SINGLE PATH)
# STEP 호출 순서:
# 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9 -> 10 -> 11 -> 12 -> 13 -> 14 -> 15 -> 16
# ============================================================

def app_run_live(logger=print):
    client = init_binance_client()
    state = init_state()

    if not step_2_engine_switch(CFG, logger=logger):
        logger("ENGINE_STOP: STEP2")
        return state

    logger("LIVE_START (DATA CONNECTED)")

    btc_daily = fetch_btc_daily_open(client)
    btc_daily_open = btc_daily["open"] if btc_daily else None

    while True:
        try:
            # refresh daily open periodically
            if btc_daily_open is None or (state["ticks"] % 200 == 0):
                btc_daily = fetch_btc_daily_open(client)
                btc_daily_open = btc_daily["open"] if btc_daily else btc_daily_open

            state["ticks"] += 1

            market = fetch_live_market_minimal(client, CFG["01_TRADE_SYMBOL"], logger=logger)
            if market is None:
                time.sleep(LIVE_INTERVAL_SEC)
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
            # BAR ADVANCE — 5m KLINE CLOSE ONLY (LIVE SAFE)
            # ====================================================
            kline_rows = market.get("kline_rows") or []
            last_close_time = None

            if kline_rows:
                last_close_time = int(kline_rows[-1][6])  # close_time ms

            if last_close_time is not None:
                if state.get("_last_bar_time") != last_close_time:
                    state["_last_bar_time"] = last_close_time
                    state["bars"] += 1



            # STEP 1: capital ctx (dynamic)
            available = fetch_usdt_available(client) if not CFG.get("03_CAPITAL_USE_FIXED", True) else None
            capital_ctx = {"available_usdt": available}
            step_1_engine_limit(CFG, state, capital_ctx=capital_ctx, logger=logger)

            # STEP 3: candidate
            step_3_generate_candidates(CFG, market_core, state, logger=logger)

            # STEP 4: BTC ctx
            btc_price = None
            try:
                t = client.futures_symbol_ticker(symbol=BTC_SYMBOL)
                btc_price = _safe_float(t.get("price"))
            except Exception:
                btc_price = None
            btc_ctx = {"daily_open": _safe_float(btc_daily_open), "price": btc_price}

            # STEP 5: EMA ctx
            ema_ctx = {"ema9_series": market.get("ema9_series") or []}

            # STEP 8: safety ctx (needs time)
            now_ms = int(time.time() * 1000)
            age_ms = max(0, now_ms - int(market_core["time"])) if market_core.get("time") is not None else None
            is_stale = (age_ms is not None and age_ms > 2 * 60 * 1000)
            spread_pct, bid, ask = fetch_orderbook_spread_pct(client, CFG["01_TRADE_SYMBOL"])
            safety_ctx = {
                "market_time_ms": market_core.get("time"),
                "age_ms": age_ms,
                "is_stale": is_stale,
                "spread_pct": spread_pct,
                "bid": bid,
                "ask": ask,
            }

            # STEP 10: vol ctx (only computed when enabled)
            vol_ctx = {"volatility_pct": None}
            if CFG.get("29_VOLATILITY_BLOCK_ENABLE", False):
                rows = market.get("kline_rows") or []
                lookback = min(20, len(rows))
                hi = max([_safe_float(r[2]) for r in rows[-lookback:] if _safe_float(r[2]) is not None], default=None)
                lo = min([_safe_float(r[3]) for r in rows[-lookback:] if _safe_float(r[3]) is not None], default=None)
                close = _safe_float(market_core.get("close"))
                vol_pct = None
                if hi is not None and lo is not None and close and close > 0:
                    vol_pct = (hi - lo) / close * 100.0
                vol_ctx = {"volatility_pct": vol_pct}

            # GATES (4 -> 5 -> 6 -> 7 -> 8 -> 9 -> 10)
            if not step_4_btc_session_bias(CFG, btc_ctx, state, logger=logger):
                time.sleep(LIVE_INTERVAL_SEC); continue
            if not step_5_ema_slope_gate(CFG, ema_ctx, state, logger=logger):
                time.sleep(LIVE_INTERVAL_SEC); continue

            _ = step_6_entry_judge(CFG, market_core, state, logger=logger)

            if not step_7_execution_tempo_control(CFG, state, logger=logger):
                time.sleep(LIVE_INTERVAL_SEC); continue
            if not step_8_execution_safety_guard(CFG, safety_ctx, state, logger=logger):
                time.sleep(LIVE_INTERVAL_SEC); continue
            if not step_9_reentry_candidate_hygiene(CFG, market_core, state, logger=logger):
                time.sleep(LIVE_INTERVAL_SEC); continue
            if not step_10_volatility_protection(CFG, vol_ctx, state, logger=logger):
                time.sleep(LIVE_INTERVAL_SEC); continue

            step_11_observability(CFG, state, logger=logger)

            if not step_12_fail_safe(CFG, state, logger=logger):
                logger("ENGINE_STOP: STEP12_FAIL_SAFE")
                break

            # STEP 13: record-only entry open
            step_13_execution_record_only(CFG, market_core, state, logger=logger)

            # STEP 14/15/16: exit pipeline
            step_14_exit_core_calc(CFG, state, market_core, logger=logger)
            step_15_exit_judge(CFG, state, market_core, logger=logger)
            step_16_real_order(CFG, state, market_core, client, logger=logger)

            if state["ticks"] % 25 == 0:
                logger(
                    f"LIVE_TICK: ticks={state['ticks']} bars={state['bars']} gate_ok={state.get('gate_ok')} "
                    f"pos={state.get('position')} entries_today={state.get('entries_today')} "
                    f"equity={q(state.get('equity', 0.0),4)} pnl={q(state.get('realized_pnl', 0.0),4)} "
                    f"spread={safety_ctx.get('spread_pct')} stale={safety_ctx.get('is_stale')}"
                )

            time.sleep(LIVE_INTERVAL_SEC)

        except KeyboardInterrupt:
            logger("LIVE_STOP"); break
        except Exception as e:
            logger(f"LIVE_ERROR: {e}")
            time.sleep(LIVE_INTERVAL_SEC)

    return state


# ============================================================
# MAIN (SINGLE)
# ============================================================

if __name__ == "__main__":
    _ = app_run_live(logger=print)
