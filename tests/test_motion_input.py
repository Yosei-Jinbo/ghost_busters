"""motion_input.py の単体テスト(asyncioはasyncio.runで包んで実行)。"""
import asyncio

import pytest

from config import SHAKE_THRESHOLD
from domain import MagicType
from motion_input import (
    JoyconMotionSource,
    MotionEvent,
    classify_shake,
)


def test_classify_shake_up_down_is_attack():
    # 上下(Z方向)が優勢 -> ATTACK
    assert classify_shake(
        diff_y=0, diff_z=SHAKE_THRESHOLD + 1, threshold=SHAKE_THRESHOLD
    ) == MagicType.ATTACK


def test_classify_shake_left_right_is_scan():
    # 左右(Y方向)が優勢 -> SCAN
    assert classify_shake(
        diff_y=SHAKE_THRESHOLD + 1, diff_z=0, threshold=SHAKE_THRESHOLD
    ) == MagicType.SCAN


def test_classify_shake_below_threshold_is_none():
    # どちらも閾値未満なら検知しない
    assert classify_shake(
        diff_y=SHAKE_THRESHOLD - 1, diff_z=SHAKE_THRESHOLD - 1, threshold=SHAKE_THRESHOLD
    ) is None


def test_joycon_source_requires_library_or_device():
    """AボタンによるターンEND終了を実装済み。

    ただし pyjoycon 未導入 / 実機未接続の環境では RuntimeError を送出する
    (CIやハード無し環境ではこちらの経路になる)。ジェスチャ判定は担当CのTODO。
    """

    async def scenario():
        q: asyncio.Queue[MotionEvent] = asyncio.Queue()
        await JoyconMotionSource(
            poll_interval_sec=0.05, shake_threshold=SHAKE_THRESHOLD
        ).run(q)

    with pytest.raises(RuntimeError):
        asyncio.run(scenario())
