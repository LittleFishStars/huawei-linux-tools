#!/usr/bin/env bash
# 校验官方 SDK 是否到位：架构、导出符号、动态依赖
# 用法：把下载到的 libsharekit.so 放进 sdk/lib/<架构>/ 后运行本脚本
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCH="$(uname -m)"
OK=1

echo "== 架构检查（本机 $ARCH）=="
for so in "$ROOT"/sdk/lib/*/libsharekit.so "$ROOT"/sdk/lib/libsharekit.so; do
    [ -f "$so" ] || continue
    farch=$(readelf -h "$so" | awk -F': *' '/^ *Machine:/{print $2}')
    echo "  $so → $farch"
    case "$ARCH:$farch" in
        x86_64:*X86-64*) echo "    ✅ 与本机匹配";;
        aarch64:*AArch64*) echo "    ✅ 与本机匹配";;
        *) echo "    ⚠ 与本机不匹配（仅信息展示）";;
    esac
done

echo
echo "== 导出符号检查（北向 API）=="
SO="$ROOT/sdk/lib/$ARCH/libsharekit.so"
[ -f "$SO" ] || SO="$ROOT/sdk/lib/libsharekit.so"
[ -f "$SO" ] || SO="$ROOT/build/libsharekit.so"
if [ -f "$SO" ]; then
    for sym in InitShareKit UninitShareKit StartShareService StopShareService \
               GetAvailableDevice TransFile CancelTransFile ConfirmRecvFile RefuseRecvFile; do
        if nm -D "$SO" 2>/dev/null | grep -q " T $sym\$"; then
            echo "  ✅ $sym"
        else
            echo "  ❌ $sym 缺失"; OK=0
        fi
    done
    echo
    echo "== 动态依赖（OpenSSL 1.1 绑定检查）=="
    ldd "$SO" 2>/dev/null | grep -E 'libssl|libcrypto' || echo "  （无 ssl/crypto 依赖）"
    readelf -d "$SO" | grep NEEDED || true
else
    echo "  （还没放到 sdk/lib/，检查 build/ 下 stub）"
fi

echo
echo "== OpenSSL 兼容性 =="
if [ -f "$SO" ] && readelf -d "$SO" 2>/dev/null | grep -q 'libcrypto.so.1.1'; then
    echo "  ⚠ 该 SDK 绑定 OpenSSL 1.1（系统是 3.x）——需要: pacman -S openssl-1.1 或从 AUR 装"
fi

[ "$OK" -eq 1 ] && echo -e "\n✅ SDK 校验通过" || echo -e "\n❌ 有符号缺失"
exit $((1-OK))
