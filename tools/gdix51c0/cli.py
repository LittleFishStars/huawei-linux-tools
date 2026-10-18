# SPDX-License-Identifier: GPL-2.0-or-later
"""``gdix51c0`` 命令行工具。

子命令一览：

    info        打印设备与驱动信息（不需要 PSK）
    diag        裸总线诊断：看 MISO、中断速率与复位效果
    pin         逐个试 GpioIo 的真实用途（复位/使能/片选）
    otp         打印芯片 ID、OTP 摘要与自校准 DAC
    provision   生成并部署一个新 PSK（破坏性，会覆盖传感器里的旧密钥）
    capture     采一帧指纹并存成 PGM/PNG
    watch       循环等手指，采到就存
    selftest    纯软件自检（不需要硬件）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from . import DEVICE_NODE, __version__, config, image, psk, wire
from .session import DEFAULT_PSK_STATE, Session, SessionError
from .transport import KernelDevice, TransportError


def _hexbytes(data: bytes, limit: int = 32) -> str:
    head = data[:limit].hex(" ")
    return head + (" …" if len(data) > limit else "")


# ---- 子命令 --------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    try:
        dev = KernelDevice(args.device)
        info = dev.info()
    except TransportError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"设备      {args.device}")
    print(f"ACPI HID  {info.hid}")
    print(f"IRQ       {info.irq}")
    print(f"复位 GPIO {'有' if info.reset_valid else '无'}"
          f"{'（低有效）' if info.reset_active_low else ''}")
    print(f"中断 GPIO {'有' if info.irq_gpio_valid else '无'}")
    print(f"SPI 速率  {info.speed_hz} Hz")
    print(f"分块大小  {info.chunk} 字节")
    print(f"中断计数  {dev.irq_count()}")
    # 中断电平需要 irq_gpio 被解析出来；没有这个资源时 ioctl 会返回 ENODEV，
    # 不是错误，只是这个功能不可用。
    if info.irq_gpio_valid:
        print(f"中断电平  {'高' if dev.irq_level() else '低'}")
    else:
        print("中断电平  不可用（_CRS 里没有可读的 GpioIo）")
    # 复位脚是必需项：拿不到时把 _CRS 全量打出来定位根因。
    # irq_gpio 是可选调试功能（默认 -1 不启用），不在此列。
    if not info.reset_valid:
        _print_missing_gpio_hint(info)
        _dump_crs(args.device)
    elif getattr(args, "verbose", False):
        _dump_crs(args.device)
    dev.close()


def _dump_crs(device: str) -> None:
    """读驱动导出的 _CRS 转储（sysfs resources 属性）。"""
    import glob

    # 设备路径 -> sysfs 属性。传进来的是 /dev/gdix51c0，不能直接拼。
    cands = glob.glob(
        "/sys/bus/spi/devices/spi-GDIX51C0:00/resources"
    ) + glob.glob("/sys/class/misc/gdix51c0/device/resources")
    if not cands:
        print("\n（找不到 _CRS 转储属性，可能驱动不是最新版）", file=sys.stderr)
        return
    try:
        with open(cands[0], encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"\n（读 _CRS 转储失败：{exc}）", file=sys.stderr)
        return
    print("\n== _CRS 实际声明的资源 ==")
    print(text.rstrip())


def _print_missing_gpio_hint(info) -> None:
    """复位脚拿不到时给出下一步的排查建议。"""
    print()
    if not info.reset_valid:
        print(
            "提示：_CRS 里没有可用的 GpioIo（复位脚）。这不一定会阻止采图：\n"
            "  传感器在收到 RESET 命令（0xa2）后会自己软复位。\n"
            "  先用下面这条看 _CRS 里到底有哪些 GPIO 资源：\n"
            "    cat /sys/bus/spi/devices/spi-GDIX51C0:00/resources\n"
            "  [] 里的数字就是资源序号，用模块参数指定：\n"
            "    sudo modprobe gdix51c0_fp reset_gpio_index=<序号>"
        )


def cmd_otp(args: argparse.Namespace) -> int:
    with Session(device_path=args.device) as sess:
        # 走完整的激活流程：_boot_probe 是会话第一步，跳过它 0xa8 会静默
        sess.start()
        chip_id = sess.sensor.chip_id
        otp = sess.sensor.otp
        dac = sess._dac_from_otp(otp)
    print(f"芯片 ID    0x{chip_id:04x}")
    print(f"OTP 长度   {len(otp)} 字节")
    print(f"OTP 前 32  {_hexbytes(otp, 32)}")
    print(f"自校准 DAC main=0x{dac[0]:04x} d1=0x{dac[1]:02x} "
          f"d2=0x{dac[2]:02x} d3=0x{dac[3]:02x}")
    return 0


def cmd_provision(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "这会覆盖传感器里现有的 PSK，之后 Windows 侧与其它主机都要重新部署。\n"
            "确认请加 --yes。",
            file=sys.stderr,
        )
        return 2
    key = bytes.fromhex(args.psk_hex) if args.psk_hex else psk.random_psk()
    if len(key) != psk.PSK_LEN:
        print(f"错误：PSK 必须是 32 字节（64 个十六进制字符）", file=sys.stderr)
        return 2
    with Session(device_path=args.device, psk_state_file=args.state) as sess:
        sess.start(psk_bytes=key)
        sess.provision_psk(key)
    print(f"已部署新 PSK，写入 {args.state}")
    print(f"PSK = {key.hex()}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    with Session(device_path=args.device, psk_state_file=args.state) as sess:
        sess.start()
        if args.wait:
            print("请按手指…", file=sys.stderr)
            if not sess.arm_finger_detect(args.wait_ms):
                print("等待手指超时", file=sys.stderr)
                return 1
        frame = sess.capture(finger=True)
        return _save_frame(frame, args, sess)


def cmd_watch(args: argparse.Namespace) -> int:
    saved = 0
    with Session(device_path=args.device, psk_state_file=args.state) as sess:
        sess.start()
        print("持续等待手指，Ctrl-C 退出", file=sys.stderr)
        while saved < args.count:
            try:
                if not sess.arm_finger_detect(args.wait_ms):
                    continue
                frame = sess.capture(finger=True)
            except SessionError as exc:
                print(f"采图失败：{exc}", file=sys.stderr)
                continue
            args.output = _numbered(args.output, saved)
            _save_frame(frame, args, sess, quiet=True)
            saved += 1
    return 0


def _numbered(template: str, index: int) -> str:
    root, ext = os.path.splitext(template)
    return f"{root}-{index:02d}{ext}"


def _save_frame(frame: image.Frame, args: argparse.Namespace, sess: Session, quiet: bool = False) -> int:
    stats = frame.stats()
    out = args.output
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    if out.lower().endswith(".png"):
        _save_png(frame, out)
    else:
        frame.save_pgm(out, invert=args.invert)
    if not quiet:
        print(
            f"已保存 {out}  {frame.width}×{frame.height}  "
            f"min={stats['min']:.0f} max={stats['max']:.0f} "
            f"mean={stats['mean']:.0f} std={stats['std']:.1f}"
        )
        if args.ascii:
            print(frame.to_ascii(args.ascii_cols))
    return 0


def _save_png(frame: image.Frame, path: str) -> None:
    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit(
            "导出 PNG 需要 python-pillow；或者把输出后缀改成 .pgm"
        ) from exc
    img = Image.frombytes(
        "L", (frame.width, frame.height), frame.to_pgm().split(b"\n", 3)[3]
    )
    img.save(path)


def cmd_selftest(args: argparse.Namespace) -> int:
    """纯软件自检：组帧、白盒 PSK、配置校验和、RAW12 解码。"""
    failures: list[str] = []

    def check(name: str, fn) -> None:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as exc:  # noqa: BLE001 - 自检要把所有失败都收齐
            failures.append(f"{name}: {exc}")
            print(f"  FAIL {name}: {exc}")

    check("wire 组帧往返", _test_wire_roundtrip)
    check("wire 官方前缀帧", _test_wire_prefix01)
    check("白盒 PSK 往返与篡改拒绝", psk.selftest)
    check("白盒 PSK 官方向量", _test_psk_official_vector)
    check("MCU 配置校验和", config.selftest)
    check("RAW12 解码索引", image.selftest)
    check("帧容量", image.frame_capacity_check)
    check("TLS record 头识别", _test_tls_header)

    if failures:
        print(f"\n{len(failures)} 项失败", file=sys.stderr)
        return 1
    print("\n全部通过")
    return 0


def cmd_diag(args: argparse.Namespace) -> int:
    """裸总线诊断：看 MISO 上到底回了什么。

    初始化失败时先跑这个：能看到复位前后的 IRQ 计数、发出去的字节、
    以及 MISO 是真 0xff（总线空）还是别的东西（时序/极性不对）。
    """
    from . import proto as _proto
    from . import wire as _wire

    dev = KernelDevice(args.device)
    try:
        info = dev.info()
        print(f"ACPI HID   {info.hid}")
        print(f"IRQ        {info.irq}")
        print(f"复位 GPIO  {'有' if info.reset_valid else '无'}")

        def rate(label: str, seconds: float = 0.5) -> float:
            """测中断速率。恒定的高速率 = 电平触发风暴。/秒"""
            a = dev.irq_count()
            time.sleep(seconds)
            b = dev.irq_count()
            r = (b - a) / seconds
            print(f"  {label}  {a} → {b}  约 {r:.0f} 次/秒")
            return r

        # 内核里实际生效的中断配置（sysfs 里那张表是 ACPI 声明的，
        # 未必等于 request_irq 之后真正生效的类型）
        stale_module = False
        line_irqinfo = ""
        for extra in ("irqinfo", "info"):
            path = f"/sys/bus/spi/devices/spi-GDIX51C0:00/{extra}"
            try:
                with open(path) as fp:
                    text = fp.read()
                if extra == "irqinfo":
                    line_irqinfo = text
                for line in text.splitlines():
                    print(f"[{extra}] {line}")
            except FileNotFoundError:
                if extra == "irqinfo":
                    stale_module = True
                print(f"[{extra}] 读不到（属性不存在）")
            except OSError as exc:
                print(f"[{extra}] 读不到（{exc}）")

        # 已加载模块 vs 磁盘上的 ko：srcversion 不同就说明内存里跑的是旧版。
        # modprobe -r 失败时 modprobe 是幂等的、会静默成功，很容易误判。
        loaded_sv = None
        try:
            with open("/sys/module/gdix51c0_fp/srcversion") as fp:
                loaded_sv = fp.read().strip()
        except OSError:
            pass
        disk_sv = None
        try:
            out = subprocess.run(
                ["modinfo", "-F", "srcversion",
                 f"/lib/modules/{os.uname().release}/updates/dkms/"
                 "gdix51c0_fp.ko.zst"],
                capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                disk_sv = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

        if loaded_sv:
            print(f"[已加载 srcversion] {loaded_sv}")
        if disk_sv:
            print(f"[磁盘上 srcversion] {disk_sv}")
        if loaded_sv and disk_sv and loaded_sv != disk_sv:
            stale_module = True
            print("  ！内存里的模块与磁盘上的不是同一份")

        if stale_module:
            print()
            print("!! 当前加载的不是新版本的模块，下面的结果没有参考价值。")
            print("   注意：modprobe -r 失败时 modprobe 是幂等的、会静默成功，")
            print("   所以必须确认旧模块真的卸载了：")
            print("     sudo rmmod gdix51c0_fp")
            print("     ls /sys/module/gdix51c0_fp 2>/dev/null   # 必须无输出")
            print("     sudo modprobe gdix51c0_fp")
            print("     ls /sys/bus/spi/devices/spi-GDIX51C0:00/ | grep irqinfo")
            return 2

        print("\n-- 空闲时的中断速率 --")
        idle_rate = rate("静止 0.5s")
        if idle_rate > 100:
            # 若触发类型已是 edge 而速率仍上千，那就不是"配成 level 造成的
            # 重触发"，而是线上真的有海量边沿 —— 浮空引脚被噪声触发的典型
            # 特征（传感器未上电时 INT 会浮空、被内部上拉拉高）。
            is_edge = "EDGE" in line_irqinfo
            if is_edge:
                print("  ！触发类型已是 EDGE，但仍有上千次/秒的边沿。")
                print("     这根线在真实翻转，不是软件重触发。结合 MISO 全 ff，")
                print("     最可能是传感器未上电、INT 引脚浮空被噪声触发。")
            else:
                print("  ！触发类型是 LEVEL，空闲时就在飙 = 电平重触发。")
                print("     用 irq_edge_trigger=1 重新加载模块。")

        def idle_read(n: int = 16) -> str:
            return dev.xfer(b"\xff" * n, n, 0).hex()

        print("\n-- 复位前 --")
        try:
            print(f"  空闲 MISO  {idle_read()}")
        except Exception as exc:  # noqa: BLE001 - 诊断要尽量往下走
            print(f"  读取失败：{exc}")
            print("  传输通道本身不通，后面的时序测试已跳过。")
            print("  先确认：cat /sys/bus/spi/devices/spi-GDIX51C0:00/"
                  "power/runtime_error")
            return 1

        # SPI 控制器自己记的传输统计：消息数涨、错误数不涨 = 控制器认为
        # 传输成功。这条能把"控制器没在发"和"发了但设备不理"分开。
        # SPI 控制器的 runtime PM 状态。挂起时 spi_sync 仍然记账但时钟
        # 不真正输出，MISO 恒为 0xff —— 看起来像设备不应答。
        print("\n-- SPI 控制器电源状态 --")
        for label, path in (
            ("设备 spi_pm", "/sys/bus/spi/devices/spi-GDIX51C0:00/info"),
            ("控制器", "/sys/devices/pci0000:00/0000:00:1e.2/power/runtime_status"),
            ("控制器 control", "/sys/devices/pci0000:00/0000:00:1e.2/power/control"),
        ):
            try:
                with open(path) as fp:
                    print(f"  {label:14} {fp.read().strip()}")
            except OSError as exc:
                print(f"  {label:14} 读不到（{exc}）")

        print("\n-- SPI 控制器统计 --")
        stat_dir = "/sys/bus/spi/devices/spi-GDIX51C0:00/statistics"
        try:
            os.listdir(stat_dir)
        except OSError as exc:
            print(f"  读不到（{exc}）")
        else:
            for name in ("messages", "bytes", "bytes_tx", "bytes_rx",
                         "transfers", "errors", "timedout"):
                try:
                    with open(f"{stat_dir}/{name}") as fp:
                        print(f"  {name:10} {fp.read().strip()}")
                except OSError:
                    pass

        # /proc/interrupts 是内核自己记的到计数，与我们的 handler 无关。
        # 两者对照能区分"硬件/上游在计数"和"我们的 handler 在重入"。
        print("\n-- 内核中断计数（/proc/interrupts）--")
        try:
            with open("/proc/interrupts") as fp:
                for line in fp:
                    if "gdix51c0" in line:
                        print(f"  {line.strip()}")
                    elif line.split(":")[0].strip() == str(info.irq):
                        print(f"  {line.strip()}")
        except OSError as exc:
            print(f"  读不到（{exc}）")

        print("\n-- 中断脚电平（连续采样，看线是不是一直拉着）--")
        try:
            samples = []
            for _ in range(10):
                samples.append(dev.irq_level())
                time.sleep(0.05)
            print(f"  10 次采样（每 50ms）= {samples}")
            if all(v == 1 for v in samples):
                print("  线一直 active：MCU 在持续请求，或者线是浮空的噪声源")
            elif all(v == 0 for v in samples):
                print("  线一直 inactive：MCU 什么都没说")
            else:
                print("  线在翻转：是真信号（不是浮空噪声）")
        except OSError as exc:
            print(f"  读不到 IRQ 电平（{exc}）")

        print("\n-- 硬复位 --")
        before = dev.irq_count()
        dev.reset(10_000)
        time.sleep(0.2)
        after = dev.irq_count()
        delta = after - before
        print(f"  中断计数   {before} → {after}（差 {delta}）")
        if delta == 0:
            print("  ！复位后没有启动脉冲：MCU 可能未被复位，或中断脚不可用")
        print(f"  复位后 MISO {idle_read()}")
        rate("复位后静止 0.5s", 0.5)

        print("\n-- prefix01 + 0xa8 --")
        dev.xfer(_wire.encode_outer_header(_wire.OUTER_WRITE, len(_proto.PREFIX01)), 0)
        dev.xfer(_proto.PREFIX01, 0)
        time.sleep(0.010)
        a8 = _proto.cmd_firmware_version()
        dev.xfer(_wire.encode_outer_header(_wire.OUTER_WRITE, len(a8)), 0)
        dev.xfer(a8, 0)
        for i in range(8):
            time.sleep(0.050)
            print(f"  +{(i + 1) * 50:3d}ms  中断={dev.irq_count():3d}  MISO={idle_read(32)}")

        print("\n-- 0xd5 force-unlock-tls --")
        d5 = _proto.cmd_force_unlock_tls()
        dev.xfer(_wire.encode_outer_header(_wire.OUTER_WRITE, len(d5)), 0)
        dev.xfer(d5, 0)
        for i in range(4):
            time.sleep(0.050)
            print(f"  +{(i + 1) * 50:3d}ms  中断={dev.irq_count():3d}  MISO={idle_read(32)}")
    finally:
        dev.close()

    print("\n诊断结论：")
    print("  IRQ 在 /proc/interrupts 里计数狂涨 = 硬件层在产生边沿，与我们的")
    print("  handler 无关（该计数在 handler 之前累加）。")
    print("  若 trig 已是 EDGE 且电平恒为 1，说明这根线在浮空、被噪声触发；")
    print("  配合 MISO 全 ff，最可能是传感器未上电或 INT/MISO 引脚未接通。")
    print()
    print("下一步（需要在 Windows 下确认）：")
    print("  · 设备管理器里该传感器是否显示为正常启动")
    print("  · Windows 休眠/关机后指纹是否还能唤醒（能唤醒=有常电）")
    print("  · 是否有 BIOS 侧的指纹开关（部分 MateBook 在 BIOS 里可禁用）")
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    """逐个试 GpioIo 的真实用途（复位 / 电源使能 / 片选）。

    背景：ACPI 里 GpioIo(140) 没标注用途，三种解释会导出完全相反的操作，
    而它们的现场表现都是"总线无响应"。这个命令拉高/拉低后测中断速率与
    MISO，哪个组合能让中断风暴停下、MISO 不再全是 ff，那个就是真相。
    """
    dev = KernelDevice(args.device)
    try:
        info = dev.info()
        if not info.reset_valid:
            print("复位脚没解析出来，无法试验。", file=sys.stderr)
            return 1

        def probe(label: str) -> None:
            a = dev.irq_count()
            time.sleep(0.4)
            b = dev.irq_count()
            miso = dev.xfer(b"\xff" * 16, 16, 0)
            all_ff = miso == b"\xff" * 16
            print(f"  {label}  中断 {(b - a) / 0.4:6.0f}/s  "
                  f"MISO {'全 ff（无应答）' if all_ff else miso.hex()}")

        print("复位脚初始电平", dev.gpio_get())
        print("\n-- 释放（高阻，平台上拉接管）--")
        dev.gpio_set(0, hold=False)
        time.sleep(0.2)
        probe("释放 ")

        for level in (0, 1):
            print(f"\n-- 保持逻辑 {level} 200ms 后 --")
            dev.gpio_set(level, hold=True)
            time.sleep(0.2)
            probe(f"pin={level}")
            print(f"     读回电平 {dev.gpio_get()}")

        print("\n-- 恢复：释放回高阻 --")
        dev.gpio_set(0, hold=False)
    finally:
        dev.close()

    print("\n怎么读结果：")
    print("  · 若某个电平下中断速率从上千次/秒降到接近 0，且 MISO 不再全 ff")
    print("    → 那就是传感器的工作状态，把它固定下来即可。")
    print("  · 若两种电平下中断速率都差不多、MISO 始终全 ff")
    print("    → 这根线不是复位/使能，传感器没被正确上电或引脚用途另有其他，")
    print("      需要从 Windows 驱动侧确认它的真实语义。")
    print()
    print("注意：_CRS 里这根线是 active-low + pullup，即空闲为高。")
    print("  若是复位脚，高 = 不复位（芯片应正常工作）；")
    print("  若是电源使能，高 = 断电（芯片根本不供电，MISO 自然是全 ff）。")
    print("  后者能一次性解释'SPI 传输成功但无应答 + 中断空转'。")
    return 0


def _test_wire_roundtrip() -> None:
    pkt = wire.encode_packet(wire.OUTER_WRITE, wire.CMD_IDLE, b"\x14\x00")
    outer, plen = wire.decode_outer_header(pkt[:4])
    assert outer == wire.OUTER_WRITE
    cmd = wire.decode_command(pkt[4 : 4 + plen])
    assert cmd.cmd == wire.CMD_IDLE
    assert cmd.data == b"\x14\x00"


def _test_wire_prefix01() -> None:
    # 官方驱动硬编码的 8 字节前缀：末字节是 NULL_CHECKSUM 0x88，
    # 不是计算值（算出来是 0xa4），传感器对这条握手帧接受空校验和。
    prefix = bytes([0x01, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00, 0x88])
    assert prefix[-1] == wire.NULL_CHECKSUM
    assert wire.checksum(prefix[:7]) != prefix[-1], "这条帧故意用空校验和"
    # 外框必须能自洽：真正解析从这里开始
    outer, plen = wire.decode_outer_header(
        wire.encode_outer_header(wire.OUTER_WRITE, len(prefix))
    )
    assert outer == wire.OUTER_WRITE and plen == len(prefix)

def _test_psk_official_vector() -> None:
    """用上游 selftest 的 oracle 向量校验白盒打包。"""
    key = bytes(range(32))
    want = bytes.fromhex(
        "45a3732acf2990f4ceb30e8996e4add12e887160d6dcaa99c8bcd8338c7743f2"
        "02ff20000000e7bc09962aedfb470fac3887de32465c5a71c88fe01dd776cfa5"
        "19e70c63f82401f8247324fde9c6562321d4d82b0dd552c0bf1f74716085b852"
        "658bfe772964"
    )
    got = psk.wrap(key)
    assert got == want, f"白盒容器不一致：\n want {want.hex()}\n  got {got.hex()}"
    assert psk.unwrap(got) == key


def _test_tls_header() -> None:
    # 一条 5+16 字节的应用数据记录
    rec = bytes([0x17, 0x03, 0x03, 0x00, 0x10]) + b"\x00" * 16
    assert wire.tls_record_header_ok(rec)
    assert not wire.tls_record_header_ok(rec[:10])
    assert not wire.tls_record_header_ok(b"\x99" + rec[1:])


# ---- 入口 ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gdix51c0",
        description="HUAWEI MateBook X Pro (MRGF-XX) 指纹传感器 (GDIX51C0) 用户态工具",
    )
    p.add_argument("--version", action="version", version=f"gdix51c0 {__version__}")
    p.add_argument("--device", default=DEVICE_NODE, help=f"字符设备路径（默认 {DEVICE_NODE}）")
    sub = p.add_subparsers(dest="command", required=True)

    info_p = sub.add_parser("info", help="打印设备与驱动信息")
    info_p.add_argument("-v", "--verbose", action="store_true",
                        help="同时打印 _CRS 声明的全部资源")
    info_p.set_defaults(func=cmd_info)

    diag_p = sub.add_parser("diag", help="裸总线诊断：看 MISO 与中断计数")
    diag_p.set_defaults(func=cmd_diag)

    pin_p = sub.add_parser("pin", help="逐个试 GpioIo 的真实用途")
    pin_p.set_defaults(func=cmd_pin)

    sub.add_parser("otp", help="读取芯片 ID、OTP 与自校准 DAC").set_defaults(func=cmd_otp)

    prov = sub.add_parser("provision", help="生成并部署新的 PSK（破坏性）")
    prov.add_argument("--state", default=DEFAULT_PSK_STATE, help="PSK 状态文件")
    prov.add_argument("--psk-hex", default=None, help="用指定的 64 位十六进制 PSK")
    prov.add_argument("--yes", "-y", action="store_true", help="确认覆盖传感器里的旧密钥")
    prov.set_defaults(func=cmd_provision)

    cap = sub.add_parser("capture", help="采一帧指纹")
    cap.add_argument("--state", default=DEFAULT_PSK_STATE, help="PSK 状态文件")
    cap.add_argument("--output", "-o", default="finger.pgm", help="输出文件（.pgm 或 .png）")
    cap.add_argument("--invert", action="store_true", help="反相（指纹纹路更接近常见观感）")
    cap.add_argument("--wait", action="store_true", help="先等手指按下再采")
    cap.add_argument("--wait-ms", type=int, default=5000, help="等手指的超时（毫秒）")
    cap.add_argument("--ascii", action="store_true", help="同时打印终端预览")
    cap.add_argument("--ascii-cols", type=int, default=40, help="终端预览宽度（字符数）")
    cap.set_defaults(func=cmd_capture)

    watch = sub.add_parser("watch", help="循环等手指并连续采图")
    watch.add_argument("--state", default=DEFAULT_PSK_STATE, help="PSK 状态文件")
    watch.add_argument("--output", "-o", default="finger.pgm", help="输出文件模板")
    watch.add_argument("--invert", action="store_true", help="反相")
    watch.add_argument("--count", type=int, default=1, help="采几张后退出（0 = 不限）")
    watch.add_argument("--wait-ms", type=int, default=30000, help="单次等手指的超时（毫秒）")
    watch.add_argument("--ascii", action="store_true")
    watch.add_argument("--ascii-cols", type=int, default=40)
    watch.set_defaults(func=cmd_watch)

    sub.add_parser("selftest", help="纯软件自检（不需要硬件）").set_defaults(func=cmd_selftest)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (SessionError, TransportError, psk.PskError, wire.WireError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
