# SPDX-License-Identifier: GPL-2.0-or-later
"""高层会话：初始化、采图、FDT。

与上游的关键差异：上游用异步 listener 线程轮询 IRQ 并把包读走，采图时必须在
80 ms 静默窗口内抑制它，否则模拟读出会在固定行被截断（"row-31 cliff"）。
这里没有后台线程——每一步读都由调用方显式发起，静默窗口天然成立，只需要
在发出采图命令后真的等满 80 ms 再读。

代价是握手阶段要自己盯着 IRQ。好在驱动把中断计数暴露成 ioctl，等边沿就是
一次 ``IOC_IRQ_WAIT``。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

from . import config as mcu_config
from . import image, proto, psk, wire
from .tls import TlsChannel, TlsError
from .transport import DeviceInfo, KernelDevice

#: 采图前的固定间隙（官方在 FDT-down 与 setmode 之间等这么久）
PRE_CAPTURE_DELAY = 0.010
#: 采图前的静默窗口：读出阵列约 73 ms，这期间总线上不能有任何时钟
CAPTURE_SETTLE = 0.080
#: 拿到图像记录的超时
IMAGE_TIMEOUT = 1.0
#: 一次采图最多尝试次数
CAPTURE_ATTEMPTS = 2
#: 初始化完成后让 MCU 稳定
POST_INIT_DELAY = 0.200

DEFAULT_PSK_STATE = "/var/lib/fprint/gdix51c0.psk"


class SessionError(RuntimeError):
    pass


@dataclass
class SensorInfo:
    chip_id: int
    otp: bytes
    dac: tuple[int, int, int, int]
    firmware: str = ""

    @property
    def sensor_id(self) -> bytes:
        return self.otp[:16]


@dataclass
class Session:
    """一次完整的设备会话。

    用法::

        with Session(psk=my_psk) as s:
            s.start()
            frame = s.capture(finger=True)
    """

    psk: bytes | None = None
    psk_state_file: str = DEFAULT_PSK_STATE
    device_path: str | None = None
    speed_hz: int = 0
    dev: KernelDevice | None = field(default=None, repr=False)
    io: proto.PacketIo | None = field(default=None, repr=False)
    tls: TlsChannel | None = field(default=None, repr=False)
    info: DeviceInfo | None = None
    sensor: SensorInfo | None = None
    _started: bool = field(default=False, repr=False)
    _reset_warned: bool = field(default=False, repr=False)

    # -- 生命周期 --

    def __enter__(self) -> "Session":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        """打开设备。幂等：已经打开时直接返回。

        驱动侧同一时刻只允许一个打开者（open 返回 EBUSY），因此这个
        方法必须幂等 —— `with Session(...)` 的 __enter__ 已经调过一次，
        调用方再显式 open() 一次不应该报 EBUSY。
        """
        if self.dev is not None:
            return
        path = self.device_path or "/dev/gdix51c0"
        self.dev = KernelDevice(path)
        self.info = self.dev.info()
        self.io = proto.PacketIo(self.dev, self.speed_hz, self.info.chunk or proto.CHUNK)

    def close(self) -> None:
        if self.dev is not None:
            self.dev.close()
            self.dev = None
        self._started = False

    # -- 底层收发（TLS 层回调的目标）--

    def _await_irq(self, timeout: float) -> bool:
        """等一个 IRQ 边沿。返回 False 表示超时。"""
        assert self.dev is not None
        return self.dev.irq_wait(int(timeout * 1000)) is not None

    def _read_packet_wait(self, timeout: float) -> proto.Packet | None:
        """先等中断，再读包。

        MCU 用一条 IRQ 表示"有数据待取"，所以等边沿之后立刻读。

        注意区分两个能力：
          - 等边沿（IOC_IRQ_WAIT）走内核 waitqueue，只要 irq 注册成功就能用
          - 读电平（IOC_IRQ_LEVEL）需要额外的 GpioIo，本机没有
        所以降级条件看 info.irq，而不是 irq_gpio_valid。之前拿后者当条件，
        本机因此整条退化成纯轮询——等于在总线上反复打时钟，而官方驱动
        明确避免这一点（采图时甚至要求完全静默）。
        """
        assert self.dev is not None
        if self.io is None:
            return None
        # 能读电平就先用电平判定（边沿可能已经错过）
        if self.info is not None and self.info.irq_gpio_valid:
            if self.dev.irq_level():
                return self.io.next_packet(timeout)
        # 等边沿；irq 没注册成功时只能轮询
        if self.info is not None and self.info.irq:
            if not self._await_irq(timeout):
                return None
        return self.io.next_packet(timeout)

    def read_tls_packet(self, timeout: float) -> bytes | None:
        """TLS 层要的"读一条记录"回调。"""
        pkt = self._read_packet_wait(timeout)
        if pkt is None:
            return None
        payload = pkt.payload
        # 单条记录可能被 MCU 拆成多包，按 TLS 头里的长度补齐
        if len(payload) >= 5 and payload[0] in wire.TLS_CONTENT_TYPES:
            need = 5 + ((payload[3] << 8) | payload[4])
            while len(payload) < need:
                more = self._read_packet_wait(timeout)
                if more is None:
                    break
                payload += more.payload
        return payload

    def write_tls_packet(self, record: bytes) -> None:
        """TLS 记录走 0xb0 外框，和普通命令的 0xa0 不同。"""
        assert self.io is not None
        self.io.write_packet(wire.OUTER_READ, record)

    # -- 初始化 --

    def _drain(self, seconds: float = 0.005) -> None:
        """把总线上残留的包读空（连同停滞区）。"""
        assert self.io is not None
        self.io.drain(seconds)

    def _wait_cmd(self, cmd: int, timeout: float) -> wire.Command:
        """等一条指定命令号的响应，跳过其它包。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pkt = self._read_packet_wait(deadline - time.monotonic())
            if pkt is None:
                break
            if pkt.inner_cmd == cmd:
                return pkt.command()
        raise SessionError(f"等命令 0x{cmd:02x} 响应超时")

    def start(self, psk_bytes: bytes | None = None) -> SensorInfo:
        """跑完初始化序列并建立 TLS 通道。"""
        if self._started:
            assert self.sensor is not None
            return self.sensor
        if self.io is None:
            self.open()

        assert self.io is not None
        assert self.dev is not None

        self._boot_probe()
        self._get_evk_version()
        self._get_mcu_state()
        self._reset_sensor("reset-sensor-1")

        chip_id = self._read_chip_id()
        if chip_id != image.CHIP_ID:
            raise SessionError(
                f"芯片 ID 不符：期望 0x{image.CHIP_ID:04x}，读到 0x{chip_id:04x}"
            )

        otp = self._read_otp()
        dac = self._dac_from_otp(otp)

        self._reset_sensor("reset-sensor-2")
        self.io.send_and_ack(proto.cmd_idle(0x14), proto.ACK_TIMEOUT)
        self._apply_dac(dac)
        self._upload_config(dac)

        time.sleep(POST_INIT_DELAY)
        self._drain(0.010)

        self.sensor = SensorInfo(chip_id=chip_id, otp=otp, dac=dac)
        self._establish_tls(psk_bytes)
        self._started = True
        return self.sensor

    # -- 初始化子步骤 --

    def _boot_probe(self) -> None:
        """会话激活的第一步：复位 → 等启动脉冲 → 解开 TLS 锁定。

        缺了这一步，后续 0xa8 会完全无响应（总线静默）——因为 MCU
        可能还停在上一会话的 TLS 已建立状态，或者还没从复位里出来。

        顺序与官方一致：
          1. 硬件复位（拉低 10ms 再释放）
          2. 等 IRQ 上升沿 + 下降沿（MCU 启动脉冲）
          3. 等 200ms 让它完全起来，排空残余边沿
          4. 发 0xd5（force-unlock-tls，无 ACK）
        """
        assert self.dev is not None
        assert self.io is not None

        self._drain(0.005)
        self._hard_reset()

        if self.info is not None and self.info.irq:
            # 启动脉冲：先升后降。等不到也不算致命 —— 有些固件版本
            # 不复位后发脉冲，但 0xd5 还是要发。
            self._await_irq(proto.BOOT_TIMEOUT)
            self._await_irq(proto.BOOT_TIMEOUT)

        time.sleep(0.200)
        self._drain(0.010)

        self.io.send_no_ack(proto.cmd_force_unlock_tls())

    def _get_evk_version(self) -> None:
        assert self.io is not None
        last: Exception | None = None
        for attempt in range(3):
            try:
                self.io.send_no_ack(proto.PREFIX01)
                time.sleep(0.010)
                self.io.send_single_response(proto.cmd_firmware_version(), proto.BOOT_TIMEOUT)
                return
            except (proto.ProtoError, wire.WireError) as exc:
                last = exc
                if attempt == 2:
                    break
                self._hard_reset()
                time.sleep(0.500)
        raise SessionError(f"三轮 EVK 版本读取全部失败：{last}")

    def _get_mcu_state(self) -> None:
        assert self.io is not None
        self.io.send_no_ack(proto.PREFIX01)
        time.sleep(0.010)
        try:
            self.io.send_single_response(proto.cmd_get_mcu_state(), proto.RESPONSE_TIMEOUT)
        except proto.ProtoError:
            # MCU 状态只用于诊断，拿不到不阻塞流程
            pass

    def _reset_sensor(self, tag: str) -> None:
        assert self.io is not None
        self.io.send_ack_then_response(proto.cmd_reset(True, False, 0x14), proto.RESPONSE_TIMEOUT)

    def _hard_reset(self) -> None:
        assert self.dev is not None
        self._drain(0.005)
        # 复位脚没解析出来时，这个 ioctl 在内核里是空操作（驱动直接返回）。
        # 静默空转会让后续的超时变得难查，所以明说一句。
        if self.info is not None and not self.info.reset_valid:
            if not self._reset_warned:
                print(
                    "警告：_CRS 里没有复位 GPIO，硬复位不可用。"
                    "传感器只能靠 RESET 命令（0xa2）软复位；"
                    "若初始化反复失败，需要一次物理断电（拔电源/重启）。",
                    file=sys.stderr,
                )
                self._reset_warned = True
        self.dev.reset(10_000)

    def _read_chip_id(self) -> int:
        assert self.io is not None
        resp = self.io.send_ack_then_response(
            proto.cmd_register_read(0, 4), proto.RESPONSE_TIMEOUT
        )
        if resp.cmd != wire.CMD_READ_SENSOR_REG or len(resp.data) != 4:
            raise SessionError(
                f"芯片 ID 响应格式错误：cmd=0x{resp.cmd:02x} len={len(resp.data)}"
            )
        # [status, value_lo, value_hi, reserved]
        return resp.data[1] | (resp.data[2] << 8)

    def _read_otp(self) -> bytes:
        assert self.io is not None
        resp = self.io.send_ack_then_response(proto.cmd_read_otp(), proto.RESPONSE_TIMEOUT)
        if resp.cmd != wire.CMD_READ_OTP:
            raise SessionError(f"OTP 响应命令号错误：0x{resp.cmd:02x}")
        if len(resp.data) != 64:
            raise SessionError(f"OTP 长度应为 64，收到 {len(resp.data)}")
        return resp.data

    @staticmethod
    def _dac_from_otp(otp: bytes) -> tuple[int, int, int, int]:
        """从 OTP 自校准模拟 DAC，空块则退回 legacy 值。"""
        if len(otp) >= 54:
            ft = otp[0x32:0x36]
            mt = otp[0x2E:0x32]
            for block in (ft, mt):
                if all(block):
                    return ((block[0] << 4) | 8, block[1], block[2], block[3])
        return (0x0B38, 0xB5, 0xB3, 0xB3)

    def _apply_dac(self, dac: tuple[int, int, int, int]) -> None:
        assert self.io is not None
        main, d1, d2, d3 = dac
        body = bytes(
            [
                0x98, 0x09, 0x00,
                main & 0xFF, (main >> 8) & 0xFF,
                d1 & 0xFF, 0x00,
                d2 & 0xFF, 0x00,
                d3 & 0xFF, 0x00,
                0x00,
            ]
        )
        self.io.send_and_ack(body, proto.ACK_TIMEOUT)

    def _upload_config(self, dac: tuple[int, int, int, int]) -> None:
        assert self.io is not None
        main, d1, d2, d3 = dac
        blob = mcu_config.build(main, d1, d2, d3)
        self.io.send_and_ack(blob, proto.ACK_TIMEOUT)

    # -- TLS --

    def _resolve_psk(self, explicit: bytes | None) -> bytes:
        if explicit is not None:
            if len(explicit) != psk.PSK_LEN:
                raise SessionError(f"PSK 必须是 32 字节，收到 {len(explicit)}")
            return explicit
        if self.psk is not None:
            return self.psk
        loaded = psk.load_state(self.psk_state_file)
        if loaded is None:
            raise SessionError(
                "找不到 PSK：用 --provision 生成一次，或用 --psk-hex 指定，"
                f"或把密钥写进 {self.psk_state_file}"
            )
        self.psk = loaded
        return loaded

    def _establish_tls(self, psk_bytes: bytes | None) -> None:
        key = self._resolve_psk(psk_bytes)
        self.tls = TlsChannel(
            read_packet=self.read_tls_packet,
            write_packet=self.write_tls_packet,
            psk=key,
        )
        self.tls.handshake()

    def provision_psk(self, key: bytes | None = None) -> bytes:
        """生成并部署一个全新的 PSK。

        这是破坏性操作：传感器里旧密钥会被覆盖，任何还在用旧密钥的主机
        （包括 Windows 侧）都要重新部署。
        """
        new_key = key or psk.random_psk()
        assert self.io is not None
        self._reset_sensor("provision-reset")
        self.io.send_and_ack(psk.provision_payload(new_key), proto.ACK_TIMEOUT)
        psk.save_state(self.psk_state_file, new_key)
        self.psk = new_key
        return new_key

    # -- FDT --

    def arm_finger_detect(self, timeout_ms: int = 5000) -> bool:
        """让 MCU 进入手动 FDT 模式并等待手指。

        返回 True 表示检测到手指按下。超时返回 False。
        """
        assert self.io is not None
        fdt_mode = wire.encode_command(
            wire.CMD_FDT_MODE, bytes([0x09, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        )
        self.io.send_and_ack(fdt_mode, proto.ACK_TIMEOUT)

        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            pkt = self._read_packet_wait(0.5)
            if pkt is None:
                continue
            if pkt.inner_cmd == wire.CMD_FDT_DOWN:
                self.io.send_and_ack(proto.cmd_ack(), proto.ACK_TIMEOUT)
                return True
        return False

    # -- 采图 --

    def capture(self, finger: bool = True, attempts: int = CAPTURE_ATTEMPTS) -> image.Frame:
        """采一帧。"""
        if not self._started or self.tls is None:
            raise SessionError("会话未启动：先调用 start()")
        assert self.io is not None

        last: Exception | None = None
        for attempt in range(attempts):
            try:
                # 清掉陈旧的图像记录，避免把上一帧当成这一帧
                self._drain_cmd(wire.TLS_APP_DATA, 0.050)
                if attempt == 0:
                    time.sleep(PRE_CAPTURE_DELAY)

                img_cmd = proto.cmd_get_image(finger)
                self.io.send_and_ack(img_cmd, proto.ACK_TIMEOUT)

                # 静默窗口：这 80 ms 内一个 SPI 时钟都不能有时
                time.sleep(CAPTURE_SETTLE)

                record = self.read_tls_packet(IMAGE_TIMEOUT)
                if record is None:
                    raise SessionError("采图超时：没有收到图像记录")
                if not wire.tls_record_header_ok(record):
                    raise SessionError(f"图像记录头非法：{record[:8].hex()}")
                if record[0] != wire.TLS_APP_DATA:
                    raise SessionError(
                        f"期望应用数据 0x17，收到 0x{record[0]:02x}"
                    )
                plain = self.tls.decrypt(record)
                return image.Frame.from_raw(plain)
            except (SessionError, TlsError, proto.ProtoError) as exc:
                last = exc
                if attempt + 1 >= attempts:
                    break
                # 重发前重新同步一次状态
                time.sleep(0.050)
        raise SessionError(f"采图失败（{attempts} 次尝试）：{last}")

    def _drain_cmd(self, cmd: int, seconds: float) -> None:
        """把指定命令号的陈旧包读掉（也清停滞区里同命令的）。"""
        assert self.io is not None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pkt = self.io.next_packet(0.010)
            if pkt is None:
                continue
            if pkt.inner_cmd == cmd:
                break
