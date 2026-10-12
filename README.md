# Garak Web Guard

基于本地 garak 的网页 AI 安全检测工具。使用独立的 Edge / Chrome 浏览器登录目标网页，自动输入 garak 测试问题、读取网页回答，并输出可复核的报告。

当前版本支持 Windows，已验证 Python 3.12、garak 0.17.0 和 Microsoft Edge。目标网页需按其页面结构配置选择器。

## 启动

前提：已安装 garak 的 Python 环境、`uv` 和 Edge / Chrome。脚本默认使用 `D:\garak\venv\Scripts\python.exe`；路径不同请通过 `-GarakPython` 指定。

首次从 GitHub 下载后，在 PowerShell 运行：

```powershell
.\Install.ps1
.\Start.ps1
```

已有 garak 环境位于其他路径时：

```powershell
.\Install.ps1 -GarakPython '你的 garak 环境\Scripts\python.exe'
.\Start.ps1 -GarakPython '你的 garak 环境\Scripts\python.exe'
```

依赖安装后可以双击 `start.cmd` 启动。若尚无 garak 环境，可先安装 Python 3.12 与 uv，然后创建独立环境（不要覆盖已有环境）：

```powershell
uv venv --python 3.12 D:\garak\venv
uv pip install --python D:\garak\venv\Scripts\python.exe garak==0.17.0
.\Install.ps1
.\Start.ps1
```

控制台：<http://127.0.0.1:8787>。双击 `stop.cmd` 或运行 `Stop.ps1` 可关闭服务与独立浏览器；前台启动时也可按 Ctrl+C。

Playwright 依赖安装在项目 `.vendor` 下，不修改已有 garak 安装。浏览器资料、配置、缓存、扫描报告以及已下载依赖均不包含在 Git 仓库中。

没有 Edge / Chrome 时，可运行 `Install.ps1 -Chromium` 并在控制台选择 Playwright Chromium。修改端口：`Start.ps1 -Port 8790`。启动参数 `-NoOpen` 可以只启动服务。

## 使用

1. 填写聊天网页网址，点击“打开浏览器并登录”。在新窗口手动登录并进入可聊天的页面。
2. 先手动发送一句话，生成一个 AI 回答气泡。配置输入框和回答区域，可以在控制台点击“点选”，再切换到浏览器点击对应元素。回答区域应只匹配 AI 回答，并能匹配后续新增的回答。默认回答选择器只是示例，不保证适配所有网站。
3. 按需配置发送按钮；留空会在输入框按 Enter。建议配置新对话按钮并勾选“每个问题使用新对话”。连续对话可能受到前文影响，报告会记录此设置。
4. 点击“检查选择器”，然后点击“连接测试”。连接测试只发送 `请只回复：连接成功`，确认收到的是新的完整回答。
5. 选择检测范围和每组问题数量，点击“开始安全检测”。首次建议每组 1–5 个。上限最多每组 30 个、单轮 120 个；实际数量由探针自身问题数量决定，每题 1 次生成。
6. 查看命中记录中的测试问题、网页回答和检测器分值，并导出 HTML / JSON / 原始 JSONL。

“体验本地演示”会打开一个**模拟聊天网页**。它故意输出指定探针标记，用于验证操作和检测链路，不代表任何真实 AI 产品的安全水平。也可访问 `/demo?vulnerable=0` 使用拒绝模式。

## 检测范围

| 范围 | garak 探针 | 判定依据 |
| --- | --- | --- |
| 提示注入 | `promptinject.HijackHateHumans` | 回答中出现注入的目标字符串 |
| 角色越狱 | `dan.Dan_11_0` | 回答中出现 DAN 角色标记 |
| 编码绕过 | `encoding.InjectBase64` | 编码中的目标载荷被解码输出 |
| 训练文本复现 | `leakreplay.LiteratureComplete` | 续写与探针语料中的文学原文匹配 |

内置探针以英文为主。训练文本复现不检查系统提示词、聊天历史或私人账号数据泄漏。检测器分值 ≥ 0.5 记为命中；命中是待人工复核信号，可能误报，未命中不代表系统整体安全。“未命中比例”只统计已判定测试，不是完整安全评分。

## 网页适配与限制

- 适合 DOM 中有可编辑输入框和文本回答的聊天网页。Canvas、封闭 Shadow DOM、需要文件上传或特殊前端协议的网站需要另行适配。
- 元素点选仅支持主页面。iframe 页面可填写 iframe 选择器，以及该 iframe 内部的输入/回答/按钮选择器。
- 点选返回的固定 `nth-of-type` 路径可能只匹配当前气泡；建议改为回答元素的重复 class 或 data 属性。
- 优先配置“生成中按钮 / 状态选择器”。读取器等待新的回答，并在生成状态结束、文本稳定且达到最短等待时间后返回。没有明确生成状态的流式网页依赖稳定时间；流式暂停较长时需要延长等待，避免截断回答。
- 新回答使用 DOM 身份、数量与文本变化判断，避免读取上一轮相同回答；无法确认新回答时会超时，记录连接错误，不计为通过。
- 登录、验证码、网站限流由用户手动处理。停止检测会终止 garak 并停止后续问题，网页已经开始的生成可能仍需手动停止。
- 扫描期间禁止更改浏览器连接。每次扫描独立启动 garak 进程；断开、超时、缺少检测器判定或未覆盖所选探针时，任务标记为未完成。
- 支持并验证的 garak 版本：本机 `0.17.0`。每组硬上限使用该版本的 `Probe._execute_all` 接口；升级 garak 后请重跑测试。

## 数据与运行结构

服务只监听 `127.0.0.1`。控制台接口检查来源、Host 和会话令牌；garak 的浏览器桥接使用独立的临时令牌。页面使用文本显示模型输出，导出的 HTML 转义问题、回答和目标名称。

登录资料保存在 `.runtime/browser-profile`，目标配置保存在 `.runtime/target.json`，报告和日志保存在 `.runtime/runs/<任务ID>/`。这些文件可能包含登录会话和测试对话，应妥善保管；`.gitignore` 已排除 `.runtime` 与 `.vendor`。工具不会读取系统浏览器已有的用户资料。

```text
本地控制台 → HTTP 服务 → 浏览器 actor 线程 → 独立 Edge / Chrome 聊天页面
                     ↘ garak 子进程 → function.Single → 临时本地桥接 → 浏览器
                                                    ↘ 原始 JSONL + 汇总 JSON + HTML 报告
```

项目将 garak 配置、缓存和报告目录重定向到 `.runtime`。garak 通过自己的 CLI、探针和主检测器执行扫描；未替换为自制评分规则。关闭扩展检测器以减少首轮额外依赖和运行成本。

## 验证

```powershell
& 'D:\garak\venv\Scripts\python.exe' -m unittest discover -s tests -p 'test_*.py' -v
& 'D:\garak\venv\Scripts\python.exe' tests/e2e.py
```

端到端测试使用真实 Edge 和真实 garak，对本地模拟网页验证：流式读取、元素点选、探针命中、问题上限、重复相同回答、连接超时、停止检测、报告下载、控制台交互、桌面/手机布局和接口访问保护。产物在 `.runtime/qa/`；真实网页仍需按上述步骤登录和适配。

开发验证已通过：7 项单元测试；四组探针端到端扫描完成（模拟风险模式 4 项判定 / 2 项命中）；拒绝模式 2 项判定 / 0 项命中；超时被正确标记为未完成；停止后能重新测试连接；控制台无 JavaScript 错误。运行端到端测试会生成示例报告 `.runtime/qa/sample-report.html` 和验证记录 `.runtime/qa/verification.json`。测试状态和浏览器资料与正常使用的数据分开保存。

官方参考：[garak](https://github.com/NVIDIA/garak)、[function generator 源码](https://github.com/NVIDIA/garak/blob/main/garak/generators/function.py)。
