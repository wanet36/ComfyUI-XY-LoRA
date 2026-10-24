"""comfyui_XYLoRA —— XY 权重扫描 / 图表拼接 / 图上标注

包含：
  xy_lora.py   comfyui_XYLoRA / comfyui_ImageBadge / comfyui_SaveWithLabels   （XY LoRA）
  xy_grid.py   comfyui_XYGridDrawer                                          （XY LoRA）
  web/         前端扩展（LoRA 名称自动补全、颜色色卡）

batch_text（批量文本）已拆到独立插件 custom_nodes/comfyui_batch_text。

命名约定：菜单显示名与 class_type 都用 comfyui_ 前缀，不再保留任何旧别名。
"""

from . import xy_grid, xy_lora

NODE_CLASS_MAPPINGS = {
    **xy_lora.NODE_CLASS_MAPPINGS,
    **xy_grid.NODE_CLASS_MAPPINGS,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    **xy_lora.NODE_DISPLAY_NAME_MAPPINGS,
    **xy_grid.NODE_DISPLAY_NAME_MAPPINGS,
}

# 前端扩展目录
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
