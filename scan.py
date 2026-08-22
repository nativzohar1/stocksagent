#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan.py  —  Decoupling Hunter / Institutional Swing Scanner v3.5 (Mega-Cap Monster Radar)
Runs on GitHub Actions (has internet). Writes results/out.json + companion files.

UNIVERSE: S&P 100 (OEX) — the ~101 largest, most established US companies, EXCHANGE-AGNOSTIC
(includes NYSE names like ORCL/CRM and Nasdaq names like NOW/PLTR). SECTOR-AGNOSTIC.
Plus a SENTINELS force-include list (VST, CEG, PLTR, CRWD, DDOG, TASE.TA). The ONLY screen
keeping slow blue-chips (banks/staples) out is Volatility >= 40%.

Python OWNS (hard quant):
  CORE (AND, NO_GO):  Volatility>=40% | Entry trigger (Path C OR Path E) | Relative Strength
                      | Risk geometry (stop distance <= 12%)            <-- v2.9
  SCORE (rank only):  Regime>200SMA | PEG<1.8 | FCF positive+growing | Catalyst (earnings 15-45d)
                      -> SKIP = data MISSING (AI heals) ; NO_GO = data PRESENT but bad (never rejects)
AI OWNS (NEEDS_LLM):  Rule-of-40/RPO | Insider Form4 | Disruption | Devil's Advocate | Data-Healing
                      | Bounce catalyst anchor (Path E)  | Disruption quality (WARN test)  <-- v2.9

v2.4: Regime is rank-only (never rejects).
v2.5: CURE 'LATENCY BLINDNESS' — patch last daily bar with live fast_info price.
v2.6: ZERO-DEPENDENCY HOLIDAY/WEEKEND GUARD.
v2.7: ISRAELI SENTINEL + AGOROT GUARDRAIL (TASE.TA, TA-125 benchmark).
v2.8: Companion files (out_summary.json / out_survivors.json / tickers/*.json).

v2.9: CATCH THE BOTTOM, NOT THE CHASE. Five changes, all evidence-driven:
  (1) RISK GEOMETRY (new CORE, rejecting). With a FIXED $10,000 allocation the real risk of a
      trade is the distance to the hard stop. Entering ORCL at 141.85 (stop 112.78) risks
      -20.5% to make +20% => reward/risk 0.98:1. Entering the same name at its floor (~123)
      risks -8.3% => 2.4:1. This gate rejects any setup whose stop is further than
      MAX_RISK_PCT (12%) below price. It is the single strongest quality filter available:
      in the live portfolio every wide-stop entry (ABT 14.3%, TMO 12.9%, DHR 16.9%)
      underperformed, while every tight-stop entry (LMT 7.5%, BMY 8.5%) led.
  (2) EARLY ENTRY / PATH E (new, alternative trigger). The v2.8 trigger required the price to be
      ON the floor AND closing ABOVE EMA21 on the SAME day. In a V-shaped reversal that day never
      exists (by the time EMA21 is reclaimed the price has left the floor -> 'no floor near
      price', which is exactly why NOW and MSFT were rejected). Path E keeps the floor + bounce
      + reversal-confirmation requirements but DROPS the EMA21-reclaim requirement, so an entry
      can be taken AT the floor. Because that is an unconfirmed entry it is fenced by
      (a) the risk-geometry gate, (b) a proximity band around EMA21 (no freefall, no chase),
      (c) a MANDATORY LLM event-anchor check, and (d) a 5-session TIME STOP.
  (3) RELATIVE STRENGTH is now DUAL-MODE. The v2.8 test sampled ONE day (the benchmark's worst
      red day) — pure noise. ORCL was rejected at 10/13 GO for falling 1.85% on 2026-07-29,
      then rallied +20% in five sessions. RS now passes on the red-day test OR on 20-session
      relative outperformance >= +2% vs the benchmark. Both are reported.
  (4) BOUNCE DATE is now computed and published, so the LLM can verify WHAT happened on the day
      the reversal started. A bounce anchored to an earnings beat / guidance / disclosure is a
      re-rating; a bounce anchored to nothing is mean-reversion noise (this is the difference
      between ORCL's $638B backlog disclosure and ISRG's narrative bounce).
  (5) DISRUPTION QUALITY test (LLM). A WARN-level disruption threat is only acceptable while the
      fundamentals are ACCELERATING. Active competitive displacement + decelerating growth =
      hard reject. This blocks the ISRG archetype without blocking BMY / LMT.

v3.0: TRADING-SESSION CALENDAR. Purely additive — ZERO change to any gate, threshold or
  survivor. out_summary.json now also carries session_date / session_index / sessions[],
  the list of real US trading sessions (a closed day has no SPY bar, so holidays are
  absent by construction). Days Held in the ledger becomes an index subtraction instead
  of the LLM counting weekdays and recalling holidays from memory — which produced wrong
  values (T and JNJ read 12 sessions when the true count was 10) and, worse, drifted the
  Path-E TIME STOP that the whole early-entry model depends on. If the calendar cannot be
  built the field is empty and the agent is instructed to leave Days Held untouched.

v3.5 RELIABILITY HARDENING. **ZERO changes to any gate, threshold, formula or verdict rule.**
  Every survivor v3.0 produces on healthy data, v3.5 produces identically. What changed is only
  HOW the data is obtained — and what happens when it cannot be obtained:
  (1) BATCH HISTORY — yf.download() in chunks of BATCH_SIZE with group_by="ticker": ~5 requests
      instead of ~106. auto_adjust=True is passed EXPLICITLY so values match Ticker.history()'s
      default exactly. A symbol missing/short in a batch falls back to a per-ticker history()
      call, so batching can never shrink the universe.
  (2) RETRY + EXPONENTIAL BACKOFF + JITTER — one wrapper (retry_call / safe_call) around every
      network call. Retries fire ONLY on transient faults (429 / timeout / connection / 5xx /
      empty payload); a real data fault is never retried into silence. Waits 1s -> 3s -> 9s with
      jitter, because a fixed 3s retry lands inside the same rate-limit window three times.
  (3) FOUR-LAYER UNIVERSE — iShares OEF holdings CSV -> Wikipedia table (the v3.0 path, parsing
      unchanged) -> results/universe_last_good.json (self-healing cache written after every
      successful live fetch) -> the 45-name static partial. NOTE: universe_source now has THREE
      prefixes: "live:", "CACHE:", "FALLBACK:".
  (4) DATA-INTEGRITY GATE — bar count, NaN in the last closes, non-positive price, and staleness
      of the last bar against the session calendar. A HARD fault becomes blocked_at="data: ..."
      with a stage-0 "Data availability" NO_GO (a data fault, never a thesis judgement). A 1-2
      session lag is a soft flag only, and a .TA lag is expected (TASE trades Sun-Thu).
  (5) ABORT-ON-DEGRADATION — if more than ABORT_MAX_FAIL_PCT (10%) of the universe returns a
      data/pipeline failure, results/ is NOT overwritten: yesterday's committed scan stays
      authoritative, results/run_status.json is written, and the process exits 1 so the Action
      goes red. A crippled scan does not fail loudly — it silently shrinks the survivor set, or
      worse produces a survivor from incomplete history. With real money downstream that is a
      buy order.
  out_summary.json additionally publishes run_health{} (coverage_pct, n_data_failures, n_errors,
  degraded_run, yfinance_version, universe_kind, abort threshold) for the Data-Health block.

v3.6 BENCHMARK BASELINE. **ZERO changes to any gate, threshold, formula or verdict rule.**
  out_summary.json now publishes benchmark{} — SPY's dividend-adjusted close on the first real
  trading session on/after BENCHMARK_BASE_DATE (the ledger's capital start date), today's close,
  and the total return between them. Absolute book return in a rising market is not evidence of
  skill; without this field "+4.9%" cannot be read as beat or miss. Everything is derived from
  history the scanner already had, so no number enters the system from outside the JSON.
  Fully additive: on any failure the key is null and the scan is untouched.

  ANTI-OVERFIT NOTE: no rule here is tuned to a single ticker. MAX_RISK_PCT, the EMA21 band and
  the RS threshold are structural risk parameters, and the two decisive filters (event anchor,
  disruption quality) are qualitative and must be evidenced with a dated, cited source by the
  LLM. If the LLM cannot cite the event, Path E MUST be refused.
"""

import io, json, math, os, random, sys, time, datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ----------------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------------
SCANNER_VERSION  = "3.6"

# ---- v3.5 TEST-RUN SWITCH (opt-in, default = exactly the v3.0 behaviour) ------------
# SCAN_FORCE_RUN=1  -> bypass the weekend/holiday guard so the FULL pipeline can be smoke
#                      tested while the market is closed. Prices are the last session's
#                      closes, so such a run is a PLUMBING test, never a trading signal.
# SCAN_OUT_DIR=...  -> write everything to another directory (e.g. results_test) so a test
#                      run can never touch the results/ the agent reads.
# Both default to OFF: with no env vars set, this file behaves identically to v3.0. There is
# nothing to "put back afterwards" — which is exactly the point, because a temporary edit that
# someone forgets to revert is how a closed-market run ends up in the ledger.
FORCE_RUN        = os.getenv("SCAN_FORCE_RUN", "") == "1"
RESULTS_DIR      = Path(os.getenv("SCAN_OUT_DIR", "results"))
OUT_PATH         = RESULTS_DIR / "out.json"
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

# ---- v2.9 parameters -------------------------------------------------------------
MAX_RISK_PCT        = 0.12   # CORE: reject if (price - stop)/price > 12%
VOL_CLIMAX_EARLY    = 1.5    # Path E accepts a softer volume climax than Path C's 2.0x
EMA_BAND_ABOVE      = 1.15   # Path E: price must be <= EMA21 * 1.15  (anti-chase, kills MSFT@487)
EMA_BAND_BELOW      = 0.85   # Path E: price must be >= EMA21 * 0.85  (anti-freefall)
RS_ROLL_WINDOW      = 20     # sessions for the rolling relative-strength test
RS_ROLL_MIN_EXCESS  = 0.02   # must beat the benchmark by >= +2% over the window
PATH_E_TIME_STOP    = 5      # sessions: a Path-E entry that has not reclaimed EMA21 is cut

# ---- v3.5 reliability parameters (NOT gate thresholds — they cannot change a verdict) -------
TICKER_SLEEP        = 0.5    # was 0.3 — politeness delay between per-ticker metadata calls
RECENT_BARS         = 15     # sessions of OHLC published per ticker for exit simulation
BATCH_SIZE          = 25     # symbols per yf.download() history batch
BATCH_SLEEP         = 1.0    # pause between history batches
USE_BATCH_HISTORY   = True   # False falls back to the v3.0 per-ticker history() path
RETRY_MAX           = 3      # attempts per network call
RETRY_BASE          = 1.0    # backoff base: 1s -> 3s -> 9s
RETRY_JITTER        = 0.40   # up to +40% random jitter, so parallel runners de-sync
MIN_BARS            = 200    # unchanged v3.0 requirement
STALE_HARD_SESSIONS = 3      # last bar >= 3 sessions behind the session date = HARD data fault
ABORT_MAX_FAIL_PCT  = 0.10   # >10% of the universe failing => DO NOT overwrite results/

# v3.6 — max share of the universe allowed to FAIL BENCHMARK INDEX ALIGNMENT before the run is
# treated as degraded. Above this it is a plumbing fault, not a hard market.
RS_ALIGN_MAX_PCT    = 0.05

UNIVERSE_CACHE   = RESULTS_DIR / "universe_last_good.json"
RUN_STATUS_PATH  = RESULTS_DIR / "run_status.json"
USER_AGENT       = f"Mozilla/5.0 (stocksagent/{SCANNER_VERSION})"

# iShares S&P 100 ETF (OEF) holdings CSV — the actual index constituents, machine readable.
OEF_HOLDINGS_URL = ("https://www.ishares.com/us/products/239723/ishares-sp-100-etf/"
                    "1467271812596.ajax?fileType=csv&fileName=OEF_holdings&dataType=fund")
WIKI_SP100_URL   = "https://en.wikipedia.org/wiki/S%26P_100"

# Non-equity / cash placeholder rows that appear in ETF holdings files.
HOLDINGS_BLACKLIST = {"CASH", "USD", "XTSLA", "MVRXX", "BLK", "WEUSD", "NA"}

# Regime ETF proxies (US)
ETF_TECH   = "XLK"     # Technology sector
ETF_SEMIS  = "SOXX"    # Semiconductors
ETF_BROAD  = "SPY"     # everything else

# Market benchmark for Relative Strength — US default
RS_BENCHMARK = "SPY"

# v3.6 — benchmark baseline for the book's alpha comparison.
# This is the ledger's capital start date. The scanner resolves the first REAL trading
# session on/after it and publishes SPY's dividend-adjusted close there, so the agent can
# state buy-and-hold return without sourcing a single number from outside the JSON.
BENCHMARK_BASE_DATE = "2026-06-28"

# v2.7 — Israeli market: TA-125 index proxy (used for both RS and Regime of ".TA" tickers)
ISRAELI_SUFFIX = ".TA"
ETF_ISRAEL     = "^TA125.TA"

# Elite high-vol monsters force-included even if not (yet) in the S&P 100.
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
# v3.5 (2) — RETRY / BACKOFF WRAPPER
# Retries ONLY transient faults. A genuine data fault is raised immediately, so it is reported
# as a data fault instead of being retried into silence.
# ----------------------------------------------------------------------------------
RUN_ERRORS = []   # transient/soft failures, published in run_health for post-mortems

def _log_error(msg):
    RUN_ERRORS.append(str(msg)[:300])
    print(f"[error] {msg}")

_TRANSIENT_TOKENS = (
    "429", "too many requests", "rate limit", "rate-limit", "timed out", "timeout",
    "temporarily", "connection reset", "connection aborted", "connection error",
    "max retries", "502", "503", "504", "bad gateway", "service unavailable",
    "remote end closed", "expecting value", "jsondecode", "ssl", "read timed out",
    "empty payload", "no data found", "unable to retrieve",
)

def _is_transient(exc):
    if isinstance(exc, (requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code in (429, 500, 502, 503, 504):
            return True
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(t in msg for t in _TRANSIENT_TOKENS)

def _is_empty(res):
    if res is None:
        return True
    if isinstance(res, (pd.DataFrame, pd.Series)):
        return res.empty
    if isinstance(res, (dict, list, tuple, set, str)):
        return len(res) == 0
    return False

def retry_call(fn, what="call", retries=RETRY_MAX, base=RETRY_BASE, allow_empty=False):
    """Run fn() with exponential backoff + jitter. Raises the last exception on failure."""
    last = None
    for attempt in range(1, retries + 1):
        try:
            res = fn()
            if not allow_empty and _is_empty(res):
                raise RuntimeError("empty payload")
            return res
        except Exception as e:
            last = e
            if attempt == retries or not _is_transient(e):
                break
            wait = base * (3 ** (attempt - 1)) * (1.0 + random.uniform(0.0, RETRY_JITTER))
            print(f"[retry] {what}: attempt {attempt}/{retries} failed ({e}); sleeping {wait:.1f}s")
            time.sleep(wait)
    raise last if isinstance(last, BaseException) else RuntimeError(f"{what} failed")

def safe_call(fn, what="call", default=None, allow_empty=True):
    """retry_call that never raises — for optional metadata (info / cashflow / calendar)."""
    try:
        return retry_call(fn, what=what, allow_empty=allow_empty)
    except Exception as e:
        _log_error(f"{what}: {e}")
        return default

# ----------------------------------------------------------------------------------
# v3.5 (3) — FOUR-LAYER UNIVERSE
#   1. iShares OEF holdings CSV        (live, the actual index constituents)
#   2. Wikipedia S&P 100 table         (live, the v3.0 path — parsing + 95-110 guard unchanged)
#   3. results/universe_last_good.json (self-healing cache, written on every live success)
#   4. SP100_PARTIAL_FALLBACK          (45 names, "everything burned" only)
# ----------------------------------------------------------------------------------
def _naive_index(obj):
    """
    v3.6 FIX — force a tz-NAIVE, midnight-normalised DatetimeIndex.

    yfinance is INCONSISTENT about timezones: Ticker.history() returns a tz-AWARE index
    (America/New_York) while yf.download() returns a tz-NAIVE one. Mixing the two breaks
    every date alignment SILENTLY:
        ts in df.index               -> always False
        series.reindex(other.index)  -> all NaN
        pd.concat(..., join="inner") -> EMPTY frame (or raises)

    That is exactly what v3.5's batch path did to Relative Strength: BOTH legs of the dual-mode
    test compare the stock frame (batch, naive) against the benchmark series (Ticker.history,
    aware), so RS returned NO_GO for the ENTIRE universe. The scan still reported 100% coverage,
    exit code 0 and a green Action — it just produced zero survivors, every single day, and the
    LLM is instructed to treat a zero-survivor run as CORRECT. A silent, self-consistent,
    permanently wrong scan is the worst failure mode this system can have.

    Values were never the problem (auto_adjust=True was already passed explicitly). The INDEX
    was. Normalising at every entry point is the only durable fix.
    """
    try:
        idx = pd.DatetimeIndex(obj.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        obj = obj.copy()
        obj.index = idx.normalize()
        return obj
    except Exception:
        return obj


def _norm_symbols(raw):
    """v3.0 normalisation, verbatim: alpha-only, <=6 chars, BRK.B -> BRK-B, order preserved."""
    out = []
    for s in raw:
        s = str(s).strip().upper()
        if 1 <= len(s) <= 6 and s.replace(".", "").replace("-", "").isalpha():
            out.append(s.replace(".", "-"))
    return list(dict.fromkeys(out))

def _fetch_universe_oef():
    r = retry_call(lambda: requests.get(OEF_HOLDINGS_URL, timeout=25,
                                        headers={"User-Agent": USER_AGENT}),
                   what="OEF holdings CSV")
    r.raise_for_status()
    lines = r.text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.lstrip('"\ufeff ').lower().startswith("ticker"):
            start = i
            break
    if start is None:
        raise ValueError("OEF holdings CSV: no 'Ticker' header row found")
    tbl = pd.read_csv(io.StringIO("\n".join(lines[start:])))
    tcol = next((c for c in tbl.columns if str(c).strip().lower() == "ticker"), None)
    if tcol is None:
        raise ValueError("OEF holdings CSV: no ticker column")
    acol = next((c for c in tbl.columns if "asset class" in str(c).strip().lower()), None)
    if acol is not None:
        tbl = tbl[tbl[acol].astype(str).str.strip().str.lower() == "equity"]
    syms = [s for s in _norm_symbols(tbl[tcol].dropna().tolist()) if s not in HOLDINGS_BLACKLIST]
    if not (90 <= len(syms) <= 115):
        raise ValueError(f"OEF holdings CSV: implausible constituent count ({len(syms)})")
    return syms

def _fetch_universe_wikipedia():
    r = retry_call(lambda: requests.get(WIKI_SP100_URL, timeout=20,
                                        headers={"User-Agent": USER_AGENT}),
                   what="Wikipedia S&P 100")
    r.raise_for_status()
    tables = pd.read_html(io.StringIO(r.text))
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
        syms = _norm_symbols(t[tcol].dropna().tolist())
        if 95 <= len(syms) <= 110:
            return syms
    raise ValueError("no S&P 100 constituents table with 95-110 tickers found")

def _save_universe_cache(symbols, source):
    try:
        UNIVERSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        UNIVERSE_CACHE.write_text(json.dumps(
            {"saved_utc": _utc_now_str(), "source": source,
             "n": len(symbols), "tickers": symbols}, indent=2), encoding="utf-8")
        print(f"[universe] cached {len(symbols)} tickers -> {UNIVERSE_CACHE}")
    except Exception as e:
        _log_error(f"universe cache write failed: {e}")

def _load_universe_cache():
    try:
        if not UNIVERSE_CACHE.exists():
            return None
        blob = json.loads(UNIVERSE_CACHE.read_text(encoding="utf-8"))
        syms = _norm_symbols(blob.get("tickers") or [])
        if len(syms) < 60:
            return None
        return {"tickers": syms, "saved_utc": blob.get("saved_utc"), "source": blob.get("source")}
    except Exception as e:
        _log_error(f"universe cache read failed: {e}")
        return None

def _with_sentinels(symbols, src):
    added = [s for s in SENTINELS if s not in symbols]
    if added:
        symbols = list(symbols) + added
        src += f" + sentinel backfill ({','.join(added)})"
    return sorted(set(symbols)), src

def fetch_universe():
    """Returns (tickers:list, source:str, kind:str) with kind in {'live','cache','fallback'}."""
    for label, fn in (("iShares OEF holdings", _fetch_universe_oef),
                      ("Wikipedia S&P 100 (OEX) constituents table", _fetch_universe_wikipedia)):
        try:
            syms = fn()
            src = f"live: {label} ({len(syms)} tickers)"
            _save_universe_cache(syms, src)
            tickers, src = _with_sentinels(syms, src)
            return tickers, src, "live"
        except Exception as e:
            _log_error(f"universe layer '{label}' failed: {e}")

    cached = _load_universe_cache()
    if cached:
        src = (f"CACHE: universe_last_good.json ({len(cached['tickers'])} tickers, "
               f"saved {cached['saved_utc']}, origin: {cached['source']}) — live fetch failed")
        tickers, src = _with_sentinels(cached["tickers"], src)
        return tickers, src, "cache"

    src = (f"FALLBACK: SP100_PARTIAL_FALLBACK ({len(set(SP100_PARTIAL_FALLBACK))} tickers, "
           f"PARTIAL — not full S&P 100) — live fetch AND cache both failed")
    return sorted(set(SP100_PARTIAL_FALLBACK)), src, "fallback"

# ----------------------------------------------------------------------------------
# v3.5 (1) — BATCH HISTORY DOWNLOAD
# auto_adjust=True is EXPLICIT so batch values match Ticker.history()'s default exactly.
# A symbol missing or short in a batch is simply absent from the cache and is refetched
# individually by scan_ticker — batching can never remove a ticker from the scan.
# ----------------------------------------------------------------------------------
def download_history_batch(symbols):
    out = {}
    chunks = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    for n, chunk in enumerate(chunks, 1):
        what = f"batch history {n}/{len(chunks)} ({len(chunk)} symbols)"
        try:
            raw = retry_call(lambda c=chunk: yf.download(
                c, period=HISTORY_PERIOD, interval="1d", group_by="ticker",
                auto_adjust=True, actions=False, progress=False, threads=True), what=what)
        except Exception as e:
            _log_error(f"{what} failed: {e} -> per-ticker fallback for this chunk")
            time.sleep(BATCH_SLEEP)
            continue
        for sym in chunk:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if sym not in set(raw.columns.get_level_values(0)):
                        continue
                    df = raw[sym]
                else:
                    df = raw
                df = df.dropna(how="all").dropna()
                if not df.empty:
                    out[sym] = _naive_index(df)   # v3.6 FIX: yf.download() is tz-naive
            except Exception:
                continue
        print(f"[{what}] ok for {sum(1 for s in chunk if s in out)}/{len(chunk)}")
        time.sleep(BATCH_SLEEP)
    return out

# ----------------------------------------------------------------------------------
# TECHNICAL HELPERS
# ----------------------------------------------------------------------------------
def _utc_now_str():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z"

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
    """Best-effort current/last price from yfinance fast_info. Returns float or None."""
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
    """CURE LATENCY BLINDNESS: patch the last bar's Close with the live fast_info price."""
    live = safe_call(lambda: get_live_price(tk), what="fast_info live price", default=None)
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
# v3.5 (4) — DATA-INTEGRITY GATE
# A HARD fault -> stage-0 "Data availability" NO_GO + blocked_at="data: ..." (the gate name the
# agent prompt already classifies as a data fault, never a thesis judgement).
# A SOFT flag -> a stage-0 informational gate only; soft flags NEVER change a verdict.
# ----------------------------------------------------------------------------------
def check_data_integrity(symbol, df, expected_session, sessions, hist_src):
    flags, hard = [], None
    bars = len(df)
    if bars < MIN_BARS:
        hard = f"only {bars} daily bars (< {MIN_BARS})"
    else:
        close = df["Close"]
        if bool(close.iloc[-5:].isna().any()):
            hard = "NaN in the last 5 closes"
        elif not np.isfinite(float(close.iloc[-1])) or float(close.iloc[-1]) <= 0:
            hard = f"non-positive/invalid last close ({close.iloc[-1]})"

    last_bar = None
    try:
        last_bar = str(pd.to_datetime(df.index[-1]).date())
    except Exception:
        flags.append("last bar date unreadable")

    lag = None
    if hard is None and last_bar and expected_session:
        if sessions and last_bar in sessions and expected_session in sessions:
            lag = sessions.index(expected_session) - sessions.index(last_bar)
        elif last_bar < expected_session:
            lag = -1                      # a gap exists but its size is unknown
    if lag is not None and lag != 0:
        if is_israeli(symbol):
            flags.append(f"TASE calendar lag (last bar {last_bar} vs session {expected_session}) "
                         f"— expected for .TA, not stale")
        elif lag >= STALE_HARD_SESSIONS:
            hard = (f"stale bars: last bar {last_bar} is {lag} sessions behind "
                    f"session {expected_session}")
        else:
            flags.append(f"last bar {last_bar} lags session {expected_session} "
                         f"({'unknown gap' if lag < 0 else str(lag) + ' session(s)'})")

    info_gate = gate(0, "Data integrity (info only)", "GO" if not flags else "SKIP",
                     f"bars={bars} | last_bar={last_bar} | session={expected_session} | "
                     f"history={hist_src} | " + ("; ".join(flags) if flags else "clean"),
                     bars, "bar count / NaN / positive price / bar freshness (informational)")
    return hard, info_gate

# ----------------------------------------------------------------------------------
# BENCHMARK / REGIME RESOLUTION  [unchanged]
# ----------------------------------------------------------------------------------
def get_benchmark_close(symbol, bench_cache):
    if symbol not in bench_cache:
        bench_cache[symbol] = safe_call(
            lambda: _naive_index(yf.Ticker(symbol).history(period="6mo")["Close"].dropna()),
            what=f"benchmark history {symbol}", default=None)
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
        etf_cache[etf_symbol] = safe_call(
            lambda: yf.Ticker(etf_symbol).history(period="2y")["Close"].dropna(),
            what=f"regime history {etf_symbol}", default=pd.Series(dtype=float))
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
# CORE GATES
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

# ---- v2.9 ------------------------------------------------------------------------
def detect_bounce_date(df, lookback=10):
    """v2.9 — the session on which the current reversal started: the lowest LOW of the
    last `lookback` bars. Published so the LLM can verify WHAT happened that day
    (earnings / guidance / disclosure = re-rating ; nothing = mean-reversion noise)."""
    try:
        seg = df["Low"].iloc[-lookback:]
        idx = seg.idxmin()
        return str(pd.to_datetime(idx).date()), float(seg.loc[idx])
    except Exception:
        return None, None

def gate_concrete_floor(df, bounce_date=None):
    """PATH C — unchanged v2.8 logic and criterion (kept for continuity).
    Returns (main_gate, sub_gates, ctx) where ctx feeds Path E."""
    close = df["Close"]
    on_floor, floor_reason = detect_floor(df)
    rsi_div = detect_rsi_divergence(df)
    vclimax, vmult = detect_volume_climax(df)
    confirm = rsi_div or vclimax
    ema21_series = ema(close, EMA_FAST)
    ema21 = float(ema21_series.iloc[-1])
    last  = float(close.iloc[-1])
    broke_out = last > ema21
    bounce = last > float(df["Low"].iloc[-10:].min())
    ok = on_floor and bounce and confirm and broke_out
    conf_txt = []
    if rsi_div: conf_txt.append("RSI-Div")
    if vclimax: conf_txt.append(f"Vol climax {vmult}x")
    detail = (f"floor[{floor_reason}] | bounce={bounce} | "
              f"confirm[{','.join(conf_txt) or 'none'}] | "
              f"close {last:.2f} {'>' if broke_out else '<'} EMA21 {ema21:.2f}"
              f" | bounce_date={bounce_date}")
    main = gate(3, "Concrete floor + EMA21 breakout", "GO" if ok else "NO_GO",
                detail, None,
                "on floor (Fib .5/.618 OR 2x support) + bounce + (RSI-Div OR Vol climax) + close>EMA21")
    sub = [
        gate(3, "Bullish RSI divergence", "GO" if rsi_div else "SKIP",
             "informational sub-signal", bool(rsi_div), "price LL & RSI HL"),
        gate(3, "Volume climax", "GO" if vclimax else "SKIP",
             "informational sub-signal", vmult, ">= 2x avg volume"),
    ]
    ctx = {"on_floor": on_floor, "floor_reason": floor_reason, "rsi_div": rsi_div,
           "vmult": vmult, "ema21": ema21, "close": last, "bounce": bounce,
           "path_c": ok, "broke_out": broke_out}
    return main, sub, ctx

def gate_early_entry(ctx):
    """v2.9 PATH E — buy AT the floor, before the EMA21 reclaim.

    Rationale: Path C demands 'on the floor' and 'closing above EMA21' on the SAME session.
    In a V-shaped reversal those two are mutually exclusive, so the scanner structurally
    misses the bottom and can only ever chase (NOW, MSFT: 'no floor near price').

    Path E drops ONLY the EMA21-reclaim requirement. Everything that makes the setup a
    reversal rather than a falling knife is kept, and three fences are added:
      * softer but still real climax (>=1.5x) OR an RSI divergence,
      * an EMA21 proximity band: no freefall (>= 0.85x) and no chase (<= 1.15x),
      * (outside this gate) the risk-geometry CORE gate and a MANDATORY LLM event anchor.
    A Path-E entry is UNCONFIRMED by construction and carries a 5-session time stop.
    """
    if ctx["path_c"]:
        return gate(3, "Early entry (Path E)", "SKIP",
                    "Path C already satisfied (confirmed entry) — Path E not needed", None,
                    "floor + bounce + soft confirm + EMA21 band, WITHOUT the EMA21 reclaim")

    ema21, last = ctx["ema21"], ctx["close"]
    ratio = (last / ema21) if ema21 else None
    soft_climax = (ctx["vmult"] is not None and ctx["vmult"] >= VOL_CLIMAX_EARLY)
    confirm_e = ctx["rsi_div"] or soft_climax
    in_band = (ratio is not None and EMA_BAND_BELOW <= ratio <= EMA_BAND_ABOVE)
    ok = ctx["on_floor"] and ctx["bounce"] and confirm_e and in_band

    conf_txt = []
    if ctx["rsi_div"]: conf_txt.append("RSI-Div")
    if soft_climax:    conf_txt.append(f"soft climax {ctx['vmult']}x")
    fails = []
    if not ctx["on_floor"]: fails.append("not on floor")
    if not confirm_e:       fails.append(f"no reversal confirm (vol {ctx['vmult']}x < {VOL_CLIMAX_EARLY}x, no RSI-Div)")
    if not in_band:
        if ratio is None:            fails.append("EMA21 unavailable")
        elif ratio > EMA_BAND_ABOVE: fails.append(f"CHASE: price {ratio:.2f}x EMA21 > {EMA_BAND_ABOVE}x")
        else:                        fails.append(f"FREEFALL: price {ratio:.2f}x EMA21 < {EMA_BAND_BELOW}x")

    if ok:
        verdict_txt = ("ELIGIBLE — UNCONFIRMED entry, requires LLM event anchor + "
                       "{}-session time stop".format(PATH_E_TIME_STOP))
    else:
        verdict_txt = "blocked: " + "; ".join(fails)
    conf_str = ",".join(conf_txt) or "none"
    ratio_str = "{:.3f}".format(ratio) if ratio is not None else "n/a"
    detail = ("floor[{}] | confirm[{}] | price/EMA21 = {} (band {}-{}) | {}"
              .format(ctx["floor_reason"], conf_str, ratio_str,
                      EMA_BAND_BELOW, EMA_BAND_ABOVE, verdict_txt))
    return gate(3, "Early entry (Path E)", "GO" if ok else "NO_GO", detail,
                round(float(ratio), 3) if ratio else None,
                "floor + bounce + (RSI-Div OR >=1.5x vol) + 0.85 <= price/EMA21 <= 1.15, "
                "WITHOUT the EMA21 reclaim; requires LLM event anchor + time stop")

def gate_relative_strength(df, bench_close, bench_name=RS_BENCHMARK):
    """v2.9 DUAL-MODE. Passes on the single worst-red-day test (v2.8) OR on rolling
    20-session outperformance vs the benchmark (>= +2%).

    Why: a one-day sample is noise, not strength. ORCL cleared 10 of 13 gates and was
    rejected solely for a -1.85% print on 2026-07-29, then rallied +20.45% in five sessions.
    The rolling leg measures persistent leadership; the +2% floor stops a broad bull market
    from waving everything through."""
    if bench_close is None or len(bench_close) < 30:
        return gate(4, "Relative Strength", "SKIP", f"{bench_name} history unavailable", None,
                    f"red-day test OR 20d excess return >= +2% vs {bench_name}")

    # ---- leg A: worst red day (v2.8) ----
    bret = bench_close.pct_change()
    worst_day = bret.iloc[-30:].idxmin()
    stock_move, bench_move, leg_a_ok, leg_a_txt = None, None, False, "no aligned red-day bar"
    if worst_day in df.index:
        stock_move = float(df["Close"].pct_change().loc[worst_day])
    else:
        common = df["Close"].reindex(bench_close.index).dropna()
        sret = common.pct_change()
        if worst_day in sret.index:
            stock_move = float(sret.loc[worst_day])
    if stock_move is not None:
        bench_move = float(bret.loc[worst_day])
        leg_a_ok = stock_move >= RS_TOLERANCE
        leg_a_txt = (f"red day {worst_day.date()} {bench_move*100:.2f}% -> "
                     f"stock {stock_move*100:.2f}% [{'PASS' if leg_a_ok else 'fail'}]")

    # ---- leg B: rolling 20-session excess return (v2.9) ----
    leg_b_ok, excess, leg_b_txt = False, None, "insufficient aligned history"
    try:
        aligned = pd.concat([df["Close"], bench_close], axis=1, join="inner").dropna()
        if len(aligned) > RS_ROLL_WINDOW:
            s = aligned.iloc[:, 0]; b = aligned.iloc[:, 1]
            s_ret = float(s.iloc[-1] / s.iloc[-1 - RS_ROLL_WINDOW] - 1)
            b_ret = float(b.iloc[-1] / b.iloc[-1 - RS_ROLL_WINDOW] - 1)
            excess = s_ret - b_ret
            leg_b_ok = excess >= RS_ROLL_MIN_EXCESS
            leg_b_txt = (f"{RS_ROLL_WINDOW}d stock {s_ret*100:+.1f}% vs {bench_name} {b_ret*100:+.1f}% "
                         f"= excess {excess*100:+.1f}% [{'PASS' if leg_b_ok else 'fail'}]")
    except Exception as e:
        leg_b_txt = f"rolling RS error ({e})"

    ok = leg_a_ok or leg_b_ok
    mode = "red-day" if leg_a_ok else ("rolling-20d" if leg_b_ok else "none")
    return gate(4, "Relative Strength", "GO" if ok else "NO_GO",
                f"A) {leg_a_txt} | B) {leg_b_txt} | passed_via={mode}",
                round(stock_move, 4) if stock_move is not None else None,
                f"red-day test OR {RS_ROLL_WINDOW}d excess return >= "
                f"+{RS_ROLL_MIN_EXCESS*100:.0f}% vs {bench_name}")

def gate_hard_stop(df):
    low10 = df["Low"].iloc[-10:].min()
    stop = round(float(low10) * STOP_MULT, 2)
    return gate(5, "Hard stop price", "GO",
                f"10d low {low10:.2f} x {STOP_MULT}", stop, "10d-low * 0.985")

def gate_risk_geometry(price, stop):
    """v2.9 NEW CORE (rejecting). With a FIXED allocation, the distance to the hard stop IS
    the position's risk. A +20% target is only worth taking when the downside is materially
    smaller than the upside.

    Evidence from the live ledger: every wide-stop entry underperformed
    (DHR 16.9% risk -> +0.5%, ABT 14.3% -> +6.4%, TMO 12.9% -> +2.6%) while every tight-stop
    entry led (LMT 7.5% -> +12.4%, BMY 8.5% -> +13.8%). A late chase is mathematically bad:
    ORCL at 141.85 with a 112.78 stop risks 20.5% to make 20% (reward/risk 0.98:1); the same
    name bought at its floor risks 8.3% (2.4:1). This gate forces the good geometry."""
    if not price or not stop or price <= 0:
        return gate(5, "Risk geometry (stop distance)", "SKIP", "price/stop unavailable", None,
                    f"(price-stop)/price <= {MAX_RISK_PCT*100:.0f}%")
    risk = (price - stop) / price
    ok = 0 < risk <= MAX_RISK_PCT
    if risk > 0:
        detail = ("stop {:.2f} is {:.1f}% below price {:.2f} (max {:.0f}%) | "
                  "reward/risk vs +20% target = {:.2f}:1"
                  ).format(stop, risk * 100, price, MAX_RISK_PCT * 100, 0.20 / risk)
    else:
        detail = "invalid stop ({:.2f}) >= price ({:.2f}) — suspect data".format(stop, price)
    return gate(5, "Risk geometry (stop distance)", "GO" if ok else "NO_GO", detail,
                round(float(risk), 4),
                "(price-stop)/price <= {:.0f}%".format(MAX_RISK_PCT * 100))

# ----------------------------------------------------------------------------------
# SCORE GATES (rank only)  [unchanged]
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
        cf = safe_call(lambda: tk.cashflow, what="cashflow", default=None)
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
        cal = safe_call(lambda: tk.calendar, what="calendar", default=None)
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
        if isinstance(ed, dt.datetime):   ed_date = ed.date()
        elif isinstance(ed, dt.date):     ed_date = ed
        else:                             ed_date = pd.to_datetime(ed).date()
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
def ai_gates(bounce_date=None, bounce_low=None, path_e=False):
    low_txt = "n/a" if bounce_low is None else "{:.2f}".format(bounce_low)
    if path_e:
        anchor_rule = ("MANDATORY — this ticker is a PATH E (unconfirmed, pre-EMA21) candidate: "
                       "NO cited dated event => REFUSE the entry.")
    else:
        anchor_rule = "Informational for a Path C entry."
    anchor_detail = (
        "AI: the reversal started on bounce_date={} (pivot low {}). "
        "Find what happened WITHIN 3 SESSIONS of that date: earnings / guidance / a company "
        "disclosure / a regulatory or contract event. A bounce anchored to a dated, cited event "
        "is a re-rating; a bounce anchored to nothing is mean-reversion noise. {}"
    ).format(bounce_date, low_txt, anchor_rule)

    g = [
        gate(2, "Rule-of-40 / RPO decoupling", "NEEDS_LLM",
             "AI: rev growth + margin > 40%? RPO growing YoY? document price/perf decoupling (cite 10-Q/10-K)",
             None, "Rule of 40 true AND RPO up YoY"),
        gate(3, "Insider buying (Form 4)", "NEEDS_LLM",
             "AI: search FRESH Form 4 insider BUYS only (NOT 13F)", None,
             "recent insider buying = confirming signal"),
        gate(1, "Disruption test", "NEEDS_LLM",
             "AI: direct threat=STRONG SELL | adapting w/ AI=BUY\u26a0\ufe0f | infra/data moat=clean",
             None, "no direct 5y replacement threat"),
        # ---- v2.9 -------------------------------------------------------------
        gate(1, "Disruption quality (WARN test)", "NEEDS_LLM",
             "AI: a \u26a0\ufe0f WARN-level disruption is ONLY acceptable while the fundamentals are "
             "ACCELERATING. Active competitive displacement + DECELERATING growth = HARD REJECT "
             "(do not log, do not hold). Cite the two most recent quarters of the key growth "
             "metric and state accelerating/decelerating. Archetype: ISRG (Hugo + Ottava entering "
             "AND procedure growth decelerating) = reject; BMY (Eliquis cliff BUT growth portfolio "
             "accelerating) and LMT (drone threat BUT record backlog) = keep.",
             None, "WARN disruption + decelerating fundamentals => reject"),
        gate(3, "Bounce catalyst anchor (Path E)", "NEEDS_LLM", anchor_detail,
             bounce_date, "Path E requires a dated, cited event within 3 sessions of bounce_date"),
        gate(2, "Devil's advocate", "NEEDS_LLM",
             "AI MUST write 2 concrete, evidenced bear reasons. NO OUTPUT without it.",
             None, "2 concrete crash reasons required"),
        gate(2, "Data healing (PEG/FCF)", "NEEDS_LLM",
             "AI: if survivor has SKIP on PEG/FCF, web-fetch real value. Fwd PEG; Yahoo>SeekingAlpha>StockAnalysis; "
             "tag '(AI-healed, source, date)'; never guess; survivors only.",
             None, "heal SKIP PEG/FCF with cited value"),
    ]
    return g

# ----------------------------------------------------------------------------------
# PER-TICKER PIPELINE
# ----------------------------------------------------------------------------------
def scan_ticker(symbol, etf_cache, bench_cache, universe_source,
                hist_cache=None, expected_session=None, sessions=None):
    item = {"ticker": symbol, "price": None, "verdict": "NO_GO",
            "go_count": 0, "blocked_at": None,
            # ---- v2.9 convenience fields (static values, no formulas) ----
            "entry_path": None, "stop": None, "risk_pct": None, "target_20pct": None,
            "bounce_date": None, "time_stop_sessions": None,
            "requires_llm_confirmation": False, "recent_bars": None, "bars_window": RECENT_BARS,
            "universe_source": universe_source, "gates": []}
    try:
        tk = yf.Ticker(symbol)

        # ----- v3.5 (1): batch first, per-ticker fallback (batching cannot drop a ticker) -----
        df, hist_src = None, "none"
        if hist_cache is not None and symbol in hist_cache:
            df, hist_src = hist_cache[symbol].copy(), "batch"
        if df is None or len(df) < MIN_BARS:
            single = safe_call(lambda: _naive_index(tk.history(period=HISTORY_PERIOD).dropna()),
                               what=f"history {symbol}", default=None, allow_empty=False)
            if single is not None and (df is None or len(single) > len(df)):
                df, hist_src = single, ("single" if hist_src == "none" else "single(after batch)")
        if df is None or df.empty:
            item["blocked_at"] = "data: history unavailable after retries"
            item["gates"].append(gate(0, "Data availability", "NO_GO",
                                      "no daily bars returned (batch + per-ticker retries failed)",
                                      0, f">={MIN_BARS} daily bars"))
            return item

        # ----- v3.5 (4): data integrity BEFORE any gate math -----
        hard, g_integrity = check_data_integrity(symbol, df, expected_session, sessions, hist_src)
        if hard:
            item["blocked_at"] = f"data: {hard}"
            item["gates"].append(gate(0, "Data availability", "NO_GO", hard, len(df),
                                      f">={MIN_BARS} clean, fresh daily bars"))
            item["gates"].append(g_integrity)
            return item

        # ----- v2.5: CURE LATENCY BLINDNESS -----
        df, live_status, live_note = apply_live_last_price(df, tk)

        info = safe_call(lambda: tk.info or {}, what=f"info {symbol}", default={}) or {}
        item["price"] = round(float(df["Close"].iloc[-1]), 2)   # native units (USD, or AGOROT for .TA)

        # ----- v3.5: publish the recent daily bars so the ledger can simulate an INTRADAY stop.
        # The close-only exit model silently deleted every intraday stop breach that recovered by
        # the bell. High/Low are already in hand — the ledger just never got to see them.
        # Same units as `price` (AGOROT for .TA). Oldest first.
        item["recent_bars"] = [
            [str(pd.to_datetime(i).date()),
             round(float(r["Open"]), 2), round(float(r["High"]), 2),
             round(float(r["Low"]), 2),  round(float(r["Close"]), 2)]
            for i, r in df.iloc[-RECENT_BARS:].iterrows()
        ]

        gates = []

        # ----- v2.7: benchmark & regime ETF -----
        israeli = is_israeli(symbol)
        bench_symbol = ETF_ISRAEL if israeli else RS_BENCHMARK
        bench_close  = get_benchmark_close(bench_symbol, bench_cache)
        etf          = classify_regime_etf(symbol, info)
        g_regime = gate_regime(etf, etf_cache)

        # ----- v2.9: bounce date (published for the LLM event anchor) -----
        bounce_date, bounce_low = detect_bounce_date(df)
        item["bounce_date"] = bounce_date

        # ----- CORE -----
        g_vol            = gate_volatility(df)
        g_floor, g_subs, ctx = gate_concrete_floor(df, bounce_date)
        g_early          = gate_early_entry(ctx)                       # v2.9
        g_rs             = gate_relative_strength(df, bench_close, bench_symbol)
        g_stop           = gate_hard_stop(df)
        g_risk           = gate_risk_geometry(item["price"], g_stop["value"])   # v2.9

        item["stop"] = g_stop["value"]
        item["risk_pct"] = g_risk["value"]
        item["target_20pct"] = round(item["price"] * 1.20, 2)

        # ----- SCORE (rank only) -----
        g_peg = gate_peg(info)
        g_fcf = gate_fcf(tk)
        g_cat = gate_catalyst(tk)

        # informational
        gates.append(gate_currency(symbol, item["price"]))
        sector = info.get("sector") or ("Financials (TASE)" if israeli else "unknown")
        gates.append(gate(1, "Sector (info only)", "GO",
                          f"{sector} -> regime ETF {etf}", sector, "informational, not a filter"))
        gates.append(gate(0, "Live price (fast_info)", live_status, live_note,
                          item["price"], "patch lagged daily bar with live price"))
        gates.append(g_integrity)

        gates += [g_regime, g_vol, g_floor] + g_subs + [g_early, g_rs, g_peg, g_fcf, g_cat,
                                                        g_stop, g_risk]

        # ----- v2.9 VERDICT ---------------------------------------------------
        # Entry trigger = Path C (confirmed) OR Path E (early, unconfirmed).
        # CORE rejecting set = Volatility | entry trigger | Relative Strength | Risk geometry.
        # Regime / PEG / FCF / Catalyst NO_GO never reject (rank-only), exactly as before.
        path_c = (g_floor["status"] == "GO")
        path_e = (g_early["status"] == "GO")
        entry_path = "C" if path_c else ("E" if path_e else None)

        gates += ai_gates(bounce_date, bounce_low, path_e=path_e)
        item["gates"] = gates

        blocker = None
        if g_vol["status"] == "NO_GO":
            blocker = "Volatility (Upside DNA)"
        elif entry_path is None:
            # keep the legacy name when Path C is the reason, so older tooling still reads it
            blocker = ("Concrete floor + EMA21 breakout"
                       if g_early["status"] == "SKIP" else
                       "Entry trigger (Path C + Path E both failed)")
        elif g_rs["status"] == "NO_GO":
            blocker = "Relative Strength"
        elif g_risk["status"] == "NO_GO":
            blocker = "Risk geometry (stop distance)"

        if blocker:
            item["verdict"] = "NO_GO"
            item["blocked_at"] = blocker
        else:
            item["verdict"] = "GO_PENDING_THESIS"
            item["blocked_at"] = None
            item["entry_path"] = entry_path
            if entry_path == "E":
                item["time_stop_sessions"] = PATH_E_TIME_STOP
                item["requires_llm_confirmation"] = True

        # v3.5: the new informational integrity gate is EXCLUDED from go_count, so the score
        # stays on the v3.0 scale (x/13) and remains comparable with every historical run.
        item["go_count"] = sum(1 for g in gates
                               if g["status"] == "GO" and g["name"] != "Data integrity (info only)")
        return item
    except Exception as e:
        item["blocked_at"] = f"error: {e}"
        item["gates"].append(gate(0, "Pipeline error", "NO_GO", str(e), None, ""))
        return item

# ----------------------------------------------------------------------------------
# v3.0 — TRADING-SESSION CALENDAR
# ----------------------------------------------------------------------------------
def build_session_calendar(bars=400):
    """
    v3.0 — publish the REAL US trading calendar so the LLM never counts sessions itself.

    Returns (sessions, last_session):
      sessions      ascending list of ISO dates that had an actual SPY bar, i.e. real
                    trading sessions. Holidays are not "filtered out" — they simply have
                    no bar, so they were never there. Nothing to maintain, ever.
      last_session  sessions[-1], the session the scan's prices belong to.

    Days Held for any ledger row then becomes a pure index subtraction:
        sessions.index(session_date) - sessions.index(entry_date)
    No holiday table, no weekend arithmetic, no counting by hand.

    Fully additive: on any failure it returns ([], None) and the scan continues
    untouched. This function CANNOT change which tickers survive.
    """
    try:
        hist = retry_call(lambda: yf.download("SPY", period="2y", progress=False,
                                              auto_adjust=False),
                          what="SPY session calendar")
        sessions = [d.date().isoformat() for d in hist.index][-bars:]
        return sessions, (sessions[-1] if sessions else None)
    except Exception as e:
        _log_error(f"[session-calendar] failed ({e}); publishing empty calendar.")
        return [], None


# ----------------------------------------------------------------------------------
# v3.6 — BENCHMARK BASELINE (book alpha)
# ----------------------------------------------------------------------------------
def build_benchmark_block():
    """
    v3.6 — publish the book's benchmark so "did we beat the market" is a FACT in the JSON
    rather than a number someone has to look up. Absolute return in a rising market is not
    evidence of skill; this is the field that turns +4.9% into beat-or-miss.

    Uses SPY's DIVIDEND-ADJUSTED closes (auto_adjust=True), i.e. TOTAL return. A price-only
    comparison silently flatters the book by the benchmark's dividend yield for the period.

    base_session is the first REAL trading session on/after BENCHMARK_BASE_DATE — the same
    "first session >= date" rule the ledger uses for Days Held, so a capital start date that
    landed on a weekend resolves identically on both sides.

    Fully additive: on ANY failure it returns None, the key is published as null, and the
    scan is untouched. This function CANNOT change which tickers survive.
    """
    try:
        h = retry_call(lambda: yf.Ticker(RS_BENCHMARK).history(period="2y", auto_adjust=True),
                       what=f"benchmark baseline {RS_BENCHMARK}")
        close = h["Close"].dropna()
        if close.empty:
            raise ValueError("empty benchmark history")
        dates = [d.date().isoformat() for d in close.index]
        base_i = next((i for i, d in enumerate(dates) if d >= BENCHMARK_BASE_DATE), None)
        if base_i is None:
            raise ValueError(f"no session on/after {BENCHMARK_BASE_DATE}")
        base_close, last_close = float(close.iloc[base_i]), float(close.iloc[-1])
        if base_close <= 0:
            raise ValueError("non-positive base close")
        return {
            "ticker": RS_BENCHMARK,
            "base_date_requested": BENCHMARK_BASE_DATE,
            "base_session": dates[base_i],
            "base_close": round(base_close, 2),
            "last_session": dates[-1],
            "last_close": round(last_close, 2),
            "return_pct": round(last_close / base_close - 1.0, 4),
            "basis": "dividend-adjusted closes (total return)",
        }
    except Exception as e:
        _log_error(f"[benchmark] baseline unavailable ({e}); publishing null.")
        return None


# ----------------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------------
def _write_run_status(payload):
    try:
        RUN_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUN_STATUS_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        print(f"[run-status] write failed: {e}")

def _is_data_failure(item):
    b = item.get("blocked_at") or ""
    return b.startswith("data:") or b.startswith("error:")


def main():
    yf_version = getattr(yf, "__version__", "unknown")
    print(f"scan.py v{SCANNER_VERSION} | yfinance {yf_version} | pandas {pd.__version__}")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # v3.0 calendar + v2.6 HOLIDAY / WEEKEND GUARD, now from ONE SPY download instead of two.
    sessions_cal, last_session = build_session_calendar()
    print(f"SESSION CALENDAR: {len(sessions_cal)} sessions, last={last_session}")
    if last_session is None:
        print("[holiday-guard] SPY session check unavailable; proceeding with scan.")
    elif last_session != dt.date.today().isoformat():
        if not FORCE_RUN:
            print(f"[!] NYSE Closed Today (last valid session: {last_session}). "
                  f"Skipping out.json overwrite.")
            return 0
        print("=" * 78)
        print(f"[TEST RUN] market closed, guard BYPASSED via SCAN_FORCE_RUN=1.")
        print(f"[TEST RUN] prices are the {last_session} closes — plumbing test only, "
              f"NOT a trading signal.")
        print(f"[TEST RUN] writing to {RESULTS_DIR}/ — do not commit this to results/.")
        print("=" * 78)

    tickers, universe_source, universe_kind = fetch_universe()
    print(f"UNIVERSE {{count: {len(tickers)}, kind: {universe_kind}, source: {universe_source}}}")

    # v3.6 — benchmark baseline for the book's alpha line (additive, cannot affect survivors).
    bench_block = build_benchmark_block()
    if bench_block:
        print(f"BENCHMARK {bench_block['ticker']}: {bench_block['base_session']} "
              f"{bench_block['base_close']} -> {bench_block['last_session']} "
              f"{bench_block['last_close']} = {bench_block['return_pct']*100:+.2f}% "
              f"({bench_block['basis']})")
    else:
        print("BENCHMARK: unavailable — published as null")

    etf_cache = {}
    bench_cache = {}
    get_benchmark_close(RS_BENCHMARK, bench_cache)

    # v3.5 (1): one batched history pass for the whole universe.
    hist_cache = {}
    if USE_BATCH_HISTORY:
        hist_cache = download_history_batch(tickers)
        print(f"BATCH HISTORY: {len(hist_cache)}/{len(tickers)} symbols pre-loaded")

    results = []
    for i, sym in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {sym}")
        results.append(scan_ticker(sym, etf_cache, bench_cache, universe_source,
                                   hist_cache=hist_cache, expected_session=last_session,
                                   sessions=sessions_cal))
        time.sleep(TICKER_SLEEP)

    # ---- v3.5 (5): ABORT-ON-DEGRADATION, evaluated BEFORE anything is written ----
    n = len(results)
    failed = [r["ticker"] for r in results if _is_data_failure(r)]
    fail_reasons = {r["ticker"]: r["blocked_at"] for r in results if _is_data_failure(r)}
    fail_pct = (len(failed) / n) if n else 1.0
    coverage_pct = round(1.0 - fail_pct, 4)

    # ---- v3.6 GUARD: Relative-Strength INDEX ALIGNMENT ----
    # "no aligned red-day bar" can only mean the benchmark's worst-red-day timestamp was not
    # found in the stock's index AT ALL — a structural alignment fault, never a market outcome.
    # A legitimate market failure reads "red day <date> ... [fail]". If this fires across the
    # universe, every survivor list from the run is meaningless, so it must abort rather than
    # publish a confident zero. This is the guard the v3.5 timezone bug had no way to trip.
    def _rs_unaligned(r):
        for g in r.get("gates", []):
            if g.get("name") == "Relative Strength":
                d = g.get("detail") or ""
                return ("no aligned red-day bar" in d) and ("passed_via=none" in d)
        return False

    rs_unaligned = [r["ticker"] for r in results if _rs_unaligned(r)]
    rs_align_pct = (len(rs_unaligned) / n) if n else 0.0
    rs_align_fault = rs_align_pct > RS_ALIGN_MAX_PCT
    if rs_align_fault:
        _log_error(f"[rs-alignment] {len(rs_unaligned)}/{n} tickers could not align to the "
                   f"benchmark index ({rs_align_pct*100:.1f}% > {RS_ALIGN_MAX_PCT*100:.0f}%) "
                   f"— index/timezone mismatch, NOT market conditions.")

    degraded = (fail_pct > ABORT_MAX_FAIL_PCT) or rs_align_fault

    run_health = {
        "scanner_version": SCANNER_VERSION,
        "generated_utc": _utc_now_str(),
        "session_date": last_session,
        "universe_kind": universe_kind,
        "universe_source": universe_source,
        "n": n,
        "n_data_failures": len(failed),
        "coverage_pct": coverage_pct,
        "abort_max_fail_pct": ABORT_MAX_FAIL_PCT,
        "degraded_run": bool(degraded),
        "degradation_reason": ("rs_index_alignment" if rs_align_fault
                              else ("data_failures" if fail_pct > ABORT_MAX_FAIL_PCT else None)),
        "rs_unaligned": len(rs_unaligned),
        "rs_alignment_fault": bool(rs_align_fault),
        "batch_history_hits": len(hist_cache),
        "n_errors": len(RUN_ERRORS),
        "failures": fail_reasons,
        "errors": RUN_ERRORS[:40],
        "yfinance_version": yf_version,
        "test_run": bool(FORCE_RUN),
        "output_dir": str(RESULTS_DIR),
    }

    if degraded:
        run_health["action"] = ("ABORTED — results/ NOT overwritten; the previous committed scan "
                                "remains authoritative. Do not trade on this run.")
        _write_run_status(run_health)
        print("=" * 78)
        print(f"[ABORT] {len(failed)}/{n} tickers failed ({fail_pct*100:.1f}% > "
              f"{ABORT_MAX_FAIL_PCT*100:.0f}%). results/ was NOT overwritten.")
        print(f"[ABORT] failures: {', '.join(failed[:25])}{' ...' if len(failed) > 25 else ''}")
        print("=" * 78)
        return 1

    # rank: survivors first, Path C before Path E, then by go_count   [unchanged]
    results.sort(key=lambda x: (x["verdict"] != "GO_PENDING_THESIS",
                                {"C": 0, "E": 1}.get(x.get("entry_path"), 2),
                                -x["go_count"]))
    survivors    = [r["ticker"] for r in results if r["verdict"] == "GO_PENDING_THESIS"]
    survivors_c  = [r["ticker"] for r in results if r.get("entry_path") == "C"]
    survivors_e  = [r["ticker"] for r in results if r.get("entry_path") == "E"]
    print(f"SURVIVORS {survivors}  (PathC={survivors_c} PathE={survivors_e})")

    OUT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"WROTE {OUT_PATH} ({len(results)} items, {len(survivors)} survivors)")

    # -------- v2.8 COMPANION FILES (browse-friendly), extended for v2.9 / v3.0 / v3.5 --------
    results_dir = OUT_PATH.parent
    (results_dir / "tickers").mkdir(parents=True, exist_ok=True)

    summary = {
        "generated_utc": run_health["generated_utc"],
        "scanner_version": SCANNER_VERSION,
        # ---- v3.0: trading-session calendar (Days Held = index subtraction) ----
        "session_date": last_session,
        "session_index": (len(sessions_cal) - 1) if sessions_cal else None,
        "sessions": sessions_cal,
        "universe_source": results[0]["universe_source"] if results else None,
        "n": len(results),
        "survivors": survivors,
        "survivors_path_c": survivors_c,
        "survivors_path_e": survivors_e,
        "params": {"MAX_RISK_PCT": MAX_RISK_PCT, "EMA_BAND": [EMA_BAND_BELOW, EMA_BAND_ABOVE],
                   "RS_ROLL_WINDOW": RS_ROLL_WINDOW, "RS_ROLL_MIN_EXCESS": RS_ROLL_MIN_EXCESS,
                   "PATH_E_TIME_STOP": PATH_E_TIME_STOP},
        # ---- v3.6: benchmark baseline, so the agent can state alpha, not just return ----
        "benchmark": bench_block,
        # ---- v3.5: run health, for the agent's Data-Health block ----
        "run_health": run_health,
        "stocks": [
            {"ticker": r["ticker"], "price": r["price"], "verdict": r["verdict"],
             "go_count": r["go_count"], "blocked_at": r["blocked_at"],
             "entry_path": r.get("entry_path"), "stop": r.get("stop"),
             "risk_pct": r.get("risk_pct"), "bounce_date": r.get("bounce_date")}
            for r in results
        ],
    }
    (results_dir / "out_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8")

    (results_dir / "out_survivors.json").write_text(
        json.dumps([r for r in results if r["verdict"] == "GO_PENDING_THESIS"],
                   indent=2, default=str), encoding="utf-8")

    for r in results:
        safe = r["ticker"].replace("/", "_")
        (results_dir / "tickers" / f"{safe}.json").write_text(
            json.dumps(r, indent=2, default=str), encoding="utf-8")

    run_health["action"] = "OK — results/ overwritten."
    _write_run_status(run_health)

    print(f"WROTE companion files: out_summary.json, out_survivors.json, run_status.json, "
          f"tickers/*.json ({len(results)} ticker files)")
    print(f"RUN HEALTH: coverage {coverage_pct*100:.1f}% | data failures {len(failed)}/{n} | "
          f"soft errors {len(RUN_ERRORS)} | universe {universe_kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
