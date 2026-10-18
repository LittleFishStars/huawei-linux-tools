#!/usr/bin/env bash
# 华为分享 Linux 移植 —— 一键构建
# 前置：pacman -S --needed base-devel glib2 libutil-linux openssl
# 产物：build/hwshare-rx、build/hwshare-dtx（libsharekit.so 未到位时用 stub）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BZ_VER=5.52
BZ="$ROOT/vendor/bluez-$BZ_VER"
DEMO="$ROOT/vendor/huaweicodelabs-ShareEngine"
OUT="$ROOT/build"
SDK_LIB="$ROOT/sdk/lib"
SDK_INC="$ROOT/sdk/include"
ARCH="$(uname -m)"

need() { command -v "$1" >/dev/null || { echo "缺少 $1，请先: pacman -S --needed $2"; exit 1; }; }
need gcc base-devel
need curl curl

# ---------- 1. BlueZ 5.52 内部静态库 ----------
if [ ! -f "$BZ/lib/.libs/bluetooth-internal.a" ] || [ ! -f "$BZ/src/.libs/libshared-mainloop.a" ]; then
    echo "== [1/4] 编译 BlueZ $BZ_VER 内部库（首次约几分钟）=="
    mkdir -p vendor
    cd vendor
    [ -f "bluez-$BZ_VER.tar.xz" ] || curl -sSL -o "bluez-$BZ_VER.tar.xz" \
        "https://mirrors.edge.kernel.org/pub/linux/bluetooth/bluez-$BZ_VER.tar.xz"
    [ -d "bluez-$BZ_VER" ] || tar xf "bluez-$BZ_VER.tar.xz"
    cd "bluez-$BZ_VER"
    # 静态库不需要 udev/cups/obex/mesh；--enable-library 提供 libbluetooth 公开库
    ./configure --prefix="$PWD/../bluez-$BZ_VER-install" \
        --disable-cups --disable-obex --disable-mesh --disable-systemd \
        --disable-udev --disable-manpages --enable-library >/dev/null
    # 只构建 demo 需要的两个内部库（全量 make 会在新 glibc 上因 pause() 冲突失败）
    make -j"$(nproc)" lib/libbluetooth-internal.la src/libshared-mainloop.la
    cd "$ROOT"
else
    echo "== [1/4] BlueZ 内部库已就绪 =="
fi

# ---------- 2. libsharekit：官方 SDK 优先，否则 stub ----------
mkdir -p "$OUT"
echo "== [2/4] 准备 libsharekit =="
REAL_SO=""
for cand in "$SDK_LIB/$ARCH/libsharekit.so" "$SDK_LIB/libsharekit.so" "$SDK_LIB/gcc/libsharekit.so"; do
    [ -f "$cand" ] && REAL_SO="$cand" && break
done
if [ -n "$REAL_SO" ]; then
    echo "    使用官方 SDK: $REAL_SO"
    cp -f "$REAL_SO" "$OUT/libsharekit.so"
    SDK_INC_DIR="$SDK_INC"
else
    echo "    !! 未找到官方 libsharekit.so（sdk/lib/ 下为空）"
    echo "    !! 使用 stub 占位——程序可跑但无分享功能，仅验证构建链"
    cat > "$OUT/libsharekit_stub.c" <<'EOF'
/* libsharekit.so 占位实现：等华为官方 Share Engine Linux SDK 到手后替换 */
#include "sharekit_api.h"
#include <stdio.h>
int InitShareKit(void* handle){ (void)handle;
    printf("[stub] libsharekit 未安装——请从华为开发者官网下载 Share Engine Linux SDK。\n");
    return -1; }
int UninitShareKit(void){ return 0; }
int StartShareService(ShareRole role, void* handle){ (void)role;(void)handle; return -1; }
int StopShareService(void){ return 0; }
int GetAvailableDevice(ShareDeviceInfo** d, int* n){ (void)d;(void)n; return -1; }
int TransFile(const ShareDeviceInfo* a, const ShareFileInfo* b){ (void)a;(void)b; return -1; }
void CancelTransFile(void){}
int ConfirmRecvFile(const char* p){ (void)p; return -1; }
void RefuseRecvFile(void){}
EOF
    gcc -c -o "$OUT/libsharekit_stub.o" "$OUT/libsharekit_stub.c" -I "$DEMO" -fPIC
    gcc -shared -o "$OUT/libsharekit.so" "$OUT/libsharekit_stub.o"
    SDK_INC_DIR="$DEMO"
fi

# ---------- 3. 编译 demo（南向 BLE 适配 + demo 主体） ----------
echo "== [3/4] 编译 =="
CFLAGS="-O2 -g -I $DEMO -I $BZ -I $BZ/lib -I $BZ/src \
  -I /usr/include/glib-2.0 -I /usr/lib/glib-2.0/include \
  -Wno-incompatible-pointer-types -Wno-deprecated-declarations"
LIBS="-L $BZ/lib/.libs -L $BZ/src/.libs -lbluetooth-internal -lshared-mainloop -lcrypto -lpthread"

gcc -c -o "$OUT/driver_ble_rx.o" "$DEMO/driver_ble_rx.c" $CFLAGS
gcc -c -o "$OUT/driver_ble_tx.o" "$DEMO/driver_ble_tx.c" $CFLAGS
gcc -c -o "$OUT/sharekitdrx.o" "$DEMO/sharekitdrx.c" -I "$DEMO" -Wno-incompatible-pointer-types
gcc -c -o "$OUT/sharekitdtx.o" "$DEMO/sharekitdtx.c" -I "$DEMO" -Wno-incompatible-pointer-types

gcc -o "$OUT/hwshare-rx" "$OUT/sharekitdrx.o" "$OUT/driver_ble_rx.o" \
    -L "$OUT" -lsharekit $LIBS -Wl,-rpath,"$OUT"
gcc -o "$OUT/hwshare-dtx" "$OUT/sharekitdtx.o" "$OUT/driver_ble_tx.o" \
    -L "$OUT" -lsharekit $LIBS -Wl,-rpath,"$OUT"

# ---------- 4. 收尾 ----------
echo "== [4/4] 完成 =="
ls -l "$OUT/hwshare-rx" "$OUT/hwshare-dtx"
if [ -z "$REAL_SO" ]; then
    echo
    echo "⚠ 当前为 stub 模式。拿到官方 SDK 后："
    echo "  1) 把 libsharekit.so 放到 $SDK_LIB/"
    echo "  2) 头文件放到 $SDK_INC/（若 SDK 自带 sharekit_api.h 则优先用 SDK 的）"
    echo "  3) 重新运行本脚本"
fi
