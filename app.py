# ============================================================
# VELLA_v8_1 — SHORT ONLY AUTO TRADING
# AWS + BINANCE FUTURES
# 구조 : ema +vwap
# ============================================================

import os
import sys
import time
import signal
import requests
from decimal import Decimal, ROUND_DOWN
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET

# ============================================================
# CFG — 번호 대역 기준 구조 (READ ONLY)
# ============================================================
CFG = {
    # BASIC
    "TRADE_SYMBOL": "POLYXUSDT",
    "CAPITAL_USDT": 30,
    "ENGINE_ENABLE": True,
    "LEVERAGE": 1,

    #=======================================================
    # STAGE 1 [01~19] : PRE-ENTRY FILTER
    #========================================================
    "01_BTC_DAILY_ENABLE": False,
    "02_BTC_1H_ENABLE": False,
    "03_SYMBOL_1H_ENABLE": False,
    "04_SHOCK_ENABLE": False,
    "14_SHOCK_PCT": 99.0,  # OFF 상태 (브8 기본)
    # 의미:
    #  - 단일 봉(짧은 구간)에서 이 % 이상 급등락하면 "쇼크"로 판단
    #  - SHOCK_ENABLE = True 일 때만 실제로 작동
    # 추천값 (쓸 경우):
    #  - 기본값: 5.0
    #  - 추천범위: 4.0 ~ 7.0
    # 사유:
    #  - 4.0 ↓ : 정상 변동도 쇼크로 오인 (체결 급감)
    #  - 5.0   : 뉴스/급발작만 컷 (보험용 최적)
    #  - 7.0 ↑ : 쇼크 필터 의미 약화
    "05_VOLUME_ENABLE": False,
    "15_VOLUME_K": 0.0,

    # --------------------------------------------------
    "06_SPREAD_ENABLE": False, # True 일 때 16 사용가능
    "16_SPREAD_MAX": 99.0,     # 99 의미는 오프라는 뜻
    # 의미:
    #  - (매도호가 - 매수호가) / 중간가 × 100
    #  - 이 %를 초과하면 "체결 환경 불량"으로 판단해 ENTRY 차단
    #  - SPREAD_ENABLE = True 일 때만 실제로 작동
    # 추천값 (쓸 경우):
    #  - 기본값: 0.20
    #  - 추천범위: 0.10 ~ 0.30
    # 사유:
    #  - 0.10 ↓ : 정상 장에서도 잦은 차단 (체결 급감)
    #  - 0.20   : 유동성 악화 구간만 컷 (권고)
    #  - 0.30 ↑ : 의미 약화, 슬리피지 위험 증가
    # --------------------------------------------------

    "07_DEAD_MARKET_ENABLE": False,  # True 일 때 17 사용가능
    "17_ATR_MIN_PCT": 0.0,   # 0 의미는 오프라는 뜻
    # 의미:
    #  - ATR(평균 진폭)을 퍼센트로 환산한 값
    #  - 이 값보다 작으면 "죽은 장(움직임 없음)"으로 판단해 ENTRY 차단
    #  - DEAD_MARKET_ENABLE = True 일 때만 실제로 작동
    # 추천값 (쓸 경우):
    #  - 기본값: 0.20
    #  - 추천범위: 0.10 ~ 0.30
    # 사유:
    #  - 0.10 ↓ : 거의 모든 장 통과 (의미 약함)
    #  - 0.20   : 완전 횡보/죽은 장만 컷 (권고)
    #  - 0.30 ↑ : 변동성 낮은 정상 추세까지 차단 위험
    # --------------------------------------------------

    "08_MATH_GUARD_ENABLE": True,  # 항상 True 로
    # 계산 결과가 이상하면 ENTRY / EXIT 전부 스킵
    # 로그 찍고 아무것도 안 함
    # --------------------------------------------------

    "09_FUNDING_ENABLE": False,
    
    #===================================================
    # [20~21] STAGE 2: ENTRY 조건
    "21_ENTRY_EMA_FAST": 10,
    "22_ENTRY_EMA_SLOW": 20,

    #=====================================================

    #===================================================
    # STAGE 3 [30~49] : ENTRY MANAGEMENT
    #=====================================================
    "30_IK_VWAP_ENABLE": False,
    "31_IK_EMA_ENABLE": False,
    "32_IK_VOLUME_ENABLE": False,
    # -------------------------------------------------- 

    "33_BIG_EMA_ENABLE": False,  # True 시 33번과 같이 사용
    "33_GRACE_BARS": 0,
    # 의미:
    #  - ENTRY 직후, 지정한 봉(bar) 수 동안
    #    EXIT / 추가 필터 / 관리 로직을 무시하는 "유예 구간"
    #  - 진입 직후 흔들림에 바로 털리는 걸 방지하기 위한 장치
    # 브8에서 0을 쓴 이유:
    #  - 사실상 OFF
    #  - 진입 후 즉시 EMA/SL 규칙 적용
    #  - 실체결 확인용 → 최대한 단순화
    # 추천값 (쓸 경우):
    #  - 기본값: 1
    #  - 추천범위: 1 ~ 3
    # 사유:
    #  - 0 : 진입 직후 미세 되돌림에도 바로 EXIT (가장 민감)
    #  - 1 : 첫 봉 흔들림 보호 (권고)
    #  - 2~3 : 추세 확인 여유 ↑ / 대신 손절 커질 수 있음
    # 사용 가이드:
    #  - 스캘핑/빠른 EMA 전략 → 0 ~ 1
    #  - 추세 추종/파동 전략 → 1 ~ 2
    #  - 3 이상은 과보호 위험
    #-----------------------------------------------------
        
    "34_BIG_DONCHIAN_ENABLE": False,
    "34_LOOKBACK": 1,
    # 의미:
    #  - ENTRY 이후 기준 구간(LOOKBACK) 동안의 최저가(Donchian Low)를 계산
    #  - 이후 가격이 그 최저가를 다시 하향 돌파하면
    #    → “추세 실패 / 구조 붕괴”로 판단하고 즉시 EXIT
    #  - SL, EMA EXIT보다 앞단에서 작동하는 조기 컷 안전망
    # 추천값:
    #  - 기본값: 3
    #  - 추천범위: 2 ~ 5
    # 사유:
    #  - 1 :
    #    · 직전 봉 저점만 기준
    #    · 정상적인 눌림에도 즉시 EXIT
    #    · 과민 반응, 체결 수 급감
    #  - 2 :
    #    · 아주 짧은 구조 확인
    #    · 스캘핑/초단기 추세에만 적합
    #  - 3 : (권고)
    #    · ENTRY 이후 “구조적 저점 갱신”만 컷
    #    · 정상 눌림 허용 + 가짜 추세 제거 균형점
    #  - 4~5 :
    #    · 추세 신뢰도 ↑
    #    · 대신 초기 손실 커질 수 있음
    #    · 변동성 큰 종목에서만 의미 있음
    # 사용 가이드:
    #  - 스캘핑 / 초단기 EMA 전략      → 2
    #  - EMA 추세 + 눌림 허용 전략     → 3 (권고)
    #  - 변동성 큰 장 / 추세 확정용    → 4 ~ 5
    #-----------------------------------------------------

    "35_SMALL_PULLBACK_ENABLE": False,
    "36_SMALL_VOL_ENABLE": False,
    "37_SMALL_ESCGO_ENABLE": False,
    "38_SURVIVAL_ENABLE": False,

    #===================================================
    # STAGE 4 [50~69] : EXIT
    #======================================================
    "50_SL_PCT": 5.0,

    #-----------------------------------------------------
    "54_SLOPE_LOOKBACK": 2,  # 아래 54_SLOPE_EXIT_PCT 같이 사용
    # 의미:
    #  - EXIT 판단에서 EMA 기울기를 계산할 때
    #    “현재 EMA”와 “과거 EMA”를 몇 봉 차이로 비교할지 결정
    #  - 즉, EMA의 방향 전환·둔화를 얼마나 빠르게 감지할지에 대한 민감도 설정
    # 추천값:
    #  - 기본값: 2
    #  - 추천범위: 1 ~ 4
    # 사유:
    #  - 1 :
    #    · 바로 직전 EMA와 비교
    #    · 가장 민감, 미세한 꺾임에도 EXIT
    #    · 노이즈 EXIT 증가 위험
    #  - 2 : (권고)
    #    · 단기 추세 꺾임만 포착
    #    · EMA-only EXIT에 가장 안정적인 반응속도
    #    · 브8 실체결 단계에 최적
    #  - 3 :
    #    · 추세 둔화가 “확실할 때만” EXIT
    #    · HOLD 시간 증가 / 손절 지연 가능
    #  - 4 :
    #    · 중기 추세 전환 감지
    #    · EXIT가 늦어지고 손실 확대 위험
    # 사용 가이드:
    #  - 스캘핑 / 빠른 반응 EXIT     → 1 ~ 2
    #  - EMA 추세 추종 / 안정 HOLD   → 2 ~ 3
    #  - 느린 추세 전략              → 3 ~ 4

    "54_SLOPE_EXIT_PCT": 999.0,
    # 의미:
    #  - EMA 기울기(slope_pct)가 이 값보다 커지면 EXIT
    #  - 즉, EMA가 “이 정도 이상 꺾였다/되돌렸다”고 판단되면
    #    EMA CROSS 이전에 미리 EXIT하는 안전장치
    # 추천값:
    #  - 기본값(OFF): 999.0
    #  - 추천범위(ON 시): 0.05 ~ 0.30
    # 사유 (값별 의미):
    #  - 0.05 :
    #    · EMA가 아주 조금만 꺾여도 EXIT
    #    · 노이즈 EXIT 많음
    #    · 초스캘핑 / 실험용
    #  - 0.10 :
    #    · 단기 추세 둔화 조기 감지
    #    · EMA CROSS 전에 절반 정도 먼저 탈출
    #    · 보수적 성향
    #  - 0.20 : (실사용 시 권고)
    #    · “명확한 추세 둔화”만 EXIT
    #    · EMA CROSS와 충돌 적음
    #    · 추세 추종 + 조기 보호 균형점
    #  - 0.30 :
    #    · 거의 EMA CROSS 수준
    #    · 사실상 보조 EXIT 의미 약화
    # 사용 가이드 (중요):
    #  - EMA 10/15만으로 EXIT 하겠다면 → 999.0 고정
    #  - EMA CROSS 전에 한 번 더 보험을 두고 싶다면 → 0.15 ~ 0.25
    #  - 두 개를 동시에 강하게 쓰는 건 비추천 (EXIT 과다)

    "54_SLOPE_EMA": 10,
    # 의미:
    #  - EMA 기울기(slope)를 계산할 때
    #    어떤 EMA를 기준으로 볼 것인지 지정
    #  - 즉,
    #    EMA(10) 자체가 꺾이고 있는지를 보겠다는 뜻
    # 추천값:
    #  - 기본값: 10
    #  - 추천범위: 10 ~ 20
    # 사유:
    #  - 10 : (권고)
    #    · 가장 빠르게 반응하는 EMA
    #    · EXIT 조기 감지에 적합
    #    · EMA 10/15/20 구조와 개념적으로 일관
    #  - 15 :
    #    · EMA 10보다 안정적
    #    · 조기 EXIT 빈도 감소
    #    · HOLD 시간이 길어짐
    #  - 20 :
    #    · 사실상 EMA CROSS에 가까워짐
    #    · slope EXIT 의미 약화
    # 사용 가이드 (현재 브8 기준):
    #  - "우리는 EMA 10/15로만 EXIT한다" → 값 의미 없음
    #  - SLOPE_EXIT_PCT = 999.0 이므로
    #    · 어떤 EMA를 넣어도 실제 동작 영향 0
    #    · 10을 둬도, 15를 둬도 결과 동일
    # 현재 브8 최종 정리:
    #  - 54_SLOPE_EMA = 10 (기본값 유지)
    #  - 기울기 EXIT는 봉인 상태
    #  - 실제 EXIT는 EMA 10/15 CROSS + SL만 작동
    #-----------------------------------------------------

    #-----------------------------------------------------
    # [56] STAGE 2: EXIT 기본 조건
    #-----------------------------------------------------
    "56_EXIT_EMA_FAST": 10,
    # 의미:
    #  - EXIT 판단에서 “빠른 쪽 EMA”
    #  - 느린 EMA(56_EXIT_EMA_SLOW)와의 관계로
    #    포지션 종료 시점을 결정
    # 실제 EXIT 조건:
    #  - EMA_FAST > EMA_SLOW  → EXIT
    #  - 즉, 숏 기준에서
    #    · 하락 추세가 끝나고
    #    · 단기 반등(되돌림)이 시작되면 청산
    # 왜 10이냐:
    #  - ENTRY에서도 EMA 10을 핵심 신호로 사용
    #  - 가장 먼저 방향 전환을 감지
    #  - EXIT를 “빠르게” 실행하기 위한 기준선
    # 추천값:
    #  - 기본값: 10
    #  - 추천범위: 9 ~ 12
    # 사유:
    #  - 9 :
    #    · 더 민감
    #    · EXIT가 매우 빠름
    #    · 수익 짧아질 수 있음
    #  - 10 : (권고)
    #    · 반응 속도와 안정성 균형
    #    · EMA 10/15/20 구조의 중심축
        #  - 11~12 :
    #    · EXIT 약간 지연
    #    · HOLD 시간 증가
    #    · 대신 수익 구간 늘어날 수 있음
    # 현재 브8 최종 해석:
    #  - EMA 10 / 15 크로스가 EXIT의 “헌법”
    #  - EXIT_EMA_FAST = 10 은 고정값
    #  - 다른 EXIT 로직은 모두 보조 또는 봉인 상태
    "56_EXIT_EMA_SLOW": 15,
    #-----------------------------------------------------

}

# ============================================================
# UTIL
# ============================================================
def q(x, p=6):
    """Display-only formatting. Never use in calculation/logic."""
    return float(Decimal(str(x)).quantize(Decimal("1." + "0" * p), rounding=ROUND_DOWN))

def now_ts():
    return int(time.time())

def log(stage, priority, name, result):
    print(f"[{now_ts()}] S{stage} P{priority} {name}: {result}")

# ============================================================
# BINANCE CLIENT
# ============================================================
def init_binance_client():
    k = os.getenv("BINANCE_API_KEY")
    s = os.getenv("BINANCE_API_SECRET")
    if not k or not s:
        raise RuntimeError("API KEY missing")
    return Client(k, s)

def setup_futures_env(client, symbol, leverage):
    try:
        client.futures_change_position_mode(dualSidePosition=False)
        print("Position mode set to ONE_WAY")
    except Exception as e:
        print(f"Position mode already ONE_WAY or error: {e}")
    
    try:
        client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
        print(f"Margin type set to ISOLATED for {symbol}")
    except Exception as e:
        print(f"Margin type already ISOLATED or error: {e}")
    
    try:
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
        print(f"Leverage set to {leverage}x for {symbol}")
    except Exception as e:
        print(f"Leverage setup error: {e}")

class FX:
    def __init__(self, client, symbol):
        self.client = client
        self.symbol = symbol
        info = client.futures_exchange_info()
        sym = next(s for s in info["symbols"] if s["symbol"] == symbol)
        lot = next(f for f in sym["filters"] if f["filterType"] == "LOT_SIZE")
        self.step = Decimal(lot["stepSize"])
        self.minq = Decimal(lot["minQty"])
        
        min_notional_filter = next((f for f in sym["filters"] if f["filterType"] == "MIN_NOTIONAL"), None)
        self.min_notional = Decimal(min_notional_filter["notional"]) if min_notional_filter else Decimal("0")
        print(f"MIN_NOTIONAL: {self.min_notional}")
    
    def _norm_qty(self, qty):
        qd = (Decimal(str(qty)) / self.step).to_integral_value(rounding=ROUND_DOWN) * self.step
        if qd < self.minq:
            return None
        d = len(str(self.step).split(".")[1].rstrip("0"))
        return f"{qd:.{d}f}"
    
    def order(self, side, qty, price=None, reduce_only=False):
        qs = self._norm_qty(qty)
        if qs is None:
            print(f"Order skipped: qty {qty} below minQty {self.minq}")
            return 0.0
        
        if price and Decimal(qs) * Decimal(str(price)) < self.min_notional:
            print(f"Order skipped: notional {Decimal(qs) * Decimal(str(price))} below MIN_NOTIONAL {self.min_notional}")
            return 0.0
        
        try:
            self.client.futures_create_order(
                symbol=self.symbol,
                side=SIDE_SELL if side == "SELL" else SIDE_BUY,
                type=ORDER_TYPE_MARKET,
                quantity=qs,
                reduceOnly=reduce_only,
                recvWindow=5000
            )
            return float(qs)
        except Exception as e:
            print(f"Order failed: {e}")
            return 0.0

# ============================================================
# MARKET DATA
# ============================================================
BINANCE_API = "https://fapi.binance.com/fapi/v1/klines"

def calc_ema(prices, period):
    if len(prices) < period:
        return None
    alpha = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * alpha + ema * (1 - alpha)
    return ema

def fetch_klines(symbol, interval, limit):
    try:
        resp = requests.get(
            BINANCE_API,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=5
        )
        data = resp.json()
        
        if not isinstance(data, list):
            print(f"fetch_klines invalid response ({symbol} {interval}): {data}")
            return None
        
        return data
    
    except Exception as e:
        print(f"fetch_klines exception ({symbol} {interval}): {e}")
        return None

def collect_data(symbol):
    klines_15m = fetch_klines(symbol, "15m", 50)
    klines_1h = fetch_klines(symbol, "1h", 30)
    klines_1m = fetch_klines(symbol, "1m", 5)
    btc_klines_daily = fetch_klines("BTCUSDT", "1d", 2)
    btc_klines_1h = fetch_klines("BTCUSDT", "1h", 30)
    
    if klines_15m is None or klines_1h is None or klines_1m is None or btc_klines_daily is None or btc_klines_1h is None:
        return None
    
    k_current = klines_15m[-2]
    
    closes_15m = [float(k[4]) for k in klines_15m[:-1]]
    highs_15m = [float(k[2]) for k in klines_15m[:-1]]
    lows_15m = [float(k[3]) for k in klines_15m[:-1]]
    volumes_15m = [float(k[5]) for k in klines_15m[:-1]]
    
    closes_1h = [float(k[4]) for k in klines_1h[:-1]]
    closes_1m = [float(k[4]) for k in klines_1m[:-1]]
    btc_closes_1h = [float(k[4]) for k in btc_klines_1h[:-1]]
    
    current_open = float(k_current[1])
    current_high = float(k_current[2])
    current_low = float(k_current[3])
    current_close = float(k_current[4])
    current_volume = float(k_current[5])
    bar_ts = int(k_current[6])
    
    ema_periods = set()
    
    ema_periods.add(CFG["21_ENTRY_EMA_FAST"])
    ema_periods.add(CFG["22_ENTRY_EMA_SLOW"])
    
    if CFG.get("31_IK_EMA_ENABLE"):
        ema_periods.add(CFG["41_IK_EMA_FAST"])
        ema_periods.add(CFG["42_IK_EMA_SLOW"])
    
    if CFG.get("33_BIG_EMA_ENABLE"):
        ema_periods.add(CFG["43_BIG_EMA_FAST"])
        ema_periods.add(CFG["44_BIG_EMA_SLOW"])
    
    ema_periods.add(CFG["54_SLOPE_EMA"])
    ema_periods.add(CFG["56_EXIT_EMA_FAST"])
    ema_periods.add(CFG["56_EXIT_EMA_SLOW"])
    
    ema_values = {}
    for period in ema_periods:
        if period <= len(closes_15m):
            ema_values[period] = calc_ema(closes_15m[-period:] + [current_close], period)
    
    cum_pv = sum(float(k[4]) * float(k[5]) for k in klines_15m[-30:-1])
    cum_v = sum(float(k[5]) for k in klines_15m[-30:-1])
    vwap = cum_pv / cum_v if cum_v > 0 else current_close
    
    volume_avg = sum(volumes_15m[-20:]) / 20
    
    trs = []
    for i in range(1, min(16, len(closes_15m))):
        h = highs_15m[-i]
        l = lows_15m[-i]
        prev_c = closes_15m[-i-1] if i < len(closes_15m) else closes_15m[-i]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    atr15 = sum(trs) / len(trs) if trs else 0
    
    donchian_low = min(lows_15m[-CFG["34_LOOKBACK"]:]) if len(lows_15m) >= CFG["34_LOOKBACK"] else current_low
    
    slope_ema_period = CFG["54_SLOPE_EMA"]
    slope_ema_hist = []
    for i in range(slope_ema_period, min(50, len(closes_15m))):
        slope_ema_hist.append(calc_ema(closes_15m[i-slope_ema_period:i], slope_ema_period))
    
    btc_daily_open = float(btc_klines_daily[-2][1])
    btc_price = float(btc_klines_daily[-1][4])
    btc_ema20_1h = calc_ema(btc_closes_1h[-20:], 20)
    
    symbol_ema20_1h = calc_ema(closes_1h[-20:], 20)
    
    price_1m_prev = closes_1m[-2] if len(closes_1m) >= 2 else current_close
    
    return {
        "bar_ts": bar_ts,
        "open": current_open,
        "high": current_high,
        "low": current_low,
        "close": current_close,
        "volume": current_volume,
        "ema_values": ema_values,
        "vwap": vwap,
        "volume_avg": volume_avg,
        "atr15": atr15,
        "donchian_low": donchian_low,
        "slope_ema_hist": slope_ema_hist,
        "btc_daily_open": btc_daily_open,
        "btc_price": btc_price,
        "btc_ema20_1h": btc_ema20_1h,
        "symbol_ema20_1h": symbol_ema20_1h,
        "price_1m_prev": price_1m_prev,
    }

# ============================================================
# CONTEXT (STAGE 간 전달 전용)
# ============================================================
class Context:
    def __init__(self):
        self.position = None
        self.entry_price = None
        self.entry_volume_ref = None
        self.position_qty = 0.0
        self.entry_bar_ts = None
        self.big_ema_state = "NONE"
        self.big_ema_entry_bar_ts = None
        self.big_ema_bar_count = 0

# ============================================================
# STAGE 1 — PRE-ENTRY FILTER
# ============================================================
def s1_p1_btc_daily(data):
    if not CFG["01_BTC_DAILY_ENABLE"]:
        return True
    btc_return = (data["btc_price"] - data["btc_daily_open"]) / data["btc_daily_open"] * 100
    result = btc_return < 0
    if not result:
        log(1, 1, "BTC_DAILY", "FAIL")
    return result

def s1_p2_btc_1h(data):
    if not CFG["02_BTC_1H_ENABLE"]:
        return True
    result = data["btc_price"] <= data["btc_ema20_1h"]
    if not result:
        log(1, 2, "BTC_1H", "FAIL")
    return result

def s1_p3_symbol_1h(data):
    if not CFG["03_SYMBOL_1H_ENABLE"]:
        return True
    result = data["close"] <= data["symbol_ema20_1h"]
    if not result:
        log(1, 3, "SYMBOL_1H", "FAIL")
    return result

def s1_p4_shock(data):
    if not CFG["04_SHOCK_ENABLE"]:
        return True
    price_change = abs((data["close"] - data["price_1m_prev"]) / data["price_1m_prev"] * 100)
    result = price_change <= CFG["14_SHOCK_PCT"]
    if not result:
        log(1, 4, "SHOCK", "FAIL")
    return result

def s1_p5_volume(data):
    if not CFG["05_VOLUME_ENABLE"]:
        return True
    result = data["volume"] >= data["volume_avg"] * CFG["15_VOLUME_K"]
    if not result:
        log(1, 5, "VOLUME", "FAIL")
    return result

def s1_p6_spread():
    if not CFG["06_SPREAD_ENABLE"]:
        return True
    return True

def s1_p7_dead_market(data):
    if not CFG["07_DEAD_MARKET_ENABLE"]:
        return True
    atr_pct = data["atr15"] / data["close"] * 100
    result = atr_pct >= CFG["17_ATR_MIN_PCT"]
    if not result:
        log(1, 7, "DEAD_MARKET", "FAIL")
    return result

def s1_p8_math_guard(data):
    if not CFG["08_MATH_GUARD_ENABLE"]:
        return True
    ema_fast = data["ema_values"].get(CFG["21_ENTRY_EMA_FAST"])
    ema_slow = data["ema_values"].get(CFG["22_ENTRY_EMA_SLOW"])
    result = data["vwap"] is not None and ema_fast is not None and ema_slow is not None
    if not result:
        log(1, 8, "MATH_GUARD", "FAIL")
    return result

def s1_p9_funding():
    if not CFG["09_FUNDING_ENABLE"]:
        return True
    return True

def stage1_preentry(data):
    if not s1_p1_btc_daily(data):
        return False
    if not s1_p2_btc_1h(data):
        return False
    if not s1_p3_symbol_1h(data):
        return False
    if not s1_p4_shock(data):
        return False
    if not s1_p5_volume(data):
        return False
    if not s1_p6_spread():
        return False
    if not s1_p7_dead_market(data):
        return False
    if not s1_p8_math_guard(data):
        return False
    if not s1_p9_funding():
        return False
    return True

# ============================================================
# STAGE 2 — ENTRY
# ============================================================
def stage2_entry(data):
    ema_fast = data["ema_values"].get(CFG["21_ENTRY_EMA_FAST"])
    ema_slow = data["ema_values"].get(CFG["22_ENTRY_EMA_SLOW"])
    if ema_fast is None or ema_slow is None:
        return False
    return data["close"] < data["vwap"] and ema_fast < ema_slow

# ============================================================
# STAGE 3 — ENTRY MANAGEMENT
# ============================================================
def s3_p1_ik_vwap(data):
    if not CFG["30_IK_VWAP_ENABLE"]:
        return False
    result = data["close"] > data["vwap"]
    if result:
        log(3, 30, "IK_VWAP", "EXIT")
    return result

def s3_p2_ik_ema(data):
    if not CFG["31_IK_EMA_ENABLE"]:
        return False
    ema_fast = data["ema_values"].get(CFG["41_IK_EMA_FAST"])
    ema_slow = data["ema_values"].get(CFG["42_IK_EMA_SLOW"])
    if ema_fast is None or ema_slow is None:
        return False
    result = ema_fast > ema_slow
    if result:
        log(3, 31, "IK_EMA", "EXIT")
    return result

def s3_p3_ik_volume(data, ctx):
    if not CFG["32_IK_VOLUME_ENABLE"]:
        return False
    if ctx.entry_volume_ref is None:
        return False
    result = data["volume"] > ctx.entry_volume_ref and data["close"] >= data["open"]
    if result:
        log(3, 32, "IK_VOLUME", "EXIT")
    return result

def s3_p4_big_ema(data, ctx):
    if not CFG["33_BIG_EMA_ENABLE"]:
        return False
    
    if ctx.big_ema_state == "NONE":
        ctx.big_ema_state = "PENDING"
        ctx.big_ema_entry_bar_ts = data["bar_ts"]
        ctx.big_ema_bar_count = 0
    
    if ctx.big_ema_entry_bar_ts != data["bar_ts"]:
        ctx.big_ema_bar_count += 1
        ctx.big_ema_entry_bar_ts = data["bar_ts"]
    
    if ctx.big_ema_state == "PENDING":
        ema_fast = data["ema_values"].get(CFG["43_BIG_EMA_FAST"])
        ema_slow = data["ema_values"].get(CFG["44_BIG_EMA_SLOW"])
        if ema_fast is None or ema_slow is None:
            return False
        
        ema_distance = abs(ema_fast - ema_slow)
        ema_expanding = ema_distance > 0.0001
        
        if ema_expanding:
            ctx.big_ema_state = "PASS"
            return False
        
        if ctx.big_ema_bar_count > CFG["33_GRACE_BARS"]:
            ctx.big_ema_state = "FAIL"
            log(3, 33, "BIG_EMA", "EXIT")
            return True
    
    return False

def s3_p5_big_donchian(data):
    if not CFG["34_BIG_DONCHIAN_ENABLE"]:
        return False
    result = data["low"] < data["donchian_low"]
    if result:
        log(3, 34, "BIG_DONCHIAN", "EXIT")
    return result

def s3_p6_small_pullback():
    if not CFG["35_SMALL_PULLBACK_ENABLE"]:
        return False
    return False

def s3_p7_small_vol():
    if not CFG["36_SMALL_VOL_ENABLE"]:
        return False
    return False

def s3_p8_small_escgo():
    if not CFG["37_SMALL_ESCGO_ENABLE"]:
        return False
    return False

def stage3_management(data, ctx):
    if s3_p1_ik_vwap(data):
        return True
    if s3_p2_ik_ema(data):
        return True
    if s3_p3_ik_volume(data, ctx):
        return True
    if s3_p4_big_ema(data, ctx):
        return True
    if s3_p5_big_donchian(data):
        return True
    if s3_p6_small_pullback():
        return True
    if s3_p7_small_vol():
        return True
    if s3_p8_small_escgo():
        return True
    return False

# ============================================================
# STAGE 4 — EXIT GATE
# ============================================================
def s4_p1_sl(data, ctx):
    if ctx.entry_price is None:
        return False
    loss_pct = (data["close"] - ctx.entry_price) / ctx.entry_price * 100
    result = loss_pct > CFG["50_SL_PCT"]
    if result:
        log(4, 50, "SL", "EXIT")
    return result

def s4_p2_ema_slope(data):
    slope_ema_period = CFG["54_SLOPE_EMA"]
    lookback = CFG["54_SLOPE_LOOKBACK"]
    
    if len(data["slope_ema_hist"]) < lookback + 1:
        return False
    
    current_ema = data["slope_ema_hist"][-1]
    prev_ema = data["slope_ema_hist"][-lookback - 1]
    
    if current_ema is None or prev_ema is None:
        return False
    
    slope_pct = (current_ema - prev_ema) / prev_ema * 100
    result = slope_pct > CFG["54_SLOPE_EXIT_PCT"]
    if result:
        log(4, 54, "EMA_SLOPE", "EXIT")
    return result

def s4_p3_ema_cross(data):
    ema_fast = data["ema_values"].get(CFG["56_EXIT_EMA_FAST"])
    ema_slow = data["ema_values"].get(CFG["56_EXIT_EMA_SLOW"])
    
    if ema_fast is None or ema_slow is None:
        return False
    
    result = ema_fast > ema_slow
    if result:
        log(4, 56, "EMA_CROSS", "EXIT")
    return result

def stage4_exit(data, ctx):
    if s4_p1_sl(data, ctx):
        return True
    if s4_p2_ema_slope(data):
        return True
    if s4_p3_ema_cross(data):
        return True
    return False

# ============================================================
# ENGINE — 완료봉 기준 단일 실행
# ============================================================
def emergency_close(fx, ctx):
    if ctx.position is not None and ctx.position_qty > 0:
        print(f"EMERGENCY CLOSE: Closing {ctx.position_qty} {ctx.position}")
        fx.order("BUY", ctx.position_qty, reduce_only=True)
        print("Position closed")

def run():
    if not CFG["ENGINE_ENABLE"]:
        print("ENGINE DISABLED")
        return
    
    client = init_binance_client()
    setup_futures_env(client, CFG["TRADE_SYMBOL"], CFG["LEVERAGE"])
    fx = FX(client, CFG["TRADE_SYMBOL"])
    ctx = Context()
    
    def signal_handler(sig, frame):
        print(f"\nReceived signal {sig}, shutting down...")
        emergency_close(fx, ctx)
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    last_bar_ts = None
    
    print("ENGINE START")
    
    try:
        while True:
            data = collect_data(CFG["TRADE_SYMBOL"])
            
            if data is None:
                time.sleep(3)
                continue
            
            if data["bar_ts"] == last_bar_ts:
                time.sleep(3)
                continue
            
            last_bar_ts = data["bar_ts"]
            
            if ctx.position is None:
                s1_pass = stage1_preentry(data)
                if s1_pass:
                    s2_pass = stage2_entry(data)
                    if s2_pass:
                        qty = fx.order("SELL", (CFG["CAPITAL_USDT"] * 0.95) / data["close"], price=data["close"], reduce_only=False)
                        if qty > 0:
                            ctx.position = "SHORT"
                            ctx.entry_price = data["close"]
                            ctx.entry_volume_ref = data["volume"]
                            ctx.position_qty = qty
                            ctx.entry_bar_ts = data["bar_ts"]
                            ctx.big_ema_state = "NONE"
                            ctx.big_ema_entry_bar_ts = None
                            ctx.big_ema_bar_count = 0
                            print(f"[ENTRY] SHORT price={q(data['close'])} qty={qty}")
            else:
                if data["bar_ts"] == ctx.entry_bar_ts:
                    time.sleep(3)
                    continue
                
                s3_kill = stage3_management(data, ctx)
                if s3_kill:
                    fx.order("BUY", ctx.position_qty, reduce_only=True)
                    print(f"[EXIT] S3 price={q(data['close'])}")
                    ctx.__init__()
                else:
                    s4_kill = stage4_exit(data, ctx)
                    if s4_kill:
                        fx.order("BUY", ctx.position_qty, reduce_only=True)
                        print(f"[EXIT] S4 price={q(data['close'])}")
                        ctx.__init__()
            
            time.sleep(3)
    
    except KeyboardInterrupt:
        print("\nKeyboard interrupt detected")
        emergency_close(fx, ctx)
    except Exception as e:
        print(f"Unexpected error: {e}")
        emergency_close(fx, ctx)
        raise

if __name__ == "__main__":
    run()