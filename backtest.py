#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py — Decoupling Hunter TECHNICAL backtest, 1-to-1 with scan.py.
"""
import numpy as np, pandas as pd, yfinance as yf
import scan   # single source of truth — same gates as production

START, END     = "2025-01-01", "2026-06-28"
LOOKBACK_START = "2023-12-01"
WIN_DAYS       = 252
BENCH_DAYS     = 126
TARGET         = 0.25
RISK_PCT       = 0.01
MAX_POS        = 3
START_EQUITY   = 100_000
COST_BPS       = 5
MIN_BARS       = 200
RS_BENCH       = "SPY"
UNIVERSE       = scan.SP100_PARTIAL_FALLBACK

def download(sym):
    try:
        df = yf.Ticker(sym).history(start=LOOKBACK_START, end=END).dropna()
        return df if len(df) >= MIN_BARS else None
    except Exception:
        return None

def evaluate(df_win, bench_win):
    gv      = scan.gate_volatility(df_win)
    gf, _   = scan.gate_concrete_floor(df_win)
    gr      = scan.gate_relative_strength(df_win, bench_win)
    gs      = scan.gate_hard_stop(df_win)
    survivor = all(g["status"] != "NO_GO" for g in (gv, gf, gr)) and gf["status"] == "GO"
    gocount  = sum(1 for g in (gv, gf, gr) if g["status"] == "GO")
    return survivor, gocount, gs["value"]

def main():
    print(f"Downloading {len(UNIVERSE)} tickers + {RS_BENCH} ...")
    bench = yf.Ticker(RS_BENCH).history(start=LOOKBACK_START, end=END)["Close"].dropna()
    data  = {s: d for s in UNIVERSE if (d := download(s)) is not None}
    print(f"Loaded {len(data)} tickers.")

    days = pd.bdate_range(START, END)
    cash, positions, trades, curve = START_EQUITY, {}, [], []

    for day in days:
        for sym in list(positions):
            df = data[sym]
            if day not in df.index: continue
            o, hi, lo = df.loc[day, ["Open", "High", "Low"]]
            p = positions[sym]; px = None
            if lo <= p["stop"]:     px = min(o, p["stop"])
            elif hi >= p["target"]: px = max(o, p["target"])
            if px is not None:
                fill = px * (1 - COST_BPS / 1e4); cash += p["qty"] * fill
                trades.append({**p, "exit_date": day.date(), "exit": round(fill, 2),
                               "pnl": round(p["qty"] * (fill - p["entry"]), 2),
                               "ret": round(fill / p["entry"] - 1, 4)})
                del positions[sym]

        slots = MAX_POS - len(positions)
        if slots > 0:
            prev = day - pd.Timedelta(days=1)
            cands = []
            for sym, df in data.items():
                if sym in positions: continue
                hist = df.loc[:prev]
                if len(hist) < MIN_BARS or day not in df.index: continue
                df_win    = hist.tail(WIN_DAYS)
                bench_win = bench.loc[:prev].tail(BENCH_DAYS)
                ok, gc, stop = evaluate(df_win, bench_win)
                if ok and stop and stop < df_win["Close"].iloc[-1]:
                    cands.append((gc, sym, df_win["Close"].iloc[-1], stop))
            cands.sort(reverse=True)
            equity = cash + sum(p["qty"] * data[s].loc[day, "Close"]
                                for s, p in positions.items() if day in data[s].index)
            for gc, sym, sig_close, stop in cands[:slots]:
                entry = data[sym].loc[day, "Open"] * (1 + COST_BPS / 1e4)
                if entry <= stop: continue
                qty = int((equity * RISK_PCT) / (entry - stop))
                if qty < 1 or qty * entry > cash: continue
                cash -= qty * entry
                positions[sym] = {"ticker": sym, "entry_date": day.date(), "entry": round(entry, 2),
                                  "qty": qty, "stop": round(stop, 2),
                                  "target": round(sig_close * (1 + TARGET), 2)}

        mtm = cash + sum(p["qty"] * data[s].loc[day, "Close"]
                         for s, p in positions.items() if day in data[s].index)
        curve.append({"date": day.date(), "equity": round(mtm, 2)})

    eq = pd.DataFrame(curve); tr = pd.DataFrame(trades)
    total_ret = eq["equity"].iloc[-1] / START_EQUITY - 1
    dd = (eq["equity"] / eq["equity"].cummax() - 1).min()
    spy_window = bench.loc[bench.index >= START]
    spy_ret = spy_window.iloc[-1] / spy_window.iloc[0] - 1
    if len(tr):
        win = (tr["pnl"] > 0).mean()
        losses = abs(tr.loc[tr.pnl < 0, "pnl"].sum())
        pf  = (tr.loc[tr.pnl > 0, "pnl"].sum() / losses) if losses else float("inf")
        avg_hold = (pd.to_datetime(tr.exit_date) - pd.to_datetime(tr.entry_date)).dt.days.mean()
    else:
        win = pf = avg_hold = float("nan")

    print("=" * 60)
    print(f"PERIOD {START} -> {END}")
    print(f"Trades: {len(tr)} | Win%: {win:.0%} | Profit factor: {pf:.2f}")
    print(f"Avg hold: {avg_hold:.0f} days | Max drawdown: {dd:.1%}")
    print(f"STRATEGY total return: {total_ret:.1%}")
    print(f"SPY buy&hold return : {spy_ret:.1%}")
    print("=" * 60)
    eq.to_csv("results/backtest_equity.csv", index=False)
    tr.to_csv("results/backtest_trades.csv", index=False)
    print("Wrote results/backtest_equity.csv + results/backtest_trades.csv")

if __name__ == "__main__":
    main()
