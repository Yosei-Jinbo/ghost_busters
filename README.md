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

```bash
# ゲームサーバ（リポジトリ直下で実行）
python3 server/main.py

# テスト
python3 -m pytest tests/ -v
```

詳細は **[docs/README.md](docs/README.md)** と **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** を参照。
