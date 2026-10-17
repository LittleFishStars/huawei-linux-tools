#!/bin/bash
# get_acpi.sh — 把 ACPI 表全部 dump 并反编译成 .dsl，供分析 NTAG0001 的设备定义
#
# 要拿的东西：
#   * NTAG0001 的 _HID/_CID/_UID/_SUB（厂商给的兼容 ID 常暴露真型号）
#   * _CRS：I2cSerialBus 参数 + 有没有 GpioInt/GpioIo
#   * _PS0/_PS3/_ON/_OFF：上电/复位时序（几个 GPIO、什么顺序、延时多少）
#   * _DSD：厂商自定义属性
#
# 用法: sudo ./get_acpi.sh
set -u
OUT=/home/ylxc/nfc-re/acpi
mkdir -p "$OUT"
cd "$OUT" || exit 1

if ! command -v acpidump >/dev/null 2>&1; then
    echo "== 安装 acpica =="
    pacman -S --needed --noconfirm acpica || { echo "安装失败"; exit 1; }
fi

echo "== dump 全部 ACPI 表 =="
acpidump -o acpi.bin || exit 1

echo "== 拆表 =="
acpixtract -a acpi.bin | tail -5
ls -la ./*.dat | head -40

echo "== 反编译（失败的个别表可以忽略） =="
for f in ./*.dat; do
    iasl -d "$f" >/dev/null 2>&1
done
echo "生成的 .dsl："
ls -la ./*.dsl 2>/dev/null || echo "  一个都没有，iasl 可能没装上"

echo
echo "== 在 .dsl 里找 NTAG0001 =="
grep -l "NTAG0001" ./*.dsl 2>/dev/null || echo "  没找到（可能在 SSDT 里且反编译失败）"

echo
echo "== 顺便看一眼 NTAG0001 的 sysfs 侧 =="
ls /sys/bus/acpi/devices/ | grep -i ntag || true
for f in /sys/bus/acpi/devices/NTAG0001:00/{status,power_state,path,hid,uid,modalias}; do
    [ -r "$f" ] && echo "  $f = $(cat "$f" 2>/dev/null)"
done

cat <<'EOF'

完成。把上面 == 在 .dsl 里找 NTAG0001 == 那行报出来的文件名告诉我，
或者直接把 acpi/ 目录留着，我来读。
EOF
