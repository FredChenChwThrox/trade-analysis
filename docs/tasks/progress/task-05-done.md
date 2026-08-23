# Task 05 完成记录

完成日期：2026-08-10

## 实现摘要

- `GET /api/indicators`：1~6 只股票 × 1~6 个指标，按日期对齐返回 `series[field][date][symbol]`。
- `templates/indicators.html` + `static/js/indicators.js`：股票搜索添加（tag 可删）、粒度/价格口径/日期、分组指标选择器（价格量/均线/MACD/RSI/BOLL/KDJ/量能/估值）、每个指标一个 ECharts 子图（多股彩色折线、缺失值断开、X 轴联动）、CSV 导出（UTF-8 BOM，date + 每指标多股 `|` 分隔）、清空/重置、URL 同步。

## 测试

`test_ui_api.py`：multi indicators 结构、超 6 股 400、字段校验。

## 决策

- CSV 多股同列用 `|` 分隔（避免逗号歧义）；导出列头 = date + fields。
