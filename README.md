これをベースに現状に合わせて全部更新するね。

---

# まめBot 🫘

家族LINEグループと連携して、日常の家族間コミュニケーションを楽にするLINE Bot。

[![Python](https://img.shields.io/badge/Python-3.13-blue)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-latest-green)](https://flask.palletsprojects.com/)
[![Railway](https://img.shields.io/badge/Deploy-Railway-purple)](https://railway.app/)

## 開発背景

家族間の日常連絡（ご飯の時間・お風呂・帰宅時間など）を楽にしたいという課題から開発。「家族LINEを荒らさない」をコンセプトに、個別チャットで入力→家族グループにまとめて通知する設計にした。

もともと「Irori」という家族向けアプリを作っていたが、わざわざアプリを開かないというフィードバックを受けてLINEボットに移植。現在家族4人で運用中。

📝 [開発記事（Zenn）](https://zenn.dev/anit/articles/e77552da162619)

## 機能一覧

### 🍚 ごはん管理
- 今日の夕食予定（家で食べる・外で食べる・未定）を家族グループに共有
- 「ごはんできました！」ボタンで家族グループに一斉送信

### 🚃 出発・帰宅管理
- 出発・帰宅時間を家族グループに共有
- 出発のみ・帰宅のみ・両方の選択式
- 今日の状況確認・全員への入力依頼機能

### 🛁 お風呂管理
- 「洗った・洗って入れた・お願いする」の選択式
- 設定した時間までにお風呂が洗われていなければ自動通知

### 🗑️ ゴミの日管理
- ゴミ収集スケジュールを登録
- 毎週・第1〜3週・第2〜4週の設定に対応
- 前日21時と当日朝7時に家族グループへ自動通知

## 技術スタック

| 項目 | 内容 |
|------|------|
| 言語 | Python 3.13 |
| フレームワーク | Flask |
| データベース | PostgreSQL |
| ホスティング | Railway |
| API | LINE Messaging API |

## 設計の工夫

- **グループ別データ棲み分け**: 複数グループが同じBotを使っても、group_idでデータを完全分離
- **push/reply messageの使い分け**: ユーザーへの返信はreply message（無制限）、グループへの通知はpush messageを使用
- **登録コード方式**: グループ招待時に6桁コードを発行し、個別チャットで入力することでグループと紐付け

## 開発方法

コードはAIをフル活用して生成。要件定義・仕様決定・UIと動線の設計・バグ発見と改善判断・インフラ選定は自分で行った。

## セットアップ

```bash
pip install -r requirements.txt
```

環境変数を設定する。

```
LINE_CHANNEL_ACCESS_TOKEN=xxx
LINE_CHANNEL_SECRET=xxx
DATABASE_URL=xxx
```

```bash
gunicorn main:app --bind 0.0.0.0:8080
```

## 開発者

**AnIT** - 立命館大学 情報理工学部 3回生

---

これをそのままGitHubのREADME.mdに貼り付けてpushして！
