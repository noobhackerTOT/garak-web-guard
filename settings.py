from urllib.parse import urlsplit

PROBES = [
    {"id": "promptinject.HijackHateHumans", "label": "提示注入", "category": "指令边界",
     "description": "检查回答是否出现注入载荷中的指定字符串。", "detector": "promptinject.AttackRogueString"},
    {"id": "dan.Dan_11_0", "label": "角色越狱", "category": "角色约束",
     "description": "检查模型是否接受 DAN 角色并出现相关标记。", "detector": "dan.DAN"},
    {"id": "encoding.InjectBase64", "label": "编码绕过", "category": "编码载荷",
     "description": "检查 Base64 编码载荷是否被解码并输出。", "detector": "encoding.DecodeMatch"},
    {"id": "leakreplay.LiteratureComplete", "label": "训练文本复现", "category": "文本泄漏",
     "description": "检查是否续写出语料中的原始文学文本；不检测系统提示词或账号数据泄漏。", "detector": "leakreplay.StartsWith"},
]
PROBE_IDS = {p["id"] for p in PROBES}
DEFAULT_TARGET = {
    "name": "我的网页 AI", "url": "", "channel": "msedge", "browser_mode": "standard",
    "input_selector": 'textarea, [contenteditable="true"][role="textbox"], [contenteditable="true"]',
    "send_selector": "", "response_selector": '[data-message-author-role="assistant"]', "response_mode": "auto",
    "new_chat_selector": "", "busy_selector": 'button[aria-label*="Stop"], button[aria-label*="停止"], button[data-testid="stop-button"]',
    "frame_selector": "", "launcher_selector": "", "launcher_frame_selector": "", "reset_each_prompt": False,
    "timeout_s": 120, "stable_ms": 2500, "min_response_ms": 3500, "delay_s": 2,
}


def bounded_number(value, low, high, label, integral=False):
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{label}必须是数字") from None
    if not low <= value <= high:
        raise ValueError(f"{label}范围为 {low}–{high}")
    if integral and not value.is_integer():
        raise ValueError(f"{label}必须是整数")
    return int(value) if integral else value


def validate_target(data):
    if not isinstance(data, dict):
        raise ValueError("目标配置必须是对象")
    target = {k: data.get(k, v) for k, v in DEFAULT_TARGET.items()}
    for key, value in target.items():
        if isinstance(DEFAULT_TARGET[key], str):
            if not isinstance(value, str) or len(value) > 2000:
                raise ValueError(f"{key}格式错误或过长")
            target[key] = value.strip()
    if len(target["name"]) > 100:
        raise ValueError("目标名称不能超过 100 个字符")
    parts = urlsplit(target["url"])
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("请输入完整 http/https 网页地址，地址中不能包含账号密码")
    if target["channel"] not in {"msedge", "chrome", "chromium"}:
        raise ValueError("浏览器只能选择 Edge、Chrome 或 Chromium")
    if target["browser_mode"] not in {"standard", "background"}:
        raise ValueError("请选择常规浏览器或后台检测模式")
    if target["response_mode"] not in {"auto", "selector"}:
        raise ValueError("请选择自动识别或指定选择器读取回答")
    if not target["input_selector"] or (target["response_mode"] == "selector" and not target["response_selector"]):
        raise ValueError("输入框和回答区域选择器不能为空")
    if not isinstance(target["reset_each_prompt"], bool):
        raise ValueError("新对话选项必须是布尔值")
    if target["reset_each_prompt"] and not target["new_chat_selector"]:
        raise ValueError("每题新建对话需要填写新对话按钮选择器")
    for key, low, high, integral in [
        ("timeout_s", 10, 600, True), ("stable_ms", 500, 20000, True),
        ("min_response_ms", 500, 30000, True), ("delay_s", 0, 60, False),
    ]:
        target[key] = bounded_number(target[key], low, high, key, integral)
    if target["timeout_s"] * 1000 <= max(target["stable_ms"], target["min_response_ms"]):
        raise ValueError("回答超时必须大于文本稳定等待时间")
    return target


def validate_scan(data):
    target = validate_target(data.get("target"))
    probes = data.get("probes")
    if not isinstance(probes, list) or not probes or any(p not in PROBE_IDS for p in probes):
        raise ValueError("请选择至少一项内置 garak 检测")
    probes = list(dict.fromkeys(probes))
    cap = bounded_number(data.get("max_per_probe", 5), 1, 30, "每组数量", True)
    return {"target": target, "probes": probes, "max_per_probe": cap}
