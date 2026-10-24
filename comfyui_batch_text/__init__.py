"""comfyui_batch_text —— 批量读取文本文件，转成提示词 / 条件

comfyui_BatchTextPrompts   批量读 txt，输出一段纯文本（只看/复制用）
comfyui_BatchTextToCond    批量文本生成（批量读 txt，逐条编码成条件）

与 comfyui_XYLoRA 是两个独立插件。
"""

from .batch_text import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
