"""統合テスト。

実際のUDPソケットや複数コンポーネントの連携を、実機(joycon/raspi)無しで検証する。
"""
import asyncio
import json
import socket

from domain import GameState, Ghost, GridPos, MagicType, Phase
from engine import GameEngine
from motion_input import MotionEvent, TurnControlEvent
from position import RSSIBuffer
from rssi_receiver import RSSIReceiverProtocol


# ---- RSSI受信(UDP) -> バッファ ----
async def _start_receiver(buffer):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: RSSIReceiverProtocol(buffer),
        local_addr=("127.0.0.1", 0),  # 空きポート
    )
    return transport, transport.get_extra_info("sockname")[1]


def test_rssi_udp_packets_reach_buffer():
    async def scenario():
        buffer = RSSIBuffer(window_sec=100.0, maxlen=20)
        transport, port = await _start_receiver(buffer)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(
            json.dumps({"beacon": "phone-1", "rssi": -67}).encode(),
            ("127.0.0.1", port),
        )
        sender.sendto(
            json.dumps({"beacon": "phone-1", "rssi": -70}).encode(),
            ("127.0.0.1", port),
        )
        sender.close()
        await asyncio.sleep(0.1)  # 受信処理を待つ
        transport.close()
        return buffer.snapshot()

    snap = asyncio.run(scenario())
    assert "phone-1" in snap
    assert len(snap["phone-1"]) == 2
    assert snap["phone-1"][-1].rssi == -70.0


def test_rssi_malformed_packet_is_ignored():
    async def scenario():
        buffer = RSSIBuffer(window_sec=100.0, maxlen=20)
        transport, port = await _start_receiver(buffer)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(b"not-json", ("127.0.0.1", port))  # 壊れたパケット
        sender.sendto(
            json.dumps({"beacon": "b", "rssi": -50}).encode(),
            ("127.0.0.1", port),
        )
        sender.close()
        await asyncio.sleep(0.1)
        transport.close()
        return buffer.snapshot()

    snap = asyncio.run(scenario())
    assert list(snap.keys()) == ["b"]  # 壊れたパケットは無視
    assert len(snap["b"]) == 1


# ---- ゲーム全体(main相当の配線、実機なし) ----
class RecordingNotifier:
    def __init__(self):
        self.calls = []
        self.resets = 0
        self.results = []

    def notify_magic(self, magic, payload):
        self.calls.append((magic, payload))

    def notify_reset(self):
        self.resets += 1

    def notify_result(self, result):
        self.results.append(result)


class FixedEstimator:
    def __init__(self, pos):
        self.pos = pos

    def estimate(self, snapshot):
        return self.pos


def test_end_to_end_motion_until_capture():
    """モーション入力 -> エンジン -> 通知 の一連が回り、捕獲でゲームが終わる。

    実機Joy-Conの代わりに、受付窓中へ魔法イベントを流し込むテスト用プロデューサで
    main相当の配線(入力キュー -> エンジン -> 通知)を検証する。
    """

    async def feed_motions(q):
        # 実機Joy-Conの代わり: 魔法を1回、続けてターン終了(Aボタン相当)を送る。
        await asyncio.sleep(0.02)
        await q.put(MotionEvent(MagicType.ATTACK))
        await asyncio.sleep(0.02)
        await q.put(TurnControlEvent("end_turn"))

    async def scenario():
        ghost = Ghost(pos=GridPos(2, 2))
        ghost.step = lambda state: None  # 動かない -> 必ず捕獲できる
        state = GameState(grid_w=5, grid_h=5, ghost=ghost)
        notifier = RecordingNotifier()
        q: asyncio.Queue[MotionEvent] = asyncio.Queue()
        engine = GameEngine(
            state=state,
            buffer=RSSIBuffer(window_sec=1.0, maxlen=10),
            estimator=FixedEstimator(GridPos(2, 2)),  # プレイヤー位置=ゴースト位置
            notifier=notifier,
            motion_queue=q,
            max_turns=10,
            attack_limit=10,
            scan_limit=10,
            warmup_sec=0.0,
            warmup_min_samples=1,
            warmup_min_beacons=1,
        )
        motion_task = asyncio.create_task(feed_motions(q))
        await asyncio.wait_for(engine.run(), timeout=3.0)
        motion_task.cancel()
        return state, notifier

    state, notifier = asyncio.run(scenario())
    assert state.phase == Phase.GAME_OVER
    assert state.result == "clear"  # ATTACKでhp0 -> 全ゴースト撃破でクリア
    # ACTIVE窓中に魔法(ATTACK)が1回届いてから end_turn で撃破・終了する
    assert len(notifier.calls) >= 1
    assert all(m in (MagicType.ATTACK, MagicType.SCAN) for m, _ in notifier.calls)
