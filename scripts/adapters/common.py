"""Source adapter 公共层（设计 §3.1、§8.3）。

- raw 文件登记：raw_objects（路径、请求元数据、content hash、抓取状态）；
  相同 content hash 已成功入库的文件跳过不重复解析（幂等硬门槛 §9.5）。
- 事务上下文：登记 + 解析 + 规范化写入在同一事务中提交；校验失败整批回滚。
- IngestResult：统一的插入/更新/跳过/冲突计数 + incomplete 原因（§2.5）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ADAPTER_VERSION = "0.1.0"

# symbol 后缀 → 市场；市场 → 本地时区（设计 §2.1：时间戳存 UTC，另存来源时区）
SUFFIX_MARKET = {"SH": "CN", "SZ": "CN", "BJ": "CN", "HK": "HK"}
MARKET_TZ = {"CN": "Asia/Shanghai", "HK": "Asia/Hong_Kong"}

_RAW_ROOT = Path(__file__).resolve().parents[2] / "data" / "raw"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def market_of(symbol: str) -> str:
    """从 ticker 后缀推断市场（603605.SH→CN，0700.HK→HK）。"""
    suffix = symbol.rsplit(".", 1)[-1].upper()
    if suffix in SUFFIX_MARKET:
        return SUFFIX_MARKET[suffix]
    raise ValueError(f"无法从 symbol 推断市场: {symbol}")


def market_tz(market: str) -> ZoneInfo:
    return ZoneInfo(MARKET_TZ[market])


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dec_str(value) -> str | None:
    """把数值转成定点十进制字符串（关键决策值存 TEXT 的约定，§9.5）。

    空值/NaN 返回 None；不使用科学计数法。
    """
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:
        pass
    s = str(value).strip()
    if s in ("", "NA", "nan", "None"):
        return None
    d = Decimal(s)
    return format(d, "f")


def parse_iso_utc(ts: str) -> datetime:
    """解析 yahoo 风格 UTC 时间戳（2026-07-06T16:00:00.000Z）。"""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def utc_ts_to_local_date(ts: str, tz: ZoneInfo) -> str:
    """UTC 时间戳 → 市场本地日期（YYYY-MM-DD）。"""
    return parse_iso_utc(ts).astimezone(tz).date().isoformat()


@dataclass
class IngestResult:
    """统一入库结果：插入/更新/跳过/冲突计数 + incomplete 原因（§2.5）。"""

    source: str = ""
    data_type: str = ""
    file_path: str = ""
    raw_object_id: str | None = None
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    conflicts: int = 0
    incomplete_reasons: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        # 校验冲突（数据问题，整批回滚）优先于一般错误（IO/解析问题）
        if self.conflicts:
            return "conflict"
        if self.errors:
            return "error"
        if self.incomplete_reasons:
            return "incomplete"
        return "ok"

    def merge(self, other: "IngestResult") -> "IngestResult":
        self.inserted += other.inserted
        self.updated += other.updated
        self.skipped += other.skipped
        self.conflicts += other.conflicts
        self.incomplete_reasons.extend(other.incomplete_reasons)
        self.errors.extend(other.errors)
        self.notes.extend(other.notes)
        return self

    def summary(self) -> str:
        parts = [
            f"inserted={self.inserted}", f"updated={self.updated}",
            f"skipped={self.skipped}", f"conflicts={self.conflicts}",
        ]
        if self.incomplete_reasons:
            parts.append(f"incomplete={self.incomplete_reasons}")
        if self.errors:
            parts.append(f"errors={self.errors}")
        if self.notes:
            parts.append(f"notes={self.notes}")
        return f"[{self.status}] {self.file_path}: " + ", ".join(parts)


class BatchRejected(Exception):
    """校验失败整批不入库（§3.2）：抛出以触发事务回滚。"""

    def __init__(self, result: IngestResult):
        super().__init__("; ".join(result.errors) or "batch rejected")
        self.result = result


def _find_meta(path: Path) -> dict | None:
    """同目录 _meta.json（stock-collect 落盘约定），不存在则返回 None。"""
    meta = path.parent / "_meta.json"
    if meta.exists():
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def register_and_parse(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    source: str,
    data_type: str,
    symbol: str | None,
    parse,
    request_params: dict | None = None,
    run_id: str | None = None,
) -> IngestResult:
    """登记 raw 文件并解析入库，**不做事务管理**（调用方负责提交/回滚）。

    - content hash 已在 raw_objects 且 fetch_status=ok → 跳过不重复解析（§8.3）；
    - parse 结果含冲突/错误 → 抛 BatchRejected，由调用方决定回滚范围
      （ingest_file 单文件回滚；daily pipeline 则回滚该股当日全部阶段）。

    parse 签名：parse(conn, Path, raw_object_id, IngestResult) -> IngestResult
    """
    path = Path(path)
    result = IngestResult(source=source, data_type=data_type, file_path=str(path))

    if not path.exists():
        result.errors.append(f"文件不存在: {path}")
        return result

    content_hash = sha256_file(path)
    row = conn.execute(
        "SELECT raw_object_id FROM raw_objects WHERE content_hash = ? AND fetch_status = 'ok'",
        (content_hash,),
    ).fetchone()
    if row is not None:
        result.raw_object_id = row["raw_object_id"]
        result.skipped = 1
        result.notes.append(f"content_hash 已登记（{row['raw_object_id']}），跳过重复解析")
        return result

    raw_object_id = f"raw_{content_hash[:16]}"
    result.raw_object_id = raw_object_id

    if request_params is None:
        meta = _find_meta(path)
        request_params = meta if meta is not None else {"inferred_from": str(path)}
    if run_id is None:
        # 路径约定 data/raw/{source}/{data_type}/{date}/{run_id}/file.csv
        parts = path.parts
        run_id = parts[-2] if len(parts) >= 2 else None

    conn.execute(
        """
        INSERT INTO raw_objects (raw_object_id, run_id, source, data_type, symbol,
                                 request_params_json, file_path, content_hash,
                                 fetch_status, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?)
        """,
        (
            raw_object_id, run_id, source, data_type, symbol,
            json.dumps(request_params, ensure_ascii=False, default=str),
            str(path), content_hash, utc_now(),
        ),
    )
    result = parse(conn, path, raw_object_id, result)
    if result.conflicts or result.errors:
        raise BatchRejected(result)
    return result


def ingest_file(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    source: str,
    data_type: str,
    symbol: str | None,
    parse,
    request_params: dict | None = None,
    run_id: str | None = None,
) -> IngestResult:
    """登记 raw 文件并在同一事务中执行解析入库（单文件原子）。

    - parse 抛 BatchRejected（校验冲突/错误）→ 整批回滚，raw_objects 也不登记；
    - parse 正常返回（含行级 skip / incomplete 标记）→ 提交。
    """
    try:
        with conn:  # 正常返回提交；BatchRejected 触发回滚
            return register_and_parse(
                conn, path, source=source, data_type=data_type, symbol=symbol,
                parse=parse, request_params=request_params, run_id=run_id,
            )
    except BatchRejected as reject:
        result = reject.result
        result.notes.append("校验失败，整批回滚未入库")
        return result


def record_revision(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    record_key: dict,
    old_value,
    new_value,
    source: str,
    reason: str,
    run_id: str | None,
) -> None:
    """规范化事实内容变化时写 data_revisions（§9.5 降级：记录前后值即可）。"""
    conn.execute(
        """
        INSERT INTO data_revisions (table_name, record_key_json, field_name,
                                    old_value, new_value, source, reason, run_id, created_at)
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            table_name,
            json.dumps(record_key, ensure_ascii=False),
            json.dumps(old_value, ensure_ascii=False, default=str),
            json.dumps(new_value, ensure_ascii=False, default=str),
            source, reason, run_id, utc_now(),
        ),
    )


# ---------------------------------------------------------------- 代码映射与交易日推进（多 adapter 共用）

# setcode → symbol 后缀（个股）；与 SUFFIX_MARKET 对齐。
# 62=中证指数系统内统一 .SH；32=港股指数。INDEX_SETCODES 归属各源（tdx kline/index 路由专用）。
SETCODE_SUFFIX = {
    "1": "SH",    # 沪市 A 股
    "0": "SZ",    # 深市 A 股
    "2": "BJ",    # 北交所
    "31": "HK",   # 港股
    "62": "SH",   # 中证指数（000300 沪深300）
    "32": "HK",   # 港股指数
}


def symbol_from_code_setcode(code: str, setcode: str) -> str:
    """code + setcode → 系统 symbol（带后缀，如 603605.SH / 00700.HK）。"""
    suffix = SETCODE_SUFFIX.get(str(setcode))
    if suffix is None:
        raise ValueError(f"未知 setcode={setcode}，无法推断 symbol 后缀")
    return f"{code}.{suffix}"


def next_open_available_at(calendar: dict, pub_date: str, market: str) -> str:
    """下一个开市交易日 00:00（本地）→ UTC ISO（§2.1 保守时点）。

    calendar 为空/缺日时退化为发布日 +1 自然日（降级，调用方应另行
    通过 incomplete_reasons 标注）。
    """
    tz = market_tz(market)
    d = datetime.fromisoformat(pub_date).date() + timedelta(days=1)
    if calendar:
        while d.isoformat() not in calendar or not calendar[d.isoformat()]["is_open"]:
            d += timedelta(days=1)
            if (d - datetime.fromisoformat(pub_date).date()).days > 40:
                break
    return datetime(d.year, d.month, d.day,
                    tzinfo=tz).astimezone(timezone.utc).isoformat()


def load_calendar(conn: sqlite3.Connection, market: str) -> dict[str, sqlite3.Row]:
    """某市场全部日历行 {trade_date: row}；空 dict 表示该市场日历缺失（incomplete）。"""
    rows = conn.execute(
        "SELECT * FROM trading_calendar WHERE market = ?", (market,)
    ).fetchall()
    return {r["trade_date"]: r for r in rows}
