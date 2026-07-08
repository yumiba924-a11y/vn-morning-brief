#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
breakdown.py — ±閾値ブレイク自動解剖（02.docxの本丸）

「VN-Indexが±X%動いた。犯人はこの銘柄。外人は？Roomは？材料は？出来高異常は？」を自動で並べる。
ニュースにならない急変の背景を消去法で推定する装置。バズ異常だけは平常比(数日)待ちで後付け。

モード:
  既定(price_board): 大引け後に1コールでその日の動きを解剖。速い・レート制限なし。
  --history        : 直近完了日をヒストリカルで解剖（プレマーケット閲覧/バックフィル用・VN30のみ・throttle）

寄与度＝時価総額ウェイト×リターン。※現状は全株時価総額ベース（浮動株未補正＝国有株は過大）。
出力: 解剖テキスト＋ social_out/breakdown.json （犯人・外人・Room・出来高を1銘柄1行で）
依存: vnstock, pandas
"""
from __future__ import annotations
import os, csv, json, time, argparse
from datetime import datetime, timezone, timedelta
import pandas as pd
from vnstock import Listing, Trading, Quote

VN30_SHARE = 0.735  # VN30の対HOSE時価総額シェア(実測)。history時の総額推定に使用
BREAK_THRESHOLD = 1.0  # ±この%超で「ブレイク」フラグ


def _leaf(c):
    return str(c[-1] if isinstance(c, tuple) else c).lower()


def _find(b, *names):
    for c in b.columns:
        if _leaf(c) in names:
            return c
    return None


def fetch_board(symbols, source="VCI"):
    tr = Trading(source=source)
    out = []
    for i in range(0, len(symbols), 50):
        try:
            out.append(tr.price_board(symbols[i:i + 50]))
        except Exception as e:
            print(f"  skip chunk: {e}")
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def index_level():
    """VN-Index の直近2終値 (prev, cur, date)。"""
    try:
        h = Quote(symbol="VNINDEX", source="VCI").history(
            start="2026-06-20", end="2026-12-31", interval="1D").dropna(subset=["close"])
        return float(h["close"].iloc[-2]), float(h["close"].iloc[-1]), str(h["time"].iloc[-1])[:10]
    except Exception as e:
        print(f"  VNINDEX取得失敗: {e}")
        return None, None, "?"


def fetch_material(symbols, days=3):
    """各銘柄の直近ニュース有無をGoogle News(越語)で判定。
    タイトルにティッカーが語として含まれ、かつ days日以内のものだけ採用（誤検知抑制）。
    返り値: {sym: {"has": bool, "headline": str|None, "date": str|None}}"""
    import feedparser, urllib.parse, html, re
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = {}
    for s in symbols:
        has, hd, hdate = False, None, None
        try:
            q = urllib.parse.quote(f"{s} cổ phiếu")
            feed = feedparser.parse(f"https://news.google.com/rss/search?q={q}&hl=vi&gl=VN&ceid=VN:vi")
            for e in feed.entries[:8]:
                title = re.sub("<[^>]+>", "", html.unescape(e.get("title", "")))
                if not re.search(rf"\b{re.escape(s)}\b", title):
                    continue
                try:
                    pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pub = None
                if pub is None or pub >= cutoff:
                    has, hd, hdate = True, title[:70], (pub.strftime("%m/%d") if pub else None)
                    break
        except Exception as ex:
            print(f"  material {s}: {ex}")
        out[s] = {"has": has, "headline": hd, "date": hdate}
    return out


def hist_return(sym):
    for _ in range(2):
        try:
            h = Quote(symbol=sym, source="VCI").history(
                start="2026-06-20", end="2026-12-31", interval="1D").dropna(subset=["close"])
            if len(h) >= 2:
                p, c = float(h["close"].iloc[-2]), float(h["close"].iloc[-1])
                return (c - p) / p
        except Exception:
            time.sleep(20)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", action="store_true", help="直近完了日をヒストリカルで解剖(プレマーケット用)")
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--outdir", default="social_out")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    lst = Listing()
    vn30 = sorted(set(lst.symbols_by_group("VN30")))
    ex = lst.symbols_by_exchange()
    hose = ex[ex["exchange"].isin(["HSX", "HOSE"])]["symbol"].tolist()

    # price_board の対象: history時はVN30だけ(レート節約)、既定は全HOSE(残差計算に必要)
    pb_universe = vn30 if args.history else sorted(set(hose) | set(vn30))
    board = fetch_board(pb_universe)
    if board.empty:
        print("price_board取得失敗。中断。")
        return
    sym = board[_find(board, "symbol")].astype(str)
    def col(name):
        c = _find(board, name); return pd.to_numeric(board[c], errors="coerce") if c is not None else None
    pb = pd.DataFrame({"symbol": sym})
    pb["match"] = col("match_price"); pb["ref"] = col("ref_price"); pb["listed"] = col("listed_share")
    fb, fs = col("foreign_buy_value"), col("foreign_sell_value")
    pb["foreign_net"] = (fb - fs) if fb is not None and fs is not None else None
    cr, tr_ = col("current_room"), col("total_room")
    pb["room_used"] = ((1 - cr / tr_) * 100).round(1) if cr is not None else None
    pb["turnover"] = col("accumulated_value")
    pb = pb.drop_duplicates("symbol").set_index("symbol")
    pb["cap"] = pb["listed"] * pb["ref"]

    iprev, icur, idate = index_level()

    # リターン: history モードは直近完了日、既定は price_board 当日
    if args.history:
        print("[mode] history（直近完了日・VN30のみ・throttle）")
        rets = {}
        for s in vn30:
            r = hist_return(s)
            if r is not None:
                rets[s] = r
            time.sleep(3.3)
        total = pb.loc[pb.index.isin(vn30), "cap"].sum() / VN30_SHARE
        universe = vn30
    else:
        print("[mode] price_board（当日・大引け後想定）")
        rets = ((pb["match"] - pb["ref"]) / pb["ref"]).to_dict()
        total = pb.loc[pb.index.isin(hose), "cap"].dropna().sum()
        universe = [s for s in hose if s in rets]

    # 寄与度(ポイント) = 指数前日値 × ウェイト × リターン
    rows = []
    for s in universe:
        if s not in rets or s not in pb.index or pd.isna(pb.loc[s, "cap"]):
            continue
        w = pb.loc[s, "cap"] / total
        contrib = (iprev or 0) * w * rets[s]
        rows.append({"symbol": s, "ret": rets[s], "w": w, "contrib_pt": contrib,
                     "foreign_net": pb.loc[s, "foreign_net"], "room_used": pb.loc[s, "room_used"],
                     "turnover": pb.loc[s, "turnover"], "in_vn30": s in set(vn30)})
    df = pd.DataFrame(rows)
    if df.empty:
        print("寄与度算出不可（動意なし/データ不足）。"); return

    idx_pt = icur - iprev if iprev else df["contrib_pt"].sum()
    vn30_pt = df.loc[df["in_vn30"], "contrib_pt"].sum()
    resid = idx_pt - vn30_pt
    idx_ret = idx_pt / iprev * 100 if iprev else 0

    print(f"\n■ ±ブレイク解剖 {idate}  VN-Index {iprev:.2f}→{icur:.2f}  {idx_pt:+.2f}pt ({idx_ret:+.2f}%)"
          + ("  ⚑BREAK" if abs(idx_ret) >= BREAK_THRESHOLD else ""))
    print(f"   VN30が説明: {vn30_pt:+.2f}pt ({vn30_pt/idx_pt*100:.0f}%) / VN30外残差: {resid:+.2f}pt"
          + ("  ← VN30外が過半＝広範な動き(要注目)" if abs(resid) > abs(vn30_pt) else ""))

    def fmt(x):
        net = x["foreign_net"]
        net_s = (f"外人{net/1e9:+.1f}十億" if pd.notna(net) else "外人n/a")
        room = f"Room{x['room_used']:.0f}%" if pd.notna(x["room_used"]) else "Room n/a"
        return (f"   {x['symbol']:5}{'' if x['in_vn30'] else '*'} {x['ret']*100:+6.2f}%  "
                f"寄与{x['contrib_pt']:+6.2f}pt  {net_s}  {room}")

    dfv = df[df["in_vn30"]]
    side = "下げ犯" if idx_pt < 0 else "上げ犯"
    print(f"\n▼ {side} TOP{args.top}（犯人＋外人フロー＋Room）")
    for _, x in dfv.sort_values("contrib_pt", ascending=idx_pt < 0).head(args.top).iterrows():
        print(fmt(x))
    print(f"▼ 逆側の支え TOP3")
    for _, x in dfv.sort_values("contrib_pt", ascending=idx_pt >= 0).head(3).iterrows():
        print(fmt(x))
    out_ext = df[~df["in_vn30"]].sort_values("contrib_pt", key=abs, ascending=False).head(4)
    if not out_ext.empty and abs(resid) > 3:
        print(f"▼ VN30外で指数を動かした銘柄（=それ自体ニュース候補）")
        for _, x in out_ext.iterrows():
            print(fmt(x))

    top = df.reindex(df["contrib_pt"].abs().sort_values(ascending=False).index).head(12)
    # 犯人上位に「材料あり/なし」を付与（材料なし＝ニュースにならない動き＝バズ注視）
    print("\n[材料] 犯人のニュース有無を Google News で確認中…")
    mat = fetch_material(list(top["symbol"]))
    culprits = []
    for _, x in top.iterrows():
        m = mat.get(x["symbol"], {})
        culprits.append({
            "symbol": x["symbol"], "ret_pct": round(x["ret"]*100, 2),
            "contrib_pt": round(x["contrib_pt"], 2),
            "foreign_net_bn": round(x["foreign_net"]/1e9, 2) if pd.notna(x["foreign_net"]) else None,
            "room_used_pct": x["room_used"] if pd.notna(x["room_used"]) else None,
            "in_vn30": bool(x["in_vn30"]),
            "has_material": m.get("has", False), "material_headline": m.get("headline"),
            "material_date": m.get("date"),
        })
    # 「材料なしで大きく動いた犯人」＝バズ注視候補を明示
    no_mat = [c for c in culprits[:6] if not c["has_material"] and abs(c["contrib_pt"]) >= 0.5]
    for c in no_mat:
        print(f"   ⚠ {c['symbol']} {c['ret_pct']:+.2f}%（寄与{c['contrib_pt']:+.2f}pt）材料なし→バズ注視")

    payload = {
        "date": idate, "index_prev": iprev, "index_cur": icur, "index_pt": round(idx_pt, 2),
        "index_ret_pct": round(idx_ret, 2), "is_break": abs(idx_ret) >= BREAK_THRESHOLD,
        "vn30_contrib_pt": round(vn30_pt, 2), "residual_pt": round(resid, 2),
        "no_material_movers": [c["symbol"] for c in no_mat],
        "culprits": culprits,
    }
    with open(os.path.join(args.outdir, "breakdown.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\nsaved: {os.path.join(args.outdir, 'breakdown.json')}")


if __name__ == "__main__":
    main()
