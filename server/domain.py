"""ゲームのドメインモデル(状態・エンティティの定義)。

ここはネットワークやデバイスに依存しない純粋なゲームの「データ」。
ゴーストは現状 位置のみだが、将来の拡張に備えてクラスで保持する。
"""
from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, Optional, Type


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


# ---- ゲーム設定(アプリケーションとしての調整値) ----
# 実行基盤(ネットワーク/RSSI/kNN/joycon/グリッド)の設定は server/config.py に置く。
# ゲームのルールと各エンティティ(ghost/attack/scan)の調整値はここに集約する。
# すべて frozen dataclass で不変。main がここから値を取り出して注入する。


@dataclass(frozen=True)
class AttackSettings:
    """BaseAttack の調整値。"""
    damage: int = 1             # 命中時に減らす体力(BaseAttackが使用)


@dataclass(frozen=True)
class GameSettings:
    """ゲーム全体のルール設定。各エンティティ設定を束ねる。

    ATTACK/SCAN は各ターンにつき各種1回のみ(回数上限の設定は持たず、engineが制御)。
    ゴーストの初期体力は BaseGhost 側の既定値(hp=1)を使う。
    """
    max_turns: int = 10         # このターン数で撃破できなければ敗北
    attack: AttackSettings = field(default_factory=AttackSettings)


# 既定のゲーム設定。main はここから各値を取り出して各アダプタへ注入する。
DEFAULT_SETTINGS = GameSettings()


@dataclass
class GridPos:
    x: int
    y: int

    def manhattan(self, other: GridPos) -> int:
        return abs(self.x - other.x) + abs(self.y - other.y)


# ---- ゴーストのレジストリ ----
# ゴースト種別を名前で登録し、将来の亜種(速い/固いゴースト等)を差し替え可能にする。
# 現在の実装は BaseGhost として "base" に登録しておく。
GHOST_REGISTRY: Dict[str, Type["BaseGhost"]] = {}


def register_ghost(name: str) -> Callable[[Type["BaseGhost"]], Type["BaseGhost"]]:
    """ゴースト種別を名前で登録するデコレータ。"""
    def decorator(cls: Type["BaseGhost"]) -> Type["BaseGhost"]:
        GHOST_REGISTRY[name] = cls
        return cls
    return decorator


@register_ghost("base")
@dataclass
class BaseGhost:
    """ゴーストの基本実装(現状=グリッド内ランダムウォーク)。

    将来の亜種はこれを継承し register_ghost で別名登録する。位置のみを持つが、
    HP・種類・行動パターンなどをこのクラス系統に足していける。
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
    ghost: BaseGhost
    player: Player = field(default_factory=Player)
    turn: int = 0
    phase: Phase = Phase.TURN_START
    result: Optional[str] = None  # 終了理由: "clear"(勝利) / "over"(敗北) / None(進行中)

    def distance(self) -> Optional[int]:
        """プレイヤーの推定セルとゴーストのマンハッタン距離。未確定なら None。"""
        p, g = self.player.pos, self.ghost.pos
        return None if p is None else p.manhattan(g)


# ---- 光り方(raspi側SenseHatの発光状態) ----
# 「ゴーストへの近さ -> 光り方」の判定を domain 側の唯一の真実として持つ。
# raspi は届いた state 名をそのまま描画するだけ(距離->光の解釈はしない)。


class LightState(str, Enum):
    """raspiに送る発光状態名。値は ghost_sensehat の描画キーと一致させる。"""
    RAINBOW = "rainbow"  # 同一セル(最接近/捕獲)
    RED = "red"          # 距離1
    GREEN = "green"      # 距離2
    BLUE = "blue"        # 距離3
    OFF = "off"          # 距離>=4 / 未確定


# 距離 -> 発光状態。近いほど派手。ここに無い距離(>=4)は OFF に落とす。
_DISTANCE_LIGHT: Dict[int, LightState] = {
    0: LightState.RAINBOW,
    1: LightState.RED,
    2: LightState.GREEN,
    3: LightState.BLUE,
}


def light_for_distance(distance: Optional[int]) -> LightState:
    """ゴーストとの距離に応じた発光状態を返す(純関数)。未確定/遠方は OFF。"""
    if distance is None:
        return LightState.OFF
    return _DISTANCE_LIGHT.get(distance, LightState.OFF)


# ---- 魔法の戦略(Strategy) ----
# 魔法1種 = 1戦略。1つのストラテジが「何をヒットとするか・ヒット時の効果・
# どう光らせるか」までを閉じて持ち、結果を純粋データ(MagicResult)で返す。
# raspi送信(UDP)は含めない: domain はデバイス非依存を保ち、送信は engine 側が
# MagicResult を見て行う。現在の実装は BaseAttack / BaseScan として登録済み。
# 新しい魔法は MagicStrategy を継承し register_magic で登録するだけ。


@dataclass(frozen=True)
class MagicResult:
    """魔法発動の結果(純粋データ)。効果の可否と raspi の光り方まで含む。"""
    magic: MagicType
    hit: bool = False                       # その魔法の「ヒット」条件を満たしたか
    distance: Optional[int] = None          # ゴーストとの距離(未確定なら None)
    light: LightState = LightState.OFF      # raspiに指示する発光状態
    message: str = ""                       # 標準出力ログ用の説明


class MagicStrategy(ABC):
    """魔法1種の戦略。hit判定・効果・光り方を1か所に閉じ、結果を純粋データで返す。"""

    @abstractmethod
    def apply(self, state: GameState) -> MagicResult:
        ...


# 魔法種別 -> 戦略インスタンスのレジストリ。register_magic で登録し、engine はここを引く。
MAGIC_STRATEGIES: Dict[MagicType, MagicStrategy] = {}


def register_magic(
    magic: MagicType,
) -> Callable[[Type[MagicStrategy]], Type[MagicStrategy]]:
    """魔法の戦略を MagicType に紐付けて登録するデコレータ(インスタンスを1つ登録)。"""
    def decorator(cls: Type[MagicStrategy]) -> Type[MagicStrategy]:
        MAGIC_STRATEGIES[magic] = cls()
        return cls
    return decorator


@register_magic(MagicType.ATTACK)
class BaseAttack(MagicStrategy):
    """ATTACKの基本実装。ヒット条件: プレイヤーの推定セルがゴーストと同一。
    ヒット時の効果: ゴーストの体力を settings.damage 減らす。光り方: 捕獲を表す RAINBOW。
    外れたときは現在の近さを示す距離ベースの光にする。
    """

    def __init__(self, settings: AttackSettings = DEFAULT_SETTINGS.attack) -> None:
        self.settings = settings

    def apply(self, state: GameState) -> MagicResult:
        p, ghost = state.player.pos, state.ghost
        dist = state.distance()
        hit = p is not None and p == ghost.pos
        if hit:
            ghost.hp -= self.settings.damage
            return MagicResult(
                MagicType.ATTACK, hit=True, distance=dist, light=LightState.RAINBOW,
                message=f"cast ATTACK at {p} (命中! ghost hp={ghost.hp})",
            )
        return MagicResult(
            MagicType.ATTACK, hit=False, distance=dist, light=light_for_distance(dist),
            message=f"cast ATTACK at {p} (同一マスにゴーストなし: 効果なし)",
        )


@register_magic(MagicType.SCAN)
class BaseScan(MagicStrategy):
    """SCANの基本実装。ヒット条件: ゴーストと同一セル(距離0=発見)。
    効果: 状態は変えない(探索のみ)。光り方: ゴーストへの近さを距離ベースの光で示す。
    """

    def apply(self, state: GameState) -> MagicResult:
        dist = state.distance()
        return MagicResult(
            MagicType.SCAN, hit=(dist == 0), distance=dist, light=light_for_distance(dist),
            message=f"cast SCAN (dist={dist}, light={light_for_distance(dist).value})",
        )
