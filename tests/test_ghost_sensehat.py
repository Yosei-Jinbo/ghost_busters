"""ghost_sensehat.py の単体テスト(SenseHat実機なしで発光ロジックを検証)。"""
import ghost_sensehat
from ghost_light_raspi import extract_light
from ghost_sensehat import (
    distance_to_state,
    rainbow_pixels,
    update_light_state,
    update_sensehat_feedback,
)


class FakeSense:
    """SenseHatの代わりに呼び出しを記録するダブル。"""

    def __init__(self):
        self.calls = []

    def clear(self, *args):
        self.calls.append(("clear", args))

    def set_pixels(self, pixels):
        self.calls.append(("set_pixels", len(pixels)))

    def show_message(self, text, scroll_speed=0.1, text_colour=None):
        self.calls.append(("show_message", text, scroll_speed, text_colour))


def test_distance_to_state_mapping():
    assert distance_to_state(0) == "rainbow"
    assert distance_to_state(1) == "red"
    assert distance_to_state(2) == "green"
    assert distance_to_state(3) == "blue"
    assert distance_to_state(4) == "off"
    assert distance_to_state(99) == "off"
    assert distance_to_state(None) == "off"


def test_rainbow_pixels_has_64_cells():
    px = rainbow_pixels()
    assert len(px) == 64
    assert all(len(c) == 3 for c in px)


def test_update_draws_rainbow_on_distance_zero():
    sense = FakeSense()
    state = update_sensehat_feedback(sense, 0, last_state=None)
    assert state == "rainbow"
    assert sense.calls == [("set_pixels", 64)]


def test_update_draws_solid_color():
    sense = FakeSense()
    state = update_sensehat_feedback(sense, 1, last_state=None)
    assert state == "red"
    assert sense.calls == [("clear", ((255, 0, 0),))]


def test_update_clears_when_far_or_unknown():
    sense = FakeSense()
    assert update_sensehat_feedback(sense, 4, last_state="red") == "off"
    assert update_sensehat_feedback(sense, None, last_state="off") == "off"
    # 距離4は off へ遷移して clear()、Noneは off のままなので再描画されない
    assert sense.calls == [("clear", ())]


def test_update_skips_redraw_when_state_unchanged():
    sense = FakeSense()
    # 同じ距離(=同じ状態)が続くときは再描画しない(ちらつき防止)。
    first = update_sensehat_feedback(sense, 2, last_state=None)
    second = update_sensehat_feedback(sense, 2, last_state=first)
    assert first == second == "green"
    assert sense.calls == [("clear", ((0, 255, 0),))]  # 2回目は描画されない


def test_update_without_sense_returns_state_only():
    # 実機なし(None)でも状態遷移だけは返す(例外を出さない)。
    assert update_sensehat_feedback(None, 0, last_state=None) == "rainbow"
    assert update_sensehat_feedback(None, 5, last_state="red") == "off"


# ---- 終了演出(点滅 / メッセージ表示) ----
def test_blink_toggles_light_and_off_each_cycle(monkeypatch):
    # time.sleep を無効化して、点灯(set_pixels)→消灯(clear)が times 回繰り返されることを確認。
    monkeypatch.setattr(ghost_sensehat.time, "sleep", lambda _s: None)
    sense = FakeSense()
    ghost_sensehat.blink(sense, "rainbow", times=2, on_sec=0.1, off_sec=0.1)
    assert sense.calls == [
        ("set_pixels", 64), ("clear", ()),
        ("set_pixels", 64), ("clear", ()),
    ]


def test_show_message_passes_text_and_color():
    sense = FakeSense()
    ghost_sensehat.show_message(sense, "GAME OVER", [255, 0, 0], 0.08)
    assert sense.calls == [("show_message", "GAME OVER", 0.08, [255, 0, 0])]


def test_blink_and_show_message_without_sense_do_nothing():
    # 実機なし(None)でも例外を出さない。
    ghost_sensehat.blink(None, "rainbow", 3, 0.1, 0.1)
    ghost_sensehat.show_message(None, "X", [1, 2, 3], 0.1)


# ---- サーバ(domain)が決めた light をそのまま描画する経路 ----
def test_update_light_state_renders_given_state():
    sense = FakeSense()
    state = update_light_state(sense, "blue", last_state=None)
    assert state == "blue"
    assert sense.calls == [("clear", ((0, 0, 255),))]


def test_update_light_state_skips_redraw_when_unchanged():
    sense = FakeSense()
    first = update_light_state(sense, "green", last_state=None)
    second = update_light_state(sense, "green", last_state=first)
    assert first == second == "green"
    assert sense.calls == [("clear", ((0, 255, 0),))]  # 2回目は描画されない


def test_extract_light_prefers_valid_state():
    assert extract_light({"light": "rainbow"}) == "rainbow"
    assert extract_light({"light": "off"}) == "off"


def test_extract_light_rejects_unknown_or_missing():
    assert extract_light({"light": "chartreuse"}) is None  # 未知の状態名
    assert extract_light({"distance": 2}) is None           # light欠落
    assert extract_light({"light": 3}) is None              # 型不正
