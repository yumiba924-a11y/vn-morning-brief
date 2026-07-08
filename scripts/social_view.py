#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
social_view.py — VN100 注目度モニター（別紙・HTML1枚）

02.docxの三層ユニバース＋「本紙＝物語／別紙＝データ」の"別紙"。
VN100全銘柄を Tier1(VN30)/Tier2(VN31-100) で並べ、各銘柄に全シグナルを横断：
  メディア言及・掲示板バズ(平常比)・指数寄与(動いた度)・外人ネット・Room・マージン締め
→「動いた×騒がれ×推奨され×締められた」が一覧で見える。

データ源（すべて social_history/ に日次蓄積 or フィード取得）:
  buzz_daily.csv / buzz_f247_daily.csv / room_daily.csv / breakdown.json / config/universe.csv
  ＋ ホット株フィード(メディア言及)・siết margin(マージン締め)
"""
import os, csv, re, json, html, statistics
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(ROOT, "social_history")
UNIVERSE = os.path.join(ROOT, "config", "universe.csv")
VN_SYMBOLS = os.path.join(ROOT, "config", "vn_symbols.txt")
OUTDIR = os.path.join(ROOT, "output")
DOCS = os.path.join(ROOT, "docs")

BASELINE_MIN_DAYS = 5
BASELINE_WINDOW = 10
FIRE = 2.5
HOT_STOP = {"USD", "VND", "EUR", "JPY", "CNY", "GDP", "CPI", "FDI", "ETF", "IPO",
            "ESG", "CEO", "CFO", "HOSE", "HNX", "SBV", "SSC", "FED", "USA", "TOP", "NEW"}


def esc(s):
    return html.escape(str(s))


def read_csv(name):
    p = os.path.join(SH, name)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_tiers():
    tier = {}
    if os.path.exists(UNIVERSE):
        with open(UNIVERSE, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                tier[r["symbol"].strip().upper()] = r.get("tier", "").strip()
    return tier


def load_vn_symbols():
    if not os.path.exists(VN_SYMBOLS):
        return set()
    return set(l.strip().upper() for l in open(VN_SYMBOLS, encoding="utf-8") if l.strip())


def latest_and_prior(rows):
    dates = sorted({r["date"] for r in rows})
    return (dates[-1] if dates else None), dates[:-1][-BASELINE_WINDOW:]


def fetch_media_and_margin(symset):
    """ホット株フィードから 言及数/銘柄・代表見出し と マージン締め銘柄集合 を返す。"""
    mention, rep, margin = {}, {}, set()
    try:
        import feedparser
    except Exception:
        return mention, rep, margin
    from urllib.parse import quote_plus
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    tag = re.compile(r"<[^>]*>?")

    def recent(e):
        try:
            return datetime(*e.published_parsed[:6], tzinfo=timezone.utc) >= cutoff
        except Exception:
            return True
    feeds = [
        "https://news.google.com/rss/search?q=" + quote_plus("cổ phiếu cần quan tâm") + "&hl=vi&gl=VN&ceid=VN:vi",
        "https://news.google.com/rss/search?q=" + quote_plus("cổ phiếu đáng chú ý") + "&hl=vi&gl=VN&ceid=VN:vi",
        "https://news.google.com/rss/search?q=" + quote_plus("khuyến nghị cổ phiếu") + "&hl=vi&gl=VN&ceid=VN:vi",
        "https://news.google.com/rss/search?q=" + quote_plus("siết margin OR căng margin OR cắt margin cổ phiếu") + "&hl=vi&gl=VN&ceid=VN:vi",
        "https://nhadautu.vn/trang-chu.rss",
    ]
    margin_re = re.compile(r"margin", re.I)
    for url in feeds:
        try:
            feed = feedparser.parse(url)
        except Exception:
            continue
        for e in feed.entries:
            if not recent(e):
                continue
            title = (e.get("title", "") or "").strip()
            text = title + " " + tag.sub(" ", html.unescape(e.get("summary", "") or ""))
            is_margin = bool(margin_re.search(text))
            for s in {m for m in re.findall(r"\b([A-Z]{3})\b", text) if m in symset and m not in HOT_STOP}:
                mention[s] = mention.get(s, 0) + 1
                if s not in rep and re.search(rf"\b{s}\b", title):
                    rep[s] = title
                if is_margin:            # どのフィードでも margin 語を含めば締めフラグ
                    margin.add(s)
    return mention, rep, margin


def build():
    os.makedirs(OUTDIR, exist_ok=True)
    os.makedirs(DOCS, exist_ok=True)
    tier = load_tiers()
    symset = load_vn_symbols()
    vn100 = [s for s in tier]  # universe.csv の100銘柄

    # --- バズ（平常比つき）---
    buzz = read_csv("buzz_daily.csv")
    b_latest, b_prior = latest_and_prior(buzz)
    n_base = len(b_prior)
    by_sym = {}
    for r in buzz:
        by_sym.setdefault(r["symbol"], {})[r["date"]] = int(r["volume_n_clean"])

    def buzz_now(s):
        return by_sym.get(s, {}).get(b_latest, 0)

    def buzz_ratio(s):
        vals = [by_sym[s][d] for d in b_prior if d in by_sym.get(s, {})]
        if len(vals) >= 3 and statistics.mean(vals) > 0:
            return buzz_now(s) / statistics.mean(vals)
        return None

    # --- 外人フロー・Room（最新日）---
    room = read_csv("room_daily.csv")
    r_latest = (sorted({r["date"] for r in room})[-1] if room else None)
    room_by = {r["symbol"]: r for r in room if r["date"] == r_latest}

    # --- 指数寄与（breakdown）---
    bd = {}
    bp = os.path.join(SH, "breakdown.json")
    if os.path.exists(bp):
        try:
            j = json.load(open(bp, encoding="utf-8"))
            bd = {c["symbol"]: c for c in j.get("culprits", [])}
            bd_date = j.get("date")
        except Exception:
            bd_date = None
    else:
        bd_date = None

    # --- メディア言及・マージン ---
    mention, rep, margin = fetch_media_and_margin(symset)

    # --- 銘柄レコード組み立て＋注目スコア ---
    recs = []
    for s in vn100:
        rm = room_by.get(s, {})
        fnet = rm.get("foreign_net_value")
        try:
            fnet = float(fnet) / 1e9 if fnet not in (None, "") else None
        except Exception:
            fnet = None
        contrib = bd.get(s, {}).get("contrib_pt")
        rec = {
            "symbol": s, "tier": tier.get(s, ""),
            "mention": mention.get(s, 0),
            "buzz": buzz_now(s), "ratio": buzz_ratio(s),
            "contrib": contrib,
            "fnet": fnet,
            "room": (float(rm["room_used_pct"]) if rm.get("room_used_pct") not in (None, "") else None),
            "margin": s in margin,
            "rep": rep.get(s, ""),
        }
        rec["score"] = (rec["mention"] * 2 + rec["buzz"] / 12.0
                        + (abs(contrib) * 3 if contrib else 0)
                        + (5 if rec["margin"] else 0))
        recs.append(rec)

    baseline_ready = n_base >= BASELINE_MIN_DAYS
    _write(recs, b_latest, r_latest, bd_date, n_base, baseline_ready)


def _row(x, baseline_ready):
    def cell(v, align="right", color="#333"):
        return f'<td style="padding:3px 7px;text-align:{align};color:{color};">{v}</td>'
    flags = ""
    if x["margin"]:
        flags += '<span style="color:#c0392b;font-weight:700;">締</span>'
    ratio = "—"
    if baseline_ready and x["ratio"] is not None:
        fire = x["ratio"] >= FIRE
        ratio = f'<span style="color:{"#c0392b" if fire else "#555"};font-weight:{700 if fire else 400};">×{x["ratio"]:.1f}{"▲" if fire else ""}</span>'
    contrib = (f'{x["contrib"]:+.2f}' if x["contrib"] is not None else "—")
    fnet = (f'{x["fnet"]:+.0f}' if x["fnet"] is not None else "—")
    room = (f'{x["room"]:.0f}%' if x["room"] is not None else "—")
    return ("<tr>"
            + f'<td style="padding:3px 7px;font-weight:700;">{esc(x["symbol"])}</td>'
            + cell(x["mention"] or "—")
            + cell(x["buzz"] or "—")
            + cell(ratio)
            + cell(contrib, color=("#c0392b" if (x["contrib"] or 0) < 0 else "#0a7d4b"))
            + cell(fnet, color=("#c0392b" if (x["fnet"] or 0) < 0 else "#0a7d4b"))
            + cell(room, color="#666")
            + cell(flags or "", align="center")
            + "</tr>")


def _table(recs, baseline_ready, top=None):
    active = [r for r in recs if r["score"] > 0]
    active.sort(key=lambda r: r["score"], reverse=True)
    quiet = len(recs) - len(active)
    shown = active[:top] if top else active
    head = ('<tr style="font-size:10px;color:#888;text-align:right;">'
            '<th style="text-align:left;padding:3px 7px;">銘柄</th>'
            '<th style="padding:3px 7px;">言及</th><th style="padding:3px 7px;">バズ</th>'
            '<th style="padding:3px 7px;">平常比</th><th style="padding:3px 7px;">寄与pt</th>'
            '<th style="padding:3px 7px;">外人(十億)</th><th style="padding:3px 7px;">Room</th>'
            '<th style="padding:3px 7px;">締</th></tr>')
    rows = "".join(_row(r, baseline_ready) for r in shown)
    note = f'<div style="font-size:10px;color:#aaa;margin-top:4px;">静穏(シグナル無し)＝{quiet}銘柄は非表示</div>'
    return f'<table style="border-collapse:collapse;width:100%;font-size:12px;">{head}{rows}</table>{note}'


def _write(recs, b_date, r_date, bd_date, n_base, baseline_ready):
    t1 = [r for r in recs if r["tier"] == "tier1"]
    t2 = [r for r in recs if r["tier"] == "tier2"]
    stamp = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M JST")
    base_note = (f"平常比＝当日バズ ÷ 直近{n_base}営業日平均（×{FIRE:.0f}以上▲）"
                 if baseline_ready else
                 f"平常比はベースライン構築中 {n_base}/{BASELINE_MIN_DAYS}営業日で有効化")
    C = {"navy": "#0f1b2d"}
    body = f"""
<div style="max-width:820px;margin:0 auto;background:#fff;font-family:'Hiragino Kaku Gothic ProN','Meiryo',sans-serif;color:#111;">
  <div style="background:{C['navy']};color:#fff;padding:16px 22px;">
    <div style="font-size:11px;opacity:.7;letter-spacing:2px;">CQC 投資調査部 ｜ 別紙・内部参考</div>
    <div style="font-size:21px;font-weight:800;margin-top:2px;">VN100 注目度モニター</div>
    <div style="font-size:12px;opacity:.85;margin-top:3px;">
      バズ {esc(b_date)} ／ 外人・Room {esc(r_date)} ／ 指数寄与 {esc(bd_date)}（ICT基準）</div>
  </div>
  <div style="padding:8px 22px;background:#f4f7fb;font-size:11px;color:#555;border-bottom:1px solid #e6e8ec;">
    「動いた×騒がれ×推奨され×締められた」を横断。{esc(base_note)}。
    言及＝ホット株メディア／バズ＝FireAnt掲示板／寄与＝指数を動かしたpt／締＝マージン規制ニュース。</div>

  <div style="padding:14px 22px;">
    <div style="font-size:13px;font-weight:800;color:#c0392b;margin-bottom:5px;">
      ● Tier2（VN31–100）＝発火監視ゾーン（本命）</div>
    {_table(t2, baseline_ready)}

    <div style="font-size:13px;font-weight:800;color:#1a3a6b;margin:20px 0 5px;">
      ○ Tier1（VN30）＝指数コア（解釈用）</div>
    {_table(t1, baseline_ready)}
  </div>
  <div style="padding:12px 22px 20px;font-size:10px;color:#999;">
    社内参考・投資助言ではありません。発火判定は _reference/tier2-firing-design.md 準拠（閾値は履歴蓄積後）。
    generated {esc(stamp)}</div>
</div>"""
    page = (f'<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>VN100 注目度モニター {esc(b_date)}</title></head>'
            f'<body style="margin:0;background:#f4f4f4;">{body}</body></html>')
    for path in (os.path.join(OUTDIR, "social_latest.html"),
                 os.path.join(DOCS, "index.html")):
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
    print(f"[social_view] VN100注目度モニター生成（Tier2 {len(t2)}／Tier1 {len(t1)}）")


if __name__ == "__main__":
    build()
