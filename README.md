# rocketnow-cli

ロケットナウの自分のアカウントを操作する非公式CLIです。iOSアプリの通信をもとに、店舗検索、メニュー、注文履歴、会計確認、登録済みカードでの注文手続きを実装しています。出力はJSONです。

検索から会計確認までは実APIで動作を確認しました。**CLIからの注文送信とブラウザでのカード認証は、実購入では未検証です。** カード会社が3-D Secureを要求した場合、本人の操作が必要です。

## 必要環境とインストール

- macOS、Python 3.11以降、iPhoneにインストール済みのロケットナウアプリ
- [`mitmdump` を含むmitmproxy](https://docs.mitmproxy.org/stable/overview/installation/)（macOSでは `brew install --cask mitmproxy`）
- iPhoneからMacのプロキシへ接続できるWi-Fi。iPhoneで `http://mitm.it` を開き、[mitmproxy CA証明書を信頼](https://docs.mitmproxy.org/stable/concepts/certificates/)してください。

アプリ通信を捕捉する場合は別のターミナルで `mitmweb --listen-host 0.0.0.0 --listen-port 8080 --web-host 127.0.0.1 --web-port 8081` を起動し、iPhoneのWi-Fi HTTPプロキシをMacのLANアドレス・8080番に設定します。Web UIの認証トークンはmitmwebのターミナルに表示されます。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
rocketnow --help
```

`auth login` のブラウザ方式を試す場合だけNode.js、Playwright、Chromeが必要です。リポジトリ内で `npm install` を実行し、`ROCKETNOW_PLAYWRIGHT_ROOT` をこのリポジトリの絶対パスに設定します。`ROCKETNOW_BROWSER=webkit` を使う場合は `npx playwright install webkit` も実行します。ログイン初期画面は開きますが、MacのChromeとWebKitではSMS認証画面がSSOから403で拒否されました。現在この方式だけでのログインは完了していません。

## 認証

APIはP-256鍵によるDPoP署名を使います。アクセストークンは公開鍵に結び付くため、アプリのトークンをコピーするだけではCLIから使えません。

`rocketnow auth pair-proxy` はMacの8082番で専用プロキシを起動します。iPhoneのWi-Fi HTTPプロキシを一時的にそのMacへ向け、アプリでログアウト・再ログインすると、CLI用の鍵に結び付いたセッションを保存します。ペアリング直後のアプリは再ログインが必要になる場合があります。完了後はiPhoneのプロキシを**元の設定**へ戻してください。

現在確認済みのCLI認証方法は `auth pair-proxy` です。`auth login` はSSO側の拒否が解消するまで実験的です。

セッションは `~/Library/Application Support/rocketnow-cli/session.json` に0600権限で保存します。トークンと秘密鍵を含むので共有しないでください。アクセストークンの有効期間は約4時間です。アプリ通信では、期限後のAPI応答に新しいトークンが返る例を確認しました。CLIも**利用時のみ**同じ更新を試し、受け取ったトークンが現在のアカウントとDPoP鍵に結び付く場合だけ保存します。常駐処理はありません。`rocketnow auth refresh` で明示的にも試せます。CLI自身が期限後に更新できるかは、次の期限到来時に実通信で確認する必要があります。更新できない場合や長時間CLIを使わなかった場合は `auth pair-proxy` で再認証が必要になる可能性があります。

## 検索と会計確認

```sh
rocketnow auth status
rocketnow address
rocketnow search 寿司 --lat 35.68 --lon 139.76
rocketnow store 12345 --lat 35.68 --lon 139.76
rocketnow dish 12345 67890
rocketnow orders
rocketnow payment-methods
rocketnow cart-quote-draft draft.json
rocketnow checkout-review-draft draft.json
```

緯度・経度は `address` の結果を使います。店舗IDと商品IDは `search` → `store` → `dish` で確認します。下記のIDはドラフトの構造例です。

```json
{
  "storeId": 12345,
  "keyword": "寿司",
  "items": [
    {"dishId": 67890, "quantity": 1, "options": []}
  ]
}
```

購入用ドラフトには、選んだ検索結果の `entity.data.logging.searchId` / `searchJourneyId` を `searchId` / `searchJourneyId` として含めると、注文前の再検索を避けられます。省略した場合は `keyword` で再検索します。保存済みカードが複数ある場合は、`payment-methods` にある対象カードのIDを `payMethodId` に含めます。

## カード注文の追加設定

アプリでカードを登録し、自分の注文通信を一度mitmwebで捕捉する必要があります。捕捉した成功済み `checkout.prepay` から必要な3項目だけを取り込めます。パスワードは端末上で入力し、リポジトリへ保存しません。

```sh
rocketnow config import-payment --mitmweb-url http://127.0.0.1:8081
rocketnow config status
```

手動で設定する場合は、`checkout.prepay` の `payment.merchantMallKey`、`deviceInfo.userAgent`、`payment.payMethodId` を、リポジトリ外の `~/Library/Application Support/rocketnow-cli/payment-config.json` に次のキーで保存します。

```json
{
  "merchantMallKey": "自分の通信から取得した値",
  "deviceUserAgent": "自分の通信から取得した値",
  "preferredPayMethodId": 123
}
```

ファイル権限は `chmod 600` にします。実値を公開リポジトリやチャットへ貼らないでください。カード番号は保存しません。`payment-config.json` がなくても検索・履歴・会計プレビューは利用できますが、`order check` は実行できません。

## 注文の流れ

`rocketnow order check draft.json` は注文に必要な項目を検査し、店舗・商品・請求額・配送先・保存済みカードを表示します。購入はしません。AIはこの内容を利用者に示し、その注文への明示承認を受けます。

`order submit` は承認した `reviewHash` と `requestedAmount` を `--approve-hash` / `--approve-amount` に指定します。承認ハッシュは配送指示、カードIDとマスク番号、商品、請求額を含む注文内容に結び付き、10分以内に一度だけ使えます。**`order submit` は実際の注文を開始します。** 会計が変わった場合や通信結果が不明な場合は自動再送しません。

送信後、`rocketnow order payment-url <pendingId>` はURLを表示せずに決済画面を開きます。3-D Secureが求められたら本人がカード会社の画面で認証します。完了後に `rocketnow order confirm <pendingId> --payment-complete` で決済結果を確認します。決済URLを手で扱う必要がある場合だけ `--reveal` を指定してください。

捕捉したカード注文では3-D Secure OTPが必要でした。決済ページはアプリのCookieや認証ヘッダーを必要とする可能性があり、Macブラウザで開けるかは未検証です。ドラフトはクーポンを自動適用しないため、承認前に請求額を確認してください。

## テスト

```sh
python -m unittest discover -s tests -v
```

検索、店舗、商品詳細、配送先、支払方法、注文履歴、カート計算、会計プレビュー、`order check` は実APIで動作確認済みです。注文送信と決済完了は次の承認済み実注文で検証します。
