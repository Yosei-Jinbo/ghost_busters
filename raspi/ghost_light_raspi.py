"""ゴースト反応ライト(raspi側・単独実行)。

ゲームPC(`engine` -> `raspi_notifier`)が送るUDP通知の `distance` を読み取り、
`ghost_sensehat` の発光ロジックでSense HATを光らせる。ゴーストに近いほど派手に光る。

受信フォーマット(例): {"type": "magic", "magic": "SCAN", "light": "blue", "distance": 3}
  `light`(発光状態名: rainbow/red/green/blue/off)があればそれをそのまま描画する
  (光り方の判定はサーバ側 domain が持つ)。`light` が無ければ従来どおり `distance`
  から発光を更新する。どちらも無い/推定不可なら消灯側に倒す。壊れたパケットは無視する。
  `{"type": "reset"}` で消灯、`{"type": "result", "result": "clear"|"over"}` で
  終了演出(clear=虹色点滅 / over="GAME OVER"スクロール表示)を行う。

ゲームから distance を届けるには、`config.RASPI_TARGETS` にこのraspiの
`(<ip>, <port>)` を追加する(notify_magic は全宛先へ distance 付きで送るため)。

起動例:
    python3 ghost_light_raspi.py --port 9101
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
from typing import Optional

# 終了演出のパラメータは server/config.py に集約している。raspi単独実行でも
# 参照できるよう server/ を import path に足してから config を読み込む。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

import config  # noqa: E402  (sys.path 調整後に読み込む)
import ghost_sensehat  # noqa: E402

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ghost-reactive Sense HAT light (raspi side)"
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="UDP listen host. Default: 0.0.0.0"
    )
    parser.add_argument(
        "--port", type=int, default=9101, help="UDP listen port. Default: 9101"
    )
    parser.add_argument(
        "--no-sensehat", action="store_true", help="Disable Sense HAT (log only)."
    )
    return parser.parse_args()


_VALID_LIGHT_STATES = {"rainbow", "red", "green", "blue", "off"}


def extract_distance(msg: dict) -> Optional[int]:
    """UDPメッセージから distance(int) を取り出す。無い/不正なら None。"""
    d = msg.get("distance")
    if d is None:
        return None
    try:
        return int(d)
    except (TypeError, ValueError):
        return None


def extract_light(msg: dict) -> Optional[str]:
    """UDPメッセージから light(発光状態名) を取り出す。無い/未知なら None。

    サーバ(domain)側が光り方を決めて送るのが本流。値が既知の状態名でなければ
    None を返し、呼び出し側は distance ベースのフォールバックに倒す。
    """
    light = msg.get("light")
    if isinstance(light, str) and light in _VALID_LIGHT_STATES:
        return light
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()

    sense = ghost_sensehat.init_sensehat(no_sensehat=args.no_sensehat)
    last_state: Optional[str] = None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.host, args.port))
    logger.info("Listening ghost distance on %s:%d", args.host, args.port)

    try:
        while True:
            data, _addr = sock.recvfrom(4096)
            try:
                msg = json.loads(data.decode())
            except (ValueError, UnicodeDecodeError):
                continue  # 壊れたパケットは無視
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "reset":
                # ターン開始リセット: LEDを消灯し、次のdistanceで必ず再描画されるようにする。
                if sense is not None:
                    sense.clear()
                last_state = None
                logger.info("reset -> clear LED")
                continue
            if msg.get("type") == "result":
                # ゲーム終了演出: clear=虹色点滅 / over=GAME OVER表示(表示中はブロッキング)。
                if msg.get("result") == "clear":
                    logger.info("GAME CLEAR -> rainbow blink")
                    ghost_sensehat.blink(
                        sense, "rainbow",
                        config.CLEAR_BLINK_TIMES,
                        config.CLEAR_BLINK_ON_SEC,
                        config.CLEAR_BLINK_OFF_SEC,
                    )
                else:
                    logger.info("GAME OVER -> show message")
                    ghost_sensehat.show_message(
                        sense,
                        config.GAMEOVER_TEXT,
                        config.GAMEOVER_TEXT_COLOR,
                        config.GAMEOVER_SCROLL_SPEED,
                    )
                last_state = None
                continue
            # 光り方は domain が決めた light をそのまま使う。無ければ distance から求める。
            light = extract_light(msg)
            if light is not None:
                last_state = ghost_sensehat.update_light_state(sense, light, last_state)
                logger.info("light=%s -> %s", light, last_state)
            else:
                distance = extract_distance(msg)
                last_state = ghost_sensehat.update_sensehat_feedback(sense, distance, last_state)
                logger.info("distance=%s -> %s", distance, last_state)
    except KeyboardInterrupt:
        logger.info("stopped")
    finally:
        if sense is not None:
            sense.clear()
        sock.close()


if __name__ == "__main__":
    main()
