#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
room_collector.py — Foreign Room（外国人保有余力）＋外人フロー 日次収集

原点思想の中核：「外人はシグナル、ただしRoom無しの銘柄に本格始動は来ない」。
Room空き × 外人買い越し の組み合わせこそ本物の始動サイン。FMの一番の欲しさ。

データ源＝VCI(Vietcap) price_board（vnstock経由）。FireAnt postsとは別系統。
各銘柄の日次スナップを social_history/room_daily.csv に追記（同一date置換＝冪等）。

列:
  date, symbol, fol_pct, room_used_pct, current_room, total_room,
  foreign_net_value, foreign_buy_value, foreign_sell_value, turnover_value, match_price

依存: vnstock, pandas。実行: python scripts/room_collector.py --symbols-file config/universe.csv
"""
from __future__ import annotations
import os, csv, argparse
from datetime import datetime, timezone, timedelta
import pandas as pd
from vnstock import Trading


def _leaf(col):
    name = col[-1] if isinstance(col, tuple) else col
    return str(name).lower()


def _find(board, *names):
    for c in board.columns:
        if _leaf(c) in names:
            return c
    return None


def read_symbols(path, fallback):
    if not path:
        return fallback
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            s = line.split(",")[0].strip().upper()
            if s and s != "SYMBOL":
                out.append(s)
    return out


def chunked(seq, n=50):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_board(symbols, source="VCI"):
    tr = Trading(source=source)
    frames = []
    for grp in chunked(symbols, 50):
        try:
            frames.append(tr.price_board(grp))
        except Exception as e:
            print(f"  skip chunk({len(grp)}): {e}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def num(board, col):
    return pd.to_numeric(board[col], errors="coerce") if col is not None else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=["VCB", "FPT", "HPG", "MWG", "VIC"])
    ap.add_argument("--symbols-file", default=None)
    ap.add_argument("--history", default=None)
    ap.add_argument("--date", default=None)
    ap.add_argument("--outdir", default="social_out")
    args = ap.parse_args()

    symbols = read_symbols(args.symbols_file, args.symbols)
    now = datetime.now(timezone.utc)
    run_date = args.date or (now + timedelta(hours=7)).strftime("%Y-%m-%d")
    os.makedirs(args.outdir, exist_ok=True)

    board = fetch_board(sorted(set(symbols)))
    if board.empty:
        print("price_board 取得失敗（空）。中断。")
        return

    sym_c = _find(board, "symbol")
    cur = _find(board, "current_room")
    tot = _find(board, "total_room")
    listed = _find(board, "listed_share")
    fbv = _find(board, "foreign_buy_value")
    fsv = _find(board, "foreign_sell_value")
    turn = _find(board, "accumulated_value")
    price = _find(board, "match_price")

    s = pd.DataFrame({"symbol": board[sym_c].astype(str)})
    s["current_room"] = num(board, cur)
    s["total_room"] = num(board, tot)
    listed_v = num(board, listed)
    s["fol_pct"] = (s["total_room"] / listed_v * 100).round(2) if listed_v is not None else None
    s["room_used_pct"] = ((1 - s["current_room"] / s["total_room"]) * 100).round(2)
    fb = num(board, fbv); fs = num(board, fsv)
    s["foreign_buy_value"] = fb
    s["foreign_sell_value"] = fs
    s["foreign_net_value"] = (fb - fs) if (fb is not None and fs is not None) else None
    s["turnover_value"] = num(board, turn)
    s["match_price"] = num(board, price)

    # 収集対象の順に整列・重複排除
    order = {sym: i for i, sym in enumerate(symbols)}
    s = s[s["symbol"].isin(order)].drop_duplicates("symbol")
    s = s.sort_values("symbol", key=lambda col: col.map(order))

    cols = ["date", "symbol", "fol_pct", "room_used_pct", "current_room", "total_room",
            "foreign_net_value", "foreign_buy_value", "foreign_sell_value",
            "turnover_value", "match_price"]

    # 当日スナップCSV
    snap = os.path.join(args.outdir, "room_snapshot.csv")
    s.assign(date=run_date)[cols].to_csv(snap, index=False, encoding="utf-8-sig")

    # 上位表示（Room埋まり降順＝満杯銘柄／外人ネット降順の両方を軽く）
    top_full = s.sort_values("room_used_pct", ascending=False).head(8)
    print(f"=== {run_date} Foreign Room（埋まり降順・上位8）===")
    for _, r in top_full.iterrows():
        net = r["foreign_net_value"]
        net_s = f"{net/1e9:+.1f}十億" if pd.notna(net) else "n/a"
        print(f"  {r['symbol']:5} FOL{r['fol_pct']:5.1f}% 埋まり{r['room_used_pct']:5.1f}% 外人ネット{net_s}")

    # 履歴追記（冪等）
    if args.history:
        append_history(args.history, run_date, s, cols)
        print(f"\nhistory += {run_date} ({len(s)}銘柄) -> {args.history}")
    print(f"saved: {snap}")


def append_history(path, run_date, s, cols):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    rows = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if r.get("date") != run_date]
    for _, r in s.assign(date=run_date).iterrows():
        rows.append({k: r.get(k, "") for k in cols})
    rows.sort(key=lambda r: (str(r["date"]), str(r["symbol"])))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


if __name__ == "__main__":
    main()
