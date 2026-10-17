#!/usr/bin/env python3
"""ntag_seek.py — 找出这颗 I2C 双接口 NFC 标签的正确寻址方式

背景
    手机端标签工具读到：
        UID         1D D3 B4 D8 82 00 00
        技术        NFC-A NDEF
        Forum 类型  NFC_FORUM_TYPE_2
        可写        false   设置只读 true
        记录1       网址 URI  https://consumer.huawei.com/en/support/huaweisharewelcome/
        记录2       application/vnd.huawei.handoff.ndefmsg (MIME)

    "NFC Forum Type 2 Tag + I2C 双接口"是 NXP NTAG I2C plus 家族的标准特征，
    而 ACPI HID 就叫 NTAG0001。这类芯片的 I2C 侧是**块寻址**的：
        写:  S + ADDR_W + [block_addr] + ...
        读:  S + ADDR_W + [block_addr] + Sr + ADDR_R + [16 字节] + NAK + P
    地址字节 bit7 = I2C_LOCKED，bit6:0 = 块号（0x00..0xFF）。

    我们之前用的是「裸读」（不发地址字节），所以只能读到芯片当时的内部指针
    位置，读空之后设备就拉死 SCL → -110 ETIMEDOUT。这不是芯片坏了。

本脚本
    1. 扫 0x50..0x5F，看看到底有几个地址应答（判型号用）
    2. 按块号 0x00 读 16 字节 —— Type 2 的 block 0 是 UID
       预期拿到 1d d3 b4 … 开头，与手机报的 UID 对得上就证明寻址方式正确
    3. 逐块读出 0x00..0x3F，拼出完整内存镜像，再解 CC / TLV / NDEF

安全
    全程只发 READ 和「发地址字节」这种不含数据的写。
    手机端显示「设置只读 true」，本脚本**永不写数据**。

用法
    sudo python3 ntag_seek.py                 # 扫描 + 读 block 0 + 全镜像
    sudo python3 ntag_seek.py --scan-only     # 只扫地址
    sudo python3 ntag_seek.py --blocks 0:16   # 只读前 16 块
"""
import argparse
import ctypes
import fcntl
import sys
import time

I2C_RDWR = 0x0707
I2C_M_RD = 0x0001
EREMOTEIO = 121          # 地址无人应答
ETIMEDOUT = 110          # 地址 ACK 但设备拉死 SCL（总线已经不能用）

# Type 2 Tag 内存布局（NTAG I2C plus / NT3H2111 家族）
#   block 0x00: UID0..2 + BCC0
#   block 0x01: UID3..6
#   block 0x02: BCC1 + internal + lock0/1
#   block 0x03: 能力容器 CC（应为 E1 10 xx 00）
#   block 0x04: 用户内存起点，NDEF TLV（03 <len> <ndef> FE）

EXPECT_UID_PREFIX = bytes.fromhex("1dd3b4")   # 手机报 UID: 1D D3 B4 D8 82 00 00


class i2c_msg(ctypes.Structure):
    _fields_ = [
        ("addr", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16),
        ("buf", ctypes.POINTER(ctypes.c_uint8)),
    ]


class i2c_rdwr_ioctl_data(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(i2c_msg)), ("nmsgs", ctypes.c_uint32)]


class Bus:
    def __init__(self, nr=0):
        self.nr = nr
        self.f = open(f"/dev/i2c-{nr}", "rb+", buffering=0)
        self.fd = self.f.fileno()

    def _ioctl(self, msgs):
        arr = (i2c_msg * len(msgs))(*msgs)
        d = i2c_rdwr_ioctl_data(arr, len(msgs))
        fcntl.ioctl(self.fd, I2C_RDWR, d)

    def ping(self, addr):
        """只发地址不发数据，能 ACK 就算在。返回 True/False。"""
        buf = (ctypes.c_uint8 * 1)(0)
        m = i2c_msg(addr, 0, 0, buf)
        try:
            self._ioctl([m])
            return True
        except OSError:
            return False

    def write_bytes(self, addr, data):
        buf = (ctypes.c_uint8 * len(data))(*data)
        self._ioctl([i2c_msg(addr, 0, len(data), buf)])

    def read_plain(self, addr, n):
        """裸读：不发地址字节。"""
        buf = (ctypes.c_uint8 * n)()
        self._ioctl([i2c_msg(addr, I2C_M_RD, n, buf)])
        return bytes(buf)

    def read_block(self, addr, block, n=16, two_byte_addr=False):
        """块寻址读：写地址字节 + 重复起始 + 读 n 字节。"""
        ab = bytes([block >> 8, block & 0xFF]) if two_byte_addr else bytes([block & 0xFF])
        wbuf = (ctypes.c_uint8 * len(ab))(*ab)
        rbuf = (ctypes.c_uint8 * n)()
        self._ioctl([
            i2c_msg(addr, 0, len(ab), wbuf),
            i2c_msg(addr, I2C_M_RD, n, rbuf),
        ])
        return bytes(rbuf)


def hexdump(data, width=16, base=0):
    for off in range(0, len(data), width):
        row = data[off:off + width]
        hexs = " ".join(f"{b:02x}" for b in row)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        print(f"  {base + off:04x}  {hexs:<{width * 3}}  |{asc}|")


def health(bus, verbose=True):
    """总线健康检查：读一个确定不存在的地址，必须得到 EREMOTEIO(121)。
    如果得到 ETIMEDOUT(110)，说明控制器已经被上一次超时搞死，读数不可信。
    """
    try:
        bus.read_plain(0x60, 1)
        if verbose:
            print("  !! 0x60 竟然应答了 —— 地址表异常")
        return "odd"
    except OSError as e:
        code = e.errno or 0
        if code == EREMOTEIO:
            if verbose:
                print(f"  总线健康（对照组 0x60 → EREMOTEIO/{code}）")
            return "ok"
        if code == ETIMEDOUT:
            if verbose:
                print(f"  !! 总线已 wedged（对照组 0x60 → ETIMEDOUT/{code}）")
            return "wedged"
        if verbose:
            print(f"  ?? 对照组异常: errno={code} {e}")
        return "odd"


def scan(bus, lo=0x50, hi=0x60):
    print(f"\n== 扫描 0x{lo:02x}..0x{hi:02x} ==")
    found = []
    for a in range(lo, hi):
        if bus.ping(a):
            found.append(a)
            print(f"  0x{a:02x}  ACK")
    if not found:
        print("  一个都没应答")
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bus", type=int, default=0)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=None)
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--blocks", default="0:64", help="块范围，形如 0:64")
    ap.add_argument("--two-byte", action="store_true",
                    help="地址用 2 字节（部分大容量型号）")
    a = ap.parse_args()

    bus = Bus(a.bus)
    print(f"总线 /dev/i2c-{a.bus}")

    h = health(bus)
    if h == "wedged":
        print("\n总线已被上一次超时搞死。先恢复：")
        print("  sudo ~/.local/bin/fix_touchpad.sh   # 或项目里的 tools/fix_touchpad.sh")
        print("  或 reboot")
        return 1

    found = scan(bus)
    if a.scan_only:
        return 0

    addr = a.addr if a.addr else (found[0] if found else 0x57)
    print(f"\n使用从地址 0x{addr:02x}")

    # --- 第一步：读 block 0，验证块寻址是否成立 ---
    print("\n== 1. 块寻址验证：读 block 0x00（Type 2 的 UID 区）==")
    ok_scheme = None
    for label, two in (("单字节地址", False), ("双字节地址", True)):
        if two is False and a.two_byte:
            continue
        try:
            d = bus.read_block(addr, 0x00, 16, two_byte_addr=two)
        except OSError as e:
            print(f"  {label}: 失败 errno={e.errno} {e}")
            if (e.errno or 0) == ETIMEDOUT:
                print("  → 总线 wedged，后面的读数不可信，先恢复再重跑")
                return 1
            continue
        print(f"  {label}: {d.hex(' ')}")
        if d[:3] == EXPECT_UID_PREFIX:
            print(f"  ✅ UID 前缀匹配手机报的 1D D3 B4 —— 寻址方式就是「{label}」")
            ok_scheme = two
            break
        if any(d):
            print(f"  （非 UID 开头，但也不是全零，继续试）")

    if ok_scheme is None:
        print("\n  块寻址没直接对上 UID。退一步：先写地址 0x00，再裸读")
        try:
            bus.write_bytes(addr, b"\x00")
            time.sleep(0.01)
            d = bus.read_plain(addr, 16)
            print(f"  写0x00+裸读: {d.hex(' ')}")
            if d[:3] == EXPECT_UID_PREFIX:
                print("  ✅ 匹配")
                ok_scheme = False
        except OSError as e:
            print(f"  失败 errno={e.errno} {e}")

    # --- 第二步：全镜像 ---
    lo, _, hi = a.blocks.partition(":")
    lo, hi = int(lo, 0), int(hi or 64, 0)
    print(f"\n== 2. 全镜像：block 0x{lo:02x}..0x{hi - 1:02x} ==")
    img = bytearray()
    two = bool(ok_scheme) if ok_scheme is not None else a.two_byte
    for b in range(lo, hi):
        try:
            d = bus.read_block(addr, b, 16, two_byte_addr=two)
        except OSError as e:
            print(f"  block 0x{b:02x} 读失败 errno={e.errno} {e}")
            if (e.errno or 0) == ETIMEDOUT:
                print("  → 总线 wedged，中止")
                break
            img += b"\x00" * 16
            continue
        img += d
        print(f"  block 0x{b:02x}: {d.hex(' ')}")

    if img:
        print("\n== 3. 内存镜像 ==")
        hexdump(img, base=lo * 16)

        print("\n== 4. 结构解析 ==")
        cc = img[3 * 16:3 * 16 + 4]
        print(f"  能力容器 CC (block 3): {cc.hex(' ')}", end="")
        if cc[0] == 0xE1:
            print(f"  ✅ magic=E1 version={cc[1]:#04x} size={cc[2] * 8}B access=0x{cc[3]:02x}")
        else:
            print("  （不是标准 CC —— 块寻址可能还没对上）")

        # NDEF TLV 从 block 4 开始
        body = img[4 * 16:]
        i = 0
        print("  TLV 扫描:")
        while i < len(body):
            t = body[i]
            if t == 0x00:
                i += 1
                continue
            if t == 0xFE:
                print(f"    +0x{i:03x}  0xFE 终止符")
                break
            if i + 1 >= len(body):
                break
            ln = body[i + 1]
            if ln == 0xFF and i + 2 < len(body):
                ln = (body[i + 2] << 8) | body[i + 3]
                hdr = 4
            else:
                hdr = 2
            print(f"    +0x{i:03x}  TLV type=0x{t:02x} len={ln}")
            if t == 0x03:
                msg = body[i + hdr:i + hdr + ln]
                print(f"           NDEF 消息 {len(msg)}B:")
                hexdump(msg)
                walk_ndef(msg)
            i += hdr + ln

        # 顺便把可打印串拉出来
        print("\n  可打印串:")
        cur, start = bytearray(), 0
        for j, byte in enumerate(img):
            if 32 <= byte < 127:
                if not cur:
                    start = j
                cur.append(byte)
            else:
                if len(cur) >= 5:
                    print(f"    +0x{start:03x} ({len(cur):3d}B): {cur.decode('ascii')}")
                cur = bytearray()
        if len(cur) >= 5:
            print(f"    +0x{start:03x} ({len(cur):3d}B): {cur.decode('ascii')}")

    return 0


def walk_ndef(msg):
    """按 NFC Forum NDEF 解析：逐条记录打出来。"""
    i = 0
    n = 0
    while i < len(msg):
        if i + 2 > len(msg):
            break
        hdr = msg[i]
        mb = (hdr >> 7) & 1
        me = (hdr >> 6) & 1
        cf = (hdr >> 5) & 1
        sr = (hdr >> 4) & 1
        il = (hdr >> 3) & 1
        tnf = hdr & 0x07
        p = i + 1
        tlen = msg[p]
        p += 1
        if sr:
            plen = msg[p]
            p += 1
        else:
            if p + 4 > len(msg):
                break
            plen = int.from_bytes(msg[p:p + 4], "big")
            p += 4
        if il:
            idlen = msg[p]
            p += 1
        else:
            idlen = 0
        typ = msg[p:p + tlen]
        p += tlen
        rid = msg[p:p + idlen]
        p += idlen
        payload = msg[p:p + plen]
        p += plen
        n += 1
        print(f"      记录{n}: MB={mb} ME={me} CF={cf} SR={sr} IL={il} TNF={tnf} "
              f"type={typ!r} id={rid!r} len={plen}")
        if typ == b"U" and payload:
            prefix = {0x00: "", 0x01: "http://www.", 0x02: "https://www.",
                      0x03: "http://", 0x04: "https://", 0x05: "tel:",
                      0x06: "mailto:", 0x07: "ftp://anonymous:anonymous@",
                      0x08: "ftp://ftp.", 0x09: "ftps://", 0x0A: "sftp://",
                      0x0B: "smb://", 0x0C: "nfs://", 0x0D: "ftp://",
                      0x0E: "dav://", 0x0F: "news:", 0x10: "telnet://",
                      0x11: "imap:", 0x12: "rtsp://", 0x13: "urn:",
                      0x14: "pop:", 0x15: "sip:", 0x16: "sips:",
                      0x17: "tftp:", 0x18: "btspp://", 0x19: "btl2cap://",
                      0x1A: "btgoep://", 0x1B: "tcpobex://", 0x1C: "irdaobex://",
                      0x1D: "file://", 0x1E: "urn:epc:id:", 0x1F: "urn:epc:tag:",
                      0x20: "urn:epc:pat:", 0x21: "urn:epc:raw:",
                      0x22: "urn:epc:", 0x23: "urn:nfc:"}.get(payload[0], "?")
            print(f"        → URI: {prefix}{payload[1:].decode('utf-8', 'replace')}")
        elif typ == b"T" and payload:
            lang_len = payload[0] & 0x3F
            lang = payload[1:1 + lang_len].decode("ascii", "replace")
            text = payload[1 + lang_len:].decode("utf-8", "replace")
            print(f"        → TEXT [{lang}] {text}")
        elif tnf == 0x02:
            print(f"        → MIME {typ.decode('ascii', 'replace')}: {plen}B payload")
            hexdump(payload)
        i = p
        if me:
            print("        (ME 置位，消息结束)")
            break


if __name__ == "__main__":
    sys.exit(main())
