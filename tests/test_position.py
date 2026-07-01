"""position.py の単体テスト(RSSIバッファ + フィンガープリントkNN位置推定)。"""
import csv
import time

from domain import GridPos
from position import PositionEstimator, RSSIBuffer, RSSISample


def _estimator(
    grid_w=3,
    grid_h=3,
    fingerprint_path=None,
    k=5,
    min_valid=8,
    weighted_vote=False,
    grid_cols=3,
):
    """本番コードはデフォルト引数を持たないため、テスト側で明示値を束ねる小ヘルパ。"""
    return PositionEstimator(
        grid_w=grid_w,
        grid_h=grid_h,
        fingerprint_path=fingerprint_path,
        k=k,
        min_valid=min_valid,
        weighted_vote=weighted_vote,
        grid_cols=grid_cols,
    )


def _write_fingerprint(path, rows, beacons=("phone1", "phone2")):
    """テスト用の最小 fingerprint CSV を書き出す。

    rows: [(label, {beacon: rssi, ...}), ...]
    load_fingerprint_samples が使うのは label / {beacon}_valid / {beacon}_rssi のみ。
    """
    header = ["label"]
    for b in beacons:
        header += [f"{b}_rssi", f"{b}_valid"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for label, vec in rows:
            row = [label]
            for b in beacons:
                if b in vec:
                    row += [vec[b], 1]
                else:
                    row += [-100.0, 0]
            w.writerow(row)


def test_buffer_groups_samples_by_beacon():
    buf = RSSIBuffer(window_sec=100.0, maxlen=20)
    now = time.time()
    buf.add(RSSISample("phone-1", -60.0, now))
    buf.add(RSSISample("phone-1", -62.0, now))
    buf.add(RSSISample("phone-2", -70.0, now))
    snap = buf.snapshot()
    assert set(snap.keys()) == {"phone-1", "phone-2"}
    assert len(snap["phone-1"]) == 2
    assert len(snap["phone-2"]) == 1


def test_buffer_drops_samples_outside_time_window(monkeypatch):
    buf = RSSIBuffer(window_sec=2.0, maxlen=20)
    buf.add(RSSISample("b", -50.0, ts=100.0))  # 古い(範囲外になる)
    buf.add(RSSISample("b", -55.0, ts=109.0))  # 新しい(範囲内)
    # now=110, window=2s -> 100.0は外、109.0は内
    monkeypatch.setattr("position.time.time", lambda: 110.0)
    snap = buf.snapshot()
    assert len(snap["b"]) == 1
    assert snap["b"][0].rssi == -55.0


def test_buffer_respects_maxlen():
    buf = RSSIBuffer(window_sec=1e9, maxlen=3)
    now = time.time()
    for i in range(10):
        buf.add(RSSISample("b", float(-i), now))
    snap = buf.snapshot()
    assert len(snap["b"]) == 3  # 直近3件のみ保持


def test_estimator_returns_none_on_empty_snapshot():
    est = _estimator()
    assert est.estimate({}) is None


def test_estimator_returns_none_without_fingerprint():
    # fingerprint未読込なら、データがあっても推定しない(None)。
    est = _estimator()
    snap = {"phone1": [RSSISample("phone1", -60.0, time.time())]}
    assert est.estimate(snap) is None


def test_estimator_knn_predicts_nearest_grid(tmp_path):
    # G1 は phone1 が強い、G2 は phone2 が強い、という最小フィンガープリント。
    fp = tmp_path / "fp.csv"
    _write_fingerprint(
        fp,
        rows=[
            ("G1", {"phone1": -50.0, "phone2": -80.0}),
            ("G2", {"phone1": -80.0, "phone2": -50.0}),
        ],
    )
    est = _estimator(fingerprint_path=str(fp), min_valid=2)

    now = time.time()
    # 観測は G1 に近い。
    snap = {
        "phone1": [RSSISample("phone1", -52.0, now)],
        "phone2": [RSSISample("phone2", -78.0, now)],
    }
    # G1 -> 番号1 -> (row=0, col=0) -> GridPos(x=0, y=0)
    assert est.estimate(snap) == GridPos(0, 0)


def test_estimator_averages_window_samples(tmp_path):
    # 同一ビーコンの直近複数サンプルは平均されて obs になる。
    fp = tmp_path / "fp.csv"
    _write_fingerprint(
        fp,
        rows=[
            ("G1", {"phone1": -50.0, "phone2": -80.0}),
            ("G2", {"phone1": -80.0, "phone2": -50.0}),
        ],
    )
    est = _estimator(fingerprint_path=str(fp), min_valid=2)

    now = time.time()
    snap = {
        "phone1": [RSSISample("phone1", -40.0, now), RSSISample("phone1", -60.0, now)],
        "phone2": [RSSISample("phone2", -80.0, now), RSSISample("phone2", -76.0, now)],
    }
    # phone1平均=-50, phone2平均=-78 -> G1 -> GridPos(0, 0)
    assert est.estimate(snap) == GridPos(0, 0)
