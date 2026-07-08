#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
朝の市況ブリーフ  build_brief.py
------------------------------------------------
収集(為替＋多言語ニュース) → Claudeで翻訳＋要約 → HTML1枚を生成
→ output/ に保存 → メール送信、まで一気通貫。

ししまるの「収集→加工→出力」パイプラインの市況ブリーフ版。
設定は config/sources.yaml、秘密情報は環境変数(GitHub Secrets)から。
"""

import os
import re
import csv
import sys
import json
import time
import html
import smtplib
import datetime as dt
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import yaml
import requests
import feedparser
import anthropic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ----------------------------------------------------------------------
# 設定・時刻
# ----------------------------------------------------------------------
def load_config():
    with open(os.path.join(ROOT, "config", "sources.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def now_jst(cfg):
    tz = ZoneInfo(cfg["output"].get("timezone", "Asia/Tokyo"))
    return dt.datetime.now(tz)


# ----------------------------------------------------------------------
# 1) 為替（レートは必ず本文に明記する ← 彰吾さんルール）
# ----------------------------------------------------------------------
def fetch_fx(cfg):
    base = cfg["fx"]["base"]
    rows = []
    rates = {}
    try:
        r = requests.get(f"https://open.er-api.com/v6/latest/{base}", timeout=20)
        rates = r.json().get("rates", {})
    except Exception as e:
        print(f"[fx] 取得失敗: {e}", file=sys.stderr)

    for p in cfg["fx"]["pairs"]:
        q = p["quote"]
        val = rates.get(q)
        if val is not None:
            disp = f"{val:,.2f}" if val < 1000 else f"{val:,.0f}"
            rows.append({"label": p["label"], "value": disp})

    # VND/JPY を派生計算（USDベース前提）
    if "JPY" in rates and "VND" in rates and rates["VND"]:
        vnd_jpy = rates["JPY"] / rates["VND"] * 100
        rows.append({"label": "VND/JPY (×100)", "value": f"{vnd_jpy:,.4f}"})

    return rows


def fetch_indices(cfg):
    """Yahoo Finance chart API から指数・コモディティの現値を取得。
    取れなかったものは黙って落とす（壊れない設計）。
    ※旧Stooq CSVは light-quote エンドポイント廃止で全滅→差し替え(2026-07)。"""
    rows = []
    for it in cfg.get("indices", []):
        try:
            sym = quote_plus(it["symbol"])
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1d&interval=1d"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            meta = r.json()["chart"]["result"][0]["meta"]
            val = meta.get("regularMarketPrice")
            if val is None:
                continue
            rows.append({"label": it["label"], "value": f"{val:,.2f}"})
        except Exception as e:
            print(f"[idx] {it.get('label')} 取得失敗: {e}", file=sys.stderr)
    return rows


# ----------------------------------------------------------------------
# 2) ニュース収集（言語非依存。RSSはただのテキスト取得）
# ----------------------------------------------------------------------
def google_news_url(src):
    q = quote_plus(src["query"])
    return (f"https://news.google.com/rss/search?q={q}"
            f"&hl={src['hl']}&gl={src['gl']}&ceid={src['ceid']}")


def within_lookback(entry, cutoff):
    try:
        pub = dt.datetime(*entry.published_parsed[:6], tzinfo=dt.timezone.utc)
        return pub >= cutoff
    except Exception:
        return True  # 日付が取れない場合は残す


def fetch_news(cfg):
    hours = cfg["filter"]["hours_lookback"]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    items = []

    for src in cfg["news"]:
        url = google_news_url(src) if src["type"] == "google_news" else src["url"]
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[news] {src['name']} 取得失敗: {e}", file=sys.stderr)
            continue

        count = 0
        for e in feed.entries:
            if count >= src.get("max", 3):
                break
            if not within_lookback(e, cutoff):
                continue
            items.append({
                "source": src["name"],
                "category": src["category"],
                "title": e.get("title", "").strip(),
                "link": e.get("link", ""),
                "snippet": html.unescape(e.get("summary", ""))[:400],
            })
            count += 1

    return items[: cfg["filter"]["max_total_items"]]


def fetch_stock_news(symbols, per=2, days=4):
    """動いた銘柄の個別ニュースを Google News(越語) から items形式で取得。
    タイトルにティッカーが語として入り、直近days日のものだけ（誤検知抑制）。"""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    out, seen = [], set()
    for s in symbols:
        try:
            q = quote_plus(f"{s} cổ phiếu")
            feed = feedparser.parse(f"https://news.google.com/rss/search?q={q}&hl=vi&gl=VN&ceid=VN:vi")
            cnt = 0
            for e in feed.entries:
                if cnt >= per:
                    break
                title = e.get("title", "").strip()
                if not re.search(rf"\b{re.escape(s)}\b", re.sub("<[^>]+>", "", html.unescape(title))):
                    continue
                try:
                    pub = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
                    if pub < cutoff:
                        continue
                except Exception:
                    pass
                link = e.get("link", "")
                if link in seen:
                    continue
                seen.add(link)
                out.append({"source": f"個別/{s}", "category": "stock", "symbol": s,
                            "title": title, "link": link,
                            "snippet": html.unescape(e.get("summary", ""))[:400]})
                cnt += 1
        except Exception as ex:
            print(f"[stock_news] {s}: {ex}", file=sys.stderr)
    return out


# ----------------------------------------------------------------------
# 3) Claudeで翻訳＋要約＋示唆（全記事を1コールでJSON化 = 低コスト）
# ----------------------------------------------------------------------
def gtranslate(text, tl="ja"):
    """Google翻訳のキー不要エンドポイントで翻訳。失敗時は原文を返す（壊さない）。
    非公式だが無料・キー不要。1日1回・数十件の低頻度用途。"""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": tl, "dt": "t", "q": text[:1500]},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
        )
        data = r.json()
        out = "".join(seg[0] for seg in data[0] if seg and seg[0])
        return out or text
    except Exception as e:
        print(f"[translate] 失敗（原文使用）: {e}", file=sys.stderr)
        return text


def summarize_with_claude(cfg, items):
    if not items:
        return []

    # 無料モード: ANTHROPIC_API_KEY が無ければ Claude を使わず、Google翻訳で日本語化。
    # （3行要約・示唆は付かないが、費用ゼロ・キー不要で「読める日本語」になる）
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[claude] APIキー未設定 → 無料モード（Google翻訳で日本語化）", file=sys.stderr)
        # <[^>]*>? … 閉じ ">" を任意にして、URLが途中で切れた未完タグ(<a href="…)も除去
        tag_re = re.compile(r"<[^>]*>?")
        for it in items:
            it["jp_title"] = gtranslate(it["title"])
            # RSSのsnippetはHTML。二重エスケープ解除 → タグ除去 の順でテキスト化。
            raw = html.unescape(html.unescape(it.get("snippet") or ""))
            txt = re.sub(r"\s+", " ", tag_re.sub(" ", raw)).strip()
            # 見出しの焼き直し（Google News等）は要約を空に。実文があるものだけ日本語化。
            dup = txt[:20].lower() in it["title"].lower()
            it["summary"] = gtranslate(txt[:200]) if (len(txt) >= 25 and not dup) else ""
            it["implication"] = ""
            time.sleep(0.15)
        return items

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を環境変数から自動取得
    payload = [
        {"i": n, "source": it["source"], "category": it["category"],
         "title": it["title"], "snippet": it["snippet"]}
        for n, it in enumerate(items)
    ]

    prompt = (
        "あなたは日系金融機関の運用・営業向けに市況ブリーフを書くアナリストです。\n"
        "以下の記事リスト(英語/ベトナム語/日本語混在)を処理してください。\n"
        "各記事について、原文言語に関わらず日本語で:\n"
        "  - jp_title: 体言止めの見出し(14字以内)。事実の羅列ではなく"
        "『何が起きたか・どちらへ動いたか』を一言で言い切る。"
        "例『AI需要が一段と拡大』『数カ月で状況一変』『外資が買い越しに転換』\n"
        "  - summary: 3行以内の日本語要約\n"
        "  - implication: 営業・運用への一言示唆(1文)。無ければ空文字\n"
        "  - keep: 市況/案件として意味がある記事なら true、雑音なら false\n"
        "を判定します。\n"
        "出力は前置き・Markdown・コードフェンス一切なしのJSON配列のみ。\n"
        'スキーマ: [{"i":番号,"jp_title":"","summary":"","implication":"","keep":true}]\n\n'
        f"記事リスト:\n{json.dumps(payload, ensure_ascii=False)}"
    )

    resp = client.messages.create(
        model=cfg["anthropic_model"],
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    text = text.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[claude] JSON parse失敗: {e}\n---\n{text[:500]}", file=sys.stderr)
        return []

    by_i = {p["i"]: p for p in parsed if isinstance(p, dict) and "i" in p}
    enriched = []
    for n, it in enumerate(items):
        p = by_i.get(n)
        if not p or not p.get("keep", True):
            continue
        it.update({
            "jp_title": p.get("jp_title", it["title"]),
            "summary": p.get("summary", ""),
            "implication": p.get("implication", ""),
        })
        enriched.append(it)
    return enriched


# ----------------------------------------------------------------------
# 4) HTML生成（メール互換=インラインCSS。上=自分用/下=営業用を1枚に）
# ----------------------------------------------------------------------
def esc(s):
    return html.escape(s or "")


def build_html(cfg, stamp, fx_rows, idx_rows, news, lead=""):
    macro = [n for n in news if n["category"] == "macro"]
    local = [n for n in news if n["category"] == "local"]

    title = cfg["output"]["title"]
    C = {"ink": "#1a1a1a", "sub": "#666", "line": "#e2e2e2",
         "accent": "#0b5", "bg": "#ffffff"}

    def news_block(items):
        if not items:
            return ('<p style="color:#666;font-size:13px;margin:6px 0 0;">'
                    '該当なし</p>')
        out = []
        for it in items:
            imp = ""
            if it.get("implication"):
                imp = (f'<div style="font-size:12px;color:{C["accent"]};'
                       f'margin-top:4px;">▶ {esc(it["implication"])}</div>')
            out.append(
                f'<div style="padding:10px 0;border-bottom:1px solid {C["line"]};">'
                f'<a href="{esc(it["link"])}" style="color:{C["ink"]};'
                f'text-decoration:none;font-weight:600;font-size:14px;">'
                f'{esc(it["jp_title"])}</a>'
                f'<div style="font-size:11px;color:{C["sub"]};margin:2px 0;">'
                f'{esc(it["source"])}</div>'
                f'<div style="font-size:13px;color:#333;line-height:1.6;">'
                f'{esc(it["summary"])}</div>{imp}</div>'
            )
        return "".join(out)

    # マーケットスナップショット（為替＋指数を横並び・ブルームバーグ風）
    snap = fx_rows + idx_rows
    snap_html = "".join(
        f'<td style="padding:8px 14px;border:1px solid {C["line"]};'
        f'font-size:12px;color:{C["sub"]};white-space:nowrap;">{esc(r["label"])}<br>'
        f'<span style="font-size:17px;color:{C["ink"]};font-weight:700;">'
        f'{esc(r["value"])}</span></td>'
        for r in snap
    ) or f'<td style="padding:8px;color:{C["sub"]};">数値取得なし</td>'

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#f4f4f4;font-family:'Meiryo UI',Meiryo,sans-serif;">
<div style="max-width:640px;margin:0 auto;background:{C['bg']};padding:22px;">

  <div style="border-bottom:2px solid {C['ink']};padding-bottom:10px;margin-bottom:16px;">
    <div style="font-size:20px;font-weight:700;color:{C['ink']};">{esc(title)}</div>
    <div style="font-size:12px;color:{C['sub']};">{stamp}</div>
  </div>

  <div style="font-size:12px;font-weight:700;color:{C['sub']};letter-spacing:1px;">
       マーケットスナップショット</div>
  <table style="border-collapse:collapse;margin:6px 0 18px;"><tr>{snap_html}</tr></table>

  {lead}

  <div style="font-size:12px;font-weight:700;color:{C['sub']};letter-spacing:1px;
       margin:20px 0 2px;">マクロ・市場全体</div>
  {news_block(macro)}

  <div style="font-size:12px;font-weight:700;color:{C['sub']};letter-spacing:1px;
       margin:20px 0 2px;">現地材料（ベトナム）</div>
  {news_block(local)}

  <div style="margin-top:22px;padding-top:10px;border-top:1px solid {C['line']};
       font-size:11px;color:{C['sub']};">
    自動生成: 為替は open.er-api.com、指数は Yahoo Finance、ニュースはGoogle News/各RSS、
    翻訳はGoogle翻訳（APIキー設定時はClaudeで要約）。数値の確定値は原典でご確認ください。
  </div>
</div></body></html>"""


# ----------------------------------------------------------------------
# 5) 保存 & メール送信
# ----------------------------------------------------------------------
def save_html(cfg, stamp_file, html_str):
    d = os.path.join(ROOT, cfg["output"]["html_dir"])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"brief-{stamp_file}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_str)
    # 最新版へのショートカット
    with open(os.path.join(d, "latest.html"), "w", encoding="utf-8") as f:
        f.write(html_str)
    # GitHub Pages 配信用（/docs/brief.html）。毎朝ここが自動更新される。
    docs = os.path.join(ROOT, "docs")
    os.makedirs(docs, exist_ok=True)
    with open(os.path.join(docs, "brief.html"), "w", encoding="utf-8") as f:
        f.write(html_str)
    print(f"[save] {path} + docs/brief.html")
    return path


def send_email(cfg, subject, html_str):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    mail_to = os.environ.get("MAIL_TO")
    # HOSTだけ有ってUSER/PASS/宛先が欠けている中途半端な状態でも落とさずスキップ。
    if not (host and user and pw and mail_to):
        print("[mail] SMTP情報が不足のため送信スキップ（HTML保存のみ）")
        return
    port = int(os.environ.get("SMTP_PORT", "465"))
    mail_from = os.environ.get("MAIL_FROM", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("朝の市況ブリーフ", mail_from))
    msg["To"] = mail_to
    msg.attach(MIMEText("HTML版でご覧ください。", "plain", "utf-8"))
    msg.attach(MIMEText(html_str, "html", "utf-8"))

    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(mail_from, [a.strip() for a in mail_to.split(",")], msg.as_string())
    print(f"[mail] 送信完了 → {mail_to}")


# ----------------------------------------------------------------------
# 6) Gemini編集モード（無料キーがある時だけ）＝5本厳選＋体言止め＋洞察＋Tier2接続
# ----------------------------------------------------------------------
def load_buzz_top(n=6):
    """Tier2バズ観察の最新日 上位を [(symbol, clean), ...] で返す。編集で中型株の話に絡める。"""
    path = os.path.join(ROOT, "social_history", "buzz_daily.csv")
    uni = os.path.join(ROOT, "config", "universe.csv")
    if not os.path.exists(path):
        return []
    tier = {}
    if os.path.exists(uni):
        with open(uni, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                tier[r["symbol"].strip().upper()] = r.get("tier", "").strip()
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    latest = max(r["date"] for r in rows)
    t2 = [r for r in rows if r["date"] == latest and tier.get(r["symbol"]) == "tier2"]
    t2.sort(key=lambda r: int(r.get("volume_n_clean") or 0), reverse=True)
    return [(r["symbol"], int(r.get("volume_n_clean") or 0)) for r in t2[:n]]


def _gemini_models(key):
    """このキーで generateContent 可能なモデルを flash優先で最大4つ返す（503時の切替候補）。"""
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": key}, timeout=30)
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        ordered = []
        for pref in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest", "flash"):
            for n in names:
                if pref in n and n not in ordered:
                    ordered.append(n)
        return ordered[:4] or names[:2]
    except Exception as e:
        print(f"[gemini] モデル一覧取得失敗: {e}", file=sys.stderr)
        return ["gemini-2.5-flash", "gemini-2.0-flash"]


def load_breakdown():
    """±ブレイク解剖の最新結果を読む（social_history/breakdown.json）。無ければNone。"""
    path = os.path.join(ROOT, "social_history", "breakdown.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def breakdown_card_html(bd):
    """解剖の実数字を直接カード化（LLMに言い換えさせない＝ファクト厳守）。冒頭に置く。"""
    if not bd or bd.get("index_pt") is None:
        return ""
    pt = bd["index_pt"]; ret = bd.get("index_ret_pct", 0)
    down = pt < 0
    culp = [c for c in bd.get("culprits", []) if c.get("in_vn30") and ((c["contrib_pt"] < 0) == down)][:3]
    # 一言の推定（外人主導 or 国内主導＝信用の可能性）
    fnets = [c.get("foreign_net_bn") for c in culp if c.get("foreign_net_bn") is not None]
    read = ""
    if fnets:
        if sum(1 for x in fnets if x < -10) >= 2:
            read = "外国人売りが主導。"
        elif all(abs(x) < 10 for x in fnets):
            read = "外国人はほぼ中立＝国内主体（信用の動きの可能性）。"
    resid = bd.get("residual_pt", 0); vn30c = bd.get("vn30_contrib_pt", 0)
    if abs(resid) > abs(vn30c):
        read += "下げの過半はVN30外＝広範。" if down else "上げの過半はVN30外＝広範。"
    def mat_cell(c):
        if c.get("has_material"):
            hd = esc((c.get("material_headline") or "")[:26])
            return f'<span style="color:#0a7d4b;">材料</span> <span style="color:#888;">{hd}</span>'
        return '<span style="color:#c0392b;">材料なし</span>'
    rows = "".join(
        f'<tr><td style="padding:3px 8px;font-weight:700;">{esc(c["symbol"])}</td>'
        f'<td style="padding:3px 8px;text-align:right;color:{"#c0392b" if c["ret_pct"]<0 else "#0a7d4b"};">{c["ret_pct"]:+.2f}%</td>'
        f'<td style="padding:3px 8px;text-align:right;">{c["contrib_pt"]:+.2f}pt</td>'
        f'<td style="padding:3px 8px;text-align:right;color:#555;white-space:nowrap;">'
        f'{"外人"+format(c["foreign_net_bn"],"+.0f")+"十億" if c.get("foreign_net_bn") is not None else "―"}</td>'
        f'<td style="padding:3px 8px;font-size:11px;">{mat_cell(c)}</td></tr>'
        for c in culp)
    brk = "⚑" if bd.get("is_break") else ""
    color = "#c0392b" if down else "#0a7d4b"
    nomat = bd.get("no_material_movers") or []
    nomat_line = (
        f'<div style="font-size:12px;color:#c0392b;margin-top:6px;font-weight:700;">'
        f'⚠ 材料なしで動いた: {esc("・".join(nomat))} → 掲示板バズを要確認</div>'
        if nomat else "")
    return (
        f'<div style="margin:0 0 14px;padding:14px 16px;background:#fff8f2;border-left:4px solid {color};">'
        f'<div style="font-size:11px;color:#999;letter-spacing:1px;">なぜ昨日動いたか（自動解剖・{esc(str(bd.get("date","")))}）</div>'
        f'<div style="font-size:17px;font-weight:800;margin:2px 0 6px;color:{color};">'
        f'{brk} VN-Index {pt:+.1f}pt（{ret:+.2f}%）</div>'
        f'<table style="border-collapse:collapse;font-size:12.5px;width:100%;">{rows}</table>'
        + (f'<div style="font-size:12px;color:#444;margin-top:6px;">▶ {esc(read)}</div>' if read else "")
        + nomat_line
        + '<div style="font-size:10px;color:#aaa;margin-top:4px;">寄与度＝時価総額加重（浮動株未補正の近似）。材料＝Google News直近。</div>'
        '</div>')


def editorial_with_gemini(cfg, items, buzz_top):
    """GEMINI_API_KEY がある時だけ、5本厳選＋編集をGeminiで実施。失敗/未設定はNone。"""
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not items:
        return None
    models = [cfg["gemini_model"]] if cfg.get("gemini_model") else _gemini_models(key)
    print(f"[gemini] 候補モデル: {models}", file=sys.stderr)
    tag_re = re.compile(r"<[^>]*>?")
    arts = []
    for i, it in enumerate(items):
        raw = html.unescape(html.unescape(it.get("snippet") or ""))
        txt = re.sub(r"\s+", " ", tag_re.sub(" ", raw)).strip()
        arts.append({"i": i, "src": it["source"], "cat": it["category"],
                     "title": it["title"], "text": txt[:300]})
    buzz_str = "、".join(f"{s}({c}件)" for s, c in buzz_top) if buzz_top else "（データなし）"
    prompt = (
        "あなたはベトナム株の朝ブリーフを書く、日系運用会社の投資調査部アナリストです。\n"
        "読み手はプロのFMと営業。**唯一の基準は『これ、人に話したくなるか？』**。\n\n"
        "【採用する＝話したくなるニュース】\n"
        "① 驚きの数字・事実（例『新規証券口座が半期で1,340万』）\n"
        "② 具体的イベントで示唆が明確（例『BSRがVN30指数に採用→パッシブ買いが入る』）\n"
        "③ 意外性・ドラマ・転換（誰かが動いた／流れが変わった／初めて／過去最大）\n"
        "④ 営業がFMとの会話の口火にできる・明日の売買判断に効く\n\n"
        "【落とす＝『ふーん』で終わる記事】\n"
        "・ありきたりな見通し/予測（『下期は銘柄選別が鍵』『上昇に期待』等、中身の無い願望）\n"
        "・角度の無い定型の配当発表（サプライズや影響が無ければ不採用）\n"
        "・どの日でも書ける一般論、既知の繰り返し\n\n"
        "ルール:\n"
        "- **強い5本が揃わなければ4本でよい**（弱い1本を足して薄めるな）。質＞本数。\n"
        "- 可能なら「個別株2〜3本＋マクロ2〜3本」。個別株は実際に動いた銘柄(cat=stock)を優先。\n"
        "- **外国人フローの話は毎回入れない**（顕著な時だけ1本）。\n"
        "- headline=フックを効かせた体言止め(8〜18字・数字や意外性を前に)。\n"
        "  body=3〜4文。1文目で『何が起きたか』、最後に『だから何か(so what)＝どう効く/どう動く』。\n"
        "- 中型株の話題は本日の掲示板バズ上位と絡めてよい→ " + buzz_str + "\n"
        "- 記事に無い数字は創作しない。source=媒体名。**i=元記事の番号を必ず**(主たる1本)。\n"
        "- others: top5に入らなかったが一応拾う小ネタ3本、一言見出しだけ。\n"
        "出力はJSONのみ(前置き・コードフェンス無し):\n"
        '{"top5":[{"i":番号,"headline":"","body":"","source":""}],"others":["",""]}\n\n'
        "記事:\n" + json.dumps(arts, ensure_ascii=False)
    )
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"}}
    # モデル×リトライで 503(高需要)/429(レート) を粘る。全滅で None（翻訳版フォールバック）。
    for model in models:
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": key}, json=body, timeout=60)
                data = r.json()
                if "candidates" in data:
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    text = text.replace("```json", "").replace("```", "").strip()
                    obj = json.loads(text)
                    if obj.get("top5"):
                        print(f"[gemini] 成功: {model}", file=sys.stderr)
                        return obj
                    print(f"[gemini] top5空: {text[:150]}", file=sys.stderr)
                    break  # 応答は来たが中身不正→別モデルへ
                code = (data.get("error") or {}).get("code")
                print(f"[gemini] {model} candidates無し HTTP{code}: {str(data)[:200]}", file=sys.stderr)
                if code in (503, 429):
                    time.sleep(4 * (attempt + 1))  # 混雑/レート→バックオフして再試行
                    continue
                break  # 他のエラー→別モデルへ
            except Exception as e:
                print(f"[gemini] {model} 例外: {e}", file=sys.stderr)
                time.sleep(3)
    print("[gemini] 全モデル失敗→翻訳版にフォールバック", file=sys.stderr)
    return None


def build_editorial_html(cfg, stamp, fx_rows, idx_rows, editorial, lead="", items=None):
    """Bloomberg形式の編集版HTML（sample.html準拠）。lead=解剖カード等を冒頭に。
    items=元記事プール（s['i']→リンク解決に使う）。"""
    title = cfg["output"]["title"]
    items = items or []
    snap = fx_rows + idx_rows
    tiles = "".join(
        f'<td style="padding:2px 12px 2px 0;vertical-align:top;white-space:nowrap;">'
        f'<div style="font-size:10px;color:#9db0c8;">{esc(r["label"])}</div>'
        f'<div style="font-size:16px;color:#fff;font-weight:700;">{esc(r["value"])}</div></td>'
        for r in snap
    ) or '<td style="color:#9db0c8;font-size:12px;">数値取得なし</td>'

    def link_of(s):
        i = s.get("i")
        if isinstance(i, int) and 0 <= i < len(items):
            return items[i].get("link", "")
        return ""

    def story(s):
        hd = esc(s.get("headline", ""))
        url = link_of(s)
        head = (f'<a href="{esc(url)}" style="color:#111;text-decoration:none;">{hd}</a>'
                if url else hd)
        src = (f'<span style="color:#888;font-size:11px;"> — {esc(s.get("source",""))}'
               + (' ↗' if url else '') + '</span>' if s.get("source") else "")
        return (f'<div style="padding:16px 0;border-bottom:1px solid #eee;">'
                f'<div style="font-size:16px;font-weight:800;">{head}</div>'
                f'<div style="font-size:13.5px;color:#333;line-height:1.85;margin-top:6px;">'
                f'{esc(s.get("body",""))}{src}</div></div>')
    top5 = editorial.get("top5", [])
    n_top = len(top5)
    stories = "".join(story(s) for s in top5)
    others = "".join(f'・{esc(o)}<br>' for o in editorial.get("others", [])) or "―"

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title></head>
<body style="margin:0;background:#f4f4f4;font-family:'Hiragino Kaku Gothic ProN','Meiryo',sans-serif;color:#111;">
<div style="max-width:680px;margin:0 auto;background:#fff;">
  <div style="padding:22px 26px 14px;border-bottom:3px solid #111;">
    <div style="font-size:11px;letter-spacing:2px;color:#0a7d4b;font-weight:700;">CQC 投資調査部</div>
    <div style="font-size:23px;font-weight:800;letter-spacing:-.5px;margin-top:3px;">{esc(title)}</div>
    <div style="font-size:12px;color:#777;margin-top:3px;">{esc(stamp)}　朝の注目{n_top}本</div>
    <div style="font-size:13px;color:#333;line-height:1.7;margin-top:10px;">
      昨日のベトナム市場と、今日を始めるにあたって押さえておきたい注目ニュースをお届けします。</div>
  </div>
  <div style="padding:14px 26px;background:#0f1b2d;">
    <table style="border-collapse:collapse;"><tr>{tiles}</tr></table>
    <div style="font-size:10px;color:#6b7c93;margin-top:6px;">指数はYahoo Finance、為替は open.er-api.com。参考値。</div>
  </div>
  <div style="padding:12px 26px 0;">{lead}</div>
  <div style="padding:6px 26px 4px;">{stories}</div>
  <div style="padding:14px 26px;background:#fafafa;border-top:1px solid #eee;">
    <div style="font-size:12px;font-weight:700;color:#666;letter-spacing:1px;margin-bottom:8px;">その他の注目ニュース</div>
    <div style="font-size:13px;color:#222;line-height:1.9;">{others}</div>
  </div>
  <div style="padding:14px 26px 22px;font-size:11px;color:#999;line-height:1.7;">
    出典: VnExpress／CafeF／Đầu tư Chứng khoán／VnEconomy 等、指数はYahoo Finance、掲示板はFireAnt。
    本資料は社内参考であり投資助言ではありません。編集はGeminiによる5本厳選・要約。
  </div>
</div></body></html>"""


# ----------------------------------------------------------------------
def main():
    cfg = load_config()
    ts = now_jst(cfg)
    stamp = ts.strftime("%Y年%m月%d日(%a) %H:%M JST")
    stamp_file = ts.strftime("%Y%m%d")

    print("[1/4] 為替・指数取得…")
    fx_rows = fetch_fx(cfg)
    idx_rows = fetch_indices(cfg)
    print("[2/4] ニュース収集…")
    items = fetch_news(cfg)
    print(f"       マクロ{len(items)}件収集")
    # 動いた銘柄（前夜の解剖の犯人）の個別ニュースをプールに追加＝5本に個別株を混ぜる材料
    bd = load_breakdown()
    movers = [c["symbol"] for c in (bd.get("culprits") if bd else [])][:6]
    if movers:
        stock_items = fetch_stock_news(movers, per=2)
        items = items + stock_items
        print(f"       個別株{len(stock_items)}件追加（{', '.join(movers)}）")

    print("[3/4] 編集・要約…")
    # ±ブレイク解剖カード（前夜の大引け後に生成された実数字を冒頭に・LLM言い換えなし）
    lead = breakdown_card_html(bd)
    if lead:
        print("       解剖カードを冒頭に挿入")
    # 優先度: Gemini編集モード（無料キー時・Bloomberg形式） > Claude/翻訳版
    buzz_top = load_buzz_top()
    editorial = editorial_with_gemini(cfg, items, buzz_top)
    if editorial:
        print(f"       Gemini編集モード（{len(editorial.get('top5', []))}本厳選）")
        html_str = build_editorial_html(cfg, stamp, fx_rows, idx_rows, editorial, lead, items)
    else:
        news = summarize_with_claude(cfg, items)
        print(f"       翻訳/Claude版 {len(news)}件採用")
        html_str = build_html(cfg, stamp, fx_rows, idx_rows, news, lead)

    print("[4/4] HTML生成・保存・送信…")
    save_html(cfg, stamp_file, html_str)
    subject = f"【朝の市況ブリーフ】{ts.strftime('%m/%d')}"
    send_email(cfg, subject, html_str)
    print("完了。")


if __name__ == "__main__":
    main()
