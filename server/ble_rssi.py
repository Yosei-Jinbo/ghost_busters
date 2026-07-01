"""フィンガープリントkNN 位置推定コア（ゲーム実行PC側）。

中継raspi が測定して UDP で送ってくる生RSSI(`rssi_receiver` -> `RSSIBuffer`)を、
PC側の `PositionEstimator` が kNN で推定するためのコア。
**ゲームPCはBLEを自分でスキャンしない**(自分の位置でのRSSIを参照しない)。
位置推定に使う強度は、すべて中継raspi が測定した値。

BLEスキャン・フィンガープリント収集は raspi 側のスタンドアロンツールが担う:
  - collect_phone_uuid_fingerprint.py            … 各グリッドのRSSIを収集し fingerprint CSV を作る
  - realtime_phone_uuid_grid_estimator_ghost.py  … 同等のkNNを raspi 単体で動かす参考実装

ここには上記スクリプトのうち「fingerprint読込・kNN推定」部分だけを原型のまま取り込み、
`RSSIBuffer.snapshot()` を入力にできる薄いアダプタ(`snapshot_to_obs`)を足してある。
"""
from __future__ import annotations

import csv
import math
import uuid
from collections import Counter, defaultdict
from typing import Dict


# ============================================================
# スマホ側ビーコンのUUIDを設定
# collect_phone_uuid_fingerprint.py と同じ設定にする
# (PC側ではビーコン名 BEACONS のみ使う。UUIDは収集スクリプトとの対応確認用)
# ============================================================
TARGETS = {
    "48534442-4C45-4144-80C0-1800FFFFFFFA": "phone1",
    "48534442-4C45-4144-80C0-1800FFFFFFFB": "phone2",
    "48534442-4C45-4144-80C0-1800FFFFFFFC": "phone3",
    "48534442-4C45-4144-80C0-1800FFFFFFFD": "phone4",
    "2F234454-CF6D-4A0F-ADF2-F4911BA9FFA5": "phone5",
    "2F234454-CF6D-4A0F-ADF2-F4911BA9FFA6": "phone6",
    "48534442-4C45-4144-80C0-1800FFFFFFFE": "phone7",
    "48534442-4C45-4144-80C0-1800FFFFFFFF": "phone8",
}


def canonical_uuid(s: str) -> str:
    return str(uuid.UUID(s.strip()))


def normalize_target_key(key: str) -> str:
    parts = key.strip().lower().split(":")

    if len(parts) == 1:
        return canonical_uuid(parts[0])

    if len(parts) == 2:
        return f"{canonical_uuid(parts[0])}:{int(parts[1])}"

    if len(parts) == 3:
        return f"{canonical_uuid(parts[0])}:{int(parts[1])}:{int(parts[2])}"

    raise ValueError(f"Invalid target key: {key}")


TARGETS = {normalize_target_key(k): v for k, v in TARGETS.items()}
BEACONS = list(TARGETS.values())


# ------------------------------------------------------------
# フィンガープリントkNN(realtime_phone_uuid_grid_estimator_ghost と同一ロジック)
# ------------------------------------------------------------
def label_to_grid_number(label):
    """
    'G1' -> 1 のように推定ラベルをグリッド番号へ変換する．
    """
    if label is None:
        return None

    label = str(label).strip()

    if label.startswith("G"):
        label = label[1:]

    try:
        n = int(label)
    except ValueError:
        return None

    if 1 <= n <= 9:
        return n

    return None


def grid_number_to_rc(n, grid_cols):
    """
    1 2 3
    4 5 6
    7 8 9
    という並びとして，番号を(row, col)へ変換する．
    """
    n = int(n)
    idx = n - 1
    return idx // grid_cols, idx % grid_cols


def label_to_grid_index(label):
    if not label.startswith("G"):
        return None

    try:
        return int(label[1:])
    except ValueError:
        return None


def load_fingerprint_samples(path, min_valid_for_loading):
    """
    fingerprint CSV 内の全サンプルを読み込む．

    1行 = 1サンプルとして扱う．
    各行の phone1_rssi, phone2_rssi, ... を使う．

    - valid=1 の値だけ使う
    - -100 などの欠損RSSIは除外する
    - 有効ビーコン数が min_valid_for_loading 未満の行は捨てる
    """
    samples = []
    labels = set()
    missing_threshold = -99.0

    with open(path, newline="") as f:
        reader = csv.DictReader(f)

        row_index = 0
        for row in reader:
            row_index += 1
            label = row["label"].strip()
            vec = {}

            for beacon in BEACONS:
                valid_col = f"{beacon}_valid"
                rssi_col = f"{beacon}_rssi"

                if valid_col in row and str(row[valid_col]).strip() != "1":
                    continue

                if rssi_col not in row:
                    continue

                val_str = str(row[rssi_col]).strip()
                if val_str == "":
                    continue

                try:
                    val = float(val_str)
                except ValueError:
                    continue

                if val <= missing_threshold:
                    continue

                vec[beacon] = val

            if len(vec) < min_valid_for_loading:
                continue

            samples.append({
                "index": row_index,
                "label": label,
                "rssi": vec,
            })
            labels.add(label)

    if not samples:
        raise RuntimeError("No valid fingerprint samples were loaded.")

    return samples, sorted(labels, key=lambda x: label_to_grid_index(x) or 9999)


def rmse_between(obs, sample_vec, min_valid):
    """
    obs と fingerprint sample の共通ビーコンだけでRMSEを計算する．
    共通ビーコン数が min_valid 未満なら None を返す．
    """
    diffs = []

    for beacon in BEACONS:
        if beacon not in obs:
            continue
        if beacon not in sample_vec:
            continue

        diffs.append(obs[beacon] - sample_vec[beacon])

    if len(diffs) < min_valid:
        return None, 0

    rmse = math.sqrt(sum(d * d for d in diffs) / len(diffs))
    return rmse, len(diffs)


def estimate_position_knn(obs, samples, k, min_valid, weighted_vote):
    """
    現在RSSI obs と fingerprint.csv内の全サンプルを比較し，
    RMSEが小さい上位k個のラベル多数決で推定する．
    """
    neighbors = []

    for s in samples:
        rmse, n_common = rmse_between(obs, s["rssi"], min_valid)

        if rmse is None:
            continue

        neighbors.append({
            "rmse": rmse,
            "label": s["label"],
            "n_common": n_common,
            "index": s["index"],
        })

    if not neighbors:
        return None, None, None, [], {}, []

    neighbors.sort(key=lambda x: x["rmse"])

    k_eff = min(k, len(neighbors))
    topk = neighbors[:k_eff]

    if weighted_vote:
        # 距離が近いほど強く投票する
        # rmse=0対策で小さい値を足す
        eps = 1e-6
        score = defaultdict(float)
        count = Counter()

        for n in topk:
            w = 1.0 / (n["rmse"] + eps)
            score[n["label"]] += w
            count[n["label"]] += 1

        # score最大，同点なら平均RMSE最小
        label_stats = []
        for label, sc in score.items():
            rmses = [n["rmse"] for n in topk if n["label"] == label]
            avg_rmse = sum(rmses) / len(rmses)
            label_stats.append((label, sc, count[label], avg_rmse))

        label_stats.sort(key=lambda x: (-x[1], x[3], x[0]))
        pred_label = label_stats[0][0]
        pred_score = label_stats[0][1]

        return pred_label, pred_score, k_eff, neighbors, dict(score), topk

    else:
        # 単純多数決
        count = Counter(n["label"] for n in topk)

        label_stats = []
        for label, cnt in count.items():
            rmses = [n["rmse"] for n in topk if n["label"] == label]
            avg_rmse = sum(rmses) / len(rmses)
            best_rmse = min(rmses)
            label_stats.append((label, cnt, avg_rmse, best_rmse))

        # 票数最大，同点なら平均RMSE最小，さらに同点なら最良RMSE最小
        label_stats.sort(key=lambda x: (-x[1], x[2], x[3], x[0]))

        pred_label = label_stats[0][0]
        pred_votes = label_stats[0][1]

        return pred_label, pred_votes, k_eff, neighbors, dict(count), topk


# ------------------------------------------------------------
# RSSIBuffer 用ヘルパ: 直近スナップショット -> kNN入力(obs)
# ------------------------------------------------------------
def snapshot_to_obs(snapshot: Dict[str, list]) -> Dict[str, float]:
    """RSSIBuffer.snapshot() の各ビーコンのRSSIを平均し、kNN入力 obs を作る。

    バッファには中継raspi が測定した生RSSIだけが入っている。
    バッファは時間窓(RSSI_WINDOW_SEC)で直近サンプルだけを保持しているので、
    その平均がリアルタイム版の移動平均(smooth_n)に相当する。
    """
    obs: Dict[str, float] = {}
    for beacon_id, samples in snapshot.items():
        vals = [s.rssi for s in samples]
        if vals:
            obs[beacon_id] = sum(vals) / len(vals)
    return obs
