"""
ocr/parser.py
-------------
OCR 结果解析器。

将 OcrResult 解析为结构化游戏状态，与具体业务逻辑解耦。
阶段一实现「咬钩」识别；「钓点枯竭」/ XYZ 坐标识别接口已预留，
具体接入留待后续阶段 (阶段三 / 阶段十)。

识别采用关键词包含匹配 + 标点/全半角归一化，降低 OCR 误识别影响。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .models import OcrResult


# 关键词集合 (支持 OCR 可能的误识别变体)
_BITE_KEYWORDS: Tuple[str, ...] = ("咬钩",)
# 「枯竭」是非常专一的词，单独匹配即可覆盖「鱼群：枯竭」「钓点枯竭」等所有变体，
# 避免因 mod 文案差异 (鱼群：枯竭 / 钓点枯竭) 漏检。
_DEPLETED_KEYWORDS: Tuple[str, ...] = ("枯竭",)

# XYZ 坐标正则：匹配 "XYZ: 123.4 / 64.0 / -37.8" 或 "123.4 / 64.0 / -37.8"
_XYZ_PATTERN = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*[/／,，]\s*(-?\d+(?:\.\d+)?)\s*[/／,，]\s*(-?\d+(?:\.\d+)?)"
)


def _normalize(text: str) -> str:
    """全角→半角，去空白，便于关键词匹配"""
    if not text:
        return ""
    out = []
    for ch in text:
        code = ord(ch)
        # 全角空格
        if code == 0x3000:
            out.append(" ")
        # 全角字符 (！～) 转半角
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


@dataclass
class GameState:
    """从 OCR 结果解析出的游戏状态"""

    bite_detected: bool = False                  # 检测到「咬钩」
    depleted: bool = False                       # 检测到「钓点枯竭」
    xyz: Optional[Tuple[float, float, float]] = None  # 玩家坐标 (x, y, z)
    matched_lines: List[str] = field(default_factory=list)  # 命中关键词的原文行


class OcrParser:
    """OCR 结果解析器"""

    def parse(self, result: OcrResult) -> GameState:
        """解析单次 OCR 结果为游戏状态"""
        state = GameState()
        if not result.available:
            return state

        for line in result.lines:
            raw = line.text
            norm = _normalize(raw)
            # 咬钩
            if not state.bite_detected and any(k in norm for k in _BITE_KEYWORDS):
                state.bite_detected = True
                state.matched_lines.append(raw)
            # 钓点枯竭
            if not state.depleted and any(k in norm for k in _DEPLETED_KEYWORDS):
                state.depleted = True
                state.matched_lines.append(raw)
            # XYZ 坐标
            if state.xyz is None:
                xyz = self._parse_xyz(norm)
                if xyz is not None:
                    state.xyz = xyz

        return state

    def parse_bite(self, result: OcrResult) -> bool:
        """仅判断是否咬钩"""
        return self.parse(result).bite_detected

    @staticmethod
    def _parse_xyz(text: str) -> Optional[Tuple[float, float, float]]:
        """从文本中提取 XYZ 坐标"""
        m = _XYZ_PATTERN.search(text)
        if not m:
            return None
        try:
            return (float(m.group(1)), float(m.group(2)), float(m.group(3)))
        except ValueError:
            return None
