#!/usr/bin/env python3
"""
social_collector.py — Fireant ソーシャル収集＋正規化（要約はしない）

朝の市況ブリーフ / ベトナムウィークリー の三層ユニバース Tier2 トリガー材料。
2レイヤーで収集する:
  - quality (type=1): 厳選された投稿/ニュース/専門家アイデア → テキストを残す
  - volume  (type=0): コミュニティ雑談フィード → 件数（バズ）＋テキスト

出力（--outdir、既定 ./social_out）:
  - social_posts.jsonl : 正規化済み投稿レコード（quality + volume 両方）
  - social_buzz.csv    : 銘柄×レイヤーの直近件数サマリ（スパム除外版も併記）
  - social_meta.json   : 実行メタ（as_of, window, token exp 等）

正規化スキーマ（1レコード）:
  source, layer, symbol, post_id, timestamp, author, text, text_len,
  url, likes, replies, shares, sentiment, is_expert, is_ai,
  tagged_symbols, is_broadcast

依存: 標準ライブラリのみ（urllib）。実行: python social_collector.py
"""
from __future__ import annotations
import os, re, csv, json, time, base64, argparse
import urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

# --- FireAnt 公開トークン（フロントJS埋め込み・scope=posts-read/exp 2029-11）------------
# 個人資格ではなくアプリ共通の公開JWT。env で上書き可能。
PUBLIC_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiIsIng1dCI6IkdYdExONzViZlZQakdvNERWdjV4QkRITHpnSSIsImtpZCI6IkdYdExONzViZlZQakdvNERWdjV4QkRITHpnSSJ9"
    ".eyJpc3MiOiJodHRwczovL2FjY291bnRzLmZpcmVhbnQudm4iLCJhdWQiOiJodHRwczovL2FjY291bnRzLmZpcmVhbnQudm4vcmVzb3VyY2VzIiwiZXhwIjoxODg5NjIyNTMwLCJuYmYiOjE1ODk2MjI1MzAsImNsaWVudF9pZCI6ImZpcmVhbnQudHJhZGVzdGF0aW9uIiwic2NvcGUiOlsiYWNhZGVteS1yZWFkIiwiYWNhZGVteS13cml0ZSIsImFjY291bnRzLXJlYWQiLCJhY2NvdW50cy13cml0ZSIsImJsb2ctcmVhZCIsImNvbXBhbmllcy1yZWFkIiwiZmluYW5jZS1yZWFkIiwiaW5kaXZpZHVhbHMtcmVhZCIsImludmVzdG9wZWRpYS1yZWFkIiwib3JkZXJzLXJlYWQiLCJvcmRlcnMtd3JpdGUiLCJwb3N0cy1yZWFkIiwicG9zdHMtd3JpdGUiLCJzZWFyY2giLCJzeW1ib2xzLXJlYWQiLCJ1c2VyLWRhdGEtcmVhZCIsInVzZXItZGF0YS13cml0ZSIsInVzZXJzLXJlYWQiXSwianRpIjoiMjYxYTZhYWQ2MTQ5Njk1ZmJiYzcwODM5MjM0Njc1NWQifQ"
    ".dA5-HVzWv-BRfEiAd24uNBiBxASO-PAyWeWESovZm_hj4aXMAZA1-bWNZeXt88dqogo18AwpDQ-h6gefLPdZSFrG5umC1dVWaeYvUnGm62g4XS29fj6p01dhKNNqrsu5KrhnhdnKYVv9VdmbmqDfWR8wDgglk5cJFqalzq6dJWJInFQEPmUs9BW_Zs8tQDn-i5r4tYq2U8vCdqptXoM7YgPllXaPVDeccC9QNu2Xlp9WUvoROzoQXg25lFub1IYkTrM66gJ6t9fJRZToewCt495WNEOQFa_rwLCZ1QwzvL0iYkONHS_jZ0BOhBCdW9dWSawD6iF1SIQaFROvMDH1rg"
)
TOKEN = os.environ.get("FIREANT_TOKEN", PUBLIC_TOKEN)
BASE = "https://restv2.fireant.vn"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# type コード（実測）: 0=コミュニティ雑談(量), 1=厳選/ニュース/専門家(質)
LAYER_TYPE = {"volume": 0, "quality": 1}

# 既定ユニバース（Tier1=VN30 の主要どころ。--symbols で上書き可）
DEFAULT_SYMBOLS = ["VIC","VHM","VRE","VCB","BID","CTG","TCB","VPB","MBB","ACB",
                   "FPT","HPG","GAS","MWG","MSN","VNM","SSI","VJC","PLX","POW"]

BROADCAST_TAG_THRESHOLD = 6   # これ超のタグ数はブロードキャスト（煽り/宣伝）とみなす
TAG_RE = re.compile(r"<[^>]+>")


def http_get_json(url: str, retries: int = 3, timeout: int = 30):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {TOKEN}", "User-Agent": UA,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return {"_http_error": e.code}
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    return {"_error": str(last)}


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    txt = TAG_RE.sub(" ", raw)
    txt = txt.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", txt).strip()


def parse_dt(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def normalize(post: dict, symbol: str, layer: str) -> dict:
    user = post.get("user") or {}
    tags = [t.get("symbol") for t in (post.get("taggedSymbols") or []) if t.get("symbol")]
    body = post.get("originalContent") or post.get("content") or post.get("title") or ""
    text = clean_text(body)
    pid = post.get("postID")
    return {
        "source": "fireant",
        "layer": layer,
        "symbol": symbol,
        "post_id": pid,
        "timestamp": post.get("date"),
        "author": user.get("name") or post.get("userName"),
        "author_id": user.get("id"),
        "text": text,
        "text_len": len(text),
        # url = 人間用アプリリンク（SPAがクライアント描画）／api_url = 検証済みJSON実体
        "url": f"https://fireant.vn/dashboard/{pid}" if pid else None,
        "api_url": f"https://restv2.fireant.vn/posts/{pid}" if pid else None,
        "likes": post.get("totalLikes") or 0,
        "replies": post.get("totalReplies") or 0,
        "shares": post.get("totalShares") or 0,
        "sentiment": post.get("sentiment"),
        "is_expert": bool(post.get("isExpertIdea")),
        "is_ai": bool(post.get("isAIGenerated")),
        "tagged_symbols": tags,
        "is_broadcast": len(tags) > BROADCAST_TAG_THRESHOLD,
    }


def fetch_layer(symbol: str, layer: str, since: datetime, limit: int, max_pages: int):
    """指定レイヤーを since まで遡って取得（正規化済みレコードのリスト）。"""
    tcode = LAYER_TYPE[layer]
    out, offset, pages = [], 0, 0
    while pages < max_pages:
        url = f"{BASE}/posts?symbol={symbol}&type={tcode}&offset={offset}&limit={limit}"
        data = http_get_json(url)
        if not isinstance(data, list) or not data:
            break
        stop = False
        for p in data:
            dt = parse_dt(p.get("date"))
            if dt and dt < since:
                stop = True
                break
            out.append(normalize(p, symbol, layer))
        pages += 1
        offset += limit
        if stop or len(data) < limit:
            break
        time.sleep(0.25)
    return out


def token_exp():
    try:
        payload = TOKEN.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(claims["exp"], tz=timezone.utc).isoformat()
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--symbols-file", default=None,
                    help="CSV/テキストから銘柄を読む（1列目=symbol・ヘッダ行'symbol'は無視）")
    ap.add_argument("--hours", type=int, default=24, help="遡る時間窓（時間）")
    ap.add_argument("--limit", type=int, default=50, help="1ページ件数")
    ap.add_argument("--max-pages", type=int, default=3, help="レイヤー×銘柄あたり最大ページ")
    ap.add_argument("--outdir", default="social_out")
    ap.add_argument("--history", default=None,
                    help="指定すると日次バズ件数をこのCSVに追記（ベースライン蓄積用）")
    ap.add_argument("--date", default=None,
                    help="履歴の日付(YYYY-MM-DD)。既定はICT(UTC+7)の当日")
    args = ap.parse_args()

    symbols = args.symbols
    if args.symbols_file:
        symbols = []
        with open(args.symbols_file, encoding="utf-8-sig") as f:
            for line in f:
                s = line.split(",")[0].strip().upper()
                if s and s != "SYMBOL":
                    symbols.append(s)

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)
    # 履歴の日付ラベルは VN 市場日（ICT=UTC+7）で切る
    run_date = args.date or (now + timedelta(hours=7)).strftime("%Y-%m-%d")
    os.makedirs(args.outdir, exist_ok=True)

    all_records, buzz = [], []
    seen = set()  # (layer, post_id) 重複排除
    for sym in symbols:
        row = {"symbol": sym}
        for layer in ("quality", "volume"):
            recs = fetch_layer(sym, layer, since, args.limit, args.max_pages)
            fresh = []
            for r in recs:
                key = (layer, r["post_id"])
                if key in seen:
                    continue
                seen.add(key)
                fresh.append(r)
            all_records.extend(fresh)
            n_all = len(fresh)
            n_clean = sum(1 for r in fresh if not r["is_broadcast"])
            row[f"{layer}_n"] = n_all
            row[f"{layer}_n_clean"] = n_clean
        buzz.append(row)
        print(f"  {sym:5}  quality={row['quality_n']:3}  volume={row['volume_n']:3} "
              f"(clean {row['volume_n_clean']:3})")
        time.sleep(0.2)

    # 出力
    posts_path = os.path.join(args.outdir, "social_posts.jsonl")
    with open(posts_path, "w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    buzz_path = os.path.join(args.outdir, "social_buzz.csv")
    with open(buzz_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "quality_n", "volume_n",
                                          "volume_n_clean", "quality_n_clean"])
        w.writeheader()
        for row in sorted(buzz, key=lambda x: x.get("volume_n_clean", 0), reverse=True):
            w.writerow({k: row.get(k, 0) for k in
                        ["symbol","quality_n","volume_n","volume_n_clean","quality_n_clean"]})

    meta = {
        "source": "fireant/restv2",
        "as_of": now.isoformat(),
        "window_hours": args.hours,
        "n_symbols": len(symbols),
        "n_records": len(all_records),
        "token_exp": token_exp(),
        "endpoint": f"{BASE}/posts?symbol=&type=&offset=&limit=",
    }
    with open(os.path.join(args.outdir, "social_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 履歴CSVへ追記（ベースライン蓄積）。同一 date は上書き＝再実行しても重複しない。
    if args.history:
        append_history(args.history, run_date, buzz)
        print(f"history += {run_date} ({len(buzz)}銘柄) -> {args.history}")

    print(f"\n合計 {len(all_records)} レコード / {len(symbols)} 銘柄")
    print(f"saved: {posts_path}")
    print(f"saved: {buzz_path}")
    print(f"token exp: {meta['token_exp']}")


def append_history(path: str, run_date: str, buzz: list[dict]):
    """日次バズ件数を 1行/銘柄 で追記。同一 date の既存行は置換（冪等）。"""
    cols = ["date", "symbol", "quality_n", "volume_n", "volume_n_clean"]
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    rows = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if r.get("date") != run_date]
    for b in buzz:
        rows.append({
            "date": run_date, "symbol": b["symbol"],
            "quality_n": b.get("quality_n", 0), "volume_n": b.get("volume_n", 0),
            "volume_n_clean": b.get("volume_n_clean", 0),
        })
    rows.sort(key=lambda r: (r["date"], r["symbol"]))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


if __name__ == "__main__":
    main()
