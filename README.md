# OneHistory AI-Ready 项目模板

这是从 `0000-000-Template` 派生的 AI 优先项目模板。它保留 OneHistory 的多专业工程素材，
同时用稳定入口、结构化清单、现行文档和可执行验证规则，让人和 AI 都能快速判断项目目标、
真值、修改边界与完成标准。


## 模板原则

- 根目录只提供必要入口，不要求 AI 扫描整个仓库。
- `project.manifest.json` 声明活动目录、文档、命令、归档和生成物。
- 根目录的可见文件夹只使用 `a-*`、`b-*`、`z-*` 三种级别。
- `b-Office/` 只留一份 `现行约定.md`。模块的指令说明就是注册时的自描述。
- 任何构建、测试或验收命令都必须在 manifest 中明确声明；不适用时使用 `null`。

## 入口

| 入口 | 用途 |
| --- | --- |
| [`AGENTS.md`](./AGENTS.md) | AI 读取顺序、真值规则、工作边界与完成要求 |
| [`project.manifest.json`](./project.manifest.json) | 可机器读取的项目身份、路径、命令和上下文排除项 |
| [`b-Office/现行约定.md`](./b-Office/现行约定.md) | 唯一的长期文档：派生后改成该项目自己的取舍 |

## 从模板建立项目

1. 以本分支建立新的项目分支和工作树，不直接修改本模板。
2. 修改 `project.manifest.json` 中的项目身份、类型、活动目录和命令，并将
   `template.isTemplate` 改为 `false`。
3. 按项目需要创建 `a-*` 子项目、`b-*` 项目组件或 `z-*` 跨项目复用元目录，使用能表达
   职责的名称，并登记到 manifest。
4. 替换 `b-Office/` 中全部 `{{...}}` 占位内容，删除不适用的小节。
5. 更新根 README，使其描述真实项目，而不是模板。
6. 执行严格验收：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\b-Code\Test-ProjectContract.ps1 -Instantiation
```

严格验收通过只代表项目入口和文档合同完整；产品本身仍须执行 manifest 中声明的构建、
测试和验收命令。

## 模板验证

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\b-Code\Test-ProjectContract.ps1
```

作者：Pinavia

## 保留内容

### 学习许可与权利保留

- 本项目允许个人、学校和研究者进行非商业性的学习、研究和引用。
- 未经著作权人书面许可，不得将项目内容用于商业开发、产品制造、商业服务、商业宣传、投标交付、毕业设计成品交付或再授权。
- 不得删除作者、来源、版本历史和第三方许可证声明，不得将本项目或其衍生成果宣称为与作者无关的原创项目。
- 软件源码适用 [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/)；机械模型、PCB、原理图、图片、文档、Unity 工程和其他非软件资料适用 [CC BY-NC-ND 4.0](https://creativecommons.org/licenses/by-nc-nd/4.0/)。
- SE、SW、STEP/STP 及其他三维模型仅允许个人或学校进行非商业学习、评审和研究；未经著作权人书面许可，不得用于加工、打样、制造、销售、量产、投标、产品交付、商业宣传、改作或再授权。
- 第三方组件、模板、元件库、外购件模型和工具仍受其原始许可证约束；本声明不扩大作者对第三方内容的权利。

商业使用、产品化、公司内部长期使用、转让或排他性授权需另行取得著作权人的书面许可。