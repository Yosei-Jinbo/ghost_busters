import time
from pyjoycon import JoyCon, get_R_id

# 1. 右のJoy-ConのIDを取得
joycon_id = get_R_id()
if not joycon_id:
    print("Joy-Con (R) が見つかりません。Bluetooth接続を確認してください。")
    exit()

joycon = JoyCon(*joycon_id)

# 振りの検知用（1つ前のフレームの値を保存する変数）
prev_y = 0
prev_z = 0

# 振ったと判定する閾値（しきい値）
SHAKE_THRESHOLD = 2000

# ─── 🆕 今回追加する状態管理用のフラグ ───
flag = False              # 振り入力を受け付けるかどうかのフラグ
already_detected = False  # 今回のボタン押しで、既に検知したかどうかのフラグ

print("プログラムが起動しました。")
print("R または ZR ボタンを押しながら、Joy-Conを上下または左右に振ってください。")
print("※1回ボタンを押す（押し続ける）ごとに、1回だけ検知します。")

try:
    while True:
        # 現在のステータスを取得
        status = joycon.get_status()
        
        # ボタンの入力状態を取得
        buttons = status.get('buttons', {}).get('right', {})
        r_pressed = buttons.get('r', 0) == 1
        zr_pressed = buttons.get('zr', 0) == 1
        
        # 加速度（現在の値）を取得
        accel = status.get('accel', {})
        current_y = accel.get('y', 0)
        current_z = accel.get('z', 0)
        
        # 1つ前のデータとの差（激しさ）を計算
        diff_y = abs(current_y - prev_y)
        diff_z = abs(current_z - prev_z)
        
        # ─── 🆕 フラグのコントロール処理 ───
        is_button_held = r_pressed or zr_pressed # ボタンが押されているか
        
        if is_button_held:
            # ボタンが押されていて、かつ「まだ今回検知していない」ならフラグを立てる
            if not already_detected:
                flag = True
        else:
            # ボタンが離されたら、すべてをリセットして次のボタン押しに備える
            flag = False
            already_detected = False
            
        # ─── 🆕 フラグが立っている間だけ振りを検知 ───
        if flag:
            # 上下（Z方向）の判定
            if diff_z > SHAKE_THRESHOLD and diff_z > diff_y:
                print("【検知】上下（Z方向）に振られました！")
                
                flag = False             # 1回表示したのでフラグを下ろす
                already_detected = True   # ボタンが離されるまで次の検知をブロック
                
            # 左右（Y方向）の判定
            elif diff_y > SHAKE_THRESHOLD and diff_y > diff_z:
                print("【検集】左右（Y方向）に振られました！")
                
                flag = False             # 1回表示したのでフラグを下ろす
                already_detected = True   # ボタンが離されるまで次の検知をブロック
        
        # 次のループのために、現在の値を「前回の値」として保存
        prev_y = current_y
        prev_z = current_z
        
        # ループの間隔（20ミリ秒 = 50Hz）
        time.sleep(0.02)

except KeyboardInterrupt:
    print("\nプログラムを終了しました。")