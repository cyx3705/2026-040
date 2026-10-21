# Echo

> 说不连贯英语也能和 AI 聊：AI 每个词带释义，你每个词有 AI 帮你接

## 定位

Echo 是一个本地部署的英语对话练习应用，面向英语说不连贯的人。名字取自只能借别人的话说话的厄科：
从 AI 给出的词里挑着说，也是在说自己的话。

- 托管在 OneHistory（编号、仓库、站点、文档规则照旧），但**不做成宿主模块**：不接 Vulcan/Aurora，独立运行。
- 全部推理在本机完成，不依赖云端模型。

## 概况

| 项 | 值 |
| --- | --- |
| 编号 | `2026-040` |
| 角色 | 独立应用（不是宿主模块） |
| 状态 | 开发中：最简单的英语对话已能跑 |
| 推理 | 自带的 Ollama 私有实例（不依赖本机 Ollama 服务），默认 `qwen3:8b` |
| 词典 | [ECDICT](https://github.com/skywind3000/ECDICT)（MIT），SQLite 离线查 |
| 界面 | Python 后端（uv 管依赖）+ 本地单页网页 |

## 能力

| 能力 | 说明 |
| --- | --- |
| 英语对话（已有） | 本地模型用简单英语流式回复，并接着提问把话题聊下去 |
| 逐词释义（待做） | AI 回复里每个英文词下方标词典释义，点开看完整词条 |
| 逐词辅助造句（待做） | 每说完一个词（空格结束）跑一次前向，列出 5 个最可能的下一个词；点选或手打都行 |

## 入口

| 入口 | 用途 |
| --- | --- |
| [`AGENTS.md`](./AGENTS.md) | AI 读取顺序、工作边界与完成要求 |
| [`project.manifest.json`](./project.manifest.json) | 项目身份、目录、命令和外部依赖 |
| [`b-Office/现行约定.md`](./b-Office/现行约定.md) | 唯一的长期文档：取舍、真机实测与已知缺口 |

## 目录

| 目录 | 职责 |
| --- | --- |
| `b-Office/` | 现行约定 |
| `b-Code/` | 项目维护工具（合同检查） |
| `b-Code-Echo/` | 应用本体：Python 包 `echo_app`（引擎管理、对话接口、页面） |

引擎、模型和引擎日志放在 `%LOCALAPPDATA%\Echo\`，不进仓。

## 构建与验证

需要 [uv](https://docs.astral.sh/uv/)。首次运行先装引擎和模型，之后直接启动：

```powershell
uv run --project b-Code-Echo echo-setup
uv run --project b-Code-Echo echo-serve
```

启动后浏览器打开 `http://127.0.0.1:8040`。还没有自动测试；合同检查：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\b-Code\Test-ProjectContract.ps1 -Instantiation
```

## 开发与发布

尚未定。不走宿主模块的 dev 管线与发布表。

## 要点

- 候选词以整词为单位：模型给的是 token，半截候选要补全到空格或标点为止。
- 预测用户输入靠续写 user 回合（`raw` 模式），生成 AI 回复走 `/api/chat` 并关思考模式。

## 保留内容

### 学习许可与权利保留

- 本项目允许个人、学校和研究者进行非商业性的学习、研究和引用。
- 未经著作权人书面许可，不得将项目内容用于商业开发、产品制造、商业服务、商业宣传、投标交付、毕业设计成品交付或再授权。
- 不得删除作者、来源、版本历史和第三方许可证声明，不得将本项目或其衍生成果宣称为与作者无关的原创项目。
- 软件源码适用 [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/)；机械模型、PCB、原理图、图片、文档、Unity 工程和其他非软件资料适用 [CC BY-NC-ND 4.0](https://creativecommons.org/licenses/by-nc-nd/4.0/)。
- SE、SW、STEP/STP 及其他三维模型仅允许个人或学校进行非商业学习、评审和研究；未经著作权人书面许可，不得用于加工、打样、制造、销售、量产、投标、产品交付、商业宣传、改作或再授权。
- 第三方组件、模板、元件库、外购件模型和工具仍受其原始许可证约束；本声明不扩大作者对第三方内容的权利。

商业使用、产品化、公司内部长期使用、转让或排他性授权需另行取得著作权人的书面许可。