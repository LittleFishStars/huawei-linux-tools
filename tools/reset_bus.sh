#!/usr/bin/env bash
# reset_bus.sh —— 复位被 wedged 的 i2c-0，并把触控板一起救回来
#
# 背景
#   NTAG0001 处于 NC(FIFO) 模式：单次读超过可见窗口（实测 16 字节）时，
#   芯片会拉住 SCL 不放，控制器等满超时后进入 wedged 状态。
#   此后该总线上所有传输都返回 -110 ETIMEDOUT，读数全不可信，
#   连触控板（GXTP7863，同一条 i2c-0）也会失灵。
#
# 为什么不能只用 unbind/bind
#   只做 PCI 控制器的 unbind/bind 能复位适配器，但内核重建 adapter 后
#   i2c-hid 抓着的是旧 client 的运行时状态，触控板会失灵。
#   所以必须两步一起做：先复位控制器，再重绑 GXTP client。
#   （这两步都已实测验证过。）
#
# 用法: sudo ./reset_bus.sh
set -u

CTRL=0000:00:15.0            # Alder Lake-P PCH Serial IO I2C #0 -> i2c-0
FOUND=0

say() { printf '\n== %s ==\n' "$*"; }

# ---------- 0. 摘掉自建模块，避免它持有 client ----------
say "0. 卸载 ntag_nfc（若在运行）"
if lsmod | grep -q '^ntag_nfc'; then
    rmmod ntag_nfc && echo "  已卸载" || echo "  卸载失败"
else
    echo "  未加载"
fi

# ---------- 1. 复位 PCI I2C 控制器 ----------
say "1. 复位 I2C 控制器 $CTRL"
DRVDIR=/sys/bus/pci/devices/$CTRL/driver
if [ ! -e "$DRVDIR" ]; then
    echo "  找不到 $CTRL 的驱动，跳过控制器复位"
else
    NAME=$(basename "$(readlink -f "$DRVDIR")")
    echo "  驱动 = $NAME"
    echo "$CTRL" > /sys/bus/pci/drivers/"$NAME"/unbind \
        && echo "  unbind OK" || echo "  unbind 失败"
    sleep 1
    echo "$CTRL" > /sys/bus/pci/drivers/"$NAME"/bind \
        && echo "  bind OK" || echo "  bind 失败"
    sleep 1
    FOUND=1
fi

# ---------- 2. 重绑触控 client（关键的第 2 步）----------
say "2. 重绑 i2c-0 上的触控与 NFC client"
for drvdir in /sys/bus/i2c/drivers/*/; do
    drv=$(basename "$drvdir")
    for l in "$drvdir"i2c-GXTP*; do
        [ -e "$l" ] || continue
        cli=$(basename "$l")
        echo "  $cli : $drv -> unbind"
        echo -n "$cli" > "$drvdir/unbind" 2>/dev/null
        sleep 1
        echo "  $cli : $drv -> bind"
        echo -n "$cli" > "$drvdir/bind" 2>/dev/null \
            && echo "    bind OK" || echo "    bind 失败"
        sleep 1
    done
done

# 若还没绑上，重载 i2c-hid 栈
if ! grep -q . /sys/bus/i2c/drivers/*/i2c-GXTP* 2>/dev/null; then
    say "2b. 重载 i2c-hid 栈"
    modprobe -r hid_multitouch 2>/dev/null
    modprobe -r i2c_hid_acpi 2>/dev/null
    modprobe -r i2c_hid 2>/dev/null
    sleep 1
    modprobe i2c_hid_acpi 2>/dev/null || modprobe i2c_hid
    modprobe hid_multitouch 2>/dev/null
    sleep 2
fi

# ---------- 3. 结果 ----------
say "3. 结果"
for l in /sys/bus/i2c/drivers/*/i2c-GXTP* /sys/bus/i2c/drivers/*/i2c-NTAG*; do
    [ -e "$l" ] || continue
    echo "  $(basename "$l")  ->  $(basename "$(dirname "$l")")"
done
echo
echo "  --- 总线 ---"
[ -e /sys/bus/i2c/devices/i2c-0/name ] \
    && echo "  i2c-0 = $(cat /sys/bus/i2c/devices/i2c-0/name)"
echo
echo "  --- 触摸类输入设备 ---"
grep -iE 'name=' /proc/bus/input/devices 2>/dev/null \
    | grep -iE 'touch|goodix|gxtp|pad' || echo "  （未找到，若指针仍不动请重跑本脚本或重启）"

say "4. 验证适配器是否已恢复"
echo "  接着跑： sudo python3 ntag_read.py --bus 0 --addr 0x57 --map"
echo "  （对照组 0x60 应为 NAK(121)；若仍 110，再跑一次本脚本）"
