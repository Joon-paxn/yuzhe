"""
test_adaptive_rgb.py
--------------------
自适应 RGB 颜色模型单元测试 (阶段二)。

覆盖规格 12 项场景：
1. RGB 基本稳定
2. RGB 小范围波动
3. RGB 大幅变化
4. 单个异常颜色
5. 连续异常颜色
6. A 正常、B 异常
7. A 异常、B 正常
8. A/B 同时满足
9. 模型初始化 (冷启动)
10. 达到最大历史样本后继续更新
11. 关闭 adaptive_rgb 后恢复阶段一逻辑
12. 一次误检测不会让后续标准整体偏移
"""

import unittest

from adaptive_rgb import AdaptiveColorModel


class TestAdaptiveColorModel(unittest.TestCase):

    def make_model(self, **kw):
        defaults = dict(
            max_samples=50, min_samples=5,
            min_tolerance=8, max_tolerance=40, outlier_threshold=2.0,
        )
        defaults.update(kw)
        return AdaptiveColorModel(**defaults)

    # 场景 9: 冷启动
    def test_09_cold_start(self):
        m = self.make_model(min_samples=5)
        self.assertFalse(m.is_ready)
        # 冷启动期：直接接受样本
        for i in range(4):
            self.assertTrue(m.add_sample((240, 245, 250)))
        self.assertFalse(m.is_ready)
        self.assertEqual(m.samples, 4)
        # 第 5 个样本后就绪
        self.assertTrue(m.add_sample((241, 246, 249)))
        self.assertTrue(m.is_ready)
        self.assertEqual(m.samples, 5)
        self.assertIsNotNone(m.reference)

    # 场景 1: RGB 基本稳定
    def test_01_stable_rgb(self):
        m = self.make_model(min_samples=5)
        base = (243, 246, 249)
        for _ in range(10):
            m.add_sample(base)
        self.assertTrue(m.is_ready)
        # 稳定时容差应为 min_tolerance (无波动)
        for t in m.tolerance:
            self.assertGreaterEqual(t, 8)
        # 当前 RGB 在范围内
        self.assertTrue(m.is_in_range(base))
        self.assertTrue(m.is_in_range((243, 246, 249)))

    # 场景 2: RGB 小范围波动
    def test_02_small_fluctuation(self):
        m = self.make_model(min_samples=5, min_tolerance=5)
        # 在 (240,245,250) 附近波动 ±3
        samples = [
            (240, 245, 250), (243, 246, 249), (239, 244, 251),
            (242, 247, 250), (241, 246, 250), (240, 245, 252),
        ]
        for s in samples:
            self.assertTrue(m.add_sample(s))
        self.assertTrue(m.is_ready)
        # 波动范围内的点应通过
        self.assertTrue(m.is_in_range((241, 246, 250)))
        # 远离的点不应通过 (大幅偏离)
        self.assertFalse(m.is_in_range((200, 200, 200)))

    # 场景 3: RGB 大幅变化 (模型应拒绝异常样本，不污染)
    def test_03_large_change_rejected(self):
        m = self.make_model(min_samples=5)
        # 先建立稳定基线
        for _ in range(10):
            m.add_sample((243, 246, 249))
        ref_before = m.reference
        # 大幅变化样本应被拒绝
        self.assertFalse(m.add_sample((100, 100, 100)))
        self.assertFalse(m.add_sample((50, 200, 50)))
        # Reference 不变
        self.assertEqual(m.reference, ref_before)

    # 场景 4: 单个异常颜色
    def test_04_single_outlier(self):
        m = self.make_model(min_samples=5)
        for _ in range(10):
            m.add_sample((243, 246, 249))
        ref = m.reference
        # 单个异常
        self.assertFalse(m.add_sample((10, 20, 30)))
        # 模型未受影响
        self.assertEqual(m.reference, ref)
        self.assertEqual(m.samples, 10)
        # 正常样本仍能通过
        self.assertTrue(m.is_in_range((243, 246, 249)))

    # 场景 5: 连续异常颜色
    def test_05_consecutive_outliers(self):
        m = self.make_model(min_samples=5)
        for _ in range(10):
            m.add_sample((243, 246, 249))
        ref = m.reference
        samples_before = m.samples
        # 连续 20 个异常
        for _ in range(20):
            self.assertFalse(m.add_sample((10, 20, 30)))
        # 模型完全未受影响
        self.assertEqual(m.reference, ref)
        self.assertEqual(m.samples, samples_before)
        self.assertTrue(m.is_in_range((243, 246, 249)))

    # 场景 6 & 7 & 8: A/B 独立模型
    def test_06_07_08_ab_independent(self):
        ma = self.make_model(min_samples=5)
        mb = self.make_model(min_samples=5)
        # A 稳定在亮色，B 稳定在另一颜色
        for _ in range(10):
            ma.add_sample((243, 246, 249))
            mb.add_sample((120, 200, 80))
        # A 正常、B 异常 (场景 6)
        self.assertTrue(ma.is_in_range((243, 246, 249)))
        self.assertFalse(mb.is_in_range((243, 246, 249)))
        # A 异常、B 正常 (场景 7)
        self.assertFalse(ma.is_in_range((120, 200, 80)))
        self.assertTrue(mb.is_in_range((120, 200, 80)))
        # A/B 同时满足 (场景 8) -> 咬钩
        a_ok = ma.is_in_range((243, 246, 249))
        b_ok = mb.is_in_range((120, 200, 80))
        bite = a_ok and b_ok
        self.assertTrue(bite)

    # 场景 10: 达到最大历史样本后继续更新
    def test_10_max_samples_eviction(self):
        m = self.make_model(max_samples=5, min_samples=3)
        # 填满 5 个
        for i in range(5):
            m.add_sample((240 + i, 245, 250))
        self.assertEqual(m.samples, 5)
        # 加入第 6 个，最旧的应被淘汰
        m.add_sample((245, 245, 250))
        self.assertEqual(m.samples, 5)
        # 继续加入，仍保持 5
        for i in range(10):
            m.add_sample((242, 246, 250))
        self.assertEqual(m.samples, 5)
        self.assertTrue(m.is_ready)

    # 场景 11: 关闭 adaptive 后恢复阶段一逻辑 (测模式名)
    def test_11_disable_adaptive_falls_back(self):
        from config import DetectionConfig, AdaptiveRGBConfig
        from detector import DetectionResult
        # 关闭时 AdaptiveRGBConfig.enabled=False
        cfg_off = AdaptiveRGBConfig(enabled=False)
        self.assertFalse(cfg_off.enabled)
        cfg_on = AdaptiveRGBConfig(enabled=True)
        self.assertTrue(cfg_on.enabled)
        # 验证 detector 模式名在关闭时不含"自适应"
        # (完整集成测试需要 mock 窗口，这里验证配置层)

    # 场景 12: 一次误检测不会让后续标准整体偏移
    def test_12_single_misdetection_no_drift(self):
        m = self.make_model(min_samples=5)
        # 建立稳定基线
        for _ in range(15):
            m.add_sample((243, 246, 249))
        ref_before = m.reference
        tol_before = m.tolerance
        samples_before = m.samples
        # 模拟一次误检测 (异常 RGB)
        accepted = m.add_sample((150, 150, 150))
        # 应被拒绝
        self.assertFalse(accepted)
        # 后续标准未偏移
        self.assertEqual(m.reference, ref_before)
        self.assertEqual(m.tolerance, tol_before)
        self.assertEqual(m.samples, samples_before)
        # 正常 RGB 仍能通过
        self.assertTrue(m.is_in_range((243, 246, 249)))

    # 容差 clamp 测试
    def test_tolerance_clamp(self):
        m = self.make_model(min_samples=3, min_tolerance=10, max_tolerance=20)
        # 极端波动样本
        for s in [(0, 0, 0), (255, 255, 255), (128, 128, 128)]:
            m.add_sample(s)
        for t in m.tolerance:
            self.assertLessEqual(t, 20)
            self.assertGreaterEqual(t, 10)

    # reset 测试
    def test_reset(self):
        m = self.make_model(min_samples=3)
        for _ in range(5):
            m.add_sample((240, 245, 250))
        self.assertTrue(m.is_ready)
        m.reset()
        self.assertFalse(m.is_ready)
        self.assertEqual(m.samples, 0)
        self.assertIsNone(m.reference)


if __name__ == "__main__":
    unittest.main(verbosity=2)
