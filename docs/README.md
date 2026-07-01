# モンスター探しゲーム — 中央サーバ

ゲーム実行PC上で動く中央サーバ。プレイヤーの**位置推定（RSSI）**、**モーション入力（joycon）**、
**ターン制のゲーム進行**、そして**魔法発動時のraspiへの通知**を、1つのプロセスにまとめる。

`slide.md` のシステム全体像のうち「ゲーム実行PC」が本リポジトリにあたる。

---

## ゲーム概要

- プレイヤーは空間のどこかにいる「ゴースト」を探す。ゴーストの位置は画面に出ない。
- ゴーストはターンごとに移動し続ける。
- プレイヤーは動き回ってゴーストに近づき、モーション（joycon）で魔法を放って捕まえる。
- 中央サーバはプレイヤーの位置をRSSIから推定し、ゴーストとの距離などをraspi（SenseHat）の光に反映する。

---

## 設計の核

「常時流れ込む入力（RSSI・モーション）」と「離散的なターン進行」を安全に同居させるため、
次の3原則で構成している。

- **single writer** — ゲーム状態 `GameState` を書き換えるのは `GameEngine` だけ。
  RSSI受信もモーション入力も「バッファ／キューに積むだけ」なので、状態にロックが要らない。
- **producer / consumer** — 入力は常駐プロデューサ、エンジンが唯一のコンシューマ。
  `RSSIBuffer` と `asyncio.Queue` だけで疎結合になっている。
- **asyncio** — 複数のI/O（UDP・Bluetooth）とターンのタイマーを単一スレッドでさばく。
  ブロッキングなデバイス読み取り（joycon）だけ `run_in_executor` で別スレッドに逃がし、
  `call_soon_threadsafe` でキューへ橋渡しする。

### データフロー

BLEスキャンは**中継raspi（ユーザが携帯）だけ**が行う。raspi が各スマホビーコンの
**生RSSIを測定して UDP で送信**し、ゲームPCはそれを `RSSIBuffer` に積む。
**ゲームPCはBLEを自分でスキャンしない**（自分の位置でのRSSIを参照しない）。
位置推定（kNN）は PC 側で、raspi が測ったRSSIだけを使って行う。

```
  スマホ(BLE) ─BLE→ 中継raspi(スキャン・計測) ─UDP(生RSSI)→ rssi_receiver ──→ RSSIBuffer ┐
                        ▲  (方法A: 同一端末可)                                          │ (snapshot→kNN位置推定)
  joycon ─BT→ motion_input ──→ asyncio.Queue ─────────────────────────────────────────┤
                        │                                                              ▼
                        │                                                          GameEngine  ← 唯一のwriter
                        │                                                              │ (魔法発動時)
                        └────────────── UDP(magic, 9100) ←── raspi_notifier ←──────────┘
```

> **方法A**: 上図の「中継raspi」と「ghost反応raspi（登録raspi）」は同一端末にできる。1台のPiが
> 生RSSIの送信（PC:9000宛）と通知の受信（自身:9101）を兼ねる。別ポート・別方向のため同居でき、
> ゲームPCは唯一の writer のまま（位置推定・ゲーム判定を一元管理）。

---

## ターンのライフサイクル

```
TURN_START : raspiへリセット通知(LED消灯) + ゴースト移動 + RSSIバッファのsnapshotからユーザ位置を推定
             → 推定セルを標準出力にグリッド表示（ACTIVE前の確認用）
ACTIVE     : Aボタン(end_turn)が来るまでモーションを受付、届くたびに魔法を即発動
             （全ゴーストを撃破したらAボタンを待たず即クリア判定へ）
RESOLVE    : 勝敗判定（全ゴースト撃破=クリア / ターン数到達=ゲームオーバー）
GAME_OVER  : ループ終了。raspiへ終了結果を通知（クリア=虹色点滅 / オーバー=GAME OVER表示）
```

ACTIVE には時間制限がない。ターンは **Joy-ConのAボタン（`end_turn`）**、または
**全ゴースト撃破**で次へ進む（撃破時はAボタンを待たずその場でクリア）。

ゲーム開始時（`run()` の最初）に、**RSSIバッファが十分たまるまで**待ってから turn1 を始める。
具体的には「時間窓内のサンプルが `RSSI_WARMUP_MIN_SAMPLES` 件以上あるビーコン」が `KNN_MIN_VALID` 個
そろうまで、最大 `RSSI_WARMUP_SEC` 秒待つ。これで turn1 の位置推定（kNN）が安定して成立する。
時間内にそろわなければそのまま開始する（turn1 は `player=None` になり得る）。

`TURN_START` の推定後、`GameEngine._print_grid()` が現在のグリッド（既定 3×3）に
推定ユーザセル `[U]`・ゴーストセル `[G]`（両者が重なると `[*]`）とゴーストの体力 `hp=`、
続くステータス行に **現在ターン/最大ターン・残りターン数・魔法(ATTACK/SCAN)の残り回数** を
標準出力へ描画する（デバッグ・動作確認用。ゴースト位置はデバッグ表示）。

**魔法と体力**: ゴーストは体力 `hp`（既定 `GHOST_HP=1`）を持つ。**ATTACK** は「プレイヤーの推定セル＝
ゴーストのセル」のときだけゴーストの `hp` を 1 減らす。**SCAN** はゴーストまでの距離を測る。
ATTACK / SCAN はそれぞれ `ATTACK_LIMIT` / `SCAN_LIMIT`（各既定 10）回まで使え、上限に達すると不発になる。

**勝敗**:
- **クリア（勝利）**: 全ゴーストを撃破（`hp<=0`）したら `GAME CLEAR` で終了。既定 `hp=1` なら
  「ゴーストと同じマスで ATTACK を1回当てる」と撃破できる。
- **ゲームオーバー（敗北）**: 撃破できないまま `MAX_TURNS`（既定 10）ターンに達したら `GAME OVER` で終了。
- **終了時の raspi 演出**: 終了時に `raspi_notifier.notify_result` が `{"type":"result","result": ...}` を
  raspi へ送り、`ghost_light_raspi.py` が **クリア=虹色点滅 / ゲームオーバー="GAME OVER"文字表示** を行う。

入力プロセスは常に動いている。エンジンは **ACTIVE フェーズの間だけ**それを魔法として作用させる
（＝「常時受付・ターン中のみ発動」）。「魔法発動 → raspi通知」はエンジンの `_cast_magic` 内に
統合してあり、別プロセスは持たない。

---

## ディレクトリ構成

役割ごとに4コンポーネント（`docs` / `data` / `server` / `raspi`）へ分割している。

```
game/
├── docs/                # ドキュメント
│   ├── README.md
│   ├── ARCHITECTURE.md
│   └── slide.md         # システム全体像のスライド
├── data/                # データ
│   └── phone_fingerprint_0624_10s_3.csv  # 収集済みfingerprint (config.FINGERPRINT_CSV)
├── server/              # ゲーム実行サーバ (中央サーバ/ゲームPC)
│   ├── domain.py        # 状態・エンティティ (GridPos / Ghost / Player / GameState, enums)
│   ├── engine.py        # ターンループ・魔法解決・勝敗判定（心臓部）
│   ├── position.py      # RSSIバッファ(実装済) + 位置推定(フィンガープリントkNN, 実装済)
│   ├── ble_rssi.py      # PC側kNN推定コア(fingerprint読込・kNN)。BLEスキャンはしない
│   ├── rssi_receiver.py # 中継raspiが測った生RSSIのUDP受信 → バッファ
│   ├── motion_input.py  # モーション入力 (右Joy-Con実機)
│   ├── raspi_notifier.py# 魔法発動のraspi通知 (UDP)
│   ├── config.py        # ポート・raspi宛先・グリッド・kNN設定・データパスなど
│   └── main.py          # アダプタの組み立てと起動 (エントリポイント)
├── raspi/               # RSSI送受信・SenseHat発光を担うraspi側スクリプト
│   ├── collect_phone_uuid_fingerprint.py  # [raspi単独ツール] BLEでRSSIを収集しfingerprint CSVを作る
│   ├── relay_rssi_sender.py       # [中継raspi] BLEスキャン → 生RSSIをPCへUDP送信
│   ├── ghost_sensehat.py          # [raspi] ゴースト距離→SenseHat発光の表示ロジック(関数のみ)
│   └── ghost_light_raspi.py       # [ghost反応raspi] UDPでdistance/終了結果を受信 → 発光・点滅・GAME OVER表示(単独実行)
├── conftest.py          # テスト用の import path 設定 (server/・raspi/ を追加)
└── tests/               # 単体テスト・統合テスト + 参考スクリプト(テスト対象外)
    ├── test_domain.py
    ├── test_position.py
    ├── test_motion_input.py
    ├── test_raspi_notifier.py
    ├── test_engine.py
    ├── test_integration.py
    ├── test_ghost_sensehat.py  # ghost_sensehat の発光ロジック単体テスト
    ├── joycon.py        # [参考] 元のjoycon操作スクリプト (motion_input の実装元。テストではない)
    └── realtime_phone_uuid_grid_estimator_ghost.py  # [参考・ゲーム未使用] raspi単独のkNN推定+SenseHat表示(ghost_sensehatの切り出し元)
```

> テストは `conftest.py` が `server/` と `raspi/` を `sys.path` に追加するため、
> 各モジュールは従来どおり `from domain import ...` / `import ghost_sensehat` のように flat に import できる。

`★` = スケルトン。入出力の型（インターフェース）だけ固定済みなので、各自が独立して中身を実装できる。

`ble_rssi.py` は単独ツール 2 本（`collect_*` / `realtime_*`）のうち**fingerprint読込・kNN推定**部分だけを
**原型のまま**取り込み、PC側の `PositionEstimator` に接続したもの。BLEスキャン自体は raspi 側で行うため、
PC側の `ble_rssi.py` にはスキャン処理（`bleak`依存）を持たせない。2 本の単独ツールは
raspi 上でのフィンガープリント収集・スタンドアロン検証用にそのまま残してある。

### 担当

| 担当 | ファイル |
|---|---|
| 担当A | `domain.py` / `engine.py` / `main.py`（ゲーム本体） |
| 担当B | `position.py`（位置推定）/ `raspi_notifier.py`（アプリ・他raspiとの通信） |
| 担当C | `motion_input.py`（モーション検知） |
| 担当D | `rssi_receiver.py`（RSSI送受信） |

---

## 動作環境

- Python 3.9 以降（3.12 で動作確認）
- **ゲームPC側は標準ライブラリのみで動作**（位置推定kNNを含め追加インストール不要）。
  `bleak` はゲームPCには不要（BLEスキャンは raspi 側で行うため）。
- テスト実行に `pytest` が必要
- raspi側でBLEスキャン（`collect_phone_uuid_fingerprint.py` / `relay_rssi_sender.py`）を動かす場合は `bleak` が必要
- raspiでSenseHat発光（`ghost_light_raspi.py`）を使う場合は `sense-hat` が必要（未導入ならログのみで動作）
- joycon実機を使う場合は `joycon-python`（`pyjoycon`）＋ `hidapi` が必要（`uv pip install joycon-python hidapi`）。
  未導入/未接続なら `JoyconMotionSource` は実行時に `RuntimeError` を送出する（モーション入力は実機必須）

---

## セットアップ & 実行

```bash
# リポジトリ直下で実行（server/ がサーバのエントリポイント）
python3 server/main.py
```

モーション入力は右Joy-Con実機（`JoyconMotionSource`）で行う。`server/main.py` を起動すると
右Joy-Conに接続し、`pyjoycon` 未導入／実機未接続なら実行時に `RuntimeError` を送出する。

**joycon操作（右Joy-Con）:**

- **R または ZR を押しながら振る → 魔法発動**。上下（Z方向）で `ATTACK`、左右（Y方向）で `SCAN`。
  1回の押下につき1回だけ検知（離すまで再検知しない）。
- **A ボタン → ターン終了**（`end_turn`）。ACTIVE窓を閉じて次のターンへ進める（時間制限は無く、これが唯一の進行手段）。

**位置推定(raspi計測RSSI + PC側kNN)を実際に動かす手順:**

```bash
# --- raspi側(raspi/ 配下。スキャナは bleak 必要) ---
# 1) 各グリッド G1〜G9 でRSSIを収集して fingerprint CSV を作る（グリッドごとに実行）
python3 raspi/collect_phone_uuid_fingerprint.py --label G1 --duration 30
python3 raspi/collect_phone_uuid_fingerprint.py --label G2 --duration 30
#   ... G9 まで（既定の出力は phone_fingerprint.csv、同じ label は上書き）

# 2) ゲーム中は中継raspiがBLEをスキャンし、各ビーコンの生RSSIを
#    {"beacon": "phone1", "rssi": -67} 形式でゲームPCへUDP送信する
#    (送信側スクリプト = relay_rssi_sender.py。--server-ip はゲームPCのIP)
python3 raspi/relay_rssi_sender.py --server-ip 192.168.0.10

# 2') 同じ raspi で「通知受信→SenseHat発光」プロセスも起動しておく
#     (方法A: 中継raspi=ghost反応raspi。9101番でPCからの通知UDPを待受)
#     ghost_light_raspi.py は distance に応じてSenseHatを光らせる。
#     config.RASPI_TARGETS に (このraspiのIP, 9101) を追加しておく
#     (notify_magic は全宛先へ distance 付きで送るのでSCAN時に距離が届く)
python3 raspi/ghost_light_raspi.py --port 9101

# --- ゲームPC側 ---
# 3) raspi が作った fingerprint CSV を data/ に置く（config.FINGERPRINT_CSV は data/ を指す）
# 4) config.RASPI_TARGETS をその raspi の固定IP:9101 に設定
# 5) サーバを起動（生RSSIをUDP受信→バッファ→ターン開始ごとにkNN推定→魔法時に9101へ通知）
python3 server/main.py
```

**方法A: 中継raspi と ghost反応raspi の同一化**。1台の raspi が「RSSIをPCへ送る（9000宛, outbound）」と
「通知をPCから受けて光る（9101, inbound）」を同時に担う。別ポート・別方向なので同居できる。
ゲームPCは引き続き唯一の writer（位置推定・ゲーム判定を一元管理）で、raspi は入出力デバイスに徹する。

ゲームPCはBLEをスキャンしない。位置推定に使うRSSIはすべて raspi の計測値。
`phone_fingerprint.csv` が無い場合は推定をスキップして `player=None` のまま進行する
（収集前でもゲームは動く）。`TARGETS`（スマホのUUID）とビーコン名（`phone1`〜）は
`ble_rssi.py` と raspi 側ツールで同じ値に揃え、raspi が送る `"beacon"` 名も
fingerprint の列名（`phone1` 等）と一致させること。

実行ログの例:

```
[turn 1] ghost=GridPos(x=2, y=2) player=None
  [grid 3x3] user=なし(推定未確定) ghost=G9 (x=2, y=2) hp=1
  turn 1/10 (残り9) | ATTACK残り10 / SCAN残り10
   .  .  .
   .  .  .
   .  . [G]
  -> cast ATTACK at None (同一マスにゴーストなし: 効果なし)
  -> cast SCAN (dist=None)
[turn 2] ghost=GridPos(x=2, y=2) player=GridPos(x=2, y=2)
  [grid 3x3] user=G9 (x=2, y=2) ghost=G9 (x=2, y=2) hp=1
  turn 2/10 (残り8) | ATTACK残り9 / SCAN残り9
   .  .  .
   .  .  .
   .  . [*]
  -> cast ATTACK at GridPos(x=2, y=2) (ghost hp=0)
```

RSSI未送信のときは `player=None`（位置推定の入力が無いため）。実際は担当Dの中継raspiから
RSSIが届き、ターン開始ごとに位置が更新される。停止は `Ctrl+C`。

---

## テスト

```bash
pip install pytest          # 初回のみ
python3 -m pytest tests/ -v
```

`pytest` のみで動く（`pytest-asyncio` 等のプラグインは不要。非同期テストは各テスト内で
`asyncio.run()` に包んである）。

- **単体テスト** … `domain` / `position` / `motion_input` / `raspi_notifier` / `engine` を単独で検証。
- **統合テスト** (`test_integration.py`) … 実UDPソケット経由の RSSI受信→バッファ投入、および
  `main` 相当の配線でモーション入力（テスト用プロデューサ）→エンジン→通知が回り捕獲で終わるところまで。

入力デバイス（位置推定・joycon）が無くても、テストダブル（`RecordingNotifier` / `FixedEstimator`）で
ゲームロジックを完全に検証できる（実機・ハードウェアに依存しない）。

> 位置推定はフィンガープリントkNNで実装済み。契約テストは新仕様へ更新済み
> （`test_estimator_knn_predicts_nearest_grid` / `test_estimator_averages_window_samples` など。
> 一時CSVでfingerprintを与えて検証する）。
> joyconは **R/ZR＋振り → 魔法（ATTACK/SCAN）** と **Aボタン → ターンEND（end_turn）** を実装済み。
> 振り判定の純関数 `classify_shake()` は `test_classify_shake_*` で、ライブラリ/実機が無い経路は
> `test_joycon_source_requires_library_or_device` で検証する。

---

## 拡張ポイント（担当別 TODO）

- **位置推定（担当B）**: `position.py` の `PositionEstimator.estimate(snapshot) -> Optional[GridPos]`
  は **フィンガープリントkNN で実装済み**（コアは `ble_rssi.py`）。snapshot をビーコン別平均RSSI
  に畳み、fingerprint CSV の全サンプルと RMSE 比較して上位 k ラベルの多数決でグリッドを返す。
  精度調整は `config.py` の `KNN_K` / `KNN_MIN_VALID` / `KNN_WEIGHTED_VOTE` / `RSSI_WINDOW_SEC` で行う。
  三辺測位／学習モデルへ差し替える場合も入出力の型を保てばよい。
- **joyconモーション（担当C）**: `motion_input.py` の `JoyconMotionSource` 内 `reader_thread`。
  **右Joy-ConのAボタン → `TurnControlEvent("end_turn")`（ターン強制終了）は実装済み**
  （`pyjoycon` を使用、ボタン立ち上がりエッジで1回発火）。残るジェスチャ判定を実装し、
  `MagicType.ATTACK` / `SCAN` を同じ `out_queue` に `call_soon_threadsafe` で入れる。
- **RSSI送受信（担当D）**: `rssi_receiver.py` の受信フォーマットを送信側と合わせる。
- **ゲーム本体（担当A）**: `engine.py` の `_resolve_attack` / `_resolve_scan` に魔法の効果と
  勝敗ルールを実装。ゴーストの行動は `domain.py` の `Ghost.step` を差し替え（Strategyへの切り出しも可）。

---

## チームで合意すること（インターフェース）

仕様は暫定。実装前にこの4点を合わせておくと噛み合う。

| 項目 | 暫定仕様 | 担当 |
|---|---|---|
| RSSI送受信 (UDP) | raspiが計測した生RSSIを `{"beacon": "phone1", "rssi": -67}` でPCへ送信 | 担当D |
| BLEスキャン (raspi側) | スマホUUIDビーコン（`TARGETS` の UUID/iBeacon）を raspi がスキャン・計測 | 担当B / 担当D |
| フィンガープリント | グリッドラベル `G1`〜`G9` ごとに各 phone の RSSI を raspi で収集（`phone_fingerprint.csv`） | 担当B |
| raspi通知 (UDP) | `{"type": "magic", "magic": "ATTACK", "distance": 3}`（`distance` は任意） | 担当B / raspi班 |
| ジェスチャ → 魔法 | slash = `ATTACK` / circle = `SCAN` | 担当C |
| グリッド分割・ターン長 | `config.py` を参照（既定 3×3 = G1〜G9） | 共通 |

---

## 設定リファレンス（`config.py`）

| 変数 | 既定値 | 説明 |
|---|---|---|
| `RSSI_LISTEN_HOST` | `"0.0.0.0"` | RSSIを受けるUDPの待受アドレス |
| `RSSI_LISTEN_PORT` | `9000` | RSSIを受けるUDPポート |
| `RASPI_TARGETS` | `[("192.168.6.102", 9101)]` | 通知するraspiの `(host, port)`。複数登録可。受信側は `ghost_light_raspi.py`（既定9101）。**中継raspiと同一端末でも可**（固定IPを指定）。IPは各自の環境に置き換える |
| `GRID_W` / `GRID_H` | `3` / `3` | 空間のグリッド分割数（fingerprint の G1〜G9 = 3×3 に合わせる） |
| `GRID_COLS` | `3` | G番号 → `(row, col)` 変換の列数（`1 2 3 / 4 5 6 / 7 8 9`） |
| `RSSI_WINDOW_SEC` | `2.0` | 位置推定に使うRSSIの時間窓（秒）。kNNの平均RSSIもこの窓で取る |
| `RSSI_BUFFER_MAXLEN` | `20` | RSSIバッファがビーコンごとに保持する最大サンプル数 |
| `RSSI_WARMUP_SEC` | `3.0` | turn1開始前にRSSIバッファがたまるのを待つ最大秒数（0で待たない） |
| `RSSI_WARMUP_MIN_SAMPLES` | `5` | ウォームアップ完了に必要な、各ビーコンの時間窓内サンプル数。この件数以上のビーコンが `KNN_MIN_VALID` 個そろうまで待つ |
| `GHOST_HP` | `1` | ゴーストの初期体力（ATTACKで1減り、`hp<=0`で撃破） |
| `MAX_TURNS` | `10` | このターン数に達しても撃破できなければゲームオーバー（敗北） |
| `ATTACK_LIMIT` | `10` | ATTACK魔法の使用回数上限 |
| `SCAN_LIMIT` | `10` | SCAN魔法の使用回数上限 |
| `CLEAR_BLINK_TIMES` | `6` | クリア時に raspi を虹色点滅させる回数 |
| `CLEAR_BLINK_ON_SEC` / `CLEAR_BLINK_OFF_SEC` | `0.3` / `0.3` | 点滅の点灯／消灯時間（秒） |
| `GAMEOVER_TEXT` | `"GAME OVER"` | ゲームオーバー時に raspi へスクロール表示する文字 |
| `GAMEOVER_TEXT_COLOR` | `[255, 0, 0]` | ゲームオーバー表示の文字色（RGB） |
| `GAMEOVER_SCROLL_SPEED` | `0.08` | ゲームオーバー表示のスクロール速度（小さいほど速い） |
| `JOYCON_POLL_INTERVAL_SEC` | `0.05` | Joy-Conのステータスをポーリングする間隔（秒） |
| `SHAKE_THRESHOLD` | `2000` | 振り検知に使う加速度差の閾値（`diff_y`/`diff_z` の比較基準） |
| `FINGERPRINT_CSV` | `"phone_fingerprint_0624_10s_3.csv"` | kNN推定に使うフィンガープリントCSV（リポジトリ同梱の収集済みデータ） |
| `KNN_K` | `5` | 多数決に使う近傍数 |
| `KNN_MIN_VALID` | `8` | 距離計算に必要な共通ビーコンの最小数 |
| `KNN_WEIGHTED_VOTE` | `False` | `True` で逆距離重み付き投票 |

> **設計方針**: 設定・ハイパパラメータは**デフォルト引数を持たせない**。値は本ファイル（`config.py`）に
> 一元管理し、`main.py`（合成点）で各コンポーネントへ**明示的に注入**する。これにより「どこかの
> デフォルト値が黙って使われる」事故を防ぎ、調整箇所を `config.py` に集約する。

---

## 今後の展望

- 3Dへの拡張、ゴースト複数体、魔法の種類追加（`Ghost` / `MagicType` を拡張）
- 位置推定の精度向上（`RSSI_WINDOW_SEC` の調整、学習モデル化）
- ターン進行トリガの拡張（現状はAボタン=`end_turn`のみ。時間制やその他条件の追加）
- 終了時のリソース解放やシグナルハンドリングの整備
