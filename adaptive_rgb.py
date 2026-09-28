"""
adaptive_rgb.py
---------------
自适应 RGB 颜色模型 (阶段二 Beta)。

每个检测点 (A / B) 维护一个独立的 AdaptiveColorModel 实例：
- 持有历史有效样本 (最多 max_samples 个，FIFO 淘汰)
- 用中位数计算动态参考值 Reference (不用平均值，降低异常颜色影响)
- 用历史波动 (总体标准差 * 2) 计算动态容差，并 clamp 到 [min_tolerance, max_tolerance]
- 新样本与 Reference 差异过大时不加入，防止误触发污染模型
- 冷启动期 (样本数 < min_samples) 不做异常过滤，直接收集，凑够再切自适应

判定：当前 RGB 是否在 Reference ± tolerance 范围内。
A 通过 AND B 通过 → 确认咬钩 → 拉鱼。
"""

from __future__ import annotations

import statistics
from typing import Dict, Optional, Tuple

RGB = Tuple[int, int, int]


class AdaptiveColorModel:
    """单检测点的自适应 RGB 颜色模型"""

    def __init__(
        self,
        max_samples: int = 50,
        min_samples: int = 5,
        min_tolerance: int = 8,
        max_tolerance: int = 40,
        outlier_threshold: float = 2.0,
    ) -> None:
        self._max_samples = max_samples
        self._min_samples = min_samples
        self._min_tolerance = min_tolerance
        self._max_tolerance = max_tolerance
        self._outlier_threshold = outlier_threshold

        self._history: list = []           # list[RGB]
        self._reference: Optional[RGB] = None
        # 各通道容差，初始为 min_tolerance
        self._tolerance: RGB = (min_tolerance, min_tolerance, min_tolerance)

    # ---------------- 状态查询 ----------------

    @property
    def is_ready(self) -> bool:
        """是否完成冷启动 (历史样本 >= min_samples)"""
        return len(self._history) >= self._min_samples

    @property
    def samples(self) -> int:
        return len(self._history)

    @property
    def reference(self) -> Optional[RGB]:
        return self._reference

    @property
    def tolerance(self) -> RGB:
        return self._tolerance

    # ---------------- 核心逻辑 ----------------

    def add_sample(self, rgb: RGB) -> bool:
        """
        尝试加入新样本。
        - 冷启动期 (samples < min_samples): 直接加入，不做异常过滤
        - 已就绪: 与 Reference 各通道差 <= outlier_threshold * tolerance 才接受
        接受后会重算 Reference / Tolerance。

        Returns:
            True 表示样本被接受并加入历史；False 表示被判定为异常丢弃。
        """
        if not _valid_rgb(rgb):
            return False

        if not self.is_ready:
            # 冷启动期：直接接受，快速建立初始 Reference
            self._append_history(rgb)
            self._recompute()
            return True

        # 已就绪：先做异常过滤
        if self._reference is None:
            # 防御性：is_ready 为 True 时 reference 不应为 None
            self._append_history(rgb)
            self._recompute()
            return True

        if self._is_outlier(rgb):
            return False

        self._append_history(rgb)
        self._recompute()
        return True

    def is_in_range(self, rgb: RGB) -> bool:
        """
        判断当前 RGB 是否处于动态范围 [Reference - tolerance, Reference + tolerance]。
        未就绪 (冷启动期) 时返回 False，让上层走基础检测规则。
        """
        if not self.is_ready or self._reference is None:
            return False
        for i in range(3):
            lo = self._reference[i] - self._tolerance[i]
            hi = self._reference[i] + self._tolerance[i]
            if not (lo <= rgb[i] <= hi):
                return False
        return True

    def _is_outlier(self, rgb: RGB) -> bool:
        """是否为异常样本 (任一通道与 Reference 差 > outlier_threshold * tolerance)"""
        for i in range(3):
            diff = abs(rgb[i] - self._reference[i])
            if diff > self._outlier_threshold * self._tolerance[i]:
                return True
        return False

    def _append_history(self, rgb: RGB) -> None:
        """加入历史，超容量则淘汰最旧 (FIFO)"""
        self._history.append(rgb)
        if len(self._history) > self._max_samples:
            del self._history[0:len(self._history) - self._max_samples]

    def _recompute(self) -> None:
        """根据历史样本重算 Reference (中位数) 和 Tolerance (标准差 * 2)"""
        if not self._history:
            return
        rs = [s[0] for s in self._history]
        gs = [s[1] for s in self._history]
        bs = [s[2] for s in self._history]
        self._reference = (
            int(statistics.median(rs)),
            int(statistics.median(gs)),
            int(statistics.median(bs)),
        )
        if len(self._history) >= 2:
            tol_r = int(round(statistics.pstdev(rs) * 2))
            tol_g = int(round(statistics.pstdev(gs) * 2))
            tol_b = int(round(statistics.pstdev(bs) * 2))
        else:
            tol_r = tol_g = tol_b = self._min_tolerance
        self._tolerance = (
            _clamp(tol_r, self._min_tolerance, self._max_tolerance),
            _clamp(tol_g, self._min_tolerance, self._max_tolerance),
            _clamp(tol_b, self._min_tolerance, self._max_tolerance),
        )

    # ---------------- 状态快照 ----------------

    def snapshot(self) -> Dict:
        """返回模型状态快照，用于调试显示"""
        return {
            "reference": self._reference,
            "samples": len(self._history),
            "tolerance": self._tolerance,
            "ready": self.is_ready,
        }

    def reset(self) -> None:
        """清空模型 (重新冷启动)"""
        self._history.clear()
        self._reference = None
        self._tolerance = (self._min_tolerance, self._min_tolerance, self._min_tolerance)


def _valid_rgb(rgb) -> bool:
    return (
        isinstance(rgb, (list, tuple))
        and len(rgb) == 3
        and all(isinstance(v, int) and 0 <= v <= 255 for v in rgb)
    )


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))
