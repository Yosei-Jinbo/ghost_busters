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
class GameSettings:
    """ゲーム全体のルール設定。各エンティティ設定を束ねる。

    ATTACK/SCAN の1ターンあたりの使用回数は attack_uses_per_turn /
    scan_uses_per_turn、ゲーム全体での使用回数は attack_uses_per_game /
    scan_uses_per_game で設定する(制限の実施は engine が担当)。
    ゴーストの初期体力は各ゴーストクラス側の既定値を使う(base/slow: hp=1, fly: hp=2)。
    ghost_type / attack_type / scan_type は各レジストリの登録名で、
    どの実装を使うかをここで選ぶ(main が create_ghost / build_magic_strategies で解決)。
    """
    max_turns: int = 10             # このターン数で撃破できなければ敗北
    ghost_type: str = "base"        # GHOST_REGISTRY の登録名 ("base" / "slow" / "fly")
    attack_type: str = "base"       # MAGIC_REGISTRY[ATTACK] の登録名
    scan_type: str = "base"         # MAGIC_REGISTRY[SCAN] の登録名
    attack_uses_per_turn: int = 1   # ATTACK を1ターンに使える回数
    scan_uses_per_turn: int = 1     # SCAN を1ターンに使える回数
    attack_uses_per_game: int = 10  # ATTACK をゲーム中に使える総回数
    scan_uses_per_game: int = 10    # SCAN をゲーム中に使える総回数

    def magic_uses_per_turn(self) -> Dict[MagicType, int]:
        """魔法種別ごとの1ターンあたり使用回数(engineへの注入用)。"""
        return {
            MagicType.ATTACK: self.attack_uses_per_turn,
            MagicType.SCAN: self.scan_uses_per_turn,
        }

    def magic_uses_per_game(self) -> Dict[MagicType, int]:
        """魔法種別ごとのゲーム全体での使用回数上限(engineへの注入用)。"""
        return {
            MagicType.ATTACK: self.attack_uses_per_game,
            MagicType.SCAN: self.scan_uses_per_game,
        }


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
    """ゴーストの基本実装。"""
    pos: GridPos
    ghost_id: int = 0
    hp: int = 1
    # 待機ターン数（0を指定すると毎ターン動く）
    wait_turns: int = 0

    def step(self, state: GameState) -> None:
        """ターン開始時の移動。待機ターン経過後に移動する。"""
        
        # 行動の周期は「待機ターン数 + 1（移動するターン）」
        cycle = self.wait_turns + 1
        
        # 現在のターンが行動周期でない場合は、移動処理をスキップして終了
        if (state.turn + 1) % cycle != 0:
            return

        excepted_step_list = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        valid_step_list = []
        for (dx, dy) in excepted_step_list:
            if state.grid_w - 1 < dx + self.pos.x or dx + self.pos.x < 0:
                continue
            if state.grid_h - 1 < dy + self.pos.y or dy + self.pos.y < 0:
                continue
            valid_step_list.append((dx, dy))
            
        # 💡 フェイルセーフ：四方を完全に壁に囲まれていて動ける場所がない場合のクラッシュ防止
        if not valid_step_list:
            return

        dx, dy = random.choice(valid_step_list)
        self.pos = GridPos(
            max(0, min(state.grid_w - 1, self.pos.x + dx)),
            max(0, min(state.grid_h - 1, self.pos.y + dy)),
        )


@register_ghost("slow")
@dataclass
class SlowGhost(BaseGhost):
    """3ターンに1回だけ移動する遅いゴースト(2ターン待機 + 1ターン移動)。"""
    wait_turns: int = 2


@register_ghost("fly")
@dataclass
class FlyGhost(BaseGhost):
    """同じ行・同じ列の任意のマスへ等確率で移動する、体力2のゴースト。"""
    hp: int = 2

    def step(self, state: GameState) -> None:
        """ターン開始時の移動。現在地と同列・同行のマス(現在地を除く)から等確率で選ぶ。"""
        cycle = self.wait_turns + 1
        if (state.turn + 1) % cycle != 0:
            return

        candidates = [
            GridPos(x, self.pos.y) for x in range(state.grid_w) if x != self.pos.x
        ] + [
            GridPos(self.pos.x, y) for y in range(state.grid_h) if y != self.pos.y
        ] + [GridPos(self.pos.x, self.pos.y)]

        # 1x1グリッドなど移動先がない場合は何もしない
        if not candidates:
            return

        self.pos = random.choice(candidates)


def create_ghost(settings: GameSettings, pos: GridPos) -> BaseGhost:
    """settings.ghost_type で選ばれたゴーストを生成する(main が使う入口)。"""
    cls = GHOST_REGISTRY.get(settings.ghost_type)
    if cls is None:
        raise ValueError(
            f"未登録のゴースト種別: {settings.ghost_type!r} "
            f"(登録済み: {sorted(GHOST_REGISTRY)})"
        )
    return cls(pos=pos)


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
# MagicResult を見て行う。現在の実装は BaseAttack / BaseScan("base")として登録済み。
# 新しい戦略は MagicStrategy を継承し register_magic(MagicType, 名前) で登録し、
# GameSettings.attack_type / scan_type にその名前を指定すると差し替わる。


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


# 魔法種別 -> {登録名 -> 戦略クラス} のレジストリ。register_magic で登録し、
# main が build_magic_strategies(settings) で使用実装を選んで engine に注入する。
MAGIC_REGISTRY: Dict[MagicType, Dict[str, Type[MagicStrategy]]] = {
    m: {} for m in MagicType
}


def register_magic(
    magic: MagicType, name: str,
) -> Callable[[Type[MagicStrategy]], Type[MagicStrategy]]:
    """魔法の戦略クラスを (MagicType, 登録名) で登録するデコレータ。"""
    def decorator(cls: Type[MagicStrategy]) -> Type[MagicStrategy]:
        MAGIC_REGISTRY[magic][name] = cls
        return cls
    return decorator


def _resolve_magic(magic: MagicType, name: str) -> Type[MagicStrategy]:
    """レジストリから登録名で戦略クラスを引く。未登録なら ValueError。"""
    cls = MAGIC_REGISTRY[magic].get(name)
    if cls is None:
        raise ValueError(
            f"未登録の{magic.name}戦略: {name!r} "
            f"(登録済み: {sorted(MAGIC_REGISTRY[magic])})"
        )
    return cls


def build_magic_strategies(
    settings: GameSettings = DEFAULT_SETTINGS,
) -> Dict[MagicType, MagicStrategy]:
    """settings の attack_type / scan_type で選んだ戦略インスタンスを組み立てる。

    生成規約: ATTACK系/SCAN系ともコンストラクタは引数なし
    (調整値が必要になったら settings を追加して揃える)。
    """
    attack_cls = _resolve_magic(MagicType.ATTACK, settings.attack_type)
    scan_cls = _resolve_magic(MagicType.SCAN, settings.scan_type)
    return {
        MagicType.ATTACK: attack_cls(),
        MagicType.SCAN: scan_cls(),
    }


@register_magic(MagicType.ATTACK, "base")
class BaseAttack(MagicStrategy):
    """ATTACKの基本実装。ヒット条件: プレイヤーの推定セルがゴーストと同一。
    ヒット時の効果: ゴーストの体力を1減らす。光り方: 捕獲を表す RAINBOW。
    外れたときは現在の近さを示す距離ベースの光にする。
    """

    def apply(self, state: GameState) -> MagicResult:
        p, ghost = state.player.pos, state.ghost
        dist = state.distance()
        hit = p is not None and p == ghost.pos
        if hit:
            ghost.hp -= 1
            return MagicResult(
                MagicType.ATTACK, hit=True, distance=dist, light=LightState.RAINBOW,
                message=f"cast ATTACK at {p} (命中! ghost hp={ghost.hp})",
            )
        return MagicResult(
            MagicType.ATTACK, hit=False, distance=dist, light=light_for_distance(dist),
            message=f"cast ATTACK at {p} (同一マスにゴーストなし: 効果なし)",
        )


@register_magic(MagicType.SCAN, "base")
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
