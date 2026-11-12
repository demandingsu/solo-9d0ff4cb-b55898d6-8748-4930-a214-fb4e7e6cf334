# 连锁便利店电价套餐比较 API

能源采购员按门店未来 12 个月的峰谷用电预测，比较公开电价套餐的年度成本，辅助续签决策。
另提供**报价变更采集 API**：按报价源采集套餐快照，核对改价、上架、下架与详情地址变化。

- **Web 框架**：Python 3.11 + FastAPI（`app/main.py`）
- **抓取与计费核心**：仅依赖 Python 标准库（`urllib` + `html.parser` + `decimal`），离线可测
- **快照存储**：标准库 `sqlite3`，同一报价源并发采集时对比与提交严格一致
- **本地报价站**：随仓库提供三个纯静态站点，nginx 容器托管，无需外网
  - `quote-site/`：原有比较演示站
  - `quote-site-v1/`：报价版本 1（基线）
  - `quote-site-v2/`：报价版本 2（含改价、上架、下架、详情改址）
- **部署**：Docker Compose 一键启动 API + 三个报价站

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由：比较 API + 报价变更采集 API
│   ├── schemas.py     # 请求/响应 Pydantic 模型
│   ├── scraper.py     # 分页列表 + 套餐详情抓取解析（含严格完整快照模式）
│   ├── snapshots.py   # SQLite 快照仓库：原子对比与提交、字段差异
│   ├── fetcher.py     # http/https/file 统一抓取层与错误归一化
│   ├── html_dom.py    # 标准库实现的轻量 HTML DOM
│   └── billing.py     # 峰谷逐档计费、资格校验、年度汇总
├── quote-site-v1/     # 报价版本 1：P001-P005（基线）
├── quote-site-v2/     # 报价版本 2：P001 改价、P002 下架、P003 详情改址、
│                      #              P005 供应商变更、P006 上架
├── quote-site/        # 原有比较演示站（P001-P007）
├── tests/             # 标准库 unittest 离线测试（21 个用例）
├── demo/              # 比较请求与报价变更请求示例
├── Dockerfile
└── docker-compose.yml
```

## 快速开始（Docker Compose，推荐）

```bash
docker compose up --build
```

- 报价版本 1：http://localhost:8081/index.html
- 报价版本 2：http://localhost:8082/index.html
- 原有报价站：http://localhost:8080/index.html
- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 报价变更采集演示（容器内地址）

```bash
# 1) 首次完整采集版本 1 -> 建立基线（changes 为空）
curl -X POST http://localhost:8000/api/v1/quote-changes \
  -H "Content-Type: application/json" --data @demo/quote-change-v1.json

# 2) 采集版本 2 -> 返回与上次完整快照的字段差异
curl -X POST http://localhost:8000/api/v1/quote-changes \
  -H "Content-Type: application/json" --data @demo/quote-change-v2.json

# 3) 重复采集版本 2 -> status=unchanged，无变更记录
curl -X POST http://localhost:8000/api/v1/quote-changes \
  -H "Content-Type: application/json" --data @demo/quote-change-v2.json
```

初始化比较请求（原有功能）：

```bash
curl -X POST http://localhost:8000/api/v1/compare-plans \
  -H "Content-Type: application/json" \
  --data @demo/request.json
```

## 无 Docker 的离线运行

核心抓取/计费逻辑仅用标准库，可直接运行测试：

```bash
python3 -I -m unittest discover -s tests -t . -v
```

手动启动报价站与 API（需自行安装 `requirements.txt`）：

```bash
python3 -m http.server 8080 --directory quote-site
python3 -m http.server 8081 --directory quote-site-v1
python3 -m http.server 8082 --directory quote-site-v2
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 请求中将 list_url 改为 http://127.0.0.1:8080/index.html
# 也支持 file:// 绝对路径，完全离线：
#   "list_url": "file:///workspace/quote-site/index.html"
```

## 报价变更采集 API

`POST /api/v1/quote-changes`

| 字段 | 说明 |
| --- | --- |
| `source_key` | 报价源标识；同一标识的快照独立对比。如 `demo-quote-site` |
| `list_url` | 该报价源套餐列表首页 URL，支持 `http(s)://` 与 `file://` |
| `timeout_seconds` | 可选，单次抓取超时 1-30 秒，默认 8 |

处理规则：

1. **快照内容**：每个套餐保存编号、名称、供应商、地区集合、月基本费，以及峰段/谷段
   逐档费率（档位、起止电量、费率）和详情地址。
2. **首次完整采集**：只建立基线，`status=baseline_created`，`changes` 为空。
3. **差异比较**：与**该来源上次完整快照**按套餐编号比较，返回
   - `added`：本次新上架（带 `new_detail_url`）；
   - `removed`：本次已下架（带 `old_detail_url`）；
   - `changed`：字段变化，`field` 为 `supplier` / `regions` / `base_fee_cny` /
     `peak_tiers` / `valley_tiers` / `detail_url`，同时给出新旧详情地址。
4. **完整性与失败保护**：以下任一情况 `status=incomplete`，响应 `issues` 列出问题地址
   （`stage` 为 `list` / `detail` / `pagination`），**不提交新快照，原快照保留**，
   因此不会把解析失败误报为下架：
   - 列表项无法解析（缺套餐编号、详情链接、供应商、地区或月基本费）；
   - 分页未到终页（页面公布的总页数未抓满、缺少下一页链接、分页序号异常或重复）；
   - 详情抓取失败或详情编号与列表不一致；
   - 详情关键字段缺失（供应商、地区、月基本费、峰段或谷段费率表）；
   - 列表与详情的供应商/地区/月基本费互相冲突。
5. **幂等**：内容与上次完整快照完全一致时 `status=unchanged`，不写入新快照、不产生变更记录。
   详情地址按“相对列表首页的站点内路径”比较，避免部署主机或 `file://` 路径前缀变化
   造成无关的地址变更。
6. **并发一致**：同一 `source_key` 的并发采集在 SQLite `BEGIN IMMEDIATE` 写事务内完成
   “读取上次快照 → 比较 → 提交”，同一来源的对比与提交不会交错；同一内容并发采集
   恰好一次建立基线，其余均为 `unchanged`。

响应结构（摘要）：

```json
{
  "source_key": "demo-quote-site",
  "status": "changed",
  "snapshot_id": 2,
  "previous_snapshot_id": 1,
  "plan_count": 5,
  "plans": [ { "plan_id": "P001", "supplier": "安能电力", "base_fee_cny": 65.0,
               "regions": ["上海"], "peak_tiers": [...], "valley_tiers": [...],
               "detail_url": "http://quote-site-v2/plans/p001.html" } ],
  "changes": [
    { "plan_id": "P001", "change_type": "changed", "field": "base_fee_cny",
      "old_value": 60.0, "new_value": 65.0,
      "old_detail_url": ".../p001.html", "new_detail_url": ".../p001.html" },
    { "plan_id": "P006", "change_type": "added", "field": null,
      "old_value": null, "new_value": {...}, "new_detail_url": ".../p006.html" },
    { "plan_id": "P002", "change_type": "removed", "field": null,
      "old_value": {...}, "new_value": null, "old_detail_url": ".../p002.html" }
  ],
  "issues": []
}
```

不完整采集时：`status=incomplete`，`changes` 为空，`issues` 非空，`snapshot_id` 仍为
上次完整快照编号（若存在），`plans` 返回保留的原快照内容。

快照数据库路径由 `SNAPSHOT_DB_PATH` 指定（默认 `./data/quote_snapshots.sqlite3`），
Compose 中挂载命名卷 `/data` 持久化。

## 请求格式

`POST /api/v1/compare-plans`

| 字段 | 说明 |
| --- | --- |
| `list_url` | 报价站套餐列表页 URL，支持 `http(s)://` 与 `file://` |
| `region` | 门店地区，需与套餐详情页地区标签精确匹配（如 `上海`） |
| `monthly_usage` | 恰好 12 条记录，每条含 `month`(1-12)、`peak_kwh`、`valley_kwh`（非负） |
| `timeout_seconds` | 可选，单次抓取超时 1-30 秒，默认 8 |

## 计费与筛选规则

1. **地区过滤**：套餐适用地区不包含请求地区 → 排除（`region_not_applicable`）。
2. **费率完整性**：缺峰段或谷段费率表、费率档位无法解析、月基本费缺失 → 排除
   （`missing_rates` / `invalid_tier_structure` / `missing_base_fee`）。
3. **档位覆盖校验**：任一月峰或谷用电量超过该费率表已公布的最高档位上限
   → 排除（`usage_exceeds_published_tiers`，响应中注明月份与段别）。无上限档（止为空）可承接任意电量。
4. **逐档计费**：每月峰、谷电量分别沿各自费率表按 `[下限, 上限)` 区间逐档计费，
   金额用 `Decimal` 保留两位小数（四舍五入），再加固定月基本费得月合计。
5. **年度汇总与排序**：12 个月月合计求和为年度成本，按从低到高排序返回。

## 抓取失败处理

- 列表页（含分页）或套餐详情抓取失败（连接错误、超时、HTTP 4xx/5xx、结构无法解析）时，
  在响应 `failures` 中列出失败地址、阶段（`list`/`detail`）、原因与 HTTP 状态码。
- 只要存在任何抓取失败，`conclusion.verification_status` 标记为 `pending_verification`（待核实）；
  全部成功且存在有效套餐时为 `verified`。无有效套餐时同样为 `pending_verification`。
- 分页跟随 `a[rel=next]`，并设有 50 页安全上限与去重，避免异常站点造成无限抓取。

## 演示数据覆盖的场景

| 套餐 | 地区 | 演示中的处理结果 |
| --- | --- | --- |
| P001 安能 峰谷A | 上海/江苏/浙江/华东 | 有效，参与排序 |
| P002 安能 峰谷B | 上海/华东 | 有效，参与排序 |
| P003 沪电 阶梯标准版 | 上海 | 有效，示例数据下年度最低价 |
| P004 通商 华北套餐 | 北京/天津/华北 | 地区不匹配，排除 |
| P005 安能 A+ | 上海/江苏/华东 | 缺谷段费率，排除 |
| P006 沪电 封顶套餐 | 上海 | 第 1 月峰用电即超 150 kWh 封顶档，排除 |
| P007 安能 C | 上海/华东 | 详情页 404，进入 `failures`，最低价标为待核实 |

示例请求下的复核年度成本（元）：**P003 6899.40 < P001 7247.40 < P002 7842.00**。

### 两个报价版本的变更场景（`quote-site-v1` → `quote-site-v2`）

| 套餐 | 版本 2 的变化 | 采集结果 |
| --- | --- | --- |
| P001 安能 峰谷A | 月基本费 60 → 65，谷段 300+ 费率 0.55 → 0.52 | `changed`：`base_fee_cny`、`valley_tiers` |
| P002 安能 峰谷B | 从列表与详情入口下架（详情页文件保留） | `removed` |
| P003 沪电 阶梯标准版 | 详情地址 `p003.html` → `p003-v2.html` | `changed`：`detail_url` |
| P004 通商 华北套餐 | 无变化 | 不产生记录 |
| P005 安能 A+ | 供应商 安能电力 → 安能新能源 | `changed`：`supplier` |
| P006 沪电 小门店新套餐 | 新上架 | `added` |

## 响应结构（摘要）

```json
{
  "conclusion": {
    "lowest_plan_id": "P003",
    "lowest_plan_name": "沪电零售 · 峰谷阶梯标准版",
    "lowest_annual_total_cny": 6899.4,
    "verification_status": "pending_verification",
    "note": "存在抓取失败的列表页或套餐详情，最低价结论待核实"
  },
  "ranked_plans": [ { "rank": 1, "annual_total_cny": 6899.4,
                      "monthly_bills": [ { "peak_charges": [...], "valley_charges": [...] } ] } ],
  "excluded_plans": [ { "plan_id": "P006", "reason": "usage_exceeds_published_tiers", "detail": "..." } ],
  "failures": [ { "url": ".../p007.html", "stage": "detail", "reason": "HTTP 404", "status": 404 } ]
}
```

每个参与排序的套餐均带 `detail_url`（详情来源）与 `list_url`（发现该套餐的列表页），
月度账单逐档列出 charged_kwh 与 charge_cny，便于采购员复核。
