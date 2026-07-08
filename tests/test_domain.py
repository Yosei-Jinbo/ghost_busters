"""domain.py の単体テスト。"""
import random

from domain import (
    DEFAULT_SETTINGS,
    GHOST_REGISTRY,
    MAGIC_STRATEGIES,
    AttackSettings,
    BaseAttack,
    BaseGhost,
    BaseScan,
    GameState,
    GridPos,
    LightState,
    MagicType,
    Phase,
    Player,
    light_for_distance,
)


def test_gridpos_manhattan():
    assert GridPos(0, 0).manhattan(GridPos(3, 4)) == 7
    assert GridPos(2, 2).manhattan(GridPos(2, 2)) == 0
    assert GridPos(1, 5).manhattan(GridPos(4, 1)) == 7  # |1-4| + |5-1|


def _make_state(w=5, h=5, gx=2, gy=2):
    return GameState(grid_w=w, grid_h=h, ghost=BaseGhost(pos=GridPos(gx, gy)))


def test_ghost_step_stays_in_bounds():
    """移動を何度繰り返してもグリッドの外には出ない(端のクランプ)。"""
    random.seed(0)
    state = _make_state(w=3, h=3, gx=1, gy=1)
    for _ in range(2000):
        state.ghost.step(state)
        assert 0 <= state.ghost.pos.x < state.grid_w
        assert 0 <= state.ghost.pos.y < state.grid_h


def test_ghost_step_moves_at_most_one_cell():
    """1ステップの移動はマンハッタン距離1以内(上下左右 or 停止)。"""
    random.seed(1)
    state = _make_state(w=10, h=10, gx=5, gy=5)
    for _ in range(500):
        before = GridPos(state.ghost.pos.x, state.ghost.pos.y)
        state.ghost.step(state)
        assert before.manhattan(state.ghost.pos) <= 1


def test_ghost_extensible_fields_have_defaults():
    g = BaseGhost(pos=GridPos(0, 0))
    assert g.hp == 1
    assert g.ghost_id == 0


def test_gamestate_defaults():
    state = _make_state()
    assert state.turn == 0
    assert state.phase == Phase.TURN_START
    assert state.player.pos is None


# ---- 距離 -> 光り方 ----
def test_gamestate_distance():
    state = _make_state(gx=0, gy=0)
    state.player.pos = GridPos(0, 3)
    assert state.distance() == 3
    state.player.pos = None
    assert state.distance() is None  # 位置未確定なら None


def test_light_for_distance_mapping():
    assert light_for_distance(0) == LightState.RAINBOW
    assert light_for_distance(1) == LightState.RED
    assert light_for_distance(2) == LightState.GREEN
    assert light_for_distance(3) == LightState.BLUE
    assert light_for_distance(4) == LightState.OFF
    assert light_for_distance(99) == LightState.OFF
    assert light_for_distance(None) == LightState.OFF


def _state_with_player(gx, gy, px, py, hp=3):
    state = GameState(grid_w=5, grid_h=5, ghost=BaseGhost(pos=GridPos(gx, gy), hp=hp))
    state.player = Player(pos=GridPos(px, py))
    return state


# ---- ATTACK戦略: ヒット判定・効果・光り方 ----
def test_attack_hit_reduces_hp_and_lights_rainbow():
    state = _state_with_player(gx=2, gy=2, px=2, py=2, hp=3)
    result = BaseAttack().apply(state)
    assert result.hit is True
    assert state.ghost.hp == 2               # ヒットで体力-1
    assert result.light == LightState.RAINBOW  # 捕獲を表す虹色


def test_attack_miss_keeps_hp_and_lights_by_distance():
    state = _state_with_player(gx=2, gy=2, px=0, py=1, hp=3)  # distance = 3
    result = BaseAttack().apply(state)
    assert result.hit is False
    assert state.ghost.hp == 3               # 外れれば体力は減らない
    assert result.light == LightState.BLUE   # distance=3 -> blue


def test_attack_without_position_lights_off():
    state = _state_with_player(gx=2, gy=2, px=0, py=0)
    state.player.pos = None
    result = BaseAttack().apply(state)
    assert result.hit is False
    assert result.distance is None
    assert result.light == LightState.OFF


# ---- SCAN戦略: 状態は変えず、近さを光で示す ----
def test_scan_does_not_change_hp_and_lights_by_distance():
    state = _state_with_player(gx=2, gy=2, px=2, py=1, hp=3)  # distance = 1
    result = BaseScan().apply(state)
    assert state.ghost.hp == 3               # SCANは状態を変えない
    assert result.magic == MagicType.SCAN
    assert result.hit is False               # 同一セルでないので発見(hit)ではない
    assert result.light == LightState.RED    # distance=1 -> red


def test_scan_on_same_cell_is_hit_and_rainbow():
    state = _state_with_player(gx=2, gy=2, px=2, py=2)
    result = BaseScan().apply(state)
    assert result.hit is True                # 同一セル=発見
    assert result.light == LightState.RAINBOW


# ---- レジストリ登録(現在の実装が Base として登録済み) ----
def test_base_ghost_registered():
    assert GHOST_REGISTRY["base"] is BaseGhost


def test_base_magic_strategies_registered():
    # 各 MagicType に対応する戦略インスタンスが登録されている。
    assert isinstance(MAGIC_STRATEGIES[MagicType.ATTACK], BaseAttack)
    assert isinstance(MAGIC_STRATEGIES[MagicType.SCAN], BaseScan)


# ---- ゲーム設定(domain.py に集約) ----
def test_default_settings_values():
    assert DEFAULT_SETTINGS.max_turns == 10
    assert DEFAULT_SETTINGS.attack.damage == 1


def test_attack_uses_configured_damage():
    # AttackSettings.damage を変えると命中時の体力減少量が変わる。
    state = _state_with_player(gx=2, gy=2, px=2, py=2, hp=5)
    result = BaseAttack(AttackSettings(damage=3)).apply(state)
    assert result.hit is True
    assert state.ghost.hp == 2  # 5 - 3
