# JMA オフィスコード / JMA Office Codes / JMA 办公室编码

気象庁（JMA）は地域ごとに天気予報を公開しており、`jma_office_code` に6桁のオフィスコードを設定することで各地の予報を取得できます。

The Japan Meteorological Agency publishes forecasts by area. Set the six-digit office code as `jma_office_code` to fetch local forecasts.

日本气象厅按区域发布天气预报，将六位办公室编码设为 `jma_office_code` 即可获取当地预报。

## コード一覧 / Code List / 编码列表

| コード | 予報区域 | Forecast area | 预报区域 |
| ------ | -------- | ------------- | -------- |
| `011000` | 宗谷地方 | Soya | 宗谷地方 |
| `012000` | 上川・留萌地方 | Kamikawa Rumoi | 上川・留萌地方 |
| `013000` | 網走・北見・紋別地方 | Abashiri Kitami Mombetsu | 网走・北见・纹别地方 |
| `014030` | 十勝地方 | Tokachi | 十胜地方 |
| `014100` | 釧路・根室地方 | Kushiro Nemuro | 钏路・根室地方 |
| `015000` | 胆振・日高地方 | Iburi Hidaka | 胆振・日高地方 |
| `016000` | 石狩・空知・後志地方 | Ishikari Sorachi Shiribeshi | 石狩・空知・后志地方 |
| `017000` | 渡島・檜山地方 | Oshima Hiyama | 渡岛・桧山地方 |
| `020000` | 青森県 | Aomori | 青森县 |
| `030000` | 岩手県 | Iwate | 岩手县 |
| `040000` | 宮城県 | Miyagi | 宫城县 |
| `050000` | 秋田県 | Akita | 秋田县 |
| `060000` | 山形県 | Yamagata | 山形县 |
| `070000` | 福島県 | Fukushima | 福岛县 |
| `080000` | 茨城県 | Ibaraki | 茨城县 |
| `090000` | 栃木県 | Tochigi | 栃木县 |
| `100000` | 群馬県 | Gunma | 群马县 |
| `110000` | 埼玉県 | Saitama | 埼玉县 |
| `120000` | 千葉県 | Chiba | 千叶县 |
| `130000` | 東京都 | Tokyo | 东京都 |
| `140000` | 神奈川県 | Kanagawa | 神奈川县 |
| `150000` | 新潟県 | Niigata | 新潟县 |
| `160000` | 富山県 | Toyama | 富山县 |
| `170000` | 石川県 | Ishikawa | 石川县 |
| `180000` | 福井県 | Fukui | 福井县 |
| `190000` | 山梨県 | Yamanashi | 山梨县 |
| `200000` | 長野県 | Nagano | 长野县 |
| `210000` | 岐阜県 | Gifu | 岐阜县 |
| `220000` | 静岡県 | Shizuoka | 静冈县 |
| `230000` | 愛知県 | Aichi | 爱知县 |
| `240000` | 三重県 | Mie | 三重县 |
| `250000` | 滋賀県 | Shiga | 滋贺县 |
| `260000` | 京都府 | Kyoto | 京都府 |
| `270000` | 大阪府 | Osaka | 大阪府 |
| `280000` | 兵庫県 | Hyogo | 兵库县 |
| `290000` | 奈良県 | Nara | 奈良县 |
| `300000` | 和歌山県 | Wakayama | 和歌山县 |
| `310000` | 鳥取県 | Tottori | 鸟取县 |
| `320000` | 島根県 | Shimane | 岛根县 |
| `330000` | 岡山県 | Okayama | 冈山县 |
| `340000` | 広島県 | Hiroshima | 广岛县 |
| `350000` | 山口県 | Yamaguchi | 山口县 |
| `360000` | 徳島県 | Tokushima | 德岛县 |
| `370000` | 香川県 | Kagawa | 香川县 |
| `380000` | 愛媛県 | Ehime | 爱媛县 |
| `390000` | 高知県 | Kochi | 高知县 |
| `400000` | 福岡県 | Fukuoka | 福冈县 |
| `410000` | 佐賀県 | Saga | 佐贺县 |
| `420000` | 長崎県 | Nagasaki | 长崎县 |
| `430000` | 熊本県 | Kumamoto | 熊本县 |
| `440000` | 大分県 | Oita | 大分县 |
| `450000` | 宮崎県 | Miyazaki | 宫崎县 |
| `460040` | 奄美地方 | Amami | 奄美地方 |
| `460100` | 鹿児島県（奄美地方除く） | Kagoshima (Excluding Amami) | 鹿儿岛县（奄美地方除外） |
| `471000` | 沖縄本島地方 | Okinawa Main Island | 冲绳本岛地方 |
| `472000` | 大東島地方 | Daitojima | 大东岛地方 |
| `473000` | 宮古島地方 | Miyakojima | 宫古岛地方 |
| `474000` | 八重山地方 | Yaeyama | 八重山地方 |

## 使い方 / Usage / 用法

1. 上記の表から該当する予報区域のコードを探します。／ Find your forecast area's code in the table above. ／ 从上表中找到对应预报区域的编码。
2. `locations.json` の該当地点に `"jma_office_code": "XXXXXX"` を追加します。／ Add `"jma_office_code": "XXXXXX"` to the location in `locations.json`. ／ 在 `locations.json` 的对应地点中添加 `"jma_office_code": "XXXXXX"`。
3. コードの典拠は [`https://www.jma.go.jp/bosai/common/const/area.json`](https://www.jma.go.jp/bosai/common/const/area.json) です。／ The source of these codes is [`https://www.jma.go.jp/bosai/common/const/area.json`](https://www.jma.go.jp/bosai/common/const/area.json). ／ 编码出处为 [`https://www.jma.go.jp/bosai/common/const/area.json`](https://www.jma.go.jp/bosai/common/const/area.json)。
