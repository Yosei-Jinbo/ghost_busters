"""ゴーストへの近さをSense HATで表現する表示ロジック(raspi側)。

`realtime_phone_uuid_grid_estimator_ghost.py` のSense HATフィードバック部分を、
距離(int)を入力とする純粋な表示ライブラリとして切り出したもの。BLE推定や
ネットワークには依存せず、「距離 -> 発光」の対応だけを担う。

発光ルール(ゴーストとのマンハッタン距離):
  distance == 0    : 虹色 (同一セル=最接近/捕獲)
  distance == 1    : 赤
  distance == 2    : 緑
  distance == 3    : 青
  distance >= 4    : 消灯
  distance is None : 消灯 (推定不可)
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

try:
    from sense_hat import SenseHat
except ImportError:  # SenseHat未導入環境(PC等)ではNoneにして発光だけ無効化する
    SenseHat = None

logger = logging.getLogger(__name__)

# 距離 -> 発光状態名。ここに無い距離(>=4)は "off" に落とす。
_DISTANCE_STATES = {0: "rainbow", 1: "red", 2: "green", 3: "blue"}

# 状態名 -> clear() に渡す単色RGB。"rainbow"/"off" はここで扱わず個別処理。
_SOLID_COLORS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
}


def init_sensehat(no_sensehat: bool):
    """Sense HAT LEDを初期化する。ライブラリ/実機が無ければ None を返す。

    None を返した場合でも呼び出し側は距離計算を続けられる(発光のみ無効)。
    """
    if no_sensehat:
        return None
    if SenseHat is None:
        logger.warning("sense_hat module is not installed. Sense HAT feedback disabled.")
        return None
    try:
        sense = SenseHat()
        sense.clear()
        return sense
    except Exception as e:  # 実機初期化失敗は致命ではない(発光のみ無効化)
        logger.warning("Sense HAT initialization failed: %s. Feedback disabled.", e)
        return None


def rainbow_pixels() -> List[List[int]]:
    """Sense HAT 8x8 LED用の虹色パターン(64画素)を返す。"""
    colors = [
        [255, 0, 0],      # red
        [255, 80, 0],     # orange
        [255, 255, 0],    # yellow
        [0, 255, 0],      # green
        [0, 255, 255],    # cyan
        [0, 0, 255],      # blue
        [128, 0, 255],    # purple
        [255, 0, 128],    # magenta
    ]
    return [colors[(x + y) % len(colors)] for y in range(8) for x in range(8)]


def distance_to_state(distance: Optional[int]) -> str:
    """距離 -> 発光状態名を返す(純関数)。0:rainbow 1:red 2:green 3:blue 他:off。"""
    if distance is None:
        return "off"
    return _DISTANCE_STATES.get(distance, "off")


def render_state(sense, state: str) -> None:
    """状態名に対応する発光を Sense HAT に反映する(sense が None なら何もしない)。"""
    if sense is None:
        return
    if state == "rainbow":
        sense.set_pixels(rainbow_pixels())
    elif state in _SOLID_COLORS:
        sense.clear(_SOLID_COLORS[state])
    else:  # "off" ほか
        sense.clear()


def update_sensehat_feedback(sense, distance: Optional[int], last_state: Optional[str]) -> str:
    """ゴーストとの距離に応じてSense HATを更新し、新しい状態名を返す。

    直前と同じ状態なら再描画しない(ちらつき防止)。返り値を次回の last_state に渡す。
    sense が None(実機なし)でも状態遷移だけは計算して返す。
    """
    state = distance_to_state(distance)
    if state == last_state:
        return state
    render_state(sense, state)
    return state


def blink(sense, state: str, times: int, on_sec: float, off_sec: float) -> None:
    """指定状態の発光と消灯を交互に times 回繰り返して点滅させる。

    ゲームクリア演出などに使う。ブロッキング(time.sleep)なので、常時受信ループ
    ではなく終了通知の受信直後にだけ呼ぶ想定。sense が None なら何もしない。
    """
    if sense is None:
        return
    for _ in range(times):
        render_state(sense, state)
        time.sleep(on_sec)
        sense.clear()
        time.sleep(off_sec)


def show_message(sense, text: str, text_color: List[int], scroll_speed: float) -> None:
    """Sense HAT に文字列をスクロール表示する(ゲームオーバー演出など)。

    ブロッキング(表示が終わるまで戻らない)。sense が None なら何もしない。
    """
    if sense is None:
        return
    sense.show_message(text, scroll_speed=scroll_speed, text_colour=text_color)
