"""ゲームエンジン(中央サーバの心臓部)。

ゲーム状態の唯一の所有者(single writer)。すべての状態変更はこのクラス内
(=asyncioループの単一スレッド)でのみ行う。プロデューサ(RSSI受信・モーション
入力)はバッファ/キューに積むだけなので、ゲーム状態にロックは不要。

1ターンの流れ:
  TURN_START : ゴースト移動 + RSSIバッファからユーザ位置を推定
  ACTIVE     : Aボタン(end_turn)が来るまでモーションを受け付け、届くたびに魔法を発動
  RESOLVE    : 勝敗判定など
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

from domain import (
    DEFAULT_SETTINGS,
    GameState,
    MagicStrategy,
    MagicType,
    Phase,
    build_magic_strategies,
)
from motion_input import InputEvent, TurnControlEvent
from position import PositionEstimator, RSSIBuffer
from raspi_notifier import RaspiNotifier


class GameEngine:
    def __init__(
        self,
        state: GameState,
        buffer: RSSIBuffer,
        estimator: PositionEstimator,
        notifier: RaspiNotifier,
        motion_queue: asyncio.Queue[InputEvent],
        max_turns: int,
        warmup_sec: float,
        warmup_min_samples: int,
        warmup_min_beacons: int,
        strategies: Optional[Dict[MagicType, MagicStrategy]] = None,
        magic_uses_per_turn: Optional[Dict[MagicType, int]] = None,
        magic_uses_per_game: Optional[Dict[MagicType, int]] = None,
    ) -> None:
        self.state = state
        self.buffer = buffer
        self.estimator = estimator
        self.notifier = notifier
        self.motion_queue = motion_queue
        self.max_turns = max_turns
        self.warmup_sec = warmup_sec
        # ウォームアップ判定: 時間窓内に warmup_min_samples 件以上たまったビーコンが
        # warmup_min_beacons 個そろったら「準備完了」とみなす。
        self.warmup_min_samples = warmup_min_samples
        self.warmup_min_beacons = warmup_min_beacons
        # 使用する魔法戦略。main が domain の設定(attack_type/scan_type)から
        # 組み立てて注入する。未指定なら既定設定(DEFAULT_SETTINGS)で組み立てる。
        self.strategies = (
            strategies if strategies is not None else build_magic_strategies()
        )
        # 魔法種別ごとの1ターンあたり使用回数上限。main が domain の設定
        # (attack_uses_per_turn/scan_uses_per_turn)から注入する。未指定なら既定設定。
        self.magic_uses_per_turn = (
            magic_uses_per_turn
            if magic_uses_per_turn is not None
            else DEFAULT_SETTINGS.magic_uses_per_turn()
        )
        # 魔法種別ごとのゲーム全体での使用回数上限。同じく main が注入する。
        self.magic_uses_per_game = (
            magic_uses_per_game
            if magic_uses_per_game is not None
            else DEFAULT_SETTINGS.magic_uses_per_game()
        )
        # このターンでの魔法種別ごとの使用回数。ターン開始ごとにリセットし、
        # 上限(magic_uses_per_turn)に達した魔法は不発にする。
        self._used_this_turn: Dict[MagicType, int] = {}
        # ゲーム開始からの魔法種別ごとの累計使用回数。リセットせず、
        # 上限(magic_uses_per_game)を使い切った魔法は以降のターンでも不発にする。
        self._used_total: Dict[MagicType, int] = {}

    async def run(self) -> None:
        await self._warmup()
        while self.state.phase != Phase.GAME_OVER:
            await self._turn_start()
            await self._turn_active()
            await self._turn_resolve()
        if self.state.result == "clear":
            print("[game] GAME CLEAR! 全ゴーストを撃破した")
        else:
            print(f"[game] GAME OVER ({self.max_turns}ターン到達)")
        # 終了演出をraspiへ通知(clear=虹色点滅 / over=GAME OVER表示)。
        self.notifier.notify_result(self.state.result)

    async def _warmup(self) -> None:
        """turn1開始前に、RSSIバッファが十分たまるまで待つ(最大 warmup_sec)。

        起動直後はバッファが空/ビーコン不足で kNN の min_valid を満たせず、turn1 の
        位置が未確定になる。そこで「時間窓内に warmup_min_samples 件以上たまった
        ビーコン」が warmup_min_beacons 個そろうまで待ってから turn1 に入る。
        warmup_sec を過ぎても満たなければそのまま進む(player=None 相当で開始)。
        """
        if self.warmup_sec <= 0:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.warmup_sec
        while loop.time() < deadline:
            ready = self._ready_beacon_count()
            if ready >= self.warmup_min_beacons:
                print(f"[game] RSSIバッファ準備OK (ready={ready}/{self.warmup_min_beacons})。ゲーム開始")
                return
            await asyncio.sleep(0.1)
        print(
            f"[game] ウォームアップ時間内にRSSIが十分たまらず "
            f"(ready={self._ready_beacon_count()}/{self.warmup_min_beacons})。そのまま開始"
        )

    def _ready_beacon_count(self) -> int:
        """時間窓内のサンプルが warmup_min_samples 件以上あるビーコン数を返す。"""
        snapshot = self.buffer.snapshot()
        return sum(
            1 for samples in snapshot.values()
            if len(samples) >= self.warmup_min_samples
        )

    # ---- 各フェーズ ----
    async def _turn_start(self) -> None:
        self.state.turn += 1
        self.state.phase = Phase.TURN_START

        # このターンの魔法使用回数をリセット(上限は magic_uses_per_turn)。
        self._used_this_turn = {}

        # 0) ターン開始ごとにraspiのLEDを一旦リセット(消灯)する。
        self.notifier.notify_reset()

        # 1) ゴースト移動
        self.state.ghost.step(self.state)

        # 2) ユーザ位置推定(RSSIバッファのスナップショットから)
        snapshot = self.buffer.snapshot()
        pos = self.estimator.estimate(snapshot)
        if pos is not None:
            self.state.player.pos = pos
            self.state.player.last_update = time.time()

        print(
            f"[turn {self.state.turn}] ghost={self.state.ghost.pos} "
            f"player={self.state.player.pos}"
        )

        # 3) 推定後・ACTIVE前に、現在のグリッドとユーザ位置を確認用に表示
        self._print_grid()

    def _print_grid(self) -> None:
        """推定したユーザ位置をグリッド上に標準出力へ表示する(ACTIVE前の確認用)。

        グリッドは grid_w(列, x) × grid_h(行, y)。左上が (x=0, y=0)、
        グリッド番号は G(y*grid_w + x + 1)（fingerprint の G1〜 と同じ並び）。
        ユーザの推定セルを [U]、ゴーストのセルを [G]、両者が重なるセルを [*]、
        それ以外を ' . ' で表す（動作確認用。ゴースト位置はデバッグ表示）。
        ヘッダにゴーストのhp、続くステータス行に現在ターン/最大ターン・残りターン数・
        魔法(ATTACK/SCAN)の残り使用回数を表示する。
        """
        p = self.state.player.pos
        g = self.state.ghost.pos
        hp = self.state.ghost.hp
        w, h = self.state.grid_w, self.state.grid_h

        def _label(pos) -> str:
            return f"G{pos.y * w + pos.x + 1}"

        if p is None:
            print(
                f"  [grid {w}x{h}] user=なし(推定未確定) "
                f"ghost={_label(g)} (x={g.x}, y={g.y}) hp={hp}"
            )
        else:
            print(
                f"  [grid {w}x{h}] user={_label(p)} (x={p.x}, y={p.y}) "
                f"ghost={_label(g)} (x={g.x}, y={g.y}) hp={hp}"
            )

        # ターン進行状況と、各魔法の残り使用回数(このターン / ゲーム全体)を表示。
        atk_left = self._uses_left(MagicType.ATTACK)
        scan_left = self._uses_left(MagicType.SCAN)
        atk_game = self._game_uses_left(MagicType.ATTACK)
        scan_game = self._game_uses_left(MagicType.SCAN)
        turns_left = max(0, self.max_turns - self.state.turn)
        print(
            f"  turn {self.state.turn}/{self.max_turns} (残り{turns_left}) | "
            f"ATTACK:残{atk_left}(全体残{atk_game}) / SCAN:残{scan_left}(全体残{scan_game})"
        )

        for y in range(h):
            cells = []
            for x in range(w):
                is_u = p is not None and p.x == x and p.y == y
                is_g = g.x == x and g.y == y
                if is_u and is_g:
                    cells.append("[*]")
                elif is_u:
                    cells.append("[U]")
                elif is_g:
                    cells.append("[G]")
                else:
                    cells.append(" . ")
            print("  " + "".join(cells))

    async def _turn_active(self) -> None:
        """モーションを受け付け、届いた順に魔法を発動する。

        タイムアウトは持たない。TurnControlEvent("end_turn")(joyconのAボタン)を
        受け取るまでACTIVE窓を開いたまま待ち続ける。ただし全ゴーストを撃破したら
        Aボタンを待たずにその場でACTIVE窓を閉じ、クリア判定へ進む。
        """
        self.state.phase = Phase.ACTIVE

        # ターン外で溜まった古いイベントを捨てる(ポリシー: 任意。不要なら削除)。
        self._drain_queue()

        while True:
            evt = await self.motion_queue.get()

            # ターン制御信号: end_turn ならACTIVE窓を閉じてターンを進める。
            if isinstance(evt, TurnControlEvent):
                if evt.action == "end_turn":
                    print("  -> ターン終了 (Aボタン)")
                    break
                continue  # 未知の制御は無視

            self._cast_magic(evt.magic)

            # 全ゴースト撃破で即ターン終了(Aボタンを待たずにクリア判定へ)。
            if self._all_ghosts_defeated():
                print("  -> 全ゴースト撃破! ターン終了")
                break

    async def _turn_resolve(self) -> None:
        self.state.phase = Phase.RESOLVE
        if self._all_ghosts_defeated():
            # 全ゴースト撃破 -> ゲームクリア。
            self.state.result = "clear"
            self.state.phase = Phase.GAME_OVER
        elif self.state.turn >= self.max_turns:
            # 規定ターン数に達しても撃破できず -> ゲームオーバー(敗北)。
            self.state.result = "over"
            self.state.phase = Phase.GAME_OVER

    # ---- 魔法: 検出 -> 効果適用 -> raspi通知 を1か所に統合 ----
    def _uses_left(self, magic: MagicType) -> int:
        """このターンで magic をあと何回使えるか(既定上限は1回)。"""
        limit = self.magic_uses_per_turn.get(magic, 1)
        used = self._used_this_turn.get(magic, 0)
        return max(0, limit - used)

    def _game_uses_left(self, magic: MagicType) -> int:
        """ゲーム全体で magic をあと何回使えるか(既定上限は10回)。"""
        limit = self.magic_uses_per_game.get(magic, 10)
        used = self._used_total.get(magic, 0)
        return max(0, limit - used)

    def _cast_magic(self, magic: MagicType) -> None:
        # 各魔法(MagicType)は1ターンにつき magic_uses_per_turn 回まで(既定1回)、
        # かつゲーム全体で magic_uses_per_game 回まで(既定10回)。
        # 制限は MagicType 単位なので、別戦略を導入しても種別ごとに数える。
        if self._game_uses_left(magic) <= 0:
            limit = self.magic_uses_per_game.get(magic, 10)
            print(f"  -> {magic.name} はゲーム中の上限({limit}回)を使い切った(不発)")
            return
        if self._uses_left(magic) <= 0:
            limit = self.magic_uses_per_turn.get(magic, 1)
            print(f"  -> {magic.name} はこのターンの上限({limit}回)に達している(不発)")
            return
        self._used_this_turn[magic] = self._used_this_turn.get(magic, 0) + 1
        self._used_total[magic] = self._used_total.get(magic, 0) + 1

        # 魔法の効果は domain 側の戦略に委譲する(engineは回数管理と送信だけ担当)。
        result = self.strategies[magic].apply(self.state)
        print(f"  -> {result.message}")

        # 登録raspiへ通知(光らせる等)。光り方(light)は戦略が決めた state をそのまま送る。
        # distance も後方互換のため付ける(raspiのフォールバック用)。
        payload: dict = {"light": result.light.value}
        if result.distance is not None:
            payload["distance"] = result.distance
        self.notifier.notify_magic(magic, payload=payload)

    # ---- 勝敗 ----
    def _all_ghosts_defeated(self) -> bool:
        # 全ゴーストの体力が尽きたか。現状はゴースト1体。
        # 将来複数体にする場合はここを list 走査に拡張する。
        return self.state.ghost.hp <= 0

    def _drain_queue(self) -> None:
        while not self.motion_queue.empty():
            self.motion_queue.get_nowait()
