"""CSV analysis tools using only the standard library (pandas is optional)."""
import csv
import io
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from ..base import ToolContext, ToolError, ToolRegistry

MISSING = {"", "na", "n/a", "nan", "null", "none", "-", "--", "?"}
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def _to_number(value: str) -> Optional[float]:
    text = value.strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def _is_date(value: str) -> bool:
    text = value.strip()
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(text, fmt)
            return True
        except ValueError:
            continue
    return False


def _load_table(ctx: ToolContext, path: str, delimiter: Optional[str], max_rows: int = 200_000):
    target = ctx.resolve_path(path, must_exist=True)
    if not target.is_file():
        raise ToolError(f"'{path}' is not a file")
    if target.stat().st_size > 50_000_000:
        raise ToolError("CSV larger than 50 MB; use run_python with chunked processing")
    raw = target.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:20000]
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        raise ToolError("CSV file is empty")
    header = [h.strip() or f"column_{i + 1}" for i, h in enumerate(header)]
    rows: list[list[str]] = []
    for row in reader:
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        rows.append(row[: len(header)])
        if len(rows) >= max_rows:
            break
    return target, header, rows, delimiter


def _profile_columns(header: list[str], rows: list[list[str]]) -> list[dict[str, Any]]:
    profile = []
    for idx, name in enumerate(header):
        values = [row[idx] for row in rows]
        non_missing = [v for v in values if v.strip().lower() not in MISSING]
        numbers = [_to_number(v) for v in non_missing]
        numeric_count = sum(1 for n in numbers if n is not None)
        if non_missing and numeric_count == len(non_missing):
            dtype = "integer" if all(float(n).is_integer() for n in numbers if n is not None) else "float"
        elif non_missing and sum(1 for v in non_missing[:200] if _is_date(v)) >= 0.9 * min(len(non_missing), 200):
            dtype = "date"
        elif non_missing and set(v.strip().lower() for v in non_missing) <= {"true", "false", "yes", "no", "0", "1"}:
            dtype = "boolean"
        else:
            dtype = "text"
        unique = len(set(non_missing))
        entry: dict[str, Any] = {
            "name": name,
            "type": dtype,
            "missing": len(values) - len(non_missing),
            "unique": unique,
        }
        if dtype == "text" and non_missing:
            entry["top_values"] = [
                {"value": v, "count": c} for v, c in Counter(non_missing).most_common(5)
            ]
        if dtype in {"integer", "float"} and numeric_count:
            nums = [n for n in numbers if n is not None]
            entry["min"], entry["max"] = min(nums), max(nums)
        profile.append(entry)
    return profile


def _describe(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    out: dict[str, Any] = {
        "count": len(values),
        "sum": round(sum(values), 4),
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "min": min(values),
        "max": max(values),
    }
    if len(values) > 1:
        out["stdev"] = round(statistics.stdev(values), 4)
        sorted_vals = sorted(values)
        out["p25"] = sorted_vals[int(0.25 * (len(values) - 1))]
        out["p75"] = sorted_vals[int(0.75 * (len(values) - 1))]
    return out


class ReadCsvParams(BaseModel):
    path: str = Field(..., description="CSV file inside the workspace.")
    max_rows: int = Field(10, ge=1, le=200, description="Sample rows to return.")
    delimiter: Optional[str] = Field(None, description="Delimiter; auto-detected when omitted.")


class StatisticsParams(BaseModel):
    path: str = Field(..., description="CSV file inside the workspace.")
    columns: Optional[list[str]] = Field(None, description="Numeric columns to analyse (default: all numeric).")
    group_by: Optional[str] = Field(None, description="Optional categorical column to aggregate by.")
    aggregate: Literal["sum", "mean", "count", "min", "max"] = Field(
        "sum", description="Aggregation used when group_by is set."
    )
    top_n: int = Field(20, ge=1, le=200, description="Max groups to return (sorted by aggregate desc).")
    delimiter: Optional[str] = Field(None, description="Delimiter; auto-detected when omitted.")


class ChartParams(BaseModel):
    path: str = Field(..., description="CSV file inside the workspace.")
    x: str = Field(..., description="Column for the x axis / categories.")
    y: str = Field(..., description="Numeric column to plot.")
    kind: Literal["bar", "line", "pie"] = "bar"
    aggregate: Literal["sum", "mean", "count"] = "sum"
    output: str = Field("reports/chart.png", description="Output PNG path inside the workspace.")
    title: Optional[str] = None
    top_n: int = Field(15, ge=1, le=100)


def _aggregate(values: list[float], how: str) -> float:
    if how == "count":
        return float(len(values))
    if not values:
        return 0.0
    if how == "sum":
        return sum(values)
    if how == "mean":
        return statistics.fmean(values)
    if how == "min":
        return min(values)
    return max(values)


def register(registry: ToolRegistry) -> None:
    @registry.tool("read_csv", "Inspect a CSV file: columns, inferred types, missing values, row count and sample rows. Never modifies the file.",
                   ReadCsvParams, tags=["csv", "data"])
    def read_csv(params: ReadCsvParams, ctx: ToolContext):
        target, header, rows, delimiter = _load_table(ctx, params.path, params.delimiter)
        return {
            "path": ctx.display_path(target),
            "delimiter": delimiter,
            "rows": len(rows),
            "columns": _profile_columns(header, rows),
            "sample": [dict(zip(header, row)) for row in rows[: params.max_rows]],
        }

    @registry.tool("calculate_statistics", "Compute descriptive statistics for numeric CSV columns, optionally aggregated by a category column.",
                   StatisticsParams, tags=["csv", "data"])
    def calculate_statistics(params: StatisticsParams, ctx: ToolContext):
        target, header, rows, _ = _load_table(ctx, params.path, params.delimiter)
        profile = {col["name"]: col for col in _profile_columns(header, rows)}
        numeric_cols = [name for name, col in profile.items() if col["type"] in {"integer", "float"}]
        wanted = params.columns or numeric_cols
        unknown = [c for c in wanted if c not in header]
        if unknown:
            raise ToolError(f"unknown columns {unknown}; available: {header}")
        non_numeric = [c for c in wanted if c not in numeric_cols]
        if non_numeric:
            raise ToolError(f"columns {non_numeric} are not numeric; numeric columns: {numeric_cols}")
        index = {name: i for i, name in enumerate(header)}

        def numeric_values(col: str, subset: list[list[str]]) -> list[float]:
            out = []
            for row in subset:
                num = _to_number(row[index[col]])
                if num is not None and not math.isnan(num):
                    out.append(num)
            return out

        result: dict[str, Any] = {
            "path": ctx.display_path(target),
            "rows": len(rows),
            "statistics": {col: _describe(numeric_values(col, rows)) for col in wanted},
        }
        if params.group_by:
            if params.group_by not in header:
                raise ToolError(f"group_by column '{params.group_by}' not found; available: {header}")
            groups: dict[str, list[list[str]]] = defaultdict(list)
            for row in rows:
                key = row[index[params.group_by]].strip() or "(missing)"
                groups[key].append(row)
            table = []
            for key, subset in groups.items():
                entry = {params.group_by: key, "rows": len(subset)}
                for col in wanted:
                    entry[f"{params.aggregate}_{col}"] = round(_aggregate(numeric_values(col, subset), params.aggregate), 4)
                table.append(entry)
            sort_key = f"{params.aggregate}_{wanted[0]}" if wanted else "rows"
            table.sort(key=lambda item: item.get(sort_key, 0), reverse=True)
            result["group_by"] = {
                "column": params.group_by,
                "aggregate": params.aggregate,
                "groups": len(table),
                "table": table[: params.top_n],
            }
        return result

    @registry.tool("create_chart", "Render a bar/line/pie chart from a CSV column pair to a PNG in the workspace (requires matplotlib).",
                   ChartParams, tags=["csv", "data", "write"])
    def create_chart(params: ChartParams, ctx: ToolContext):
        try:
            import matplotlib  # type: ignore

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt  # type: ignore
        except ImportError:
            raise ToolError("matplotlib is not installed; run `pip install matplotlib` or describe the data in a table instead")
        target, header, rows, _ = _load_table(ctx, params.path, None)
        for col in (params.x, params.y):
            if col not in header:
                raise ToolError(f"column '{col}' not found; available: {header}")
        xi, yi = header.index(params.x), header.index(params.y)
        buckets: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            num = _to_number(row[yi])
            if num is not None:
                buckets[row[xi].strip() or "(missing)"].append(num)
        series = sorted(
            ((k, _aggregate(v, params.aggregate)) for k, v in buckets.items()),
            key=lambda kv: kv[1], reverse=True,
        )[: params.top_n]
        if not series:
            raise ToolError("no numeric data to plot")
        labels, values = zip(*series)
        output = ctx.resolve_path(params.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        fig, ax = plt.subplots(figsize=(9, 5))
        if params.kind == "pie":
            ax.pie(values, labels=labels, autopct="%1.1f%%")
        elif params.kind == "line":
            ax.plot(labels, values, marker="o")
            ax.tick_params(axis="x", rotation=45)
        else:
            ax.bar(labels, values)
            ax.tick_params(axis="x", rotation=45)
        ax.set_title(params.title or f"{params.aggregate} of {params.y} by {params.x}")
        fig.tight_layout()
        fig.savefig(output, dpi=120)
        plt.close(fig)
        return {"chart": ctx.display_path(output), "points": len(series), "kind": params.kind}
