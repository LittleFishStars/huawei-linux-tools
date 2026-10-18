# huawei-gdix51c0-fp

把 **HUAWEI MateBook X Pro（MRGF-XX）内置指纹传感器** 在 Linux 上跑通的适配工程。

传感器是汇顶 **Goodix GDIX51C0**，走 SPI + 两路 GPIO，控制器在 ACPI 路径
`\_SB.PC00.SPI0.SPBA`。Linux 内核没有它的驱动，`spi-GDIX51C0:00` 这个设备
一直处于"未绑定"状态。

## 结论

$$\boxed{\text{内核侧只需要一个很薄的 SPI/GPIO 搬运层，全部协议放在用户态}}$$

- 内核模块 `gdix51c0_fp.ko` 只做三件事：绑定 ACPI 设备、把全双工 SPI 搬运
  暴露成 `/dev/gdix51c0`、把中断计数暴露成 ioctl
- 线协议、TLS-PSK 加密、white-box 密钥封装、RAW12 图像解码全在 Python 里
- 传感器在 TLS 之上传图像，所以必须先把 PSK 部署进去才能采图；这一步是
  **破坏性** 的，会覆盖 Windows 侧正在用的密钥

已验证：内核模块可编译、DKMS 打包可安装、wire/PSK/配置/解码四层自检与上游
官方实现逐字节对齐。未验证：真机采图（开发环境无 root，无法 insmod）。

## 硬件事实

| 项目 | 值 |
| --- | --- |
| ACPI HID | `GDIX51C0`（`acpi:GDIX51C0:GDIX51C0:`） |
| ACPI 路径 | `\_SB_.PC00.SPI0.SPBA` |
| SPI 设备 | `spi-GDIX51C0:00`，父控制器 `pxa2xx-spi.6`（PCI 8086:51aa） |
| 芯片 ID | `0x2504`（sensor type 12，算法 ChicagoHS） |
| 传感器阵列 | 64（快轴）× 80（慢轴），5120 像素，12 bit 打包成 7680 字节 |
| SPI 模式 | mode 0，8 bit，10 MHz，单次最多 2048 字节 |
| 加密 | TLS 1.2，cipher `PSK-AES128-GCM-SHA256`，PSK identity `Client_identity` |
| 密钥封装 | 102 字节 white-box 容器，写入寄存器 `0xbb010003` |

## 目录

```
driver/      gdix51c0_fp.c + Makefile   内核传输模块（开发用）
tools/       gdix51c0/                  用户态协议栈（Python）
  transport.py   /dev/gdix51c0 的 ioctl 封装
  wire.py        Goodix 线协议组帧/解帧
  proto.py       包级收发与四种命令时序
  config.py      0x90 配置块（232 字节，含 DAC 与时序）
  psk.py         102 字节 white-box 密钥容器
  tls.py         TLS 1.2 PSK 通道（MemoryBIO + 补丁）
  image.py       RAW12 解码与 PGM/ASCII 输出
  session.py     初始化序列、采图、FDT
packaging/   dkms.conf.in / Makefile.kbuild / .install / 启动器
PKGBUILD     pacman 包定义（本项目以 pacman 包交付）
tests/       假设备驱动的协议时序单测（不需硬件）
docs/        逆向报告
analysis/    ACPI 与协议取证材料
```

## 安装

```bash
makepkg -si
```

包做两件事：

1. 把内核模块源码装到 `/usr/src/gdix51c0-fp-0.1.12/`，触发 DKMS 为每个已装
   内核编译（pacman 的 dkms hook 自动执行）
2. 把 `gdix51c0` 工具装到 `/usr/bin`，Python 包装进 site-packages

依赖 `dkms`、`python`、`python-cryptography`；`python-pillow` 可选（导出 PNG）。

## 使用

```bash
sudo modprobe gdix51c0_fp     # 绑定 spi-GDIX51C0:00，创建 /dev/gdix51c0
sudo gdix51c0 info            # 设备/中断/速率概览

sudo gdix51c0 diag            # 裸总线诊断：MISO 原始字节 + 中断速率
sudo gdix51c0 pin             # 试 GpioIo 的真实用途（复位/使能/片选）
sudo gdix51c0 otp             # 芯片 ID、OTP、自校准 DAC
sudo gdix51c0 provision --yes # 生成并部署 PSK（破坏性！）
sudo gdix51c0 capture --wait -o ~/finger.pgm
sudo gdix51c0 capture --ascii # 终端直接看纹路
gdix51c0 selftest             # 不需要硬件的纯软件自检
python3 /usr/share/doc/gdix51c0-fp/tests/test_proto.py   # 协议时序单测（假设备）
```

模块参数（`/etc/modprobe.d/` 里调）：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `reset_gpio_index` | 1 | `_CRS` 里 GPIO 资源的**全局**序号（GpioIo 与 GpioInt 数在一起）——复位脚 |
| `reset_active_low` | -1 | 复位脚极性：-1=跟随 `_CRS`，0=高有效，1=低有效 |
| `irq_resource_index` | 0 | `_CRS` 里第几个 GpioInt 是中断 |
| `irq_gpio_index` | -1 | 额外把某个 GpioIo 当可读电平的中断脚（同样是全局序号） |
| `irq_edge_trigger` | 1 | 中断用**双边沿**触发（与官方驱动一致）。本机 `_CRS` 把 GpioInt 写成 level，按 level 配会在线上持续 active 时无限重触发（实测约 1200 次/秒）；设 0 可退回跟随 `_CRS` |
| `speed_hz` | 10000000 | SPI 时钟 |
| `chunk_bytes` | 2048 | 单次 `spi_transfer` 上限 |

## 三个必须知道的技术点

**1. 头和载荷要分两个 CS 周期发。** 官方驱动的调用序列是 `write(hdr, 4)` 然后
`write(payload, len)`，不是一个连续缓冲。合并发送时 MCU 会在两段之间丢字节。

**2. 采图前有 80 ms 必须完全静默。** 读阵列大约 73 ms，这期间 SPI 上不能有
任何时钟。上游如果用异步线程轮询中断，会把模拟读出在固定行截断（社区称之为
"row-31 cliff"，错误码 `0x0b78`）。本实现没有后台线程，静默窗口天然成立。

**3. TLS 记录走 `0xb0` 外框，普通命令走 `0xa0`。** 外框只表示搬运类别，包的
语义看载荷首字节。图像是 `0x17`（TLS 应用数据），不是命令号。

**4. `_CRS` 里的 GPIO 资源不一定齐全。** 本机实测：GpioInt 有（irq=214），但
GpioIo 一个都没有（`devm_gpiod_get_index` 返回 `-ENOENT`）。所以复位只能靠
`0xa2` 命令软复位，硬复位不可用。想看清 `_CRS` 到底声明了哪些资源：

```bash
cat /sys/bus/spi/devices/spi-GDIX51C0:00/resources
```

## 与上游的关系

协议细节参考了 [libfprint-goodix-spi](https://github.com/berkekbgz/libfprint-goodix-spi)
（LGPL-2.1-or-later）与社区对 `gfspi.dll` 的逆向结论。本项目的差异：

- 上游用 spidev + 用户态 SPI，本项目用真内核模块（能拿 GPIO、保证 CS 时序）
- 上游给 libfprint 打补丁做影子库，本项目先做独立的 `gdix51c0` 工具
- 上游靠 listener 线程 + suppress 标志处理静默窗口，本项目改为同步调用

## 许可证

GPL-2.0-or-later。
