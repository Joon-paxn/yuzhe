"""test_vision.py
阶段四视觉识别测试脚本。

用法：
    python test_vision.py                 # 截取当前 Minecraft 窗口识别
    python test_vision.py <图片路径>      # 识别指定图片
    python test_vision.py --annotate      # 同时输出标注图 vision_annotated.png

输出水域连通域/候选钓点信息，并保存水域掩码 vision_mask.png。
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np

from config import load_config
from minecraft_window import MinecraftWindow
from vision import VisionDetector


def load_frame(argv) -> np.ndarray:
    if len(argv) > 1 and argv[1] not in ("--annotate",):
        path = argv[1]
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            print(f"无法读取图片: {path}")
            sys.exit(1)
        return img
    # 截取 Minecraft 窗口
    mc = MinecraftWindow(load_config("config.json").window)
    rect = mc.get_rect()
    if rect is None:
        print("未找到有效 Minecraft 窗口，请打开游戏或传入图片路径")
        sys.exit(1)
    import mss
    x, y, w, h = rect
    shot = mss.mss().grab({"left": x, "top": y, "width": w, "height": h})
    return np.array(shot)[:, :, :3]


def main() -> None:
    cfg = load_config("config.json")
    annotate = "--annotate" in sys.argv
    frame = load_frame(sys.argv)
    print(f"画面尺寸: {frame.shape[1]}x{frame.shape[0]}")

    det = VisionDetector(cfg.vision)
    result = det.detect(frame)
    print(f"\n=== 视觉识别结果 ===\n{result.summary()}\n")

    print(f"水域连通域 ({len(result.water)}):")
    for i, r in enumerate(result.water):
        print(f"  [{i}] bbox=({r.x},{r.y},{r.width},{r.height}) area={r.area} "
              f"norm=({r.norm_x:.3f},{r.norm_y:.3f},{r.norm_w:.3f},{r.norm_h:.3f}) "
              f"conf={r.confidence:.2f}")

    print(f"\n候选钓点 ({len(result.candidate_fishing_spots)}):")
    for i, c in enumerate(result.candidate_fishing_spots):
        r = c.region
        print(f"  [{i}] 中心=({r.cx:.0f},{r.cy:.0f}) area={r.area} "
              f"conf={c.confidence:.2f} marker={c.has_marker}")

    print(f"\n可通行区域: {len(result.walkable)}  障碍: {len(result.obstacles)}")

    # 保存水域掩码
    _, mask = det._water.detect(frame)
    cv2.imwrite("vision_mask.png", mask)
    print("\n水域掩码已保存: vision_mask.png")

    if annotate:
        vis = frame.copy()
        for r in result.water:
            cv2.rectangle(vis, (r.x, r.y), (r.x + r.width, r.y + r.height), (255, 0, 0), 2)
            cv2.putText(vis, f"W{r.area}", (r.x, max(0, r.y - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)
        for r in result.obstacles:
            cv2.rectangle(vis, (r.x, r.y), (r.x + r.width, r.y + r.height), (0, 0, 255), 1)
        for c in result.candidate_fishing_spots:
            r = c.region
            cv2.circle(vis, (int(r.cx), int(r.cy)), 6, (0, 255, 0), -1)
        cv2.imwrite("vision_annotated.png", vis)
        print("标注图已保存: vision_annotated.png")


if __name__ == "__main__":
    main()
