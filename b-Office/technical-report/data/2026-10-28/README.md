# REPORT-001 原始测量（2026-10-28）

技术报告第 5 节的全部数字均取自本目录。被测应用代码为提交 `143bec4`；基准脚本为 `technical-report` 分支上的 `b-Code-Echo/tests/`，`api_comparison.py` 在该分支中增加了候选词记录与 hit@k。

| 文件 | 来源 | 报告位置 |
| --- | --- | --- |
| `native.json` | `api_comparison.py --mode native` | 表 1、表 2 运行时列、§5.2 拆分、图 1 候选复现 |
| `api.json` | `api_comparison.py --mode api`（Echo 关闭，`ECHO_KV_ADAPTER_URL=""`） | 表 1、表 2 API 两列 |
| `reader.json` | `native_reader_acceptance.py` | 表 3 |
| `acceptance.jsonl` | `native_acceptance.py --cycles 1000`（每行一个 JSON） | §5.4 缓存与冷计算对比、§5.5 生命周期与隔离 |

运行条件：
- 机器：RTX 4080 Laptop GPU（12 GB，驱动 610.62），32 GB 内存，Windows 10。
- 软件：Echo 私有引擎 Ollama 0.40.7，`qwen3:8b`（Q4_K_M），数据目录 `ECHO_DATA_DIR=F:\LinuxModuleServices\echo-data`（学会词 632 个）。
- 运行前停止了 WSL 统一模型服务，除桌面程序约 1.3 GB 外无其他显存占用。
- 运行顺序：原生基准 14:06，网页 14:07，生命周期 14:08，API 基准 14:09–14:12。
