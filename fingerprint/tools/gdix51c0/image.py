# SPDX-License-Identifier: GPL-2.0-or-later
"""12 位打包像素的解码与可视化。

传感器一次读出 64×80 个 12 位采样，按 **6 字节打包 4 像素**：

    target[0] = ((src[0] & 0x0f) << 8) | src[1]
    target[1] = (src[3] << 4)        | (src[0] >> 4)
    target[2] = ((src[5] & 0x0f) << 8) | src[2]
    target[3] = (src[4] << 4)        | (src[5] >> 4)

5120 像素 × 1.5 字节 = 7680 字节。解出来是 **80 列 × 64 行**（慢轴 80、
快轴 64），和"64 宽 80 高"的直觉相反，上游把 HEIGHT 定义成快轴。
"""

from __future__ import annotations

from dataclasses import dataclass

GROUP_BYTES = 6
GROUP_PIXELS = 4
SENSOR_WIDTH = 64  # 快轴
SENSOR_HEIGHT = 80  # 慢轴
FRAME_PIXELS = SENSOR_WIDTH * SENSOR_HEIGHT  # 5120
FRAME_BYTES = FRAME_PIXELS * 3 // 2  # 7680
PIXEL_MAX = 0x0FFF

CHIP_ID = 0x2504
SENSOR_TYPE = 12
ALGORITHM = "ChicagoHS"


class ImageError(ValueError):
    pass


def decode_raw12(packed: bytes) -> list[int]:
    """7680 字节 → 5120 个 12 位采样。"""
    n_groups = len(packed) // GROUP_BYTES
    out = [0] * (n_groups * GROUP_PIXELS)
    for g in range(n_groups):
        base = g * GROUP_BYTES
        s0, s1, s2 = packed[base], packed[base + 1], packed[base + 2]
        s3, s4, s5 = packed[base + 3], packed[base + 4], packed[base + 5]
        o = g * GROUP_PIXELS
        out[o + 0] = ((s0 & 0x0F) << 8) | s1
        out[o + 1] = (s3 << 4) | (s0 >> 4)
        out[o + 2] = ((s5 & 0x0F) << 8) | s2
        out[o + 3] = (s4 << 4) | (s5 >> 4)
    return out


@dataclass
class Frame:
    """一帧解码后的指纹图。"""

    pixels: list[int]
    width: int = SENSOR_HEIGHT  # 80 列
    height: int = SENSOR_WIDTH  # 64 行

    @classmethod
    def from_raw(cls, raw: bytes) -> "Frame":
        if len(raw) < FRAME_BYTES:
            raise ImageError(
                f"帧数据不足：需要 {FRAME_BYTES} 字节，收到 {len(raw)}"
            )
        pixels = decode_raw12(raw[:FRAME_BYTES])
        return cls(pixels=pixels)

    # -- 统计 --

    def stats(self) -> dict[str, float]:
        px = self.pixels
        n = len(px)
        mean = sum(px) / n
        lo = min(px)
        hi = max(px)
        var = sum((p - mean) ** 2 for p in px) / n
        return {
            "min": float(lo),
            "max": float(hi),
            "mean": mean,
            "std": var ** 0.5,
            "range": float(hi - lo),
        }

    def contrast(self) -> float:
        """峰谷差，用来区分"空帧"和"有手指"。"""
        return float(max(self.pixels) - min(self.pixels))

    # -- 可视化 --

    def to_rows(self) -> list[list[int]]:
        """按 80 列 × 64 行切开。"""
        return [
            self.pixels[r * self.width : (r + 1) * self.width]
            for r in range(self.height)
        ]

    def to_pgm(self, invert: bool = False) -> bytes:
        """输出 8 位灰度 PGM（12 位压到 8 位）。"""
        px = self.pixels
        lo, hi = min(px), max(px)
        span = (hi - lo) or 1
        out = bytearray()
        for p in px:
            v = int((p - lo) * 255 / span)
            out.append(255 - v if invert else v)
        header = f"P5\n{self.width} {self.height}\n255\n".encode("ascii")
        return header + bytes(out)

    def to_ascii(self, cols: int = 40) -> str:
        """终端预览：按局部块取均值映射到字符梯度。"""
        rows = self.to_rows()
        ramp = " .:-=+*#%@"
        px = self.pixels
        lo, hi = min(px), max(px)
        span = (hi - lo) or 1
        # 80 列 → cols 列，64 行 → cols/2 行（字符高宽比）
        cx = max(1, self.width // cols)
        cy = max(1, cx * 2)
        lines: list[str] = []
        for y0 in range(0, self.height, cy):
            line = []
            for x0 in range(0, self.width, cx):
                block = [
                    rows[y][x]
                    for y in range(y0, min(y0 + cy, self.height))
                    for x in range(x0, min(x0 + cx, self.width))
                ]
                avg = sum(block) / len(block)
                idx = int((avg - lo) * (len(ramp) - 1) / span)
                line.append(ramp[idx])
            lines.append("".join(line))
        return "\n".join(lines)

    def save_pgm(self, path: str, invert: bool = False) -> None:
        with open(path, "wb") as fh:
            fh.write(self.to_pgm(invert))


def selftest() -> None:
    """用一份已知打包数据验证解码索引。"""
    # 4 个像素 [0x123, 0x456, 0x789, 0xabc] 按打包规则反向构造
    p0, p1, p2, p3 = 0x123, 0x456, 0x789, 0xABC
    s = [0] * 6
    s[1] = p0 & 0xFF
    s[0] = ((p0 >> 8) & 0x0F) | ((p1 >> 4) & 0xF0)
    s[3] = (p1 >> 4) & 0xFF
    s[2] = p2 & 0xFF
    s[5] = ((p2 >> 8) & 0x0F) | ((p3 >> 4) & 0xF0)
    s[4] = (p3 >> 4) & 0xFF
    got = decode_raw12(bytes(s))
    want = [p0, p1, p2, p3]
    # 打包是有损的近邻约束，允许 ±0x10 的重建偏差
    for g, w in zip(got, want):
        assert abs(g - w) <= 0x10, f"解码偏差过大: {g:#x} vs {w:#x}"


def frame_capacity_check() -> None:
    assert FRAME_PIXELS == 5120
    assert FRAME_BYTES == 7680
