# -*- coding: utf-8 -*-
"""XY lora图像拼接

把一次 XY 扫描产出的多张图拼成一张带标签的图表，与「XY lora模型加载」输出的
图表信息（XY_GRID_INFO 字典）配套使用。

三个特点：
1. **可以强制换行**：默认按图表信息排成 1 行长条；把「列数」填 >0 时按该列数换行，
   并自动改用「逐格标注」把每张图的标签画在图片下方
   （11 档权重拼成 6×2 比 1×11 好读得多）。
2. **字体逐级回退**：先扫 custom_nodes 下各插件自带的字体目录，其次本插件 fonts/，
   再次系统字体，不会因为某个插件缺失而报错。
3. **单元格边框 / 标题 / 图片缩放 / 自定义颜色**：出图更接近成品图表的观感。

三种标注模式：
- 轴标签(表头)：列标签画在顶部、行标签画在左侧，左上角画 "Y \\ X"。
- 逐格标注：每张图下方画自己的标签，配合「列数」换行使用。
- 不显示：纯拼图。

输入说明（配合 XY lora模型加载 使用时）：
- 「图像」接 VAEDecode 的图像输出（Automatic 会以列表形式传进来，本节点全部收下）；
- 「图表信息」接 XY lora模型加载 的「图表信息」；
- 「标签列表」可选，接 XY lora模型加载 的「标签列表」，逐格标注时优先用它。
"""

import glob
import logging
import math
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch
import folder_paths
from PIL import Image, ImageDraw, ImageFont

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------- 颜色

_COLOR_NAMES: Dict[str, Tuple[int, int, int]] = {
    "白色": (255, 255, 255),
    "黑色": (0, 0, 0),
    "灰色": (128, 128, 128),
    "浅灰": (232, 232, 232),
    "深灰": (60, 60, 60),
    "红色": (214, 64, 64),
    "绿色": (56, 158, 96),
    "蓝色": (58, 104, 200),
    "黄色": (240, 200, 64),
}


def _to_rgb(value: Any, default: Tuple[int, int, int] = (255, 255, 255)) -> Tuple[int, int, int]:
    """把下拉框的中文颜色名或 #RRGGBB 解析成 RGB 三元组。"""
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            return tuple(int(v) for v in value[:3])  # type: ignore[return-value]
        except (TypeError, ValueError):
            return default
    text = str(value or "").strip()
    if not text:
        return default
    if text in _COLOR_NAMES:
        return _COLOR_NAMES[text]
    hex_text = text.lstrip("#")
    if len(hex_text) == 3:
        hex_text = "".join(ch * 2 for ch in hex_text)
    if len(hex_text) == 6:
        try:
            return tuple(int(hex_text[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            return default
    return default


# ---------------------------------------------------------------- 字体

# 下拉框里优先展示的字体（按顺序匹配表里的键）
_PREFERRED_FONTS = (
    r"可商用\中文\nfdcsCN24.ttf",
    r"可商用\中文\nfdcsCN16.ttf",
    r"可商用\中文\字魂扁桃体.ttf",
    r"可商用\中文\平方追风体.ttf",
    r"可商用\中文\爱点风雅黑长体.ttf",
    r"可商用\英文\Facon.ttf",
)

_SYSTEM_FONTS = ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc",
                 "segoeui.ttf", "arial.ttf")


def _base_path() -> str:
    return str(getattr(folder_paths, "base_path", "") or "")


@lru_cache(maxsize=1)
def _font_table() -> Tuple[Tuple[str, str], ...]:
    """扫描可用字体，返回 ((下拉显示名, 绝对路径), ...)。结果缓存，避免每次校验都扫盘。"""
    table: Dict[str, str] = {}

    def scan(root: str, prefix: str = "") -> None:
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if name.lower().endswith((".ttf", ".otf", ".ttc")):
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, root)
                    table.setdefault(prefix + rel, full)

    base = _base_path()
    # custom_nodes 下各插件自带的字体目录（形如 <插件>/…/Font），先扫到的优先
    for pattern in ("*" + os.sep + "Font", "*" + os.sep + "*" + os.sep + "Font"):
        for root_dir in sorted(glob.glob(os.path.join(base, "custom_nodes", pattern))):
            if os.path.isdir(root_dir):
                scan(root_dir)

    local_font = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    if os.path.isdir(local_font):
        scan(local_font, "本地" + os.sep)

    win_font = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    if os.path.isdir(win_font):
        for name in _SYSTEM_FONTS:
            full = os.path.join(win_font, name)
            if os.path.exists(full):
                table.setdefault("系统" + os.sep + name, full)

    ordered: List[Tuple[str, str]] = []
    for key in _PREFERRED_FONTS:
        if key in table:
            ordered.append((key, table.pop(key)))
    ordered.extend(sorted(table.items(), key=lambda kv: kv[0].lower()))
    return tuple(ordered)


def _resolve_font(name: str) -> Optional[str]:
    """把下拉框的名字 / 绝对路径解析成真实字体文件路径。"""
    table = dict(_font_table())
    text = str(name or "").strip().strip('"').strip("'")
    if text and os.path.isabs(text) and os.path.exists(text):
        return text
    if text in table:
        return table[text]
    if text:
        stem = os.path.basename(text).lower()
        for key, path in table.items():
            if os.path.basename(key).lower() == stem:
                return path
        if os.path.exists(text):
            return text
    return next(iter(table.values())) if table else None


@lru_cache(maxsize=64)
def _load_font(path: Optional[str], size: int) -> ImageFont.FreeTypeFont:
    size = max(8, int(size))
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            LOGGER.warning("字体加载失败，退回默认字体：%s", path)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10 没有 size 参数
        return ImageFont.load_default()


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> Tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_w: int) -> List[str]:
    """按像素宽度逐字符换行（中文没有空格，只能按字符切）。"""
    max_w = max(8, int(max_w))
    lines: List[str] = []
    for para in str(text).split("\n"):
        current = ""
        for ch in para:
            if not current or _measure(draw, current + ch, font)[0] <= max_w:
                current += ch
            else:
                lines.append(current)
                current = ch
        lines.append(current)
    return lines or [""]


def _layout(draw: ImageDraw.ImageDraw, text: str, font_path: Optional[str], size: int,
            max_w: int, max_lines: int = 3) -> Dict[str, Any]:
    """给定宽度排版一段文字：先换行，行数超限就缩字号。"""
    size = max(8, int(size))
    font = _load_font(font_path, size)
    lines = _wrap(draw, str(text), font, max_w)
    while len(lines) > max_lines and size > 10:
        size = max(10, int(size * 0.85))
        font = _load_font(font_path, size)
        lines = _wrap(draw, str(text), font, max_w)
    line_h = max(1, _measure(draw, "Hg国", font)[1])
    width = max((_measure(draw, line, font)[0] for line in lines), default=0)
    return {"font": font, "lines": lines, "lh": line_h, "w": width, "h": line_h * len(lines)}


def _draw_lines(draw: ImageDraw.ImageDraw, layout: Dict[str, Any], x: int, y: int,
                fill: Tuple[int, int, int]) -> None:
    for line in layout["lines"]:
        draw.text((x, y), line, font=layout["font"], fill=fill)
        y += layout["lh"]


# ---------------------------------------------------------------- 输入处理

def _scalar(value: Any, default: Any = None) -> Any:
    """INPUT_IS_LIST 下所有输入都是 list，这里取标量。"""
    if isinstance(value, (list, tuple)):
        return value[0] if len(value) > 0 else default
    return default if value is None else value


def _flatten_tags(value: Any) -> List[str]:
    """把 STRING / list[str] / list[list[str]] 统一成扁平的字符串列表。"""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: List[str] = []
    for item in items:
        if isinstance(item, (list, tuple)):
            out.extend(str(v) for v in item if v is not None)
        elif item is not None:
            out.append(str(item))
    return out


def _tensor_to_pil(chw_or_hwc: Any, scale: float) -> Optional[Image.Image]:
    if isinstance(chw_or_hwc, Image.Image):
        image = chw_or_hwc.convert("RGB")
    else:
        array = chw_or_hwc.detach().cpu().numpy() if isinstance(chw_or_hwc, torch.Tensor) else chw_or_hwc
        if not isinstance(array, np.ndarray):
            return None
        if array.ndim != 3:
            return None
        if array.dtype != np.uint8:
            array = np.clip(array * 255.0, 0, 255).astype(np.uint8)
        if array.shape[-1] not in (1, 3, 4):
            return None
        if array.shape[-1] == 1:
            array = np.repeat(array, 3, axis=-1)
        image = Image.fromarray(array[..., :3]).convert("RGB")

    if scale and abs(scale - 1.0) > 1e-3:
        width = max(1, int(round(image.width * scale)))
        height = max(1, int(round(image.height * scale)))
        image = image.resize((width, height), Image.LANCZOS)
    return image


def _flatten_images(value: Any, scale: float = 1.0) -> List[Image.Image]:
    items = value if isinstance(value, (list, tuple)) else [value]
    images: List[Image.Image] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, (torch.Tensor, np.ndarray, Image.Image)):
            array = item
        else:
            continue
        if isinstance(array, (torch.Tensor, np.ndarray)) and getattr(array, "ndim", 0) == 4:
            for i in range(array.shape[0]):
                image = _tensor_to_pil(array[i], scale)
                if image is not None:
                    images.append(image)
        else:
            image = _tensor_to_pil(array, scale)
            if image is not None:
                images.append(image)
    return images


# ---------------------------------------------------------------- 节点

class XYGridDrawer:
    """把 XY 扫描产出的多张图拼成一张带标签的图表。"""

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        fonts = [key for key, _path in _font_table()]
        if not fonts:
            fonts = ["arial.ttf"]
        return {
            "required": {
                "图像": ("IMAGE",),
                "标题": ("STRING", {
                    "default": "",
                    "tooltip": "画在图表正上方的总标题，留空不画",
                }),
                "字体": (fonts, {
                    "tooltip": "优先列出 custom_nodes 下各插件自带的字体，其次本插件 fonts/ 与系统字体",
                }),
                "字体文件路径": ("STRING", {
                    "default": "",
                    "tooltip": "填字体文件的绝对路径可覆盖上面的字体下拉，留空则用下拉里的字体",
                }),
                "字体大小": ("INT", {"default": 48, "min": 8, "max": 400, "step": 2}),
                "网格间距": ("INT", {"default": 20, "min": 0, "max": 400, "step": 2}),
                "列数": ("INT", {
                    "default": 0, "min": 0, "max": 64, "step": 1,
                    "tooltip": "0 = 按图表信息排布（排成一条长条）；填 N 则强制每行 N 列，"
                               "超出换行并自动改为『逐格标注』。图多时建议填 5~8。",
                }),
                "标签样式": (["轴标签(表头)", "逐格标注", "不显示"], {
                    "default": "轴标签(表头)",
                    "tooltip": "轴标签：列标签在顶部、行标签在左侧；"
                               "逐格标注：每张图下方写自己的标签（需配合列数换行）",
                }),
            },
            "optional": {
                "图表信息": ("*", {
                    "tooltip": "接 XY lora模型加载 的「图表信息」（XY_GRID_INFO 字典）",
                }),
                "标签列表": ("STRING", {
                    "forceInput": True,
                    "tooltip": "接 XY lora模型加载 的「标签列表」；逐格标注时优先使用它",
                }),
                "单元格排布": (["横向排列", "竖向排列"], {
                    "default": "横向排列",
                    "tooltip": "一个格子里有多张图时的排列方向",
                }),
                "图片缩放": ("FLOAT", {"default": 1.0, "min": 0.1, "max": 4.0, "step": 0.05}),
                "自定义背景色": ("STRING", {
                    "default": "",
                    "tooltip": "带色卡：点色块或取色器选色，也可手填 #RRGGBB / 中文色名；"
                               "留空 = 白色",
                }),
                "自定义文字色": ("STRING", {
                    "default": "",
                    "tooltip": "带色卡：点色块或取色器选色，也可手填 #RRGGBB / 中文色名；"
                               "留空 = 黑色",
                }),
                "边框颜色": ("STRING", {
                    "default": "#999999",
                    "tooltip": "带色卡：点色块或取色器选色；清空 = 灰色 #999999",
                }),
                # 注意：「单元格边框」必须留在最后，且必须是普通控件（不是 DOM 色卡）。
                # 前端 1.55 里 DOM 控件的覆盖层会盖住节点右下角的缩放手柄，
                # 末位一旦是色卡，节点就彻底不能调整大小了。
                "单元格边框": ("INT", {
                    "default": 0, "min": 0, "max": 20, "step": 1,
                    "tooltip": "0 = 不画边框",
                }),
            },
        }

    # 上游把「图像」当成列表传进来（XY 扫描 = 多次采样），本节点必须一次收下所有图。
    INPUT_IS_LIST = True
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("图表",)
    FUNCTION = "draw"
    CATEGORY = "XY LoRA"
    DESCRIPTION = "把 XY 扫描的多张图拼成带标签的图表，兼容 XY lora模型加载 的图表信息"

    def draw(self, 图像, 标题, 字体, 字体文件路径, 字体大小, 网格间距, 列数, 标签样式,
             图表信息=None, 标签列表=None, 单元格排布=None, 图片缩放=None,
             自定义背景色=None, 自定义文字色=None, 单元格边框=None, 边框颜色=None):
        scale = float(_scalar(图片缩放, 1.0) or 1.0)
        images = _flatten_images(图像, scale)
        if not images:
            LOGGER.warning("XYGridDrawer 没收到任何图像，返回 64x64 占位图")
            return (torch.zeros((1, 64, 64, 3), dtype=torch.float32),)

        font_size = int(_scalar(字体大小, 48))
        margin = max(0, int(_scalar(网格间距, 20)))
        # 背景/文字色只有色卡一处来源：留空就是白底黑字
        bg = _to_rgb(_scalar(自定义背景色, ""), (255, 255, 255))
        fg = _to_rgb(_scalar(自定义文字色, ""), (0, 0, 0))
        border_width = max(0, int(_scalar(单元格边框, 0) or 0))
        border_color = _to_rgb(_scalar(边框颜色, "#999999"), (153, 153, 153))
        direction = str(_scalar(单元格排布, "横向排列"))
        style = str(_scalar(标签样式, "轴标签(表头)"))
        font_override = str(_scalar(字体文件路径, "") or "").strip()
        font_path = _resolve_font(font_override or str(_scalar(字体, "")))

        info = _scalar(图表信息, None)
        if not isinstance(info, dict):
            info = {}
        x_labels = [str(v) for v in (info.get("x_labels") or [])]
        y_labels = [str(v) for v in (info.get("y_labels") or [])]
        x_title = str(info.get("x_title") or "")
        y_title = str(info.get("y_title") or "")
        cell_counts_info = info.get("cell_image_counts")
        tags = _flatten_tags(标签列表)

        total = len(images)
        forced_cols = int(_scalar(列数, 0) or 0)
        cols = forced_cols if forced_cols > 0 else int(info.get("count_x") or 0)
        if cols <= 0:
            cols = int(math.ceil(math.sqrt(total)))
        cols = max(1, min(cols, total))
        rows = int(math.ceil(total / cols))

        axis_mode = (style == "轴标签(表头)"
                     and len(x_labels) == cols
                     and len(y_labels) in (0, rows))
        if style == "轴标签(表头)" and not axis_mode:
            LOGGER.info("图表信息（%d 列 / %d 行标签）与实际排布（%d×%d）不一致，改用逐格标注",
                        len(x_labels), len(y_labels), cols, rows)

        # ---- 每格装几张图 ----
        counts: Optional[List[int]] = None
        if isinstance(cell_counts_info, (list, tuple)) and len(cell_counts_info) == cols * rows:
            try:
                candidate = [max(0, int(v)) for v in cell_counts_info]
            except (TypeError, ValueError):
                candidate = None
            if candidate is not None and sum(candidate) == total:
                counts = candidate
        if counts is None:
            base, extra = divmod(total, cols * rows)
            counts = [base + (1 if i < extra else 0) for i in range(cols * rows)]

        cells: List[List[Image.Image]] = []
        cursor = 0
        for count in counts:
            cells.append(images[cursor:cursor + count])
            cursor += count
        if cursor < total:  # 图表信息与实际数量不符时，多出来的图塞进最后一格
            cells[-1].extend(images[cursor:])

        ref_w, ref_h = images[0].width, images[0].height
        blocks: List[Tuple[int, int]] = []
        for cell in cells:
            if not cell:
                blocks.append((ref_w, ref_h))
            elif direction == "竖向排列":
                blocks.append((
                    max(im.width for im in cell),
                    sum(im.height for im in cell) + margin * (len(cell) - 1),
                ))
            else:
                blocks.append((
                    sum(im.width for im in cell) + margin * (len(cell) - 1),
                    max(im.height for im in cell),
                ))

        col_w = [0] * cols
        row_h = [0] * rows
        for index, (block_w, block_h) in enumerate(blocks):
            row, col = divmod(index, cols)
            col_w[col] = max(col_w[col], block_w)
            row_h[row] = max(row_h[row], block_h)
        col_w = [w or ref_w for w in col_w]
        row_h = [h or ref_h for h in row_h]

        probe = ImageDraw.Draw(Image.new("RGB", (1, 1), bg))

        # ---- 顶部列标签（轴模式）----
        x_layouts: List[Dict[str, Any]] = []
        header_h = 0
        if axis_mode and x_labels:
            for col in range(cols):
                text = x_labels[col] if col < len(x_labels) else ""
                layout = _layout(probe, text, font_path, font_size,
                                 max(font_size, col_w[col] - margin * 2))
                x_layouts.append(layout)
                header_h = max(header_h, layout["h"])
            header_h += margin * 2

        has_y_labels = any(text.strip() for text in y_labels)
        corner_layout = None        # 画在左上角（有 Y 轴时）
        axis_title_layout = None    # 画在整条表头上方（没有 Y 轴时）
        if axis_mode and (x_title or y_title):
            corner_text = ("%s \\ %s" % (y_title, x_title)).strip(" \\")
            if corner_text:
                layout = _layout(probe, corner_text, font_path, font_size, 10 ** 6)
                if has_y_labels:
                    corner_layout = layout
                    header_h = max(header_h, layout["h"] + margin * 2)
                else:
                    # 只有一行、没有 Y 轴时，把 "权重 | LoRA名" 放到列标签上方当整条表头的标题，
                    # 否则左侧会为了容纳这段长文字留出一大片空白
                    axis_title_layout = layout
        axis_title_h = axis_title_layout["h"] + margin if axis_title_layout else 0

        # ---- 左侧行标签（轴模式）----
        y_layouts: List[Dict[str, Any]] = []
        sidebar_w = 0
        if axis_mode and has_y_labels:
            limit = max(font_size * 2, int(font_size * 10))
            for row in range(rows):
                text = y_labels[row] if row < len(y_labels) else ""
                layout = _layout(probe, text, font_path, font_size, limit)
                y_layouts.append(layout)
                sidebar_w = max(sidebar_w, layout["w"])
            sidebar_w += margin * 2
        if corner_layout is not None:
            sidebar_w = max(sidebar_w, corner_layout["w"] + margin * 2)

        # ---- 总标题 ----
        title_text = str(_scalar(标题, "") or "").strip()
        grid_w = sum(col_w) + margin * max(0, cols - 1)
        title_layout = None
        if title_text:
            title_layout = _layout(probe, title_text, font_path, int(font_size * 1.25),
                                  max(font_size, grid_w), max_lines=2)
            title_h = title_layout["h"] + margin
        else:
            title_h = 0

        # ---- 每格标签（逐格模式）----
        flat_labels: List[str] = tags
        if not flat_labels:
            if x_labels and len(y_labels) > 1 and len(x_labels) * len(y_labels) == total:
                flat_labels = ["%s / %s" % (xl, yl) for yl in y_labels for xl in x_labels]
            elif x_labels:
                flat_labels = list(x_labels)

        caption_h = 0
        caption_layouts: List[Dict[str, Any]] = []
        if not axis_mode and style != "不显示" and any(text.strip() for text in flat_labels):
            for index in range(cols * rows):
                text = flat_labels[index] if index < len(flat_labels) else ""
                layout = _layout(probe, text, font_path, font_size,
                                 max(font_size, col_w[index % cols] - margin))
                caption_layouts.append(layout)
                caption_h = max(caption_h, layout["h"] + margin)
            row_h = [h + caption_h for h in row_h]

        # ---- 画布 ----
        x0 = margin + sidebar_w
        y0 = margin + title_h + axis_title_h + header_h
        grid_h = sum(row_h) + margin * max(0, rows - 1)
        canvas_w = max(16, x0 + grid_w + margin)
        canvas_h = max(16, y0 + grid_h + margin)
        canvas = Image.new("RGB", (canvas_w, canvas_h), bg)
        draw = ImageDraw.Draw(canvas)

        if title_layout is not None:
            _draw_lines(draw, title_layout, (canvas_w - title_layout["w"]) // 2, margin, fg)

        if axis_title_layout is not None:
            # 没有 Y 轴时的整条表头标题，左对齐到网格起点
            _draw_lines(draw, axis_title_layout, x0, margin + title_h, fg)

        if corner_layout is not None:
            cx = margin + max(0, sidebar_w - corner_layout["w"]) // 2
            cy = margin + title_h + max(0, header_h - corner_layout["h"]) // 2
            _draw_lines(draw, corner_layout, cx, cy, fg)

        for col, layout in enumerate(x_layouts):
            col_x = x0 + sum(col_w[:col]) + margin * col
            lx = col_x + max(0, col_w[col] - layout["w"]) // 2
            ly = margin + title_h + axis_title_h + max(0, header_h - layout["h"]) // 2
            _draw_lines(draw, layout, lx, ly, fg)

        for row, layout in enumerate(y_layouts):
            row_y = y0 + sum(row_h[:row]) + margin * row
            lx = margin + max(0, sidebar_w - layout["w"]) // 2
            ly = row_y + max(0, row_h[row] - caption_h - layout["h"]) // 2
            _draw_lines(draw, layout, lx, ly, fg)

        for index in range(cols * rows):
            row, col = divmod(index, cols)
            cell_x = x0 + sum(col_w[:col]) + margin * col
            cell_y = y0 + sum(row_h[:row]) + margin * row
            image_area_h = row_h[row] - caption_h
            block_w, block_h = blocks[index]
            base_x = cell_x + max(0, col_w[col] - block_w) // 2
            base_y = cell_y + max(0, image_area_h - block_h) // 2

            offset_x = offset_y = 0
            for image in cells[index]:
                if direction == "竖向排列":
                    canvas.paste(image, (base_x + max(0, block_w - image.width) // 2, base_y + offset_y))
                    offset_y += image.height + margin
                else:
                    canvas.paste(image, (base_x + offset_x, base_y + max(0, block_h - image.height) // 2))
                    offset_x += image.width + margin

            if border_width > 0 and cells[index]:
                draw.rectangle(
                    [base_x - border_width, base_y - border_width,
                     base_x + block_w + border_width - 1, base_y + block_h + border_width - 1],
                    outline=border_color, width=border_width)

            if caption_h and index < len(caption_layouts):
                layout = caption_layouts[index]
                cx = cell_x + max(0, col_w[col] - layout["w"]) // 2
                cy = cell_y + image_area_h + max(0, caption_h - layout["h"]) // 2
                _draw_lines(draw, layout, cx, cy, fg)

        array = np.asarray(canvas.convert("RGB"), dtype=np.float32) / 255.0
        return (torch.from_numpy(array).unsqueeze(0),)


NODE_CLASS_MAPPINGS = {
    "comfyui_XYGridDrawer": XYGridDrawer,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "comfyui_XYGridDrawer": "XY lora图像拼接",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
