/**
 * Anima Toolkit —— 颜色输入框的取色面板（色卡 + 系统取色器）
 * ------------------------------------------------------------
 * 目标：把节点里那些手填 #RRGGBB 的纯文本输入框，换成
 *       「色块按钮 + 十六进制输入框 + 清空」的一行控件；色卡不铺在节点上占高度，
 *       而是收进点色块弹出的取色面板里，面板同时提供系统取色器（任意色）、
 *       12 色色卡、清空。
 *
 * 目前覆盖两个节点（见 NODE_CONFIGS）：
 *   comfyui_XYGridDrawer      自定义背景色 / 自定义文字色 / 边框颜色
 *   comfyui_ImageBadge        文字颜色 / 描边颜色
 *   comfyui_XYLoRA / comfyui_SaveWithLabels  没有颜色控件，只做「旧标题还原」
 *
 * 实现要点（针对 ComfyUI 前端 1.55，Classic 画布与 Nodes 2.0 面板都适用）：
 *   1. 用 node.addDOMWidget(name, type, element, options) 建原生 DOM 控件，
 *      它的 value 走 options.getValue / options.setValue，所以仍然按
 *      widgets_values 序列化成字符串，Python 端依旧收到 STRING，无需改后端。
 *   2. **原位替换**：先记下原生 STRING widget 的下标，建好 DOM widget（会被
 *      追加到末尾）再挪回原下标，保证工作流 widgets_values 的顺序不变。
 *   3. 弹层挂在 document.body + position:fixed：节点容器会裁切溢出内容，
 *      挂在 body 上才不会被切掉；靠 data-uid 判断"点在面板/触发按钮上"，
 *      点别处或按 Esc 自动关闭。
 *   4. DOM widget 不会自己画名字标签，所以标签做在元素内部。
 *   5. type 带实例序号：Nodes 2.0 的 WidgetDOM 行以 (节点id, 名字, 类型) 作为
 *      key，类型固定时重建节点可能复用旧组件、导致新元素不挂载。
 *   6. 留空 = 未设置（背景走白色、文字走黑色、边框走 #999999）。
 */

import { app } from "../../scripts/app.js";

/** 色卡：白 → 黑 → 常用彩色，够覆盖字体/背景/描边的绝大多数需求 */
const PRESETS = [
    "#FFFFFF", "#F2F2F2", "#D9D9D9", "#9E9E9E", "#5A5A5A", "#000000",
    "#D64040", "#E08A2E", "#3C9E60", "#3A68C8", "#8E5FD0", "#F0C840",
];

/**
 * 历史布局的控件顺序。读老工作流时 widgets_values 是按位置映射的，
 * 控件增删/换序后必须按名字重新对齐，否则值会串到别的控件上。
 */
// 15 项：最早的版本（含后来删掉的「背景颜色 / 文字颜色」两个下拉）
const LEGACY_ORDER_15 = [
    "字体", "字体大小", "网格间距", "背景颜色", "文字颜色", "列数", "标签样式",
    "单元格排布", "图片缩放", "标题", "自定义背景色", "自定义文字色",
    "字体文件路径", "单元格边框", "边框颜色",
];
// 13 项：带色卡、但「单元格边框」还在「边框颜色」之前的那版
const LEGACY_ORDER_13 = [
    "标题", "字体", "字体文件路径", "字体大小", "网格间距", "列数", "标签样式",
    "单元格排布", "图片缩放", "自定义背景色", "自定义文字色", "单元格边框", "边框颜色",
];

/** 拼图节点：按值个数与类型猜出旧布局（返回 null 表示已经是当前布局） */
function pickGridOrder(values) {
    if (values.length === LEGACY_ORDER_15.length) return LEGACY_ORDER_15;
    // 旧 13 项布局里第 12 位（索引 11）是整数宽度；当前布局里那是颜色字符串
    if (values.length === LEGACY_ORDER_13.length && typeof values[11] === "number") return LEGACY_ORDER_13;
    return null;
}

/**
 * 每个节点要改造哪些输入。
 *   targets    控件名 → { label: 行内显示的短标签, fallback: 留空时预览用的颜色 }
 *   bottomRoom 新建节点时底部额外留白（末位是 DOM 色卡才需要，见下方说明）
 *   pickOrder  旧工作流的 widgets_values 顺序表（控件增删过才需要）
 *
 * 注意：**最后一个控件不能是 DOM 色卡**。前端 1.55 里末位是 DOM 控件时，
 * 它的判定区会盖住节点右下角的缩放手柄，节点就再也拖不动大小了。
 * comfyui_ImageBadge 的末位是「自动缩放」(BOOLEAN)，安全。
 */
const GRID_TARGETS = {
    "自定义背景色": { label: "背景色", fallback: "#FFFFFF" },
    "自定义文字色": { label: "文字色", fallback: "#000000" },
    "边框颜色": { label: "边框色", fallback: "#999999" },
};
const GRID_CONFIG = {
    bottomRoom: 18,
    targets: GRID_TARGETS,
    pickOrder: pickGridOrder,
    // 老工作流里给这个节点起过的自定义标题；打开时清掉，让它回落到新的显示名
    legacyTitles: [
        "ZML XY Grid Drawer",
        "Anima XY 图表拼接（替代 ZML）",
        "XY 图表拼接（替代 ZML）",
    ],
};

const BADGE_TARGETS = {
    "文字颜色": { label: "文字色", fallback: "#FFFFFF" },
    "描边颜色": { label: "描边色", fallback: "#000000" },
};
const BADGE_CONFIG = {
    bottomRoom: 0,
    targets: BADGE_TARGETS,
    legacyTitles: [
        "Anima 逐张文字标注（LoRA名 + 权重）",
        "逐张文字标注（LoRA名 + 权重）",
    ],
};

// 没有颜色控件的两个节点：只用来把老工作流里的旧标题还原成显示名
const MODEL_CONFIG = {
    targets: {},
    legacyTitles: [
        "Anima XY LoRA（改 LoRA名称 / 权重起止 / 档数）",
        "XY LoRA（改 LoRA名称 / 权重起止 / 档数）",
    ],
};
const SAVE_CONFIG = {
    targets: {},
    legacyTitles: [
        "Anima 按标签命名保存（文件名带 LoRA名 + 权重）",
        "按标签命名保存（文件名带 LoRA名 + 权重）",
        "Anima 按标签命名保存",
    ],
};

const NODE_CONFIGS = {
    comfyui_XYGridDrawer: GRID_CONFIG,
    comfyui_ImageBadge: BADGE_CONFIG,
    comfyui_XYLoRA: MODEL_CONFIG,
    comfyui_SaveWithLabels: SAVE_CONFIG,
};

const HEX_RE = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i;
const EMPTY_HATCH = "repeating-linear-gradient(45deg, #c9c9c9 0 4px, #ffffff 4px 8px)";
const POP_WIDTH = 226;
const POP_EST_HEIGHT = 210;

let instanceSeq = 0;
let uidSeq = 0;

/** "#FFF" / "fff" / "#ffffff" → "#ffffff"；非法或空 → "" */
function normalizeHex(value) {
    const raw = String(value ?? "").trim();
    if (!HEX_RE.test(raw)) return "";
    let hex = raw.replace("#", "").toLowerCase();
    if (hex.length === 3) hex = hex.split("").map((c) => c + c).join("");
    return `#${hex}`;
}

function injectStyle() {
    if (document.getElementById("anima-color-widget-style")) return;
    const style = document.createElement("style");
    style.id = "anima-color-widget-style";
    style.textContent = `
/* 前端给每个 DOM 控件套的 .dom-widget 层是 pointer-events:auto，且会盖住节点
   右下角的缩放手柄（实测 node_over 为 null，拖不动）。这里只对包着色卡的那层做
   穿透，色卡内部的控件仍然可点。 */
.dom-widget:has(> .anima-color-widget) { pointer-events: none !important; }
.anima-color-swatch:hover { filter: brightness(1.15); }
.anima-color-pop button:hover { filter: brightness(1.15); }
.anima-color-widget input:focus { outline: 1px solid var(--p-primary-color, #4a9eff); }
`;
    document.head.appendChild(style);
}

// ---------------------------------------------------------------- 取色面板

let openPopover = null;

function closePopover() {
    if (!openPopover) return;
    const el = openPopover;
    openPopover = null;
    try {
        el.__onClose?.();
    } catch (err) {
        console.warn("[Anima颜色] 关闭面板回调出错：", err);
    }
    el.remove?.();
}

/** 沿 DOM 往上找带 data-uid 的元素（判断点击是否落在面板/触发按钮上） */
function closestUid(el) {
    let cur = el;
    while (cur) {
        if (cur.dataset?.uid) return cur.dataset.uid;
        cur = cur.parent ?? cur.parentNode;
    }
    return null;
}

document.addEventListener("pointerdown", (event) => {
    if (!openPopover) return;
    if (closestUid(event.target) === openPopover.dataset?.uid) return;
    closePopover();
}, true);

window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closePopover();
}, true);

function placePopover(el, anchor) {
    const rect = anchor?.getBoundingClientRect?.() ?? { left: 0, top: 0, bottom: 0 };
    const vw = window.innerWidth || 1280;
    const vh = window.innerHeight || 800;
    let left = Math.round(rect.left);
    let top = Math.round((rect.bottom ?? 0) + 6);
    if (left + POP_WIDTH > vw - 8) left = Math.max(8, vw - POP_WIDTH - 8);
    if (top + POP_EST_HEIGHT > vh - 8) top = Math.max(8, Math.round(rect.top - POP_EST_HEIGHT - 6));
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
}

/**
 * 造一行控件：标签 + 色块按钮（点开取色面板）+ 十六进制输入框 + 清空。
 * @returns {{root: HTMLElement, apply: (v: string) => void, getValue: () => string, bind: (w: any) => void}}
 */
function buildColorCard(cfg, initialValue) {
    const uid = `anima-color-${++uidSeq}`;

    const root = document.createElement("div");
    root.className = "anima-color-widget";
    // 高度写死成一行：前端全局样式会把 input 的 padding/min-height 撑大，
    // 不锁高度的话每个 DOM widget 会比普通控件行高出一截，行距看起来就散了
    root.style.cssText = [
        "width:100%", "box-sizing:border-box", "font-size:11px",
        "line-height:1", "color:var(--input-text,var(--fg-color,#ddd))",
        "height:20px", "max-height:20px", "overflow:hidden",
        "display:flex", "align-items:center",
        "padding:0 26px 0 14px", // 右侧多留 26px：× 不能压到节点右下角的缩放手柄
        "pointer-events:none", // 空白处不拦鼠标，否则会挡住节点右下角的缩放手柄
    ].join(";");

    const line = document.createElement("div");
    line.style.cssText = [
        "display:flex", "align-items:center", "gap:6px",
        "width:100%", "height:20px",
    ].join(";");

    const label = document.createElement("span");
    label.textContent = cfg.label;
    label.title = "留空则用默认色";
    label.style.cssText = "flex:0 0 auto;opacity:.85;white-space:nowrap;line-height:20px;";

    const swatch = document.createElement("button");
    swatch.type = "button";
    swatch.className = "anima-color-swatch";
    swatch.dataset.uid = uid;          // 面板靠它判断"这是触发按钮"
    swatch.title = "打开取色面板（色卡 + 系统取色器）";
    swatch.style.cssText = [
        "flex:0 0 auto", "width:26px", "height:20px", "min-height:0",
        "padding:0", "margin:0", "box-sizing:border-box", "pointer-events:auto",
        "border:1px solid var(--border-color,#555)", "border-radius:4px",
        "cursor:pointer", "overflow:hidden", "background:#fff",
    ].join(";");

    const text = document.createElement("input");
    text.type = "text";
    text.placeholder = "留空 = 默认";
    text.spellcheck = false;
    text.style.cssText = [
        "flex:1 1 auto", "min-width:0", "height:20px", "min-height:0", "max-height:20px",
        "padding:0 6px", "margin:0", "box-sizing:border-box", "pointer-events:auto",
        "border:1px solid var(--border-color,#555)", "border-radius:4px",
        "background:var(--comfy-input-bg,#222)", "color:inherit",
        "font:inherit", "line-height:18px", "appearance:none", "-webkit-appearance:none",
    ].join(";");

    const clear = document.createElement("button");
    clear.type = "button";
    clear.textContent = "×";
    clear.title = "清空（恢复默认色）";
    clear.style.cssText = [
        "flex:0 0 auto", "width:20px", "height:20px", "min-height:0",
        "padding:0", "margin:0", "box-sizing:border-box", "pointer-events:auto",
        "border:1px solid var(--border-color,#555)", "border-radius:4px",
        "background:var(--comfy-input-bg,#222)", "color:inherit",
        "cursor:pointer", "line-height:18px", "font-size:13px",
    ].join(";");

    line.append(label, swatch, text, clear);
    root.append(line);

    let widget = null;
    let pop = null;                    // {el, native, readout, chips: []}

    const apply = (value) => {
        const raw = value == null ? "" : String(value).trim();
        const hex = normalizeHex(raw);
        // 合法颜色统一成小写 #rrggbb（Python 端两种写法都认，这里只为显示一致）；
        // 非法文本原样保留，交给用户自己改，不会被悄悄改掉
        const shown = hex || raw;
        if (text.value !== shown) text.value = shown;

        swatch.style.background = hex || cfg.fallback;
        swatch.style.backgroundImage = hex ? "none" : EMPTY_HATCH;

        if (pop) {
            pop.native.value = hex || cfg.fallback;
            pop.readout.textContent = hex || (raw ? raw : "未设置");
            pop.preview.style.background = hex || cfg.fallback;
            pop.preview.style.backgroundImage = hex ? "none" : EMPTY_HATCH;
            for (const chip of pop.chips) {
                const on = !!hex && chip.dataset.hex.toLowerCase() === hex;
                chip.style.borderColor = on ? "var(--fg-color,#fff)" : "rgba(0,0,0,.45)";
                chip.style.boxShadow = on ? "0 0 0 1px var(--fg-color,#fff)" : "none";
            }
        }
    };

    /** 用户改动后写回控件（走 widget.value，前端才能收到 callback） */
    const commit = () => {
        const value = text.value.trim();
        if (widget) {
            try {
                widget.value = value;   // → options.setValue → apply + callback
            } catch (err) {
                console.error("[Anima颜色] 写回数值失败：", err);
            }
        } else {
            apply(value);
        }
        app.graph?.setDirtyCanvas?.(true, true);
    };

    const pick = (hex) => {
        apply(hex);
        commit();
    };

    const buildPopover = () => {
        const el = document.createElement("div");
        el.className = "anima-color-pop";
        el.dataset.uid = uid;
        el.style.cssText = [
            "position:fixed", "z-index:10000", `width:${POP_WIDTH}px`,
            "padding:10px", "box-sizing:border-box", "border-radius:10px",
            "border:1px solid var(--border-color,#4a4a4a)",
            "background:var(--comfy-menu-bg,#242424)",
            "box-shadow:0 8px 24px rgba(0,0,0,.45)",
            "display:flex", "flex-direction:column", "gap:8px",
            "font-size:11px", "color:var(--input-text,#ddd)",
        ].join(";");

        const mkBtn = (labelText, onClick) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.textContent = labelText;
            btn.style.cssText = [
                "height:20px", "padding:0 8px", "white-space:nowrap",
                "border:1px solid var(--border-color,#555)", "border-radius:4px",
                "background:var(--comfy-input-bg,#222)", "color:inherit",
                "cursor:pointer", "font-size:11px",
            ].join(";");
            btn.addEventListener("click", (event) => {
                event.stopPropagation?.();
                onClick();
            });
            return btn;
        };

        const head = document.createElement("div");
        head.style.cssText = "display:flex;align-items:center;gap:8px;";

        // 色块用 div 画：原生 input[type=color] 的色块会被前端全局 reset 样式吃掉，
        // 只留一个透明 input 盖在上面，点它依旧打开浏览器/系统的取色器。
        const swatchBox = document.createElement("div");
        swatchBox.style.cssText = [
            "position:relative", "flex:0 0 auto", "width:40px", "height:26px",
            "border-radius:5px", "overflow:hidden",
        ].join(";");

        const preview = document.createElement("div");
        preview.style.cssText = [
            "position:absolute", "inset:0", "box-sizing:border-box",
            "border:1px solid var(--border-color,#555)", "border-radius:5px",
            "background:#ffffff",
        ].join(";");

        const native = document.createElement("input");
        native.type = "color";
        native.title = "点这里用系统取色器调任意颜色";
        native.style.cssText = [
            "position:absolute", "inset:0", "width:100%", "height:100%",
            "margin:0", "padding:0", "border:0", "opacity:0",
            "-webkit-appearance:none", "appearance:none", "cursor:pointer",
        ].join(";");

        swatchBox.append(preview, native);

        const readout = document.createElement("span");
        readout.textContent = "未设置";
        readout.style.cssText = "flex:1 1 auto;font-family:var(--font-mono,monospace);opacity:.9;";

        const sysBtn = mkBtn("系统取色器…", () => native.click?.());
        head.append(swatchBox, readout, sysBtn);

        const hint = document.createElement("div");
        hint.textContent = "点左侧色块或「系统取色器…」调任意色，点下面色卡快速选色";
        hint.style.cssText = "opacity:.55;font-size:10px;line-height:1.35;";

        const grid = document.createElement("div");
        grid.style.cssText = "display:grid;grid-template-columns:repeat(6,1fr);gap:4px;";

        const chipBtns = [];
        for (const hex of PRESETS) {
            const chip = document.createElement("button");
            chip.type = "button";
            chip.dataset.hex = hex;
            chip.title = hex;
            chip.style.cssText = [
                "height:22px", "padding:0", "border:1px solid rgba(0,0,0,.45)",
                "border-radius:4px", `background:${hex}`, "cursor:pointer",
            ].join(";");
            chip.addEventListener("click", (event) => {
                event.stopPropagation?.();
                pick(hex);
            });
            chipBtns.push(chip);
            grid.appendChild(chip);
        }

        const foot = document.createElement("div");
        foot.style.cssText = "display:flex;gap:6px;justify-content:flex-end;";

        const reset = mkBtn("恢复默认", () => pick(""));
        const done = mkBtn("完成", () => closePopover());
        foot.append(reset, done);

        el.append(head, hint, grid, foot);

        native.addEventListener("input", () => pick(native.value));
        // 面板里的按键不要冒泡到画布（否则 Delete 会删掉选中的节点）
        for (const type of ["keydown", "keyup"]) {
            el.addEventListener(type, (event) => event.stopPropagation?.());
        }

        return { el, native, preview, readout, chips: chipBtns };
    };

    const togglePopover = () => {
        if (pop) {
            closePopover();
            return;
        }
        closePopover();
        pop = buildPopover();
        pop.el.__onClose = () => { pop = null; };
        openPopover = pop.el;
        document.body.appendChild(pop.el);
        placePopover(pop.el, swatch);
        apply(text.value);         // 同步面板里的预览与高亮
    };

    swatch.addEventListener("click", (event) => {
        event.stopPropagation?.();
        togglePopover();
    });
    text.addEventListener("input", () => {
        const hex = normalizeHex(text.value);
        swatch.style.background = hex || cfg.fallback;
        swatch.style.backgroundImage = hex ? "none" : EMPTY_HATCH;
    });
    text.addEventListener("change", commit);
    text.addEventListener("blur", commit);
    clear.addEventListener("click", (event) => {
        event.stopPropagation?.();
        pick("");
    });

    // 控件内的操作不要传给画布（否则会被当节点拖拽 / 触发快捷键）
    for (const type of ["pointerdown", "mousedown", "wheel"]) {
        root.addEventListener(type, (event) => event.stopPropagation?.());
    }

    apply(initialValue);
    return {
        root,
        apply,
        getValue: () => text.value.trim(),
        bind: (w) => { widget = w; },
    };
}

// ---------------------------------------------------------------- 安装与迁移

/**
 * 控件增删/换序后，把老存档里的值按名字搬回正确的控件上。
 * 优先用 widgets_values_named（前端保存时自带），否则按该节点的历史布局顺序对齐。
 */
function migrateLegacyValues(node, info, cfg) {
    const values = info?.widgets_values;
    if (!Array.isArray(values)) return;

    const named = info?.widgets_values_named;
    let mapping = null;
    if (named && typeof named === "object" && !Array.isArray(named)) {
        mapping = named;
    } else if (typeof cfg.pickOrder === "function") {
        const order = cfg.pickOrder(values);
        if (order) mapping = Object.fromEntries(order.map((name, i) => [name, values[i]]));
    }
    if (!mapping) return;

    for (const widget of node.widgets ?? []) {
        if (widget?.serialize === false) continue;
        if (!(widget.name in mapping)) continue;
        const value = mapping[widget.name];
        if (value === undefined) continue;
        try {
            if (widget.value !== value) widget.value = value;
        } catch (err) {
            console.warn(`[Anima颜色] 迁移 ${widget.name} 的旧值失败：`, err);
        }
    }
}

/**
 * 让 DOM 控件宽度始终等于节点宽度。
 * ComfyUI 前端 #12443 会把过期的测量值写进 widget.width（经典画布下也会），
 * 结果是节点拉宽/缩窄后透明元素还停在旧宽度，缩窄时会盖住右下角的缩放手柄。
 */
function bindWidgetWidthToNode(node, widget) {
    try {
        Object.defineProperty(widget, "width", {
            configurable: true,
            enumerable: true,
            get: () => (Array.isArray(node.size) ? node.size[0] : (node.width ?? 300)),
            set() { /* 忽略前端写入的过期宽度 */ },
        });
    } catch (err) {
        console.warn("[Anima颜色] 绑定控件宽度失败：", err);
    }
}

/**
 * 老工作流里节点常带自定义标题（会盖住新的显示名）。
 * 打开文件时若标题仍是历史名称，就清空它 —— litegraph 会回落到节点定义的显示名。
 */
function normalizeTitle(node, cfg) {
    const title = node?.title == null ? "" : String(node.title).trim();
    if (!title) return;
    // 历史标题，或早期版本写进标题的「（旧名 · 已弃用）」
    const stale = (cfg?.legacyTitles ?? []).includes(title) || title.includes("（旧名 · 已弃用）");
    if (!stale) return;
    try {
        node.title = null;          // null → 用 display_name
        node.setDirtyCanvas?.(true, true);
        console.log(`[Anima颜色] 已把旧标题「${title}」还原为显示名`);
    } catch (err) {
        console.warn("[Anima颜色] 还原标题失败：", err);
    }
}

/** 等布局完成再把节点撑到能放下色卡（bottomRoom = 0 时基本不会触发） */
function growToFit(node, bottomRoom) {
    try {
        const size = node.computeSize?.();
        if (!Array.isArray(size) || !Array.isArray(node.size)) return;
        // 末位若是 DOM 色卡，底部要留一条空白，否则色卡会压住右下角的缩放手柄
        const need = (Number(size[1]) || 0) + Math.max(0, Number(bottomRoom) || 0);
        if (need > node.size[1] + 4) {
            node.setSize?.([node.size[0], need]);
            node.setDirtyCanvas?.(true, true);
        }
    } catch (err) {
        console.warn("[Anima颜色] 调整节点高度失败：", err);
    }
}

/** 把节点上的颜色输入换成一行式控件 */
function installColorCards(node, cfg) {
    if (!node?.widgets?.length || typeof node.addDOMWidget !== "function") return;

    for (const [name, target] of Object.entries(cfg.targets)) {
        const existing = node.widgets.find((w) => w?.name === name && !w.__animaColorCard);
        if (!existing) continue;              // 没有这个输入（或已替换过）

        const index = node.widgets.indexOf(existing);
        const initial = existing.value ?? "";
        const card = buildColorCard(target, initial);

        instanceSeq += 1;
        let dom;
        try {
            dom = node.addDOMWidget(name, `anima_color_${instanceSeq}`, card.root, {
                hideOnZoom: false,
                margin: 0,          // 上下留白会变成行间空隙，这里必须归零
                getMinHeight: () => 22,
                getValue: () => card.getValue(),
                setValue: (v) => card.apply(v),
            });
        } catch (err) {
            console.error(`[Anima颜色] ${name} 创建色卡失败，保留原生输入框：`, err);
            continue;
        }

        dom.__animaColorCard = true;
        card.bind(dom);
        bindWidgetWidthToNode(node, dom);
        try { dom.serialize = true; } catch (_e) { /* 只读属性时忽略 */ }
        if (dom.options) dom.options.serialize = true;

        // addDOMWidget 内部的“还原控件值”可能按 (节点, 名字, 序号) 取到相邻控件的值，
        // 这里用原生输入框的当前值兜一次底
        if (!normalizeHex(dom.value) && normalizeHex(initial)) card.apply(initial);

        // 删掉原生 STRING widget，再把色卡挪回它原来的位置（保持 widgets_values 顺序）。
        // 注意不能用 node.removeWidget：那个接口只处理 subgraph 的额外控件，动不了基础控件。
        const at = node.widgets.indexOf(existing);
        if (at >= 0) node.widgets.splice(at, 1);
        const current = node.widgets.indexOf(dom);
        if (current >= 0 && index >= 0 && current !== index) {
            node.widgets.splice(current, 1);
            node.widgets.splice(Math.min(index, node.widgets.length), 0, dom);
        }
    }
}

app.registerExtension({
    name: "AnimaToolkit.ColorCard",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const cfg = NODE_CONFIGS[nodeData?.name];
        if (!cfg) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            try {
                injectStyle();
                installColorCards(this, cfg);
            } catch (err) {
                console.error("[Anima颜色] 色卡初始化失败：", err);
            }
            // 只在"新建节点"时兜一次底撑高；读档（onConfigure）不碰尺寸，
            // 否则用户手动拉小后一刷新就被弹回去
            requestAnimationFrame(() => {
                if (!this.__animaConfigured) growToFit(this, cfg.bottomRoom);
            });
            return result;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            this.__animaConfigured = true;
            const result = onConfigure?.apply(this, arguments);
            try {
                migrateLegacyValues(this, info, cfg);
                normalizeTitle(this, cfg);
            } catch (err) {
                console.warn("[Anima颜色] 旧值迁移失败：", err);
            }
            return result;
        };

        // 拖动改尺寸时重绘，避免色卡行宽度不动
        const onResize = nodeType.prototype.onResize;
        nodeType.prototype.onResize = function () {
            const result = onResize?.apply(this, arguments);
            try {
                this.setDirtyCanvas?.(true, true);
            } catch (err) {
                console.warn("[Anima颜色] 重绘失败：", err);
            }
            return result;
        };

        // 节点被删/复制时别留下孤儿弹层
        const onRemoved = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            if (openPopover) closePopover();
            return onRemoved?.apply(this, arguments);
        };
    },
});
