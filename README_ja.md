# Weather Briefing

[![CI](https://github.com/IceCodeNew/weather-briefing/actions/workflows/ci.yml/badge.svg)](https://github.com/IceCodeNew/weather-briefing/actions/workflows/ci.yml)
[![Unittest](https://github.com/IceCodeNew/weather-briefing/actions/workflows/unittest.yml/badge.svg)](https://github.com/IceCodeNew/weather-briefing/actions/workflows/unittest.yml)
[![codecov](https://codecov.io/gh/IceCodeNew/weather-briefing/branch/master/graph/badge.svg?token=JUmxcPx7js)](https://codecov.io/gh/IceCodeNew/weather-briefing)
![Python Version from PEP 621 TOML](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2FIceCodeNew%2Fweather-briefing%2Frefs%2Fheads%2Fmaster%2Fpyproject.toml)
[![CodeQL](https://github.com/IceCodeNew/weather-briefing/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/IceCodeNew/weather-briefing/actions/workflows/github-code-scanning/codeql)

[English](README.md) | [简体中文](README_zh-Hans.md) | [日本語](README_ja.md)

Weather Briefing は、天気・大気質・警報などの情報源を定期的に集約し、大規模言語モデルを使って出典付きのメッセージを配信します。

## 主な機能

- 天気、大気質、気象警報、生活アドバイスを毎日配信します。
- 設定した監視時間帯に天気の変化を監視し、利用者の行動が必要なときだけ通知します。
- 履歴・未送信メッセージ・有効な警報を永続化し、重複や見落としを防ぎます。
- 複数の地点・複数の出力言語に対応し、地点ごとに状態は独立しています。
- 地域に応じて複数の気象サービスを組み合わせ、障害時は利用可能なサービスに切り替えます。
- プライベートな RSS コンテンツを追加でき、すべての結論に検証可能な出典リンクが残ります。

## 事前準備

デプロイにあたり、以下を用意してください：

- プログラムを長期稼働させ、実行状態を保持できる環境。
- 配信サービスの認証情報。Telegram には Bot Token と Chat ID、Bark にはデバイスキーが必要です。Bark の暗号化キーと IV は任意です。
- 対応する大規模言語モデルのアカウント、モデル名、認証情報。[any-llm プロバイダ一覧](https://docs.mozilla.ai/any-llm/providers)を参照してください。
- 最低1つの対象地点。
- 実行状態とジオコーディング結果を永続化できるディレクトリ。

天気サービスの API キーを設定しなくても、本プロジェクトは利用できます。中国本土で QWeather を利用する場合は、プロジェクト ID、認証情報 ID、専用 API ホスト、Base64 エンコードされた Ed25519 秘密鍵も必要です。認証方式は [QWeather JWT ドキュメント](https://dev.qweather.com/docs/configuration/authentication/#json-web-token)を参照してください。

リポジトリには以下の設定テンプレートが含まれています：

- [`env.example`](env.example) &mdash; 環境変数とその用途。
- [`locations.example.json`](locations.example.json) &mdash; 対象地点。
- [`rss-sources.example.json`](rss-sources.example.json) &mdash; オプションの RSS ソース。

## 公開イメージを使う

デプロイには Docker の利用を推奨します。以下の例では、Docker Hub のイメージをバージョン指定で使用します。POSIX 環境で直接運用する場合も、プログラムを常時稼働させ、設定ファイルやプログラムが作成するデータを保存できる環境を用意してください。Windows ネイティブ環境には対応していません。

まず、ホスト側のディレクトリと設定ファイルを用意します。以下の例では、現在のユーザーのホームディレクトリに保存します。別の場所に保存する場合は `ROOT_DIR` を変更してください。

```sh
CONTAINER_NAME="weather-briefing"
ROOT_DIR="${HOME}/${CONTAINER_NAME}"
CONTAINER_ROOT_DIR="/home/nonroot/app"

mkdir -p "${ROOT_DIR}/state"
touch "${ROOT_DIR}/.env" "${ROOT_DIR}/locations.json"
```

本プロジェクトのテンプレートを参考に `.env` と `locations.json` を編集します。`locations.json` は有効な JSON 配列にする必要があり、空ファイルのままでは起動できません。

設定が完了したらファイルの権限を絞り、サービスを起動します。以下のコマンドは、GID `65532` のグループに設定ファイルと状態ディレクトリへの書き込み権限を与えます。ホスト側で同じ GID のグループを使う場合は、このサービスに関係のないユーザーを所属させないでください。

```sh
sudo chgrp -R 65532 "${ROOT_DIR}"
find "${ROOT_DIR}" -type d -exec chmod 770 {} +
find "${ROOT_DIR}" -type f -exec chmod 660 {} +

WEATHER_BRIEFING_IMAGE="icecodexi/weather-briefing"
WEATHER_BRIEFING_VERSION="2.3.0"
TZ="$(sed -n 's/^BRIEFING_TIMEZONE=//p' "${ROOT_DIR}/.env" | tail -n 1 | tr -d '\n\r')"
docker pull "${WEATHER_BRIEFING_IMAGE}:${WEATHER_BRIEFING_VERSION}"

docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  --env "TZ=${TZ:-Asia/Shanghai}" \
  --env-file "${ROOT_DIR}/.env" \
  --mount \
  "type=bind,src=${ROOT_DIR}/locations.json,dst=${CONTAINER_ROOT_DIR}/locations.json" \
  --mount \
  "type=bind,src=${ROOT_DIR}/state,dst=${CONTAINER_ROOT_DIR}/state" \
  "${WEATHER_BRIEFING_IMAGE}:${WEATHER_BRIEFING_VERSION}" \
  daemon
```

アップグレードするときは `WEATHER_BRIEFING_VERSION` を変更し、イメージの取得と起動コマンドをもう一度実行してください。

## 地点の設定

各地点には、ほかの地点と重複しない `id` を設定してください。`id` を変更すると別の地点として扱われるため、設定後は変更しないでください。また、各地点には地点名、緯度と経度のペア、またはその両方を指定する必要があります：

- `name`：地点の名称。
- 緯度 `latitude` と経度 `longitude` のペア。

地点名だけを指定すると、プログラムが座標を検索し、結果を `locations.json` に保存します。座標だけを指定すると、表示用の地点名を調べて保存します。既存のフィールドは上書きしません。検索条件を緩めて見つかった候補は、確認が必要なためファイルには保存されません。両方を指定した場合、ジオコーディングは実行されません。

地点ごとのブリーフィング言語は `language` で設定します。基本的な BCP 47 形式のタグを指定でき、デフォルトは `en` です。タグを正規化し（`ja-jp` は `ja-JP` になります）、ブリーフィングの生成言語として使います。ブリーフィング内のラベルは `en`、`ja`、`zh-CN`、`zh-TW` に対応しています。地域や文字体系を含むタグも、対応するラベルがあればその言語で表示され、なければ英語になります。日本の地点で JMA 予報を利用する場合は、さらに6桁の `jma_office_code` を設定してください。

利用する気象サービスは地域ごとに次のように決まります：

- 中国本土：QWeather を優先し、Open-Meteo をバックアップとして使用。
- シンガポール：Open-Meteo を基本に、NEA の2時間予報を追加。
- 日本：Open-Meteo を基本に、`jma_office_code` が設定されている場合は JMA の予報を追加。
- その他の地域：Open-Meteo。

`WEATHER_PROVIDERS` では、利用するサービスと優先順位を指定できます。NEA や JMA の予報も使う場合は、`WEATHER_PROVIDERS` に `nea-sg` または `jma-jp` も指定してください。基本の予報を取得するサービスを先に、NEA や JMA のような補足サービスを後に指定してください。サービスの指定順は、呼び出しの優先度だけを決めるもので、情報の信頼性とは関係ありません。

NEA または JMA の予報内容が Open-Meteo と異なる場合は、現地の公的機関が発表した最新情報を優先します。どちらの予報を信頼するか判断できるよう、双方の情報源も示します。

### JMA オフィスコード

日本の47都道府県をカバーする予報区域のオフィスコードと使い方は [`docs/jma-office-codes.md`](docs/jma-office-codes.md) を参照してください。

## モデルと配信の設定

`.env` に最低限以下を記入してください：

- `LLM_PROVIDER` と `LLM_MODEL`。
- 選択したモデルサービスに必要な認証情報。
- Telegram で配信する場合は `PUBLISHER=telegram`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`。または
- Bark で配信する場合は `PUBLISHER=bark` と `BARK_DEVICE_KEY`。暗号化を有効にする場合は `BARK_ENCRYPTION_KEY` と `BARK_ENCRYPTION_IV` の両方。

Telegram のプライベートチャットに配信する場合は、初回配信前にボットへ `/start` を送信してください。ボットがメッセージを送れるのは、ユーザーが先に会話を開始したプライベートチャットの Chat ID に限られます。グループに配信する場合、この操作は不要ですが、ボットをグループに追加し、メッセージの送信権限を付与してください。

Bark は、暗号化キーと IV を設定しなければ平文で送信します。暗号化を推奨します。

暗号化する場合は、`BARK_ENCRYPTION_KEY` と `BARK_ENCRYPTION_IV` を両方設定します。値の生成方法と Bark App の設定については、[`env.example`](env.example) に記載した公式ドキュメントを参照してください。

セルフホストした Bark サーバーを使用する場合は、そのルート URL を `BARK_BASE_URL` に設定します。デフォルトは `https://api.day.app` です。Bark ブリーフィングは、APNs の payload 上限に収まるよう、650文字までに制限します。

モデル呼び出しは any-llm によって処理されます。各サービスに必要な認証情報の変数は [any-llm プロバイダドキュメント](https://docs.mozilla.ai/any-llm/providers)を参照してください。公式イメージには DeepSeek、OpenAI、OpenRouter に必要なコンポーネントが同梱されています。

RSS はオプションで、デフォルトではマウントされません。有効にする場合は、[`rss-sources.example.json`](rss-sources.example.json) を参考に `rss-sources.json` を作成し、ソース名・URL・対象地点を記入します。そのうえで、以下のオプションを `docker run` コマンドのイメージ名より前に追加してください：

```sh
--mount \
  "type=bind,src=${ROOT_DIR}/rss-sources.json,dst=${CONTAINER_ROOT_DIR}/rss-sources.json,readonly"
```

追加後にコンテナを再作成してください。

## 実行とトラブルシューティング

常駐スケジューラはデフォルトで毎日 08:00 に天気予報を送信し、09:00&ndash;23:00 は定期的に最新の情報を確認し、必要に応じて通知します。タイムゾーンとスケジュールは `.env` で調整できます。

デフォルトのタイムゾーンは `Asia/Shanghai` です。日本で使う場合は `BRIEFING_TIMEZONE` を `Asia/Tokyo` に変更してください。上記の起動コマンドが `.env` から値を読み取り、同じ値を `TZ` としてコンテナに渡します。

新しいシェルを開くたびに、`CONTAINER_NAME` に実際のコンテナ名を設定します。別の名前で起動した場合は、以下の値を変更してください。

```sh
CONTAINER_NAME="weather-briefing"
```

`FORECAST_DATE` には、ブリーフィングのタイムゾーンで未来の日付を指定します。

```sh
: "${CONTAINER_NAME:?Set CONTAINER_NAME to the deployed container name}"
FORECAST_DATE="YYYY-MM-DD"

# 指定日の天気予報を確認
docker exec "${CONTAINER_NAME}" \
  /home/nonroot/app/.venv/bin/weather-briefing \
  run forecast --date "${FORECAST_DATE}" --run-now
# その場でブリーフィングを実行
docker exec "${CONTAINER_NAME}" \
  /home/nonroot/app/.venv/bin/weather-briefing \
  run briefing --run-now
```

`PUBLISHER=stdout` で動作確認できたら、`.env` で `PUBLISHER=telegram` または `PUBLISHER=bark` を選び、対応する認証情報を記入してコンテナを作り直してください。

アプリケーションの動作ログは標準エラー出力に書き込まれます。通常のログには認証情報、座標、本文、プライベート URL は記録されません。

生成されたメッセージの全文を確認したい場合は、`.env` に `DEBUG=true` を設定し、上記の起動コマンドでコンテナを作り直してください。`docker restart` だけでは `--env-file` の変更は反映されません。

新しいコンテナが起動したら、以下のコマンドで診断を有効にします：

```sh
: "${CONTAINER_NAME:?Set CONTAINER_NAME to the deployed container name}"

docker exec "${CONTAINER_NAME}" \
  /home/nonroot/app/.venv/bin/weather-briefing \
  diagnostics rendered-text enable --for 15m
```

問題を再現し、必要なログを取得した後で診断を無効にします。

```sh
: "${CONTAINER_NAME:?Set CONTAINER_NAME to the deployed container name}"

docker exec "${CONTAINER_NAME}" \
  /home/nonroot/app/.venv/bin/weather-briefing \
  diagnostics rendered-text disable
```

診断テキストには地点やソースの内容が含まれる可能性があります。トラブルシューティング後は速やかに診断を無効にし、ログを適切に保護してください。

本製品が想定する利用場面と要件については [`docs/requirements.md`](docs/requirements.md) を、現在の実装については [`docs/design.md`](docs/design.md) を、一見すると分かりにくい技術的判断については [`docs/notes.md`](docs/notes.md) を参照してください。

天気・花粉データは Open-Meteo および CAMS ENSEMBLE に基づく場合があります。地点の検索には OpenStreetMap Nominatim を利用することがあり、データの著作権はその貢献者に帰属します。
