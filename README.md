# Echo

> 说不连贯英语也能和 AI 聊：AI 每个词带释义，你每个词有 AI 帮你接。

Echo（2026-040）是 HistoryVulcan 的宿主模块（域 `echo`）。界面在 Aurora 里：左边是会话历史，右边是气泡对话和发送栏。宿主在跑时，`http://127.0.0.1:8040` 也在跑，浏览器扩展连它，原来的网页也还能打开（输入预测、中文输入、点词学会在网页上）。推理一律经 Ratio 模块的指令（`provider=local`），Echo 不直连任何推理服务、不管模型进程。

## 能力与当前边界

- Aurora 页面（`echo-history` / `echo-chat`）：会话卡片带搜索、新对话、删除；对话气泡边生成边显示，AI 回复按网页端逐词上下排版（英文在上、中文释义在下）；可选难度、可停止。指令 `echo.conv.list/show/new/delete`、`echo.chat.send/stop/level`，AI 也能直接调。
- 英语对话、难度 A1～C2+/国内考试/雅思档位、回答长度、对话历史与搜索、自动中文标题、Word 单词表练词。
- 对话流式输出英文；每说完一个词就选取中文释义。服务支持共享 KV 时，A 在服务里持续生成，每个词在 A 上分叉 B 选义（`ratio.seq.*`），否则说完后用 JSON Schema 约束的普通请求逐词选义。每轮最多 8 个多义词，超出或失败保留带退化标记的词典释义。断流未完成时不保存半截回复。
- 输入预测按用户回合读取下一 token 前 20 个候选（`ratio.local.probs`），经词典过滤/去重、半词补全后显示 1～20 个候选；45 秒等不到算超时。
- Ctrl 中文输入由自有 Windows EDIT/IMM 输入桥（C++，源码随模块嵌入）承接；空格确认完整中文词后，从词典英文候选里用一次受限标签选出整词。
- 统一浏览器扩展提供网页、YouTube 和 Shorts 逐词伴读；接口与原 Python 版完全相同，扩展不用改。页面/视频的近期文本上下文写入数据目录 `reader-context/`。

## 安装与运行

- 前提：HistoryVulcan 宿主 6.0+、Ratio 0.8.0+（`dependsOn`）、本机推理服务（`ratio.local.start`）。
- 首次准备离线词典：控制台执行 `echo.dict.setup`（ECDICT，分段续传）。中文输入桥需 VS C++ Build Tools x64 + Windows SDK，首次使用时自动编译，也可先执行 `echo.input.build`。
- 打开页面：进入 Aurora 的 HistoryEcho 场景。8040 仅由浏览器扩展使用，Echo 不再提供独立网页入口。`echo.app.status` 看扩展服务、数据位置与推理状态；`echo.app.stop` / `echo.app.start` 手动停起。
- 数据：学习数据（学会的词、对话、单词表、设置）在仓内 `z-学习数据/user.db`（`ECHO_USER_DB` 可改）；可重建数据（词典、输入桥构建、伴读上下文）在 `ECHO_DATA_DIR`，缺省 `%USERPROFILE%\.echo`。端口 `ECHO_WEB_PORT`，`ECHO_AUTOSTART=0` 时宿主启动不自动起网页服务。

## 浏览器扩展与多设备

在 Edge/Chrome 扩展管理页开启开发者模式，加载 `b-Code-Reader/extension`。扩展连 Echo 的 8040 端口，Echo 再经 Ratio 调推理。网页只发送可见段落文字，不发送页面 URL、Cookie、表单或 HTML。

多设备共享一台 Echo 及同一学习库：主机执行 `echo.app.start share=true`（需要监听全部地址的 URL 权限），扩展填写主机地址与数据目录 `extension-key.txt` 里的密钥（或 `ECHO_SHARE_KEY`）。远端只开放状态/伴读/学习四个接口。

## 项目入口与验证

| 路径 | 职责 |
| --- | --- |
| [project.manifest.json](project.manifest.json) | 身份、活动目录、命令、依赖与发布描述 |
| [AGENTS.md](AGENTS.md) | AI 工作合同 |
| [现行约定](b-Office/现行约定.md) | 当前规则与历史验收 |
| `b-Code/Echo/` | 模块：指令、HTTP 服务、对话/预测/伴读、词典与学习库、网页（static/）、输入桥源码（kernel/） |
| `b-Code/Echo.Tests/` | 离线测试（假推理 + 临时库 + 真 HTTP 服务 + 宿主 `--probe` 装载）与页面脚本测试 `ui/` |
| `b-Code-Reader/`、`b-Code-YouTube/` | MV3 扩展与其浏览器测试 |

改动走宿主模块开发管线（`vulcan.dev.start/submit/finish`）。`b-Office/technical-report/` 在独立分支 `technical-report` 上发展，记录的是旧原生共享 KV 实验。

## 保留内容

### logo与作者

- 作者：pinavia
- ![logo](./Logo.png)

### 学习许可与权利保留

- 本项目允许个人、学校和研究者进行非商业性的学习、研究和引用。
- 未经著作权人书面许可，不得将项目内容用于商业开发、产品制造、商业服务、商业宣传、投标交付、毕业设计成品交付或再授权。
- 不得删除作者、来源、版本历史和第三方许可证声明，不得将本项目或其衍生成果宣称为与作者无关的原创项目。
- 软件源码适用 [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/)；机械模型、PCB、原理图、图片、文档、Unity 工程和其他非软件资料适用 [CC BY-NC-ND 4.0](https://creativecommons.org/licenses/by-nc-nd/4.0/)。
- SE、SW、STEP/STP 及其他三维模型仅允许个人或学校进行非商业学习、评审和研究；未经著作权人书面许可，不得用于加工、打样、制造、销售、量产、投标、产品交付、商业宣传、改作或再授权。
- 第三方组件、模板、元件库、外购件模型和工具仍受其原始许可证约束；本声明不扩大作者对第三方内容的权利。

商业使用、产品化、公司内部长期使用、转让或排他性授权需另行取得著作权人的书面许可。
