# AIReady 项目工具

本目录是 AIReady 模板自身的 b 级代码组件，保存项目合同检查等可重复执行的维护工具。
它不承载派生项目的业务源码；派生项目可按实际组件建立 `b-Code-<组件名>/`。

`Test-ProjectContract.ps1` 验证 manifest、活动路径、a/b/z 根目录规则、现行文档、本地 Markdown
链接和实例化占位符。脚本只读检查仓库，不提交、推送、发布或修改外部系统。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\b-Code\Test-ProjectContract.ps1
```

派生项目首次启用时运行严格模式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\b-Code\Test-ProjectContract.ps1 -Instantiation
```
