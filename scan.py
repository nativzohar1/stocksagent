#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan.py  —  Decoupling Hunter / Institutional Swing Scanner v2.7 (Mega-Cap Monster Radar)
Runs on GitHub Actions (has internet). Writes results/out.json.

UNIVERSE: S&P 100 (OEX) — the ~101 largest, most established US companies, EXCHANGE-AGNOSTIC
(includes NYSE names like ORCL/CRM and Nasdaq names like NOW/PLTR). SECTOR-AGNOSTIC.
Plus a SENTINELS force-include list (VST, CEG, PLTR, CRWD, DDOG, TASE.TA). The ONLY screen
keeping slow blue-chips (banks/staples) out is Volatility >= 40%.

Python OWNS (hard quant):
  CORE (AND, NO_GO):  Volatility>=40% | Concrete floor+EMA21 | RS on red day | Hard stop
  SCORE (rank only):  Regime>200SMA | PEG<1.8 | FCF positive+growing | Catalyst (earnings 15-45d)
                      -> SKIP = data MISSING (AI heals) ; NO_GO = data PRESENT but bad (no rank point, never rejects)
AI OWNS (NEEDS_LLM):  Rule-of-40/RPO decoupling | Insider Form4 | Disruption | Devil's Advocate | Data-Healing

v2.4: Regime is rank-only (never rejects) — catch monsters that decoupled from a weak sector.
v2.5: CURE 'LATENCY BLINDNESS'. Patch the last daily bar with the live fast_info price BEFORE
      any price gate runs, so floor/EMA21-breakout/RS/stop all see the current price.
v2.6: ZERO-DEPENDENCY HOLIDAY/WEEKEND GUARD. On a US non-trading day, SPY's most recent
      session date != today's date -> main() exits WITHOUT overwriting out.json.
v2.7: ISRAELI SENTINEL + AGOROT GUARDRAIL. TASE.TA (Tel Aviv Stock Exchange Ltd) is force-
      included. For any ".TA" (Tel Aviv) ticker the RS benchmark and the Regime ETF are
      measured against the Israeli TA-125 index (^TA125.TA), NOT SPY/XLK/SOXX. A
      'Currency (info only)' gate stamps that .TA prices are in AGOROT (ILA) — divide by 100
      for ILS — so the LLM never falls into the agorot trap (12,500 agorot = ILS 125.00, NOT USD).
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
HISTORY_PERIOD   = "1y"
PEG_MAX          = 1.8
VOL_MIN          = 0.40          # the ONLY sector-replacement filter
CATALYST_MIN_D   = 15
CATALYST_MAX_D   = 45
STOP_MULT        = 0.985
RS_TOLERANCE     = -0.003
FIB_TOL          = 0.03
SUPPORT_TOL      = 0.025
VOL_CLIMAX_MULT  = 2.0
EMA_FAST         = 21
LIVE_OVERRIDE_TOL = 0.001        # only override if live price differs >0.1% from last close

# Regime ETF proxies (US)
ETF_TECH   = "XLK"     # Technology sector
ETF_SEMIS  = "SOXX"    # Semiconductors
ETF_BROAD  = "SPY"     # everything else

# Market benchmark for Relative Strength (worst-red-day test) — US default
RS_BENCHMARK = "SPY"

# v2.7 — Israeli market: TA-125 index proxy (used for both RS and Regime of ".TA" tickers)
ISRAELI_SUFFIX = ".TA"
ETF_ISRAEL     = "^TA125.TA"

# Elite high-vol monsters force-included even if not (yet) in the S&P 100.
# TASE.TA = Tel Aviv Stock Exchange Ltd (prices in AGOROT — see Currency gate).
SENTINELS = ["VST", "CEG", "PLTR", "CRWD", "DDOG", "TASE.TA"]

# Emergency PARTIAL fallback (used ONLY if live fetch fails). Mega-cap subset.
SP100_PARTIAL_FALLBACK = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","GOOG","META","AVGO","TSLA","ORCL",
    "AMD","CRM","ADBE","NFLX","NOW","ACN","INTC","QCOM","TXN","AMAT",
    "PLTR","CSCO","IBM","INTU","PYPL","UBER","LIN","PEP","COST","JPM",
    "V","MA","HD","LLY","ABBV","MRK","UNH","XOM","CVX","CAT","BA","GE",
    "VST","CEG","CRWD","DDOG","TASE.TA"
]

# ----------------------------------------------------------------------------------
# UNIVERSE  (live S&P 100 / OEX constituents TABLE via pandas.read_html)
# ----------------------------------------------------------------------------------
def fetch_universe():
    """Returns (tickers:list, source:str). Live S&P 100 (OEX) constituents table + sentinels."""
    try:
        import io
        url = "https://en.wikipedia.org/wiki/S%26P_100"
        r = requests.get(url, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0 (stocksagent/2.7)"})
        r.raise_for_status()
        tables = pd.read_html(io.StringIO(r.text))

        tickers = []
        for t in tables:
            cols = [str(c).strip().lower() for c in t.columns]
            tcol = None
            for cand in ("symbol", "ticker"):
                for i, c in enumerate(cols):
                    if cand in c:
                        tcol = t.columns[i]; break
                if tcol is not None: break
            if tcol is None:
                continue
            raw = [str(s).strip().upper() for s in t[tcol].dropna().tolist()]
            syms = []
            for s in raw:
                if 1 <= len(s) <= 6 and s.replace(".", "").replace("-", "").isalpha():
                    syms.append(s.replace(".", "-"))   # BRK.B -> BRK-B for yfinance
            syms = list(dict.fromkeys(syms))
            if 95 <= len(syms) <= 110:          # plausibility window for the S&P 100 table (101)
                tickers = syms
                break

        if not tickers:
            raise ValueError("no S&P 100 constituents table with 95-110 tickers found")

        src = f"live: Wikipedia S&P 100 (OEX) constituents table ({len(tickers)} tickers)"
        added = [s for s in SENTINELS if s not in tickers]
        if added:
            tickers += added
            src += f" + sentinel backfill ({','.join(added)})"
        return sorted(set(tickers)), src
    except Exception as e:
        return list(dict.fromkeys(SP100_PARTIAL_FALLBACK)), \
               (f"FALLBACK: SP100_PARTIAL_FALLBACK ({len(set(SP100_PARTIAL_FALLBACK))} tickers, "
                f"PARTIAL — not full S&P 100) — live fetch failed ({e})")

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

def is_israeli(symbol):
    return symbol.upper().endswith(ISRAELI_SUFFIX)

def gate_currency(symbol, price):
    """v2.7 AGOROT GUARDRAIL — info only, never a filter."""
    if is_israeli(symbol):
        ils = (price / 100.0) if price else None
        return gate(0, "Currency (info only)", "GO",
                    f"ILA (agorot): yfinance price {price} = ILS {ils:.2f} (divide by 100). "
                    f"Stop/Target are in AGOROT too -> divide by 100 for ILS display. "
                    f"P/L% is currency-agnostic. NEVER read as USD.",
                    "ILA", "agorot guardrail: .TA price is agorot; show ILS = price/100, not USD")
    return gate(0, "Currency (info only)", "GO", f"USD {price}", "USD",
                "informational, US dollars")

def get_live_price(tk):
    """Best-effort current/last price from yfinance fast_info (quote endpoint),
    which is fresher than the daily-history bar. Returns float or None."""
    try:
        fi = getattr(tk, "fast_info", None)
        if fi is None:
            return None
        for key in ("last_price", "lastPrice"):
            try:
                v = fi[key] if isinstance(fi, dict) else getattr(fi, key, None)
            except Exception:
                v = None
            if v is not None and np.isfinite(float(v)) and float(v) > 0:
                return float(v)
    except Exception:
        return None
    return None

def apply_live_last_price(df, tk):
    """CURE LATENCY BLINDNESS: patch the last bar's Close (stretch High/Low) with the
    live fast_info price so every price-dependent gate (floor/EMA21-breakout/RS/stop)
    sees today's move, not a lagged daily bar. Returns (df, status, note)."""
    live = get_live_price(tk)
    if live is None:
        return df, "SKIP", "fast_info live price unavailable -> using last daily close"
    last_close = float(df["Close"].iloc[-1])
    if last_close <= 0:
        return df, "SKIP", "invalid last close -> no override"
    if abs(live - last_close) / last_close <= LIVE_OVERRIDE_TOL:
        return df, "GO", f"live {live:.2f} == last close {last_close:.2f} (fresh, no override)"
    i = df.index[-1]
    df.at[i, "Close"] = live
    df.at[i, "High"]  = max(float(df["High"].iloc[-1]), live)
    df.at[i, "Low"]   = min(float(df["Low"].iloc[-1]),  live)
    return df, "GO", f"LIVE OVERRIDE: lagged daily close {last_close:.2f} -> live {live:.2f} (fast_info)"

# ----------------------------------------------------------------------------------
# BENCHMARK / REGIME RESOLUTION
# ----------------------------------------------------------------------------------
def get_benchmark_close(symbol, bench_cache):
    """6-month close series for the Relative-Strength benchmark (cached)."""
    if symbol not in bench_cache:
        try:
            bench_cache[symbol] = yf.Ticker(symbol).history(period="6mo")["Close"].dropna()
        except Exception:
            bench_cache[symbol] = None
    return bench_cache[symbol]

def classify_regime_etf(symbol, info):
    """Israeli (.TA) -> TA-125 | Semiconductors -> SOXX | Technology -> XLK | else -> SPY."""
    if is_israeli(symbol):
        return ETF_ISRAEL
    sector = (info.get("sector") or "").lower()
    industry = (info.get("industry") or "").lower()
    if "semiconductor" in industry or "semiconductor" in sector:
        return ETF_SEMIS
    if "technology" in sector:
        return ETF_TECH
    return ETF_BROAD

def gate_regime(etf_symbol, etf_cache):
    if etf_symbol not in etf_cache:
        try:
            h = yf.Ticker(etf_symbol).history(period="2y")["Close"].dropna()
            etf_cache[etf_symbol] = h
        except Exception:
            etf_cache[etf_symbol] = pd.Series(dtype=float)
    h = etf_cache[etf_symbol]
    if h is None or len(h) < 200:
        return gate(1, "Sector trend (Regime, rank-only)", "SKIP",
                    f"{etf_symbol} history unavailable", None, "ETF > 200SMA (rank-only, non-rejecting)")
    sma200 = h.rolling(200).mean().iloc[-1]
    last = h.iloc[-1]
    ok = last > sma200
    return gate(1, "Sector trend (Regime, rank-only)", "GO" if ok else "NO_GO",
                f"{etf_symbol} {last:.2f} {'>' if ok else '<'} 200SMA {sma200:.2f}"
                f"{'' if ok else ' (sector/market weak — DECOUPLING context, NOT a rejection)'}",
                round(float(last), 2), "ETF > 200SMA (rank-only, non-rejecting)")

# ----------------------------------------------------------------------------------
# CORE GATES (AND, NO_GO)
# ----------------------------------------------------------------------------------
def gate_volatility(df):
    hi = df["High"].max(); lo = df["Low"].min()
    if lo <= 0 or math.isnan(lo):
        return gate(1, "Volatility (Upside DNA)", "SKIP", "no valid 52w low", None, "(H-L)/L >= 40%")
    rng = (hi - lo) / lo
    ok = rng >= VOL_MIN
    return gate(1, "Volatility (Upside DNA)", "GO" if ok else "NO_GO",
                f"52w range {rng*100:.1f}%", round(float(rng), 4), "(52wH-52wL)/52wL >= 40%")

def detect_floor(df):
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

def gate_relative_strength(df, bench_close, bench_name=RS_BENCHMARK):
    if bench_close is None or len(bench_close) < 30:
        return gate(4, "Relative Strength", "SKIP", f"{bench_name} history unavailable", None,
                    f"on {bench_name} worst red day: stock fell <0.3% or green")
    bret = bench_close.pct_change()
    worst_day = bret.iloc[-30:].idxmin()
    if worst_day not in df.index:
        common = df["Close"].reindex(bench_close.index).dropna()
        sret = common.pct_change()
        if worst_day not in sret.index:
            return gate(4, "Relative Strength", "SKIP", "no aligned red-day bar", None,
                        f"on {bench_name} worst red day: stock fell <0.3% or green")
        stock_move = sret.loc[worst_day]
    else:
        stock_move = df["Close"].pct_change().loc[worst_day]
    bench_move = bret.loc[worst_day]
    ok = stock_move >= RS_TOLERANCE
    return gate(4, "Relative Strength", "GO" if ok else "NO_GO",
                f"{bench_name} red day {worst_day.date()} {bench_move*100:.2f}% -> stock {stock_move*100:.2f}%",
                round(float(stock_move), 4),
                f"stock fell <0.3% or green on {bench_name} worst red day")

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
             "AI: direct threat=STRONG SELL | adapting w/ AI=BUY\u26a0\ufe0f | infra/data moat=clean",
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
# PER-TICKER PIPELINE  (SECTOR-AGNOSTIC — Regime is rank-only, NOT a rejecting gate)
# ----------------------------------------------------------------------------------
def scan_ticker(symbol, etf_cache, bench_cache, universe_source):
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

        # ----- v2.5: CURE LATENCY BLINDNESS — patch last bar with live fast_info price -----
        df, live_status, live_note = apply_live_last_price(df, tk)

        info = {}
        try: info = tk.info or {}
        except Exception: info = {}
        item["price"] = round(float(df["Close"].iloc[-1]), 2)   # native units (USD, or AGOROT for .TA)

        gates = []

        # ----- v2.7: choose benchmark & regime ETF (Israeli .TA -> TA-125) -----
        israeli = is_israeli(symbol)
        bench_symbol = ETF_ISRAEL if israeli else RS_BENCHMARK
        bench_close  = get_benchmark_close(bench_symbol, bench_cache)
        etf          = classify_regime_etf(symbol, info)

        g_regime = gate_regime(etf, etf_cache)

        # ----- CORE (AND, NO_GO) — sector-agnostic; Volatility is the only screen -----
        g_vol    = gate_volatility(df)
        g_floor, g_subs = gate_concrete_floor(df)
        g_rs     = gate_relative_strength(df, bench_close, bench_symbol)
        g_stop   = gate_hard_stop(df)

        # ----- SCORE (rank only) -----
        g_peg = gate_peg(info)
        g_fcf = gate_fcf(tk)
        g_cat = gate_catalyst(tk)

        # informational: currency (agorot guardrail), sector, live-price freshness (NOT filters)
        gates.append(gate_currency(symbol, item["price"]))
        sector = info.get("sector") or ("Financials (TASE)" if israeli else "unknown")
        gates.append(gate(1, "Sector (info only)", "GO",
                          f"{sector} -> regime ETF {etf}", sector, "informational, not a filter"))
        gates.append(gate(0, "Live price (fast_info)", live_status, live_note,
                          item["price"], "patch lagged daily bar with live price"))

        gates += [g_regime, g_vol, g_floor] + g_subs + [g_rs, g_peg, g_fcf, g_cat, g_stop]
        gates += ai_gates()
        item["gates"] = gates

        # ----- Verdict: any CORE NO_GO => rejected. Regime/PEG/FCF NO_GO never reject -----
        core = [g_vol, g_floor, g_rs]   # g_stop always GO; Regime & SCORE gates excluded
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
    # ---------------------------------------------------------------------------
    # v2.6 — ZERO-DEPENDENCY HOLIDAY / WEEKEND GUARD
    # On a US market holiday (e.g. July 4th) or weekend, dt.date.today() still
    # returns that calendar day, but the NYSE is closed so SPY's most recent bar
    # is the PRIOR trading session. We detect the date mismatch and exit safely
    # WITHOUT overwriting out.json (the last valid trading-day scan is preserved).
    # Reuses yfinance — no extra dependency, keeps requirements.txt lean.
    # NOTE: the guard is US-centric (SPY). On a rare US-closed / TASE-open day the
    # whole scan (incl. TASE.TA) is skipped — acceptable for a post-US-close schedule.
    # ---------------------------------------------------------------------------
    try:
        last_valid_session = yf.download("SPY", period="5d", progress=False).index[-1].date()
        if last_valid_session != dt.date.today():
            print(f"[!] NYSE Closed Today (last valid session: {last_valid_session}). "
                  f"Skipping out.json overwrite.")
            return
    except Exception as e:
        # A transient yfinance hiccup must NOT falsely skip a real trading day.
        print(f"[holiday-guard] SPY session check failed ({e}); proceeding with scan.")

    tickers, universe_source = fetch_universe()
    print(f"UNIVERSE {{count: {len(tickers)}, source: {universe_source}}}")

    etf_cache = {}
    bench_cache = {}
    # warm the US benchmark cache (Israeli TA-125 is fetched lazily per .TA ticker)
    get_benchmark_close(RS_BENCHMARK, bench_cache)

    results = []
    for i, sym in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {sym}")
        results.append(scan_ticker(sym, etf_cache, bench_cache, universe_source))
        time.sleep(0.3)

    results.sort(key=lambda x: (x["verdict"] != "GO_PENDING_THESIS", -x["go_count"]))
    survivors = [r["ticker"] for r in results if r["verdict"] == "GO_PENDING_THESIS"]
    print(f"SURVIVORS {survivors}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"WROTE {OUT_PATH} ({len(results)} items, {len(survivors)} survivors)")

if __name__ == "__main__":
    main()
