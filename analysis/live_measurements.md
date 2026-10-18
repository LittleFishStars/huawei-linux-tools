# 现场实测记录（2026-10-18，root 权限下）

工具：`libgpiod 2.3.1`（gpioget / gpiomon / gpioset）+ `/sys/kernel/debug/gpio`

## 引脚归属确认

```
gpio-140  reset        out hi ACTIVE LOW     ← _CRS[1] GpioIo，低有效复位
gpio-321  irq          in  hi IRQ            ← _CRS[0] GpioInt，中断
gpio-337  interrupt    in  hi IRQ            ← 对照：触控板，工作正常
gpio-168  TPD0 GpioInt in  hi IRQ ACTIVE LOW ← 对照：触控板
```

## INT 引脚（321）的行为

`gpiomon` 2 秒窗口内的事件数，多次测量：

| 条件 | rising | falling | 合计 |
|---|---|---|---|
| 默认 | 2412 | 2436 | ≈ 2400（1200/s） |
| `--bias pull-up` | — | — | 2394 量级 |
| `--bias pull-down` | — | — | 2457 量级 |
| `--bias disabled` | — | — | 2394 量级 |
| 对照：pin 337（触控板） | — | — | **1** |

结论：

1. **上升沿与下降沿数量相当**（2412 : 2436），是真实的方波振荡，
   频率约 1.2 kHz，不是"只有上升沿"的检测伪影。
2. **三种内部偏置都改变不了它**（2394~2457 几乎相同），说明这根线
   被外部电路强力驱动，内部上下拉无效。
3. 同一 GPIO 控制器上的触控板引脚（337）2 秒只有 1 次事件，
   排除了"控制器本身有问题"。
4. **驱动卸载后风暴依然存在**（6031 次 / 5 秒），与我们的内核模块无关。
5. 偶尔观察到短暂的"0 次"窗口，但持续测量会恢复 —— 那只是采样太短的巧合。

## 驱动侧的对照

```
驱动未加载:           边沿 1200/s（硬件自行振荡）
驱动加载、未传输:     边沿 1200/s
驱动加载、SPI 传输后: 边沿 1200/s，MISO 恒为 ff
```

**SPI 传输与该振荡无因果关系** —— 振荡是芯片自身持续输出的。

## SPI 传输本身是正常的

```
messages 在涨、bytes_tx == bytes_rx、errors=0、timedout=0
控制器 runtime_status = active
```

控制器认为每次传输都成功，但 MISO 恒为 `0xff`（无器件驱动数据线）。

## ACPI 层面的关键发现（见 spba_acpi.asl）

`_INI` 中有两次 `SHPO`（直接写 GPIO 寄存器把引脚置为输出高）：

```asl
Method (_INI, 0, NotSerialized)
{
    SHPO (0x090E0001, One)   // INT 引脚
    SHPO (0x0907000C, One)   // 复位引脚
}
```

这是 BIOS 固定的上电序列。Windows 驱动依赖它完成初始化。
Linux 下 ACPI 的 `_INI` 会在设备枚举时执行，但 `_CRS` 里
`GpioInt` 是 `PullDefault`（不指定上下拉），因此内核把该引脚切成
输入后，**没有任何上拉/下拉被配置**。

## 尚未解释的部分

- 为什么 INT 会以 1.2 kHz 持续振荡（方波，外部驱动，不受内部偏置影响）
- 为什么 MISO 恒为 ff 而 SPI 控制器报告成功

这两个现象指向同一个可能：**芯片处于未完成初始化的状态**，
可能缺 Windows 驱动在 `_INI` 之外还要做的某一步（例如通过 SPI 发送
特定命令序列使其退出某种测试/待机模式）。

## 驱动参数速查（便于现场对照）

```
reset_gpio_index=1      # _CRS 里 GPIO 资源的全局序号
reset_active_low=-1     # -1=跟随 _CRS
reset_release=0         # 0=复位后继续驱动在未复电位
irq_edge_trigger=1      # 双边沿
irq_enable=1            # 0=不注册 handler
pm_keep_awake=0
```
