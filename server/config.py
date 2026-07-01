"""設定値(仮)。本番値はチームで合わせて調整する。"""
from __future__ import annotations

import os

# このファイル(server/config.py)から見たリポジトリルート。
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# --- ネットワーク設定 ---
RSSI_LISTEN_HOST = "0.0.0.0"   # 中継raspiからのRSSIを受けるUDPの待受
RSSI_LISTEN_PORT = 9000

# 魔法発動を通知するraspiの宛先。複数登録可。(host, port)
# 方法A: 魔法raspi と 中継raspi(RSSIを送ってくるPi)を同一端末にできる。
#   その場合 host は中継raspiと同じ固定IPにし、port は魔法受信用(9100)を使う。
#   RSSI送信(PC:9000宛, outbound)と魔法受信(raspi:9100, inbound)は別ポート・別方向で同居可能。
#   → 下のIPは各自の中継raspiの固定IPに置き換えること。
RASPI_TARGETS = [
    ("192.168.6.102", 9100),
    ("192.168.6.102", 9101),
    # ("192.168.0.51", 9100),
]

# --- ゲーム設定 ---
# fingerprint の G1〜G9 (3×3) に合わせる。GRID_COLS と整合させること。
GRID_W = 3
GRID_H = 3
GRID_COLS = 3           # G番号 -> (row, col) 変換に使う列数 (1 2 3 / 4 5 6 / 7 8 9)
# ターンの進行はJoy-ConのAボタン(end_turn)でのみ行う。時間制の受付窓は持たない。
RSSI_WINDOW_SEC = 2.0   # 位置推定に使うRSSIの時間窓
RSSI_BUFFER_MAXLEN = 20  # RSSIバッファがビーコンごとに保持する最大サンプル数
# turn1開始前のウォームアップ。RSSIバッファが十分たまるまで待ってから位置推定する。
# 「時間窓(RSSI_WINDOW_SEC)内のサンプルが RSSI_WARMUP_MIN_SAMPLES 件以上あるビーコン」が
# KNN_MIN_VALID 個そろうまで待つ(=kNN推定に必要な数のビーコンが十分たまるまで)。
# RSSI_WARMUP_SEC を過ぎても満たなければ、そのまま player=None 相当で開始する。
# RSSI_WARMUP_SEC=0 にすると待たない。
RSSI_WARMUP_SEC = 3.0
RSSI_WARMUP_MIN_SAMPLES = 5  # 各ビーコンのバッファに必要な最小サンプル数(<= RSSI_BUFFER_MAXLEN)

# --- ゲームルール ---
GHOST_HP = 1            # ゴーストの初期体力(ATTACKで1減り、hp<=0で撃破)
MAX_TURNS = 10          # このターン数に達しても撃破できなければゲームオーバー(敗北)
ATTACK_LIMIT = 10       # ATTACK魔法の使用回数上限
SCAN_LIMIT = 10         # SCAN魔法の使用回数上限

# --- ゲーム終了演出 (raspi側 ghost_light_raspi.py が使用) ---
# 終了通知({"type":"result"})を受けたraspiの光り方。ghost_light_raspi.py が
# sys.path 経由でこのconfigをimportし、blink()/show_message()へ明示的に渡す。
CLEAR_BLINK_TIMES = 6            # クリア時の虹色点滅の回数
CLEAR_BLINK_ON_SEC = 0.3        # 点灯時間(秒)
CLEAR_BLINK_OFF_SEC = 0.3       # 消灯時間(秒)
GAMEOVER_TEXT = "GAME OVER"     # ゲームオーバー時にスクロール表示する文字
GAMEOVER_TEXT_COLOR = [255, 0, 0]  # 文字色(赤)
GAMEOVER_SCROLL_SPEED = 0.08    # スクロール速度(小さいほど速い)

# --- モーション入力 ---
# ハイパパラメータはここに集約し、生成時に明示的に注入する(デフォルト引数は持たせない)。
JOYCON_POLL_INTERVAL_SEC = 0.05  # Joy-Conのステータスをポーリングする間隔(秒)
SHAKE_THRESHOLD = 2000           # 振り検知に使う加速度差の閾値(diff_y/diff_z の比較基準)

# --- 位置推定(フィンガープリントkNN) ---
# RSSIは中継raspiがBLEスキャンして測定した値をUDPで受信する(rssi_receiver)。
# ゲームPCはBLEを自分でスキャンしない。fingerprint CSVもraspiが収集したものを使う。
# data/ ディレクトリのフィンガープリントCSV(collect_phone_uuid_fingerprint.py(raspi側)の出力)。
# CWDに依存しないよう絶対パスで解決する。
FINGERPRINT_CSV = os.path.join(_REPO_ROOT, "data", "phone_fingerprint_0624_10s_3.csv")
KNN_K = 5               # 多数決に使う近傍数
KNN_MIN_VALID = 8       # 距離計算に必要な共通ビーコンの最小数
KNN_WEIGHTED_VOTE = False  # True で逆距離重み付き投票
