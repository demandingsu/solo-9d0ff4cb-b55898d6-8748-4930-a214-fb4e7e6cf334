"""连锁便利店电价套餐比较 API。

入口：POST /api/v1/compare-plans
流程：抓取报价站分页列表与套餐详情 -> 地区过滤 -> 资格校验
（缺费率 / 超已公布档位不得参与）-> 峰谷逐档计费 + 月基本费 -> 年度汇总排序。
任何列表或详情抓取失败都会记录失败地址，并把最低价结论标记为“待核实”。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI

from .billing import annual_total, bill_plan, check_eligibility
from .schemas import (
    CompareRequest,
    CompareResponse,
    CrawlFailureOut,
    ExcludedPlanOut,
    MonthlyBillOut,
    QuoteChangeOut,
    QuoteChangeRequest,
    QuoteChangeResponse,
    RankedPlanOut,
    RateTierOut,
    SnapshotIssueOut,
    SnapshotPlanOut,
    TierChargeOut,
)
from .scraper import IncompleteSnapshot, crawl_quote_site, plan_payload, scrape_complete_snapshot
from .snapshots import SnapshotStore

app = FastAPI(
    title="连锁便利店电价套餐比较 API",
    version="1.1.0",
    description=(
        "按门店峰谷用电预测比较公开电价套餐的年度成本；"
        "并可按报价源采集套餐快照，返回改价、上架、下架等字段差异。"
    ),
)
_snapshot_store: SnapshotStore | None = None


def get_snapshot_store() -> SnapshotStore:
    global _snapshot_store
    if _snapshot_store is None:
        _snapshot_store = SnapshotStore(os.environ.get("SNAPSHOT_DB_PATH"))
    return _snapshot_store


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tier_out(charges) -> list[TierChargeOut]:
    return [TierChargeOut(**vars(charge)) for charge in charges]


def _monthly_out(bills) -> list[MonthlyBillOut]:
    return [
        MonthlyBillOut(
            month=bill.month,
            peak_kwh=bill.peak_kwh,
            valley_kwh=bill.valley_kwh,
            peak_charges=_tier_out(bill.peak_charges),
            valley_charges=_tier_out(bill.valley_charges),
            peak_energy_fee_cny=bill.peak_energy_fee_cny,
            valley_energy_fee_cny=bill.valley_energy_fee_cny,
            base_fee_cny=bill.base_fee_cny,
            monthly_total_cny=bill.monthly_total_cny,
        )
        for bill in bills
    ]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/compare-plans", response_model=CompareResponse)
def compare_plans(request: CompareRequest) -> CompareResponse:
    usage = [item.model_dump() for item in request.monthly_usage]
    crawl_result = crawl_quote_site(request.list_url, timeout=request.timeout_seconds)

    ranked: list[RankedPlanOut] = []
    excluded: list[ExcludedPlanOut] = []

    for plan in crawl_result.plans:
        eligibility = check_eligibility(plan, request.region, usage)
        if not eligibility.eligible:
            excluded.append(
                ExcludedPlanOut(
                    plan_id=plan.plan_id,
                    plan_name=plan.name,
                    supplier=plan.supplier,
                    regions=plan.regions,
                    reason=eligibility.reason or "unknown",
                    detail=eligibility.detail or "",
                    detail_url=plan.detail_url,
                    list_url=plan.list_url,
                )
            )
            continue
        bills = bill_plan(plan, usage)
        ranked.append(
            RankedPlanOut(
                rank=0,  # 排序后统一回填
                plan_id=plan.plan_id,
                plan_name=plan.name,
                supplier=plan.supplier,
                regions=plan.regions,
                annual_total_cny=annual_total(bills),
                detail_url=plan.detail_url,
                list_url=plan.list_url,
                monthly_bills=_monthly_out(bills),
            )
        )

    ranked.sort(key=lambda item: item.annual_total_cny)
    for index, item in enumerate(ranked, start=1):
        item.rank = index

    failures = [
        CrawlFailureOut(url=f.url, stage=f.stage, reason=f.reason, status=f.status)
        for f in crawl_result.failures
    ]

    has_failure = bool(failures)
    if ranked:
        cheapest = ranked[0]
        conclusion = {
            "lowest_plan_id": cheapest.plan_id,
            "lowest_plan_name": cheapest.plan_name,
            "lowest_annual_total_cny": cheapest.annual_total_cny,
            "verification_status": "pending_verification" if has_failure else "verified",
            "note": (
                "存在抓取失败的列表页或套餐详情，最低价结论待核实"
                if has_failure
                else "全部列表页与套餐详情抓取成功，最低价结论已核实"
            ),
        }
    else:
        conclusion = {
            "lowest_plan_id": None,
            "lowest_plan_name": None,
            "lowest_annual_total_cny": None,
            "verification_status": "pending_verification",
            "note": "没有可参与比较的有效套餐，无法给出最低价结论，待核实",
        }

    return CompareResponse(
        request_region=request.region,
        list_url=request.list_url,
        crawl={
            "visited_list_urls": crawl_result.visited_list_urls,
            "plans_crawled": len(crawl_result.plans),
            "eligible_count": len(ranked),
            "excluded_count": len(excluded),
            "failure_count": len(failures),
        },
        conclusion=conclusion,
        ranked_plans=ranked,
        excluded_plans=excluded,
        failures=failures,
    )


def _snapshot_plan_out(plan: dict) -> SnapshotPlanOut:
    return SnapshotPlanOut(
        plan_id=plan["plan_id"],
        name=plan["name"],
        supplier=plan["supplier"],
        regions=plan["regions"],
        base_fee_cny=plan["base_fee_cny"],
        peak_tiers=[RateTierOut(
            index=tier["index"],
            lower_kwh=tier["lower_kwh"],
            upper_kwh=tier["upper_kwh"],
            rate_cny_per_kwh=tier["rate_cny_per_kwh"],
        ) for tier in plan["peak_tiers"]],
        valley_tiers=[RateTierOut(
            index=tier["index"],
            lower_kwh=tier["lower_kwh"],
            upper_kwh=tier["upper_kwh"],
            rate_cny_per_kwh=tier["rate_cny_per_kwh"],
        ) for tier in plan["valley_tiers"]],
        detail_url=plan["detail_url"],
    )


@app.post("/api/v1/quote-changes", response_model=QuoteChangeResponse)
def quote_changes(request: QuoteChangeRequest) -> QuoteChangeResponse:
    """采集报价源完整快照，并与该来源上次完整快照比较字段差异。

    - 首次完整采集：建立基线，changes 为空；
    - 采集不完整（列表项无法解析、分页未到终页、详情失败、关键字段缺失）：
      返回 issues 中的问题地址，不提交新快照，原快照保留；
    - 与上次快照内容一致：unchanged，不产生变更记录。
    """
    store = get_snapshot_store()
    try:
        details, visited = scrape_complete_snapshot(request.list_url, timeout=request.timeout_seconds)
    except IncompleteSnapshot as exc:
        issues = [
            SnapshotIssueOut(url=issue.url, stage=issue.stage, reason=issue.reason, status=issue.status)
            for issue in exc.issues
        ]
        store.record_failed_attempt(
            request.source_key, request.list_url, [issue.model_dump() for issue in issues]
        )
        previous = store.current_snapshot(request.source_key)
        return QuoteChangeResponse(
            source_key=request.source_key,
            list_url=request.list_url,
            status="incomplete",
            snapshot_id=previous.snapshot_id if previous else None,
            previous_snapshot_id=previous.snapshot_id if previous else None,
            completed_at=_utc_now(),
            plan_count=len(previous.plans) if previous else 0,
            plans=[_snapshot_plan_out(plan) for plan in previous.plans] if previous else [],
            changes=[],
            issues=issues,
            message=(
                "本次采集不完整，已保留原快照"
                if previous
                else "本次采集不完整，尚未建立基线"
            ),
        )

    plans = [dict(plan_payload(detail, request.list_url)) for detail in details]
    outcome = store.commit_snapshot(request.source_key, request.list_url, plans, visited)

    messages = {
        "baseline_created": "首次完整采集，已建立基线快照",
        "unchanged": "采集内容与上次完整快照一致，无变更",
        "changed": "采集完成，检测到报价变更",
    }
    return QuoteChangeResponse(
        source_key=request.source_key,
        list_url=request.list_url,
        status=outcome.status,
        snapshot_id=outcome.snapshot_id,
        previous_snapshot_id=outcome.previous_snapshot_id,
        completed_at=_utc_now(),
        plan_count=outcome.plan_count,
        plans=[_snapshot_plan_out(plan) for plan in plans],
        changes=[
            QuoteChangeOut(
                plan_id=change.plan_id,
                plan_name=change.plan_name,
                change_type=change.change_type,
                field=change.field,
                old_value=change.old_value,
                new_value=change.new_value,
                old_detail_url=change.old_detail_url,
                new_detail_url=change.new_detail_url,
            )
            for change in outcome.changes
        ],
        issues=[],
        message=messages[outcome.status],
    )
