"""
test_ocr.py
-----------
OCR 基础测试脚本 (阶段一)。

用法：
    python test_ocr.py            # 连续识别 ~10 秒，输出识别结果与游戏状态
    python test_ocr.py --once     # 仅识别一次
    python test_ocr.py --save shot.png   # 同时保存 ROI 截图

不启动自动钓鱼，不影响现有钓鱼逻辑。仅验证 OCR 引擎 / ROI / 咬钩识别可用。
若未安装 OCR 库，会提示安装命令并以非零码退出。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import cv2

from config import load_config
from minecraft_window import MinecraftWindow
from ocr import OcrService
from logger import get_logger


CONFIG_PATH = "config.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR 基础测试 (阶段一)")
    parser.add_argument("--once", action="store_true", help="仅识别一次后退出")
    parser.add_argument("--save", type=str, default="", help="保存 ROI 截图到指定路径")
    parser.add_argument("--duration", type=float, default=10.0, help="连续识别时长 (秒)")
    args = parser.parse_args()

    log = get_logger()
    cfg = load_config(CONFIG_PATH)

    log.info("===== OCR 基础测试 (阶段一) =====")
    mc = MinecraftWindow(cfg.window.title_keyword)
    if not mc.find():
        log.error("未找到 Minecraft 窗口，请先启动并进入游戏世界")
        return 1

    # 临时启用 OCR 以便测试 (不修改 config.json)
    cfg.ocr.enabled = True
    service = OcrService(mc, cfg.ocr)

    if not service.engine_available:
        log.error(
            f"OCR 引擎不可用: {service.engine_name}\n"
            "请安装 OCR 库: pip install rapidocr onnxruntime "
            "(或 pip install rapidocr-onnxruntime)"
        )
        service.close()
        return 2

    log.success(f"OCR 引擎可用: {service.engine_name}")
    roi_screen = service.get_roi_screen()
    if roi_screen is not None:
        x, y, w, h = roi_screen
        log.info(f"OCR ROI 屏幕坐标: ({x},{y}) {w}x{h}")
    else:
        log.warn("无法计算 OCR ROI 屏幕坐标")

    # 单次识别 (含截图保存)
    log.info("--- 单次识别 ---")
    frame = service.capture_roi_once()
    if frame is None:
        log.error("截图失败，请确认 Minecraft 窗口在前台且未被遮挡")
        service.close()
        return 3

    if args.save:
        try:
            cv2.imwrite(args.save, frame)
            log.success(f"ROI 截图已保存: {args.save}")
        except Exception as e:
            log.error(f"保存截图失败: {e}")

    result = service.recognize_once(frame)
    _print_result(log, result)

    if args.once:
        service.close()
        log.info("===== 测试结束 (单次模式) =====")
        return 0

    # 连续识别 (同步循环，不走后台线程，便于观察)
    log.info(f"--- 连续识别 {args.duration:.0f} 秒 (Ctrl+C 提前结束) ---")
    start = time.monotonic()
    last_print = 0.0
    try:
        while time.monotonic() - start < args.duration:
            result = service.recognize_once()
            now = time.monotonic()
            if result.available and now - last_print >= 0.5:
                last_print = now
                _print_result(log, result, compact=True)
            elif not result.available and result.error and now - last_print >= 1.0:
                last_print = now
                log.warn(f"识别未成功: {result.error}")
            time.sleep(max(0.1, cfg.ocr.interval_ms / 1000.0))
    except KeyboardInterrupt:
        log.info("用户中断")

    service.close()
    log.info("===== 测试结束 =====")
    return 0


def _print_result(log, result, compact: bool = False) -> None:
    """打印识别结果与解析出的游戏状态"""
    if not result.available:
        log.warn(f"识别未成功: {result.error or '未知原因'}")
        return
    from ocr import OcrParser
    state = OcrParser().parse(result)
    if compact:
        log.test(
            f"[{result.elapsed_ms:.0f}ms] 文本={result.full_text!r} | "
            f"咬钩={'是' if state.bite_detected else '否'} "
            f"枯竭={'是' if state.depleted else '否'} "
            f"XYZ={state.xyz if state.xyz else '无'}"
        )
        return
    log.success(f"识别成功 ({result.elapsed_ms:.0f}ms)，共 {len(result.lines)} 行:")
    for ln in result.lines:
        log.info(f"  - {ln.text!r}  conf={ln.confidence:.2f}  bbox={ln.bbox}")
    log.info(f"拼接文本: {result.full_text!r}")
    log.info(
        f"游戏状态: 咬钩={'是' if state.bite_detected else '否'} | "
        f"钓点枯竭={'是' if state.depleted else '否'} | "
        f"XYZ={state.xyz if state.xyz else '未识别'}"
    )


if __name__ == "__main__":
    sys.exit(main())
