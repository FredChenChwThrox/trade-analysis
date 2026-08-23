# trade-analysis

个人股票监测系统：Python/SQLite 确定性管线 + LLM 受限参与（只消费底稿、只产 draft）。

一套"估值排期卡"驱动的左侧买入监测框架：确定性 Python 管线负责数据采集、指标计算与信号判定（无未来函数），LLM 仅消费存档事实产出研判 draft，所有规范化数字由管线产生、排期卡 activate/reject 必须人工确认。

## 核心构成

| 模块 | 说明 |
|---|---|
| `scripts/adapters/` | 数据源适配层（通达信 tdx / kimi-datasource / 天眼查 / yahoo），统一 upsert 与 data_revisions 溯源 |
| `scripts/pipeline/` | ingest 入库、daily 例行管线、排期卡 card、财报披露日回填 pit_backfill |
| `scripts/indicators/` | 日线/周线指标（MACD/KDJ/RSI/均线/PE-TTM 点时口径） |
| `scripts/signals/` | 信号层：锚点、五项衰竭信号（恐慌/干涸/三周不新低/底背离/持续时间）、档位触发、右侧确认 |
| `scripts/ui/` | Flask 只读 Web UI（股票池看板） |
| `skills/` | LLM 受限参与的工作流规范（估值排期卡、采集规范、胜率赔率、业绩筛选器） |
| `config/` | watchlist / 交易日历 / 指标与信号参数 |

## 快速开始

```bash
# 环境：uv 管理
uv sync

# 测试（332 项）
uv run pytest -q

# 初始化数据库（SQLite data/market.db：建表 + 导入 watchlist/交易日历种子）
uv run python -m scripts.pipeline.db seed

# Web UI
uv run python -m scripts.ui.app
```

数据说明：行情/公告/财报原始 CSV 与派生数据库（`data/`）不随仓库分发，需自行通过数据源采集后经 `scripts.pipeline.ingest` 入库。

## 硬性约定（设计基线摘录）

- 复权/不复权口径不得跨尺度直接比较（§5.4）
- 数据缺失输出 incomplete/degraded，不猜（§2.5）
- 信号无未来函数；LLM 不产生规范化数字
- 排期卡 activate/reject 必须人工

详见 `docs/system_design.md`（设计基线）与 `docs/database_schema.md`（表结构）。

## 声明

本项目为个人研究用途，不构成任何投资建议。
