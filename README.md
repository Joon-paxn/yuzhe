# Minecraft 自动钓鱼

一个运行在 Minecraft 游戏外部的 Windows 自动钓鱼工具。程序通过屏幕图像识别检测「咬钩！」提示，并模拟鼠标右键完成收竿和重抛；不修改游戏文件，也不是 Mod。

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/平台-Windows%2010%2F11-0078D4?logo=windows&logoColor=white)

## 功能

- 自动查找 Minecraft 窗口，检测区域按窗口比例保存，窗口移动或缩放后仍可使用。
- 提供白色像素、目标颜色和图像模板三种咬钩检测方式。
- 支持连续帧确认、检测冷却和 F9 检测测试，便于减少误触发。
- 检测到咬钩后右键收竿，随机等待配置的时间后右键重抛。
- 提供 Tkinter 控制面板和全局快捷键；切换到其他窗口时自动暂停，回到 Minecraft 后等待 3 秒再恢复。
- Minecraft 窗口失效时停止自动操作。

## 环境要求

- Windows 10 或 Windows 11
- Python 3.10 或更高版本
- Minecraft Java Edition，建议使用窗口或无边框模式

## 安装与启动

在仓库根目录打开 PowerShell，进入 `AutoFishing` 目录后安装依赖并启动：

```powershell
cd AutoFishing
python -m pip install -r requirements.txt
python main.py
```

请先启动 Minecraft 并进入游戏世界，再运行程序。首次启动时，程序会在控制台提示找到的 Minecraft 窗口，并打开控制面板。

如果全局快捷键注册失败，请以管理员身份启动 PowerShell。`keyboard` 库在部分 Windows 环境中需要提升权限。

## 首次使用

1. 在 Minecraft 中抛竿，等待「咬钩！」提示出现。
2. 按 **F8**，拖动框选提示文字所在区域并确认。
3. 咬钩提示显示时按 **F10**，在放大取色界面中点击文字像素。程序保存目标颜色，并尝试将当前检测区域保存为模板。
4. 按 **F9** 测试检测效果；根据控制台输出确认提示出现时能稳定识别。
5. 手动抛竿让浮标入水，然后按 **F6** 开始自动钓鱼。

F10 截取模板失败时，颜色检测仍会保留并启用。请在咬钩提示再次出现时重试 F10。检测模式按模板匹配、目标颜色、白色像素的顺序选择；未启用或不可用的模式会回退到下一种。

## 快捷键

| 快捷键 | 操作 |
| --- | --- |
| `F6` | 开始自动钓鱼；运行中切换暂停或恢复 |
| `F7` | 立即停止 |
| `F8` | 重新选择咬钩检测区域 |
| `F9` | 测试检测约 8 秒，并在控制台输出识别数据 |
| `F10` | 取色并截取咬钩模板 |
| `Ctrl+C` | 从控制台退出程序 |

也可以在控制面板中使用对应按钮。选区域、测试和截取模板前，请先停止自动钓鱼。

## 检测与配置

所有设置都保存在 `config.json`，程序会在启动时读取。首次使用建议先通过 F8 和 F10 完成检测区域及目标样本设置，再按 F9 测试。常用参数如下：

| 参数 | 作用 |
| --- | --- |
| `window.title_keyword` | Minecraft 窗口标题的匹配关键词；找不到窗口时可按实际标题修改 |
| `detection.use_template_matching` | 是否启用模板匹配；需要存在 `bite_template.png` |
| `detection.template_match_threshold` | 模板匹配阈值，范围为 `0` 到 `1` |
| `detection.use_color_detection` | 是否启用目标颜色检测；F10 取色后会自动启用 |
| `detection.color_tolerance` | 目标颜色各通道的匹配容差 |
| `detection.white_threshold` | 白色像素检测的亮度阈值 |
| `detection.white_pixel_threshold` | 判定咬钩所需的大连通域像素数 |
| `detection.bite_confirm_frames` | 连续满足条件的确认帧数 |
| `fishing.recast_delay_min_ms` / `recast_delay_max_ms` | 收竿后重抛前随机等待时间的上下限 |
| `roi` | 检测区域相对于 Minecraft 窗口的位置和尺寸，使用 F8 设置更方便 |

例如，要调整收竿后的随机等待时间，可修改：

```json
"fishing": {
  "recast_delay_min_ms": 1000,
  "recast_delay_max_ms": 5000
}
```

该片段需要合并到 `config.json` 对应的 `fishing` 对象中；JSON 不支持注释。配置修改后重启程序生效。

## 自动钓鱼流程

```text
等待咬钩 -> 右键收竿 -> 随机等待 -> 右键重抛 -> 等待下一次咬钩
```

等待时间由 `recast_delay_min_ms` 和 `recast_delay_max_ms` 决定。检测和每次鼠标操作前都会检查 Minecraft 窗口；切换到其他窗口时程序会自动暂停。

## 项目结构

```text
AutoFishing/
├── main.py                 # 程序入口与应用协调
├── gui.py                  # Tkinter 控制面板
├── config.py               # 配置加载、保存与校验
├── detector.py             # 屏幕截图与咬钩识别
├── fishing.py              # 自动钓鱼状态机
├── minecraft_window.py     # Minecraft 窗口查找与检测
├── input_controller.py     # 鼠标输入
├── hotkeys.py              # 全局快捷键
├── region_selector.py      # 检测区域框选
├── pixel_picker.py         # 目标颜色取样
├── logger.py               # 日志输出
├── config.json             # 运行配置
├── requirements.txt        # Python 依赖
└── bite_template.png       # F10 生成的检测模板（未生成时不存在）
```

## 常见问题

**快捷键没有响应**

尝试以管理员身份运行 PowerShell 和程序，并检查是否有其他软件占用了快捷键。

**程序找不到 Minecraft 窗口**

确认游戏已经进入世界，而非停留在启动器。必要时修改 `config.json` 中的 `window.title_keyword`，使其与游戏窗口标题相符。

**检测不到咬钩或发生误触发**

重新用 F8 框选提示区域，在提示出现时用 F10 取色并截取模板，再用 F9 测试。也可以调整检测阈值和 `bite_confirm_frames`。

**鼠标操作没有生效**

尝试切换到窗口或无边框模式。独占全屏和部分输入环境可能会影响模拟鼠标操作。

**检测区域位置不准确**

按 F8 重新选择。Windows 显示缩放可能影响坐标识别；如果位置持续偏移，可尝试将显示缩放调整为 100%。

## 免责声明

本项目通过屏幕识别和模拟鼠标输入工作。请遵守 Minecraft、服务器及相关 Mod 的规则；作者不对因使用本工具造成的账号或游戏数据影响负责。
