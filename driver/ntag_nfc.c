// SPDX-License-Identifier: GPL-2.0
/*
 * ntag_nfc.c - 华为 NTAG0001 内置 NFC 控制器（SPB/I2C 挂载）Linux 适配驱动
 *
 * 目标硬件
 *   机型     : Huawei MateBook X Pro 2023 (MRGF-XX)
 *   ACPI     : _HID = "NTAG0001"   _CID = 无   (Windows 侧 INF 用 ACPI\NTAG0001 绑定)
 *   总线     : Intel LPSS DesignWare I2C  (0000:00:15.0 -> i2c-0)  + 一根 GPIO 中断线
 *
 * 为什么用内核 i2c 驱动而不是直接操作 /dev/i2c-0
 *   内核在启动时已经按 ACPI _CRS 里的 I2cSerialBus 资源创建好了 i2c client
 *   "i2c-NTAG0001:00"（见 /sys/bus/i2c/devices/），从地址与 GpioInt 中断号都写在
 *   ACPI 表里。绑上这个 client 就自动拿到这两样东西，不需要在代码里硬编码从地址。
 *
 * 本驱动做四件事
 *   1. 绑定 ACPI\NTAG0001，打印从地址 / 中断号 / 控制器能力（逆向所需的关键事实）
 *   2. 提供 /dev/ntag_nfc：写 = 原始 I2C 写，读 = 原始 I2C 读（供协议逆向用）
 *   3. 注册中断：只记录次数与时间戳，不假设芯片的语义
 *   4. probe_on_load=1（默认）时按几种常见 NCI 帧格式发 CORE_RESET_CMD，
 *   4. 不主动写芯片：0x57 上极可能是一颗 NFC Forum Type 2 Tag（16 字节页），
 *      盲写会误改配置/锁定位。需要探测时用 probe_on_load=1 显式开启。
 *
 * 构建:  make           (需要 linux-headers，Arch: pacman -S linux-headers)
 * 装载:  sudo insmod ntag_nfc.ko
 * 观察:  sudo dmesg | grep ntag
 */

#include <linux/module.h>
#include <linux/irq.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/i2c.h>
#include <linux/acpi.h>
#include <linux/interrupt.h>
#include <linux/miscdevice.h>
#include <linux/fs.h>
#include <linux/uaccess.h>
#include <linux/mutex.h>
#include <linux/delay.h>
#include <linux/slab.h>
#include <linux/ktime.h>
#include <linux/atomic.h>
#include <linux/sysfs.h>

#define NTAG_DRV_NAME	"ntag_nfc"
#define NTAG_MAX_XFER	512

/* NCI 2.0: CORE_RESET_CMD, GID=0 Core / MT=1 Command, payload = reset config */
static const u8 nci_core_reset[] = { 0x20, 0x00, 0x01, 0x01 };

struct ntag_dev {
	struct i2c_client	*client;
	struct miscdevice	misc;
	struct mutex		lock;		/* 串行化所有 I2C 访问 */
	u8			last_rx[NTAG_MAX_XFER];
	size_t			last_rx_len;
	int			irq;
	atomic_t		irq_count;
	ktime_t			irq_last;
	bool			irq_ok;
};

static struct ntag_dev *ntag;

static bool probe_on_load = false;    /* 默认不发任何数据：0x57 上的很可能是一颗 Type 2 Tag，盲写会毁掉它的配置页 */
/* 中断触发类型：0=按 ACPI 自动（默认），1=上升沿，2=下降沿，3=双边沿。
 * 板级布线有时与 ACPI 声明不一致，用这个参数现场纠偏，不必改代码重编。
 */
static int irq_type = 0;
module_param(irq_type, int, 0644);
MODULE_PARM_DESC(irq_type, "0=ACPI auto, 1=rising, 2=falling, 3=both");
module_param(probe_on_load, bool, 0644);
MODULE_PARM_DESC(probe_on_load, "装载时发 NCI CORE_RESET 探测 (默认 0；目标若是 NFC 标签切勿开启)");

static unsigned int read_len = 32;
module_param(read_len, uint, 0644);
MODULE_PARM_DESC(read_len, "探测读回的最大字节数 (默认 32)");

/* ------------------------------------------------------------------ */
/* 底层 I2C 原语                                                        */
/* ------------------------------------------------------------------ */

static int ntag_i2c_write(struct ntag_dev *nd, const u8 *buf, u16 len)
{
	struct i2c_msg msg = {
		.addr	= nd->client->addr,
		.flags	= 0,
		.len	= len,
		.buf	= (u8 *)buf,
	};
	int ret = i2c_transfer(nd->client->adapter, &msg, 1);

	if (ret == 1)
		return 0;
	if (ret < 0)
		return ret;
	return -EIO;
}

/*
 * 先可选地写一个前缀（长度字节 / 0x00 头），再读 len 字节。
 * 不同厂家的 NCI-over-I2C 读法不一致，这里把前缀暴露出来便于逐个试。
 */
static int ntag_i2c_read(struct ntag_dev *nd, u8 *buf, u16 len,
			 const u8 *prefix, u16 plen)
{
	struct i2c_msg msg[2];
	int ret, n = 0;

	if (plen) {
		msg[n].addr	= nd->client->addr;
		msg[n].flags	= 0;
		msg[n].len	= plen;
		msg[n].buf	= (u8 *)prefix;
		n++;
	}
	msg[n].addr	= nd->client->addr;
	msg[n].flags	= I2C_M_RD;
	msg[n].len	= len;
	msg[n].buf	= buf;
	n++;

	ret = i2c_transfer(nd->client->adapter, msg, n);
	if (ret == n)
		return 0;
	if (ret < 0)
		return ret;
	return -EIO;
}

static void ntag_hexdump(const char *tag, const u8 *buf, size_t len)
{
	char line[3 * 64 + 1];
	size_t i, off = 0;

	for (i = 0; i < len && off + 4 < sizeof(line); i++)
		off += scnprintf(line + off, sizeof(line) - off, "%02x ", buf[i]);
	dev_info(&ntag->client->dev, "%s [%zu] %s\n", tag, len, line);
}

/* ------------------------------------------------------------------ */
/* 中断：只做记录，不假设语义（Windows 侧中断用于唤醒挂起的 16 字节应答） */
/* ------------------------------------------------------------------ */

static irqreturn_t ntag_irq_handler(int irq, void *data)
{
	struct ntag_dev *nd = data;

	atomic_inc(&nd->irq_count);
	nd->irq_last = ktime_get();
	return IRQ_HANDLED;
}

/* ------------------------------------------------------------------ */
/* 探测：判断芯片是否说 NFC Forum NCI                                   */
/* ------------------------------------------------------------------ */

struct ntag_framing {
	const char	*name;
	u8		wprefix[2];	/* 写 CORE_RESET 前加的前缀 */
	u16		wprefix_len;
	u8		rprefix[2];	/* 读之前加的前缀 */
	u16		rprefix_len;
};

static const struct ntag_framing framings[] = {
	{ "raw            ", { 0 }, 0, { 0 }, 0 },
	{ "len-byte-w     ", { 0 }, 0, { 0 }, 0 },   /* 写: 见下面特殊处理 */
	{ "0x00-head      ", { 0x00 }, 1, { 0x00 }, 1 },
};

static int ntag_probe_nci(struct ntag_dev *nd)
{
	u8 tx[8], rx[NTAG_MAX_XFER];
	int i, ret, found = 0;

	for (i = 0; i < ARRAY_SIZE(framings); i++) {
		const struct ntag_framing *f = &framings[i];
		u16 txlen = 0;

		/* 组装写入帧 */
		if (f->wprefix_len)
			memcpy(tx + txlen, f->wprefix, f->wprefix_len),
			txlen += f->wprefix_len;
		if (i == 1)			/* "len-byte-w": 前置长度字节 */
			tx[txlen++] = sizeof(nci_core_reset);
		memcpy(tx + txlen, nci_core_reset, sizeof(nci_core_reset));
		txlen += sizeof(nci_core_reset);

		ntag_hexdump("probe: write NCI CORE_RESET_CMD", tx, txlen);
		ret = ntag_i2c_write(nd, tx, txlen);
		if (ret) {
			dev_info(&ntag->client->dev,
				 "probe[%s] write failed: %d\n", f->name, ret);
			continue;
		}
		msleep(60);			/* NCI 允许 CORE_RESET 有数百 ms 延迟 */

		memset(rx, 0, sizeof(rx));
		ret = ntag_i2c_read(nd, rx, read_len, f->rprefix, f->rprefix_len);
		if (ret) {
			dev_info(&ntag->client->dev,
				 "probe[%s] read failed: %d\n", f->name, ret);
			continue;
		}
		ntag_hexdump("probe: response", rx, min_t(unsigned int, read_len, 32));

		/* CORE_RESET_RSP: MT=2/GID=0 -> 0x40 0x00 0x01 status */
		if (rx[0] == 0x40 && rx[1] == 0x00) {
			dev_info(&ntag->client->dev,
				 "probe[%s] *** 命中 NCI: CORE_RESET_RSP status=0x%02x ***\n",
				 f->name, rx[3]);
			found = 1;
		}
		/* CORE_RESET_NTF: 0x60 0x00 ... 里面带厂商/芯片信息 */
		if (rx[0] == 0x60 && rx[1] == 0x00)
			dev_info(&ntag->client->dev,
				 "probe[%s] 收到 CORE_RESET_NTF，芯片自报信息见上面的 hexdump\n",
				 f->name);
	}
	return found;
}

/* ------------------------------------------------------------------ */
/* /dev/ntag_nfc：原始读写通道                                          */
/* ------------------------------------------------------------------ */

static ssize_t ntag_dev_read(struct file *fp, char __user *ubuf,
			     size_t len, loff_t *ppos)
{
	struct ntag_dev *nd = fp->private_data;
	u8 *buf;
	int ret;

	if (len == 0 || len > NTAG_MAX_XFER)
		return -EINVAL;
	buf = kzalloc(len, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;

	mutex_lock(&nd->lock);
	ret = ntag_i2c_read(nd, buf, len, NULL, 0);
	if (!ret) {
		memcpy(nd->last_rx, buf, len);
		nd->last_rx_len = len;
	}
	mutex_unlock(&nd->lock);

	if (ret) {
		kfree(buf);
		return ret;
	}
	if (copy_to_user(ubuf, buf, len)) {
		kfree(buf);
		return -EFAULT;
	}
	kfree(buf);
	return len;
}

static ssize_t ntag_dev_write(struct file *fp, const char __user *ubuf,
			      size_t len, loff_t *ppos)
{
	struct ntag_dev *nd = fp->private_data;
	u8 *buf;
	int ret;

	if (len == 0 || len > NTAG_MAX_XFER)
		return -EINVAL;
	buf = memdup_user(ubuf, len);
	if (IS_ERR(buf))
		return PTR_ERR(buf);

	mutex_lock(&nd->lock);
	ret = ntag_i2c_write(nd, buf, len);
	mutex_unlock(&nd->lock);
	kfree(buf);

	return ret ? ret : (ssize_t)len;
}

static long ntag_dev_ioctl(struct file *fp, unsigned int cmd, unsigned long arg)
{
	/* 目前只用 read/write；保留 ioctl 入口给后续 NCI 栈 */
	return -ENOTTY;
}

static int ntag_dev_open(struct inode *ino, struct file *fp)
{
	fp->private_data = ntag;
	return 0;
}

static const struct file_operations ntag_fops = {
	.owner		= THIS_MODULE,
	.open		= ntag_dev_open,
	.read		= ntag_dev_read,
	.write		= ntag_dev_write,
	.unlocked_ioctl	= ntag_dev_ioctl,
	.llseek		= noop_llseek,
};

/* ------------------------------------------------------------------ */
/* sysfs                                                                */
/* ------------------------------------------------------------------ */

static ssize_t info_show(struct device *dev, struct device_attribute *attr,
			 char *buf)
{
	struct ntag_dev *nd = ntag;

	return sysfs_emit(buf,
		"acpi_id     : NTAG0001\n"
		"i2c_adapter : %s (nr %d)\n"
		"i2c_addr    : 0x%02x\n"
		"irq         : %d (%s)\n"
		"irq_count   : %d\n"
		"last_rx_len : %zu\n",
		nd->client->adapter->name, nd->client->adapter->nr,
		nd->client->addr, nd->irq, nd->irq_ok ? "reserved" : "none",
		atomic_read(&nd->irq_count), nd->last_rx_len);
}
static DEVICE_ATTR_RO(info);

static ssize_t last_rx_show(struct device *dev, struct device_attribute *attr,
			    char *buf)
{
	struct ntag_dev *nd = ntag;

	return sysfs_emit(buf, "%*phN\n", (int)nd->last_rx_len, nd->last_rx);
}
static DEVICE_ATTR_RO(last_rx);

static ssize_t warm_reset_store(struct device *dev, struct device_attribute *attr,
				const char *buf, size_t len)
{
	int ret;

	mutex_lock(&ntag->lock);
	ntag->last_rx_len = 0;
	ret = ntag_probe_nci(ntag);
	mutex_unlock(&ntag->lock);

	return ret ? 1 : len;
}
static DEVICE_ATTR_WO(warm_reset);

static struct attribute *ntag_attrs[] = {
	&dev_attr_info.attr,
	&dev_attr_last_rx.attr,
	&dev_attr_warm_reset.attr,
	NULL,
};
ATTRIBUTE_GROUPS(ntag);

/* ------------------------------------------------------------------ */
/* probe / remove                                                       */
/* ------------------------------------------------------------------ */

static int ntag_probe(struct i2c_client *client)
{
	struct ntag_dev *nd;
	int irq, ret = 0;

	if (!i2c_check_functionality(client->adapter, I2C_FUNC_I2C)) {
		dev_err(&client->dev, "adapter 不支持 I2C_FUNC_I2C\n");
		return -ENODEV;
	}

	nd = devm_kzalloc(&client->dev, sizeof(*nd), GFP_KERNEL);
	if (!nd)
		return -ENOMEM;
	nd->client = client;
	mutex_init(&nd->lock);
	atomic_set(&nd->irq_count, 0);
	i2c_set_clientdata(client, nd);
	ntag = nd;

	/* 从地址是 ACPI _CRS 里 I2cSerialBus 声明的；中断号来自同处 GpioInt */
	dev_info(&client->dev,
		 "绑定成功: adapter=%s(nr=%d) i2c_addr=0x%02x irq=%d\n",
		 client->adapter->name, client->adapter->nr,
		 client->addr, client->irq);

	irq = client->irq;
	if (irq <= 0 && ACPI_COMPANION(&client->dev))
		irq = acpi_dev_gpio_irq_get(ACPI_COMPANION(&client->dev), 0);
	nd->irq = irq;
	if (irq > 0) {
		/* 触发极性以 ACPI 为准：_CRS 里是 GpioInt(Edge, ActiveHigh)，
		 * 即上升沿。irq_get_trigger_type() 返回内核按 ACPI 映射好的类型，
		 * 只有它为空时才回落到上升沿。之前硬编码下降沿是错的。
		 */
		unsigned long tf = irq_get_trigger_type(irq);
		const char *tfdesc;

		if (!tf)
			tf = IRQF_TRIGGER_RISING;

		switch (irq_type) {
		case 1: tf = IRQF_TRIGGER_RISING;  break;
		case 2: tf = IRQF_TRIGGER_FALLING; break;
		case 3: tf = IRQF_TRIGGER_RISING | IRQF_TRIGGER_FALLING; break;
		default: break;
		}

		switch (tf & (IRQF_TRIGGER_RISING | IRQF_TRIGGER_FALLING |
			      IRQF_TRIGGER_HIGH | IRQF_TRIGGER_LOW)) {
		case IRQF_TRIGGER_RISING:  tfdesc = "上升沿"; break;
		case IRQF_TRIGGER_FALLING: tfdesc = "下降沿"; break;
		case IRQF_TRIGGER_HIGH:    tfdesc = "高电平"; break;
		case IRQF_TRIGGER_LOW:     tfdesc = "低电平"; break;
		default:                   tfdesc = "双边沿/其它"; break;
		}

		ret = devm_request_threaded_irq(&client->dev, irq, NULL,
						ntag_irq_handler,
						IRQF_ONESHOT | tf,
						NTAG_DRV_NAME, nd);
		if (ret) {
			dev_warn(&client->dev,
				 "申请中断 %d 失败(%d)，继续存活\n", irq, ret);
			nd->irq_ok = false;
			ret = 0;
		} else {
			nd->irq_ok = true;
			dev_info(&client->dev,
				 "中断 %d 已注册(%s, oneshot)，flags=0x%lx\n",
				 irq, tfdesc, tf);
		}
	} else {
		dev_warn(&client->dev, "没有可用的 GpioInt（ACPI 未声明或 GPIO 未就绪）\n");
	}

	nd->misc.name  = NTAG_DRV_NAME;
	nd->misc.minor = MISC_DYNAMIC_MINOR;
	nd->misc.fops  = &ntag_fops;
	nd->misc.mode  = 0600;
	ret = misc_register(&nd->misc);
	if (ret) {
		dev_err(&client->dev, "misc_register 失败: %d\n", ret);
		return ret;
	}
	dev_info(&client->dev, "已创建 /dev/%s\n", NTAG_DRV_NAME);

	if (probe_on_load) {
		mutex_lock(&nd->lock);
		ntag_probe_nci(nd);
		mutex_unlock(&nd->lock);
	}
	return 0;
}

static void ntag_remove(struct i2c_client *client)
{
	struct ntag_dev *nd = i2c_get_clientdata(client);

	if (!nd)
		return;
	misc_deregister(&nd->misc);
	ntag = NULL;
}

static const struct acpi_device_id ntag_acpi_ids[] = {
	{ "NTAG0001", 0 },
	{ }
};
MODULE_DEVICE_TABLE(acpi, ntag_acpi_ids);

static const struct i2c_device_id ntag_i2c_ids[] = {
	{ "ntag0001", 0 },
	{ }
};
MODULE_DEVICE_TABLE(i2c, ntag_i2c_ids);

static struct i2c_driver ntag_driver = {
	.driver = {
		.name		= NTAG_DRV_NAME,
		.acpi_match_table = ACPI_PTR(ntag_acpi_ids),
		.dev_groups	= ntag_groups,
	},
	.probe		= ntag_probe,
	.remove		= ntag_remove,
	.id_table	= ntag_i2c_ids,
};
module_i2c_driver(ntag_driver);

MODULE_AUTHOR("nfc-re");
MODULE_DESCRIPTION("Huawei NTAG0001 (ACPI) NFC over I2C - 探针/适配驱动");
MODULE_LICENSE("GPL");
