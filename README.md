# huawei-ntag0001-nfc

把 **Huawei MateBook X Pro 2023（MRGF-XX）内置 NFC** 在 Linux 上讲清楚并跑通的工程。

逆向对象是华为官方 Windows 驱动 `SpbNfcDriver.sys`（随 `NFC_23.2.3.0.zip` 分发）。

## 结论

$$\boxed{\text{芯片是上海复旦微电子的 FM11NT082C-NTCI，一张双界面 NFC Forum Type 2 标签}}$$

| 项 | 值 |
|---|---|
| 型号 | **FM11NT082C-NTCI**（上海复旦微电子 / FMSH） |
| 定位 | Dual interface Tag & **Channel** IC，8 Kbit EEPROM，用户区 888 字节 |
| RF 侧 | ISO/IEC 14443-A，NFC Forum Type 2，7 字节 UID，106 kbps |
| 接触侧 | I2C，从地址 **0x57**（配置字节 EE `0x03B3`，默认 `7'b1010_111`） |
| ACPI | `\_SB.PC00.NFC_`，`_HID = NTAG0001`，`I2cSerialBusV2(0x57, 400kHz, \_SB.PC00.I2C0)` |
| 中断 | `GpioInt(Edge, ActiveHigh, pin 176)` → **irq 213**（FD 场检测） |
| 手机读到的 | UID `1D D3 B4 D8 82 00 00`；NDEF：华为分享网址 + `application/vnd.huawei.handoff.ndefmsg` |

**NFC 在 Linux 下已经可用**：手机贴上能读到网址、能唤起华为分享。
RF 侧完全由芯片自主完成，与主机装什么系统无关。

Windows 驱动做的是另一件事——「碰一碰 → PC 侧华为分享联动」：

- KMDF 驱动，**不导入 `nfccx.sys`**，无一行 NCI 编解码
- 经 `\Device\RESOURCE_HUB\<ConnectionId>` 打开 I2C 目标
- 私有 16 字节块协议（命令类 `0x10`…`0x90`）+ 空密钥 HMAC-SHA256 八组校验值
- 真正的 NFC 协议栈不在这个包里（`NFC.exe` 是 NSIS 安装器）

**标签本身不需要这个驱动**，在 Linux 上也不需要被移植。

## 目录

```
driver/      ntag_nfc.c + Makefile     内核模块：绑 ACPI、建 /dev/ntag_nfc、注册 FD 中断
tools/       ntag_read.py              ★ 主力：一次连续读 + 自校验定相位，解 NDEF
             touchd.py                 触碰事件守护进程（轮询 irq 计数，可挂回调）
             bus_diag.py               I2C 总线体检（对照组 / 全扫 / 组帧试错）
             get_acpi.sh               dump + 反编译 ACPI，定位 NTAG0001
             fix_touchpad.sh           适配器 wedged 后救援触控板（已验证）
analysis/    SpbNfcDriver.sys.dis.txt  驱动全反汇编（objdump，5591 行）
             acpi/nfc_device.asl       ACPI 设备定义摘录 + GNUM 换算
docs/        逆向与适配报告.md          完整逆向报告
```

## 快速开始

```bash
# 1. 读出片上内存（主要用途）
sudo python3 tools/ntag_read.py
#   自动连读 2048 字节，扫描 1024 个相位，用 Type 2 不变量定位
#   满分 30 分 = BCC0/BCC1/CC/UID-ASCII/0x57/FMSH 全部对上

# 2. 触碰事件
sudo insmod driver/ntag_nfc.ko probe_on_load=0
sudo python3 tools/touchd.py --exec 'notify-send "NFC" "手机碰触于 %s"'

# 3. 硬件事实与 ACPI
grep ntag_nfc /proc/interrupts     # 触碰时计数 +1
sudo tools/get_acpi.sh
```

## 接触口怎么读 —— 三条实测事实

这部分是踩坑之后才搞清的，写在这里免得重走：

**① 写地址不生效。** 写 1 字节地址、写 2 字节地址、什么都不写，读到的都是
内部指针当前位置之后的连续数据。证据：`ntag_seek.py` 里「单字节地址读」与
「双字节地址读」拿到的两段，正好是同一段连续流里相邻的 16 字节。

**② 指针自增，每次读只前进。** 不能「定位再读」，只能一次连续读满再认位置。
读到内存末尾后芯片拉住 SCL 不放，导致控制器 `-110 ETIMEDOUT` ——
**这不是芯片坏了，是没数据了**。

**③ 分块读会在块边界重复字节。** 实测 dump 里 `0x0f` 与 `0x10` 同为 `0x68`
（`"huawei.h"+"handoff"` 拼成 `"huawei.hhandoff"`）、`0x3f` 与 `0x40` 同为 `0x7f`。
一旦出现这种重复，那份数据就不是干净镜像，必须重取。
但**不能按固定规则去重**：统计整份 1024 字节，63 个块边界里只有少数满足相邻相等，
`aa55` 交替填充区会破坏条件。所以工具只做一次连续读，遇到分块伪影直接判失败，
不猜着修。

## 相位怎么定

EEPROM 的 1024 字节里有一组互相独立的不变量，只有相位正确时才同时成立：

| 偏移 | 不变量 | 权重 |
|---|---|---|
| `0x0000` | `BCC0 = UID0^UID1^UID2^UID3`（XOR 为 0） | 5 |
| `0x0004` | `BCC1 = UID3^UID4^UID5^UID6` | 5 |
| `0x000C` | CC 首字节固定 `0xE1` | 4 |
| `0x0390` | UID 的 ASCII 表示 `1DD3B4D8` | 4 |
| `0x0398` | 本芯片 I2C 从地址 `0x57` | 4 |
| `0x03B7` | 厂商标识 `FMSH` | 3 |
| `0x0010` | NDEF TLV 起始 `0x03` | 2 |
| `0x0000` | UID 三字节 `1d d3 b4` | 3 |

两个 BCC 各自偶然命中的概率是 1/256，加上其余项，联合误判基本不可能。
离线用 7 个不同起始相位验证（含跨回卷），全部 30/30 还原正确。

## 内存布局（手册 3.2.3，接触口与 RF 侧共用同一块 EE）

| 字节地址 | 内容 | 权限 |
|---|---|---|
| `0x0000`~`0x0008` | UID（7B）+ BCC0 + BCC1 | R |
| `0x000A`~`0x000B` | Static Lock Bytes | OTP |
| `0x000C`~`0x000F` | Capability Container | RWL |
| `0x0010`~`0x003F` | Static Data Area（页 4-15） | RWL |
| `0x0040`~`0x0387` | Dynamic Data Area | RWL |
| `0x0388`~`0x038B` | Dynamic Lock Bytes | OTP |
| `0x038C`~`0x039B` | **配置区**（`0x0390` UID-ASCII、`0x0398` I2C 地址） | RWL |
| `0x03B3` | 手册规定的 I2C 地址配置字节 | RWL |
| `0x03C0`~`0x03C7` | CT Lock Bits（接触口锁位，每 bit 锁 16 字节） | OTP |

## 构建内核模块

```bash
cd driver
sudo pacman -S --needed base-devel linux-headers
make
sudo insmod ntag_nfc.ko probe_on_load=0
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `probe_on_load` | 0 | 装载时是否主动写芯片。**保持 0**，标签内容已只读锁定 |
| `irq_type` | 0 | 0=按 ACPI 自动，1=上升沿，2=下降沿，3=双边沿 |

## 踩过的坑

- **不要 unbind/bind `0000:00:15.0`**（PCI 控制器）来复位总线。内核会重建 adapter
  但 i2c-hid 的 client 状态失效，**触控板直接失灵**。救援用 `tools/fix_touchpad.sh`
  （只重绑 client，不用重启）。
- i2c 控制器超时一次就会 wedged，之后所有传输都返回 `-110`，读数全不可信。
  所以 `ntag_read.py` 第一步永远是**对照组**：读 `0x60` 必须是 `121`，
  若是 `110` 就停下来要求先复位。
- `-110 ETIMEDOUT` ≠ 设备坏：地址 ACK 了、随后设备拉住 SCL，是「设备活着但没数据」。
  `-121 EREMOTEIO` 才是「这个地址没人」。

## 参考手册

- FM11NT081D《双界面 NFC Forum Type2 标签芯片 技术手册》V1.0（45 页，I2C 协议完整）
- FM11NT082C《NFC Dual interface Tag & Channel IC Datasheet》V1.0（18 页，型号编码与电气特性）

## 许可

分析结果与自写代码可自由使用。`analysis/` 下为静态反汇编文本，仅作研究参考；
`SpbNfcDriver.sys` 本体为华为版权作品，未随本仓库分发。
