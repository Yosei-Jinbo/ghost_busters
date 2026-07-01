import argparse
import asyncio
import csv
import json
import math
import socket
import time
import uuid
from collections import Counter, defaultdict, deque
from bleak import BleakScanner

try:
    from sense_hat import SenseHat
except ImportError:
    SenseHat = None


# ============================================================
# スマホ側ビーコンのUUIDを設定
# collect_phone_uuid_fingerprint.py と同じ設定にする
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


def parse_ibeacon(manufacturer_data):
    """
    Apple iBeacon:
      Company ID: 0x004C
      Data: 0x02 0x15 UUID(16B) major(2B) minor(2B) tx_power(1B)
    """
    results = []

    for company_id, data in (manufacturer_data or {}).items():
        if company_id != 0x004C:
            continue

        if len(data) < 23:
            continue

        if data[0] != 0x02 or data[1] != 0x15:
            continue

        beacon_uuid = str(uuid.UUID(bytes=bytes(data[2:18])))
        major = int.from_bytes(data[18:20], byteorder="big")
        minor = int.from_bytes(data[20:22], byteorder="big")
        tx_power = int.from_bytes(data[22:23], byteorder="big", signed=True)

        results.append({
            "source": "ibeacon",
            "uuid": beacon_uuid,
            "major": major,
            "minor": minor,
            "tx_power": tx_power,
        })

    return results


def extract_uuid_candidates(device, adv_data):
    candidates = []

    for su in getattr(adv_data, "service_uuids", []) or []:
        try:
            candidates.append({
                "source": "service",
                "uuid": canonical_uuid(su),
                "major": None,
                "minor": None,
                "tx_power": None,
            })
        except ValueError:
            continue

    candidates.extend(parse_ibeacon(getattr(adv_data, "manufacturer_data", {}) or {}))

    return candidates


def find_target(device, adv_data):
    candidates = extract_uuid_candidates(device, adv_data)

    for c in candidates:
        u = c["uuid"]
        major = c["major"]
        minor = c["minor"]

        keys = []

        if major is not None and minor is not None:
            keys.append(f"{u}:{major}:{minor}")

        if major is not None:
            keys.append(f"{u}:{major}")

        keys.append(u)

        for key in keys:
            if key in TARGETS:
                return TARGETS[key], key, c

    return None, None, None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Realtime BLE grid estimation using kNN over all fingerprint samples."
    )

    parser.add_argument(
        "--fingerprint",
        required=True,
        help="Fingerprint CSV file, e.g., phone_fingerprint.csv"
    )

    parser.add_argument(
        "--sample-interval",
        type=float,
        default=0.2,
        help="Estimation interval in seconds. Default: 0.2"
    )

    parser.add_argument(
        "--smooth-n",
        type=int,
        default=1,
        help="Moving average window size for realtime RSSI. 1 means raw RSSI. Default: 1"
    )

    parser.add_argument(
        "--k",
        type=int,
        default=5,
        help="Number of nearest fingerprint samples used for majority voting. Default: 5"
    )

    parser.add_argument(
        "--weighted-vote",
        action="store_true",
        help="Use inverse-distance weighted voting instead of simple majority voting."
    )

    parser.add_argument(
        "--stale-sec",
        type=float,
        default=6.0,
        help="Seconds before a beacon is treated as missing. Default: 6.0"
    )

    parser.add_argument(
        "--min-valid",
        type=int,
        default=2,
        help="Minimum number of common valid beacons required for distance calculation. Default: 2"
    )

    parser.add_argument(
        "--grid-cols",
        type=int,
        default=3,
        help="Grid display columns. Default: 3"
    )

    parser.add_argument(
        "--show-neighbors",
        type=int,
        default=8,
        help="Number of nearest neighbors to show on screen. Default: 8"
    )

    parser.add_argument(
        "--log",
        default=None,
        help="Optional realtime estimation log CSV."
    )

    parser.add_argument(
        "--udp-ip",
        default=None,
        help="Optional PC IP for UDP output."
    )

    parser.add_argument(
        "--udp-port",
        type=int,
        default=5002,
        help="UDP output port. Default: 5002"
    )

    parser.add_argument(
        "--target-grid",
        type=int,
        default=1,
        choices=range(1, 10),
        metavar="1-9",
        help="Correct/current grid number for Sense HAT feedback. Default: 1"
    )

    parser.add_argument(
        "--no-sensehat",
        action="store_true",
        help="Disable Sense HAT LED feedback."
    )

    parser.add_argument(
        "--debug-all",
        action="store_true",
        help="Print all detected UUID candidates."
    )

    return parser.parse_args()


def init_sensehat(no_sensehat=False):
    """
    Sense HAT LEDを初期化する．
    sense_hatライブラリが無い環境ではNoneを返して推定だけ続ける．
    """
    if no_sensehat:
        return None

    if SenseHat is None:
        print("[WARN] sense_hat module is not installed. Sense HAT feedback is disabled.")
        return None

    try:
        sense = SenseHat()
        sense.clear()
        return sense
    except Exception as e:
        print(f"[WARN] Sense HAT initialization failed: {e}")
        print("[WARN] Sense HAT feedback is disabled.")
        return None


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


def grid_number_to_rc(n, grid_cols=3):
    """
    1 2 3
    4 5 6
    7 8 9
    という並びとして，番号を(row, col)へ変換する．
    """
    n = int(n)
    idx = n - 1
    return idx // grid_cols, idx % grid_cols


def grid_distance(pred_label, target_grid, grid_cols=3):
    """
    推定位置と設定した正解位置の距離を返す．
    距離は上下左右の移動回数，つまりマンハッタン距離．

    例: target=1 のとき
      G1 -> 0
      G2/G4 -> 1
      G3/G5/G7 -> 2
      G6/G8 -> 3
      G9 -> 4
    """
    pred_grid = label_to_grid_number(pred_label)

    if pred_grid is None:
        return None

    pr, pc = grid_number_to_rc(pred_grid, grid_cols)
    tr, tc = grid_number_to_rc(target_grid, grid_cols)

    return abs(pr - tr) + abs(pc - tc)


def rainbow_pixels():
    """
    Sense HAT 8x8 LED用の虹色パターンを返す．
    """
    colors = [
        [255, 0, 0],      # red
        [255, 80, 0],     # orange
        [255, 255, 0],    # yellow
        [0, 255, 0],      # green
        [0, 255, 255],    # cyan
        [0, 0, 255],      # blue
        [128, 0, 255],    # purple
        [255, 0, 128],    # magenta
    ]

    pixels = []
    for y in range(8):
        for x in range(8):
            pixels.append(colors[(x + y) % len(colors)])

    return pixels


def update_sensehat_feedback(sense, pred_label, target_grid, grid_cols, last_state):
    """
    推定結果と設定した正解位置との差に応じてSense HATを光らせる．

    distance=0: 虹色
    distance=1: 赤
    distance=2: 青
    distance>=3 or prediction unavailable: 消灯
    """
    if sense is None:
        return last_state, None

    dist = grid_distance(pred_label, target_grid, grid_cols) if pred_label is not None else None

    if dist == 0:
        state = ("rainbow", pred_label, target_grid)
    elif dist == 1:
        state = ("red", pred_label, target_grid)
    elif dist == 2:
        state = ("blue", pred_label, target_grid)
    else:
        state = ("off", pred_label, target_grid)

    if state == last_state:
        return last_state, dist

    if state[0] == "rainbow":
        sense.set_pixels(rainbow_pixels())
    elif state[0] == "red":
        sense.clear((255, 0, 0))
    elif state[0] == "blue":
        sense.clear((0, 0, 255))
    else:
        sense.clear()

    return state, dist
def update_sensehat_feedback(sense, pred_label, target_grid, grid_cols, last_state):
    """
    推定結果と設定した正解位置との差に応じてSense HATを光らせる．

    距離はマンハッタン距離:
      distance = 縦方向の差 + 横方向の差

    distance=0: 虹色
    distance=1: 赤
    distance=2: 緑
    distance=3: 青
    distance>=4 or prediction unavailable: 消灯
    """
    if sense is None:
        return last_state, None

    dist = grid_distance(pred_label, target_grid, grid_cols) if pred_label is not None else None

    if dist == 0:
        state = ("rainbow", pred_label, target_grid)
    elif dist == 1:
        state = ("red", pred_label, target_grid)
    elif dist == 2:
        state = ("green", pred_label, target_grid)
    elif dist == 3:
        state = ("blue", pred_label, target_grid)
    else:
        state = ("off", pred_label, target_grid)

    if state == last_state:
        return last_state, dist

    if state[0] == "rainbow":
        sense.set_pixels(rainbow_pixels())
    elif state[0] == "red":
        sense.clear((255, 0, 0))
    elif state[0] == "green":
        sense.clear((0, 255, 0))
    elif state[0] == "blue":
        sense.clear((0, 0, 255))
    else:
        sense.clear()

    return state, dist

def load_fingerprint_samples(path, min_valid_for_loading=1):
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


def estimate_position_knn(obs, samples, k, min_valid, weighted_vote=False):
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


def label_to_grid_index(label):
    if not label.startswith("G"):
        return None

    try:
        return int(label[1:])
    except ValueError:
        return None


def print_grid(pred_label, labels, grid_cols):
    n = len(labels)
    rows = math.ceil(n / grid_cols)

    print()
    for r in range(rows):
        cells = []

        for c in range(grid_cols):
            idx = r * grid_cols + c

            if idx >= n:
                cells.append("      ")
                continue

            label = labels[idx]

            if label == pred_label:
                cells.append(f"[{label:^4}]")
            else:
                cells.append(f" {label:^4} ")

        print(" ".join(cells))


async def main():
    args = parse_args()

    smooth_n = max(1, args.smooth_n)
    k = max(1, args.k)

    samples, labels = load_fingerprint_samples(
        args.fingerprint,
        min_valid_for_loading=1,
    )

    print("Loaded fingerprint samples:")
    print(f"  file     : {args.fingerprint}")
    print(f"  samples  : {len(samples)}")
    print(f"  labels   : {labels}")
    print(f"  k        : {k}")
    print(f"  smooth_n : {smooth_n}")
    print(f"  vote     : {'weighted' if args.weighted_vote else 'majority'}")
    print(f"  target   : G{args.target_grid}")

    latest = {}
    rssi_buf = defaultdict(lambda: deque(maxlen=smooth_n))
    debug_last = defaultdict(float)

    sense = init_sensehat(args.no_sensehat)
    last_sense_state = None

    udp_sock = None
    if args.udp_ip is not None:
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    log_file = None
    log_writer = None

    if args.log is not None:
        log_file = open(args.log, "a", newline="")
        log_writer = csv.writer(log_file)

        if log_file.tell() == 0:
            header = [
                "wall_time",
                "pred_label",
                "pred_score_or_votes",
                "k_used",
                "n_candidates",
                "best_neighbor_label",
                "best_neighbor_rmse",
                "vote_mode",
                "smooth_n",
                "target_grid",
                "grid_distance",
            ]

            for beacon in BEACONS:
                header += [
                    f"{beacon}_rssi_used",
                    f"{beacon}_rssi_raw",
                    f"{beacon}_age_s",
                    f"{beacon}_valid",
                ]

            log_writer.writerow(header)

    def callback(device, adv_data):
        now = time.time()

        if args.debug_all:
            addr = device.address.upper()
            if now - debug_last[addr] > 1.0:
                name = device.name or adv_data.local_name or "unknown"
                cands = extract_uuid_candidates(device, adv_data)
                if cands:
                    print(f"[DEBUG] addr={addr} name={name} rssi={adv_data.rssi} candidates={cands}")
                debug_last[addr] = now

        beacon_id, target_key, cand = find_target(device, adv_data)
        if beacon_id is None:
            return

        rssi_raw = adv_data.rssi

        rssi_buf[beacon_id].append(rssi_raw)
        rssi_used = sum(rssi_buf[beacon_id]) / len(rssi_buf[beacon_id])

        latest[beacon_id] = {
            "beacon_id": beacon_id,
            "target_key": target_key,
            "uuid": cand["uuid"],
            "source": cand["source"],
            "major": cand["major"],
            "minor": cand["minor"],
            "addr": device.address.upper(),
            "rssi_raw": rssi_raw,
            "rssi_used": rssi_used,
            "timestamp": now,
        }

    print()
    print("Start smartphone UUID BLE kNN realtime grid estimation")
    print("Targets:")
    for key, name in TARGETS.items():
        print(f"  {name}: {key}")
    print("Press Ctrl+C to stop")

    try:
        scanner = BleakScanner(
            callback,
            scanning_mode="active",
            bluez={
                "filters": {
                    "Transport": "le",
                    "DuplicateData": True,
                }
            },
        )
    except TypeError:
        scanner = BleakScanner(callback)

    await scanner.start()

    try:
        while True:
            await asyncio.sleep(args.sample_interval)

            now = time.time()
            obs = {}
            ages = {}

            for beacon in BEACONS:
                if beacon not in latest:
                    ages[beacon] = None
                    continue

                age = now - latest[beacon]["timestamp"]
                ages[beacon] = age

                if age <= args.stale_sec:
                    obs[beacon] = latest[beacon]["rssi_used"]

            pred_label, pred_score, k_used, neighbors, votes, topk = estimate_position_knn(
                obs=obs,
                samples=samples,
                k=k,
                min_valid=args.min_valid,
                weighted_vote=args.weighted_vote,
            )

            last_sense_state, sense_distance = update_sensehat_feedback(
                sense=sense,
                pred_label=pred_label,
                target_grid=args.target_grid,
                grid_cols=args.grid_cols,
                last_state=last_sense_state,
            )

            print("\033[2J\033[H", end="")
            print("Realtime Smartphone UUID BLE kNN Grid Estimator")
            print(f"fingerprint : {args.fingerprint}")
            print(f"time        : {time.strftime('%H:%M:%S')}")
            print(f"smooth_n    : {smooth_n}")
            print(f"k           : {k}")
            print(f"vote mode   : {'weighted' if args.weighted_vote else 'majority'}")
            print(f"min_valid   : {args.min_valid}")
            print(f"target grid : G{args.target_grid}")
            if sense is not None:
                print(f"Sense HAT   : enabled distance={sense_distance if sense_distance is not None else 'N/A'}")
            else:
                print("Sense HAT   : disabled")
            print()

            print("Current RSSI:")
            for beacon in BEACONS:
                if beacon in obs:
                    raw = latest[beacon]["rssi_raw"]
                    used = latest[beacon]["rssi_used"]
                    print(
                        f"  {beacon:8s}: used={used:7.2f} dBm "
                        f"raw={raw:4d} dBm age={ages[beacon]:.2f}s"
                    )
                else:
                    age_text = "None" if ages[beacon] is None else f"{ages[beacon]:.2f}s"
                    print(f"  {beacon:8s}: missing age={age_text}")

            print()

            if pred_label is None:
                print("Prediction: unavailable")
                print(f"reason    : valid/common beacons < {args.min_valid}")
                print(f"candidates: {len(neighbors)}")
            else:
                print(f"Prediction: {pred_label}")
                if args.weighted_vote:
                    print(f"score     : {pred_score:.3f}")
                else:
                    print(f"votes     : {pred_score}/{k_used}")
                print(f"k_used    : {k_used}")
                print(f"candidates: {len(neighbors)}")

                print()
                print("Vote summary:")
                if args.weighted_vote:
                    for label, score in sorted(votes.items(), key=lambda x: -x[1]):
                        print(f"  {label:6s}: score={score:.3f}")
                else:
                    for label, cnt in sorted(votes.items(), key=lambda x: (-x[1], x[0])):
                        print(f"  {label:6s}: votes={cnt}")

                print()
                print(f"Nearest neighbors top {min(args.show_neighbors, len(topk))}:")
                for i, n in enumerate(topk[:args.show_neighbors], start=1):
                    print(
                        f"  {i:2d}: label={n['label']:6s} "
                        f"rmse={n['rmse']:6.2f} dB "
                        f"common={n['n_common']} "
                        f"row={n['index']}"
                    )

                print_grid(pred_label, labels, args.grid_cols)

            if log_writer is not None:
                best_label = ""
                best_rmse = ""

                if topk:
                    best_label = topk[0]["label"]
                    best_rmse = round(topk[0]["rmse"], 3)

                row = [
                    now,
                    pred_label if pred_label is not None else "",
                    round(pred_score, 3) if isinstance(pred_score, float) else (pred_score or ""),
                    k_used if k_used is not None else 0,
                    len(neighbors),
                    best_label,
                    best_rmse,
                    "weighted" if args.weighted_vote else "majority",
                    smooth_n,
                    args.target_grid,
                    "" if sense_distance is None else sense_distance,
                ]

                for beacon in BEACONS:
                    if beacon in obs:
                        row += [
                            round(latest[beacon]["rssi_used"], 3),
                            round(latest[beacon]["rssi_raw"], 3),
                            round(ages[beacon], 3),
                            1,
                        ]
                    else:
                        row += [
                            "",
                            "",
                            "" if ages[beacon] is None else round(ages[beacon], 3),
                            0,
                        ]

                log_writer.writerow(row)
                log_file.flush()

            if udp_sock is not None and pred_label is not None:
                payload = {
                    "type": "position_estimate_knn",
                    "timestamp": now,
                    "pred_label": pred_label,
                    "pred_score_or_votes": pred_score,
                    "k": k,
                    "k_used": k_used,
                    "vote_mode": "weighted" if args.weighted_vote else "majority",
                    "smooth_n": smooth_n,
                    "n_candidates": len(neighbors),
                    "rssi_used": obs,
                    "rssi_raw": {
                        beacon: latest[beacon]["rssi_raw"]
                        for beacon in obs
                    },
                    "topk": topk[:args.show_neighbors],
                    "votes": votes,
                }

                udp_sock.sendto(
                    json.dumps(payload).encode("utf-8"),
                    (args.udp_ip, args.udp_port),
                )

    finally:
        await scanner.stop()

        if sense is not None:
            sense.clear()

        if udp_sock is not None:
            udp_sock.close()

        if log_file is not None:
            log_file.close()


if __name__ == "__main__":
    asyncio.run(main())
