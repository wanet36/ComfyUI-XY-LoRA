/**
 * XY LoRA —— LoRA 名称输入框自动补全
 * ------------------------------------------------
 * 目标：在节点的「LoRA名称」文本框里打字时，自动匹配 loras 目录里的文件名，
 *       效果与内置「LoraLoader」节点选择 LoRA 时一致。
 *
 * 实现思路：
 *   1. 监听节点定义注册，锁定 type = "comfyui_XYLoRA" 的节点。
 *   2. LoRA 文件清单直接复用内置 LoraLoader 的 `lora_name` 选项
 *      （即 folder_paths.get_filename_list("loras") 的结果），
 *      必要时回退到 /object_info/LoraLoader 拉取，保证清单与官方 loader 完全一致。
 *   3. 给「LoRA名称」这个 STRING widget 的 DOM input 挂一个 <datalist>，
 *      浏览器原生就会做"输入即过滤 + 下拉候选"，同时保留自由输入（可手填相对路径）。
 *   4. 每次输入时按关键词刷新候选；另外额外支持"只输文件名"的简写匹配。
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_TYPES = new Set(["comfyui_XYLoRA"]);
// 主选择框「LoRA名称」现在是 COMBO（下拉），ComfyUI 原生就带搜索补全；
// 这里给可选的自由输入框「LoRA完整路径或别名」也挂一份补全。
const WIDGET_NAME = "LoRA完整路径或别名";

let loraListCache = null;

/** 取 LoRA 清单：优先用内置 LoraLoader 的选项，其次走 API。 */
async function getLoraList() {
    if (loraListCache && loraListCache.length) return loraListCache;

    // 途径 1：当前工作流里已存在的 LoraLoader 节点，其 widget.options.values 就是完整清单
    try {
        const nodes = app.graph?._nodes || [];
        for (const n of nodes) {
            const w = (n.widgets || []).find((x) => x.name === "lora_name");
            if (w?.options?.values?.length) {
                loraListCache = [...w.options.values].filter((v) => v && v !== "None");
                return loraListCache;
            }
        }
    } catch (e) { /* 忽略，走途径 2 */ }

    // 途径 2：查后端节点定义
    try {
        const resp = await api.fetchApi("/object_info/LoraLoader");
        const info = await resp.json();
        const vals = info?.LoraLoader?.input?.required?.lora_name?.[0];
        if (Array.isArray(vals) && vals.length) {
            loraListCache = vals.filter((v) => v && v !== "None");
            return loraListCache;
        }
    } catch (e) { /* 忽略 */ }

    loraListCache = [];
    return loraListCache;
}

function normalize(s) {
    return String(s || "").replace(/\//g, "\\").toLowerCase();
}

/** 按关键词过滤候选：既匹配完整相对路径，也匹配"仅文件名"。 */
function filterLoras(list, keyword) {
    const kw = normalize(keyword).trim();
    if (!kw) return list.slice(0, 300);
    const out = [];
    for (const item of list) {
        const full = normalize(item);
        const base = full.split("\\").pop();
        if (full.includes(kw) || base.includes(kw)) out.push(item);
        if (out.length >= 300) break;
    }
    return out;
}

function attachAutocomplete(node) {
    const widget = (node.widgets || []).find((w) => w.name === WIDGET_NAME);
    if (!widget) return false;

    const inputEl = widget.inputEl || widget.element;
    if (!inputEl || inputEl.dataset.animaLoraBound === "1") return !!inputEl;

    const listId = `anima-lora-list-${node.id ?? Math.random().toString(36).slice(2)}`;
    let datalist = document.getElementById(listId);
    if (!datalist) {
        datalist = document.createElement("datalist");
        datalist.id = listId;
        document.body.appendChild(datalist);
    }
    inputEl.setAttribute("list", listId);
    inputEl.dataset.animaLoraBound = "1";

    const refresh = async () => {
        const list = await getLoraList();
        const matched = filterLoras(list, inputEl.value);
        datalist.innerHTML = "";
        for (const item of matched) {
            const opt = document.createElement("option");
            opt.value = item;
            datalist.appendChild(opt);
        }
    };

    inputEl.addEventListener("input", refresh);
    inputEl.addEventListener("focus", refresh);
    // 首次进入就准备好候选，不必先打字
    refresh();
    return true;
}

app.registerExtension({
    name: "Anima.XY.LoRA.Autocomplete",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!NODE_TYPES.has(nodeData?.name)) return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function (...args) {
            const r = onCreated?.apply(this, args);
            // 节点创建瞬间 widget 的 DOM 可能还没就绪，稍后重试几次
            let tries = 0;
            const tryAttach = () => {
                let ok = false;
                try {
                    ok = attachAutocomplete(this);
                } catch (e) {
                    console.warn("[Anima XY LoRA] 自动补全挂载失败：", e);
                }
                if (!ok && ++tries <= 20) setTimeout(tryAttach, 150);
            };
            tryAttach();
            return r;
        };

        // 兜底：右键节点菜单里可手动重挂一次
        const getMenu = nodeType.prototype.getExtraMenuOptions;
        nodeType.prototype.getExtraMenuOptions = function (canvas, options) {
            const r = getMenu?.apply(this, arguments);
            options.push({
                content: "重挂 LoRA 自动补全",
                callback: () => {
                    try {
                        if (attachAutocomplete(this)) {
                            console.log("[Anima XY LoRA] 自动补全已重挂");
                        } else {
                            console.warn("[Anima XY LoRA] 仍未找到输入框元素");
                        }
                    } catch (e) {
                        console.warn("[Anima XY LoRA]", e);
                    }
                },
            });
            return r;
        };
    },

    // 预取一次清单，减少首次输入的延迟
    async setup() {
        try {
            await getLoraList();
        } catch (e) { /* 忽略 */ }
    },
});
