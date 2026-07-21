#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weekly_digest.py — 日次エンジン→週次への橋渡し。
covered.jsonl(日次で拾った5本)＋バズ/フローを直近5営業日ぶん集約し、
Geminiで「今週のニュース」に編集→ social_history/weekly_digest.json ＋ docs/weekly.html。
月曜のウィークリーが weekly_digest.json を「今週の材料」として読む。
"""
import os, re, json, html
import datetime as dt
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COVERED = os.path.join(ROOT, "social_history", "covered.jsonl")
BUZZ = os.path.join(ROOT, "social_history", "buzz_daily.csv")
ROOM = os.path.join(ROOT, "social_history", "room_daily.csv")
NAMES = os.path.join(ROOT, "config", "names.csv")


def esc(s):
    return html.escape(str(s or ""))


def load_week(days=7):
    if not os.path.exists(COVERED):
        return []
    cutoff = ((dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=7)) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for line in open(COVERED, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if str(r.get("date", "")) >= cutoff and r.get("headlines"):
            out.append((r["date"], r["headlines"]))
    return sorted(out)


def gemini_models(key):
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key}, timeout=30)
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        out = []
        for p in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest", "flash"):
            for n in names:
                if p in n and n not in out:
                    out.append(n)
        return out[:4] or names[:2]
    except Exception:
        return ["gemini-2.5-flash", "gemini-2.0-flash"]


def gemini_digest(week):
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not week:
        return None
    days = "\n".join(f"【{d}】\n" + "\n".join("・" + h for h in hs) for d, hs in week)
    label = f"{week[0][0]}〜{week[-1][0]}"
    prompt = (
        "あなたはベトナム株の週次レポートを書く日系運用会社の投資調査部アナリストです。\n"
        "下は今週、日次ブリーフで拾った注目見出しです。これを束ね『今週のベトナム市況』に編集してください。\n"
        "読み手はプロのFMと営業。既視感回避は不要（束ねるのが目的）。\n"
        "ルール: one_liner=今週を一言で(1文・言い切る)。themes=今週の軸3〜5本"
        "（title=体言止め見出し、points=2〜4個の要点。同じ話題を束ねる）。"
        "dramas=個別銘柄の目玉3〜5本（一言）。数字は見出しにあるものだけ。断定しすぎない。\n"
        "銘柄は『社名（ティッカー）』表記。\n"
        f"week_label は「{label}」。\n"
        "出力はJSONのみ(前置き無し):\n"
        '{"week_label":"","one_liner":"","themes":[{"title":"","points":["",""]}],"dramas":["",""]}\n\n'
        "今週の見出し:\n" + days
    )
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.5, "responseMimeType": "application/json"}}
    import time
    for model in gemini_models(key):
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": key}, json=body, timeout=60)
                data = r.json()
                if "candidates" in data:
                    txt = data["candidates"][0]["content"]["parts"][0]["text"].replace("```json", "").replace("```", "").strip()
                    obj = json.loads(txt)
                    if obj.get("themes"):
                        obj.setdefault("week_label", label)
                        print(f"[gemini] 週次digest成功: {model}")
                        return obj
                    break
                code = (data.get("error") or {}).get("code")
                if code in (503, 429):
                    time.sleep(4 * (attempt + 1)); continue
                break
            except Exception as e:
                print(f"[gemini] {model} {e}"); time.sleep(3)
    return None


def render_html(dg, week):
    themes = "".join(
        f'<div style="margin:14px 0;"><div style="font-size:15px;font-weight:800;color:#0a7d4b;">■ {esc(t.get("title"))}</div>'
        f'<ul style="margin:6px 0 0;padding-left:20px;font-size:13.5px;line-height:1.8;color:#222;">'
        + "".join(f"<li>{esc(p)}</li>" for p in t.get("points", [])) + "</ul></div>"
        for t in dg.get("themes", []))
    dramas = "".join(f"・{esc(x)}<br>" for x in dg.get("dramas", [])) or "―"
    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>今週のベトナム {esc(dg.get('week_label'))}</title></head>
<body style="margin:0;background:#f4f4f4;font-family:'Hiragino Kaku Gothic ProN','Meiryo',sans-serif;color:#111;">
<div style="max-width:720px;margin:0 auto;background:#fff;">
  <div style="padding:22px 26px 14px;border-bottom:3px solid #0a7d4b;">
    <div style="font-size:11px;letter-spacing:2px;color:#0a7d4b;font-weight:700;">CQC 投資調査部 ｜ 今週の材料</div>
    <div style="font-size:22px;font-weight:800;margin-top:2px;">今週のベトナム市況</div>
    <div style="font-size:12px;color:#777;margin-top:2px;">{esc(dg.get('week_label'))}（日次ブリーフの集約）</div>
    <div style="font-size:15px;color:#111;line-height:1.7;margin-top:12px;font-weight:700;">
      {esc(dg.get('one_liner'))}</div>
  </div>
  <div style="padding:14px 26px;">{themes}
    <div style="margin-top:18px;padding-top:12px;border-top:1px solid #eee;">
      <div style="font-size:13px;font-weight:800;color:#666;margin-bottom:6px;">今週の個別ドラマ</div>
      <div style="font-size:13px;line-height:1.9;color:#222;">{dramas}</div></div>
  </div>
  <div style="padding:12px 26px 20px;font-size:10px;color:#999;">
    日次ブリーフ(covered.jsonl)の1週間分をGeminiが集約。社内参考・投資助言ではありません。</div>
</div></body></html>"""


def _clean(x):
    return re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", " ", x))).strip()


def build_raw(week):
    """今週の日次ブリーフHTML(output/brief-YYYYMMDD.html)から全ニュースを丸ごと集約(編集なし)。
    5本(見出し＋本文＋リンク)＋その他 を日付別に。ウィークリー執筆の"素材"用。"""
    days = []
    for d, _ in week:
        ymd = d.replace("-", "")
        p = os.path.join(ROOT, "output", f"brief-{ymd}.html")
        if not os.path.exists(p):
            continue
        h = open(p, encoding="utf-8").read()
        stories = []
        # 見出し(＋任意リンク)＋本文 を取る
        for m in re.finditer(r'font-size:16px;font-weight:800;">(.*?)</div>\s*<div style="font-size:13\.5px[^>]*>(.*?)</div>', h, re.S):
            head_raw, body_raw = m.group(1), m.group(2)
            link = ""
            lm = re.search(r'href="([^"]+)"', head_raw)
            if lm:
                link = lm.group(1)
            stories.append({"headline": _clean(head_raw), "body": _clean(body_raw), "link": link})
        others = ""
        om = re.search(r'その他の注目ニュース.*?line-height:1\.9;">(.*?)</div>', h, re.S)
        if om:
            others = _clean(om.group(1).replace("<br>", " / "))
        days.append({"date": d, "stories": stories, "others": others})
    return days


def render_raw_html(days, label):
    blocks = []
    for day in days:
        arts = "".join(
            f'<div style="margin:10px 0;padding-bottom:8px;border-bottom:1px solid #f0f0f0;">'
            f'<div style="font-size:14px;font-weight:700;">'
            + (f'<a href="{esc(a["link"])}" style="color:#111;">{esc(a["headline"])}</a> ↗' if a["link"] else esc(a["headline"]))
            + f'</div><div style="font-size:12.5px;color:#333;line-height:1.75;margin-top:3px;">{esc(a["body"])}</div></div>'
            for a in day["stories"])
        oth = f'<div style="font-size:12px;color:#666;margin-top:6px;">その他: {esc(day["others"])}</div>' if day["others"] else ""
        blocks.append(
            f'<div style="margin:16px 0;"><div style="font-size:15px;font-weight:800;color:#0a7d4b;'
            f'border-bottom:2px solid #0a7d4b;padding-bottom:3px;">{esc(day["date"])}</div>{arts}{oth}</div>')
    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>今週の素材 {esc(label)}</title></head>
<body style="margin:0;background:#f4f4f4;font-family:'Hiragino Kaku Gothic ProN','Meiryo',sans-serif;color:#111;">
<div style="max-width:760px;margin:0 auto;background:#fff;">
  <div style="padding:20px 26px 12px;border-bottom:3px solid #111;">
    <div style="font-size:11px;letter-spacing:2px;color:#0a7d4b;font-weight:700;">CQC 投資調査部 ｜ 今週の素材（編集前・全件）</div>
    <div style="font-size:21px;font-weight:800;margin-top:2px;">今週の材料まるごと</div>
    <div style="font-size:12px;color:#777;margin-top:2px;">{esc(label)}／日次ブリーフの全ニュースを無編集で集約</div>
  </div>
  <div style="padding:8px 26px 22px;">{"".join(blocks)}</div>
</div></body></html>"""


def main():
    week = load_week()[-5:]  # 直近5営業日(月〜金)
    if not week:
        print("今週の covered データなし。中断。"); return
    dg = gemini_digest(week)
    if not dg:  # フォールバック: 見出しを日付別に列挙
        dg = {"week_label": f"{week[0][0]}〜{week[-1][0]}", "one_liner": "（Gemini未実行・見出し集約）",
              "themes": [{"title": d, "points": hs} for d, hs in week], "dramas": []}
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
    with open(os.path.join(ROOT, "social_history", "weekly_digest.json"), "w", encoding="utf-8") as f:
        json.dump(dg, f, ensure_ascii=False, indent=2)
    with open(os.path.join(ROOT, "docs", "weekly.html"), "w", encoding="utf-8") as f:
        f.write(render_html(dg, week))
    # 素材まるごと版（編集前・全ニュース）
    raw = build_raw(week)
    with open(os.path.join(ROOT, "docs", "weekly_raw.html"), "w", encoding="utf-8") as f:
        f.write(render_raw_html(raw, dg.get("week_label", "")))
    with open(os.path.join(ROOT, "social_history", "weekly_raw.json"), "w", encoding="utf-8") as f:
        json.dump({"week_label": dg.get("week_label"), "days": raw}, f, ensure_ascii=False, indent=2)
    n = sum(len(d["stories"]) for d in raw)
    print(f"[weekly] digest テーマ{len(dg.get('themes', []))}本 ＋ 素材 {n}本({len(raw)}日) → weekly.html / weekly_raw.html")


if __name__ == "__main__":
    main()
