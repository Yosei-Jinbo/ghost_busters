"""魔法発動・ターンリセットの通知(中央サーバ -> 登録raspi)。

魔法が発動したら、登録済みのraspiへUDPで通知する。
受信側(SenseHatで光る等)は `ghost_light_raspi.py`(raspi上で実行)。
送信フォーマット:
  - 魔法      : {"type": "magic", "magic": "ATTACK", "distance": 3}
  - リセット  : {"type": "reset"}  (ターン開始時にLEDを消灯させる)
  - 終了      : {"type": "result", "result": "clear"}  (勝利=虹色点滅 / 敗北=GAME OVER表示)
fire-and-forget の単純UDP送信なので非同期化は不要。
"""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import List, Optional

from domain import MagicType


@dataclass(frozen=True)
class RaspiTarget:
    host: str
    port: int


class RaspiNotifier:
    """登録済みのraspiへ魔法発動を通知する。"""

    def __init__(self, targets: List[RaspiTarget]) -> None:
        self.targets: List[RaspiTarget] = list(targets)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def register(self, target: RaspiTarget) -> None:
        self.targets.append(target)

    def notify_magic(self, magic: MagicType, payload: Optional[dict]) -> None:
        # 送信フォーマット(仮): {"type": "magic", "magic": "ATTACK", "distance": 3}
        msg = {"type": "magic", "magic": magic.name}
        if payload:
            msg.update(payload)
        self._send(msg)

    def notify_reset(self) -> None:
        """ターン開始時などにraspiのLEDを消灯させるリセット通知。"""
        self._send({"type": "reset"})

    def notify_result(self, result: str) -> None:
        """ゲーム終了をraspiへ通知する。result は "clear"(勝利) / "over"(敗北)。

        受信側(`ghost_light_raspi.py`)が clear=虹色点滅 / over=GAME OVER表示 に振り分ける。
        """
        self._send({"type": "result", "result": result})

    def _send(self, msg: dict) -> None:
        data = json.dumps(msg).encode()
        for t in self.targets:
            self._sock.sendto(data, (t.host, t.port))
