#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FM11NT082C 接触口读取器

芯片：上海复旦微电子 FM11NT082C-NTCI
      双界面 NFC Forum Type2 标签 + Channel 芯片
      8 Kbit EEPROM = 1024 字节 = 64 block x 16 byte
      接触口 I2C，从地址 0x57（配置字节 EE[0x03B3]）
      本机 ACPI：_SB.PC00.NFC (NTAG0001)，I2C0 @400kHz，GpioInt 上升沿 -> irq 213

## 设计原则：一次连续读 + 自校验定相位

实测（ntag_seek.py）：写 1 字节地址、写 2 字节地址、不写地址，三种方式
读到的都是内部指针当前位置之后的连续数据，写进去的地址字节被忽略；
单字节地址读与双字节地址读拿到的两段，正好是同一段连续流里相邻的 16 字节。
所以接触口在当前配置下是「自增指针 + 纯读」，不能寻址。

于是流程只有两步：

  1. 一次连续读满 2048 字节（一个 I2C 事务，没有任何块间边界，
     不会出现分块读那种块边界重复/丢字节的问题）。

  2. 相位未知，但 Type 2 Tag 头部有一组互相独立的不变量，只有相位正确
     时才会同时成立，用它们在 1024 个相位上扫描打分：

        EE 0x0000  UID0..UID3   第 4 字节 BCC0 = UID0^UID1^UID2^UID3
        EE 0x0004  UID3..UID6   第 9 字节 BCC1 = UID3^UID4^UID5^UID6
        EE 0x000C  CC 首字节    固定 0xE1 (MAGIC)
        EE 0x0390  UID 的 ASCII 表示 "1DD3B4D8"（本机实测布局）
        EE 0x0398  本芯片 I2C 从地址 0x57
        EE 0x03B7  厂商标识 "FMSH"
        EE 0x0010  NDEF TLV 起始

    两个 BCC 各自偶然命中的概率是 1/256，加上 CC 的 0xE1 与 0x0398，
    联合误判基本不可能。离线用 5 个不同起始相位验证，全部还原正确。

## 用法

  sudo python3 ntag_read.py                  # 读 2048 字节并自动解析
  sudo python3 ntag_read.py --len 4096       # 读更长，验证 1024 字节回卷
  sudo python3 ntag_read.py --save s.bin     # 保存原始流
  sudo python3 ntag_read.py --file s.bin     # 离线分析保存的流
  sudo python3 ntag_read.py --raw            # 只打原始流，不解析

  大读失败（超时）时可用 --chunk 32 降级为分块读做交叉验证，
  但分块读会引入块边界伪影，只用于确认芯片是否在吐数据。
"""

import argparse
import ctypes
import fcntl
import os
import sys
import time

I2C_RDWR = 0x0707
I2C_M_RD = 0x0001
EREMOTEIO = 121
ETIMEDOUT = 110

EEPROM_SIZE = 1024
I2C_ADDR = 0x57

# 手机端「标签工具」读到的真实 UID（7 字节），EE 0x0000 起
KNOWN_UID = bytes.fromhex("1dd3b4d8820000")
UID_ASCII = b"1DD3B4D8"          # EE 0x0390，本机实测布局

URI_PREFIXES = [
    "", "http://www.", "https://www.", "http://", "https://",
    "tel:", "mailto:", "ftp://anonymous:anonymous@", "ftp://ftp.",
    "ftps://", "sftp://", "smb://", "nfs://", "ftp://", "dav://",
    "news:", "telnet://", "imap:", "rtsp://", "urn:", "pop:",
    "sip:", "sips:", "tftp:", "bts//", "btl2cap://", "btgoep://",
    "tcpobex://", "irdaobex://", "file://", "urn:epc:id:",
    "urn:epc:tag:", "urn:epc:pat:", "urn:epc:raw:", "urn:epc:",
    "urn:nfc:",
]


class i2c_msg(ctypes.Structure):
    _fields_ = [
        ("addr", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16),
        ("buf", ctypes.POINTER(ctypes.c_uint8)),
    ]


class i2c_rdwr_ioctl_data(ctypes.Structure):
    _fields_ = [
        ("msgs", ctypes.POINTER(i2c_msg)),
        ("nmsgs", ctypes.c_uint32),
    ]


class Bus:
    def __init__(self, busnr, addr):
        self.addr = addr
        self.fd = os.open("/dev/i2c-%d" % busnr, os.O_RDWR)
        fcntl.ioctl(self.fd, 0x0703, addr)  # I2C_SLAVE

    def close(self):
        os.close(self.fd)

    def _run(self, m, buf):
        arr = (ctypes.c_uint8 * max(len(buf), 1)).from_buffer(buf) if buf else None
        m.buf = ctypes.cast(arr, ctypes.POINTER(ctypes.c_uint8)) if arr else None
        msgs = (i2c_msg * 1)(m)
        rd = i2c_rdwr_ioctl_data(ctypes.cast(msgs, ctypes.POINTER(i2c_msg)), 1)
        try:
            fcntl.ioctl(self.fd, I2C_RDWR, rd)
            return None
        except OSError as e:
            return e.errno

    def write1(self, byte, addr=None):
        m = i2c_msg()
        m.addr = addr if addr is not None else self.addr
        m.flags = 0
        m.len = 1
        return self._run(m, bytearray([byte]))

    def read(self, n):
        """当前地址读：不发地址字节，纯读 n 字节"""
        rbuf = bytearray(n)
        m = i2c_msg()
        m.addr = self.addr
        m.flags = I2C_M_RD
        m.len = n
        err = self._run(m, rbuf)
        return (None, err) if err else (bytes(rbuf), None)


def hexdump(data, base=0, width=16):
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hexs = " ".join("%02x" % b for b in chunk).ljust(width * 3 - 1)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print("  %04x  %s  |%s|" % (base + off, hexs, ascii_))


def health(bus):
    saved = bus.addr
    bus.addr = 0x60
    err = bus.write1(0x00)
    bus.addr = saved
    if err is None:
        print("  对照组 0x60 竟然 ACK —— 总线状态可疑")
        return False
    if err == EREMOTEIO:
        print("  对照组 0x60 -> NAK(121)   总线健康")
        return True
    if err == ETIMEDOUT:
        print("  对照组 0x60 -> 超时(110)   适配器卡死，读数不可信")
        print("     复位：sudo modprobe -r ntag_nfc && sudo modprobe ntag_nfc")
        return False
    print("  对照组 0x60 -> errno=%r" % (err,))
    return False


def probe_sizes(fd):
    """探测 /dev/ntag_nfc 单次事务的最大可读字节数。

    为什么不直接一次读 1024：手册 2.2.3 写明「双接口共享 32 字节 FIFO」。
    若接触口处于 NC(Channel) 模式，单次读一旦超过 FIFO 容量，
    芯片就会拉住 SCL 不放，表现为 -110 ETIMEDOUT，整条总线随之卡死。
    所以必须先定界，再决定每次读多少。

    探测顺序从小到大，遇到第一个失败即停（失败后适配器不可信）。
    """
    print("\n== 单次事务可读字节数探测 ==")
    print("  %6s  %-22s %s" % ("字节", "结果", "内容 / 耗时"))
    ok_max, wedged = 0, False
    for n in (1, 2, 4, 8, 12, 15, 16, 17, 20, 24, 28, 31, 32, 33, 48, 64):
        t0 = time.time()
        try:
            d = os.read(fd, n)
            ms = (time.time() - t0) * 1000
            if d:
                print("  %6d  OK 读到 %-3d 字节      %.0f ms   %s"
                      % (n, len(d), ms, d[:16].hex(" ")))
                ok_max = max(ok_max, len(d))
            else:
                print("  %6d  读到 0 字节           %.0f ms" % (n, ms))
        except OSError as e:
            ms = (time.time() - t0) * 1000
            err = getattr(e, "errno", "?")
            print("  %6d  失败 errno=%-4s      %.0f ms" % (n, err, ms))
            if err == ETIMEDOUT:
                wedged = True
                print("        -> 已超时，停止探测（此后适配器不可信）")
                break

    print("\n  单次事务最大成功字节数 = %d" % ok_max)
    if ok_max == 0:
        print("""
  一个字节都没读到。按可能性排序：
    1. 芯片内部指针已经读到 EEPROM 末尾，接触口不再吐数据
       （手册 4.1.3.2：读操作后的停止条件才让 EEPROM 回到等待；
         而本机写地址不生效，也就无法把指针拨回去）
    2. 芯片正被 RF 场占用（手机贴在感应区上）
    3. 芯片处于 NC 模式且 FIFO 已空
  建议：把手机贴近 NFC 区域再立刻重跑本探测（RF 场可能重置 FIFO），
        或 sudo modprobe -r ntag_nfc && sudo modprobe ntag_nfc 复位控制器""")
    elif ok_max < 32:
        print("  注意：上限 %d 字节 < 32，与手册的 32 字节 FIFO 一致，"
              "说明接触口在 NC(Channel) 模式。\n"
              "  后续读取必须按这个尺寸分块，且每块之间可能有边界伪影。"
              % ok_max)
    else:
        print("  上限达到 %d 字节，接近或超过 FIFO 容量，接触口可能在 NT 模式。" % ok_max)

    if wedged:
        print("\n  适配器已 wedged，复位后重试：")
        print("    sudo modprobe -r ntag_nfc")
        print("    sudo insmod ~/Projects/lib/huawei-ntag0001-nfc/driver/ntag_nfc.ko probe_on_load=0")
    return ok_max


def self_check(img):
    """Type 2 Tag 自校验。返回 (得分, 命中的不变量列表)。"""
    score, notes = 0, []
    if img[0] ^ img[1] ^ img[2] ^ img[3] == 0:
        score += 5
        notes.append("BCC0")
    if img[4] ^ img[5] ^ img[6] ^ img[7] ^ img[8] == 0:
        score += 5
        notes.append("BCC1")
    if img[0x0C] == 0xE1:
        score += 4
        notes.append("CC=0xE1")
    if img[0x398] == I2C_ADDR:
        score += 4
        notes.append("EE398=0x57")
    if img[0x390:0x398] == UID_ASCII:
        score += 4
        notes.append("UID-ASCII@0x390")
    if img[0x3B7:0x3BB] == b"FMSH":
        score += 3
        notes.append("FMSH@0x3B7")
    if img[0x10] == 0x03:
        score += 2
        notes.append("TLV@0x10=NDEF")
    if img[0:3] == KNOWN_UID[:3]:
        score += 3
        notes.append("UID 匹配")
    return score, notes


def solve(stream):
    """在 1024 个相位上扫描，返回自校验得分最高的镜像。

    返回 (img, score, notes, phase) 或 None
    """
    if len(stream) < EEPROM_SIZE:
        return None
    doubled = stream + stream[:EEPROM_SIZE]
    best = None
    for ph in range(EEPROM_SIZE):
        img = doubled[ph:ph + EEPROM_SIZE]
        score, notes = self_check(img)
        if best is None or score > best[1]:
            best = (img, score, notes, ph)
            if score >= 30:          # 满分，不可能更好
                break
    return best


def find_period(buf, maxp=4096):
    """找最小周期 p（用于确认 EEPROM 1024 字节回卷）"""
    n = len(buf)
    for p in range(16, min(maxp, n // 2) + 1):
        hits = sum(1 for i in range(n - p) if buf[i] == buf[i + p])
        if hits > 0.98 * (n - p):
            return p
    return None


# ----------------------------------------------------------------- 解析

def parse_ndef_msg(msg):
    i = 0
    recno = 0
    while i + 2 <= len(msg):
        hdr = msg[i]
        tnf = hdr & 0x07
        sr = hdr & 0x10
        il = hdr & 0x08
        me = hdr & 0x40
        i += 1
        tlen = msg[i]
        i += 1
        if sr:
            if i >= len(msg):
                break
            plen = msg[i]
            i += 1
        else:
            if i + 4 > len(msg):
                break
            plen = int.from_bytes(msg[i:i + 4], "big")
            i += 4
        idlen = 0
        if il:
            idlen = msg[i]
            i += 1
        typ = msg[i:i + tlen]
        i += tlen
        if il:
            i += idlen
        payload = msg[i:i + plen]
        i += plen
        recno += 1
        print("      记录%d  TNF=%d  %s  type=%r  payload=%d 字节"
              % (recno, tnf, "SR" if sr else "长记录", typ, len(payload)))
        if tnf == 1 and typ == b"U" and payload:
            p = payload[0]
            pre = URI_PREFIXES[p] if p < len(URI_PREFIXES) else "(前缀 0x%02x)" % p
            print("        URI: %s%s" % (pre, payload[1:].decode("latin1", "replace")))
        elif tnf == 1 and typ == b"T" and payload:
            st = payload[0]
            ln = st & 0x3F
            enc = "utf-8" if (st & 0x80) else "utf-16"
            print("        文本(%s): %s"
                  % (enc, payload[1 + ln:].decode(enc, "replace")))
        elif tnf == 2:
            print("        MIME type: %s" % typ.decode("latin1", "replace"))
            print("        载荷 %d 字节:" % len(payload))
            hexdump(payload, 0)
        else:
            print("        原始载荷: %s" % payload[:64].hex(" "))
        if me:
            break
    if i < len(msg):
        print("      (记录后剩 %d 字节)" % (len(msg) - i))


def parse_tlv(img, limit=0x388):
    off = 0x10
    print("  == TLV（EE 0x0010 起）==")
    while off < limit - 1:
        t = img[off]
        if t == 0x00:
            off += 1
            continue
        if t == 0xFE:
            print("    0x%04x  NULL 终止符" % off)
            break
        if t == 0x03:
            ln = img[off + 1]
            if ln == 0xFF:
                ln = (img[off + 2] << 8) | img[off + 3]
                body = off + 4
            else:
                body = off + 2
            print("    0x%04x  NDEF TLV  len=%d  body@0x%04x" % (off, ln, body))
            parse_ndef_msg(bytes(img[body:body + ln]))
            off = body + ln
            continue
        if t in (0x01, 0x02):
            ln = img[off + 1]
            print("    0x%04x  %s TLV  len=%d"
                  % (off, "Lock Control" if t == 0x01 else "Memory Control", ln))
            off += 2 + ln
            continue
        print("    0x%04x  未知 TLV 0x%02x（0xFE 之前出现异常值）" % (off, t))
        break


def analyse(img):
    print("\n== 结构解析（已对齐到 EE 0x0000）==")
    print("  EE 0x0000 UID      : %s" % img[0:7].hex(" "))
    print("    BCC0 = %02x  XOR(UID0..3) = %02x  %s"
          % (img[3], img[0] ^ img[1] ^ img[2] ^ img[3],
             "通过" if img[0] ^ img[1] ^ img[2] ^ img[3] == 0 else "不符"))
    print("    BCC1 = %02x  XOR(UID3..6) = %02x  %s"
          % (img[8], img[4] ^ img[5] ^ img[6] ^ img[7] ^ img[8],
             "通过" if img[4] ^ img[5] ^ img[6] ^ img[7] ^ img[8] == 0 else "不符"))
    print("  EE 0x000A 内部/锁位 : %s" % img[10:12].hex(" "))
    cc = img[0x0C:0x10]
    print("  EE 0x000C CC       : %s" % cc.hex(" "))
    if cc[0] == 0xE1:
        print("    -> 合法 Type 2 CC：版本 %d.%d，容量 0x%02x*8 = %d 字节"
              % (cc[1] >> 4, cc[1] & 0x0F, cc[2], cc[2] * 8))
    else:
        print("    -> 不是 0xE1")

    print("\n  == 头部 0x00..0x7F ==")
    hexdump(img[0:0x80], 0)

    parse_tlv(img)

    print("\n  == 配置区 0x388..0x39F ==")
    hexdump(img[0x388:0x3A0], 0x388)
    print("    0x0390 = %r（UID 的 ASCII）" % img[0x390:0x398])
    print("    0x0398 = 0x%02x（I2C 从地址）" % img[0x398])
    print("    0x03B3 = 0x%02x（手册规定的 I2C 地址配置字节）" % img[0x3B3])
    print("    0x03B7 = %r（厂商标识）" % img[0x3B7:0x3BC])

    print("\n  == 接触口锁位 0x3C0..0x3C7 ==")
    hexdump(img[0x3C0:0x3C8], 0x3C0)
    print("\n  == 尾部 0x3E0..0x3FF ==")
    hexdump(img[0x3E0:0x400], 0x3E0)

    print("\n  == 全部可打印串 ==")
    cur, start = b"", 0
    for i in range(len(img) + 1):
        b = img[i] if i < len(img) else 0
        if 32 <= b < 127:
            if not cur:
                start = i
            cur += bytes([b])
        else:
            if len(cur) >= 4:
                print("    +0x%03x (%2dB): %s"
                      % (start, len(cur), cur.decode("latin1")))
            cur = b""


def fetch(bus, want, chunk=0):
    """读 want 字节。chunk=0 优先一次连续读（唯一事务，最干净）。

    返回 (data, note) 或 (None, errno)
    """
    if chunk > 0:
        buf = bytearray()
        while len(buf) < want:
            d, err = bus.read(chunk)
            if d is None:
                return None, err
            buf += d
        return bytes(buf[:want]), "分块 %d 字节 x %d 次（有块边界，仅供参考）" \
            % (chunk, (want + chunk - 1) // chunk)

    for n in (want, 1024, 512, 256, 128, 64, 32):
        if n < 1 or n > want:
            continue
        d, err = bus.read(n)
        if d is not None:
            return d, "一次连续读 %d 字节" % n
        print("  %4d 字节失败 errno=%r" % (n, err))
        if err == ETIMEDOUT:
            return None, err
    return None, None


def main():
    ap = argparse.ArgumentParser(description="FM11NT082C 读取器（连续读 + 自校验定相位）")
    ap.add_argument("--bus", type=int, default=0)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=I2C_ADDR)
    ap.add_argument("--len", type=int, default=2048,
                    help="读取长度，默认 2048（两圈，便于验证 1024 回卷）")
    ap.add_argument("--chunk", type=int, default=0,
                    help="分块读每次字节数，0=一次连续读（推荐，唯一事务）")
    ap.add_argument("--raw", action="store_true", help="只打原始流，不做解析")
    ap.add_argument("--save", metavar="PATH", help="保存原始流")
    ap.add_argument("--file", metavar="PATH", help="离线分析之前保存的流")
    ap.add_argument("--dev", metavar="PATH", default=None,
                    help="从内核模块字符设备读；模块已绑定时用 --dev /dev/ntag_nfc")
    ap.add_argument("--probe", action="store_true",
                    help="配合 --dev：探测单次事务的最大可读字节数后退出")
    args = ap.parse_args()

    if args.file:
        data = open(args.file, "rb").read()
        how = "离线文件 %s" % args.file
        print("%s（%d 字节）" % (how, len(data)))
    elif args.dev:
        # 走内核模块的 /dev/ntag_nfc：模块已绑定 ACPI client，
        # 此时用户态不能对 0x57 发 I2C_SLAVE（会 EBUSY），所以用模块的读接口
        print("设备 %s" % args.dev)
        try:
            fd = os.open(args.dev, os.O_RDONLY)
        except OSError as e:
            print("  打不开 %s: %s" % (args.dev, e))
            print("  先装载模块：sudo insmod driver/ntag_nfc.ko probe_on_load=0")
            return 1
        try:
            if args.probe:
                probe_sizes(fd)
                return 0
            chunk = args.chunk if args.chunk > 0 else 16
            print("  每次连续读 %d 字节（--chunk 可改；上限受芯片 FIFO 限制）" % chunk)
            buf = bytearray()
            t0 = time.time()
            while len(buf) < args.len:
                try:
                    part = os.read(fd, min(chunk, args.len - len(buf)))
                except OSError as e:
                    print("  偏移 0x%04x 处读失败: %s" % (len(buf), e))
                    if getattr(e, "errno", None) == ETIMEDOUT:
                        print("  -> 超过 FIFO 容量或指针已到末尾。"
                              "先用 --probe 定界，或换更小的 --chunk")
                    break
                if not part:
                    print("  偏移 0x%04x 处读到 0 字节（设备无数据）" % len(buf))
                    break
                buf += part
                if time.time() - t0 > 30:
                    print("  超时防护触发（30 秒），已读 %d 字节" % len(buf))
                    break
        finally:
            os.close(fd)
        data = bytes(buf)
        how = "内核模块 %s，每次 %d 字节" % (args.dev, chunk)
        print("  成功读到 %d 字节（%s）" % (len(data), how))
        if not data:
            print("\n  没读到任何数据，不进行解析。先跑：")
            print("    sudo python3 ntag_read.py --dev %s --probe" % args.dev)
            return 2
        if args.save:
            with open(args.save, "wb") as f:
                f.write(data)
            print("  已保存到 %s" % args.save)

    else:
        print("总线 /dev/i2c-%d   从地址 0x%02x" % (args.bus, args.addr))
        bus = Bus(args.bus, args.addr)
        try:
            print("\n== 0. 对照组 ==")
            if not health(bus):
                return 1
            print("\n== 1. 读 %d 字节 ==" % args.len)
            data, how = fetch(bus, args.len, args.chunk)
        finally:
            bus.close()
        if data is None:
            if how == ETIMEDOUT:
                print("  适配器卡死。复位后重试：")
                print("    sudo modprobe -r ntag_nfc && sudo modprobe ntag_nfc")
                return 2
            print("  一个长度都没读成功 —— 芯片可能在等 RF 事件，指针无数据可吐")
            return 2
        print("  成功读到 %d 字节（%s）" % (len(data), how))
        if args.save:
            with open(args.save, "wb") as f:
                f.write(data)
            print("  已保存到 %s" % args.save)
    print("\n== 2. 原始流前 64 字节 ==")
    hexdump(data[:64], 0)

    if args.raw:
        print("\n== 原始流全文 ==")
        hexdump(data, 0)
        return 0

    print("\n== 3. 周期检测（1024 回卷）==")
    p = find_period(data)
    if p:
        print("  最小周期 = %d 字节%s"
              % (p, "  = EEPROM 容量，回卷确认"
                 if p == EEPROM_SIZE else "（不等于 1024，注意）"))
    else:
        print("  未找到周期 —— 读得不够长，或未回卷")

    print("\n== 4. 相位求解（1024 相位，按 Type 2 自校验打分）==")
    best = solve(data)
    if best is None:
        print("  只有 %d 字节，不足一圈，无法求解" % len(data))
        return 3
    img, score, notes, phase = best
    print("  最优相位 0x%04x（原始流[0x%04x] 对应 EE 0x0000）"
          % (phase, phase))
    print("  得分 %d / 30：%s" % (score, "、".join(notes) if notes else "无"))

    print("\n  == 镜像前 64 字节 ==")
    hexdump(img[:64], 0)

    analyse(img)

    print("\n== 5. 自检 ==")
    ok = score >= 20
    print("  得分 %d / 30  ->  %s"
          % (score, "数据完整，相位正确" if ok else "存在不一致"))
    if not ok:
        print("  建议：--len 4096 多读一圈再试；或先确认手机未在读的同时触发 RF 事件")
    return 0 if ok else 4


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("提示：需要 root（或加入 i2c 组）", file=sys.stderr)
    sys.exit(main())
