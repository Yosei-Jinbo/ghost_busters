# モンスター探しゲーム

役割ごとに4コンポーネントへ分割している。

| ディレクトリ | 役割 |
|---|---|
| `server/` | ゲーム実行サーバ（中央サーバ）。位置推定・ターン進行・魔法解決。エントリは `server/main.py` |
| `raspi/` | RSSI送受信・SenseHat発光を担うraspi側スクリプト |
| `data/` | フィンガープリントCSV等のデータ |
| `docs/` | ドキュメント（[README](docs/README.md) / [ARCHITECTURE](docs/ARCHITECTURE.md) / slide） |
| `tests/` | 単体・統合テスト |

## クイックスタート

実機は raspi → ゲームPC の順に実行する。

```bash
# フィンガープリント収集（raspi、ゲーム前に G1〜G9 を1回ずつ）
python3 raspi/collect_phone_uuid_fingerprint.py --label G1 --duration 30

# RSSIをゲームPCへ送信（中継raspi）
python3 raspi/relay_rssi_sender.py --server-ip <ゲームPCのIP>

# 通知受信→SenseHat発光（raspi、9101で待受）
python3 raspi/ghost_light_raspi.py --port 9101

# ゲームサーバ（リポジトリ直下で実行）
python3 server/main.py

# テスト
python3 -m pytest tests/ -v
```

> 事前に収集した fingerprint CSV を `data/` に置き、`server/config.py` の `FINGERPRINT_CSV` と
> `RASPI_TARGETS`（raspiのIP:9100/9101）を合わせておく。詳細は **[docs/README.md](docs/README.md)** を参照。

詳細は **[docs/README.md](docs/README.md)** と **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** を参照。
