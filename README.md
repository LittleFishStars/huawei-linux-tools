# huawei-linux-tools

华为笔记本（HUAWEI MateBook X Pro 2023 / MRGF-XX 等）在 Linux 下补齐原厂功能的工具集，由三个独立子项目合并而来，各自保留完整提交历史。

| 子目录 | 原仓库 | 内容 | 状态 |
|---|---|---|---|
| [`ntag-nfc/`](ntag-nfc) | huawei-ntag0001-nfc | 内置 NFC（复旦微 FM11NT082C-NTCI，i2c-0 @0x57）的逆向结论与 Linux 读写工具（`tools/ntag_read.py`） | 可读，写入受芯片 NC 模式限制 |
| [`fingerprint/`](fingerprint) | huawei-gdix51c0-fp | Goodix GDIX51C0 指纹：DKMS 内核 SPI 模块 + 用户态 TLS-PSK 协议栈 | 协议栈可跑通，待整机验证 |
| [`share/`](share) | huawei-share-linux | 华为分享（Share Engine）Linux 移植：官方 SDK 打包、一键构建、BlueZ 内部库方案 | SDK 需向华为申请，缺席时自动 stub |

## 使用

各子目录自带 README，进入对应目录按说明构建：

```bash
cd ntag-nfc      # 读 NFC 标签
sudo python3 tools/ntag_read.py --dump

cd fingerprint   # 构建 DKMS 模块
sudo ./scripts/install.sh

cd share         # 构建华为分享（SDK 缺席时构建 stub）
./scripts/build.sh
```

## 合并说明

三个子目录由 `git subtree` 合并，历史与各自原仓库一致：

```bash
git log --oneline ntag-nfc/       # 只看 NFC 的历史
git log --oneline fingerprint/
git log --oneline share/
```

原仓库 `huawei-ntag0001-nfc`、`huawei-gdix51c0-fp`、`huawei-share-linux` 已归档删除，后续开发在本仓库进行。
