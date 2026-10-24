"""
XY lora模型加载 —— 按名字选 LoRA 的权重扫描
--------------------------------------------
用名字指定某一个 LoRA，从「权重起始值」到「权重结束值」均分「权重档数」档，
输出模型 / CLIP 列表 + 图表信息，可直接对接本包的「XY lora图像拼接」绘制带标签的图表。

设计要点：
- 输入直接是 MODEL（不是 checkpoint），所以适配 Anima / Krea2 这类
  UNETLoader + CLIPLoader 的模型。
- 扫描轴是**权重**：从"权重起始值"到"权重结束值"均分"权重档数"档。
- 具体画成几行几列由拼接节点依据图表信息自动决定。

LoRA 名称支持三种写法：
    1. 相对路径（推荐，与 ComfyUI 的 loras 目录一致）
       例：anima\\美学\\anima-base-1-masterpiece-v51.safetensors
    2. 仅文件名（会在 loras 目录里递归搜索唯一匹配）
       例：anima-base-1-masterpiece-v51.safetensors
    3. 绝对路径
"""

import glob
import json
import math
import os
import re


import comfy.sd
import comfy.utils
import folder_paths
from PIL import Image, ImageDraw, ImageFont


def _font_roots():
    """custom_nodes 下各插件自带的字体目录（形如 <插件>/…/Font），按路径排序。"""
    base = os.path.join(folder_paths.base_path, "custom_nodes")
    roots = []
    for pattern in ("*" + os.sep + "Font", "*" + os.sep + "*" + os.sep + "Font"):
        roots += [d for d in glob.glob(os.path.join(base, pattern)) if os.path.isdir(d)]
    return sorted(set(roots))


def _find_font(name: str):
    """在 ComfyUI 各插件的字体目录里找一个可用字体文件。"""
    for root in _font_roots():
        for cand in (os.path.join(root, name),
                     os.path.join(root, "可商用", "中文", name)):
            if os.path.exists(cand):
                return cand
    # 兜底：扫一遍插件目录里任何名字带 Font 的目录
    base = os.path.join(folder_paths.base_path, "custom_nodes")
    for root, _dirs, files in os.walk(base):
        if "Font" in os.path.basename(root) and name in files:
            return os.path.join(root, name)
    return None


def _load_font(name: str, size: int):
    path = _find_font(name)
    for fp in (path, "arial.ttf"):
        if fp:
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()


_FONT_CHOICES = [
    r"可商用\中文\nfdcsCN24.ttf",
    r"可商用\中文\nfdcsCN16.ttf",
    r"可商用\中文\字魂扁桃体.ttf",
    r"可商用\中文\平方追风体.ttf",
    r"可商用\中文\爱点风雅黑长体.ttf",
    r"可商用\英文\Facon.ttf",
]


def _list_all_loras():
    """列出 loras 目录下所有可用 LoRA 的相对路径。"""
    try:
        return list(folder_paths.get_filename_list("loras"))
    except Exception:
        return []


def _resolve_one_lora(name: str):
    """把用户提供的名字解析成 loras 目录下的相对路径；失败时抛出带候选的提示。"""
    name = (name or "").strip().strip('"').strip("'")
    if not name:
        raise ValueError("LoRA 名称不能为空。请填写例如：anima\\美学\\xxx.safetensors")

    # 绝对路径
    if os.path.isabs(name) and os.path.exists(name):
        return name

    # 规范化分隔符
    norm = name.replace("/", "\\")

    # 补全扩展名
    candidates_ext = [norm]
    if not os.path.splitext(norm)[1]:
        candidates_ext += [norm + ".safetensors", norm + ".pt", norm + ".ckpt"]

    all_loras = _list_all_loras()

    # 1) 完全匹配
    for cand in candidates_ext:
        for rel in all_loras:
            if rel.replace("/", "\\").lower() == cand.lower():
                return rel

    # 2) 目录内唯一匹配（只给了文件名的情况）
    if os.sep not in norm and "/" not in norm:
        stem = os.path.splitext(os.path.basename(norm))[0].lower()
        hits = [rel for rel in all_loras
                if os.path.splitext(os.path.basename(rel))[0].lower() == stem]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ValueError(
                "LoRA 名 '%s' 在 loras 目录里匹配到多个文件，请写相对路径：%s"
                % (name, " | ".join(hits[:8]))
            )

    # 3) 兜底：交给 ComfyUI 自己解析
    for cand in candidates_ext:
        try:
            p = folder_paths.get_full_path("loras", cand)
            if p and os.path.exists(p):
                return cand
        except Exception:
            pass

    sample = " | ".join(all_loras[:10])
    raise ValueError(
        "找不到 LoRA '%s'。请检查名称，或改填 loras 目录下的相对路径。当前可用的前几个：%s"
        % (name, sample)
    )


class XYLoRA:
    """按名字指定 LoRA，扫描其权重并输出模型列表 + 图表信息。"""

    @classmethod
    def INPUT_TYPES(cls):
        # 用 COMBO（下拉框）而不是自由文本：ComfyUI 会原位给出搜索补全，
        # 体验与内置 LoraLoader 一致。清单即 folder_paths.get_filename_list("loras")。
        try:
            loras = ["None"] + list(folder_paths.get_filename_list("loras"))
        except Exception:
            loras = ["None"]
        return {
            "required": {
                "模型": ("MODEL",),
                "LoRA名称": (loras, {
                    "tooltip": "下拉里搜索选择；也可直接键入文件名（会递归匹配唯一项）",
                }),
                "权重起始值": ("FLOAT", {"default": 0.2, "min": -10.0, "max": 10.0, "step": 0.05}),
                "权重结束值": ("FLOAT", {"default": 1.0, "min": -10.0, "max": 10.0, "step": 0.05}),
                "权重档数": ("INT", {"default": 5, "min": 1, "max": 50, "step": 1}),
            },
            "optional": {
                "CLIP": ("CLIP",),
                "LoRA完整路径或别名": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "非空时优先生效：可填绝对路径，或列表之外的写法",
                }),
                "无LoRA对比": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "额外跑一张完全不加 LoRA 的图作为基线（会放在第一档位置）",
                }),
            },
        }

    RETURN_TYPES = ("MODEL", "CLIP", "XY_GRID_INFO", "STRING")
    RETURN_NAMES = ("模型", "CLIP", "图表信息", "标签列表")
    OUTPUT_IS_LIST = (True, True, False, True)
    FUNCTION = "load_by_name"
    CATEGORY = "XY LoRA"

    def load_by_name(self, 模型, LoRA名称, 权重起始值, 权重结束值, 权重档数,
                     CLIP=None, LoRA完整路径或别名="", 无LoRA对比=False,
                     标签格式="{name} ({weight})"):
        if CLIP is None:
            raise ValueError(
                "请把 CLIPLoader 的输出接到本节点的 CLIP 输入。"
                "LoRA 需要同时作用于模型和文本编码器，缺少 CLIP 会导致提示词编码失败。"
            )

        # 覆盖字段优先（便于填绝对路径），否则用下拉选择的值
        chosen = (LoRA完整路径或别名 or "").strip() or (LoRA名称 or "").strip()
        if not chosen or chosen == "None":
            raise ValueError("请选择或填写一个 LoRA（当前为空或 None）。")

        rel = _resolve_one_lora(chosen)
        full = folder_paths.get_full_path("loras", rel)
        if not full or not os.path.exists(full):
            raise ValueError("解析到 LoRA 路径但文件不存在：%s" % rel)

        # 权重档位
        n = int(权重档数)
        if n <= 1:
            weights = [round(float(权重起始值), 3)]
        else:
            step = (float(权重结束值) - float(权重起始值)) / (n - 1)
            weights = [round(float(权重起始值) + i * step, 3) for i in range(n)]

        label = os.path.splitext(os.path.basename(full))[0]

        out_models, out_clips = [], []
        lora_data = None

        # 每个格子对应的标签（供逐张标注用）
        tags = (["None"] if 无LoRA对比 else [])
        tags += [标签格式.format(name=label, weight=w, lora=label) for w in weights]

        # 可选：无 LoRA 基线
        if 无LoRA对比:
            out_models.append(模型)
            out_clips.append(CLIP)

        for w in weights:
            if lora_data is None:
                lora_data = comfy.utils.load_torch_file(full)
            m, c = comfy.sd.load_lora_for_models(模型, CLIP, lora_data, w, w)
            out_models.append(m)
            out_clips.append(c)

        # 布局：单行多列（每列一个权重值），横向排列
        # Drawer 约定：cols = count_x、rows = count_y，按行优先填充；
        # cell_image_counts 长度必须等于 cols*rows，否则 Drawer 会重置为全 1。
        axis_labels = (["None"] if 无LoRA对比 else []) + [str(w) for w in weights]
        grid_info = {
            "x_labels": axis_labels,          # 列标签 = 各权重值
            "y_labels": [""],                 # 仅一行
            "x_title": "权重 | " + label,    # 把 LoRA 名放在标题里，避免每格重复
            "y_title": "",
            "count_x": len(out_models),       # 列数 = 实际图片数
            "count_y": 1,                     # 只排一行
            "cell_image_counts": [1] * len(out_models),
            "total_images": len(out_models),
        }
        return (out_models, out_clips, grid_info, tags)




class ImageBadge:
    """逐张在图像上标注文字（LoRA 名称 / 权重等）。

    与 XYLoRA 的「标签列表」输出配对使用时，每张图会拿到自己的标签。
    标签数量不足时，超出的图沿用最后一个标签；只给一个标签则等于统一水印。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图像": ("IMAGE",),
                "标签": ("STRING", {"forceInput": True}),
                "字体大小": ("INT", {"default": 36, "min": 8, "max": 300, "step": 2}),
                "位置": (["左下", "左上", "右下", "右上", "居中"], {"default": "左下"}),
                "文字颜色": ("STRING", {
                    "default": "#FFFFFF",
                    "tooltip": "带色卡：点色块弹出取色面板（系统取色器 + 12 色），"
                               "也可手填 #RRGGBB；清空 = 白色",
                }),
                "描边颜色": ("STRING", {
                    "default": "#000000",
                    "tooltip": "带色卡：点色块弹出取色面板（系统取色器 + 12 色），"
                               "也可手填 #RRGGBB；清空 = 黑色（描边宽度为 0 时不画）",
                }),
                "描边宽度": ("INT", {"default": 2, "min": 0, "max": 20, "step": 1}),
                "边距": ("INT", {"default": 18, "min": 0, "max": 400, "step": 2}),
                "底色不透明度": ("FLOAT", {"default": 0.35, "min": 0.0, "max": 1.0, "step": 0.05}),
                "自动缩放": ("BOOLEAN", {"default": True}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("图像",)
    FUNCTION = "draw"
    CATEGORY = "XY LoRA"

    # ---------- 工具 ----------
    @staticmethod
    def _rgb(c, default=(255, 255, 255)):
        c = str(c or "").strip().lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        try:
            return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
        except Exception:
            return default

    @staticmethod
    def _font_file():
        cands = (os.path.join("可商用", "中文", "nfdcsCN24.ttf"),
                 os.path.join("可商用", "中文", "nfdcsCN16.ttf"),
                 "arial.ttf")
        for root in _font_roots():
            for rel in cands:
                p = os.path.join(root, rel)
                if os.path.exists(p):
                    return p
        # 兜底：插件目录里任何 ttf/otf
        base = os.path.join(folder_paths.base_path, "custom_nodes")
        for root, _d, files in os.walk(base):
            for f in files:
                if f.lower().endswith((".ttf", ".otf")):
                    return os.path.join(root, f)
        return None

    @staticmethod
    def _font(path, size):
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
        try:
            return ImageFont.load_default(size)
        except Exception:
            return ImageFont.load_default()

    @classmethod
    def _measure(cls, draw, text, font, sw):
        try:
            box = draw.textbbox((0, 0), text, font=font, stroke_width=int(sw))
        except TypeError:
            box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0], box[3] - box[1]

    # ---------- 主函数 ----------
    def draw(self, 图像, 标签, 字体大小, 位置, 文字颜色, 描边颜色, 描边宽度, 边距,
             底色不透明度, 自动缩放):
        if isinstance(标签, str):
            标签 = [标签]
        标签 = [str(x) for x in (标签 or []) if x is not None]

        font_path = self._font_file()
        fill = self._rgb(文字颜色, (255, 255, 255))
        stroke = self._rgb(描边颜色, (0, 0, 0))

        n = int(图像.shape[0])
        out = []
        for i in range(n):
            arr = 图像[i].cpu().numpy()
            img = Image.fromarray((arr * 255.0).clip(0, 255).astype("uint8")).convert("RGB")
            text = 标签[i] if i < len(标签) else (标签[-1] if 标签 else "")
            if text:
                img = self._stamp(img, text, font_path, int(字体大小), 位置,
                                  int(边距), fill, stroke, int(描边宽度),
                                  float(底色不透明度), bool(自动缩放))
            out.append(img)

        import numpy as np
        import torch
        arr = np.stack([np.asarray(im.convert("RGB"), dtype="float32") / 255.0 for im in out],
                       axis=0)
        return (torch.from_numpy(arr),)

    def _stamp(self, img, text, font_path, size, 位置, margin, fill, stroke, sw, bg_alpha,
                auto_fit):
        W, H = img.size
        draw = ImageDraw.Draw(img)

        # 先找能放下的字号（这一步是之前那版失效的地方，这里保证生效）
        limit = max(32, W - 2 * margin)
        font = self._font(font_path, size)
        tw, th = self._measure(draw, text, font, sw)
        if auto_fit and tw > limit:
            while size > 8 and tw > limit:
                size = max(8, int(size * 0.9))
                font = self._font(font_path, size)
                tw, th = self._measure(draw, text, font, sw)

        # 定位
        if "左" in 位置:
            x = margin
        elif "右" in 位置:
            x = max(margin, W - tw - margin)
        else:
            x = max(margin, (W - tw) // 2)
        if "上" in 位置:
            y = margin
        elif "下" in 位置:
            y = max(margin, H - th - margin * 2)
        else:
            y = max(margin, (H - th) // 2)

        # 半透明底条，提升可读性
        if bg_alpha > 0:
            pad = max(4, int(th * 0.28))
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(layer).rectangle(
                [x - pad, y - pad, x + tw + pad, y + th + pad],
                fill=(0, 0, 0, int(255 * bg_alpha)))
            img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
            draw = ImageDraw.Draw(img)

        try:
            draw.text((x, y), text, font=font, fill=fill,
                      stroke_width=sw, stroke_fill=stroke)
        except TypeError:
            draw.text((x, y), text, font=font, fill=fill)
        return img



class SaveWithLabels:
    """按标签命名保存 —— 文件名里直接带 LoRA 名与权重。

    SaveImage 每批只能用一个 filename_prefix，无法逐张命名；本节点把每张图的
    标签（如 "anima-base-1-masterpiece-v51 (0.5)"）清洗后作为文件名主体：

        anima-base-1-masterpiece-v51_0.5_00001_.png
        anima-base-1-masterpiece-v51_0.75_00002_.png

    标签少于图像时超出的图沿用最后一个标签；只给一个标签相当于统一前缀。
    """

    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        self.compress_level = 4

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图像": ("IMAGE",),
                "标签": ("STRING", {"forceInput": True}),
                "文件名模板": ("STRING", {
                    "default": "{label}",
                    "tooltip": "占位符：{label}=标签原文，{index}=序号。例：{label} 或 {index}_{label}",
                }),
                "存放子目录": ("STRING", {"default": "anima_xy/labeled"}),
                "扩展名": (["png", "jpg", "webp"], {"default": "png"}),
                "加序号后缀": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "开：ComfyUI_00001_00001_.png（与自带 SaveImage 一致）；"
                               "关：ComfyUI_00001.png（更干净，重名时自动加 _2、_3）",
                }),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ()
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "XY LoRA"

    @staticmethod
    def _safe(text):
        """把标签变成安全、可读的文件名主体。"""
        t = str(text or "").strip()
        t = t.replace(chr(92), "/")            # 反斜杠 -> 正斜杠
        t = t.split("/")[-1]                  # 只取最后一段
        t = re.sub(r"\.safetensors$", "", t, flags=re.I)
        t = re.sub("[<>:" + chr(34) + r"|?*#\r\n\t\\]", "_", t)
        t = re.sub(r"\s+", " ", t).strip(" ._")
        return t[:120] or "image"

    def save(self, 图像, 标签, 文件名模板, 存放子目录, 扩展名, 加序号后缀=False,
             prompt=None, extra_pnginfo=None):
        from PIL import Image

        if isinstance(标签, str):
            标签 = [标签]
        标签 = [str(x) for x in (标签 or [])]

        prefix = (存放子目录 or "").strip().strip("/" + chr(92))
        ext = 扩展名.lower()
        results = []

        for i in range(int(图像.shape[0])):
            label = 标签[i] if i < len(标签) else (标签[-1] if 标签 else "")
            try:
                stem = 文件名模板.format(label=self._safe(label), index=i + 1)
            except Exception:
                stem = self._safe(label)
            stem = self._safe(stem) or "image"

            full_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
                (prefix + "/" + stem) if prefix else stem,
                self.output_dir, int(图像.shape[2]), int(图像.shape[1]))

            arr = 图像[i].cpu().numpy()
            img = Image.fromarray((arr * 255.0).clip(0, 255).astype("uint8"))

            meta = None
            if prompt is not None or extra_pnginfo is not None:
                meta = {}
                if prompt:
                    meta.update(prompt)
                if extra_pnginfo:
                    meta.update(extra_pnginfo)

            if 加序号后缀:
                file_name = "%s_%05d_.%s" % (filename, counter, ext)
            else:
                # 不加计数器：直接用「标签.ext」；若已存在则追加 _2、_3…
                file_name = "%s.%s" % (filename, ext)
                if os.path.exists(os.path.join(full_folder, file_name)):
                    n = 2
                    while os.path.exists(os.path.join(full_folder,
                                                      "%s_%d.%s" % (filename, n, ext))):
                        n += 1
                    file_name = "%s_%d.%s" % (filename, n, ext)
            out_path = os.path.join(full_folder, file_name)

            if ext in ("jpg", "jpeg"):
                img.convert("RGB").save(out_path, quality=95)
            elif ext == "webp":
                img.convert("RGB").save(out_path, quality=95)
            else:
                if meta:
                    from PIL import PngImagePlugin
                    info = PngImagePlugin.PngInfo()
                    for k, v in meta.items():
                        try:
                            info.add_text(str(k), v if isinstance(v, str)
                                          else json.dumps(v, ensure_ascii=False))
                        except Exception:
                            pass
                    img.save(out_path, pnginfo=info, compress_level=self.compress_level)
                else:
                    img.save(out_path, compress_level=self.compress_level)

            results.append({"filename": file_name, "subfolder": subfolder, "type": self.type})

        return {"ui": {"images": results}}


NODE_CLASS_MAPPINGS = {
    "comfyui_XYLoRA": XYLoRA,
    "comfyui_ImageBadge": ImageBadge,
    "comfyui_SaveWithLabels": SaveWithLabels,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "comfyui_XYLoRA": "XY lora模型加载",
    "comfyui_ImageBadge": "XY lora图像标注",
    "comfyui_SaveWithLabels": "XY lora图像保存",
}

# 前端扩展目录：里面的 JS 会被 ComfyUI 自动加载，用于给「LoRA名称」输入框
# 挂上自动补全（候选来自内置 LoraLoader 的 lora_name 列表）。
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
