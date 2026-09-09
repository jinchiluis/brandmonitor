#!/usr/bin/env python3
"""Model token capacity and inference cost for a multi-stage document funnel.

The built-in scenario is a simple aggregate workload:

    50M raw documents/day
    10B total tokens/day
    5:1 input/output token ratio
    100% batch processing
    0% cached input

Pricing defaults use the official global Claude API list prices for Claude Fable
5.1, verified 2026-09-05. Run with --help to replace volumes, prices, discounts,
or funnel stages.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable


D = Decimal
MILLION = D("1000000")
BILLION = D("1000000000")
HUNDRED = D("100")


@dataclass(frozen=True)
class Stage:
    """One nested stage of the funnel.

    reach_pct is the percentage of original raw documents reaching this stage.
    cached_input_pct and batch_share_pct are token-weighted percentages.
    """

    name: str
    reach_pct: Decimal
    input_tokens_per_document: Decimal
    output_tokens_per_document: Decimal
    cached_input_pct: Decimal
    batch_share_pct: Decimal


@dataclass(frozen=True)
class Pricing:
    """Prices are USD per one million tokens."""

    input_per_million: Decimal
    cached_input_per_million: Decimal
    output_per_million: Decimal
    batch_discount_pct: Decimal


@dataclass(frozen=True)
class StageResult:
    stage: Stage
    documents_per_day: Decimal
    input_tokens: Decimal
    cached_input_tokens: Decimal
    uncached_input_tokens: Decimal
    output_tokens: Decimal
    total_tokens: Decimal
    batch_tokens: Decimal
    realtime_tokens: Decimal
    uncached_input_cost: Decimal
    cached_input_cost: Decimal
    output_cost: Decimal
    cache_savings: Decimal
    batch_savings: Decimal
    daily_cost: Decimal


DEFAULT_PRICING = Pricing(
    input_per_million=D("10.00"),
    cached_input_per_million=D("0.25"),
    output_per_million=D("50.00"),
    batch_discount_pct=D("50"),
)

MODEL_NAME = "Claude Fable 5.1"
PRICING_VERIFIED = "2026-09-05"
PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"


def build_aggregate_stage(
    documents_per_day: Decimal,
    total_tokens_per_day: Decimal,
    input_output_ratio: Decimal,
    cached_input_pct: Decimal,
    batch_share_pct: Decimal,
) -> Stage:
    """Build one stage by splitting a total token workload using an I:O ratio."""
    _require_finite_nonnegative("documents_per_day", documents_per_day)
    _require_finite_nonnegative("total_tokens_per_day", total_tokens_per_day)
    _require_finite_nonnegative("input_output_ratio", input_output_ratio)
    _require_percent("cached_input_pct", cached_input_pct)
    _require_percent("batch_share_pct", batch_share_pct)
    if documents_per_day == 0:
        raise ValueError("documents_per_day must be greater than zero")
    if total_tokens_per_day == 0:
        raise ValueError("total_tokens_per_day must be greater than zero")

    tokens_per_document = total_tokens_per_day / documents_per_day
    output_tokens = tokens_per_document / (input_output_ratio + D("1"))
    input_tokens = tokens_per_document - output_tokens
    return Stage(
        name="Aggregate workload",
        reach_pct=D("100"),
        input_tokens_per_document=input_tokens,
        output_tokens_per_document=output_tokens,
        cached_input_pct=cached_input_pct,
        batch_share_pct=batch_share_pct,
    )


def _require_finite_nonnegative(name: str, value: Decimal) -> None:
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite, non-negative number")


def _require_percent(name: str, value: Decimal) -> None:
    _require_finite_nonnegative(name, value)
    if value > HUNDRED:
        raise ValueError(f"{name} must be between 0 and 100")


def validate(
    documents_per_day: Decimal,
    days_per_month: Decimal,
    daily_token_capacity: Decimal,
    target_utilization_pct: Decimal,
    active_hours_per_day: Decimal,
    document_input_tokens_min: Decimal,
    document_input_tokens_max: Decimal,
    pricing: Pricing,
    stages: Iterable[Stage],
) -> tuple[Stage, ...]:
    _require_finite_nonnegative("documents_per_day", documents_per_day)
    _require_finite_nonnegative("days_per_month", days_per_month)
    _require_finite_nonnegative("daily_token_capacity", daily_token_capacity)
    _require_percent("target_utilization_pct", target_utilization_pct)
    _require_finite_nonnegative("active_hours_per_day", active_hours_per_day)
    _require_finite_nonnegative(
        "document_input_tokens_min", document_input_tokens_min
    )
    _require_finite_nonnegative(
        "document_input_tokens_max", document_input_tokens_max
    )

    if days_per_month == 0:
        raise ValueError("days_per_month must be greater than zero")
    if daily_token_capacity == 0:
        raise ValueError("daily_token_capacity must be greater than zero")
    if active_hours_per_day == 0 or active_hours_per_day > D("24"):
        raise ValueError("active_hours_per_day must be greater than 0 and at most 24")
    if document_input_tokens_min == 0 or document_input_tokens_max == 0:
        raise ValueError("document input token bounds must be greater than zero")
    if document_input_tokens_min > document_input_tokens_max:
        raise ValueError(
            "document_input_tokens_min must not exceed document_input_tokens_max"
        )

    for field, value in asdict(pricing).items():
        if field.endswith("_pct"):
            _require_percent(field, value)
        else:
            _require_finite_nonnegative(field, value)

    checked = tuple(stages)
    if not checked:
        raise ValueError("at least one funnel stage is required")

    for index, stage in enumerate(checked, 1):
        if not stage.name.strip():
            raise ValueError(f"stage {index} needs a name")
        _require_percent(f"{stage.name}.reach_pct", stage.reach_pct)
        _require_finite_nonnegative(
            f"{stage.name}.input_tokens_per_document",
            stage.input_tokens_per_document,
        )
        _require_finite_nonnegative(
            f"{stage.name}.output_tokens_per_document",
            stage.output_tokens_per_document,
        )
        _require_percent(f"{stage.name}.cached_input_pct", stage.cached_input_pct)
        _require_percent(f"{stage.name}.batch_share_pct", stage.batch_share_pct)

    weighted_tokens = sum(
        (
            stage.reach_pct
            / HUNDRED
            * (stage.input_tokens_per_document + stage.output_tokens_per_document)
            for stage in checked
        ),
        D("0"),
    )
    if weighted_tokens == 0:
        raise ValueError("the funnel must process at least one token per raw document")

    return checked


def calculate_stage(
    stage: Stage,
    documents_per_day: Decimal,
    pricing: Pricing,
) -> StageResult:
    stage_documents = documents_per_day * stage.reach_pct / HUNDRED
    input_tokens = stage_documents * stage.input_tokens_per_document
    output_tokens = stage_documents * stage.output_tokens_per_document
    cached_tokens = input_tokens * stage.cached_input_pct / HUNDRED
    uncached_tokens = input_tokens - cached_tokens
    total_tokens = input_tokens + output_tokens

    batch_share = stage.batch_share_pct / HUNDRED
    discount = pricing.batch_discount_pct / HUNDRED
    batch_factor = D("1") - batch_share * discount

    input_cost_before_batch = (
        uncached_tokens * pricing.input_per_million / MILLION
    )
    cached_cost_before_batch = (
        cached_tokens * pricing.cached_input_per_million / MILLION
    )
    output_cost_before_batch = output_tokens * pricing.output_per_million / MILLION
    pre_batch_cost = (
        input_cost_before_batch
        + cached_cost_before_batch
        + output_cost_before_batch
    )

    cache_savings = (
        cached_tokens
        * (pricing.input_per_million - pricing.cached_input_per_million)
        / MILLION
    )
    batch_savings = pre_batch_cost * batch_share * discount

    return StageResult(
        stage=stage,
        documents_per_day=stage_documents,
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        uncached_input_tokens=uncached_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        batch_tokens=total_tokens * batch_share,
        realtime_tokens=total_tokens * (D("1") - batch_share),
        uncached_input_cost=input_cost_before_batch * batch_factor,
        cached_input_cost=cached_cost_before_batch * batch_factor,
        output_cost=output_cost_before_batch * batch_factor,
        cache_savings=cache_savings,
        batch_savings=batch_savings,
        daily_cost=pre_batch_cost * batch_factor,
    )


def calculate(
    documents_per_day: Decimal,
    days_per_month: Decimal,
    daily_token_capacity: Decimal,
    target_utilization_pct: Decimal,
    active_hours_per_day: Decimal,
    document_input_tokens_min: Decimal,
    document_input_tokens_max: Decimal,
    pricing: Pricing,
    stages: Iterable[Stage],
) -> dict:
    checked_stages = validate(
        documents_per_day,
        days_per_month,
        daily_token_capacity,
        target_utilization_pct,
        active_hours_per_day,
        document_input_tokens_min,
        document_input_tokens_max,
        pricing,
        stages,
    )
    stage_results = [
        calculate_stage(stage, documents_per_day, pricing)
        for stage in checked_stages
    ]

    total_input = sum((r.input_tokens for r in stage_results), D("0"))
    total_cached = sum((r.cached_input_tokens for r in stage_results), D("0"))
    total_output = sum((r.output_tokens for r in stage_results), D("0"))
    total_tokens = total_input + total_output
    total_batch = sum((r.batch_tokens for r in stage_results), D("0"))
    total_realtime = sum((r.realtime_tokens for r in stage_results), D("0"))
    total_daily_cost = sum((r.daily_cost for r in stage_results), D("0"))
    total_cache_savings = sum((r.cache_savings for r in stage_results), D("0"))
    total_batch_savings = sum((r.batch_savings for r in stage_results), D("0"))
    actual_uncached_cost = sum(
        (r.uncached_input_cost for r in stage_results), D("0")
    )
    actual_cached_cost = sum(
        (r.cached_input_cost for r in stage_results), D("0")
    )
    actual_output_cost = sum((r.output_cost for r in stage_results), D("0"))

    weighted_tokens_per_raw_document = sum(
        (
            stage.reach_pct
            / HUNDRED
            * (stage.input_tokens_per_document + stage.output_tokens_per_document)
            for stage in checked_stages
        ),
        D("0"),
    )
    max_documents = (
        daily_token_capacity / weighted_tokens_per_raw_document
        if weighted_tokens_per_raw_document
        else D("0")
    )
    safe_max_documents = max_documents * target_utilization_pct / HUNDRED
    utilization_pct = total_tokens / daily_token_capacity * HUNDRED
    headroom = daily_token_capacity - total_tokens
    baseline_cost = total_daily_cost + total_cache_savings + total_batch_savings
    input_output_ratio = (
        total_input / total_output if total_output else None
    )
    active_minutes_per_day = active_hours_per_day * D("60")
    total_tokens_per_minute = total_tokens / active_minutes_per_day
    input_tokens_per_minute = total_input / active_minutes_per_day
    output_tokens_per_minute = total_output / active_minutes_per_day
    midpoint_document_input_tokens = (
        document_input_tokens_min + document_input_tokens_max
    ) / D("2")
    minimum_requests_per_minute = (
        input_tokens_per_minute / document_input_tokens_max
    )
    maximum_requests_per_minute = (
        input_tokens_per_minute / document_input_tokens_min
    )
    midpoint_requests_per_minute = (
        input_tokens_per_minute / midpoint_document_input_tokens
    )

    return {
        "documents_per_day": documents_per_day,
        "days_per_month": days_per_month,
        "daily_token_capacity": daily_token_capacity,
        "target_utilization_pct": target_utilization_pct,
        "pricing": pricing,
        "stage_results": stage_results,
        "total_input_tokens": total_input,
        "total_cached_input_tokens": total_cached,
        "total_uncached_input_tokens": total_input - total_cached,
        "total_output_tokens": total_output,
        "total_tokens": total_tokens,
        "total_batch_tokens": total_batch,
        "total_realtime_tokens": total_realtime,
        "input_output_ratio": input_output_ratio,
        "active_hours_per_day": active_hours_per_day,
        "active_minutes_per_day": active_minutes_per_day,
        "document_input_tokens_min": document_input_tokens_min,
        "document_input_tokens_max": document_input_tokens_max,
        "midpoint_document_input_tokens": midpoint_document_input_tokens,
        "total_tokens_per_minute": total_tokens_per_minute,
        "input_tokens_per_minute": input_tokens_per_minute,
        "output_tokens_per_minute": output_tokens_per_minute,
        "minimum_requests_per_minute": minimum_requests_per_minute,
        "maximum_requests_per_minute": maximum_requests_per_minute,
        "midpoint_requests_per_minute": midpoint_requests_per_minute,
        "weighted_tokens_per_raw_document": weighted_tokens_per_raw_document,
        "capacity_utilization_pct": utilization_pct,
        "capacity_headroom_tokens": headroom,
        "max_documents_at_capacity": max_documents,
        "max_documents_at_target_utilization": safe_max_documents,
        "actual_uncached_input_cost_per_day": actual_uncached_cost,
        "actual_cached_input_cost_per_day": actual_cached_cost,
        "actual_output_cost_per_day": actual_output_cost,
        "daily_cost": total_daily_cost,
        "monthly_cost": total_daily_cost * days_per_month,
        "baseline_cost_per_day": baseline_cost,
        "cache_savings_per_day": total_cache_savings,
        "batch_savings_per_day": total_batch_savings,
        "total_savings_per_day": total_cache_savings + total_batch_savings,
    }


def parse_decimal(raw: str) -> Decimal:
    try:
        value = D(raw.replace("_", "").replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise argparse.ArgumentTypeError(f"invalid number: {raw!r}") from exc
    if not value.is_finite():
        raise argparse.ArgumentTypeError("number must be finite")
    return value


def parse_stage(raw: str) -> Stage:
    """Parse NAME,REACH%,INPUT_TOKENS,OUTPUT_TOKENS,CACHED_INPUT%,BATCH%."""
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 6:
        raise argparse.ArgumentTypeError(
            "stage must be NAME,REACH%,INPUT_TOKENS,OUTPUT_TOKENS,"
            "CACHED_INPUT%,BATCH%"
        )
    try:
        return Stage(
            name=parts[0],
            reach_pct=parse_decimal(parts[1]),
            input_tokens_per_document=parse_decimal(parts[2]),
            output_tokens_per_document=parse_decimal(parts[3]),
            cached_input_pct=parse_decimal(parts[4]),
            batch_share_pct=parse_decimal(parts[5]),
        )
    except argparse.ArgumentTypeError:
        raise
    except Exception as exc:
        raise argparse.ArgumentTypeError(f"invalid stage: {raw!r}") from exc


def human_number(value: Decimal) -> str:
    absolute = abs(value)
    if absolute >= BILLION:
        return f"{value / BILLION:,.2f}B"
    if absolute >= MILLION:
        return f"{value / MILLION:,.2f}M"
    if absolute >= D("1000"):
        return f"{value / D('1000'):,.2f}K"
    return f"{value:,.2f}"


def money(value: Decimal) -> str:
    return f"${value:,.2f}"


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---:" if i else "---" for i in range(len(headers))) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def render_markdown(result: dict) -> str:
    pricing: Pricing = result["pricing"]
    stage_results: list[StageResult] = result["stage_results"]
    days = result["days_per_month"]

    stage_rows = []
    for item in stage_results:
        stage_rows.append(
            [
                item.stage.name,
                f"{item.stage.reach_pct:,.2f}%",
                human_number(item.documents_per_day),
                human_number(item.input_tokens),
                human_number(item.cached_input_tokens),
                human_number(item.output_tokens),
                f"{item.stage.batch_share_pct:,.1f}%",
                human_number(item.total_tokens),
                money(item.daily_cost),
            ]
        )

    cost_rows = [
        [
            "Uncached input",
            money(result["actual_uncached_input_cost_per_day"]),
            money(result["actual_uncached_input_cost_per_day"] * days),
        ],
        [
            "Cached input",
            money(result["actual_cached_input_cost_per_day"]),
            money(result["actual_cached_input_cost_per_day"] * days),
        ],
        [
            "Output",
            money(result["actual_output_cost_per_day"]),
            money(result["actual_output_cost_per_day"] * days),
        ],
        [
            "**Total after savings**",
            f"**{money(result['daily_cost'])}**",
            f"**{money(result['monthly_cost'])}**",
        ],
    ]

    headroom = result["capacity_headroom_tokens"]
    capacity_status = (
        f"{human_number(headroom)} headroom"
        if headroom >= 0
        else f"{human_number(-headroom)} over capacity"
    )
    warning = ""
    if result["capacity_utilization_pct"] > result["target_utilization_pct"]:
        warning = (
            "\n> **Capacity warning:** utilization exceeds the configured "
            f"{result['target_utilization_pct']:,.1f}% target. "
            "Cache and batch discounts lower cost, not token load.\n"
        )
    price_warning = ""
    if pricing.cached_input_per_million > pricing.input_per_million:
        price_warning = (
            "\n> **Pricing warning:** cached-input price exceeds regular-input "
            "price. Check the provider rates.\n"
        )
    ratio = result["input_output_ratio"]
    ratio_text = f"{ratio:,.2f}:1" if ratio is not None else "n/a"

    return f"""# {MODEL_NAME} Inference Calculation

> Official global Claude API list pricing, verified {PRICING_VERIFIED}: input
> $10/MTok, output $50/MTok, cache reads $0.25/MTok, and Batch API 50% off input
> and output. Source: {PRICING_SOURCE}

## Funnel

{markdown_table(
    [
        "Stage",
        "Raw reach",
        "Documents/day",
        "Input tokens",
        "Cached input [1]",
        "Output tokens",
        "Batch share [2]",
        "Total tokens",
        "Cost/day",
    ],
    stage_rows,
)}

[1] Cached input is a token-weighted subset of input, not additional tokens.  
[2] Batch share is token-weighted. Funnel stages are nested and their raw-reach
percentages are not meant to sum to 100%.

## Capacity

- Raw documents/day: **{human_number(result["documents_per_day"])}**
- Input tokens/day: **{human_number(result["total_input_tokens"])}**
- Output tokens/day: **{human_number(result["total_output_tokens"])}**
- Input/output ratio: **{ratio_text}**
- Cached input tokens/day: **{human_number(result["total_cached_input_tokens"])}**
- Batch tokens/day: **{human_number(result["total_batch_tokens"])}**
- Real-time tokens/day: **{human_number(result["total_realtime_tokens"])}**
- Weighted tokens/raw document: **{result["weighted_tokens_per_raw_document"]:,.0f}**
- Token load: **{human_number(result["total_tokens"])}/day**
- Daily capacity: **{human_number(result["daily_token_capacity"])}**
- Utilization: **{result["capacity_utilization_pct"]:,.2f}%** - {capacity_status}
- Maximum raw volume at 100% capacity: **{human_number(result["max_documents_at_capacity"])} documents/day**
- Maximum at {result["target_utilization_pct"]:,.1f}% target utilization: **{human_number(result["max_documents_at_target_utilization"])} documents/day**
{warning}
## Active Throughput

- Work window: **{result["active_hours_per_day"]:,.2f} hours/day**
- Total throughput: **{human_number(result["total_tokens_per_minute"])} TPM**
- Input throughput: **{human_number(result["input_tokens_per_minute"])} TPM**
- Output throughput: **{human_number(result["output_tokens_per_minute"])} TPM**
- Assumed document input size: **{result["document_input_tokens_min"]:,.0f}-{result["document_input_tokens_max"]:,.0f} tokens**
- Implied request rate: **{result["minimum_requests_per_minute"]:,.0f}-{result["maximum_requests_per_minute"]:,.0f} RPM**
- At the {result["midpoint_document_input_tokens"]:,.0f}-token midpoint: **{result["midpoint_requests_per_minute"]:,.0f} RPM**

Cache and batch settings affect cost, not the required TPM or RPM.

## Cost

Rates per million tokens: input **{money(pricing.input_per_million)}**, cached input
**{money(pricing.cached_input_per_million)}**, output
**{money(pricing.output_per_million)}**. Batch discount:
**{pricing.batch_discount_pct:,.1f}%**.

{markdown_table(["Component", "Daily", f"{days:,.0f}-day month"], cost_rows)}

- Cost without cache or batch savings: **{money(result["baseline_cost_per_day"])}/day**
- Cache savings: **{money(result["cache_savings_per_day"])}/day**
- Batch savings: **{money(result["batch_savings_per_day"])}/day**
- Total savings: **{money(result["total_savings_per_day"])}/day**
{price_warning}"""


def json_ready(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (Stage, Pricing, StageResult)):
        return {key: json_ready(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calculate tokens, capacity, cache savings, and batch savings "
        "for a multi-stage inference funnel.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--documents-per-day", type=parse_decimal, default=D("50000000"))
    parser.add_argument(
        "--total-tokens-per-day",
        type=parse_decimal,
        default=D("10000000000"),
        help="Aggregate token workload used when no custom --stage is supplied",
    )
    parser.add_argument(
        "--input-output-ratio",
        type=parse_decimal,
        default=D("5"),
        help="Input tokens per one output token in aggregate mode",
    )
    parser.add_argument(
        "--cached-input-share",
        type=parse_decimal,
        default=D("0"),
        help="Token-weighted cached share of input in aggregate mode",
    )
    parser.add_argument(
        "--batch-share",
        type=parse_decimal,
        default=D("100"),
        help="Token-weighted batch share in aggregate mode",
    )
    parser.add_argument("--days-per-month", type=parse_decimal, default=D("30"))
    parser.add_argument(
        "--daily-token-capacity",
        type=parse_decimal,
        default=D("10000000000"),
    )
    parser.add_argument(
        "--target-utilization",
        type=parse_decimal,
        default=D("80"),
        help="Desired maximum percentage of daily token capacity",
    )
    parser.add_argument(
        "--active-hours-per-day",
        type=parse_decimal,
        default=D("24"),
        help="Hours in which the daily token workload is processed",
    )
    parser.add_argument(
        "--document-input-tokens-min",
        type=parse_decimal,
        default=D("2000"),
        help="Smallest expected input-token count per document",
    )
    parser.add_argument(
        "--document-input-tokens-max",
        type=parse_decimal,
        default=D("8000"),
        help="Largest expected input-token count per document",
    )
    parser.add_argument(
        "--input-price",
        type=parse_decimal,
        default=DEFAULT_PRICING.input_per_million,
        help="Regular input price in USD per million tokens",
    )
    parser.add_argument(
        "--cached-input-price",
        type=parse_decimal,
        default=DEFAULT_PRICING.cached_input_per_million,
        help="Cached input price in USD per million tokens",
    )
    parser.add_argument(
        "--output-price",
        type=parse_decimal,
        default=DEFAULT_PRICING.output_per_million,
        help="Output price in USD per million tokens",
    )
    parser.add_argument(
        "--batch-discount",
        type=parse_decimal,
        default=DEFAULT_PRICING.batch_discount_pct,
        help="Discount percentage applied to the batch share after cache pricing",
    )
    parser.add_argument(
        "--stage",
        action="append",
        type=parse_stage,
        help=(
            "Replace defaults with a repeated stage definition: "
            "NAME,REACH%%,INPUT_TOKENS,OUTPUT_TOKENS,CACHED_INPUT%%,BATCH%%. "
            "When supplied, aggregate workload options are ignored"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of Markdown",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    pricing = Pricing(
        input_per_million=args.input_price,
        cached_input_per_million=args.cached_input_price,
        output_per_million=args.output_price,
        batch_discount_pct=args.batch_discount,
    )
    try:
        stages = (
            tuple(args.stage)
            if args.stage
            else (
                build_aggregate_stage(
                    documents_per_day=args.documents_per_day,
                    total_tokens_per_day=args.total_tokens_per_day,
                    input_output_ratio=args.input_output_ratio,
                    cached_input_pct=args.cached_input_share,
                    batch_share_pct=args.batch_share,
                ),
            )
        )
        result = calculate(
            documents_per_day=args.documents_per_day,
            days_per_month=args.days_per_month,
            daily_token_capacity=args.daily_token_capacity,
            target_utilization_pct=args.target_utilization,
            active_hours_per_day=args.active_hours_per_day,
            document_input_tokens_min=args.document_input_tokens_min,
            document_input_tokens_max=args.document_input_tokens_max,
            pricing=pricing,
            stages=stages,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.json:
        print(json.dumps(json_ready(result), indent=2, ensure_ascii=False))
    else:
        print(render_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
