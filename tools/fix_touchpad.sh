#!/bin/bash
# fix_touchpad.sh — 恢复 i2c-0 上的触控设备（PCI 控制器 unbind/bind 之后的常见后果）
#
# 原理：我把 0000:00:15.0 解绑再绑，内核重建了 i2c adapter 和 i2c client，
# 但 i2c-hid 那套驱动抓着的是"旧" client 的运行时状态（复位/上电手势已失效），
# 于是后续传输一直失败、指针不动。解法是把 client 重新 bind 一次，或把 i2c-hid 栈重载。
#
# 用法: sudo ./fix_touchpad.sh
set -u

say() { printf '\n== %s ==\n' "$*"; }

say "0. 先摘掉我加的 NFC 模块（别让它搅局）"
modprobe -r ntag_nfc 2>/dev/null && echo "已卸载 ntag_nfc" || echo "ntag_nfc 未加载"

say "1. 当前 i2c-0 上的设备和它们的驱动"
for l in /sys/bus/i2c/drivers/*/i2c-GXTP* /sys/bus/i2c/drivers/*/i2c-NTAG*; do
    [ -e "$l" ] || continue
    echo "  $(basename "$l")  ->  $(basename "$(dirname "$l")")"
done
ls /sys/bus/i2c/devices/ | grep -E 'GXTP|NTAG' || true

say "2. 逐个 client：unbind 再 bind"
for drvdir in /sys/bus/i2c/drivers/*/; do
    drv=$(basename "$drvdir")
    for l in "$drvdir"i2c-GXTP*; do
        [ -e "$l" ] || continue
        cli=$(basename "$l")
        echo "  $cli : $drv -> unbind"
        echo -n "$cli" > "$drvdir/unbind" 2>/dev/null
        sleep 1
        echo "  $cli : $drv -> bind"
        echo -n "$cli" > "$drvdir/bind" 2>/dev/null || echo "     bind 失败（可能驱动不认了）"
        sleep 1
    done
done

say "3. 如果还没好，重载 i2c-hid 栈"
if ! grep -q . /sys/bus/i2c/drivers/*/i2c-GXTP* 2>/dev/null; then
    echo "  没看到 GXTP 绑定，尝试重载模块"
    modprobe -r hid_multitouch 2>/dev/null
    modprobe -r i2c_hid_acpi 2>/dev/null
    modprobe -r i2c_hid 2>/dev/null
    sleep 1
    modprobe i2c_hid_acpi 2>/dev/null || modprobe i2c_hid
    modprobe hid_multitouch 2>/dev/null
    sleep 2
fi

say "4. 结果"
for l in /sys/bus/i2c/drivers/*/i2c-GXTP*; do
    [ -e "$l" ] || continue
    echo "  $(basename "$l")  ->  $(basename "$(dirname "$l")")"
done
echo
echo "--- 输入设备 ---"
grep -iE 'name=' /proc/bus/input/devices | grep -iE 'touch|goodix|gxtp|pad|synaptics' || echo "  （没找到触摸类输入设备）"

say "5. dmesg 末尾"
dmesg | tail -30

cat <<'EOF'

如果指针还是不动：
  1) 再跑一次本脚本
  2) 实在不行就 sudo reboot —— 触控板会在 ACPI 枚举时被完整重新初始化
注意：本脚本不动 i2c-0 的 PCI 控制器本身，只重绑 client，比刚才的 recover_bus.sh 温和。
EOF
