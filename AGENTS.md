# AGENTS.md

个人股票监测系统：Python/SQLite 确定性管线 + LLM 受限参与（只消费底稿、只产 draft）。

- **交接入口**：先读 `docs/handoff.md`（环境/命令/约定/当前状态），设计基线 `docs/system_design.md`，执行历史 `docs/execution_log.md`。
- **环境**：uv 管理（命令一律 `uv run`），包下载失败走系统代理；测试 `uv run pytest -q`，全绿才算完成。
- **硬性约定**：复权/不复权口径不得跨尺度直接比较（§5.4，比较前 ÷ 当日因子折回）；数据缺失输出 incomplete/degraded 不猜（§2.5）；信号无未来函数；LLM 不产生规范化数字；排期卡 activate/reject 必须人工。
- **文档纪律**：每次改动追加 `docs/execution_log.md`；设计变更同步 `docs/system_design.md`；config/signals.yaml 中带 ⚠️ 的参数在人工核对期内不得擅自调整。
- **每日例行**：采集增量 CSV 后 `uv run python -m scripts.pipeline.daily --date <交易日> --raw-dir <目录>`。
