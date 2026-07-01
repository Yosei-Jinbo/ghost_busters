"""RSSIのバッファリングと位置推定。

- RSSIBuffer : ビーコンごとに直近のRSSIを時間窓で保持(実装済み)。
- PositionEstimator : スナップショット -> グリッド位置(フィンガープリントkNN, 実装済み)。

受信(プロデューサ)と位置推定(コンシューマ)が別スレッドから触れるよう
バッファは Lock で保護してある。asyncioループ内だけで完結させるなら
ロックは無くてもよい。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional

import ble_rssi
from domain import GridPos

logger = logging.getLogger(__name__)


@dataclass
class RSSISample:
    beacon_id: str
    rssi: float
    ts: float


class RSSIBuffer:
    """ビーコン(スマホ等)ごとにRSSIを時間窓でバッファリングする。"""

    def __init__(self, window_sec: float, maxlen: int) -> None:
        self._window = window_sec
        self._buf: Dict[str, Deque[RSSISample]] = defaultdict(
            lambda: deque(maxlen=maxlen)
        )
        self._lock = threading.Lock()

    def add(self, sample: RSSISample) -> None:
        with self._lock:
            self._buf[sample.beacon_id].append(sample)

    def snapshot(self) -> Dict[str, List[RSSISample]]:
        """直近 window_sec 以内のサンプルをビーコン別に返す。"""
        now = time.time()
        with self._lock:
            return {
                bid: [s for s in samples if now - s.ts <= self._window]
                for bid, samples in self._buf.items()
            }


class PositionEstimator:
    """RSSIスナップショット -> グリッド位置 を返す。

    フィンガープリントkNN(`ble_rssi`)で位置推定する。直近バッファの
    スナップショットをビーコン別の平均RSSI(obs)へ畳み、fingerprint CSVの
    全サンプルとRMSE比較して上位kラベルの多数決でグリッドを決める。

    fingerprint CSVが無い/読めない場合は推定を行わず None を返す
    (ゲーム本体は player=None のまま進行する)。
    """

    def __init__(
        self,
        grid_w: int,
        grid_h: int,
        fingerprint_path: Optional[str],
        k: int,
        min_valid: int,
        weighted_vote: bool,
        grid_cols: int,
    ) -> None:
        self.grid_w = grid_w
        self.grid_h = grid_h
        self.k = max(1, k)
        self.min_valid = min_valid
        self.weighted_vote = weighted_vote
        self.grid_cols = grid_cols

        self.samples: Optional[List[dict]] = None
        self.labels: List[str] = []

        if fingerprint_path and os.path.exists(fingerprint_path):
            try:
                self.samples, self.labels = ble_rssi.load_fingerprint_samples(
                    fingerprint_path, min_valid_for_loading=1
                )
                logger.info(
                    "Loaded %d fingerprint samples (labels=%s) from %s",
                    len(self.samples), self.labels, fingerprint_path,
                )
            except (OSError, RuntimeError, KeyError) as e:
                logger.warning("Failed to load fingerprint %s: %s", fingerprint_path, e)
                self.samples = None
        elif fingerprint_path:
            logger.warning("Fingerprint file not found: %s", fingerprint_path)

    def estimate(self, snapshot: Dict[str, List[RSSISample]]) -> Optional[GridPos]:
        if not snapshot or not self.samples:
            return None

        obs = ble_rssi.snapshot_to_obs(snapshot)
        if not obs:
            return None

        pred_label, *_ = ble_rssi.estimate_position_knn(
            obs=obs,
            samples=self.samples,
            k=self.k,
            min_valid=self.min_valid,
            weighted_vote=self.weighted_vote,
        )
        if pred_label is None:
            return None

        return self._label_to_gridpos(pred_label)

    def _label_to_gridpos(self, label: str) -> Optional[GridPos]:
        """kNNラベル 'Gn' を GridPos(x=col, y=row) に変換する。"""
        n = ble_rssi.label_to_grid_number(label)
        if n is None:
            return None
        row, col = ble_rssi.grid_number_to_rc(n, self.grid_cols)
        return GridPos(col, row)
