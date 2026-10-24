"""comfyui_batch_text —— 批量读取文本 → 提示词 / 条件

1) BatchTextPrompts  → comfyui_BatchTextPrompts  批量读 txt，输出一段纯文本（只看/复制用）
2) BatchTextToCond   → comfyui_BatchTextToCond   批量读 txt，逐条编码成条件（一条文本一张图）
"""

import io, os, re, glob

import folder_paths


# ======================================================================
# 1) 纯文本读取（不参与生图）
# ======================================================================
class BatchTextPrompts:
    """批量读取文本文件，输出为一段带编号的纯文本。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "文本文件夹": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "存放 txt 的文件夹。留空则用 ComfyUI 的 input 目录",
                }),
                "文件匹配": ("STRING", {"default": "*.txt", "multiline": False}),
                "递归子目录": ("BOOLEAN", {"default": False}),
                "切分方式": (["按空行分段", "按行", "整文件一条"], {"default": "按空行分段"}),
                "排序": (["文件名升序", "文件名降序", "修改时间升序", "修改时间降序"],
                         {"default": "文件名升序"}),
                "编码": (["utf-8", "utf-8-sig", "gbk"], {"default": "utf-8-sig"}),
                "最大条数": ("INT", {"default": 32, "min": 1, "max": 512, "step": 1}),
                "标签内容": (["源文件名", "提示词原文", "序号"], {
                    "default": "源文件名",
                    "tooltip": "文本里每条前面的标签用哪种形式；源文件名最简洁",
                }),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("文本预览",)
    OUTPUT_IS_LIST = (False,)
    FUNCTION = "build"
    CATEGORY = "Batch Text"

    # ---------- 文件与文本 ----------
    @staticmethod
    def _split(text, mode):
        t = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not t:
            return []
        if mode == "整文件一条":
            return [re.sub(r"\s+", " ", t).strip()]
        if mode == "按行":
            return [ln.strip() for ln in t.split("\n") if ln.strip()]
        parts = re.split(r"\n\s*\n", t)
        return [re.sub(r"\s+", " ", p).strip() for p in parts if p.strip()]

    def list_files(self, folder, pattern, recursive, order):
        """返回排序后的文件列表。"""
        folder = (folder or "").strip().strip('"').strip("'")
        if not folder:
            folder = os.path.join(folder_paths.get_input_directory())
        if not os.path.isdir(folder):
            raise ValueError("文本文件夹不存在：%s" % folder)

        files = glob.glob(os.path.join(folder, "**", pattern), recursive=recursive) \
            if recursive else glob.glob(os.path.join(folder, pattern))
        files = [f for f in files if os.path.isfile(f)]

        if order.endswith("降序"):
            files.sort(reverse=True)
        elif order.startswith("修改时间"):
            files.sort(key=os.path.getmtime, reverse=order.endswith("降序"))
        else:
            files.sort()
        return files

    def collect(self, folder, pattern, recursive, order, encoding, mode, limit,
                start_file=1):
        """返回 [(prompt, 源文件名干, 文件内第几条), ...]。

        start_file 从 1 开始：跳过它之前的文件。
        """
        files = self.list_files(folder, pattern, recursive, order)
        start = max(1, int(start_file))
        files = files[start - 1:]

        items = []
        for path in files:
            try:
                with io.open(path, "r", encoding=encoding, errors="ignore") as f:
                    raw = f.read()
            except Exception:
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            for k, p in enumerate(self._split(raw, mode)):
                if len(items) >= limit:
                    break
                items.append((p, stem, k))
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def make_labels(items, 标签内容):
        labels = []
        for idx, (p, stem, k) in enumerate(items, 1):
            if 标签内容 == "提示词原文":
                labels.append(p)
            elif 标签内容 == "序号":
                labels.append("%03d" % idx)
            else:
                labels.append(stem if k == 0 else "%s_%02d" % (stem, k + 1))
        return labels

    def build(self, 文本文件夹, 文件匹配, 递归子目录, 切分方式, 排序, 编码,
              最大条数, 标签内容="源文件名"):
        limit = int(最大条数)
        items = self.collect(文本文件夹, 文件匹配, 递归子目录, 排序, 编码,
                             切分方式, limit)[:limit]
        if not items:
            raise ValueError(
                "没有读到任何提示词。请检查：文本文件夹路径、文件匹配（当前 %r）、"
                "以及文件内容是否为空。" % 文件匹配)

        labels = self.make_labels(items, 标签内容)
        lines = ["共 %d 条提示词" % len(items), "=" * 60]
        for i, (label, (p, _stem, _k)) in enumerate(zip(labels, items), 1):
            lines.append("[%02d] %s" % (i, label))
            lines.append("    " + p)
            lines.append("")
        lines.append("=" * 60)
        return ("\n".join(lines),)


# ======================================================================
# 2) 批量文本 → 条件（生图）
# ======================================================================
class BatchTextToCond:
    """批量读取 txt → 逐条编码成条件（一条 prompt 一张图）。

    条件输出为**列表**（OUTPUT_IS_LIST），KSampler 才会逐条执行一次。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "clip": ("CLIP",),
                "文本文件夹": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "存放 txt 的文件夹。留空则用 ComfyUI 的 input 目录",
                }),
                "文件匹配": ("STRING", {"default": "*.txt", "multiline": False}),
                "递归子目录": ("BOOLEAN", {"default": False}),
                "切分方式": (["按空行分段", "按行", "整文件一条"], {"default": "整文件一条"}),
                "排序": (["文件名升序", "文件名降序", "修改时间升序", "修改时间降序"],
                         {"default": "文件名升序"}),
                "编码": (["utf-8", "utf-8-sig", "gbk"], {"default": "utf-8-sig"}),
                "起始文件序号": ("INT", {
                    "default": 1, "min": 1, "max": 100000, "step": 1,
                    "tooltip": "从第几个文件开始读取（1 = 第一个）。用于断点续跑",
                }),
                "最大条数": ("INT", {"default": 32, "min": 1, "max": 512, "step": 1}),
                "标签内容": (["源文件名", "提示词原文", "序号"], {
                    "default": "源文件名",
                    "tooltip": "标签用于图片命名与图上文字",
                }),
            },
            # 两个提示词框放在最下方（optional 在界面上排在 required 之后）
            "optional": {
                "额外正面提示词": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "拼接到每条正向提示词末尾（例如质量词、画风词）；留空则不追加",
                }),
                "负面提示词": ("STRING", {
                    "default": "worst quality, low quality, blurry, jpeg artifacts, watermark, signature, text",
                    "multiline": True,
                }),
            },
        }

    RETURN_TYPES = ("CONDITIONING", "CONDITIONING", "STRING", "STRING")
    RETURN_NAMES = ("正向条件", "负向条件", "标签列表", "文本预览")
    # 前三个是列表（逐条执行）；最后一个是整段文本（非列表，直接接 ShowText）
    OUTPUT_IS_LIST = (True, True, True, False)
    FUNCTION = "build"
    CATEGORY = "Batch Text"

    def build(self, clip, 文本文件夹, 文件匹配, 递归子目录, 切分方式, 排序, 编码,
              起始文件序号, 最大条数, 标签内容="源文件名",
              额外正面提示词="", 负面提示词=""):
        helper = BatchTextPrompts()
        limit = int(最大条数)
        items = helper.collect(文本文件夹, 文件匹配, 递归子目录, 排序, 编码,
                               切分方式, limit, start_file=起始文件序号)[:limit]
        if not items:
            raise ValueError(
                "没有读到任何提示词。请检查：文本文件夹路径、文件匹配（当前 %r）、"
                "起始文件序号（当前 %d）、以及文件内容是否为空。"
                % (文件匹配, int(起始文件序号)))

        labels = helper.make_labels(items, 标签内容)

        extra = (额外正面提示词 or "").strip().strip(",").strip()
        prompts = []
        for p, _stem, _k in items:
            prompts.append(("%s, %s" % (p, extra)) if extra else p)

        positives = [clip.encode_from_tokens_scheduled(clip.tokenize(p)) for p in prompts]
        neg_single = clip.encode_from_tokens_scheduled(clip.tokenize(负面提示词 or ""))
        negatives = [neg_single for _ in prompts]

        # 文本输出：把最终送入编码的提示词（已含额外正面词）整段列出
        lines = ["共 %d 条提示词" % len(prompts), "=" * 60]
        for i, (label, p) in enumerate(zip(labels, prompts), 1):
            lines.append("[%02d] %s" % (i, label))
            lines.append("    " + p)
            lines.append("")
        lines.append("=" * 60)
        if (负面提示词 or "").strip():
            lines.append("负面提示：" + 负面提示词.strip())
        preview = "\n".join(lines)

        return (positives, negatives, labels, preview)


NODE_CLASS_MAPPINGS = {
    "comfyui_BatchTextPrompts": BatchTextPrompts,
    "comfyui_BatchTextToCond": BatchTextToCond,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "comfyui_BatchTextPrompts": "批量文本读取（仅输出文本）",
    "comfyui_BatchTextToCond": "批量文本生条件（一条文本一张图）",
}
