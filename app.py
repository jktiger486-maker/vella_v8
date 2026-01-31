# ============================================================
# VELLA V8 — app_min.py (PROFIT CUT / AWS READY / TRAILING FIX)
# ============================================================

import os, time, requests
from decimal import Decimal, ROUND_DOWN

# ---------------- CFG ----------------
CFG = {
    "01_TRADE_SYMBOL": "RONINUSDT",
    "02_CAPITAL_BASE_USDT": 30,
    "03_CAPITAL_USE_FIXED": True,
    "04_CAPITAL_MAX_LOSS_PCT": 100.0,

    "05_ENGINE_ENABLE": True,
    "06_ENTRY_CANDIDATE_ENABLE": True,
    "07_ENTRY_EXEC_ENABLE": True,

    "31_LOG_CANDIDATES": True,
    "32_LOG_EXECUTIONS": True,

    "35_SL_PCT": 0.60,
    "36_TP_PCT": 0.70,
    "37_TRAILING_PCT": 0.30,
}

# ---------------- STATE ----------------
def init_state():
    return {
        "bars": 0,
        "_last_bar_time": None,

        "has_candidate": False,
        "candidates": [],
        "last_candidate_bar": None,

        "entry_ready": False,
        "entry_bar": None,

        "position": None,
        "position_open_bar": None,
        "position_qty": None,

        "capital_usdt": None,
        "initial_equity": None,
        "equity": None,
        "realized_pnl": 0.0,

        "entry_price": None,
        "sl_price": None,
        "tp_price": None,

        "tp_touched": False,
        "trailing_active": False,
        "trailing_anchor": None,
        "trailing_stop": None,

        "exit_ready": False,
        "exit_reason": None,
    }

def q(x, p=6):
    return float(Decimal(str(x)).quantize(Decimal("1."+"0"*p), rounding=ROUND_DOWN))

# ---------------- BINANCE ----------------
try:
    from binance.client import Client
    from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
except Exception:
    Client=None
    SIDE_BUY="BUY"; SIDE_SELL="SELL"; ORDER_TYPE_MARKET="MARKET"

def init_binance_client():
    if Client is None:
        raise RuntimeError("python-binance missing")
    k=os.getenv("BINANCE_API_KEY")
    s=os.getenv("BINANCE_API_SECRET")
    if not k or not s:
        raise RuntimeError("API KEY missing")
    return Client(k,s)

class FX:
    def __init__(self, client):
        self.client = client
        self._load()

    def _load(self):
        info = self.client.futures_exchange_info()
        sym = next(x for x in info["symbols"] if x["symbol"] == CFG["01_TRADE_SYMBOL"])
        lot = next(f for f in sym["filters"] if f["filterType"] == "LOT_SIZE")
        self.step = Decimal(lot["stepSize"])
        self.minq = Decimal(lot["minQty"])

    def _norm(self, qty):
        qd = (Decimal(str(qty)) / self.step).to_integral_value(rounding=ROUND_DOWN) * self.step
        if qd < self.minq:
            raise RuntimeError("QTY_TOO_SMALL")
        d = len(str(self.step).split(".")[1].rstrip("0"))
        return f"{qd:.{d}f}"

    def order(self, side, qty):
        qs = self._norm(qty)
        self.client.futures_create_order(
            symbol=CFG["01_TRADE_SYMBOL"],
            side=SIDE_SELL if side=="SELL" else SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=qs
        )
        return float(qs)

# ---------------- MARKET (REST) ----------------
BINANCE_SPOT="https://api.binance.com/api/v3/klines"
EMA9_PERIOD=9
KLINE_INTERVAL="5m"

_rest = {
    "ema9_series": [],
    "closes": []
}

def fetch_klines(symbol, interval, limit=100):
    r = requests.get(
        BINANCE_SPOT,
        params={"symbol":symbol,"interval":interval,"limit":limit},
        timeout=5
    )
    r.raise_for_status()
    return r.json()

def poll_rest_kline(symbol, logger=print):
    kl = fetch_klines(symbol, KLINE_INTERVAL, limit=EMA9_PERIOD+5)
    k = kl[-2]

    t = int(k[6])
    o = float(k[1])
    h = float(k[2])
    l = float(k[3])
    c = float(k[4])

    series = _rest["ema9_series"]
    ema = c if not series else (c*(2/(EMA9_PERIOD+1)) + series[-1]*(1-2/(EMA9_PERIOD+1)))
    series.append(ema)
    series[:] = series[-50:]

    _rest["closes"].append(c)
    _rest["closes"] = _rest["closes"][-50:]

    logger(f"REST_CLOSE t={t} close={c} ema9={q(ema)}")
    return {"time":t,"open":o,"high":h,"low":l,"close":c,"ema9":ema}

# ---------------- STEPS ----------------
def step_1(cfg, state):
    if state["initial_equity"] is None:
        cap=float(cfg["02_CAPITAL_BASE_USDT"])
        state["capital_usdt"]=cap
        state["initial_equity"]=cap
        state["equity"]=cap

def step_3(cfg, mkt, state, logger):
    if not cfg["06_ENTRY_CANDIDATE_ENABLE"]:
        return
    if mkt["low"] < mkt["ema9"] and state["last_candidate_bar"] != state["bars"]:
        state["has_candidate"]=True
        state["last_candidate_bar"]=state["bars"]
        state["candidates"]=[{"bar":state["bars"],"price":mkt["low"]}]
        logger(f"CANDIDATE bar={state['bars']}")

def step_6_entry_judge(state):
    if state["position"] is not None:
        return
    if not state["has_candidate"]:
        return
    state["entry_ready"]=True
    state["entry_bar"]=state["bars"]

def step_13_entry(cfg, mkt, state, fx, logger):
    if not state["entry_ready"]:
        return
    if state["bars"] != state["entry_bar"]:
        state["entry_ready"]=False
        return

    price = mkt["close"]
    qty = fx.order(
        "SELL",
        (state["capital_usdt"] * 0.95) / price
    ) if cfg["07_ENTRY_EXEC_ENABLE"] else 1.0

    state["position"]="OPEN"
    state["position_open_bar"]=state["bars"]
    state["entry_price"]=price
    state["position_qty"]=qty
    state["entry_ready"]=False

    logger(f"ENTRY bar={state['bars']} price={price} qty={qty}")

def step_14_exit_calc(cfg, state, mkt):
    if state["position"]!="OPEN":
        return

    e = state["entry_price"]

    if state["sl_price"] is None:
        state["sl_price"]=q(e*(1+cfg["35_SL_PCT"]/100))
        state["tp_price"]=q(e*(1-cfg["36_TP_PCT"]/100))

    low = mkt["low"]
    anchor = state["trailing_anchor"] if state["trailing_anchor"] is not None else e
    anchor = min(anchor, low)

    state["trailing_anchor"]=q(anchor)
    state["trailing_stop"]=q(anchor*(1+cfg["37_TRAILING_PCT"]/100))

def step_15_exit_judge(state, mkt):
    if state["position"]!="OPEN":
        return False
    if state["bars"]<=state["position_open_bar"]:
        return False

    price=mkt["close"]

    # REAL TRAILING STOP
    if state["trailing_active"] and price >= state["trailing_stop"]:
        state["exit_ready"]=True
        state["exit_reason"]="TRAIL_STOP"
        return True

    if price >= state["sl_price"]:
        state["exit_ready"]=True
        state["exit_reason"]="SL"
        return True

    if price <= state["tp_price"]:
        state["tp_touched"]=True
        state["trailing_active"]=True

    closes=_rest["closes"]
    if len(closes)>=2:
        avg2=(closes[-1]+closes[-2])/2
        if price>avg2:
            state["exit_ready"]=True
            state["exit_reason"]="TRAIL" if state["trailing_active"] else "BASE"
            return True

    return False

def step_16_exit_exec(cfg, state, mkt, client, logger):
    if not state["exit_ready"]:
        return

    if cfg["07_ENTRY_EXEC_ENABLE"]:
        client.futures_create_order(
            symbol=CFG["01_TRADE_SYMBOL"],
            side=SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=abs(state["position_qty"]),
            reduceOnly=True
        )

    exit_price=mkt["close"]
    pnl=(state["entry_price"]-exit_price)/state["entry_price"]*state["capital_usdt"]

    state["equity"]+=pnl
    state["realized_pnl"]+=pnl

    logger(f"EXIT {state['exit_reason']} pnl={q(pnl,4)} eq={q(state['equity'],4)}")

    # FULL RESET
    state.update({
        "has_candidate": False,
        "candidates": [],
        "last_candidate_bar": None,
        "entry_ready": False,
        "entry_bar": None,
        "position": None,
        "position_open_bar": None,
        "position_qty": None,
        "entry_price": None,
        "sl_price": None,
        "tp_price": None,
        "tp_touched": False,
        "trailing_active": False,
        "trailing_anchor": None,
        "trailing_stop": None,
        "exit_ready": False,
        "exit_reason": None,
    })

# ---------------- RUN ----------------
def app_run_live(logger=print):
    client=init_binance_client()
    fx=FX(client)
    state=init_state()

    logger("LIVE_START MIN")

    while True:
        mkt=poll_rest_kline(CFG["01_TRADE_SYMBOL"], logger)

        if state["_last_bar_time"]!=mkt["time"]:
            state["_last_bar_time"]=mkt["time"]
            state["bars"]+=1

        step_1(CFG, state)
        step_3(CFG, mkt, state, logger)
        step_6_entry_judge(state)
        step_13_entry(CFG, mkt, state, fx, logger)
        step_14_exit_calc(CFG, state, mkt)
        step_15_exit_judge(state, mkt)
        step_16_exit_exec(CFG, state, mkt, client, logger)

        time.sleep(5)

if __name__=="__main__":
    app_run_live(print)
