"""ゲームのドメインモデル(状態・エンティティの定義)。

ここはネットワークやデバイスに依存しない純粋なゲームの「データ」。
ゴーストは現状 位置のみだが、将来の拡張に備えてクラスで保持する。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class Phase(Enum):
    """1ターンのライフサイクル。"""
    TURN_START = auto()  # ゴースト移動 + ユーザ位置推定
    ACTIVE = auto()      # モーションを受け付けて魔法を発動する区間
    RESOLVE = auto()     # ターン終了処理(勝敗判定など)
    GAME_OVER = auto()


class MagicType(Enum):
    """発動できる魔法の種類(2種)。名前は仮。"""
    ATTACK = auto()  # 例: 現在地周辺を攻撃してゴーストを捕まえる
    SCAN = auto()    # 例: ゴーストまでの距離/方向を探る


@dataclass
class GridPos:
    x: int
    y: int

    def manhattan(self, other: GridPos) -> int:
        return abs(self.x - other.x) + abs(self.y - other.y)


@dataclass
class Ghost:
    """ゴースト。現状は位置のみだが、将来の拡張用にクラスとして保持する。
    HP・種類・行動パターンなどをこのクラスに足していける。
    """
    pos: GridPos
    ghost_id: int = 0
    hp: int = 1
    # 行動ロジックは step() の差し替え、または Strategy への切り出しで拡張可能。

    def step(self, state: GameState) -> None:
        """ターン開始時の移動。スケルトン: グリッド内ランダムウォーク。"""
        dx, dy = random.choice([(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)])
        self.pos = GridPos(
            max(0, min(state.grid_w - 1, self.pos.x + dx)),
            max(0, min(state.grid_h - 1, self.pos.y + dy)),
        )


@dataclass
class Player:
    """ユーザ。位置推定の結果が入る。"""
    pos: Optional[GridPos] = None
    last_update: float = 0.0


@dataclass
class GameState:
    """ゲーム全体の状態。GameEngine だけがこれを書き換える(single writer)。"""
    grid_w: int
    grid_h: int
    ghost: Ghost
    player: Player = field(default_factory=Player)
    turn: int = 0
    phase: Phase = Phase.TURN_START
    result: Optional[str] = None  # 終了理由: "clear"(勝利) / "over"(敗北) / None(進行中)
