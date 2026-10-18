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
# 1. 读出片上内存（主要用途）—— 两种方式，二选一
#
#    方式 A：内核模块已绑定时（此时 0x57 被占，用户态会 EBUSY）
sudo insmod driver/ntag_nfc.ko probe_on_load=0
sudo python3 tools/ntag_read.py --dev /dev/ntag_nfc
#
#    方式 B：先卸载模块释放 0x57，走 /dev/i2c-0
sudo rmmod ntag_nfc
sudo python3 tools/ntag_read.py
#
#   自动读 2048 字节，扫描 1024 个相位，用 Type 2 不变量定位
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

**① 模块与用户态抢不到同一个地址。** `ntag_nfc.ko` 绑定后，
内核不允许用户态再对 0x57 发 `I2C_SLAVE`，会返回 `EBUSY`（`Errno 16`）。
所以要么用 `--dev /dev/ntag_nfc`，要么 `rmmod ntag_nfc` 后走 `/dev/i2c-0`。

**② 写地址不生效。** 写 1 字节地址、写 2 字节地址、什么都不写，读到的都是
内部指针当前位置之后的连续数据。证据：`ntag_seek.py` 里「单字节地址读」与
「双字节地址读」拿到的两段，正好是同一段连续流里相邻的 16 字节。

**③ 指针自增，每次读只前进。** 不能「定位再读」，只能一次连续读满再认位置。
读到内存末尾后芯片拉住 SCL 不放，导致控制器 `-110 ETIMEDOUT` ——
**这不是芯片坏了，是没数据了**。

**④ 分块读会在块边界重复字节。** 实测 dump 里 `0x0f` 与 `0x10` 同为 `0x68`
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

- unbind/bind `0000:00:15.0`（PCI 控制器）**能**复位被卡死的适配器，但内核重建
  adapter 后 i2c-hid 的 client 状态失效，**触控板会失灵**。所以必须两步一起做：
  先复位控制器，再重绑 GXTP client —— 这就是 `tools/reset_bus.sh` 做的事
  （已实测：复位 + 触控板一并恢复）。只想救触控板用 `tools/fix_touchpad.sh`。
- **单次 I2C 读的长度有硬上限：`<=16` 字节成功，`>=17` 字节必然 `-110` 并打死适配器。**
  写新工具时务必从 16 往下试，不要从大往小试（踩过：基线读写了 32，刚复位好又被自己打死）。
- i2c 控制器超时一次就会 wedged，之后所有传输都返回 `-110`，读数全不可信。
  所以 `ntag_read.py` 第一步永远是**对照组**：读 `0x60` 必须是 `121`，
  若是 `110` 就停下来要求先复位。
- `-110 ETIMEDOUT` ≠ 设备坏：地址 ACK 了、随后设备拉住 SCL，是「设备活着但没数据」。
  `-121 EREMOTEIO` 才是「这个地址没人」。

---

## 现状：主机侧能读到什么（2026-10-18 实测定论）

**RF 侧已经完全可用，主机侧读不到 EEPROM。** 分开说：

### 已经确定可用的部分

- 手机贴到掌托右侧，能正常读出标签内容（网址 + 华为分享 MIME），
  说明芯片 RF 侧与天线完全正常，**与操作系统无关**。
- GpioInt 场检测中断工作正常：`irq 213`（gpiochip0 pin176，
  内核注册名 `ntag_nfc`），手机每贴一次计数就涨，极性跟随 ACPI 的
  `ActiveHigh`（即上升沿）。
- 芯片身份确定：上海复旦微电子 **FM11NT082C-NTCI**，I2C 从地址 `0x57`。

### 主机侧访问的最终结论

最后一次完整实测（`--map`，复位后立刻跑）：

```
对照组 0x60 -> NAK(121)   总线健康        <- 总线诚实，0x57 确实在应答
基线裸读 16 字节: 00 x16
EEPROM 0x0000~0x03FF 步进 16（64 个地址）+ 寄存器 0xFFF0~0xFFFF（16 个）
  -> 非零 0 处，全零 80 处，失败 0 处
同地址重复读 0x0000 / 0x0010 / 0x0390 / 0xFFF7 各 3 次 -> 全部稳定
```

即：**芯片会 ACK 它的地址，但对任何地址都返回同样的零，且写地址不改变读取位置。**

### 但与「什么都没读到过」矛盾

最早一次（刚复位控制器后）用裸读拿到的是**真实数据**：

```
00 03 8c 91 01 33 55 04 63 6f 6e 73 75 6d 65 72 ...
  ^  TLV   ^NDEF 头 ^'U' ^https://  "consumer"
继续读: 72 2e 68 75 61 77 65 69 2e 63 6f 6d 2f 65 6e 2f   ("r.huawei.com/en/")
继续读: 2f 73 75 70 70 6f 72 74 2f 68 75 61 77 65 69 73   ("/support/huaweis")
继续读: 73 68 61 72                                        ("shar")
```

拼起来正是手机读到的那条 NDEF（`https://consumer.huawei.com/en/support/huaweishare...`）。
`91 01 33 55 04` 解码为：NDEF 记录头（MB=1, ME=**0**, SR=1, TNF=1）、
type 长 1、payload 长 `0x33`=51、type=`'U'`、前缀码 `0x04`=`https://`。
ME=0 说明后面还有一条记录 —— 正是那个 `application/vnd.huawei.handoff.ndefmsg`。

**所以芯片的 I2C 是能吐出真实数据的，只是内部读指针的位置不受主机控制。**
推论：指针是**自动前进**的（连续几次读拿到的是连续的字节），
我们从没把它拨回去过，就把指针一路推到了 EEPROM 末尾的空白区
（`0x03F0`~`0x03FF` 本来就是全零，且再往后就没数据了 ——
这同时解释了「怎么读都是同一段零」和「恰好 16 字节后超时」）。

### 还没试过的两条路

1. **冷启动复位指针**（最省事、零风险）。整机关机再开机（不是 reboot），
   芯片掉电后内部指针应回到 0。开机后**立刻**跑
   `sudo python3 tools/ntag_read.py --bus 0 --addr 0x57 --raw`
   （只 16 字节读）。若又能看到 `00 03 8c 91 ...`，说明指针模型成立，
   也顺带给了我们一个可重复的读取流程。
2. **写厂商默认配置字**（有写入动作）。复旦微自家 demo 用两颗配置字
   `EE[0x3B1]=0x78`、`EE[0x3B5]=0x90` 让芯片进入可访问状态
   （值来自 `app_demo_nfc.c` 的 `wBuf[5]={0x05,0x78,0xF7,0x90,0x02}`）。
   注意 demo 面向的是同代的 FM11NC08/FM327，而本机是 082C，
   配置区位置不一定相同（本机 EE 自发铭牌落在 `0x038C`~`0x039B`）。
   这条要写 EEPROM，属于会改变芯片状态的操作。

### 追加实测：修好上限 bug 后，2048 字节连续读仍全零（2026-10-18 晚）

`fetch()` 原先会把 `--len 2048` 当成第一个候选长度直接发出去，一次事务就把适配器打成
`ETIMEDOUT`（commit `ec76611` 已修：单事务永不 >16 字节，超长自动分块）。修好后重跑：

```
sudo ./tools/reset_bus.sh
sudo python3 tools/ntag_read.py --bus 0 --addr 0x57 --raw
```

- 对照组 `0x60` -> `NAK(121)`，**总线健康**
- 2048 字节**成功**读出（`分块 16 字节 x 128 次`）
- **128 次连续事务 = 两个完整 EEPROM 回环，全部 `0x00`**

这一条把「内部指针跑过头」的解释**彻底排除**：只要读真的落在 EEPROM 上、且指针随读前进，
两圈之内必然再次撞上数据区（NDEF 在 `0x0004` 起，约 200 字节）。

于是只剩两种可能：

1. 读**根本没有落到 EEPROM** —— 芯片处于 NC / 通道态，读到的是空 FIFO；
2. EEPROM 内容真的变成了全零（但我们从没写过任何**数据字节**，只发过 2 字节地址；需靠 RF 侧确认）。

### 下一个实验：一次把「冷启动」和「RF 活动」同时测干净（关键：全程别碰手机）

1. **先把手机放远**（几米外，或直接关机），确认 NFC / 华为分享都关掉。
2. 完整关机 -> 再开机（不是重启）。
3. 开机后**立刻**跑 `sudo python3 tools/ntag_read.py --bus 0 --addr 0x57 --raw`。
4. 若这次读到非零：**先别动**，把手机贴上去，再跑一次同样的命令。
   - 贴手机后变成全零 -> 机制确认：**RF 活动把芯片切到通道态，且会锁存**。
   - 依旧全零 -> 冷启动无法复位，或 RF 不是切换原因。
5. 无论哪条分支，都再贴一次手机，确认手机上**还能弹出那个链接** —— 这验证芯片 EEPROM 没被弄坏。

## 参考手册

- FM11NT081D《双界面 NFC Forum Type2 标签芯片 技术手册》V1.0（45 页，I2C 协议完整）
- FM11NT082C《NFC Dual interface Tag & Channel IC Datasheet》V1.0（18 页，型号编码与电气特性）
- Hi3861 `nfc_demo`（同代芯片的官方驱动源码，寄存器表与配置字来源）
  <https://gitee.com/larySTL/vendor_hisilicon/blob/master/hispark_pegasus/demo/nfc_demo/app_demo_config.h>

## 许可

分析结果与自写代码可自由使用。`analysis/` 下为静态反汇编文本，仅作研究参考；
`SpbNfcDriver.sys` 本体为华为版权作品，未随本仓库分发。
