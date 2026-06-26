"""
scan.py — "Iron Checklist" stock screener (6-stage Go/No-Go).

Default behavior: with NO arguments it scans the entire **Nasdaq-100**.
This is what GitHub Actions / the Dust agent should call:

    python scan.py                       # scans the whole Nasdaq-100
    python scan.py --json out.json       # + writes full JSON
    python scan.py NVDA AVGO             # scan only specific tickers
    python scan.py --file tickers.txt    # tickers from a file
    python scan.py --fast                # stop a ticker at first No-Go
    python scan.py --limit 20            # only first N of the universe (debug)
    python scan.py --top 3               # print only the 3 best survivors

Quantitative gates are computed from yfinance. Qualitative gates (disruption
thesis, macro backdrop, RPO/backlog from filings, product-launch catalysts) are
returned as status "NEEDS_LLM" for the Dust agent to resolve via the web.

------------------------------------------------------------------------------
CHANGELOG (softened "best-practices" build)
------------------------------------------------------------------------------
* Stage 3 — fixed the AND bottleneck. RSI-divergence and Volume-climax are no
  longer independent rejecting gates. The "Concrete floor" is now ONE gate using
  OR logic: a real floor (fib 0.5/0.618 OR 2x horizontal support) + a bounce
  candle, confirmed by EITHER (A) bullish RSI divergence, OR (B) an exact fib
  touch with a volume climax >= 1.5x. RSI-div and Volume are reported as
  informational sub-signals (GO/SKIP) that never auto-reject.
* Stage 1 — added a "Macro backdrop" NEEDS_LLM gate so the agent checks
  Fed/CPI/jobs and the broad-market regime before approving an entry.
* Stage 1 — Disruption Test guidance now distinguishes a STRONG/direct threat
  (hard reject) from a MODERATE threat the company is adapting to (soft flag
  "GO (thesis risk)", do not auto-reject).
* Stage 5 — "Next earnings" outside the window is SKIP (non-rejecting) instead
  of NO_GO; the product/launch catalyst web check governs the catalyst decision.

Dependencies: yfinance>=0.2.59, curl_cffi>=0.7.0, pandas>=2.0, numpy,
              lxml, html5lib, beautifulsoup4, requests
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except Exception as exc:  # pragma: no cover
    print(f"[FATAL] yfinance import failed: {exc}", file=sys.stderr)
    raise

# curl_cffi gives yfinance a browser-impersonating session -> far fewer 429s.
try:
    from curl_cffi import requests as cffi_requests

    def make_session():
        return cffi_requests.Session(impersonate="chrome")
except Exception:  # pragma: no cover
    def make_session():
        return None


# --------------------------------------------------------------------------- #
# Nasdaq-100 universe                                                         #
# --------------------------------------------------------------------------- #

# Fallback list used only if the live Wikipedia fetch fails.
NDX_FALLBACK = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
    "AMZN", "ANSS", "APP", "ARM", "ASML", "AVGO", "AZN", "BIIB", "BKNG", "BKR",
    "CCEP", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD", "CSCO",
    "CSGP", "CSX", "CTAS", "CTSH", "DASH", "DDOG", "DXCM", "EA", "EXC", "FANG",
    "FAST", "FTNT", "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX", "INTC",
    "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "LULU", "MAR", "MCHP",
    "MDLZ", "MELI", "META", "MNST", "MRVL", "MSFT", "MU", "NFLX", "NVDA", "NXPI",
    "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PYPL", "QCOM",
    "REGN", "ROP", "ROST", "SBUX", "SNPS", "TEAM", "TMUS", "TSLA", "TTD", "TTWO",
    "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]


def get_nasdaq100(session=None) -> list:
    """Fetch the live Nasdaq-100 constituents from Wikipedia; fall back to a
    hardcoded list on any failure."""
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
        for t in tables:
            cols = [str(c).lower() for c in t.columns]
            tcol = next((t.columns[i] for i, c in enumerate(cols)
                         if "ticker" in c or "symbol" in c), None)
            if tcol is not None:
                syms = []
                for s in t[tcol].tolist():
                    s = str(s).strip().upper().replace(".", "-")
                    if s and s.replace("-", "").isalpha() and 1 <= len(s) <= 6:
                        syms.append(s)
                syms = sorted(set(syms))
                if len(syms) >= 90:
                    return syms
    except Exception as e:
        print(f"[warn] live Nasdaq-100 fetch failed ({e}); using fallback list.",
              file=sys.stderr)
    return sorted(set(NDX_FALLBACK))


# --------------------------------------------------------------------------- #
# Result containers                                                           #
# --------------------------------------------------------------------------- #

GO = "GO"
NO_GO = "NO_GO"
NEEDS_LLM = "NEEDS_LLM"   # qualitative — resolved by the Dust agent via the web
SKIP = "SKIP"             # not computable / informational — does not auto-reject


@dataclass
class Gate:
    stage: str
    name: str
    status: str
    detail: str = ""
    value: Optional[float] = None
    criterion: str = ""

    def line(self) -> str:
        icon = {GO: "✅", NO_GO: "⛔", NEEDS_LLM: "🌐", SKIP: "⚪"}.get(self.status, "?")
        return f"  {icon} [{self.status:<9}] {self.stage} · {self.name}: {self.detail}"


@dataclass
class TickerReport:
    ticker: str
    price: Optional[float] = None
    gates: list = field(default_factory=list)
    verdict: str = ""
    blocked_at: str = ""
    go_count: int = 0

    def add(self, g: Gate):
        self.gates.append(g)

    def finalize(self):
        self.go_count = sum(1 for g in self.gates if g.status == GO)
        if any(g.status == NO_GO for g in self.gates):
            self.verdict = NO_GO
            self.blocked_at = next(g.name for g in self.gates if g.status == NO_GO)
        elif any(g.status == NEEDS_LLM for g in self.gates):
            self.verdict = "GO_PENDING_THESIS"  # quant clean, web checks remain
        elif all(g.status in (GO, SKIP) for g in self.gates):
            self.verdict = GO
        else:
            self.verdict = "REVIEW"


# --------------------------------------------------------------------------- #
# Sector -> sector ETF mapping (Stage 1 macro gate)                           #
# --------------------------------------------------------------------------- #

SECTOR_ETF = {
    "Technology": "XLK",
    "Communication Services": "XLC",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
}
INDUSTRY_ETF = {"semiconductor": "SOXX", "software": "IGV"}


# --------------------------------------------------------------------------- #
# Indicators                                                                  #
# --------------------------------------------------------------------------- #

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def last_swing(close: pd.Series, lookback: int = 120):
    window = close.tail(lookback)
    low_idx = window.idxmin()
    after = window.loc[low_idx:]
    high_idx = after.idxmax()
    return float(window.loc[low_idx]), float(window.loc[high_idx]), low_idx, high_idx


def pivot_lows(low: pd.Series, k: int = 3) -> list:
    """Return prices of local-minimum bars: a bar whose Low is the lowest in a
    +/- k window around it."""
    vals = low.values
    out = []
    for i in range(k, len(vals) - k):
        win = vals[i - k:i + k + 1]
        if vals[i] == win.min():
            out.append(float(vals[i]))
    return out


def horizontal_support(low: pd.Series, current_low: float, current_price: float,
                       k: int = 3, tol_pct: float = 0.015, min_touches: int = 2):
    """Detect a horizontal support level the price has bounced off >= min_touches
    times, and check whether the current bar's low sits on it.
    Returns (is_on_support, level, touches)."""
    lows = pivot_lows(low, k=k)
    if not lows:
        return False, None, 0
    tol = tol_pct * current_price
    best_level, best_touches = None, 0
    for lvl in lows:
        touches = sum(1 for p in lows if abs(p - lvl) <= tol)
        if touches > best_touches:
            best_level, best_touches = lvl, touches
    if best_level is None or best_touches < min_touches:
        return False, best_level, best_touches
    on_support = abs(current_low - best_level) <= tol
    return on_support, best_level, best_touches


# --------------------------------------------------------------------------- #
# Stage evaluators                                                            #
# --------------------------------------------------------------------------- #

def stage1_macro(rep, tk, info, etf_cache, session, sector_etf_override):
    rep.add(Gate(
        "1·Moat", "Disruption Test", NEEDS_LLM,
        detail="Flagship under direct replacement threat in 5y? Pass only if the "
               "company is the infrastructure/enabler or has an expensive data moat. "
               "STRONG, direct threat (core product cloneable / displaced by AI) = "
               "HARD REJECT. MODERATE threat the company is actively adapting to "
               "(e.g. embedding AI into the product) = soft flag 'GO (thesis risk)', "
               "do NOT auto-reject.",
        criterion="Enabler or expensive-to-replace data moat; soft-flag moderate risk",
    ))
    rep.add(Gate(
        "1·Macro", "Macro backdrop", NEEDS_LLM,
        detail="Web-check the next ~45 days: Fed/FOMC decision, CPI/PCE & jobs "
               "prints, and the broad-market regime (VIX, QQQ vs 50/200-SMA). Flag "
               "if a macro event could sink the name regardless of its own setup.",
        criterion="No imminent macro shock; market regime supportive",
    ))
    etf = sector_etf_override
    if not etf:
        industry = (info.get("industry") or "").lower()
        if "semiconductor" in industry:
            etf = INDUSTRY_ETF["semiconductor"]
        elif "software" in industry:
            etf = INDUSTRY_ETF["software"]
        else:
            etf = SECTOR_ETF.get(info.get("sector", ""), "QQQ")
    try:
        if etf not in etf_cache:
            etf_cache[etf] = yf.Ticker(etf, session=session).history(period="1y")
        etf_close = etf_cache[etf]["Close"]
        sma200 = sma(etf_close, 200).iloc[-1]
        last = float(etf_close.iloc[-1])
        if np.isnan(sma200):
            rep.add(Gate("1·Macro", f"Sector trend ({etf})", SKIP,
                         detail="not enough history for 200-SMA"))
        else:
            ok = last > sma200
            rep.add(Gate("1·Macro", f"Sector trend ({etf})", GO if ok else NO_GO,
                         detail=f"{etf} {last:.2f} vs 200SMA {sma200:.2f}",
                         value=round(last - sma200, 2),
                         criterion="Sector ETF above 200-day SMA"))
    except Exception as e:
        rep.add(Gate("1·Macro", f"Sector trend ({etf})", SKIP, detail=f"error: {e}"))


def stage2_fundamentals(rep, tk, info):
    rep.add(Gate(
        "2·Cash", "RPO / Backlog", NEEDS_LLM,
        detail="Check latest 10-Q/10-K: is RPO growing YoY? (web search the filing).",
        criterion="RPO growing vs year-ago quarter",
    ))
    # FCF positive & growing YoY
    try:
        cf = tk.cashflow
        fcf_series = None
        if cf is not None and not cf.empty:
            idx = {str(i).lower(): i for i in cf.index}
            fcf_row = next((idx[k] for k in idx if "free cash flow" in k), None)
            if fcf_row is not None:
                fcf_series = cf.loc[fcf_row].dropna()
            else:
                ocf = next((idx[k] for k in idx
                            if "operating cash flow" in k or "total cash from operating" in k), None)
                capex = next((idx[k] for k in idx if "capital expenditure" in k), None)
                if ocf is not None and capex is not None:
                    fcf_series = (cf.loc[ocf] + cf.loc[capex]).dropna()
        if fcf_series is not None and len(fcf_series) >= 2:
            latest, prev = float(fcf_series.iloc[0]), float(fcf_series.iloc[1])
            ok = latest > 0 and latest >= prev
            rep.add(Gate("2·Cash", "Free Cash Flow", GO if ok else NO_GO,
                         detail=f"FCF latest {latest/1e9:.2f}B vs prior {prev/1e9:.2f}B",
                         value=round(latest, 0),
                         criterion="FCF positive and growing YoY (supports buybacks)"))
        else:
            fcf = info.get("freeCashflow")
            if fcf is not None:
                ok = fcf > 0
                rep.add(Gate("2·Cash", "Free Cash Flow", GO if ok else NO_GO,
                             detail=f"FCF(ttm) {fcf/1e9:.2f}B (growth unknown)",
                             value=float(fcf), criterion="FCF positive"))
            else:
                rep.add(Gate("2·Cash", "Free Cash Flow", SKIP, detail="no cashflow data"))
    except Exception as e:
        rep.add(Gate("2·Cash", "Free Cash Flow", SKIP, detail=f"error: {e}"))
    # PEG < 1.8
    try:
        peg = info.get("trailingPegRatio") or info.get("pegRatio")
        if peg is None:
            pe = info.get("trailingPE") or info.get("forwardPE")
            growth = info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth")
            if pe and growth and growth > 0:
                peg = pe / (growth * 100)
        if peg is not None and peg > 0:
            ok = peg < 1.8
            rep.add(Gate("2·Cash", "PEG ratio", GO if ok else NO_GO,
                         detail=f"PEG {peg:.2f}", value=round(float(peg), 2),
                         criterion="PEG < 1.8"))
        else:
            rep.add(Gate("2·Cash", "PEG ratio", SKIP, detail="PEG unavailable"))
    except Exception as e:
        rep.add(Gate("2·Cash", "PEG ratio", SKIP, detail=f"error: {e}"))


def stage3_technical_floor(rep, hist):
    """Stage 3 — 'Concrete floor' with OR logic.

    The strict build required BOTH an RSI divergence AND a fib bounce (two
    separate rejecting gates), which choked strong names that crash straight to
    the fib line and rip higher without forming a second low. This build
    confirms the floor if the price is sitting on a real level and today's candle
    bounced, AND at least ONE buyer-confirmation fires:
        Path A : bullish RSI divergence (sellers slowly exhausted), OR
        Path B : exact fib 0.5/0.618 touch + volume climax >= 1.5x avg
                 (institutions swallowed the supply in one shot).
    RSI-divergence and Volume-climax are reported as informational sub-signals
    (GO/SKIP) and NEVER auto-reject on their own.
    """
    close, low, high, vol = hist["Close"], hist["Low"], hist["High"], hist["Volume"]

    # --- Locate the floor + read today's candle ---------------------------- #
    near_fib = on_support = buyers_tail = bounced = False
    sup_level = None
    touches = 0
    fib50 = fib618 = c_low = float("nan")
    try:
        slow, shigh, _, _ = last_swing(close)
        rng = shigh - slow
        fib50 = shigh - 0.5 * rng
        fib618 = shigh - 0.618 * rng
        c_open = float(hist["Open"].iloc[-1])
        c_close = float(close.iloc[-1])
        c_low = float(low.iloc[-1])
        c_high = float(high.iloc[-1])
        tol = 0.02 * c_close

        near_fib = min(abs(c_low - fib50), abs(c_low - fib618)) <= tol
        on_support, sup_level, touches = horizontal_support(low, c_low, c_close)

        candle_rng = c_high - c_low
        buyers_tail = candle_rng > 0 and (c_close - c_low) / candle_rng >= 0.5
        bounced = c_close >= c_open * 0.995
    except Exception as e:
        rep.add(Gate("3·Floor", "Concrete floor", SKIP, detail=f"error: {e}"))
        return

    # --- Confirmation A: bullish RSI divergence ---------------------------- #
    rsi_div = False
    rsi_detail = "n/a"
    try:
        r = rsi(close)
        win = 40
        pc, pr = close.tail(win), r.tail(win)
        half = win // 2
        l1 = pc.iloc[:half].idxmin()
        l2 = pc.iloc[half:].idxmin()
        price_ll = float(pc.loc[l2]) <= float(pc.loc[l1])
        rsi_hl = float(pr.loc[l2]) > float(pr.loc[l1])
        rsi_div = price_ll and rsi_hl
        rsi_detail = (f"price LL={price_ll}, RSI HL={rsi_hl} "
                      f"(RSI {float(pr.loc[l1]):.1f}->{float(pr.loc[l2]):.1f})")
    except Exception as e:
        rsi_detail = f"error: {e}"

    # --- Confirmation B component: volume climax on the bounce day --------- #
    vol_climax = False
    ratio = float("nan")
    vol_detail = "n/a"
    try:
        avg_vol = float(vol.tail(21).mean())
        recent = hist.tail(10)
        bounce_idx = recent["Low"].idxmin()
        bounce_vol = float(vol.loc[bounce_idx])
        ratio = bounce_vol / avg_vol if avg_vol else float("nan")
        vol_climax = (not np.isnan(ratio)) and ratio >= 1.5
        vol_detail = (f"bounce-day vol {bounce_vol/1e6:.1f}M vs 21d avg "
                      f"{avg_vol/1e6:.1f}M ({ratio:.2f}x)")
    except Exception as e:
        vol_detail = f"error: {e}"

    # --- OR logic ---------------------------------------------------------- #
    floor_location = near_fib or on_support
    path_a = rsi_div
    path_b = near_fib and vol_climax
    confirmed = floor_location and buyers_tail and bounced and (path_a or path_b)

    if near_fib:
        where = f"fib (50={fib50:.2f}/618={fib618:.2f})"
    elif on_support:
        where = f"horizontal support {sup_level:.2f} ({touches} touches)"
    else:
        where = (f"none (low {c_low:.2f}; fib50 {fib50:.2f}/618 {fib618:.2f}; "
                 + (f"best support {sup_level:.2f}/{touches}x)" if sup_level else "no support)"))

    if path_a and path_b:
        via = "RSI divergence + fib/volume climax"
    elif path_a:
        via = "RSI divergence"
    elif path_b:
        via = "fib touch + volume climax"
    else:
        via = "no buyer confirmation"

    rep.add(Gate(
        "3·Floor", "Concrete floor (RSI-div OR fib+volume)",
        GO if confirmed else NO_GO,
        detail=f"low={c_low:.2f} on {where}; buyers_tail={buyers_tail}; "
               f"bounced={bounced}; confirmed via {via}",
        value=round(ratio, 2) if not np.isnan(ratio) else None,
        criterion="At a real floor + bounce candle + (bullish RSI divergence OR "
                  "exact 0.5/0.618 fib touch with >=1.5x volume climax)"))

    # Informational sub-signals — never auto-reject; shown for transparency.
    rep.add(Gate("3·Floor", "Bullish RSI divergence",
                 GO if rsi_div else SKIP, detail=rsi_detail,
                 criterion="Price lower-low while RSI higher-low (optional path A)"))
    rep.add(Gate("3·Floor", "Volume climax",
                 GO if vol_climax else SKIP, detail=vol_detail,
                 value=round(ratio, 2) if not np.isnan(ratio) else None,
                 criterion="Reversal-day volume >= 1.5x monthly avg (optional path B)"))


def stage4_relative_strength(rep, hist, qqq_hist):
    try:
        qret = qqq_hist["Close"].pct_change()
        recent_q = qret.tail(10)
        worst_day = recent_q.idxmin()
        worst_ret = float(recent_q.loc[worst_day])
        if worst_ret > -0.012:
            rep.add(Gate("4·RS", "Strength on a red day", SKIP,
                         detail=f"no QQQ day <= -1.2% in last 2 weeks (worst {worst_ret*100:.2f}%)"))
            return
        sret = hist["Close"].pct_change()
        target = worst_day.date()
        match = next((i for i in sret.tail(12).index if i.date() == target), None)
        if match is None:
            rep.add(Gate("4·RS", "Strength on a red day", SKIP,
                         detail="stock has no bar on QQQ's worst day"))
            return
        s = float(sret.loc[match])
        ok = s >= -0.003
        rep.add(Gate("4·RS", "Strength on a red day", GO if ok else NO_GO,
                     detail=f"on {target} QQQ {worst_ret*100:.2f}% / stock {s*100:.2f}%",
                     value=round(s * 100, 2),
                     criterion="Stock >= -0.3% (or green) on QQQ's strongest down day"))
    except Exception as e:
        rep.add(Gate("4·RS", "Strength on a red day", SKIP, detail=f"error: {e}"))


def stage5_catalyst(rep, tk, info):
    try:
        cal = None
        try:
            cal = tk.get_earnings_dates(limit=8)
        except Exception:
            cal = None
        next_dt = None
        now = datetime.now(timezone.utc)
        if cal is not None and not cal.empty:
            if cal.index.tz is None:
                future = [d.replace(tzinfo=timezone.utc) for d in cal.index.to_pydatetime()
                          if d.replace(tzinfo=timezone.utc) > now]
            else:
                future = [d for d in cal.index.to_pydatetime() if d > now]
            if future:
                next_dt = min(future)
        if next_dt is None:
            ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
            if ts:
                next_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if next_dt is not None:
            if next_dt.tzinfo is None:
                next_dt = next_dt.replace(tzinfo=timezone.utc)
            days = (next_dt - now).days
            inwin = 15 <= days <= 45
            # Softened: earnings outside the 0-60d window no longer rejects the
            # name; the product/launch catalyst web check governs the decision.
            if 0 <= days <= 60:
                status = GO
                note = "" if inwin else " — outside ideal 15-45d window (still a catalyst)"
            else:
                status = SKIP
                note = " — no earnings catalyst in window; rely on product/launch catalyst"
            rep.add(Gate("5·Catalyst", "Next earnings", status,
                         detail=f"earnings in {days} days ({next_dt.date()})" + note,
                         value=days,
                         criterion="Hard catalyst (earnings) inside ~15-45 days; "
                                   "outside = SKIP, agent confirms a product catalyst"))
        else:
            rep.add(Gate("5·Catalyst", "Next earnings", SKIP, detail="no earnings date"))
    except Exception as e:
        rep.add(Gate("5·Catalyst", "Next earnings", SKIP, detail=f"error: {e}"))
    rep.add(Gate(
        "5·Catalyst", "Product/launch catalyst", NEEDS_LLM,
        detail="Web-search next 15-45 days for product/chip/version launch or event. "
               "If earnings already GO, this is optional.",
        criterion="At least one scheduled catalyst in 15-45 days",
    ))


def stage6_stop(rep, hist):
    try:
        floor = float(hist["Low"].tail(10).min())
        stop = round(floor * (1 - 0.015), 2)
        rep.add(Gate("6·Exit", "Hard stop price", GO,
                     detail=f"floor {floor:.2f} -> HARD STOP {stop:.2f} (daily close below = auto-sell)",
                     value=stop,
                     criterion="Stop = floor - 1.5%, typed in advance"))
    except Exception as e:
        rep.add(Gate("6·Exit", "Hard stop price", SKIP, detail=f"error: {e}"))


# --------------------------------------------------------------------------- #
# Per-ticker driver                                                           #
# --------------------------------------------------------------------------- #

def scan_ticker(symbol, session, qqq_hist, etf_cache,
                sector_etf_override=None, fast=False) -> TickerReport:
    rep = TickerReport(ticker=symbol.upper())
    tk = yf.Ticker(symbol, session=session)
    try:
        info = tk.get_info()
    except Exception:
        info = {}
    try:
        hist = tk.history(period="1y", auto_adjust=False)
    except Exception as e:
        rep.add(Gate("0", "Price data", NO_GO, detail=f"no history: {e}"))
        rep.finalize()
        return rep
    if hist is None or hist.empty or len(hist) < 60:
        rep.add(Gate("0", "Price data", NO_GO, detail="insufficient history"))
        rep.finalize()
        return rep

    rep.price = round(float(hist["Close"].iloc[-1]), 2)
    stages = [
        lambda: stage1_macro(rep, tk, info, etf_cache, session, sector_etf_override),
        lambda: stage2_fundamentals(rep, tk, info),
        lambda: stage3_technical_floor(rep, hist),
        lambda: stage4_relative_strength(rep, hist, qqq_hist),
        lambda: stage5_catalyst(rep, tk, info),
        lambda: stage6_stop(rep, hist),
    ]
    for run in stages:
        run()
        if fast and any(g.status == NO_GO for g in rep.gates):
            break
    rep.finalize()
    return rep


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Iron-Checklist 6-stage Go/No-Go screener. "
                    "With no tickers it scans the whole Nasdaq-100.")
    p.add_argument("tickers", nargs="*", help="Optional explicit tickers, e.g. NVDA AVGO.")
    p.add_argument("--file", help="File with one ticker per line.")
    p.add_argument("--universe", default="nasdaq100",
                   help="Universe to scan when no tickers given (default: nasdaq100).")
    p.add_argument("--sector-etf", default=None, help="Override sector ETF (e.g. SOXX, IGV).")
    p.add_argument("--fast", action="store_true", help="Stop a ticker at the first No-Go.")
    p.add_argument("--limit", type=int, default=0, help="Only scan first N of the universe.")
    p.add_argument("--top", type=int, default=0, help="Print only the N best survivors.")
    p.add_argument("--json", help="Write full results to this JSON path.")
    return p.parse_args(argv)


def resolve_universe(args, session) -> list:
    tickers = list(args.tickers)
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            tickers += [ln.strip().upper() for ln in fh
                        if ln.strip() and not ln.startswith("#")]
    if not tickers:
        # No explicit tickers -> scan the default universe (Nasdaq-100).
        if args.universe.lower() in ("nasdaq100", "ndx", "nasdaq-100"):
            tickers = get_nasdaq100(session)
            print(f"[info] no tickers given -> scanning Nasdaq-100 "
                  f"({len(tickers)} names).", file=sys.stderr)
        else:
            print(f"[error] unknown universe '{args.universe}'.", file=sys.stderr)
            return []
    seen, out = set(), []
    for t in tickers:
        u = t.upper()
        if u not in seen:
            seen.add(u)
            out.append(u)
    if args.limit and args.limit > 0:
        out = out[:args.limit]
    return out


def main(argv=None):
    args = parse_args(argv)
    session = make_session()
    tickers = resolve_universe(args, session)
    if not tickers:
        print("No tickers resolved.", file=sys.stderr)
        return 2

    etf_cache = {}
    try:
        qqq_hist = yf.Ticker("QQQ", session=session).history(period="6mo")
    except Exception:
        qqq_hist = pd.DataFrame()

    reports = []
    for i, sym in enumerate(tickers, 1):
        try:
            rep = scan_ticker(sym, session, qqq_hist, etf_cache,
                              sector_etf_override=args.sector_etf, fast=args.fast)
        except Exception as e:
            rep = TickerReport(ticker=sym)
            rep.add(Gate("0", "Scan error", SKIP, detail=str(e)))
            rep.finalize()
        reports.append(rep)
        print(f"[{i}/{len(tickers)}] {rep.ticker}: {rep.verdict}"
              + (f" (blocked: {rep.blocked_at})" if rep.blocked_at else ""),
              file=sys.stderr)

    # Survivors ranked by GO count (most gates passed first).
    survivors = [r for r in reports if r.verdict in (GO, "GO_PENDING_THESIS")]
    survivors.sort(key=lambda r: r.go_count, reverse=True)

    to_show = survivors[:args.top] if args.top and args.top > 0 else survivors
    for rep in to_show:
        print(f"\n{'='*72}")
        print(f"  {rep.ticker}   price={rep.price}   VERDICT: {rep.verdict}"
              f"   (GO gates: {rep.go_count})")
        print(f"{'='*72}")
        for g in rep.gates:
            print(g.line())

    if not survivors:
        print("\nNo stocks passed the quantitative gates.")

    if args.json:
        payload = [{
            "ticker": r.ticker, "price": r.price, "verdict": r.verdict,
            "blocked_at": r.blocked_at, "go_count": r.go_count,
            "gates": [asdict(g) for g in r.gates],
        } for r in reports]
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"\n[json] wrote {args.json}")

    # Compact machine-readable summary for the Dust agent.
    print("\nSURVIVORS " + json.dumps(
        [{"ticker": r.ticker, "verdict": r.verdict, "go_count": r.go_count,
          "price": r.price} for r in survivors], ensure_ascii=False))
    print("SUMMARY " + json.dumps({r.ticker: r.verdict for r in reports},
                                  ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
