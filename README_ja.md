# Weather Briefing

[English](README.md) | [简体中文](README_zh-Hans.md) | [日本語](README_ja.md)

Weather Briefing は、天気・大気質・警報などの情報源を定期的に集約し、大規模言語モデルを使って出典付きのメッセージを配信します。

## 主な機能

- 天気、大気質、気象警報、生活アドバイスを毎日配信します。
- 天気の変化を継続的に監視し、利用者の行動が必要なときだけ通知します。
- 履歴・未送信メッセージ・有効な警報を永続化し、重複や見落としを防ぎます。
- 複数の地点・複数の出力言語に対応し、地点ごとに状態は独立しています。
- 地域に応じて広域気象サービスと地域固有のサービスを組み合わせ、主情報源が使えないときは自動的に切り替えます。
- プライベートな RSS やウェブコンテンツを追加でき、すべての結論に検証可能な出典リンクが残ります。

## 事前準備

デプロイにあたり、以下を用意してください：

- プログラムを長期稼働させ、実行状態を保持できる環境。
- 配信プラットフォームのアカウントと認証情報。現在は Telegram に対応しています。Bot Token と Chat ID が必要です。
- 対応する大規模言語モデルのアカウント、モデル名、認証情報。[any-llm プロバイダ一覧](https://docs.mozilla.ai/any-llm/providers)を参照してください。
- 最低 1 つの対象地点。
- 実行状態とジオコーディング結果を永続化できるディレクトリ。

デフォルトの天気サービスは API キー不要です。中国本土で QWeather を利用する場合は、プロジェクト ID、認証情報 ID、専用 API ホスト、Base64 エンコードされた Ed25519 秘密鍵も必要です。認証方式は [QWeather JWT ドキュメント](https://dev.qweather.com/docs/configuration/authentication/#json-web-token)を参照してください。

リポジトリには以下の設定テンプレートが含まれています：

- [`env.example`](env.example) &mdash; 環境変数とその用途。
- [`locations.example.json`](locations.example.json) &mdash; 対象地点。
- [`rss-sources.example.json`](rss-sources.example.json) &mdash; オプションの RSS ソース。

## 公開イメージを使う

Docker はデプロイ方法の 1 つです。以下の例では Docker Hub の固定バージョンイメージを使います。プログラムを常駐起動し、上記の設定と状態を保持できるなら、他の方法で実行しても構いません。

まず、作業ディレクトリを作成し、設定テンプレートをコピーします：

```sh
export ROOT_DIR="${HOME}/weather-briefing"
mkdir -p "${ROOT_DIR}/state"
cp env.example "${ROOT_DIR}/.env"
cp locations.example.json "${ROOT_DIR}/locations.json"
```

`.env` と `locations.json` を編集します。設定が完了したらファイルの権限を絞り、サービスを起動します。以下のコマンドでは GID `65532` を書き込み可能な信頼済みコンテナサービスグループとして扱うため、無関係なホストユーザーをこのグループに追加しないでください。

```sh
sudo chgrp -R 65532 "${ROOT_DIR}"
find "${ROOT_DIR}" -type d -exec chmod 770 {} +
find "${ROOT_DIR}" -type f -exec chmod 660 {} +
WEATHER_BRIEFING_VERSION="2.2.0"
IMAGE="icecodexi/weather-briefing:${WEATHER_BRIEFING_VERSION}"
TZ="$(sed -n 's/^BRIEFING_TIMEZONE=//p' "${ROOT_DIR}/.env" | tail -n 1 | tr -d '\r')"

docker pull "${IMAGE}"
docker run -d \
  --name weather-briefing \
  --restart unless-stopped \
  --env "TZ=${TZ:-Asia/Shanghai}" \
  --env-file "${ROOT_DIR}/.env" \
  --mount \
  "type=bind,src=${ROOT_DIR}/locations.json,dst=/home/nonroot/app/locations.json,readonly" \
  --mount \
  "type=bind,src=${ROOT_DIR}/state,dst=/home/nonroot/app/state" \
  "${IMAGE}" daemon
```

アップグレードするときは、イメージのバージョンを変更し、古いコンテナを削除して、同じ設定で再作成してください。

## 地点の設定

各地点には固有の `id` が必要です。`id` は地点を識別するために使われるため、設定後に安易に変更しないでください。また、各地点には地点名、緯度と経度のペア、またはその両方を指定する必要があります：

- `name`：地点の名称。
- 緯度 `latitude` と経度 `longitude` のペア。

地点名のみ指定した場合は、プログラムが座標を検索してキャッシュします。座標のみ指定した場合は、プログラムが地点名を逆引きします。両方指定した場合は、ジオコーディング呼び出しは行われません。

`language` はその地点の言語を指定します。基本的な BCP 47 形式のタグを指定でき、デフォルトは `en` です。タグは正規化され（`ja-jp` は `ja-JP` になります）、言語モデルに渡されます。ブリーフィングのラベルは `en`、`ja`、`zh-CN`、`zh-TW` にローカライズされています。派生タグには最も近いローカライズが使われ、未対応の主要言語では英語のラベルにフォールバックします。日本の地点で JMA 予報を利用する場合は、さらに 6 桁の `jma_office_code` を設定してください。

プログラムは地域に応じてデフォルトの天気ソースを選択します：

- 中国本土：QWeather を優先し、Open-Meteo をバックアップとして使用。
- シンガポール：Open-Meteo の広域天気を取得し、NEA の 2 時間予報を追加。
- 日本：Open-Meteo の広域天気を取得し、`jma_office_code` が設定されている場合は JMA 予報を追加。
- その他の地域：Open-Meteo。

`WEATHER_PROVIDERS` で地域ごとのデフォルト取得順序を置き換えることもできます。地域の補足サービスを引き続き利用する場合は、`nea-sg` または `jma-jp` を明示的に含めてください。広域天気サービスは、限定的な情報しか提供しないサービスの前に指定してください。この順序はデータ取得方法のみに影響します。

NEA や JMA の内容が同じ時刻・地域の Open-Meteo と矛盾する場合、現地の公的機関の最新情報を優先し、競合する情報源はユーザーが検証できるよう残します。

### JMA オフィスコード

日本の 47 都道府県をカバーする予報区域のオフィスコードと使い方は [`docs/jma-office-codes.md`](docs/jma-office-codes.md) を参照してください。

## モデルと配信の設定

`.env` に最低限以下を記入してください：

- `LLM_PROVIDER` と `LLM_MODEL`。
- 選択したモデルサービスに必要な認証情報。
- Telegram で配信する場合は `TELEGRAM_BOT_TOKEN` と `TELEGRAM_CHAT_ID`。`PUBLISHER=stdout` でテストする場合は不要です。

Telegram での配信を初めて利用する際は、ボットに `/start` を送信してください。ボットがメッセージを送れるのは、ユーザーが先に会話を開始した Chat ID に限られます。

モデル呼び出しは any-llm によって処理されます。各サービスに必要な認証情報の変数は [any-llm プロバイダドキュメント](https://docs.mozilla.ai/any-llm/providers)を参照してください。公式イメージには DeepSeek、OpenAI、OpenRouter に必要なコンポーネントが同梱されています。

RSS はオプションです。利用する場合は、ソース名・URL・対象地点をプライベート設定に記入してください。

上記の Docker 例を使う場合は、まずファイルを準備します：

```sh
cp rss-sources.example.json "${ROOT_DIR}/rss-sources.json"
sudo chgrp 65532 "${ROOT_DIR}/rss-sources.json"
chmod 660 "${ROOT_DIR}/rss-sources.json"
```

次に、以下のオプションを `docker run` コマンドに追加し、`"${IMAGE}" daemon` より前に配置してください：

```sh
--mount \
  "type=bind,src=${ROOT_DIR}/rss-sources.json,dst=/home/nonroot/app/rss-sources.json,readonly"
```

## 実行とトラブルシューティング

常駐スケジューラはデフォルトで毎日 08:00 に天気予報を送信し、09:00&ndash;23:00 の間、天気の変化をチェックします。タイムゾーンとスケジュールは `.env` で調整できます。

デフォルトのタイムゾーンは `Asia/Shanghai` です。日本で使う場合は `BRIEFING_TIMEZONE` を `Asia/Tokyo` に変更してください。上記の起動コマンドが `.env` から値を読み取り、同じ値を `TZ` としてコンテナに渡します。

単発のタスクを手動実行するには：

```sh
# 指定日の天気予報を確認
docker exec weather-briefing \
  /home/nonroot/app/.venv/bin/weather-briefing \
  run forecast --date 2026-07-23 --run-now
# その場でブリーフィングを実行
docker exec weather-briefing \
  /home/nonroot/app/.venv/bin/weather-briefing \
  run briefing --run-now
```

`PUBLISHER=stdout` で動作確認できたら、`.env` を `PUBLISHER=telegram` に戻し、`TELEGRAM_BOT_TOKEN` と `TELEGRAM_CHAT_ID` を記入してコンテナを作り直してください。

アプリケーションの動作ログは標準エラー出力に書き込まれます。通常のログには認証情報、座標、本文、プライベート URL は記録されません。

生成されたメッセージの全文を確認したい場合は、`.env` に `DEBUG=true` を設定し、上記の起動コマンドでコンテナを作り直してください。`docker restart` では `--env-file` が再読み込みされないため注意が必要です。

新しいコンテナが起動したら、以下のコマンドで診断を有効にします：

```sh
docker exec weather-briefing \
  /home/nonroot/app/.venv/bin/weather-briefing \
  diagnostics rendered-text enable --for 15m
docker exec weather-briefing \
  /home/nonroot/app/.venv/bin/weather-briefing \
  diagnostics rendered-text disable
```

診断テキストには地点やソースの内容が含まれる可能性があります。トラブルシューティング後は速やかに診断を無効にし、ログを適切に保護してください。

本製品が解決するシナリオについては [`docs/requirements.md`](docs/requirements.md) を、現在の実装については [`docs/design.md`](docs/design.md) を、一見すると分かりにくい技術的判断については [`docs/notes.md`](docs/notes.md) を参照してください。

天気・花粉データは Open-Meteo および CAMS ENSEMBLE に基づく場合があります。地点の検索には OpenStreetMap Nominatim を利用することがあり、データの著作権はその貢献者に帰属します。
