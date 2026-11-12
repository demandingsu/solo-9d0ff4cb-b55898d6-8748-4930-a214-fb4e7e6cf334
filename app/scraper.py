"""报价站抓取与解析：分页列表 + 套餐详情。

页面结构随应用提供（见 ../quote-site），解析器依赖页面上的稳定数据属性：
- 列表页：article.plan-card[data-plan-id] 内含 a.plan-link 与 region 标签
- 分页：nav[data-pagination] 内 a[rel=next]
- 详情页：main[data-page=plan-detail]，费率表 table.rate-table[data-rate-kind]，
  档位 tr[data-rate-tier] 携带起止电量与费率数据属性
"""
from __future__ import annotations

import posixpath
import re
import urllib.parse
from dataclasses import dataclass, field

from .fetcher import FetchError, fetch_url, join_url
from .html_dom import Element, parse_html

MAX_LIST_PAGES = 50  # 分页安全上限，防止异常站点造成无限翻页


@dataclass
class RateTier:
    index: int
    lower_kwh: float
    upper_kwh: float | None  # None 表示无上限
    rate_cny_per_kwh: float


@dataclass
class PlanDetail:
    plan_id: str
    name: str
    supplier: str | None
    regions: list[str]
    base_fee_cny: float
    peak_tiers: list[RateTier]
    valley_tiers: list[RateTier]
    detail_url: str
    list_url: str
    base_fee_gap: bool = False  # 详情页未公布月基本费（base_fee_cny 为 NaN）


@dataclass
class PlanListEntry:
    plan_id: str
    detail_url: str
    list_url: str
    name: str | None = None
    supplier: str | None = None
    regions: list[str] = field(default_factory=list)
    base_fee_cny: float | None = None


@dataclass
class CrawlFailure:
    url: str
    stage: str  # list | detail
    reason: str
    status: int | None = None


@dataclass
class CrawlResult:
    plans: list[PlanDetail] = field(default_factory=list)
    failures: list[CrawlFailure] = field(default_factory=list)
    visited_list_urls: list[str] = field(default_factory=list)


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_list_page(markup: str, page_url: str) -> tuple[list[PlanListEntry], str | None]:
    root = parse_html(markup)
    entries: list[PlanListEntry] = []
    for card in root.find_all(tag="article", attr="data-plan-id"):
        plan_id = card.get("data-plan-id") or ""
        link = card.find(tag="a", attr="class", attr_value="plan-link")
        href = link.get("href") if link is not None else None
        if not plan_id or not href:
            continue
        name = link.text or None
        entries.append(
            PlanListEntry(
                plan_id=plan_id,
                detail_url=join_url(page_url, href),
                list_url=page_url,
                name=name,
            )
        )
    next_link = root.find(tag="a", attr="rel", attr_value="next")
    next_url = join_url(page_url, next_link.get("href")) if next_link is not None and next_link.get("href") else None
    return entries, next_url


def _card_attr_text(card: Element, attr: str) -> str | None:
    node = card.find(attr=attr)
    if node is None:
        return None
    value = node.get(attr)
    return value.strip() if isinstance(value, str) else None


def _supplier_from_text(card: Element) -> str | None:
    """回退：从卡片文本“供应商：X”中取出供应商名。"""
    text = card.text
    for separator in ("：", ":"):
        marker = "供应商" + separator
        if marker in text:
            value = text.split(marker, 1)[1].strip()
            for stop in ("月基本费", "适用地区", "¥", "\n"):
                value = value.split(stop, 1)[0].strip()
            return value or None
    return None


def parse_list_page_strict(markup: str, page_url: str) -> tuple[list[PlanListEntry], str | None, list["SnapshotIssue"], dict]:
    """严格解析列表页：无法解析的套餐项与分页异常都记入问题地址。

    与宽松版 parse_list_page 不同，本函数把每张 article.plan-card 视为一个套餐项，
    缺少套餐编号、详情链接、供应商、地区或月基本费都无法构成可核对的档案，
    记录为问题而不是静默跳过，从而避免把解析失败误判成“下架”。

    返回 (套餐项, 下一页地址, 问题清单, 分页信息)。分页信息含 ``page_no`` 与
    ``total_pages``（页面公布“第 X 页 / 共 Y 页”时），用于确认是否已到终页。
    """
    root = parse_html(markup)
    if root.find(tag="main", attr="data-page", attr_value="plan-list") is None:
        raise ValueError("列表页缺少 plan-list 主容器")

    entries: list[PlanListEntry] = []
    issues: list[SnapshotIssue] = []
    # 统一扫描所有 article，避免“部分卡片带编号、部分不带”时漏掉后者。
    cards = [
        card for card in root.find_all(tag="article")
        if card.get("data-plan-id") is not None or "plan-card" in (card.get("class") or "").split()
    ]

    for card in cards:
        plan_id = (card.get("data-plan-id") or "").strip()
        link = card.find(tag="a", attr="class", attr_value="plan-link")
        href = (link.get("href") or "").strip() if link is not None else ""
        if not plan_id or not href:
            missing = []
            if not plan_id:
                missing.append("套餐编号")
            if not href:
                missing.append("详情链接")
            issues.append(
                SnapshotIssue(url=page_url, stage="list", reason="列表项无法解析，缺少" + "、".join(missing))
            )
            continue
        supplier = _card_attr_text(card, "data-supplier") or _supplier_from_text(card)
        base_fee = _parse_float(_card_attr_text(card, "data-base-fee-cny"))
        regions = [(node.get("data-region") or "").strip() for node in card.find_all(attr="data-region")]
        regions = [region for region in regions if region]
        entries.append(
            PlanListEntry(
                plan_id=plan_id,
                detail_url=join_url(page_url, href),
                list_url=page_url,
                name=link.text or None,
                supplier=supplier,
                regions=regions,
                base_fee_cny=base_fee,
            )
        )
        if supplier is None or base_fee is None or not regions:
            missing = []
            if supplier is None:
                missing.append("供应商")
            if base_fee is None:
                missing.append("月基本费")
            if not regions:
                missing.append("地区")
            issues.append(
                SnapshotIssue(
                    url=join_url(page_url, href),
                    stage="list",
                    reason=f"列表项 {plan_id} 无法解析，缺少" + "、".join(missing),
                )
            )

    next_link = root.find(tag="a", attr="rel", attr_value="next")
    next_url = None
    if next_link is not None and next_link.get("href"):
        next_url = join_url(page_url, next_link.get("href").strip())

    pagination = _parse_pagination(root)
    return entries, next_url, issues, pagination


_PAGE_PATTERN = re.compile(r"第\s*([0-9]+)\s*页\s*/\s*共\s*([0-9]+)\s*页")


def _parse_pagination(root: Element) -> dict:
    """解析页面公布的分页信息“第 X 页 / 共 Y 页”。"""
    info: dict = {}
    nav = root.find(tag="nav", attr="data-pagination")
    text = nav.text if nav is not None else root.text
    match = _PAGE_PATTERN.search(text)
    if match:
        info["page_no"] = int(match.group(1))
        info["total_pages"] = int(match.group(2))
        return info
    main = root.find(tag="main", attr="data-page", attr_value="plan-list")
    if main is not None and main.get("data-page-no"):
        try:
            info["page_no"] = int(main.get("data-page-no"))
        except (TypeError, ValueError):
            pass
    return info


def _parse_tiers(table: Element) -> list[RateTier]:
    tiers: list[RateTier] = []
    for idx, row in enumerate(table.find_all(tag="tr", attr="data-rate-tier"), start=1):
        lower = _parse_float(row.get("data-tier-from-kwh"))
        upper = _parse_float(row.get("data-tier-to-kwh"))  # 空属性 -> None（无上限）
        rate = _parse_float(row.get("data-rate-cny-per-kwh"))
        if lower is None or rate is None:
            # 档位缺少下限或费率：数据属性不完整，跳过该档，后续校验会拦截。
            continue
        tiers.append(RateTier(index=idx, lower_kwh=lower, upper_kwh=upper, rate_cny_per_kwh=rate))
    return tiers


def parse_detail_page(markup: str, detail_url: str, list_url: str) -> PlanDetail:
    root = parse_html(markup)
    main = root.find(tag="main", attr="data-page", attr_value="plan-detail")
    if main is None:
        raise ValueError("详情页缺少 plan-detail 主容器")

    plan_id = main.get("data-plan-id") or ""
    name_node = main.find(attr="data-plan-name")
    name = name_node.text if name_node is not None else plan_id
    supplier_node = main.find(attr="data-supplier")
    supplier = supplier_node.text if supplier_node is not None else None
    regions = [tag.get("data-region", "") for tag in main.find_all(attr="data-region")]
    regions = [r for r in regions if r]
    base_fee_node = main.find(attr="data-base-fee-cny")
    base_fee = _parse_float(base_fee_node.get("data-base-fee-cny")) if base_fee_node is not None else None

    peak_tiers: list[RateTier] = []
    valley_tiers: list[RateTier] = []
    for table in main.find_all(tag="table", attr="class", attr_value="rate-table"):
        kind = table.get("data-rate-kind")
        tiers = _parse_tiers(table)
        if kind == "peak":
            peak_tiers = tiers
        elif kind == "valley":
            valley_tiers = tiers

    return PlanDetail(
        plan_id=plan_id,
        name=name,
        supplier=supplier,
        regions=regions,
        base_fee_cny=base_fee if base_fee is not None else float("nan"),
        peak_tiers=peak_tiers,
        valley_tiers=valley_tiers,
        detail_url=detail_url,
        list_url=list_url,
        base_fee_gap=base_fee is None,
    )


def crawl_quote_site(list_url: str, timeout: float = 8.0) -> CrawlResult:
    """从报价列表页入口开始，抓取全部分页与每个套餐详情。"""
    result = CrawlResult()
    seen_list_urls: set[str] = set()
    entries: list[PlanListEntry] = []
    current: str | None = list_url

    while current and current not in seen_list_urls and len(seen_list_urls) < MAX_LIST_PAGES:
        seen_list_urls.add(current)
        result.visited_list_urls.append(current)
        try:
            page = fetch_url(current, timeout=timeout)
        except FetchError as exc:
            result.failures.append(CrawlFailure(url=current, stage="list", reason=exc.reason, status=exc.status))
            break
        page_entries, next_url = parse_list_page(page.text, page.url)
        entries.extend(page_entries)
        current = next_url

    # 同一详情地址在多页中重复出现时只抓一次。
    seen_detail: set[str] = set()
    for entry in entries:
        if entry.detail_url in seen_detail:
            continue
        seen_detail.add(entry.detail_url)
        try:
            page = fetch_url(entry.detail_url, timeout=timeout)
        except FetchError as exc:
            result.failures.append(
                CrawlFailure(url=entry.detail_url, stage="detail", reason=exc.reason, status=exc.status)
            )
            continue
        try:
            result.plans.append(parse_detail_page(page.text, page.url, entry.list_url))
        except ValueError as exc:
            result.failures.append(CrawlFailure(url=entry.detail_url, stage="detail", reason=str(exc)))

    return result


# ---------------------------------------------------------------------------
# 报价变更采集所需的“严格完整快照”抓取
# ---------------------------------------------------------------------------

# 列表页签：详情解析结果必须与列表档案一致，否则视为列表项无法解析/解析冲突。
_LIST_SIGNATURE_FIELDS = ("plan_id", "name", "supplier", "regions", "base_fee_cny")


class IncompleteSnapshot(Exception):
    """抓取存在无法确认完整性的问题时抛出，携带问题地址清单。"""

    def __init__(self, issues: list["SnapshotIssue"]):
        super().__init__(f"快照不完整，共 {len(issues)} 个问题")
        self.issues = issues


@dataclass
class SnapshotIssue:
    url: str
    stage: str  # list | detail | pagination
    reason: str
    status: int | None = None


def _norm_text(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) else value


def list_entry_signature(entry: PlanListEntry) -> dict:
    """列表卡片上可核对的档案字段（供应商 / 地区 / 基本费）。"""
    return {
        "plan_id": entry.plan_id,
        "name": _norm_text(entry.name),
        "supplier": _norm_text(entry.supplier),
        "regions": sorted(entry.regions),
        "base_fee_cny": entry.base_fee_cny,
    }


def _detail_signature(detail: PlanDetail) -> dict:
    return {
        "plan_id": detail.plan_id,
        "name": _norm_text(detail.name),
        "supplier": _norm_text(detail.supplier),
        "regions": sorted(detail.regions),
        "base_fee_cny": None if detail.base_fee_gap else detail.base_fee_cny,
    }


def _same_number(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b is None
    return abs(float(a) - float(b)) < 1e-9


def _signature_mismatch(list_sig: dict, detail_sig: dict) -> str | None:
    """比对列表与详情的关键档案字段，返回不一致描述或 None。"""
    for name in _LIST_SIGNATURE_FIELDS:
        left, right = list_sig.get(name), detail_sig.get(name)
        if name == "base_fee_cny":
            if not _same_number(left, right):
                return f"月基本费列表为 {left}，详情为 {right}"
        elif name == "regions":
            if set(left or []) != set(right or []):
                return f"地区标签不一致：列表 {left or []}，详情 {right or []}"
        elif left != right:
            return f"{name} 不一致：列表 {left!r}，详情 {right!r}"
    return None


def missing_key_fields(detail: PlanDetail) -> list[str]:
    """返回详情页缺失的关键字段名（套餐编号 / 供应商 / 地区 / 月基本费 / 峰谷档位）。"""
    missing: list[str] = []
    if not detail.plan_id:
        missing.append("plan_id")
    if not detail.supplier:
        missing.append("supplier")
    if not detail.regions:
        missing.append("regions")
    if detail.base_fee_gap or detail.base_fee_cny != detail.base_fee_cny:
        missing.append("base_fee_cny")
    if not detail.peak_tiers:
        missing.append("peak_tiers")
    if not detail.valley_tiers:
        missing.append("valley_tiers")
    return missing


def _tier_payload(tiers: list[RateTier]) -> list[dict]:
    return [
        {
            "index": tier.index,
            "lower_kwh": tier.lower_kwh,
            "upper_kwh": tier.upper_kwh,
            "rate_cny_per_kwh": tier.rate_cny_per_kwh,
        }
        for tier in tiers
    ]


def plan_payload(detail: PlanDetail, root_url: str | None = None) -> dict:
    """快照中保存的字段（与抓取地址分离，便于比较）。

    ``detail_url`` 保存抓取时的绝对地址用于回显；另存相对列表首页的
    ``detail_ref`` 专用于比较。同一报价源的列表地址可能因部署主机/端口或
    file:// 路径前缀不同而变化，直接用绝对地址比较会产生无关的“地址变更”。
    站点外或无法归一化的地址，``detail_ref`` 保留原值。
    """
    return {
        "plan_id": detail.plan_id,
        "name": detail.name,
        "supplier": detail.supplier,
        "regions": sorted(detail.regions),
        "base_fee_cny": detail.base_fee_cny,
        "peak_tiers": _tier_payload(detail.peak_tiers),
        "valley_tiers": _tier_payload(detail.valley_tiers),
        "detail_url": detail.detail_url,
        "detail_ref": relative_detail_ref(detail.detail_url, root_url),
    }


def relative_detail_ref(detail_url: str, root_url: str | None) -> str:
    """同一站点内返回相对列表首页的路径，否则返回原绝对地址。"""
    if not root_url:
        return detail_url
    base = urllib.parse.urlsplit(root_url)
    target = urllib.parse.urlsplit(detail_url)
    if base.scheme != target.scheme or base.netloc != target.netloc:
        return detail_url
    if not target.path.startswith(posixpath.dirname(base.path) or "/"):
        return detail_url
    rel = posixpath.relpath(target.path, posixpath.dirname(base.path) or "/")
    return rel + (("?" + target.query) if target.query else "") + \
        (("#" + target.fragment) if target.fragment else "")


def scrape_complete_snapshot(list_url: str, timeout: float = 8.0) -> tuple[list[PlanDetail], list[str]]:
    """严格整站采集：只有全部分页、全部列表项与全部详情都完整可解析时才返回。

    以下任一情况都会抛出 IncompleteSnapshot，并列出全部问题地址：
    - 列表页抓取失败或分页未到终页；
    - 列表卡片无法解析（缺少套餐编号/详情链接/供应商/地区/月基本费）；
    - 列表与详情的关键档案字段冲突；
    - 详情页抓取失败或缺少关键字段（含峰谷档位）；
    - 详情编号与列表编号不一致，或整站无任何套餐。
    """
    issues: list[SnapshotIssue] = []
    visited: list[str] = []
    seen_list_urls: set[str] = set()
    entries: list[PlanListEntry] = []
    current: str | None = list_url
    terminal = False
    announced_total: int | None = None

    while current:
        if current in seen_list_urls:
            issues.append(SnapshotIssue(url=current, stage="pagination", reason="分页出现重复地址，无法确认终页"))
            break
        if len(seen_list_urls) >= MAX_LIST_PAGES:
            issues.append(SnapshotIssue(url=current, stage="pagination", reason=f"分页超过 {MAX_LIST_PAGES} 页安全上限，未确认终页"))
            break
        seen_list_urls.add(current)
        visited.append(current)
        try:
            page = fetch_url(current, timeout=timeout)
        except FetchError as exc:
            issues.append(SnapshotIssue(url=current, stage="list", reason=exc.reason, status=exc.status))
            break
        try:
            page_entries, next_url, page_issues, pagination = parse_list_page_strict(page.text, page.url)
        except ValueError as exc:
            issues.append(SnapshotIssue(url=current, stage="list", reason=str(exc)))
            break
        issues.extend(page_issues)
        entries.extend(page_entries)
        page_no = pagination.get("page_no")
        total_pages = pagination.get("total_pages")
        if page_no is not None and page_no != len(visited):
            issues.append(
                SnapshotIssue(
                    url=current,
                    stage="pagination",
                    reason=f"列表页公布为第 {page_no} 页，实际为第 {len(visited)} 页，分页序号异常",
                )
            )
        if total_pages is not None:
            if announced_total is None:
                announced_total = total_pages
            elif announced_total != total_pages:
                issues.append(
                    SnapshotIssue(
                        url=current,
                        stage="pagination",
                        reason=f"各页公布的共 {announced_total} 页与 {total_pages} 页不一致",
                    )
                )
        if next_url is None:
            terminal = True
            if total_pages is not None and page_no is not None and page_no < total_pages:
                issues.append(
                    SnapshotIssue(
                        url=current,
                        stage="pagination",
                        reason=f"列表页公布共 {total_pages} 页，但第 {page_no} 页缺少下一页链接，分页未到终页",
                    )
                )
        current = next_url

    if not terminal and not issues:
        issues.append(SnapshotIssue(url=list_url, stage="pagination", reason="未找到终页标记，无法确认分页完整"))
    if terminal and announced_total is not None and len(visited) != announced_total and not issues:
        issues.append(
            SnapshotIssue(
                url=list_url,
                stage="pagination",
                reason=f"列表页公布共 {announced_total} 页，实际抓取 {len(visited)} 页，分页不完整",
            )
        )

    details: list[PlanDetail] = []
    seen_detail: set[str] = set()
    seen_plan_ids: set[str] = set()
    for entry in entries:
        if entry.plan_id in seen_plan_ids:
            issues.append(SnapshotIssue(url=entry.list_url, stage="list", reason=f"套餐编号 {entry.plan_id} 在列表页重复出现"))
            continue
        seen_plan_ids.add(entry.plan_id)
        if entry.detail_url in seen_detail:
            issues.append(SnapshotIssue(url=entry.detail_url, stage="detail", reason="同一详情地址被多个套餐引用"))
            continue
        seen_detail.add(entry.detail_url)
        try:
            page = fetch_url(entry.detail_url, timeout=timeout)
        except FetchError as exc:
            issues.append(SnapshotIssue(url=entry.detail_url, stage="detail", reason=exc.reason, status=exc.status))
            continue
        try:
            detail = parse_detail_page(page.text, page.url, entry.list_url)
        except ValueError as exc:
            issues.append(SnapshotIssue(url=entry.detail_url, stage="detail", reason=str(exc)))
            continue
        if detail.plan_id != entry.plan_id:
            issues.append(
                SnapshotIssue(
                    url=entry.detail_url,
                    stage="detail",
                    reason=f"详情套餐编号 {detail.plan_id or '(空)'} 与列表 {entry.plan_id} 不一致",
                )
            )
            continue
        missing = missing_key_fields(detail)
        if missing:
            issues.append(
                SnapshotIssue(url=entry.detail_url, stage="detail", reason="关键字段缺失：" + "、".join(missing))
            )
            continue
        mismatch = _signature_mismatch(list_entry_signature(entry), _detail_signature(detail))
        if mismatch:
            issues.append(SnapshotIssue(url=entry.detail_url, stage="list", reason=f"列表与详情不一致：{mismatch}"))
            continue
        details.append(detail)

    if not entries and not issues:
        issues.append(SnapshotIssue(url=list_url, stage="list", reason="列表页未发现任何套餐项"))

    if issues:
        raise IncompleteSnapshot(issues)
    return details, visited
