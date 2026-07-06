#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
send_view.py — Tier2バズ観察ビューをメール送信（SMTP未設定なら黙ってスキップ）

朝ブリーフと同じ SMTP_* / MAIL_* シークレットを使い回す。
Secretsが無い環境では何もせず正常終了する（social収集の「Secrets不要」を維持）。
"""
import os, sys, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEW = os.path.join(ROOT, "output", "social_latest.html")


def main():
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    mail_to = os.environ.get("MAIL_TO")
    # HOSTだけ有ってUSER/PASS/宛先が欠けている中途半端な状態でも落とさずスキップ。
    if not (host and user and pw and mail_to):
        print("[send_view] SMTP情報が不足のため送信スキップ（HTMLは生成済）")
        return
    if not os.path.exists(VIEW):
        print(f"[send_view] ビューが無い: {VIEW}", file=sys.stderr)
        return

    with open(VIEW, encoding="utf-8") as f:
        html_str = f.read()

    port = int(os.environ.get("SMTP_PORT", "465"))
    mail_from = os.environ.get("MAIL_FROM", user)

    md = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%m/%d")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"【Tier2バズ観察】{md}"
    msg["From"] = formataddr(("ベトナムTier2バズ観察", mail_from))
    msg["To"] = mail_to
    msg.attach(MIMEText("HTML版でご覧ください（内部参考）。", "plain", "utf-8"))
    msg.attach(MIMEText(html_str, "html", "utf-8"))

    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(mail_from, [a.strip() for a in mail_to.split(",")], msg.as_string())
    print(f"[send_view] 送信完了 → {mail_to}")


if __name__ == "__main__":
    main()
