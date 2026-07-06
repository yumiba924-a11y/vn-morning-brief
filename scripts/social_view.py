#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
social_view.py — 日次バズ観察ビュー（HTML1枚）

social_history/buzz_daily.csv と config/universe.csv を読み、
最新営業日の Tier2(VN31-100) バズ序列を出す。数日貯まると各銘柄の
「平常比（当日clean ÷ 直近平常mean）」カラムが自動で効き始める。

発火判定そのものはしない（閾値は data-driven で後決め＝tier2-firing-design.md）。
これは「今日どこがうるさいか」を毎営業日 目視するための観察ビュー。
"""
import os, csv, html, statistics
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY = os.path.join(ROOT, "social_history", "buzz_daily.csv")
F247_HISTORY = os.path.join(ROOT, "social_history", "buzz_f247_daily.csv")
UNIVERSE = os.path.join(ROOT, "config", "universe.csv")
OUTDIR = os.path.join(ROOT, "output")

BASELINE_MIN_DAYS = 5      # これ未満は平常比を「構築中」表示（design準拠）
BASELINE_WINDOW = 10       # 平常mean の窓（営業日）
PROVISIONAL_FIRE = 2.5     # 平常比の仮フラグ（design未決＝暫定・目安）


def load_history():
    if not os.path.exists(HISTORY):
        return []
    with open(HISTORY, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_f247_latest():
    """F247履歴の最新日を symbol -> {active,new,posts_delta} で返す。
    posts_delta は前営業日との sum_posts 差（＝その日の投稿ペース。無ければ None）。"""
    if not os.path.exists(F247_HISTORY):
        return {}
    with open(F247_HISTORY, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {}
    dates = sorted({r["date"] for r in rows})
    latest, prev = dates[-1], (dates[-2] if len(dates) >= 2 else None)
    prev_posts = {r["symbol"]: int(r["sum_posts"]) for r in rows if r["date"] == prev} if prev else {}
    out = {}
    for r in rows:
        if r["date"] != latest:
            continue
        sym = r["symbol"]
        delta = None
        if sym in prev_posts:
            delta = int(r["sum_posts"]) - prev_posts[sym]
        out[sym] = {"active": int(r["topics_active_24h"]),
                    "new": int(r["topics_new_24h"]), "posts_delta": delta}
    return out


def load_tiers():
    tier = {}
    if os.path.exists(UNIVERSE):
        with open(UNIVERSE, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                tier[r["symbol"].strip().upper()] = r.get("tier", "").strip()
    return tier


def esc(s):
    return html.escape(str(s))


def build():
    rows = load_history()
    tier = load_tiers()
    f247 = load_f247_latest()
    os.makedirs(OUTDIR, exist_ok=True)

    if not rows:
        _write("<p>履歴なし（social_history/buzz_daily.csv が空）</p>", "—")
        return

    dates = sorted({r["date"] for r in rows})
    latest = dates[-1]
    prior_dates = dates[:-1][-BASELINE_WINDOW:]
    n_base = len(prior_dates)

    # 銘柄別の平常mean（当日を除く直近営業日のclean平均）
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], {})[r["date"]] = int(r["volume_n_clean"])

    def baseline_mean(sym):
        vals = [by_sym[sym][d] for d in prior_dates if d in by_sym.get(sym, {})]
        return statistics.mean(vals) if len(vals) >= 3 else None

    # 最新日の行を組み立て
    today = [r for r in rows if r["date"] == latest]
    recs = []
    for r in today:
        sym = r["symbol"]
        clean = int(r["volume_n_clean"])
        mean = baseline_mean(sym)
        ratio = (clean / mean) if (mean and mean > 0) else None
        fv = f247.get(sym, {})
        recs.append({
            "symbol": sym, "tier": tier.get(sym, "?"),
            "clean": clean, "vol": int(r["volume_n"]),
            "quality": int(r["quality_n"]), "mean": mean, "ratio": ratio,
            "f247_active": fv.get("active"), "f247_delta": fv.get("posts_delta"),
        })

    tier2 = sorted([x for x in recs if x["tier"] == "tier2"],
                   key=lambda x: x["clean"], reverse=True)
    tier1 = sorted([x for x in recs if x["tier"] == "tier1"],
                   key=lambda x: x["clean"], reverse=True)
    quiet = sum(1 for x in tier2 if x["clean"] <= 3)
    baseline_ready = n_base >= BASELINE_MIN_DAYS

    # ---- HTML ----
    def ratio_cell(x):
        if not baseline_ready or x["ratio"] is None:
            return '<span style="color:#9aa;">—</span>'
        fire = x["ratio"] >= PROVISIONAL_FIRE
        color = "#E94560" if fire else "#1D1D1F"
        mark = " ▲" if fire else ""
        return f'<span style="color:{color};font-weight:{700 if fire else 400};">×{x["ratio"]:.1f}{mark}</span>'

    def f247_cell(x):
        a = x.get("f247_active")
        if a is None:
            return '<span style="color:#9aa;">—</span>'
        d = x.get("f247_delta")
        if d is not None:
            return f'{a}<span style="color:#6B7280;"> ·Δ{d:+d}</span>'
        return str(a)

    def rows_html(items, top=20):
        out = []
        for i, x in enumerate(items[:top], 1):
            out.append(
                f'<tr>'
                f'<td style="padding:4px 8px;color:#9aa;">{i}</td>'
                f'<td style="padding:4px 8px;font-weight:700;">{esc(x["symbol"])}</td>'
                f'<td style="padding:4px 8px;text-align:right;font-weight:700;">{x["clean"]}</td>'
                f'<td style="padding:4px 8px;text-align:right;color:#6B7280;">{x["vol"]}</td>'
                f'<td style="padding:4px 8px;text-align:right;color:#6B7280;">{x["quality"]}</td>'
                f'<td style="padding:4px 8px;text-align:right;color:#6B7280;">{f247_cell(x)}</td>'
                f'<td style="padding:4px 8px;text-align:right;">{ratio_cell(x)}</td>'
                f'</tr>'
            )
        return "".join(out)

    if baseline_ready:
        base_note = (f"平常比＝当日clean ÷ 直近{n_base}営業日平均。"
                     f"×{PROVISIONAL_FIRE:.0f}以上を暫定フラグ▲（正式閾値はdesign未決）")
    else:
        base_note = (f"ベースライン構築中 {n_base}/{BASELINE_MIN_DAYS}営業日。"
                     f"平常比は{BASELINE_MIN_DAYS}日到達後に有効化。今は「本日の生序列」。")

    C = {"navy": "#1A2744", "ink": "#1D1D1F", "sub": "#6B7280", "line": "#e6e8ec"}
    thead = ('<tr style="font-size:11px;color:#6B7280;text-align:left;">'
             '<th style="padding:4px 8px;">#</th><th style="padding:4px 8px;">銘柄</th>'
             '<th style="padding:4px 8px;text-align:right;">clean</th>'
             '<th style="padding:4px 8px;text-align:right;">vol</th>'
             '<th style="padding:4px 8px;text-align:right;">質</th>'
             '<th style="padding:4px 8px;text-align:right;">F247</th>'
             '<th style="padding:4px 8px;text-align:right;">平常比</th></tr>')

    body = f"""
<div style="max-width:640px;margin:0 auto;background:#fff;font-family:'Meiryo UI',Meiryo,sans-serif;">
  <div style="background:{C['navy']};color:#fff;padding:16px 20px;">
    <div style="font-size:12px;opacity:.75;letter-spacing:1px;">TIER2 BUZZ MONITOR ｜ 内部参考</div>
    <div style="font-size:20px;font-weight:700;margin-top:2px;">掲示板バズ観察ビュー</div>
    <div style="font-size:12px;opacity:.85;margin-top:2px;">対象日 {esc(latest)}（ICT）｜ VN31–100 の70銘柄</div>
  </div>

  <div style="padding:10px 20px;background:#F8F9FA;font-size:11px;color:{C['sub']};
       border-bottom:1px solid {C['line']};">{esc(base_note)}</div>

  <div style="padding:14px 20px;">
    <div style="font-size:13px;font-weight:700;color:{C['ink']};margin-bottom:6px;">
      本日うるさい順（Tier2・clean件数）</div>
    <table style="border-collapse:collapse;width:100%;font-size:13px;">
      {thead}{rows_html(tier2, 20)}
    </table>
    <div style="font-size:11px;color:{C['sub']};margin-top:8px;">
      Tier2 70銘柄中 静穏(clean≤3)＝{quiet}銘柄 ｜
      clean＝broadcast煽り除外後の投稿数(FireAnt) ｜ vol＝除外前 ｜ 質＝type1(ニュース/専門家)件数 ｜
      F247＝Discourse稼働スレ数(·Δは前日比 投稿ペース・2日目から)
    </div>

    <div style="font-size:13px;font-weight:700;color:{C['ink']};margin:18px 0 6px;">
      参考：Tier1(VN30) 上位（発火判定には使わない・解釈用）</div>
    <table style="border-collapse:collapse;width:100%;font-size:13px;">
      {thead}{rows_html(tier1, 8)}
    </table>
  </div>

  <div style="padding:12px 20px;border-top:1px solid {C['line']};font-size:11px;color:{C['sub']};">
    FireAnt掲示板より自動集計（社内参考・投資助言ではありません）。
    発火判定ロジックは _reference/tier2-firing-design.md 準拠、閾値は履歴蓄積後に確定。
  </div>
</div>"""
    _write(body, latest)


def _write(body, latest):
    stamp = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M JST")
    page = (f'<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Tier2 Buzz Monitor {esc(latest)}</title></head>'
            f'<body style="margin:0;background:#f4f4f4;">{body}'
            f'<div style="max-width:640px;margin:6px auto;font-size:10px;color:#9aa;'
            f'text-align:right;font-family:sans-serif;">generated {esc(stamp)}</div>'
            f'</body></html>')
    with open(os.path.join(OUTDIR, "social_latest.html"), "w", encoding="utf-8") as f:
        f.write(page)
    dated = os.path.join(OUTDIR, f"social-{str(latest).replace('-','')}.html")
    with open(dated, "w", encoding="utf-8") as f:
        f.write(page)
    # GitHub Pages 配信用（source=main /docs）。同じ内容を index.html として置く。
    docs = os.path.join(ROOT, "docs")
    os.makedirs(docs, exist_ok=True)
    with open(os.path.join(docs, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    print(f"[social_view] wrote output/social_latest.html & docs/index.html (対象日 {latest})")


if __name__ == "__main__":
    build()
