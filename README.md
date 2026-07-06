# 朝の市況ブリーフ

英語・ベトナム語・日本語のニュースと為替を毎朝集めて、Claudeが日本語で翻訳・要約し、
**メール送信＋GitHubにHTML蓄積**するGitHub Actionsパイプライン。
ししまるの「収集→加工→出力」の型を市況ブリーフ用に組み直したもの。

## 仕組み（3層）
1. **収集** … 為替(open.er-api.com)＋ニュース(Google News RSS/各RSS)。言語非依存でただ取ってくるだけ。
2. **加工** … 全記事をまとめてClaude APIに1回投げ、翻訳＋3行要約＋一言示唆をJSONで受け取る（低コスト）。
3. **出力** … 最上部にマーケットスナップショット(為替＋指数を横並び)、その下にマクロ(英語ソース)・現地材料(ベトナム語ソース)を、体言止め見出し＋3行要約＋一言示唆で。メール送信し、`output/`にも保存。

## セットアップ（15分）

### 1. リポジトリに配置
このフォルダ一式を新規リポジトリにpush。

### 2. Secrets を登録
リポジトリの Settings → Secrets and variables → Actions → New repository secret

| Secret | 中身 |
|---|---|
| `ANTHROPIC_API_KEY` | Claude APIキー |
| `SMTP_HOST` | 例: `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | 送信元Gmailアドレス |
| `SMTP_PASS` | Gmailの**アプリパスワード**（後述） |
| `MAIL_TO` | 宛先（カンマ区切りで複数可） |
| `MAIL_FROM` | 送信元表示（省略時は`SMTP_USER`） |

> **Gmailアプリパスワード**: Googleアカウント→セキュリティ→2段階認証を有効化→「アプリパスワード」を発行し、その16桁を`SMTP_PASS`に入れる。通常のログインパスワードは使えません。
> SMTPを設定しなければ、送信はスキップされHTML保存だけ行われます（まず動作確認したい時に便利）。

### 3. まず手動実行でテスト
Actions タブ → morning-brief → Run workflow。
`output/latest.html` が生成され、メールが届けば成功。

### 4. あとは放置
平日 **09:30 JST** に自動実行（`.github/workflows/brief.yml` の cron で変更可）。

## カスタマイズは `config/sources.yaml` だけ
- 収集ワードの追加/削除（`news`の`query`）
- 言語の切替（`hl`/`gl`/`ceid`）
- スナップショットの指数追加（`indices`にStooqシンボルを足す）
- 記事本数・遡り時間の調整

コードは触らずに運用できます。

## 既知の割り切り（設計判断）
- **指数の確定値**（VN-Index・日経の終値数値）はニュース要約側でカバーする方針。
  厳密な数値が要る場合は、`fetch_fx`と同様の関数を足してStooq/LSEG等を繋いでください。
  最初から数値APIを盛ると壊れやすいので、まず型を固めるのを優先しています。
- ソースは英3＋越2＋案件、から開始。効くものだけ残す“候補カード選定”方式が結局いちばん早い。

## モデル
`config/sources.yaml` の `anthropic_model` を、利用可能な最新モデル文字列に。
