# 🩺 医学論文自動収集・AI要約システム

毎朝7時、自分の専門領域の最新論文トップ5が**AI要約付きでメールに届く**仕組みです。

- **完全自動** — PCの起動不要、サーバー不要
- **費用ほぼゼロ** — GitHub Actions無料枠 + Gemini API無料枠で動作
- **どの専門科でも使える** — 専門領域を入力するだけでAIが自動設定
- **プログラミング不要** — ボタンとコピペだけで導入できます

---

## 📬 届くレポートのイメージ
<img width="372" height="368" alt="Image" src="https://github.com/user-attachments/assets/e6b1d141-6301-4238-a25b-8fc9e3a4eeea" />

毎朝7時にGmailに届くWordファイルの中身：

- **論文情報**（タイトル・雑誌名・DOIリンク）
- **論文タイプ別の要約**（研究報告・システマティックレビュー・ガイドラインで形式が異なる）
- **臨床的ポイント**（その論文が日常診療にどう活きるか）
- **日本の実臨床への実践メモ**

---

## ⚙️ 仕組み

```
毎朝UTC 22:00（日本時間 翌朝7:00）に自動起動
        ↓
PubMedから過去7日分の論文を最大200本収集
        ↓
スコアリング（論文タイプ・ジャーナルランク・臨床関連性）で上位5本を選出
        ↓
Gemini AIが論文タイプを判定して適した形式で日本語要約
        ↓
Word形式でレポートを生成してGmailに送信
```

---

## 🚀 セットアップ手順

### STEP 1 — 必要なアカウントを作る（すべて無料）

| サービス | 用途 | 登録先 |
|----------|------|--------|
| GitHub | コードの保存・自動実行 | [github.com](https://github.com) |
| Google AI Studio | Gemini APIキーの発行 | [aistudio.google.com](https://aistudio.google.com) |
| NCBI | PubMed APIキーの発行 | [ncbi.nlm.nih.gov/account](https://www.ncbi.nlm.nih.gov/account/) |
| Gmail | レポートの送受信 | [gmail.com](https://gmail.com) |

---

### STEP 2 — このリポジトリを自分のアカウントにコピーする

1. このページ右上の **「Use this template」** ボタンをクリック
2. **「Create a new repository」** を選択
3. リポジトリ名を入力（例: `medical-paper-bot`）して作成

> ⚠️ **「Private」で作成することを推奨します**（APIキーの管理のため）

---

### STEP 3 — APIキー等をSecretsに登録する

自分のリポジトリページで **Settings → Secrets and variables → Actions → New repository secret** を開き、以下の6つを登録します。

#### ① GEMINI_API_KEY
1. [Google AI Studio](https://aistudio.google.com) を開く
2. 「Get API key」→「Create API key」でキーを発行
3. 表示された文字列をコピーして登録

#### ② NCBI_EMAIL
自分のメールアドレスをそのまま入力（例: `yourname@gmail.com`）

#### ③ NCBI_API_KEY
1. [NCBI](https://www.ncbi.nlm.nih.gov/account/) でアカウント作成・ログイン
2. 右上のアカウント名 → **API Key Management** でキーを発行して登録

#### ④ GMAIL_USERNAME
送信に使うGmailアドレスを入力（例: `yourname@gmail.com`）

#### ⑤ GMAIL_APP_PASSWORD
通常のGmailパスワードとは別の「アプリパスワード」が必要です。
1. [Googleアカウントのセキュリティ設定](https://myaccount.google.com/security) を開く
2. 2段階認証を有効にする（まだの場合）
3. 「アプリパスワード」→ 名前を入力（例: `論文システム`）→「作成」
4. 表示された **16桁のパスワード** をコピーして登録
   > ⚠️ この画面を閉じると二度と確認できません。必ずコピーしてください

#### ⑥ GMAIL_RECIPIENT
レポートを受け取りたいメールアドレスを入力（自分のアドレスでOK）

---

### STEP 4 — 専門領域をAIに自動設定させる

1. 自分のリポジトリで **「Actions」** タブを開く
2. 左メニューから **「🔧 Setup - 専門領域を設定する」** をクリック
3. 右側の **「Run workflow」** ボタンをクリック
4. 以下を入力して「Run workflow」を押す

| 入力項目 | 内容 |
|---------|------|
| 専門領域 | 日本語で入力（例: `消化器内科`、`神経内科`、`腫瘍内科`） |
| 曜日ごとのテーマ | 任意（例: `月曜=大腸癌, 火曜=肝臓`）。空白でAIが自動決定 |
| 基礎研究を含めるか | 臨床家は「いいえ」、研究者は「はい」 |

1〜2分で設定が完了します。これ以降は毎朝7時に自動で動き始めます。

---

### STEP 5 — 動作確認（任意）

セットアップ後すぐに動作確認したい場合：

1. **「Actions」** タブを開く
2. **「Daily Paper Summary and Email」** をクリック
3. **「Run workflow」** → **「Run workflow」** で手動実行

数分後にGmailにレポートが届いていれば成功です。

---

## 🔧 設定のカスタマイズ

`config.yaml` を直接編集することで細かい設定ができます。

```yaml
search:
  days_back: 7      # 何日前までの論文を検索するか
  top_n: 10         # 最大何本選出するか

journals:
  tier1:            # 最優先ジャーナル
    - "N Engl J Med"
    - "JAMA"
```

Setupを再実行すれば `config.yaml` は上書きされます。

---

## 💰 費用の目安

| サービス | 費用 |
|---------|------|
| GitHub Actions | 無料（月2,000分の無料枠内で収まる） |
| Gemini API | 無料枠内（月数百円以下） |
| NCBI / PubMed | 完全無料 |
| **合計** | **ほぼ無料** |

---

## ❓ よくある質問

**Q: 循環器内科以外でも使えますか？**
A: はい。STEP 4のSetupで専門領域を入力すれば、どの科でも自動設定されます。

**Q: 英語論文のまま届くのですか？**
A: AIが日本語で要約します。タイトルや著者情報は英語のままですが、内容の要約はすべて日本語です。

**Q: 論文が届かない日はありますか？**
A: 過去7日間に新しい論文がなかった場合はメールが送信されません（エラーではありません）。

**Q: 同じ論文が何度も届きませんか？**
A: 一度届いた論文はリポジトリ内の `history.json` に記録され、翌日以降は除外されます。

---

## 📄 ライセンス

MIT License

---

作者: [@yush02084](https://github.com/yush02084)
README_draft.md
