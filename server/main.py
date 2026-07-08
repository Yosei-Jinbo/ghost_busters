"""エントリポイント。各アダプタを組み立て、常駐プロセスとゲームループを並行起動する。

  - RSSI受信(UDP)        : 常駐プロデューサ -> RSSIBuffer
  - モーション入力(joycon) : 常駐プロデューサ -> motion_queue
  - GameEngine           : 単一コンシューマ。状態を所有しターンを回す。
"""
from __future__ import annotations

import asyncio

import config
from domain import DEFAULT_SETTINGS, BaseGhost, GameState, GridPos
from engine import GameEngine
from motion_input import InputEvent, JoyconMotionSource
from position import PositionEstimator, RSSIBuffer
from raspi_notifier import RaspiNotifier, RaspiTarget
from rssi_receiver import start_rssi_receiver


async def main() -> None:
    # --- 状態 ---
    # グリッドは実行基盤(config, fingerprintレイアウト依存)、ゲームルールは domain(DEFAULT_SETTINGS)。
    # ゴーストの初期体力は BaseGhost の既定値(hp=1)を使う。
    ghost = BaseGhost(pos=GridPos(config.GRID_W // 2, config.GRID_H // 2))
    state = GameState(grid_w=config.GRID_W, grid_h=config.GRID_H, ghost=ghost)

    # --- 入出力アダプタ ---
    buffer = RSSIBuffer(
        window_sec=config.RSSI_WINDOW_SEC,
        maxlen=config.RSSI_BUFFER_MAXLEN,
    )
    estimator = PositionEstimator(
        config.GRID_W,
        config.GRID_H,
        fingerprint_path=config.FINGERPRINT_CSV,
        k=config.KNN_K,
        min_valid=config.KNN_MIN_VALID,
        weighted_vote=config.KNN_WEIGHTED_VOTE,
        grid_cols=config.GRID_COLS,
    )
    notifier = RaspiNotifier([RaspiTarget(h, p) for h, p in config.RASPI_TARGETS])
    motion_queue: asyncio.Queue[InputEvent] = asyncio.Queue()

    motion_source = JoyconMotionSource(
        poll_interval_sec=config.JOYCON_POLL_INTERVAL_SEC,
        shake_threshold=config.SHAKE_THRESHOLD,
    )

    engine = GameEngine(
        state=state,
        buffer=buffer,
        estimator=estimator,
        notifier=notifier,
        motion_queue=motion_queue,
        max_turns=DEFAULT_SETTINGS.max_turns,
        warmup_sec=config.RSSI_WARMUP_SEC,
        warmup_min_samples=config.RSSI_WARMUP_MIN_SAMPLES,
        warmup_min_beacons=config.KNN_MIN_VALID,
    )

    # --- RSSI受信を常駐起動 (UDP: 中継raspiが測定した生RSSIを受信) ---
    # ゲームPCはBLEを自分でスキャンしない。位置推定に使う強度はすべて中継raspi計測値。
    await start_rssi_receiver(buffer, config.RSSI_LISTEN_HOST, config.RSSI_LISTEN_PORT)

    # --- プロデューサ(モーション) と コンシューマ(ゲームループ) を並行実行 ---
    tasks = [
        asyncio.create_task(motion_source.run(motion_queue)),
        asyncio.create_task(engine.run()),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in done:
        t.result()  # 例外があれば送出


if __name__ == "__main__":
    try:
        # 右Joy-Con実機でモーションを検知して起動する。
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[server] stopped")
