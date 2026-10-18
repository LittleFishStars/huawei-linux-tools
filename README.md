# 华为分享 Linux 移植（hwsare-linux）

在 Arch Linux（HUAWEI MateBook X Pro）上让华为手机「华为分享」发现这台电脑并互传文件。

> 顺带回答标题里的问题：**不需要逆向移植——华为官方就发布过 Linux 版分享引擎
> （Share Engine / Share Kit）。** 本仓库把它打包成一键构建，唯一的门槛是
> `libsharekit.so` 需要用华为开发者账号（实名）从官网下载。

## 现状

| 组件 | 状态 |
|---|---|
| 调研（官方 SDK 存在性 / 协议 / 门槛） | ✅ 见 [docs/调研报告.md](docs/调研报告.md) |
| BlueZ 5.52 内部静态库自动编译 | ✅ `scripts/build.sh` 已自动化 |
| 官方 demo vendored（Apache-2.0） | ✅ `vendor/huaweicodelabs-ShareEngine/` |
| 一键构建（SDK 缺席时自动 stub） | ✅ `./scripts/build.sh` → `build/hwshare-rx` / `hwshare-dtx` |
| 官方 `libsharekit.so` | ⏳ **需自行下载**（见下） |
| 不等 SDK 的替代互传方案 | ✅ [docs/替代方案.md](docs/替代方案.md) |

## 快速开始

### 1. 构建验证（现在就能跑，stub 模式）

```bash
sudo pacman -S --needed base-devel glib2 openssl
./scripts/build.sh
./build/hwshare-rx   # stub 模式下打印提示后退出，证明构建链 OK
```

### 2. 获取官方 SDK（真正可用的一步）

1. 注册/登录[华为开发者账号](https://developer.huawei.com/consumer/en/)并完成实名认证；
2. 打开 [Obtaining the Linux SDK](https://developer.huawei.com/consumer/en/doc/connectivity-Library/sdk-download-linux-0000001052056169)
   下载 Share Engine Linux SDK；
3. 解压后把 `libsharekit.so` 放进 `sdk/lib/x86_64/`，SDK 自带头文件放进 `sdk/include/`；
4. `./scripts/check-sdk.sh` 校验符号/架构；
5. `./scripts/build.sh` 重新构建。

### 3. 运行（需 root 蓝牙权限）

前置：系统用 wpa_supplicant 管理 Wi-Fi 且开启 P2P（本机当前是 iwd，见调研报告 §3.4）。

```bash
# 一次性：切换 Wi-Fi 后端到 wpa_supplicant（NetworkManager 下）
sudo systemctl disable --now iwd 2>/dev/null
sudo systemctl enable --now wpa_supplicant
sudo systemctl restart NetworkManager

sudo pacman -S --needed bluez-utils
sudo systemctl enable --now bluetooth

sudo ./build/hwshare-rx          # 接收端：手机「华为分享」里应出现本机
```

手机上：设置 → 华为分享 → 打开；选择附近设备 → 本机名称 → 发送文件。

## 仓库结构

```
├── docs/
│   ├── 调研报告.md          # 官方 SDK 存在性、协议细节（0xFE35）、卡点、全网排查
│   └── 替代方案.md          # LocalSend / KDE Connect / SMB，今天就能用
├── scripts/
│   ├── build.sh             # 一键构建（BlueZ 内部库 + demo + stub/真实 SDK 切换）
│   └── check-sdk.sh          # libsharekit.so 架构/符号/依赖校验
├── sdk/
│   ├── include/             # ← 官方 SDK 头文件放这里（自取）
│   └── lib/                 # ← libsharekit.so 放这里（自取，不入库）
├── src/                     # （预留）我们自己的常驻服务包装
├── systemd/                 # （预留）huawei-share-rx.service
└── vendor/
    ├── bluez-5.52/          # 编译产物，不入库
    └── huaweicodelabs-ShareEngine/  # 官方 demo 源码（Apache-2.0）
```

## 关键协议事实（详见调研报告）

- 发现/认证走 **BLE GATT，服务 UUID `0xFE35`**（Huawei Nearby）；
- 文件传输走 **Wi-Fi P2P**（`CHANNEL_P2P`），官方标称 80 Mbps；
- 南向适配层必须链接 **BlueZ 5.52 内部静态库**（`bluetooth-internal` / `shared-mainloop`）——
  直接 `pacman -S bluez` 是编不出来的，`build.sh` 已处理；
- Wi-Fi 侧 SDK 自己实现了一套 wpa_supplicant 调用，与 NetworkManager/iwd 无关；
- NFC（NTAG0001，见姊妹仓库 [huawei-ntag0001-nfc](../huawei-ntag0001-nfc)）只是
  「碰一碰弹欢迎页」的标签，与分享通道无关。

## 许可

- 本仓库脚本与文档：MIT。
- `vendor/huaweicodelabs-ShareEngine/`：Apache-2.0，© 2019 Huawei，保留原始版权头。
- `libsharekit.so`（需自取）：华为专有，仅限按华为开发者协议使用，**不得入库分发**。
