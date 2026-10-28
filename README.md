# Echo

> 说不连贯英语也能和 AI 聊：AI 每个词带释义，你每个词有 AI 帮你接。

Echo（2026-040，应用版本 0.3.0）是独立的英语练习应用，不接 Vulcan/Aurora、不注册宿主指令。

2026-10-28 起，Echo **只消费外部推理服务**（EXT-001）。默认连接本机统一工作区提供的 WSL 服务；Echo 不再启动 Ollama、加载本地模型、导入模型文件、升级引擎或启动原生 KV 适配器。模型与其他客户端的资源分配全部由外部服务负责。

## 能力与当前边界

- 英语对话、难度 A1～C2+/国内考试/雅思档位、回答长度、对话历史与搜索、自动中文标题、Word 单词表练词继续保留。
- 对话流式输出英文；每确认一个完整词，就立即选取并显示中文释义。支持 ECHO-KV-001 的服务保留真实 A 序列，通过一次服务操作完成当前词前缀匹配、B 分叉和单 token 选义，选完立即释放；其他服务使用结构化请求。每轮最多处理 8 个多义词，超出或失败保留带退化标记的词典释义。断流未完成时不保存半截回复。
- 输入预测仍按用户回合读取下一 token 的前 20 个候选，经过词典过滤/去重、半词补全后显示 1～20 个候选；相同模型、相同提示和候选上限可复用 Echo 最近一份排名。相邻不同提示的 KV 复用由服务决定，不承诺旧 C++ 内核的延迟。
- 预测超过 1.5 秒显示等待服务；总等待最多 45 秒（配置的推理超时更短时从其值），包含本地与服务排队。超时或网页断开取消旧请求，失败可点「重试」，没有候选会明确提示继续输入（PRED-003）。
- Ctrl 中文输入仍由独立 Windows EDIT/IMM 输入桥承接；普通空格确认完整中文词后，从词典英文候选中选择整词，失败保留中文。共享 KV 服务使用一个受限单 token 标签判别；普通服务使用受限 JSON。
- 统一浏览器扩展继续提供网页、YouTube 和 Shorts 逐词伴读、上下双行字幕、英文轨道读取、竖屏自动开启字幕、时间轴提前处理与视频切换取消。扩展协议不变，只加载 `b-Code-Reader/extension`（0.7.2）。
- 网页/字幕先显示词典释义，再按每批最多 12 个目标选义。共享 KV 服务只预填一份完整段落 A，各目标顺序分叉 B 并判别；普通服务使用 JSON Schema。缺词由模型按上下文补译并标注来源。标熟和恢复已有释义只改显示；学习记录仍在同一 `user.db`，其他设备约 15 秒同步。
- 断线后保留已完成释义，重试中断任务；字幕完成结果继续按视频保存在扩展中。活动 Shorts 绑定、滚回复用、动态网页保护及连接授权沿用现有实现。
- 页面/视频的近期**文本上下文**写入数据目录 `reader-context/`，每份最多 64 段、约 8000 字符，最多 128 份；纯已学会请求不创建模型任务。Echo 不读写 `reader-kv/` 模型快照；外部服务管理 KV/磁盘缓存。ECHO-KV-001 恢复了真实位置分叉，当前 B 顺序执行；双 B 同批与返回页面免预填仍未恢复。
- Echo 每轮最多保留一个 A 和一个临时 B；统一服务使用有界两槽调度，支持 ECHO-KV-004 时，A 使用一条持续 SSE 连接生成，下一步在服务内优先调度；旧服务保留逐步请求兼容。后台快照在任一公共槽或 A/B 租用期间推迟。页面“并发 1/2”仍表示 Echo 同时发出的推理 HTTP 请求上限，不加载第二份模型；其他客户端占用服务时可能排队。

ECHO-KV-004 将主 KV 生成改为持续 SSE，免除每 token 的 Windows／WSL HTTP 往返，并在服务内优先调度 A。当前 8B 同提示 64 token、3 轮中位数：经入口纯生成约 13.8→65.2 token/s，加入 8 次选义约 15.2→31.7 token/s；各路径正文摘要一致。选义仍消耗模型计算，详细验收及限制见现行约定。

## 安装与运行

Windows x64、Python ≥3.12、uv。首次只需准备离线词典：

```powershell
uv run --project b-Code-Echo echo-setup
```

`echo-setup` 只下载 ECDICT 并构建 SQLite，不下载任何引擎或模型。已有词典时直接复用。Ctrl 中文输入桥仍需 Visual Studio C++ Build Tools x64 和 Windows SDK；首次使用会自动编译本项目的 Windows 辅助程序，不加载模型。也可提前运行 manifest 的 `build` 命令。

先由统一工作区自行保持推理服务运行，再运行：

```powershell
$env:ECHO_INFERENCE_URL = 'http://127.0.0.1:11435/v1'
$env:ECHO_MODEL = 'qwen3.5:35b-a3b-q4_K_M'
uv run --project b-Code-Echo echo-serve
```

Echo 默认打开 `http://127.0.0.1:8040`。`--no-browser` 不打开浏览器；再次启动只打开已运行页面。关闭 Echo 只关闭应用自己的 HTTP 客户端和输入桥，外部推理服务继续运行。

旧用户先确认 `ECHO_DATA_DIR` 指向迁移后的现有数据目录，保留 `dict/ecdict.db` 和 `user.db`；不要在失效旧路径启动一个空库。显式指定目录不存在时 Echo 拒绝启动并提示修正。默认数据目录仍为用户目录的 `.echo`，不自动搜索或迁移数据，本仓不保存机器路径和密钥。

| 环境变量 | 用途 |
| --- | --- |
| `ECHO_INFERENCE_URL` | OpenAI 风格接口根地址，默认 `http://127.0.0.1:11435/v1` |
| `ECHO_COMPLETION_URL` | llama-server 原始下一词概率接口；默认上述地址去掉末尾 `/v1` 后加 `/completion`。设为空时不提供输入预测 |
| `ECHO_INFERENCE_API_KEY` | 可选 Bearer 密钥；仅从环境读取，不返回页面、不写入仓库 |
| `ECHO_MODEL` | 当前模型 ID；显式设置时优先于旧库选择；否则保留仍可用的旧选择，失效时改选服务现有模型 |
| `ECHO_INFERENCE_TIMEOUT` | 推理 HTTP 超时秒数，默认 120 |
| `ECHO_DATA_DIR` | Echo 词典、学习/对话库、文本上下文和输入桥产物目录 |
| `ECHO_WEB_PORT` / `ECHO_WEB_HOST` | Echo 自身监听地址，默认 `127.0.0.1:8040` |

服务需提供 `GET /v1/models` 和流式/非流式 `POST /v1/chat/completions`，选义需支持 `response_format` JSON Schema。输入预测另需 llama-server 扩展 `/completion` 的 `n_predict=1`、`n_probs` 和 `completion_probabilities[].top_logprobs`，不是所有 OpenAI 兼容服务都有。Qwen/Gemma 用户回合模板支持输入预测；未知模型仍可对话，但预测明确报不支持，不伪装成另一模型的模板。

点击顶栏「更换模型」查看服务模型列表。支持 ECHO-MODEL-001 的统一服务列出已下载模型和不可用原因；选择后由服务等待现有请求结束、保存缓存、释放旧模型、加载新模型，再尝试恢复该模型的兼容缓存。前端显示进度，切换时暂停发送和预测，失败时服务恢复原模型。此操作切换共用服务，其他客户端也会受到影响。Echo 仅调用外部 HTTP，不启动推理进程或加载权重；普通 OpenAI 服务仍只选择它已公布的模型 ID。未登记、下载不完整或没有显存配置的模型不可选，不自动下载。

`echo-upgrade`、`echo-kv-adapter` 已移除安装入口；旧源码中的原生适配器主入口也明确拒绝启动。原生 C++/KV 源码仅保留用于历史研究和输入桥构建所需的既有工具，不属于当前推理路径。

统一服务当前支持 35B、Qwen3 8B 和 Gemma4 12B 的模型切换。两个 Qwen 使用共享 KV；Gemma4 的滑动窗口检查点尚未适配，使用普通对话/概率与结构化选义，能力探测明确报告原生分叉不支持。27B 尚无本机显存配置，菜单禁用并说明原因。

## 浏览器扩展与多设备

在 Edge/Chrome 扩展管理页开启开发者模式，加载 `b-Code-Reader/extension`。已有安装重新加载扩展并刷新目标网页；停用旧独立 YouTube 插件，避免重复字幕。扩展连接 Echo 的 8040 端口，Echo 再连接推理服务；无需修改扩展为直连模型服务。

弹窗可选择所有网页默认开启、单网站自动开启或本页临时开启。授予所需网站权限后生效；关闭并还原本页不会撤销持久设置。普通点击链接照常打开，Alt+点击链接中的词切换标熟；非链接词直接点击切换。网页只发送可见段落文字，不发送页面 URL、Cookie、表单或 HTML；跳过编辑区、代码、iframe 等。字幕仍需视频提供可读英文轨道，不能识别烧录在画面中的文字。

多设备仍只共享一台 Echo 及同一学习库：主机运行 `echo-serve --share`，扩展填写 Echo 主机地址及数据目录 `extension-key.txt` 中的密钥。共享模式默认监听 `0.0.0.0:8040`，访问密钥首次生成后复用，也可用 `ECHO_SHARE_KEY` 指定至少 32 位密钥。远程仅开放状态/伴读/学习接口；模型选择、对话管理和系统输入法仍只供主机本地使用。不需要向其他设备开放模型端口。本项目不配置公网部署、防火墙或 VPN。

## 项目入口与验证

| 路径 | 职责 |
| --- | --- |
| [project.manifest.json](project.manifest.json) | 身份、活动目录、命令和依赖 |
| [AGENTS.md](AGENTS.md) | AI 工作合同 |
| [现行约定](b-Office/现行约定.md) | 当前规则与明确标注的历史验收 |
| `b-Code-Echo/` | Python 业务/HTTP、外部服务客户端、词典和学习库、网页及 Windows 输入桥 |
| `b-Code-Reader/` | 唯一 MV3 扩展与网页/连接/恢复测试 |
| `b-Code-YouTube/` | YouTube/Shorts 专项测试 |
| `b-Code/` | 仓库合同检查 |

```powershell
uv run --project b-Code-Echo python -m unittest discover -s b-Code-Echo/tests
node b-Code-Echo/tests/suggest_queue.cjs
node b-Code-Echo/tests/composer_input.cjs
node b-Code-Echo/tests/model_switch_browser.cjs
powershell -NoProfile -ExecutionPolicy Bypass -File .\b-Code\Test-ProjectContract.ps1 -Instantiation
```

EXT-001～004 验收使用内存 HTTP 模拟服务和临时用户库，不连接正在运行的推理服务。测试覆盖协议、取消、断流、模型选择、输入预测、受限选义、上下文边界及退化。实机速度、共用服务排队及真实 WSL 端到端另验，不把旧原生运行时的性能结果套用于新版本。其余浏览器测试命令见 manifest，带 `Live` 的测试才依赖实际运行的 Echo。

`b-Office/technical-report/` 保留旧版原生共享 KV 实验的英文报告及历史数据解释，`reportBuild` 只构建文稿，不表示当前外部服务架构已达到旧版性能或能力。无自动发布、提交、推送或部署。

## 统一服务适配验收

ECHO-SVC-003：统一服务 11435 入口已改为 C++ HTTP 服务，转发到同一 C++ llama-server。Echo 的聊天、原始预测、共享 KV 和 SSE 不经过 Python HTTP 代理，也不再在每次请求前调用模板渲染、分词和磁盘分支匹配；模型自身的提示缓存、后台快照和启动恢复继续由服务管理。C++ 保留等待响应头阶段的取消、上游状态/错误原文、有界流缓冲和模型管理接口；能力按实际模型探测。模型切换与磁盘快照的低频管理脚本仍使用 Python，Echo 业务层也保持 Python，不属于逐 token 推理路径。

同一 8B、相同工作量、交替 8 轮的热请求中位数：旧 Python / 新 C++ 入口的预测约 221.7 / 21.5ms，A 下一 token 27.5 / 23.3ms，短选义分叉 40.4 / 37.9ms，普通 SSE 首正文 29.0 / 22.6ms。预测收益主要来自取消每请求的额外缓存匹配；这些是热请求样例，不是长上下文或共用负载下的延迟保证。完整报告保留在服务工作区 `status/echo-cpp-proxy-benchmark.json`。

ECHO-SVC-001/002 为此前 Python 代理阶段的缓存统计与取消适配记录；新入口不更新旧代理的文本前缀计数和逐请求磁盘指标，`/cache/stats` 明确报告 C++ 实现及后台快照状态。`GET /v1/echo/capabilities` 仍是不触发推理的只读能力说明，`X-Local-AI-Client: echo` 不改变授权。

manifest 的 `testExternalLive` 会向运行中的服务发起真实请求，使用临时用户库验收预测、中文转英文、网页选义、流式对话保存和自动标题；先把当前进程的 `ECHO_DATA_DIR` 指向现有词典所在目录。该测试不操作服务生命周期，也不写现有学习库。

ECHO-KV-001：`testSharedKVLive` 额外要求服务实际支持 `/v1/kv/op`，网页选义报告 `decision_steps=1` 和真实共享前缀，完成后 A/B 为零、分叉与释放计数相等。`/api/status` 显示实际探测结果。推理仍全部发生在外部同一模型进程；Echo 不加载第二份模型。混合模型分叉同时共享注意力 KV、恢复目标之前的循环检查点，再重放不足部分；`shared_prefix_tokens` 与 `replayed_tokens` 分开报告。

当前服务每次租用一份 A、一个临时 B，A 上限 8192 token、B 后缀上限 2048 token，最多保留 8 份循环检查点，闲置约 90 秒回收。分支标签必须是模型的独立单 token；Qwen3.5 的 `0` 不加前导空格。长提示和资源失败会按业务退化，不把 prompt cache 或磁盘恢复称为位置分叉。数值比较不能直接套用旧模型性能：Q8 KV、MoE 和不同计算批次可能产生 logits 差异，服务实机验收另检查循环状态恢复与 A 隔离。

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
