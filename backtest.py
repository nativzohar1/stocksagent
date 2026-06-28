#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py — Decoupling Hunter TECHNICAL backtest v2.7.1 "Goldilocks".
Runs on GitHub Actions (needs internet). Imports the EXACT gate functions from scan.py and
replays them POINT-IN-TIME over [START, END].

Config (v2.7.1):
  - REGIME_FILTER = True   -> hard NO_GO unless SPY > 200SMA (no buying in a bear market).
  - CONFLUENCE_MODE = "OR" -> entry needs (Close>EMA21) AND (Floor) AND (RSI-Div OR Vol-Climax).
                             (This is what scan.gate_concrete_floor already enforces for a GO.)
  - Earnings Blackout (<=14d) + holiday-glitch ffill fix: ACTIVE (from v2.6).
"""
import numpy as np, pandas as pd, yfinance as yf
import scan   # single source of truth — same gates as production

START, END         = "2025-01-01", "2026-06-28"
LOOKBACK_START     = "2023-12-01"
WIN_DAYS           = 252
BENCH_DAYS         = 126
TARGET             = 0.25
RISK_PCT           = 0.01
MAX_POS            = 3
START_EQUITY       = 100_000
COST_BPS           = 5
MIN_BARS           = 200
EARN_BLACKOUT_DAYS = 14
REGIME_FILTER      = True          # hard: require SPY > 200SMA to enter
REGIME_SMA         = 200
CONFLUENCE_MODE    = "OR"          # v2.7.1: "OR" = RSI-Div OR Vol-Climax (Goldilocks)
RS_BENCH           = "SPY"
UNIVERSE           = scan.SP100_PARTIAL_FALLBACK


def download(sym):
    try:
        df = yf.Ticker(sym).history(start=LOOKBACK_START, end=END).dropna()
        if len(df) < MIN_BARS:
            return None
        df.index = df.index.tz_localize(None)
        return df
    except Exception:
        return None


def load_earnings(sym):
    try:
        e = yf.Ticker(sym).get_earnings_dates(limit=40)
        if e is None or len(e) == 0:
            return []
        idx = e.index
        if idx.tz is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
        return sorted(pd.Timestamp(d).normalize() for d in idx)
    except Exception:
        return []


def evaluate(df_win, bench_win):
    """Returns (survivor, go_count, stop, rsi_div_go, vclimax_go)."""
    gv = scan.gate_volatility(df_win)
    gf, sub = scan.gate_concrete_floor(df_win)
    gr = scan.gate_relative_strength(df_win, bench_win)
    gs = scan.gate_hard_stop(df_win)
    survivor = all(g["status"] != "NO_GO" for g in (gv, gf, gr)) and gf["status"] == "GO"
    gocount = sum(1 for g in (gv, gf, gr) if g["status"] == "GO")
    rsi_div_go = sub[0]["status"] == "GO"   # Bullish RSI divergence
    vclimax_go = sub[1]["status"] == "GO"   # Volume climax
    return survivor, gocount, gs["value"], rsi_div_go, vclimax_go


def main():
    print(f"Downloading {len(UNIVERSE)} tickers + {RS_BENCH} ...")
    bench = yf.Ticker(RS_BENCH).history(start=LOOKBACK_START, end=END)["Close"].dropna()
    bench.index = bench.index.tz_localize(None)
    data = {s: d for s in UNIVERSE if (d := download(s)) is not None}
    print(f"Loaded {len(data)} tickers. Fetching earnings calendars ...")
    earnings = {s: load_earnings(s) for s in data}
    print(f"Earnings loaded. Regime filter={REGIME_FILTER} | Confluence={CONFLUENCE_MODE}")

    def in_blackout(sym, day):
        for ed in earnings.get(sym, []):
            if 0 <= (ed - day).days <= EARN_BLACKOUT_DAYS:
                return True
        return False

    all_days = sorted(set().union(*[set(df.loc[START:END].index) for df in data.values()]))
    panel = pd.concat({sym: df["Close"] for sym, df in data.items()}, axis=1).reindex(all_days).ffill()

    cash, positions, trades, curve = START_EQUITY, {}, [], []
    blocked_earnings = blocked_confluence = regime_blocked_days = 0

    for day in all_days:
        # ---- EXITS (bracket, gap-aware) ----
        for sym in list(positions):
            df = data[sym]
            if day not in df.index:
                continue
            o, hi, lo = df.loc[day, ["Open", "High", "Low"]]
            p = positions[sym]
            px = None
            if lo <= p["stop"]:
                px = min(o, p["stop"])
            elif hi >= p["target"]:
                px = max(o, p["target"])
            if px is not None:
                fill = px * (1 - COST_BPS / 1e4)
                cash += p["qty"] * fill
                trades.append({**p, "exit_date": day.date(), "exit": round(fill, 2),
                               "pnl": round(p["qty"] * (fill - p["entry"]), 2),
                               "ret": round(fill / p["entry"] - 1, 4)})
                del positions[sym]

        # ---- REGIME GATE (point-in-time SPY > 200SMA) ----
        prev = day - pd.Timedelta(days=1)
        regime_ok = True
        if REGIME_FILTER:
            bh = bench.loc[:prev]
            regime_ok = (len(bh) >= REGIME_SMA) and (bh.iloc[-1] > bh.rolling(REGIME_SMA).mean().iloc[-1])

        # ---- ENTRIES ----
        slots = MAX_POS - len(positions)
        if slots > 0 and not regime_ok:
            regime_blocked_days += 1
        if slots > 0 and regime_ok:
            cands = []
            for sym, df in data.items():
                if sym in positions or day not in df.index:
                    continue
                hist = df.loc[:prev]
                if len(hist) < MIN_BARS:
                    continue
                df_win = hist.tail(WIN_DAYS)
                bench_win = bench.loc[:prev].tail(BENCH_DAYS)
                ok, gc, stop, rdiv, vclmx = evaluate(df_win, bench_win)
                if not (ok and stop and stop < df_win["Close"].iloc[-1]):
                    continue
                conf = (rdiv and vclmx) if CONFLUENCE_MODE == "AND" else (rdiv or vclmx)
                if not conf:
                    blocked_confluence += 1
                    continue
                if in_blackout(sym, day):
                    blocked_earnings += 1
                    continue
                cands.append((gc, sym, df_win["Close"].iloc[-1], stop))
            cands.sort(reverse=True)
            equity = cash + sum(p["qty"] * panel.at[day, s] for s, p in positions.items())
            for gc, sym, sig_close, stop in cands[:slots]:
                entry = data[sym].loc[day, "Open"] * (1 + COST_BPS / 1e4)
                if entry <= stop:
                    continue
                qty = int((equity * RISK_PCT) / (entry - stop))
                if qty < 1 or qty * entry > cash:
                    continue
                cash -= qty * entry
                positions[sym] = {"ticker": sym, "entry_date": day.date(), "entry": round(entry, 2),
                                  "qty": qty, "stop": round(stop, 2),
                                  "target": round(sig_close * (1 + TARGET), 2)}

        mtm = cash + sum(p["qty"] * panel.at[day, s] for s, p in positions.items())
        curve.append({"date": day.date(), "equity": round(mtm, 2)})

    # ---- METRICS ----
    eq = pd.DataFrame(curve)
    tr = pd.DataFrame(trades)
    total_ret = eq["equity"].iloc[-1] / START_EQUITY - 1
    dd = (eq["equity"] / eq["equity"].cummax() - 1).min()
    spy_window = bench.loc[bench.index >= START]
    spy_ret = spy_window.iloc[-1] / spy_window.iloc[0] - 1
    if len(tr):
        win = (tr["pnl"] > 0).mean()
        losses = abs(tr.loc[tr.pnl < 0, "pnl"].sum())
        pf = (tr.loc[tr.pnl > 0, "pnl"].sum() / losses) if losses else float("inf")
        avg_hold = (pd.to_datetime(tr.exit_date) - pd.to_datetime(tr.entry_date)).dt.days.mean()
        expectancy = tr["pnl"].mean()
    else:
        win = pf = avg_hold = expectancy = float("nan")

    print("=" * 64)
    print(f"PERIOD {START} -> {END}   (v2.7.1 Goldilocks | Regime={REGIME_FILTER} | Confluence={CONFLUENCE_MODE})")
    print(f"Final equity: ${eq['equity'].iloc[-1]:,.0f}")
    print(f"Trades: {len(tr)} | Win%: {win:.0%} | Profit factor: {pf:.2f}")
    print(f"Expectancy/trade: ${expectancy:,.0f}")
    print(f"Avg hold: {avg_hold:.0f} days | Max drawdown: {dd:.1%}")
    print(f"Blocked -> earnings: {blocked_earnings} | confluence: {blocked_confluence} | regime-off days: {regime_blocked_days}")
    print(f"STRATEGY total return: {total_ret:.1%}")
    print(f"SPY buy&hold return : {spy_ret:.1%}")
    print("=" * 64)
    eq.to_csv("results/backtest_equity.csv", index=False)
    tr.to_csv("results/backtest_trades.csv", index=False)
    print("Wrote results/backtest_equity.csv + results/backtest_trades.csv")


if __name__ == "__main__":
    main()
