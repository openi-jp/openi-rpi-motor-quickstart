import time
import traceback
import select
import sys
import termios
import tty
import RPi.GPIO as GPIO
import spidev

# ==============================================================================
# L6470 ステッピングモータードライバー チュートリアルプログラム (Raspberry Pi用)
# 動作内容: 右に1回転 -> 一定時間待機 -> 左に1回転
# ==============================================================================

# ------------------------------------------------------------------------------
# 1. GPIOピン設定 (CSピン, BUSYピンの組み合わせ)
# ------------------------------------------------------------------------------
# 以下の8通りの組み合わせから使用するピンペアを選択できます。
# (4, 12) 以外はコメントアウトしています。必要に応じて変更してください。

CS_PIN, BUSY_PIN = 4, 12      # デフォルト設定 (CS: GPIO 4, BUSY: GPIO 12)
# CS_PIN, BUSY_PIN = 5, 13    # (CS: GPIO 5, BUSY: GPIO 13)
# CS_PIN, BUSY_PIN = 16, 18   # (CS: GPIO 16, BUSY: GPIO 18)
# CS_PIN, BUSY_PIN = 17, 19   # (CS: GPIO 17, BUSY: GPIO 19)
# CS_PIN, BUSY_PIN = 20, 22   # (CS: GPIO 20, BUSY: GPIO 22)
# CS_PIN, BUSY_PIN = 21, 23   # (CS: GPIO 21, BUSY: GPIO 23)
# CS_PIN, BUSY_PIN = 24, 26   # (CS: GPIO 24, BUSY: GPIO 26)
# CS_PIN, BUSY_PIN = 25, 27   # (CS: GPIO 25, BUSY: GPIO 27)

# ------------------------------------------------------------------------------
# 2. L6470 コマンド・レジスタ定義
# ------------------------------------------------------------------------------
# コマンドコードの定義
CMD_NOP          = 0x00  # 何もしない
CMD_SET_PARAM    = 0x00  # レジスタ設定 (0x00 | レジスタアドレス)
CMD_GET_PARAM    = 0x20  # レジスタ取得 (0x20 | レジスタアドレス)
CMD_RUN          = 0x50  # 連続回転 (0x50 | 方向)
CMD_MOVE         = 0x40  # 指定ステップ数移動 (0x40 | 方向)
CMD_SOFT_STOP    = 0xB0  # 減速停止 (位置・トルク保持)
CMD_HARD_STOP    = 0xB8  # 即時停止 (位置・トルク保持)
CMD_SOFT_HIZ     = 0xA0  # 減速停止 (出力OFF / モーター出力解放)
CMD_HARD_HIZ     = 0xA8  # 即時停止 (出力OFF / モーター出力解放)
CMD_RESET_DEVICE = 0xC0  # L6470チップの再起動(初期化)

# 回転方向の定義 (L6470仕様)
DIR_FWD = 1  # 正転 (右回転 / 時計回り)
DIR_REV = 0  # 逆転 (左回転 / 反時計回り)

# 主なレジスタアドレス
REG_ACC        = 0x05  # 加速度 (12bit)
REG_DEC        = 0x06  # 減速度 (12bit)
REG_MAX_SPEED  = 0x07  # 最大速度 (10bit)
REG_KVAL_HOLD  = 0x09  # 停止時の駆動電圧 (8bit)
REG_KVAL_RUN   = 0x0A  # 定速回転時の駆動電圧 (8bit)
REG_KVAL_ACC   = 0x0B  # 加速時の駆動電圧 (8bit)
REG_KVAL_DEC   = 0x0C  # 減速時の駆動電圧 (8bit)
REG_STEP_MODE  = 0x16  # マイクロステップモード (8bit)

# ------------------------------------------------------------------------------
# 3. SPIおよびGPIO通信制御関数
# ------------------------------------------------------------------------------
spi = spidev.SpiDev()

def init_hardware():
    """Raspberry PiのGPIOピンおよびSPI通信を初期化します"""
    # GPIOをBCM(ピン番号指定)モードに設定
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    
    # CSピンを出力(初期状態: HIGH)、BUSYピンを入力(内部プルアップ有効)に設定
    GPIO.setup(CS_PIN, GPIO.OUT, initial=GPIO.HIGH)
    GPIO.setup(BUSY_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    # SPIバス0, デバイス0をオープン
    spi.open(0, 0)
    try:
        # ドライバーによっては既定値が異なるため、対応値を明示する。
        spi.bits_per_word = 8
        spi.max_speed_hz = 1000000  # 通信クロック: 1MHz (L6470は最大5MHzまで対応)
        spi.mode = 3                # L6470は SPI Mode 3 (CPOL=1, CPHA=1) を使用
        # no_csは環境によってEINVALになるため設定しない。
        # L6470のCSは上のGPIO.output()で制御し、SPIの既定CSは未接続のままにする。
    except OSError as exc:
        raise OSError(
            exc.errno,
            "SPI設定に失敗しました。SPIモード、速度、またはbits_per_wordに対応しているか確認してください",
        ) from exc

def transfer_byte(data_byte):
    """L6470へ1バイト送信し、CSピンを個別にコントロールします"""
    GPIO.output(CS_PIN, GPIO.LOW)   # CSをLOWにしてL6470を選択
    time.sleep(0.000001)            # 1μsウェイト
    rx = spi.xfer2([data_byte & 0xFF])
    GPIO.output(CS_PIN, GPIO.HIGH)  # CSをHIGHにしてトグル完了
    time.sleep(0.000001)            # 1μsウェイト
    return rx

def set_param(param_addr, len_bytes, value):
    """L6470の内部レジスタにパラメータを設定します"""
    # SetParamコマンド (0x00 | アドレス) を送信
    transfer_byte(CMD_SET_PARAM | (param_addr & 0x1F))
    # 上位バイトから順番に送信 (MSBファースト)
    for i in range(len_bytes - 1, -1, -1):
        byte_val = (value >> (8 * i)) & 0xFF
        transfer_byte(byte_val)

def move(direction, steps):
    """指定した方向(direction)へ指定マイクロステップ数(steps)だけ移動します"""
    # Moveコマンド送信 (0x40 | 方向)
    cmd = CMD_MOVE | (direction & 0x01)
    transfer_byte(cmd)
    
    # 移動ステップ数 (22bit長) を3バイトで送信
    transfer_byte((steps >> 16) & 0x3F)
    transfer_byte((steps >> 8) & 0xFF)
    transfer_byte(steps & 0xFF)

def wait_for_move():
    """L6470のBUSYピンを監視し、モーターの動作完了まで待ちます"""
    time.sleep(0.005)  # コマンド送信後、BUSYピンがLOWになるまでのタイムラグを考慮
    
    # L6470のBUSYピンはオープンドレイン出力のため、動作中=LOW(0)、停止完了=HIGH(1)
    while GPIO.input(BUSY_PIN) == GPIO.LOW:
        time.sleep(0.01)  # CPU負荷軽減のため10ms周期でチェック

def wait_for_quit_or_timeout(timeout):
    """qキーが押されるか、指定時間が経過するまで待機します"""
    if not sys.stdin.isatty():
        raise RuntimeError("qキーを受け取るには端末から実行してください")

    original_terminal = termios.tcgetattr(sys.stdin)
    deadline = time.monotonic() + timeout
    try:
        tty.setcbreak(sys.stdin.fileno())
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            readable, _, _ = select.select([sys.stdin], [], [], min(0.1, remaining))
            if readable and sys.stdin.read(1).lower() == "q":
                return True
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal)
    return False

def wait_for_move_or_quit():
    """移動完了またはqキー入力まで待機します"""
    while GPIO.input(BUSY_PIN) == GPIO.LOW:
        if wait_for_quit_or_timeout(0.01):
            return True
    return False

def init_l6470():
    """L6470の各種パラメータを初期化します"""
    # チップのリセット (電源オン状態に戻す)
    transfer_byte(CMD_RESET_DEVICE)
    time.sleep(0.01)
    
    # マイクロステップモード設定: 1/128ステップ (デフォルト)
    # 0x07: 1/128, 0x04: 1/16, 0x00: フルステップ
    set_param(REG_STEP_MODE, 1, 0x07)
    
    # モーター印加電圧(KVAL)の設定 (0x00〜0xFF, 0x29 ≒ 定格の16%)
    set_param(REG_KVAL_HOLD, 1, 0x29)  # 停止時
    set_param(REG_KVAL_RUN,  1, 0x29)  # 定速走行時
    set_param(REG_KVAL_ACC,  1, 0x29)  # 加速時
    set_param(REG_KVAL_DEC,  1, 0x29)  # 減速時
    
    # 加減速プロファイル・最高速度の設定
    set_param(REG_ACC,       2, 0x08A)  # 加速度 (約2008 step/s^2)
    set_param(REG_DEC,       2, 0x08A)  # 減速度 (約2008 step/s^2)
    set_param(REG_MAX_SPEED, 2, 0x041)  # 最高速度 (約300 RPM相当)

# ------------------------------------------------------------------------------
# 4. メイン処理
# ------------------------------------------------------------------------------
def main():
    print("=== L6470 チュートリアルプログラム開始 ===")
    print(f"選択されたピン構成 -> CS: GPIO {CS_PIN}, BUSY: GPIO {BUSY_PIN}")
    
    try:
        # ハードウェアおよびL6470の初期化
        init_hardware()
        init_l6470()
        
        STEPS_PER_REV = 200 * 128
        WAIT_TIME = 0.5
        print("\n右回転、0.5秒停止、左回転、0.5秒停止を繰り返します...")
        while True:
            print("右回転中...")
            move(DIR_FWD, STEPS_PER_REV)
            if wait_for_move_or_quit():
                break

            print("0.5秒停止中...")
            if wait_for_quit_or_timeout(WAIT_TIME):
                break

            print("左回転中...")
            move(DIR_REV, STEPS_PER_REV)
            if wait_for_move_or_quit():
                break

            print("0.5秒停止中...")
            if wait_for_quit_or_timeout(WAIT_TIME):
                break

        transfer_byte(CMD_HARD_STOP)
        wait_for_move()
        transfer_byte(CMD_SOFT_HIZ)
        print("\n停止しました。")

    except KeyboardInterrupt:
        # Ctrl+C 等で強制終了された場合
        print("\nユーザーによって動作が中断されました。")
        try:
            transfer_byte(CMD_HARD_HIZ)  # 安全のため即座に出力をオフ
        except OSError:
            pass
    except Exception as e:
        print(f"\nエラーが発生しました: {e}")
        traceback.print_exc()
        try:
            transfer_byte(CMD_HARD_HIZ)
        except Exception:
            pass  # SPI未オープン時は無視
    finally:
        # ピンおよびSPIリソースの解放処理
        spi.close()
        GPIO.cleanup()
        print("GPIOおよびSPIインターフェースを正常に解放しました。")

if __name__ == "__main__":
    main()