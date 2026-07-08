"""engine.py の単体テスト(中央サーバの心臓部)。

位置推定とraspi通知はテストダブルに差し替え、ターン進行・魔法発動・勝敗判定を
決定的に検証する。
"""
import asyncio
import time

from domain import BaseGhost, GameSettings, GameState, GridPos, MagicType, Phase
from engine import GameEngine
from motion_input import MotionEvent, TurnControlEvent
from position import RSSIBuffer, RSSISample


class RecordingNotifier:
    """UDP送信の代わりに呼び出しを記録するダブル。"""

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
    """常に固定の位置(またはNone)を返すダブル。"""

    def __init__(self, pos):
        self.pos = pos

    def estimate(self, snapshot):
        return self.pos


def _make_engine(
    player_pos=None,
    ghost_pos=(2, 2),
    max_turns=100,
    warmup_sec=0.0,
    warmup_min_samples=1,
    warmup_min_beacons=1,
    buffer=None,
    magic_uses_per_turn=None,
    magic_uses_per_game=None,
):
    ghost = BaseGhost(pos=GridPos(*ghost_pos))
    state = GameState(grid_w=5, grid_h=5, ghost=ghost)
    notifier = RecordingNotifier()
    engine = GameEngine(
        state=state,
        buffer=buffer if buffer is not None else RSSIBuffer(window_sec=1.0, maxlen=10),
        estimator=FixedEstimator(GridPos(*player_pos) if player_pos else None),
        notifier=notifier,
        motion_queue=asyncio.Queue(),
        max_turns=max_turns,
        warmup_sec=warmup_sec,
        warmup_min_samples=warmup_min_samples,
        warmup_min_beacons=warmup_min_beacons,
        magic_uses_per_turn=magic_uses_per_turn,
        magic_uses_per_game=magic_uses_per_game,
    )
    return engine, notifier


# ---- TURN_START ----
def test_turn_start_increments_turn_and_sets_player_position():
    engine, _ = _make_engine(player_pos=(1, 3))
    engine.state.ghost.step = lambda state: None  # ランダム移動を固定
    asyncio.run(engine._turn_start())
    assert engine.state.turn == 1
    assert engine.state.phase == Phase.TURN_START
    assert engine.state.player.pos == GridPos(1, 3)


def test_turn_start_sends_reset_notification():
    # 各ターン開始時にraspiへリセット通知(LED消灯)を1回送る。
    engine, notifier = _make_engine(player_pos=(1, 1))
    engine.state.ghost.step = lambda state: None
    asyncio.run(engine._turn_start())
    assert notifier.resets == 1


def test_turn_start_calls_ghost_step():
    engine, _ = _make_engine(player_pos=(0, 0))
    calls = []
    engine.state.ghost.step = lambda state: calls.append(1)
    asyncio.run(engine._turn_start())
    assert len(calls) == 1


def test_turn_start_keeps_previous_position_when_estimate_is_none():
    engine, _ = _make_engine(player_pos=None)
    engine.state.player.pos = GridPos(4, 4)  # 前回の推定値
    engine.state.ghost.step = lambda state: None
    asyncio.run(engine._turn_start())
    assert engine.state.player.pos == GridPos(4, 4)  # Noneなら上書きしない


def test_turn_start_prints_user_grid(capsys):
    # 推定後・ACTIVE前に、グリッドとユーザ位置・ゴースト位置が標準出力に出る。
    engine, _ = _make_engine(player_pos=(1, 0), ghost_pos=(2, 2))  # user=G2 / ghost=G13
    engine.state.ghost.step = lambda state: None
    asyncio.run(engine._turn_start())
    out = capsys.readouterr().out
    assert "[grid 5x5]" in out
    assert "user=G2" in out
    assert "ghost=G13" in out
    assert "hp=1" in out  # ゴーストの体力が表示される
    assert "[U]" in out  # ユーザのセルが描画される
    assert "[G]" in out  # ゴーストのセルが描画される
    # ターン進行と、このターンの各魔法の残り使用回数のステータス行(既定は各1回)。
    assert "turn 1/100" in out
    assert "残り99" in out          # 100 - 1
    assert "ATTACK:残1" in out       # ターン開始直後は未使用
    assert "SCAN:残1" in out


def test_print_grid_marks_none_when_no_position(capsys):
    engine, _ = _make_engine(player_pos=None, ghost_pos=(2, 2))
    engine.state.player.pos = None
    engine._print_grid()
    out = capsys.readouterr().out
    assert "user=なし" in out
    assert "[U]" not in out  # 位置が無ければ描画されない
    assert "[G]" in out  # ゴーストは常に描画される


def test_print_grid_marks_overlap_when_user_and_ghost_same_cell(capsys):
    # ユーザとゴーストが同セル(捕獲状態)なら [*] で表す。
    engine, _ = _make_engine(player_pos=(2, 2), ghost_pos=(2, 2))
    engine.state.player.pos = GridPos(2, 2)
    engine._print_grid()
    out = capsys.readouterr().out
    assert "[*]" in out
    assert "[U]" not in out
    assert "[G]" not in out


# ---- 魔法 ----
def test_cast_magic_applies_and_notifies_with_distance():
    engine, notifier = _make_engine(player_pos=(0, 0), ghost_pos=(0, 3))
    engine.state.player.pos = GridPos(0, 0)
    engine._cast_magic(MagicType.SCAN)
    assert len(notifier.calls) == 1
    magic, payload = notifier.calls[0]
    assert magic == MagicType.SCAN
    # distance=3 -> light "blue"。payload には light と distance の両方が載る。
    assert payload == {"light": "blue", "distance": 3}  # manhattan((0,0),(0,3))


def test_cast_magic_payload_light_off_without_player_position():
    engine, notifier = _make_engine(player_pos=None)
    engine.state.player.pos = None
    engine._cast_magic(MagicType.ATTACK)
    _, payload = notifier.calls[0]
    # 位置未確定なら distance は付かず、light は off。
    assert payload == {"light": "off"}


# ---- ACTIVE(受付窓: Aボタンで閉じる) ----
def test_turn_active_processes_motions_until_end_turn():
    """届いた魔法を順に発動し、end_turn(Aボタン)で窓を閉じる。"""
    engine, notifier = _make_engine(player_pos=(1, 1))
    engine.state.player.pos = GridPos(1, 1)

    async def scenario():
        async def producer():
            for m in (MagicType.ATTACK, MagicType.SCAN, MagicType.ATTACK):
                await engine.motion_queue.put(MotionEvent(m))
            await engine.motion_queue.put(TurnControlEvent("end_turn"))

        await asyncio.gather(
            asyncio.wait_for(engine._turn_active(), timeout=1.0),
            producer(),
        )

    asyncio.run(scenario())
    # 各魔法は1ターンに1回。2回目のATTACKは不発なので通知は ATTACK, SCAN の2件。
    assert [m for m, _ in notifier.calls] == [MagicType.ATTACK, MagicType.SCAN]


def test_turn_active_ends_on_control_event():
    """TurnControlEvent("end_turn")でACTIVE窓を閉じる。

    タイムアウトは無いので、end_turn が来るまで待ち続ける(wait_for 1.0で保証)。
    end_turn後に届いた魔法(SCAN)は処理されない。
    """
    engine, notifier = _make_engine(player_pos=(1, 1))
    engine.state.player.pos = GridPos(1, 1)

    async def scenario():
        async def producer():
            await engine.motion_queue.put(MotionEvent(MagicType.ATTACK))
            await engine.motion_queue.put(TurnControlEvent("end_turn"))
            await engine.motion_queue.put(MotionEvent(MagicType.SCAN))  # 終了後: 無視される

        await asyncio.gather(
            asyncio.wait_for(engine._turn_active(), timeout=1.0),
            producer(),
        )

    asyncio.run(scenario())
    # end_turnで打ち切られ、ATTACKのみ発動・SCANは届かない。
    assert [m for m, _ in notifier.calls] == [MagicType.ATTACK]


def test_turn_active_ends_immediately_when_all_ghosts_defeated():
    """全ゴースト撃破時は end_turn(Aボタン)を待たずにACTIVE窓が閉じる。"""
    engine, notifier = _make_engine(player_pos=(1, 1), ghost_pos=(1, 1))
    engine.state.player.pos = GridPos(1, 1)
    engine.state.ghost.hp = 1

    async def scenario():
        async def producer():
            # 同一マスでATTACK -> hp 1->0。end_turn は送らない。
            await engine.motion_queue.put(MotionEvent(MagicType.ATTACK))

        await asyncio.gather(
            asyncio.wait_for(engine._turn_active(), timeout=1.0),
            producer(),
        )

    asyncio.run(scenario())  # 撃破で窓が閉じなければ wait_for がタイムアウトする
    assert engine.state.ghost.hp == 0
    assert [m for m, _ in notifier.calls] == [MagicType.ATTACK]


def test_turn_active_ignores_unknown_control_action():
    """未知の制御アクションは無視され、end_turnが来て初めて窓が閉じる。"""
    engine, notifier = _make_engine(player_pos=(1, 1))
    engine.state.player.pos = GridPos(1, 1)

    async def scenario():
        async def producer():
            await engine.motion_queue.put(TurnControlEvent("pause"))  # 未対応アクション
            await engine.motion_queue.put(TurnControlEvent("end_turn"))

        await asyncio.gather(
            asyncio.wait_for(engine._turn_active(), timeout=1.0),
            producer(),
        )

    asyncio.run(scenario())
    assert notifier.calls == []  # 何も発動せず、end_turnで正常終了


def test_turn_active_drops_stale_events_queued_before_window():
    """_drain_queue により、ターン外で溜まった古いイベントは破棄される。
    このドロップ仕様を変える場合はこのテストも更新する。"""
    engine, notifier = _make_engine(player_pos=(1, 1))
    engine.state.player.pos = GridPos(1, 1)
    engine.motion_queue.put_nowait(MotionEvent(MagicType.ATTACK))
    engine.motion_queue.put_nowait(MotionEvent(MagicType.SCAN))

    async def scenario():
        async def producer():
            # drain(冒頭・同期)が古いイベントを捨てた後に end_turn を届ける。
            await asyncio.sleep(0.02)
            await engine.motion_queue.put(TurnControlEvent("end_turn"))

        await asyncio.gather(
            asyncio.wait_for(engine._turn_active(), timeout=1.0),
            producer(),
        )

    asyncio.run(scenario())
    assert notifier.calls == []


# ---- 勝敗 / RESOLVE ----
def test_all_ghosts_defeated_true_when_hp_zero():
    engine, _ = _make_engine()
    engine.state.ghost.hp = 0
    assert engine._all_ghosts_defeated() is True


def test_all_ghosts_defeated_false_when_hp_remains():
    engine, _ = _make_engine()
    engine.state.ghost.hp = 1
    assert engine._all_ghosts_defeated() is False


def test_turn_resolve_game_clear_when_ghost_defeated():
    engine, _ = _make_engine()
    engine.state.ghost.hp = 0
    asyncio.run(engine._turn_resolve())
    assert engine.state.phase == Phase.GAME_OVER
    assert engine.state.result == "clear"


def test_turn_resolve_game_over_on_turn_limit():
    # 規定ターン数に達し、ゴースト未撃破なら敗北で終了。
    engine, _ = _make_engine(max_turns=3)
    engine.state.ghost.hp = 1  # 未撃破
    engine.state.turn = 3
    asyncio.run(engine._turn_resolve())
    assert engine.state.phase == Phase.GAME_OVER
    assert engine.state.result == "over"


def test_turn_resolve_continues_before_turn_limit():
    # ターン数未達・未撃破なら継続(GAME_OVERにならない)。
    engine, _ = _make_engine(max_turns=10)
    engine.state.ghost.hp = 1
    engine.state.turn = 3
    asyncio.run(engine._turn_resolve())
    assert engine.state.phase == Phase.RESOLVE
    assert engine.state.result is None


# ---- ATTACK効果(体力) ----
def test_attack_reduces_hp_when_on_same_cell():
    engine, _ = _make_engine(player_pos=(2, 2), ghost_pos=(2, 2))
    engine.state.player.pos = GridPos(2, 2)
    engine.state.ghost.hp = 3
    engine._cast_magic(MagicType.ATTACK)
    assert engine.state.ghost.hp == 2


def test_attack_does_not_reduce_hp_when_not_on_ghost_cell():
    engine, _ = _make_engine(player_pos=(0, 0), ghost_pos=(2, 2))
    engine.state.player.pos = GridPos(0, 0)
    engine.state.ghost.hp = 3
    engine._cast_magic(MagicType.ATTACK)
    assert engine.state.ghost.hp == 3


def test_attack_no_effect_when_player_position_unknown():
    engine, _ = _make_engine(player_pos=None, ghost_pos=(2, 2))
    engine.state.player.pos = None
    engine.state.ghost.hp = 3
    engine._cast_magic(MagicType.ATTACK)
    assert engine.state.ghost.hp == 3


# ---- 魔法は各ターン各種1回 ----
def test_attack_blocked_second_time_in_same_turn():
    # 同一ターンでのATTACK2回目は不発(hp減らず、通知もしない)。
    engine, notifier = _make_engine(player_pos=(2, 2), ghost_pos=(2, 2))
    engine.state.player.pos = GridPos(2, 2)
    engine.state.ghost.hp = 10
    for _ in range(3):
        engine._cast_magic(MagicType.ATTACK)
    assert engine.state.ghost.hp == 9  # 1回だけ効いた
    assert [m for m, _ in notifier.calls] == [MagicType.ATTACK]


def test_attack_and_scan_each_allowed_once_per_turn():
    # ATTACKとSCANは独立して各1回ずつ使える。2回目以降はそれぞれ不発。
    engine, notifier = _make_engine(player_pos=(2, 2), ghost_pos=(2, 2))
    engine.state.player.pos = GridPos(2, 2)
    engine.state.ghost.hp = 10
    engine._cast_magic(MagicType.ATTACK)  # OK
    engine._cast_magic(MagicType.ATTACK)  # 既に使用 -> 不発
    engine._cast_magic(MagicType.SCAN)    # OK(ATTACKとは独立)
    engine._cast_magic(MagicType.SCAN)    # 既に使用 -> 不発
    assert [m for m, _ in notifier.calls] == [MagicType.ATTACK, MagicType.SCAN]


def test_magic_uses_per_turn_is_configurable():
    # GameSettings で上限を変えると、1ターンに複数回使える(上限超過は不発)。
    settings = GameSettings(attack_uses_per_turn=2, scan_uses_per_turn=3)
    engine, notifier = _make_engine(
        player_pos=(2, 2), ghost_pos=(2, 2),
        magic_uses_per_turn=settings.magic_uses_per_turn(),
    )
    engine.state.player.pos = GridPos(2, 2)
    engine.state.ghost.hp = 10
    for _ in range(4):
        engine._cast_magic(MagicType.ATTACK)  # 2回まで有効
    for _ in range(4):
        engine._cast_magic(MagicType.SCAN)    # 3回まで有効
    assert engine.state.ghost.hp == 8  # ATTACKは2回だけ効いた
    magics = [m for m, _ in notifier.calls]
    assert magics.count(MagicType.ATTACK) == 2
    assert magics.count(MagicType.SCAN) == 3


def test_magic_uses_per_game_limits_total_uses_across_turns():
    # ゲーム全体の上限を使い切ると、ターンが変わっても不発のまま。
    settings = GameSettings(scan_uses_per_game=2)
    engine, notifier = _make_engine(
        player_pos=(2, 2), ghost_pos=(2, 2),
        magic_uses_per_game=settings.magic_uses_per_game(),
    )
    engine.state.ghost.step = lambda state: None
    engine.state.player.pos = GridPos(2, 2)
    for _ in range(4):  # 4ターンで毎ターンSCANを試みる(有効なのは最初の2回)
        engine._cast_magic(MagicType.SCAN)
        asyncio.run(engine._turn_start())
    magics = [m for m, _ in notifier.calls]
    assert magics.count(MagicType.SCAN) == 2


def test_magic_game_limit_applies_within_single_turn():
    # ターン内上限を大きくしても、ゲーム全体の上限が先に尽きればそこで不発。
    settings = GameSettings(attack_uses_per_turn=10, attack_uses_per_game=3)
    engine, notifier = _make_engine(
        player_pos=(2, 2), ghost_pos=(2, 2),
        magic_uses_per_turn=settings.magic_uses_per_turn(),
        magic_uses_per_game=settings.magic_uses_per_game(),
    )
    engine.state.player.pos = GridPos(2, 2)
    engine.state.ghost.hp = 10
    for _ in range(5):
        engine._cast_magic(MagicType.ATTACK)
    assert engine.state.ghost.hp == 7  # 3回だけ効いた
    assert [m for m, _ in notifier.calls].count(MagicType.ATTACK) == 3


def test_magic_usage_resets_next_turn():
    # ターンが変わると使用状況がリセットされ、再び各種1回使える。
    engine, notifier = _make_engine(player_pos=(2, 2), ghost_pos=(2, 2))
    engine.state.ghost.step = lambda state: None  # ゴーストを(2,2)に固定
    engine.state.player.pos = GridPos(2, 2)
    engine.state.ghost.hp = 5
    engine._cast_magic(MagicType.ATTACK)   # turn A: 命中(5->4)
    engine._cast_magic(MagicType.ATTACK)   # turn A: 不発
    asyncio.run(engine._turn_start())      # 次ターン開始 -> リセット(player=(2,2)へ再推定)
    engine._cast_magic(MagicType.ATTACK)   # turn B: また命中(4->3)
    assert engine.state.ghost.hp == 3
    assert [m for m, _ in notifier.calls] == [MagicType.ATTACK, MagicType.ATTACK]


# ---- ウォームアップ(turn1前にRSSIバッファが十分たまるまで待つ) ----
def _buffer_with(beacon_counts):
    """指定ビーコンに指定件数のRSSIサンプルを積んだバッファを作る。"""
    buf = RSSIBuffer(window_sec=100.0, maxlen=50)
    now = time.time()
    for bid, n in beacon_counts.items():
        for _ in range(n):
            buf.add(RSSISample(beacon_id=bid, rssi=-60.0, ts=now))
    return buf


def test_ready_beacon_count_counts_only_beacons_over_threshold():
    buf = _buffer_with({"a": 5, "b": 3, "c": 6})
    engine, _ = _make_engine(warmup_min_samples=5, buffer=buf)
    assert engine._ready_beacon_count() == 2  # a(5), c(6) のみが閾値5以上


def test_warmup_returns_when_enough_beacons_ready():
    # 3ビーコンが各5件たまっていれば min_beacons=3 / min_samples=5 を満たし即返る。
    buf = _buffer_with({"a": 5, "b": 5, "c": 5})
    engine, _ = _make_engine(
        warmup_sec=5.0, warmup_min_samples=5, warmup_min_beacons=3, buffer=buf
    )
    asyncio.run(asyncio.wait_for(engine._warmup(), timeout=1.0))


def test_warmup_times_out_when_samples_insufficient():
    # 各ビーコンのサンプルが閾値未満(2件)なら準備完了せず、warmup_sec後に返る。
    buf = _buffer_with({"a": 2, "b": 2, "c": 2})
    engine, _ = _make_engine(
        warmup_sec=0.2, warmup_min_samples=5, warmup_min_beacons=3, buffer=buf
    )
    asyncio.run(asyncio.wait_for(engine._warmup(), timeout=1.0))


def test_warmup_times_out_when_too_few_beacons():
    # 1ビーコンだけ十分でも、必要ビーコン数(3)に満たなければタイムアウトで返る。
    buf = _buffer_with({"a": 10})
    engine, _ = _make_engine(
        warmup_sec=0.2, warmup_min_samples=5, warmup_min_beacons=3, buffer=buf
    )
    asyncio.run(asyncio.wait_for(engine._warmup(), timeout=1.0))


def test_warmup_skipped_when_zero():
    engine, _ = _make_engine(warmup_sec=0.0)
    asyncio.run(engine._warmup())  # 即返る(待たない)


# ---- run() ループ ----
def test_run_terminates_on_capture():
    engine, notifier = _make_engine(player_pos=(1, 1), ghost_pos=(1, 1))
    engine.state.ghost.step = lambda state: None  # 動かさない -> 必ず捕獲

    async def scenario():
        async def producer():
            # 同一マスで ATTACK して hp を 1->0 にし、end_turn で RESOLVE 捕獲成立。
            await engine.motion_queue.put(MotionEvent(MagicType.ATTACK))
            await engine.motion_queue.put(TurnControlEvent("end_turn"))

        await asyncio.gather(
            asyncio.wait_for(engine.run(), timeout=2.0),
            producer(),
        )

    asyncio.run(scenario())
    assert engine.state.phase == Phase.GAME_OVER
    assert engine.state.result == "clear"
    assert engine.state.turn == 1
    # クリア時はraspiへ "clear" を1回通知する(虹色点滅の合図)。
    assert notifier.results == ["clear"]


def test_run_ends_game_over_on_turn_limit():
    # 撃破できないまま max_turns に達したら敗北で終了する。
    engine, notifier = _make_engine(player_pos=(0, 0), ghost_pos=(2, 2), max_turns=2)
    engine.state.ghost.step = lambda state: None  # 動かさない(別マスのまま)

    async def scenario():
        async def producer():
            # 各ターンのACTIVE窓を end_turn で閉じ続ける。
            for _ in range(5):
                await asyncio.sleep(0.02)
                await engine.motion_queue.put(TurnControlEvent("end_turn"))

        await asyncio.gather(
            asyncio.wait_for(engine.run(), timeout=2.0),
            producer(),
        )

    asyncio.run(scenario())
    assert engine.state.phase == Phase.GAME_OVER
    assert engine.state.result == "over"
    assert engine.state.turn == 2
    # 敗北時はraspiへ "over" を1回通知する(GAME OVER表示の合図)。
    assert notifier.results == ["over"]
