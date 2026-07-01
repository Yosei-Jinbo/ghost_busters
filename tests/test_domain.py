"""domain.py の単体テスト。"""
import random

from domain import GameState, Ghost, GridPos, Phase


def test_gridpos_manhattan():
    assert GridPos(0, 0).manhattan(GridPos(3, 4)) == 7
    assert GridPos(2, 2).manhattan(GridPos(2, 2)) == 0
    assert GridPos(1, 5).manhattan(GridPos(4, 1)) == 7  # |1-4| + |5-1|


def _make_state(w=5, h=5, gx=2, gy=2):
    return GameState(grid_w=w, grid_h=h, ghost=Ghost(pos=GridPos(gx, gy)))


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
    g = Ghost(pos=GridPos(0, 0))
    assert g.hp == 1
    assert g.ghost_id == 0


def test_gamestate_defaults():
    state = _make_state()
    assert state.turn == 0
    assert state.phase == Phase.TURN_START
    assert state.player.pos is None
