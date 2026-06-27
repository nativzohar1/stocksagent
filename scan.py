#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan.py  —  Decoupling Hunter / Institutional Swing Scanner v2.0 (FINAL, QA-fixed)
Runs on GitHub Actions (has internet). Writes results/out.json.
Spec: FROZEN PRD v2.0.

Python OWNS (hard quant):
  CORE (AND, NO_GO):  Regime>200SMA | Volatility>=40% | Concrete floor+EMA21 breakout | RS on red day | Hard stop
  SCORE (rank only):  PEG<1.8 | FCF positive+growing | Catalyst (earnings 15-45d)
                      -> SKIP = data MISSING (AI heals) ; NO_GO = data PRESENT but bad (no rank point, never rejects)
AI OWNS (NEEDS_LLM):  Rule-of-40/RPO decoupling | Insider Form4 | Disruption | Devil's Advocate | Data-Healing
"""

import json, time, math, datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ----------------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------------
OUT_PATH         = Path("results/out.json")
HISTORY_PERIOD   = "1y"          # daily bars for technicals
EXCLUDED_SECTORS = {"Consumer Defensive", "Utilities"}   # XLP / XLU
PEG_MAX          = 1.8
VOL_MIN          = 0.40          # 40% annual range
CATALYST_MIN_D   = 15
CATALYST_MAX_D   = 45
STOP_MULT        = 0.985         # 10-day low * 0.985
RS_TOLERANCE     = -0.003        # fell < 0.3% (or green) on QQQ's worst red day
FIB_TOL          = 0.03          # within 3% of a fib level
SUPPORT_TOL      = 0.025         # horizontal support clustering tolerance
VOL_CLIMAX_MULT  = 2.0           # >= 2x avg volume
EMA_FAST         = 21

# Regime ETF proxies
ETF_SOFTWARE = "IGV"
ETF_SEMIS    = "SOXX"
ETF_DEFAULT  = "QQQ"

# Known mega-caps the live Wikipedia article silently drops -> sentinel backfill
SENTINELS = ["NOW"]

# Hardcoded NDX-100 fallback (used ONLY if live fetch fails) — keep reasonably current
NDX_FALLBACK = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","GOOG","META","AVGO","TSLA","COST",
    "NFLX","AMD","PEP","ADBE","LIN","CSCO","TMUS","INTU","TXN","QCOM",
    "AMGN","ISRG","CMCSA","AMAT","HON","BKNG","VRTX","PANW","ADP","GILD",
    "SBUX","ADI","MU","REGN","LRCX","MDLZ","KLAC","SNPS","CDNS","PYPL",
    "MELI","CRWD","MAR","CTAS","ORLY","ASML","ABNB","CEG","WDAY","NXPI",
    "ROP","MNST","CSX","FTNT","ADSK","PCAR","AEP","DASH","CHTR","PAYX",
    "TTD","ODFL","KDP","ROST","FANG","EA","CPRT","BKR","FAST","VRSK",
    "GEHC","CTSH","EXC","XEL","KHC","IDXX","CCEP","TEAM","MCHP","ON",
    "DXCM","ANSS","CSGP","ZS","DDOG","BIIB","ARM","WBD","ILMN","GFS",
    "MRVL","TTWO","WBA","MDB","SMCI","LULU","PDD","DLTR","SIRI","ENPH",
    "NOW","HON"
]

# ----------------------------------------------------------------------------------
# UNIVERSE
# ----------------------------------------------------------------------------------
def fetch_universe():
    """Returns (tickers:list, source:str). Live Wikipedia MediaWiki API + sentinel backfill,
    else hardcoded fallback."""
    try:
        url = "https://en.wikipedia.org/w/api.php"
        params = {"action": "parse", "page": "Nasdaq-100", "prop": "wikitext", "format": "json"}
        r = requests.get(url, params=params, timeout=20,
                         headers={"User-Agent": "stocksagent/2.0"})
        r.raise_for_status()
        wt = r.json()["parse"]["wikitext"]["*"]

        import re
        cand = set(re.findall(r"\b([A-Z]{1,5})\b", wt))
        tickers = sorted({t for t in cand if 1 <= len(t) <= 5})
        noise = {"THE","AND","INC","CORP","ETF","USD","NYSE","SEC","CEO","API","USA","ID","UTC","ISO"}
        tickers = [t for t in tickers if t not in noise]
        if not (80 <= len(tickers) <= 130):
            raise ValueError(f"implausible parse count={len(tickers)}")

        src = f"live: Wikipedia MediaWiki API ({len(tickers)} tickers)"
        added = [s for s in SENTINELS if s not in tickers]
        if added:
            tickers += added
            src += f" + sentinel backfill ({','.join(added)})"
        return sorted(set(tickers)), src
    except Exception as e:
        return list(dict.fromkeys(NDX_FALLBACK)), \
               f"FALLBACK: NDX_FALLBACK hardcoded list ({len(set(NDX_FALLBACK))} tickers) — live fetch failed ({e})"

# ----------------------------------------------------------------------------------
# TECHNICAL HELPERS
# ----------------------------------------------------------------------------------
def rsi(series, period=14):
    delta = series.diff()
    up = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def gate(stage, name, status, detail, value=None, criterion=""):
    return {"stage": stage, "name": name, "status": status,
            "detail": detail, "value": value, "criterion": criterion}

# ----------------------------------------------------------------------------------
# CORE GATES (AND, NO_GO)
# ----------------------------------------------------------------------------------
def classify_regime_etf(info):
    sector = (info.get("sector") or "").lower()
    industry = (info.get("industry") or "").lower()
    if "semiconductor" in industry or "semiconductor" in sector:
        return ETF_SEMIS
    if "software" in industry or "technology" in sector or "communication" in sector:
        return ETF_SOFTWARE
    return ETF_DEFAULT

def gate_regime(etf_symbol, etf_cache):
    if etf_symbol not in etf_cache:
        try:
            h = yf.Ticker(etf_symbol).history(period="2y")["Close"].dropna()
            etf_cache[etf_symbol] = h
        except Exception:
            etf_cache[etf_symbol] = pd.Series(dtype=float)
    h = etf_cache[etf_symbol]
    if h is None or len(h) < 200:
        return gate(1, "Sector trend (Regime)", "SKIP",
                    f"{etf_symbol} history unavailable", None, "ETF > 200SMA")
    sma200 = h.rolling(200).mean().iloc[-1]
    last = h.iloc[-1]
    ok = last > sma200
    return gate(1, "Sector trend (Regime)", "GO" if ok else "NO_GO",
                f"{etf_symbol} {last:.2f} {'>' if ok else '<'} 200SMA {sma200:.2f}",
                round(float(last), 2), "ETF > 200SMA")

def gate_volatility(df):
    hi = df["High"].max(); lo = df["Low"].min()
    if lo <= 0 or math.isnan(lo):
        return gate(1, "Volatility (Upside DNA)", "SKIP", "no valid 52w low", None, "(H-L)/L >= 40%")
    rng = (hi - lo) / lo
    ok = rng >= VOL_MIN
    return gate(1, "Volatility (Upside DNA)", "GO" if ok else "NO_GO",
                f"52w range {rng*100:.1f}%", round(float(rng), 4), "(52wH-52wL)/52wL >= 40%")

def detect_floor(df):
    """Returns (is_on_floor:bool, reason:str). Fib 0.5/0.618 OR horizontal support (2+ touches)."""
    close = df["Close"]; low = df["Low"]
    recent = close.iloc[-1]
    win = df.iloc[-126:] if len(df) >= 126 else df
    swing_hi = win["High"].max(); swing_lo = win["Low"].min()
    rng = swing_hi - swing_lo
    fib_hits = []
    if rng > 0:
        for r, label in [(0.5, "0.5"), (0.618, "0.618")]:
            level = swing_hi - r * rng
            if abs(recent - level) / recent <= FIB_TOL:
                fib_hits.append(f"Fib {label}@{level:.2f}")
    lows = low.iloc[-126:] if len(low) >= 126 else low
    troughs = lows[(lows.shift(1) > lows) & (lows.shift(-1) > lows)].dropna()
    touches = [t for t in troughs if abs(t - recent) / recent <= SUPPORT_TOL]
    horiz = len(touches) >= 2
    reasons = []
    if fib_hits: reasons.append(" / ".join(fib_hits))
    if horiz:    reasons.append(f"horizontal support ({len(touches)} touches)")
    return (bool(reasons), "; ".join(reasons) if reasons else "no floor near price")

def detect_rsi_divergence(df):
    r = rsi(df["Close"])
    low = df["Low"]
    seg = df.iloc[-60:]
    if len(seg) < 20: return False
    lows = seg["Low"]
    troughs_idx = lows[(lows.shift(1) > lows) & (lows.shift(-1) > lows)].dropna().index
    if len(troughs_idx) < 2: return False
    t1, t2 = troughs_idx[-2], troughs_idx[-1]
    price_ll = low.loc[t2] < low.loc[t1]
    rsi_hl = r.loc[t2] > r.loc[t1]
    return bool(price_ll and rsi_hl)

def detect_volume_climax(df):
    vol = df["Volume"]
    if len(vol) < 50: return (False, None)
    avg = vol.iloc[-50:].mean()
    recent_max = vol.iloc[-5:].max()
    mult = recent_max / avg if avg > 0 else 0
    return (mult >= VOL_CLIMAX_MULT, round(float(mult), 2))

def gate_concrete_floor(df):
    close = df["Close"]
    on_floor, floor_reason = detect_floor(df)
    rsi_div = detect_rsi_divergence(df)
    vclimax, vmult = detect_volume_climax(df)
    confirm = rsi_div or vclimax
    ema21 = ema(close, EMA_FAST)
    broke_out = close.iloc[-1] > ema21.iloc[-1]
    bounce = close.iloc[-1] > df["Low"].iloc[-10:].min()
    ok = on_floor and bounce and confirm and broke_out
    conf_txt = []
    if rsi_div: conf_txt.append("RSI-Div")
    if vclimax: conf_txt.append(f"Vol climax {vmult}x")
    detail = (f"floor[{floor_reason}] | bounce={bounce} | "
              f"confirm[{','.join(conf_txt) or 'none'}] | "
              f"close {close.iloc[-1]:.2f} {'>' if broke_out else '<'} EMA21 {ema21.iloc[-1]:.2f}")
    main = gate(3, "Concrete floor + EMA21 breakout", "GO" if ok else "NO_GO",
                detail, None,
                "on floor (Fib .5/.618 OR 2x support) + bounce + (RSI-Div OR Vol climax) + close>EMA21")
    sub = [
        gate(3, "Bullish RSI divergence", "GO" if rsi_div else "SKIP",
             "informational sub-signal", bool(rsi_div), "price LL & RSI HL"),
        gate(3, "Volume climax", "GO" if vclimax else "SKIP",
             "informational sub-signal", vmult, ">= 2x avg volume"),
    ]
    return main, sub

def gate_relative_strength(df, qqq_close):
    if qqq_close is None or len(qqq_close) < 30:
        return gate(4, "Relative Strength", "SKIP", "QQQ history unavailable", None,
                    "on QQQ worst red day: stock fell <0.3% or green")
    qret = qqq_close.pct_change()
    worst_day = qret.iloc[-30:].idxmin()
    if worst_day not in df.index:
        common = df["Close"].reindex(qqq_close.index).dropna()
        sret = common.pct_change()
        if worst_day not in sret.index:
            return gate(4, "Relative Strength", "SKIP", "no aligned red-day bar", None,
                        "on QQQ worst red day: stock fell <0.3% or green")
        stock_move = sret.loc[worst_day]
    else:
        stock_move = df["Close"].pct_change().loc[worst_day]
    qqq_move = qret.loc[worst_day]
    ok = stock_move >= RS_TOLERANCE
    return gate(4, "Relative Strength", "GO" if ok else "NO_GO",
                f"QQQ red day {worst_day.date()} {qqq_move*100:.2f}% -> stock {stock_move*100:.2f}%",
                round(float(stock_move), 4), "stock fell <0.3% or green on QQQ worst red day")

def gate_hard_stop(df):
    low10 = df["Low"].iloc[-10:].min()
    stop = round(float(low10) * STOP_MULT, 2)
    return gate(5, "Hard stop price", "GO",
                f"10d low {low10:.2f} x {STOP_MULT}", stop, "10d-low * 0.985")

# ----------------------------------------------------------------------------------
# SCORE GATES (rank only) — SKIP = data MISSING (AI heals) ; NO_GO = present but bad
# ----------------------------------------------------------------------------------
def gate_peg(info):
    peg = info.get("trailingPegRatio") or info.get("pegRatio")
    if peg is None or (isinstance(peg, float) and math.isnan(peg)) or peg == 0:
        return gate(2, "PEG", "SKIP", "PEG unavailable (yfinance null) -> AI heals", None, "PEG < 1.8")
    ok = peg < PEG_MAX
    return gate(2, "PEG", "GO" if ok else "NO_GO",
                f"PEG {peg:.2f} ({'<' if ok else '>='} {PEG_MAX})",
                round(float(peg), 2), "PEG < 1.8")

def gate_fcf(tk):
    try:
        cf = tk.cashflow
        if cf is None or cf.empty:
            return gate(2, "FCF", "SKIP", "cashflow unavailable -> AI heals", None, "FCF positive & growing YoY")
        idx = {str(i): i for i in cf.index}
        fcf_row = None
        for key in ["Free Cash Flow"]:
            if key in idx:
                fcf_row = cf.loc[idx[key]]
                break
        if fcf_row is None and "Operating Cash Flow" in idx and "Capital Expenditure" in idx:
            fcf_row = cf.loc[idx["Operating Cash Flow"]] + cf.loc[idx["Capital Expenditure"]]
        if fcf_row is None:
            return gate(2, "FCF", "SKIP", "FCF rows missing -> AI heals", None, "FCF positive & growing YoY")
        vals = fcf_row.dropna().values
        if len(vals) < 2:
            return gate(2, "FCF", "SKIP", "insufficient FCF history -> AI heals", None, "FCF positive & growing YoY")
        latest, prior = float(vals[0]), float(vals[1])
        ok = latest > 0 and latest > prior
        return gate(2, "FCF", "GO" if ok else "NO_GO",
                    f"FCF {latest/1e9:.2f}B vs prior {prior/1e9:.2f}B"
                    f" ({'positive & growing' if ok else 'negative or shrinking'})",
                    round(latest, 0), "FCF positive & growing YoY")
    except Exception as e:
        return gate(2, "FCF", "SKIP", f"FCF error ({e}) -> AI heals", None, "FCF positive & growing YoY")

def gate_catalyst(tk):
    try:
        cal = tk.calendar
        ed = None
        if isinstance(cal, dict):
            ev = cal.get("Earnings Date")
            if isinstance(ev, (list, tuple)) and ev: ed = ev[0]
            elif ev: ed = ev
        elif cal is not None and hasattr(cal, "loc") and "Earnings Date" in getattr(cal, "index", []):
            ed = cal.loc["Earnings Date"][0]
        if ed is None:
            return gate(2, "Next earnings (Catalyst)", "SKIP", "earnings date unknown", None,
                        "earnings in 15-45 days")
        if isinstance(ed, dt.datetime):
            ed_date = ed.date()
        elif isinstance(ed, dt.date):
            ed_date = ed
        else:
            ed_date = pd.to_datetime(ed).date()
        days = (ed_date - dt.date.today()).days
        ok = CATALYST_MIN_D <= days <= CATALYST_MAX_D
        return gate(2, "Next earnings (Catalyst)", "GO" if ok else "NO_GO",
                    f"earnings {ed_date} (in {days}d, window {CATALYST_MIN_D}-{CATALYST_MAX_D})",
                    days, "earnings in 15-45 days")
    except Exception as e:
        return gate(2, "Next earnings (Catalyst)", "SKIP", f"calendar error ({e})", None,
                    "earnings in 15-45 days")

# ----------------------------------------------------------------------------------
# AI GATES (NEEDS_LLM — Python does NOT resolve these)
# ----------------------------------------------------------------------------------
def ai_gates():
    return [
        gate(2, "Rule-of-40 / RPO decoupling", "NEEDS_LLM",
             "AI: rev growth + margin > 40%? RPO growing YoY? document price/perf decoupling (cite 10-Q/10-K)",
             None, "Rule of 40 true AND RPO up YoY"),
        gate(3, "Insider buying (Form 4)", "NEEDS_LLM",
             "AI: search FRESH Form 4 insider BUYS only (NOT 13F)", None,
             "recent insider buying = confirming signal"),
        gate(1, "Disruption test", "NEEDS_LLM",
             "AI: direct threat=STRONG SELL | adapting w/ AI=BUY⚠️ | infra/data moat=clean",
             None, "no direct 5y replacement threat"),
        gate(2, "Devil's advocate", "NEEDS_LLM",
             "AI MUST write 2 concrete, evidenced bear reasons. NO OUTPUT without it.",
             None, "2 concrete crash reasons required"),
        gate(2, "Data healing (PEG/FCF)", "NEEDS_LLM",
             "AI: if survivor has SKIP on PEG/FCF, web-fetch real value. Fwd PEG; Yahoo>SeekingAlpha>StockAnalysis; "
             "tag '(AI-healed, source, date)'; never guess; survivors only.",
             None, "heal SKIP PEG/FCF with cited value"),
    ]

# ----------------------------------------------------------------------------------
# PER-TICKER PIPELINE
# ----------------------------------------------------------------------------------
def scan_ticker(symbol, etf_cache, qqq_close, universe_source):
    item = {"ticker": symbol, "price": None, "verdict": "NO_GO",
            "go_count": 0, "blocked_at": None,
            "universe_source": universe_source, "gates": []}
    try:
        tk = yf.Ticker(symbol)
        df = tk.history(period=HISTORY_PERIOD).dropna()
        if df.empty or len(df) < 200:
            item["blocked_at"] = "data: insufficient history"
            item["gates"].append(gate(0, "Data availability", "NO_GO",
                                       f"only {len(df)} bars", len(df), ">=200 daily bars"))
            return item
        info = {}
        try: info = tk.info or {}
        except Exception: info = {}
        item["price"] = round(float(df["Close"].iloc[-1]), 2)

        gates = []

        # ----- Sector exclusion (hard) -----
        sector = info.get("sector")
        if sector in EXCLUDED_SECTORS:
            gates.append(gate(1, "Sector exclusion", "NO_GO",
                              f"{sector} (XLP/XLU excluded)", sector, "not Consumer Defensive / Utilities"))
            item["gates"] = gates
            item["blocked_at"] = f"Sector exclusion ({sector})"
            return item
        gates.append(gate(1, "Sector exclusion", "GO",
                          f"{sector or 'unknown'}", sector, "not Consumer Defensive / Utilities"))

        # ----- CORE (AND, NO_GO) -----
        etf = classify_regime_etf(info)
        g_regime = gate_regime(etf, etf_cache)
        g_vol    = gate_volatility(df)
        g_floor, g_subs = gate_concrete_floor(df)
        g_rs     = gate_relative_strength(df, qqq_close)
        g_stop   = gate_hard_stop(df)

        # ----- SCORE (rank only) -----
        g_peg = gate_peg(info)
        g_fcf = gate_fcf(tk)
        g_cat = gate_catalyst(tk)

        gates += [g_regime, g_vol, g_floor] + g_subs + [g_rs, g_peg, g_fcf, g_cat, g_stop]
        gates += ai_gates()
        item["gates"] = gates

        # ----- Verdict: any CORE NO_GO => rejected. SCORE NO_GO never rejects -----
        core = [g_regime, g_vol, g_floor, g_rs]   # g_stop always GO; SCORE gates intentionally excluded
        blocker = next((g for g in core if g["status"] == "NO_GO"), None)
        if blocker:
            item["verdict"] = "NO_GO"
            item["blocked_at"] = blocker["name"]
        else:
            item["verdict"] = "GO_PENDING_THESIS"
            item["blocked_at"] = None

        item["go_count"] = sum(1 for g in gates if g["status"] == "GO")
        return item
    except Exception as e:
        item["blocked_at"] = f"error: {e}"
        item["gates"].append(gate(0, "Pipeline error", "NO_GO", str(e), None, ""))
        return item

# ----------------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------------
def main():
    tickers, universe_source = fetch_universe()
    print(f"UNIVERSE {{count: {len(tickers)}, source: {universe_source}}}")

    etf_cache = {}
    try:
        qqq_close = yf.Ticker("QQQ").history(period="6mo")["Close"].dropna()
    except Exception:
        qqq_close = None

    results = []
    for i, sym in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {sym}")
        results.append(scan_ticker(sym, etf_cache, qqq_close, universe_source))
        time.sleep(0.4)

    results.sort(key=lambda x: (x["verdict"] != "GO_PENDING_THESIS", -x["go_count"]))
    survivors = [r["ticker"] for r in results if r["verdict"] == "GO_PENDING_THESIS"]
    print(f"SURVIVORS {survivors}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"WROTE {OUT_PATH} ({len(results)} items, {len(survivors)} survivors)")

if __name__ == "__main__":
    main()
