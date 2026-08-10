# Task 07 完成记录

完成日期：2026-08-10

## 实现摘要

- `GET /api/compare`：2~6 只股票单指标对比；`close/volume/amount` 走 bars 口径（支持复权与周线聚合），其余走指标折回口径；返回 `dates + series[symbol][] + metadata`。
- `templates/compare.html` + `static/js/compare.js`：股票 tag 增删（最多 6 最少 2）、指标分组下拉、粒度/价格/日期、大尺寸折线图（图例点击显隐、toolbox 缩放/保存）、标准化开关（同起点=100）、区间统计表（最新/起始/涨跌幅/最大/最小/均值/标准差）、URL 同步。

## 测试

`test_ui_api.py`：pe_ttm 对比结构、close 完全复权值精确断言、少于 2 只 400。

## 决策

- 区间涨跌幅按起始值非零绝对值计；标准差样本式（n-1）。
