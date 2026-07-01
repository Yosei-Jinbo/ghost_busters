"""raspi_notifier.py の単体テスト。

実際にlocalhostのUDPソケットを立て、送信内容(JSON)を検証する。
"""
import json
import socket

from domain import MagicType
from raspi_notifier import RaspiNotifier, RaspiTarget


def _bind_udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))  # 空きポートを自動割当
    s.settimeout(1.0)
    return s, s.getsockname()[1]


def test_notify_magic_sends_udp_with_payload():
    sock, port = _bind_udp()
    try:
        notifier = RaspiNotifier([RaspiTarget("127.0.0.1", port)])
        notifier.notify_magic(MagicType.ATTACK, payload={"distance": 3})
        data, _ = sock.recvfrom(4096)
        msg = json.loads(data.decode())
        assert msg["type"] == "magic"
        assert msg["magic"] == "ATTACK"
        assert msg["distance"] == 3
    finally:
        sock.close()


def test_notify_magic_sends_to_all_targets():
    s1, p1 = _bind_udp()
    s2, p2 = _bind_udp()
    try:
        notifier = RaspiNotifier(
            [RaspiTarget("127.0.0.1", p1), RaspiTarget("127.0.0.1", p2)]
        )
        notifier.notify_magic(MagicType.SCAN, payload=None)
        for s in (s1, s2):
            data, _ = s.recvfrom(4096)
            assert json.loads(data.decode())["magic"] == "SCAN"
    finally:
        s1.close()
        s2.close()


def test_notify_result_sends_udp():
    sock, port = _bind_udp()
    try:
        notifier = RaspiNotifier([RaspiTarget("127.0.0.1", port)])
        notifier.notify_result("clear")
        data, _ = sock.recvfrom(4096)
        msg = json.loads(data.decode())
        assert msg["type"] == "result"
        assert msg["result"] == "clear"
    finally:
        sock.close()


def test_register_appends_target():
    notifier = RaspiNotifier([])
    assert notifier.targets == []
    target = RaspiTarget("127.0.0.1", 9999)
    notifier.register(target)
    assert target in notifier.targets
