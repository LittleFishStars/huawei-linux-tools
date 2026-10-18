// SPDX-License-Identifier: GPL-2.0
/*
 * gdix51c0_fp - Goodix GDIX51C0 指纹传感器 Linux 传输层驱动
 *
 * 硬件事实（HUAWEI MateBook X Pro MRGF-XX / MateBook X 家族）：
 *   - 指纹传感器 ACPI HID = GDIX51C0（汇顶 Goodix），挂在 ACPI SPI 总线上
 *     /sys/bus/spi/devices/spi-GDIX51C0:00，控制器为 pxa2xx-spi (PCI 8086:51aa)
 *   - 复位脚与中断脚都在 _CRS 里以 GpioIo / GpioInt 资源描述，走
 *     GpioInt(Edge, ActiveLow) + GpioIo(OutputOnly) 的常见写法
 *   - 传感器内部 MCU 会自己发起 TLS 1.2 PSK 握手，因此加解密必须在用户态
 *     完成（内核里没有需要暴露的密钥材料）。本模块只负责“可靠搬运字节 +
 *     复位 + 中断计数”，协议与 TLS 由 tools/ 下的 Python 栈实现。
 *
 * 设计取舍：
 *   1) 不走 spidev。spidev 只提供固定 4096 字节/32 段的一次性 ioctl 描述符，
 *      且 ACPI 设备默认不会自动绑定（要用 driver_override 硬塞），中断脚还得
 *      由用户态另外找 gpiochip 行号——而这个行号在不同 MateBook 机型上是变
 *      的（上游把 321/140 写死了）。本模块在 probe 时直接解析 _CRS，把
 *      GpioInt 变成真正的 Linux irq，GpioIo 变成 reset gpio，跨机型可用。
 *   2) 不实现"上半部读满一帧"的 DMA 流水线。传感器一次采图约 73 ms 内
 *      必须在 SPI 上保持完全静默，任何并发读都会把模拟读出截断在固定行
 *      （参考实现里著名的 0x0b78 "row-31 cliff"）。策略放到用户态更灵活，
 *      内核只提供一个可 select() 的中断计数通道。
 *   3) 单实例：一台机器只有一颗传感器，故用全局 misc 设备。
 *
 * ioctl / read / poll 语义见 include/uapi 里的自定义编号（此处内联定义）。
 */

#include <linux/acpi.h>
#include <linux/delay.h>
#include <linux/fs.h>
#include <linux/gpio/consumer.h>
#include <linux/interrupt.h>
#include <linux/ioctl.h>
#include <linux/jiffies.h>
#include <linux/miscdevice.h>
#include <linux/minmax.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/poll.h>
#include <linux/slab.h>
#include <linux/spi/spi.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/uaccess.h>
#include <linux/wait.h>
#include <linux/sprintf.h>
#include <linux/version.h>

#define GDIX51C0_DRV_NAME	"gdix51c0_fp"
#define GDIX51C0_ACPI_HID	"GDIX51C0"

/* ---- 与 tools/gdix51c0/transport.py 保持一致的 ABI ---- */

struct gdix51c0_xfer_req {
	__u32 tx_len;		/* 发送字节数（不足则补 0xff） */
	__u32 rx_len;		/* 接收字节数；全双工时 = max(tx_len, rx_len) */
	__u32 speed_hz;		/* 0 = 用模块默认值 */
	__u32 flags;		/* 保留 */
	__u64 tx_buf;		/* 用户态指针 */
	__u64 rx_buf;
};

struct gdix51c0_info {
	char hid[16];		/* ACPI HID */
	__u32 irq;		/* Linux irq 号，0 = 未取得 */
	__u32 reset_valid;	/* 1 = reset gpio 可用 */
	__u32 reset_active_low;
	__u32 speed_hz;
	__u32 chunk;		/* 单次 spi_transfer 上限 */
	__u32 irq_gpio_valid;
};

#define GDIX51C0_IOC_MAGIC	0xD1
#define GDIX51C0_IOC_XFER	_IOWR(GDIX51C0_IOC_MAGIC, 0x01, struct gdix51c0_xfer_req)
#define GDIX51C0_IOC_RESET	_IOW(GDIX51C0_IOC_MAGIC, 0x02, __u32)	/* 参数：复位保持 us */
#define GDIX51C0_IOC_INFO	_IOR(GDIX51C0_IOC_MAGIC, 0x03, struct gdix51c0_info)
#define GDIX51C0_IOC_IRQ_COUNT	_IOR(GDIX51C0_IOC_MAGIC, 0x04, __u64)
#define GDIX51C0_IOC_IRQ_LEVEL	_IOR(GDIX51C0_IOC_MAGIC, 0x05, __u32)
#define GDIX51C0_IOC_IRQ_WAIT	_IOWR(GDIX51C0_IOC_MAGIC, 0x06, __u32)	/* in: 超时 ms；out: 新计数 */

/* 一次采图窗口内的静默期，默认 80 ms（见文件头 2)） */
#define GDIX51C0_DEFAULT_SPEED_HZ	10000000U
#define GDIX51C0_DEFAULT_CHUNK		2048
#define GDIX51C0_DEFAULT_RESET_US	10000

struct gdix51c0 {
	struct device *dev;
	struct spi_device *spi;

	struct gpio_desc *reset_gpio;
	struct gpio_desc *irq_gpio;
	int irq;

	struct mutex io_lock;		/* 串行化复位与 SPI 传输 */
	wait_queue_head_t irq_wait;
	spinlock_t irq_lock;
	u64 irq_seen;			/* IRQ 累计次数 */
	u64 irq_served;			/* 用户态已消费的次数 */

	u32 speed_hz;
	u32 chunk;
	u32 reset_active_low;
	bool reset_valid;
	bool opened;
};

/* ---- 模块参数：跨机型兜底 ---- */

static int reset_gpio_index = 1;
module_param(reset_gpio_index, int, 0444);
MODULE_PARM_DESC(reset_gpio_index,
		 " _CRS 中 GPIO 资源的全局序号（GpioIo 与 GpioInt 数在一起），"
		 "指向复位脚；默认 1 对应本机 [1]=GpioIo pins=140");

static int irq_gpio_index = -1;
module_param(irq_gpio_index, int, 0444);
MODULE_PARM_DESC(irq_gpio_index,
		 "额外把第 N 个 GpioIo 当可读电平的中断脚用；-1 = 不启用");

static int irq_resource_index = 0;
module_param(irq_resource_index, int, 0444);
MODULE_PARM_DESC(irq_resource_index,
		 " _CRS 中 GpioInt 资源的序号（中断），默认 0");

static uint speed_hz = GDIX51C0_DEFAULT_SPEED_HZ;
module_param(speed_hz, uint, 0444);
MODULE_PARM_DESC(speed_hz, "SPI 时钟，默认 10000000 (10 MHz)");

/*
 * 复位脚极性：-1 = 跟随 ACPI _CRS（默认），0 = 高电平有效，1 = 低电平有效。
 *
 * 不能简单默认 false —— 本机 _CRS 明确写的是 active-low，而映射表里的
 * active_low 会覆盖 _CRS 的极性，默认 false 会把物理极性弄反。
 */
static int reset_active_low = -1;
module_param(reset_active_low, int, 0444);
MODULE_PARM_DESC(reset_active_low,
		 "复位脚极性：-1=跟随 ACPI _CRS（默认），0=高有效，1=低有效");

static uint chunk_bytes = GDIX51C0_DEFAULT_CHUNK;
module_param(chunk_bytes, uint, 0444);
MODULE_PARM_DESC(chunk_bytes, "单次 spi_transfer 上限，默认 2048");

static struct gdix51c0 *gdix51c0_singleton;
static DEFINE_MUTEX(gdix51c0_singleton_lock);

/* ------------------------------------------------------------------ */
/* 中断                                                                */
/* ------------------------------------------------------------------ */

static irqreturn_t gdix51c0_irq_handler(int irq, void *data)
{
	struct gdix51c0 *gd = data;
	unsigned long flags;

	spin_lock_irqsave(&gd->irq_lock, flags);
	gd->irq_seen++;
	spin_unlock_irqrestore(&gd->irq_lock, flags);

	wake_up_interruptible(&gd->irq_wait);
	return IRQ_HANDLED;
}

/* ------------------------------------------------------------------ */
/* SPI 传输                                                            */
/* ------------------------------------------------------------------ */

/*
 * 全双工搬运。传感器要求“写的同时就在读”，因此必须用 spi_sync 带 rx_buf
 * 的单段 transfer，不能用 spi_write 再 read 的半双工写法——后者会让图像数据
 * 错位（参考实现里有明确注释）。
 */
static int gdix51c0_xfer(struct gdix51c0 *gd, u8 *tx, u8 *rx, size_t len,
			 u32 speed_hz)
{
	size_t off = 0;
	int ret;

	while (off < len) {
		struct spi_transfer xfer = { 0 };
		struct spi_message msg;
		size_t n = min_t(size_t, len - off, (size_t)gd->chunk);

		xfer.tx_buf = tx + off;
		xfer.rx_buf = rx + off;
		xfer.len = n;
		xfer.speed_hz = speed_hz;
		xfer.bits_per_word = 8;

		spi_message_init(&msg);
		spi_message_add_tail(&xfer, &msg);

		ret = spi_sync(gd->spi, &msg);
		if (ret < 0)
			return ret;

		off += n;
	}
	return 0;
}

/* ------------------------------------------------------------------ */
/* 复位                                                               */
/* ------------------------------------------------------------------ */

static void gdix51c0_hw_reset(struct gdix51c0 *gd, u32 hold_us)
{

	if (!gd->reset_valid)
		return;

	/*
	 * gpiod_set_value*() 设的是**逻辑值**，物理极已由描述符的
	 * active_low 处理（先由 ACPI _CRS 的 polarity 提供，再用模块参数覆盖）。
	 * 所以“复位有效”恒为逻辑 1，“释放”恒为逻辑 0 —— 不要再自己算反。
	 */
	const int assert_level = 1;
	const int deassert_level = 0;
	gpiod_set_value_cansleep(gd->reset_gpio, assert_level);
	usleep_range(hold_us, hold_us + 1000);
	gpiod_set_value_cansleep(gd->reset_gpio, deassert_level);
	/* 给 MCU 启动留时间，用户态随后还会按协议再等 */
	msleep(10);
}

/* ------------------------------------------------------------------ */
/* 字符设备                                                            */
/* ------------------------------------------------------------------ */

static int gdix51c0_open(struct inode *inode, struct file *file)
{
	struct gdix51c0 *gd = gdix51c0_singleton;
	int ret = 0;

	if (!gd)
		return -ENODEV;

	mutex_lock(&gd->io_lock);
	if (gd->opened)
		ret = -EBUSY;
	else
		gd->opened = true;
	mutex_unlock(&gd->io_lock);

	if (ret)
		return ret;

	file->private_data = gd;
	return stream_open(inode, file);
}

static int gdix51c0_release(struct inode *inode, struct file *file)
{
	struct gdix51c0 *gd = file->private_data;

	if (gd) {
		mutex_lock(&gd->io_lock);
		gd->opened = false;
		mutex_unlock(&gd->io_lock);
	}
	return 0;
}

static ssize_t gdix51c0_read(struct file *file, char __user *buf,
			     size_t count, loff_t *ppos)
{
	struct gdix51c0 *gd = file->private_data;
	u64 seen;
	int ret;

	if (count < sizeof(seen))
		return -EINVAL;

	ret = wait_event_interruptible(gd->irq_wait,
				       READ_ONCE(gd->irq_seen) !=
				       READ_ONCE(gd->irq_served));
	if (ret)
		return -ERESTARTSYS;

	spin_lock_irq(&gd->irq_lock);
	seen = gd->irq_seen;
	gd->irq_served = seen;
	spin_unlock_irq(&gd->irq_lock);

	if (copy_to_user(buf, &seen, sizeof(seen)))
		return -EFAULT;

	return sizeof(seen);
}

static __poll_t gdix51c0_poll(struct file *file, poll_table *wait)
{
	struct gdix51c0 *gd = file->private_data;
	__poll_t mask = 0;

	poll_wait(file, &gd->irq_wait, wait);
	if (READ_ONCE(gd->irq_seen) != READ_ONCE(gd->irq_served))
		mask |= EPOLLIN | EPOLLRDNORM;

	return mask;
}

static long gdix51c0_ioctl_xfer(struct gdix51c0 *gd,
				struct gdix51c0_xfer_req __user *uarg)
{
	struct gdix51c0_xfer_req req;
	u8 *tx = NULL, *rx = NULL;
	size_t total;
	u32 spd;
	int ret;

	if (copy_from_user(&req, uarg, sizeof(req)))
		return -EFAULT;

	if (!req.tx_len && !req.rx_len)
		return 0;
	if (req.tx_len > SZ_1M || req.rx_len > SZ_1M)
		return -E2BIG;
	if ((req.tx_len && !req.tx_buf) || (req.rx_len && !req.rx_buf))
		return -EINVAL;

	total = max(req.tx_len, req.rx_len);
	spd = req.speed_hz ? min(req.speed_hz, gd->speed_hz) : gd->speed_hz;

	tx = kzalloc(total, GFP_KERNEL);
	rx = kzalloc(total, GFP_KERNEL);
	if (!tx || !rx) {
		ret = -ENOMEM;
		goto out;
	}

	/* 没有 tx 的区间用 0xff 填充：传感器把 0xff 当作空操作 */
	memset(tx, 0xff, total);
	if (req.tx_len &&
	    copy_from_user(tx, u64_to_user_ptr(req.tx_buf), req.tx_len)) {
		ret = -EFAULT;
		goto out;
	}

	mutex_lock(&gd->io_lock);
	ret = gdix51c0_xfer(gd, tx, rx, total, spd);
	mutex_unlock(&gd->io_lock);

	if (ret)
		goto out;

	if (req.rx_len &&
	    copy_to_user(u64_to_user_ptr(req.rx_buf), rx, req.rx_len)) {
		ret = -EFAULT;
		goto out;
	}

	ret = 0;
out:
	kfree(tx);
	kfree(rx);
	return ret;
}

static long gdix51c0_unlocked_ioctl(struct file *file, unsigned int cmd,
				    unsigned long arg)
{
	struct gdix51c0 *gd = file->private_data;
	void __user *uarg = (void __user *)arg;
	struct gdix51c0_info info;
	u64 count;
	u32 val;
	int ret = 0;

	switch (cmd) {
	case GDIX51C0_IOC_XFER:
		return gdix51c0_ioctl_xfer(gd, uarg);

	case GDIX51C0_IOC_RESET:
		if (get_user(val, (u32 __user *)uarg))
			return -EFAULT;
		if (val > 1000000)
			return -EINVAL;
		mutex_lock(&gd->io_lock);
		gdix51c0_hw_reset(gd, val);
		mutex_unlock(&gd->io_lock);
		return 0;

	case GDIX51C0_IOC_INFO:
		memset(&info, 0, sizeof(info));
		strscpy(info.hid, GDIX51C0_ACPI_HID, sizeof(info.hid));
		info.irq = gd->irq > 0 ? (u32)gd->irq : 0;
		info.reset_valid = gd->reset_valid ? 1 : 0;
		info.reset_active_low = gd->reset_active_low ? 1 : 0;
		info.speed_hz = gd->speed_hz;
		info.chunk = gd->chunk;
		info.irq_gpio_valid = gd->irq_gpio ? 1 : 0;
		if (copy_to_user(uarg, &info, sizeof(info)))
			return -EFAULT;
		return 0;

	case GDIX51C0_IOC_IRQ_COUNT:
		spin_lock_irq(&gd->irq_lock);
		count = gd->irq_seen;
		spin_unlock_irq(&gd->irq_lock);
		if (copy_to_user(uarg, &count, sizeof(count)))
			return -EFAULT;
		return 0;

	case GDIX51C0_IOC_IRQ_LEVEL:
		if (!gd->irq_gpio)
			return -ENODEV;
		val = gpiod_get_value_cansleep(gd->irq_gpio) ? 1 : 0;
		if (copy_to_user(uarg, &val, sizeof(val)))
			return -EFAULT;
		return 0;

	case GDIX51C0_IOC_IRQ_WAIT:
		if (get_user(val, (u32 __user *)uarg))
			return -EFAULT;
		ret = wait_event_interruptible_timeout(
			gd->irq_wait,
			READ_ONCE(gd->irq_seen) != READ_ONCE(gd->irq_served),
			msecs_to_jiffies(val ? val : 1));
		if (ret < 0)
			return ret;
		spin_lock_irq(&gd->irq_lock);
		count = gd->irq_seen;
		gd->irq_served = count;
		spin_unlock_irq(&gd->irq_lock);
		if (copy_to_user(uarg, &count, sizeof(count)))
			return -EFAULT;
		return ret == 0 ? -ETIMEDOUT : 0;

	default:
		return -ENOTTY;
	}
}

static const struct file_operations gdix51c0_fops = {
	.owner		= THIS_MODULE,
	.open		= gdix51c0_open,
	.release	= gdix51c0_release,
	.read		= gdix51c0_read,
	.poll		= gdix51c0_poll,
	.unlocked_ioctl	= gdix51c0_unlocked_ioctl,
};

static struct miscdevice gdix51c0_miscdev = {
	.name	= "gdix51c0",
	.fops	= &gdix51c0_fops,
	.minor	= MISC_DYNAMIC_MINOR,
	.mode	= 0600,
};

/* ------------------------------------------------------------------ */
/* sysfs                                                              */
/* ------------------------------------------------------------------ */

static ssize_t info_show(struct device *dev, struct device_attribute *attr,
			 char *buf)
{
	struct gdix51c0 *gd = dev_get_drvdata(dev);

	return sysfs_emit(buf,
			  "hid=%s irq=%d reset=%s irq_gpio=%s speed=%u chunk=%u\n",
			  GDIX51C0_ACPI_HID, gd->irq,
			  gd->reset_valid ? "yes" : "no",
			  gd->irq_gpio ? "yes" : "no",
			  gd->speed_hz, gd->chunk);
}
static DEVICE_ATTR_RO(info);

/* ---- _CRS 资源转储：把 ACPI 到底声明了哪些资源打印出来 ----------------
 *
 * 这个属性是排查 GPIO 解析失败的唯一可靠手段。GpioIo 找不到时（-ENOENT）
 * 光靠改 index 试不出来——必须知道 _CRS 里究竟有几个 GpioIo、几个 GpioInt，
 * 以及每个资源的引脚号、极性、触发方式。
 */


static const char *gdix51c0_trigger_name(u8 t)
{
	switch (t) {
	case ACPI_EDGE_SENSITIVE:  return "edge";
	case ACPI_LEVEL_SENSITIVE: return "level";
	default:                   return "?";
	}
}

static const char *gdix51c0_polarity_name(u8 p)
{
	if (p & ACPI_ACTIVE_LOW)
		return "active-low";
	return "active-high";
}

struct gdix51c0_crs_ctx {
	char *buf;
	unsigned int len;
	unsigned int size;
	unsigned int gpio_io_idx;
	unsigned int gpio_int_idx;
	unsigned int gpio_all_idx;   /* GpioIo+GpioInt 的全局序号 = crs_entry_index */
};

static void crs_emit(struct gdix51c0_crs_ctx *ctx, const char *fmt, ...)
{
	va_list ap;
	int n;

	if (ctx->len >= ctx->size)
		return;

	va_start(ap, fmt);
	n = vscnprintf(ctx->buf + ctx->len, ctx->size - ctx->len, fmt, ap);
	va_end(ap);

	if (n > 0)
		ctx->len += min((unsigned int)n, ctx->size - ctx->len);
}

static int crs_one_cb(struct acpi_resource *ares, void *data)
{
	struct gdix51c0_crs_ctx *ctx = data;
	struct acpi_resource_gpio *agpio;
	struct acpi_resource_irq *airq;
	struct acpi_resource_extended_irq *aeirq;
	int i;

	switch (ares->type) {
	case ACPI_RESOURCE_TYPE_GPIO:
		agpio = &ares->data.gpio;
		if (agpio->connection_type == ACPI_RESOURCE_GPIO_TYPE_INT) {
			crs_emit(ctx, "[%u] GpioInt  pins=", ctx->gpio_all_idx);
		} else {
			crs_emit(ctx, "[%u] GpioIo   pins=", ctx->gpio_all_idx);
		}
		/* 全局序号就是 acpi_gpio_params.crs_entry_index 要填的值 */
		ctx->gpio_all_idx++;
		if (agpio->connection_type == ACPI_RESOURCE_GPIO_TYPE_INT)
			ctx->gpio_int_idx++;
		else
			ctx->gpio_io_idx++;
		for (i = 0; i < agpio->pin_table_length; i++)
			crs_emit(ctx, i ? ",%u" : "%u", agpio->pin_table[i]);
		crs_emit(ctx, " %s %s",
			 gdix51c0_trigger_name(agpio->triggering),
			 gdix51c0_polarity_name(agpio->polarity));
		if (agpio->resource_source.string_length > 0)
			crs_emit(ctx, " src=%.*s",
				 agpio->resource_source.string_length,
				 agpio->resource_source.string_ptr);
		crs_emit(ctx, "\n");
		break;

	case ACPI_RESOURCE_TYPE_IRQ:
		airq = &ares->data.irq;
		crs_emit(ctx, "     IRQ     ");
		for (i = 0; i < airq->interrupt_count; i++)
			crs_emit(ctx, i ? ",%u" : "%u", airq->interrupts[i]);
		crs_emit(ctx, " %s %s\n",
			 gdix51c0_trigger_name(airq->triggering),
			 gdix51c0_polarity_name(airq->polarity));
		break;

	case ACPI_RESOURCE_TYPE_EXTENDED_IRQ:
		aeirq = &ares->data.extended_irq;
		crs_emit(ctx, "     ExtIRQ  ");
		for (i = 0; i < aeirq->interrupt_count; i++)
			crs_emit(ctx, i ? ",%u" : "%u", aeirq->interrupts[i]);
		crs_emit(ctx, " %s %s\n",
			 gdix51c0_trigger_name(aeirq->triggering),
			 gdix51c0_polarity_name(aeirq->polarity));
		break;

	default:
		crs_emit(ctx, "     其它     type=%u\n", ares->type);
		break;
	}

	return AE_OK;
}

static ssize_t resources_show(struct device *dev, struct device_attribute *attr,
			      char *buf)
{
	struct acpi_device *adev = ACPI_COMPANION(dev);
	struct gdix51c0_crs_ctx ctx;
	LIST_HEAD(res_list);
	int ret;

	ctx.buf = buf;
	ctx.len = 0;
	ctx.size = PAGE_SIZE;
	ctx.gpio_io_idx = 0;
	ctx.gpio_int_idx = 0;

	if (!adev)
		return sysfs_emit(buf, "非 ACPI 枚举设备\n");

	ret = acpi_dev_get_resources(adev, &res_list, crs_one_cb, &ctx);
	acpi_dev_free_resource_list(&res_list);
	if (ret < 0)
		return sysfs_emit(buf, "_CRS 读取失败: %d\n", ret);

	crs_emit(&ctx, "\n合计：GPIO 资源 %u 个（GpioIo=%u GpioInt=%u）\n",
		 ctx.gpio_all_idx, ctx.gpio_io_idx, ctx.gpio_int_idx);
	crs_emit(&ctx,
		 "上面 [] 里的数字就是 crs_entry_index；当前模块参数："
		 "reset_gpio_index=%d irq_resource_index=%d irq_gpio_index=%d\n",
		 reset_gpio_index, irq_resource_index, irq_gpio_index);

	return ctx.len;
}
static DEVICE_ATTR_RO(resources);

static ssize_t irq_count_show(struct device *dev, struct device_attribute *attr,
			      char *buf)
{
	struct gdix51c0 *gd = dev_get_drvdata(dev);
	u64 count;

	spin_lock_irq(&gd->irq_lock);
	count = gd->irq_seen;
	spin_unlock_irq(&gd->irq_lock);

	return sysfs_emit(buf, "%llu\n", count);
}
static DEVICE_ATTR_RO(irq_count);

static struct attribute *gdix51c0_attrs[] = {
	&dev_attr_info.attr,
	&dev_attr_resources.attr,
	&dev_attr_irq_count.attr,
	NULL,
};
ATTRIBUTE_GROUPS(gdix51c0);

/* ------------------------------------------------------------------ */
/* probe / remove                                                     */
/* ------------------------------------------------------------------ */
/* ---- ACPI _CRS 序号 -> GPIO 描述符 ----------------------------------
 *
 * 关键点：devm_gpiod_get_index(dev, NULL, idx, ...) 在 con_id == NULL 时
 * 走的是 _DSD 属性查找（找名为 "gpios" 的属性），不是 _CRS 资源序号。
 * 本机的 _CRS 里明明有 GpioIo pins=140，但因为没有 _DSD，所以那个调用
 * 返回 -ENOENT。
 *
 * 要对 _CRS 里的第 N 个 GPIOn 资源取句柄，必须先通过
 * devm_acpi_dev_add_driver_gpios() 注册一张映射表，表项里的
 * crs_entry_index 就是资源序号。之后再用映射名去取。
 *
 * 注意 crs_entry_index 数的是 _CRS 里
 * 所有 GPIO 资源（GpioIo + GpioInt 混在一起）的序号，
 * 不是“第几个 GpioIo”。本机顺序是 [0]=GpioInt(pins=321)、
 * [1]=GpioIo(pins=140)，所以复位脚的 crs_entry_index 是 1。
 */

/*
 * 映射表必须在运行时填 —— crs_entry_index 取决于 _CRS 里 GPIO 资源的顺序，
 * 不同机型可能不同。所以这里用非 const 的可写数组。
 */
static struct acpi_gpio_params gdix51c0_reset_gpio_params[] = {
	{ 0, 0, false },
};

static struct acpi_gpio_params gdix51c0_irq_gpio_params[] = {
	{ 0, 0, false },
};

static struct acpi_gpio_mapping gdix51c0_gpio_mappings[] = {
	{ "reset-gpios", gdix51c0_reset_gpio_params, 1, 0 },
	{ "irq-gpios", gdix51c0_irq_gpio_params, 1, 0 },
	{ }
};


static int gdix51c0_setup_power_and_irq(struct gdix51c0 *gd)
{
	struct acpi_device *adev = ACPI_COMPANION(gd->dev);
	int ret;

	if (!adev) {
		dev_err(gd->dev, "不是 ACPI 枚举的 SPI 设备，无法解析 _CRS\n");
		return -ENODEV;
	}

	/*
	 * 复位脚：先把 _CRS 里的资源序号映射成一个具名属性，再按名字取。
	 *
	 * 注意 reset_gpio_index 的语义变了：它现在是 _CRS 里 GPIO 资源的
	 * 全局序号（GpioIo 与 GpioInt 数在一起），不是“第几个 GpioIo”。
	 * 本机 _CRS： [0]=GpioInt pins=321、 [1]=GpioIo pins=140
	 * 所以默认值取 1（默认见模块参数定义）。
	 */
	gdix51c0_reset_gpio_params[0].crs_entry_index = reset_gpio_index;
	gdix51c0_reset_gpio_params[0].line_index = 0;
	/*
	 * active_low 在 acpi_gpio_params 里是覆盖位：只能改真/假，无法表达
	 * “不覆盖”。所以 -1（默认）时只能取与 _CRS 一致的值。
	 * 本机 _CRS 写的是 active-low，上游驱动与 Windows 也当低有效复位，
	 * 所以 -1 暂按 true 处理；其它机型可用模块参数强制指定。
	 */
	gdix51c0_reset_gpio_params[0].active_low =
		(reset_active_low < 0) ? true : (reset_active_low != 0);

	ret = devm_acpi_dev_add_driver_gpios(gd->dev, gdix51c0_gpio_mappings);
	if (ret)
		dev_warn(gd->dev,
			 "ACPI GPIO 映射注册失败: %d（复位可能不可用）\n", ret);

	gd->reset_gpio = devm_gpiod_get_index(
		gd->dev, "reset", 0,
		gdix51c0_reset_gpio_params[0].active_low ? GPIOD_OUT_HIGH
							: GPIOD_OUT_LOW);
	if (IS_ERR(gd->reset_gpio)) {
		ret = PTR_ERR(gd->reset_gpio);
		gd->reset_gpio = NULL;
		dev_warn(gd->dev,
			 "复位脚解析失败 (crs_entry_index=%d, %d)，复位功能不可用\n",
			 reset_gpio_index, ret);
	} else {
		gd->reset_valid = true;
		gd->reset_active_low = gdix51c0_reset_gpio_params[0].active_low ? 1 : 0;
		dev_info(gd->dev,
			 "复位脚就绪 (crs_entry_index=%d, active_low=%d)\n",
			 reset_gpio_index,
			 gdix51c0_reset_gpio_params[0].active_low ? 1 : 0);
	}

	/* 中断：_CRS 里的 GpioInt 资源 -> 真实 Linux irq */
	gd->irq = acpi_dev_gpio_irq_get(adev, irq_resource_index);
	if (gd->irq < 0) {
		dev_warn(gd->dev,
			 "GpioInt 解析失败 (index=%d, %d)，只能用轮询模式\n",
			 irq_resource_index, gd->irq);
		gd->irq = 0;
	} else {
		ret = devm_request_irq(gd->dev, gd->irq, gdix51c0_irq_handler,
				       0, "gdix51c0-fp", gd);
		if (ret) {
			dev_warn(gd->dev, "request_irq(%d) 失败: %d\n",
				 gd->irq, ret);
			gd->irq = 0;
		} else {
			dev_info(gd->dev, "中断就绪 irq=%d\n", gd->irq);
		}
	}

	/* 可选：把某个 GpioIo 当可读电平的中断脚（调试用） */
	if (irq_gpio_index >= 0) {
		gdix51c0_irq_gpio_params[0].crs_entry_index = irq_gpio_index;
		gdix51c0_irq_gpio_params[0].line_index = 0;
		gd->irq_gpio = devm_gpiod_get_index(gd->dev, "irq", 0, GPIOD_IN);
		if (IS_ERR(gd->irq_gpio)) {
			dev_warn(gd->dev, "irq_gpio_index=%d 解析失败\n",
				 irq_gpio_index);
			gd->irq_gpio = NULL;
		}
	}

	return 0;
}

static int gdix51c0_probe(struct spi_device *spi)
{
	struct gdix51c0 *gd;
	int ret;

	mutex_lock(&gdix51c0_singleton_lock);
	if (gdix51c0_singleton) {
		mutex_unlock(&gdix51c0_singleton_lock);
		return -EBUSY;
	}

	gd = devm_kzalloc(&spi->dev, sizeof(*gd), GFP_KERNEL);
	if (!gd) {
		mutex_unlock(&gdix51c0_singleton_lock);
		return -ENOMEM;
	}

	gd->dev = &spi->dev;
	gd->spi = spi;
	gd->speed_hz = speed_hz;
	gd->chunk = chunk_bytes ? chunk_bytes : GDIX51C0_DEFAULT_CHUNK;
	/* gd->reset_active_low 稍后在 setup_power_and_irq 里按实际解析结果填 */
	mutex_init(&gd->io_lock);
	init_waitqueue_head(&gd->irq_wait);
	spin_lock_init(&gd->irq_lock);
	dev_set_drvdata(&spi->dev, gd);

	spi->mode = SPI_MODE_0;
	spi->bits_per_word = 8;
	spi->max_speed_hz = gd->speed_hz;
	ret = spi_setup(spi);
	if (ret < 0) {
		dev_err(&spi->dev, "spi_setup 失败: %d\n", ret);
		mutex_unlock(&gdix51c0_singleton_lock);
		return ret;
	}

	ret = gdix51c0_setup_power_and_irq(gd);
	if (ret < 0) {
		mutex_unlock(&gdix51c0_singleton_lock);
		return ret;
	}

	/* 上电复位一次，让 MCU 处于确定状态 */
	gdix51c0_hw_reset(gd, GDIX51C0_DEFAULT_RESET_US);

	ret = misc_register(&gdix51c0_miscdev);
	if (ret < 0) {
		dev_err(&spi->dev, "misc_register 失败: %d\n", ret);
		mutex_unlock(&gdix51c0_singleton_lock);
		return ret;
	}

	gdix51c0_singleton = gd;
	mutex_unlock(&gdix51c0_singleton_lock);

	dev_info(&spi->dev,
		 "%s: /dev/gdix51c0 已就绪 (spi=%s, speed=%uHz, irq=%d)\n",
		 GDIX51C0_DRV_NAME, dev_name(&spi->dev), gd->speed_hz, gd->irq);
	return 0;
}

static void gdix51c0_remove(struct spi_device *spi)
{
	struct gdix51c0 *gd = dev_get_drvdata(&spi->dev);

	mutex_lock(&gdix51c0_singleton_lock);
	if (gd) {
		misc_deregister(&gdix51c0_miscdev);
		if (gd->reset_valid)
			gpiod_set_value_cansleep(gd->reset_gpio,
						 gd->reset_active_low ? 1 : 0);
	}
	gdix51c0_singleton = NULL;
	mutex_unlock(&gdix51c0_singleton_lock);

	dev_info(&spi->dev, "%s 已卸载\n", GDIX51C0_DRV_NAME);
}

static const struct acpi_device_id gdix51c0_acpi_ids[] = {
	{ GDIX51C0_ACPI_HID, 0 },
	{ }
};
MODULE_DEVICE_TABLE(acpi, gdix51c0_acpi_ids);

static struct spi_driver gdix51c0_spi_driver = {
	.driver = {
		.name		= GDIX51C0_DRV_NAME,
		.acpi_match_table = gdix51c0_acpi_ids,
		.dev_groups	= gdix51c0_groups,
	},
	.probe		= gdix51c0_probe,
	.remove		= gdix51c0_remove,
};

module_spi_driver(gdix51c0_spi_driver);

MODULE_AUTHOR("ylxc");
MODULE_DESCRIPTION("Goodix GDIX51C0 指纹传感器 SPI/GPIO 传输层驱动 (HUAWEI MateBook X Pro)");
MODULE_LICENSE("GPL");
MODULE_ALIAS("acpi:" GDIX51C0_ACPI_HID);
