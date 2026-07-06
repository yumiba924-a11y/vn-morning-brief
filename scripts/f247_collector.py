#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
f247_collector.py — F247(Discourse) ソーシャル収集＝第2ソース

F247 は Discourse フォーラム（認証不要・Cloudflareなし）。銘柄は
ticker タグで管理されており /tag/{SYMBOL}.json で銘柄別に取れる。
FireAnt(social_collector.py)と揃えて、日次バズを social_history に貯める。

各銘柄の日次指標（VN 市場日 ICT で切る）:
  - topics_active_24h : 直近24hに投稿があったスレ数
  - topics_new_24h    : 直近24hに立った新規スレ数
  - sum_posts         : タグ配下スレの累計投稿数スナップ（翌日差分＝日次投稿ペース）
  - sum_views         : 同 累計view

出力:
  - social_history/buzz_f247_daily.csv  （1行/銘柄/日・同一dateは置換＝冪等）
  - <outdir>/f247_topics.jsonl          （当日拾ったスレの生メタ・アーカイブ用）

依存: 標準ライブラリのみ。実行: python scripts/f247_collector.py --symbols-file config/universe.csv
"""
from __future__ import annotations
import os, csv, json, time, argparse
import urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

BASE = "https://f247.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def http_get_json(url: str, retries: int = 3, timeout: int = 25):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"_http_error": 404}
            last = e
            time.sleep(1.5 * (i + 1))
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    return {"_error": str(last)}


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
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


def collect_symbol(sym, since):
    """1銘柄ぶんの日次指標＋生スレメタを返す。"""
    d = http_get_json(f"{BASE}/tag/{sym}.json")
    topics = d.get("topic_list", {}).get("topics", []) if isinstance(d, dict) else []
    active = new = sum_posts = sum_views = 0
    raw = []
    for t in topics:
        lp = parse_dt(t.get("last_posted_at") or t.get("bumped_at"))
        cr = parse_dt(t.get("created_at"))
        pc = t.get("posts_count") or 0
        vw = t.get("views") or 0
        sum_posts += pc
        sum_views += vw
        if lp and lp >= since:
            active += 1
        if cr and cr >= since:
            new += 1
        raw.append({
            "symbol": sym, "topic_id": t.get("id"), "title": t.get("title"),
            "posts_count": pc, "reply_count": t.get("reply_count"),
            "views": vw, "like_count": t.get("like_count"),
            "created_at": t.get("created_at"), "last_posted_at": t.get("last_posted_at"),
            "tags": t.get("tags"), "slug": t.get("slug"),
            "url": f"{BASE}/t/{t.get('slug')}/{t.get('id')}" if t.get("id") else None,
        })
    return {
        "symbol": sym, "topics_active_24h": active, "topics_new_24h": new,
        "sum_posts": sum_posts, "sum_views": sum_views, "n_topics_seen": len(topics),
    }, raw


def append_history(path, run_date, rows):
    cols = ["date", "symbol", "topics_active_24h", "topics_new_24h", "sum_posts", "sum_views"]
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    existing = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            existing = [r for r in csv.DictReader(f) if r.get("date") != run_date]
    for r in rows:
        existing.append({"date": run_date, **{k: r.get(k, 0) for k in cols[1:]}})
    existing.sort(key=lambda r: (r["date"], r["symbol"]))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in existing:
            w.writerow({k: r.get(k, "") for k in cols})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=["VIC", "HPG", "DIG"])
    ap.add_argument("--symbols-file", default=None)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--history", default=None)
    ap.add_argument("--date", default=None)
    ap.add_argument("--outdir", default="social_out")
    args = ap.parse_args()

    symbols = read_symbols(args.symbols_file, args.symbols)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)
    run_date = args.date or (now + timedelta(hours=7)).strftime("%Y-%m-%d")
    os.makedirs(args.outdir, exist_ok=True)

    rows, all_raw = [], []
    for sym in symbols:
        metrics, raw = collect_symbol(sym, since)
        rows.append(metrics)
        all_raw.extend(raw)
        print(f"  {sym:5} active24h={metrics['topics_active_24h']:2} "
              f"new={metrics['topics_new_24h']:2} posts={metrics['sum_posts']:6}")
        time.sleep(0.35)   # Discourse への礼儀（匿名read）

    with open(os.path.join(args.outdir, "f247_topics.jsonl"), "w", encoding="utf-8") as f:
        for r in all_raw:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if args.history:
        append_history(args.history, run_date, rows)
        print(f"history += {run_date} ({len(rows)}銘柄) -> {args.history}")

    print(f"\nF247 合計 {len(all_raw)} スレメタ / {len(symbols)} 銘柄")


if __name__ == "__main__":
    main()
