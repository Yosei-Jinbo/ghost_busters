"""モーション入力。

MotionSource を抽象化し、joycon / SenseHat 等の入力実装を差し替え可能にする。
各実装は検出した MotionEvent を asyncio.Queue に push し続ける「常駐タスク」。
ゲームループはこのキューを消費するだけなので、入力デバイスの実装と
ゲーム本体を独立して開発できる。
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Union

from domain import MagicType

try:
    from pyjoycon import JoyCon, get_R_ids, get_R_id
except ImportError:  # joycon-python未導入環境では JoyconMotionSource 実行時にのみ失敗させる
    JoyCon = None
    get_R_ids = None
    get_R_id = None

logger = logging.getLogger(__name__)


def classify_shake(
    diff_y: float, diff_z: float, threshold: float
) -> Optional[MagicType]:
    """直前フレームとの加速度差から振りの向きを判定し、対応する魔法を返す。

    上下(Z方向)の振り -> ATTACK、左右(Y方向)の振り -> SCAN。
    どちらの閾値も超えない、または優劣がつかないなら None。
    ハードウェア無しで単体テストできるよう純関数として切り出す。
    """
    if diff_z > threshold and diff_z > diff_y:
        return MagicType.ATTACK
    if diff_y > threshold and diff_y > diff_z:
        return MagicType.SCAN
    return None


@dataclass
class MotionEvent:
    """検出したモーションと、対応する魔法。"""
    magic: MagicType
    raw: Optional[dict] = None


@dataclass
class TurnControlEvent:
    """ターン制御イベント。モーションと同じキューに流す制御信号。

    今は action="end_turn"(ターンの強制終了)のみ。joyconのAボタン等で
    タイムアウトを待たずにACTIVE窓を打ち切るのに使う。将来 pause/skip 等を足せる。
    """
    action: str = "end_turn"
    raw: Optional[dict] = None


# モーション入力キューに流れるイベント(魔法 or ターン制御)。
InputEvent = Union[MotionEvent, TurnControlEvent]


class MotionSource(ABC):
    """モーション入力の抽象。検出した InputEvent を out_queue に流し続ける。"""

    @abstractmethod
    async def run(self, out_queue: asyncio.Queue[InputEvent]) -> None:
        ...


class JoyconMotionSource(MotionSource):
    """joyconからBluetoothで入力を取得する。

    実装済みの入力は2系統:

    * **右Joy-ConのAボタン → ターン強制終了(end_turn)**。立ち上がりで1回。
    * **R または ZR を押しながら振る → 魔法発動**。上下(Z方向)で ATTACK、
      左右(Y方向)で SCAN。1回の押下につき1回だけ検知する(離すまで再検知しない)。

    いずれも同じ out_queue に call_soon_threadsafe で流す(送信ロジックは共通)。

    joycon系ライブラリ(joycon-python / pyjoycon)はブロッキング読取なので、
    別スレッド(run_in_executor)でポーリングし、loop.call_soon_threadsafe で
    asyncioキューへ安全に橋渡しする。`pyjoycon` 未導入や実機未接続のときは
    RuntimeError を送出する(joyconを使う設定で選ばれる入力源のため)。
    """

    def __init__(self, poll_interval_sec: float, shake_threshold: float) -> None:
        self.poll_interval_sec = poll_interval_sec
        self.shake_threshold = shake_threshold

    async def run(self, out_queue: asyncio.Queue[InputEvent]) -> None:
        loop = asyncio.get_running_loop()

        def reader_thread() -> None:
            if JoyCon is None or get_R_ids is None:
                raise RuntimeError(
                    "joycon-python(pyjoycon) が見つかりません。"
                    "`uv pip install joycon-python hidapi` を実行してください。"
                )

            joycon_id = get_R_id()
            if not joycon_id or joycon_id[0] is None:
                raise RuntimeError(
                    "右Joy-Conが見つかりません。ペアリング/接続を確認してください。"
                )
            logger.info("Joy-Con id: %s", joycon_id)

            joycon = JoyCon(*joycon_id)
            logger.info(
                "Joy-Con connected (A -> end_turn, R/ZR + shake -> magic)"
            )

            prev_a = 0
            prev_y = 0
            prev_z = 0
            # R/ZR 押下中の振り受付状態。flag=受付中、already_detected=今回押下で検知済み。
            flag = False
            already_detected = False

            while True:
                status = joycon.get_status()
                buttons = status.get("buttons", {}).get("right", {})

                # --- Aボタン: ターン強制終了(立ち上がりで1回) ---
                # 押しっぱなしでも連発しないようエッジ検出する。
                a = buttons.get("a", 0)
                if a and not prev_a:
                    logger.debug("A button pressed -> end_turn")
                    loop.call_soon_threadsafe(
                        out_queue.put_nowait, TurnControlEvent("end_turn")
                    )
                prev_a = a

                # --- R/ZR を押しながら振る: 魔法発動 ---
                is_button_held = (
                    buttons.get("r", 0) == 1 or buttons.get("zr", 0) == 1
                )

                accel = status.get("accel", {})
                current_y = accel.get("y", 0)
                current_z = accel.get("z", 0)
                # 直前フレームとの加速度差を「振りの激しさ」とみなす。
                diff_y = abs(current_y - prev_y)
                diff_z = abs(current_z - prev_z)

                if is_button_held:
                    # 押下中はまだ未検知のときだけ受付を開く。
                    if not already_detected:
                        flag = True
                else:
                    # 離したらリセットして次の押下に備える。
                    flag = False
                    already_detected = False

                if flag:
                    magic = classify_shake(diff_y, diff_z, self.shake_threshold)
                    if magic is not None:
                        loop.call_soon_threadsafe(
                            out_queue.put_nowait,
                            MotionEvent(
                                magic,
                                raw={"diff_y": diff_y, "diff_z": diff_z},
                            ),
                        )
                        # 1回流したら離すまで再検知をブロック。
                        flag = False
                        already_detected = True

                prev_y = current_y
                prev_z = current_z

                time.sleep(self.poll_interval_sec)

        await loop.run_in_executor(None, reader_thread)
