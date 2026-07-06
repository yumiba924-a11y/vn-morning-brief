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
import sys
import json
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


# ----------------------------------------------------------------------
# 3) Claudeで翻訳＋要約＋示唆（全記事を1コールでJSON化 = 低コスト）
# ----------------------------------------------------------------------
def summarize_with_claude(cfg, items):
    if not items:
        return []

    # 無料モード: ANTHROPIC_API_KEY が無ければ Claude をスキップし、原文見出しのまま通す。
    # （日本語訳・3行要約・示唆は付かないが、費用ゼロで配信できる）
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[claude] APIキー未設定 → 無料モード（要約なし・原文見出しのまま）", file=sys.stderr)
        # <[^>]*>? … 閉じ ">" を任意にして、URLが途中で切れた未完タグ(<a href="…)も除去
        tag_re = re.compile(r"<[^>]*>?")
        for it in items:
            it["jp_title"] = it["title"]
            # RSSのsnippetはHTML。二重エスケープ解除 → タグ除去 の順で確実にテキスト化。
            raw = html.unescape(html.unescape(it.get("snippet") or ""))
            txt = re.sub(r"\s+", " ", tag_re.sub(" ", raw)).strip()
            # 実テキストが無い（リンクだけの）記事は要約空欄＝見出しのみ表示にする。
            it["summary"] = txt[:160] if len(txt) >= 20 else ""
            it["implication"] = ""
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


def build_html(cfg, stamp, fx_rows, idx_rows, news):
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

  <div style="font-size:12px;font-weight:700;color:{C['sub']};letter-spacing:1px;
       margin:20px 0 2px;">マクロ・市場全体</div>
  {news_block(macro)}

  <div style="font-size:12px;font-weight:700;color:{C['sub']};letter-spacing:1px;
       margin:20px 0 2px;">現地材料（ベトナム）</div>
  {news_block(local)}

  <div style="margin-top:22px;padding-top:10px;border-top:1px solid {C['line']};
       font-size:11px;color:{C['sub']};">
    自動生成: 為替は open.er-api.com、指数は Yahoo Finance、ニュースはGoogle News/各RSS、
    翻訳・要約はClaude API（APIキー設定時）。数値の確定値は原典でご確認ください。
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
    print(f"[save] {path}")
    return path


def send_email(cfg, subject, html_str):
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("[mail] SMTP未設定のため送信スキップ（HTML保存のみ）")
        return
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    pw = os.environ["SMTP_PASS"]
    mail_to = os.environ["MAIL_TO"]
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
    print(f"       {len(items)}件収集")
    print("[3/4] Claudeで翻訳・要約…")
    news = summarize_with_claude(cfg, items)
    print(f"       {len(news)}件採用")
    print("[4/4] HTML生成・保存・送信…")
    html_str = build_html(cfg, stamp, fx_rows, idx_rows, news)
    save_html(cfg, stamp_file, html_str)
    subject = f"【朝の市況ブリーフ】{ts.strftime('%m/%d')}"
    send_email(cfg, subject, html_str)
    print("完了。")


if __name__ == "__main__":
    main()
