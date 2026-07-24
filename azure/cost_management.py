from collections import defaultdict
from datetime import datetime, timezone
from math import ceil
from urllib.parse import urlparse

import requests


MANAGEMENT_ENDPOINT = "https://management.azure.com"
COST_API_VERSION = "2025-03-01"
REQUEST_TIMEOUT_SECONDS = 25
MAX_PAGES = 20


class CostManagementError(RuntimeError):
    def __init__(self, message, status_code=None, error_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after = retry_after


class CostManagementUnsupportedError(CostManagementError):
    pass


def _error_message(status_code):
    messages = {
        400: "当前订阅类型暂不支持该费用查询",
        401: "Azure 身份验证失败，请更新该管理账户的凭据",
        403: "费用读取权限不足，请为服务主体授予订阅范围的 Cost Management Reader 权限",
        404: "当前订阅暂不支持 Cost Management 费用数据",
        429: "Azure 费用接口请求过于频繁，请稍后重试",
    }
    if status_code and status_code >= 500:
        return "Azure 费用服务暂时不可用，请稍后重试"
    return messages.get(status_code, "Azure 费用查询失败，请稍后重试")


def _safe_error_details(response):
    try:
        payload = response.json()
    except ValueError:
        return None, ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None, ""
    return error.get("code"), str(error.get("message") or "")


def _retry_after_seconds(response):
    value = response.headers.get("Retry-After")
    try:
        return max(1, ceil(float(value)))
    except (TypeError, ValueError):
        return None


def _validate_next_link(next_link):
    parsed = urlparse(next_link)
    if parsed.scheme != "https" or parsed.netloc.lower() != "management.azure.com":
        raise CostManagementError("Azure 费用接口返回了无效的分页地址")


def _post_dataset(session, url, token, body):
    headers = {
        "Authorization": "Bearer {}".format(token),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    columns = []
    rows = []
    next_url = url
    for _ in range(MAX_PAGES):
        try:
            response = session.post(
                next_url,
                headers=headers,
                json=body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise CostManagementError("无法连接 Azure 费用服务，请稍后重试") from error
        if response.status_code == 204:
            return columns, rows
        if not 200 <= response.status_code < 300:
            error_code, error_detail = _safe_error_details(response)
            error_class = CostManagementError
            message = _error_message(response.status_code)
            if response.status_code == 400 and (
                "cost management data is unavailable" in error_detail.lower()
                or "offer" in error_detail.lower() and "not supported" in error_detail.lower()
            ):
                error_class = CostManagementUnsupportedError
                message = "当前订阅报价不支持 Azure Cost Management 费用 API"
            raise error_class(
                message,
                status_code=response.status_code,
                error_code=error_code,
                retry_after=_retry_after_seconds(response) if response.status_code == 429 else None,
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise CostManagementError("Azure 费用接口返回了无法解析的数据") from error
        properties = payload.get("properties", {}) if isinstance(payload, dict) else {}
        page_columns = properties.get("columns") or []
        if not columns:
            columns = [column.get("name", "") for column in page_columns]
        rows.extend(properties.get("rows") or [])
        next_url = properties.get("nextLink") or payload.get("nextLink")
        if not next_url:
            return columns, rows
        _validate_next_link(next_url)
    raise CostManagementError("Azure 费用明细分页过多，请缩小查询范围")


def _rows_as_dicts(columns, rows):
    return [
        {columns[index]: value for index, value in enumerate(row) if index < len(columns)}
        for row in rows
    ]


def _value(row, *names):
    normalized = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in normalized:
            return normalized[name.lower()]
    return None


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _cost(row):
    return _number(_value(row, "Cost", "PreTaxCost", "totalCost"))


def _format_usage_date(value):
    if value in (None, ""):
        return None
    text = str(value)
    if text.endswith(".0"):
        text = text[:-2]
    compact = text.replace("-", "")[:8]
    try:
        return datetime.strptime(compact, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return text[:10]


def _resource_parts(resource_id):
    if not resource_id:
        return None, None, None
    parts = [part for part in str(resource_id).split("/") if part]
    lowered = [part.lower() for part in parts]
    resource_group = None
    resource_type = None
    if "resourcegroups" in lowered:
        index = lowered.index("resourcegroups")
        if index + 1 < len(parts):
            resource_group = parts[index + 1]
    if "providers" in lowered:
        index = lowered.index("providers")
        provider = parts[index + 1:index + 2]
        type_parts = parts[index + 2:-1:2]
        if provider or type_parts:
            resource_type = "/".join(provider + type_parts)
    return parts[-1] if parts else str(resource_id), resource_group, resource_type


def _query_url(subscription_id, operation):
    return (
        "{}/subscriptions/{}/providers/Microsoft.CostManagement/{}?api-version={}"
        .format(MANAGEMENT_ENDPOINT, subscription_id, operation, COST_API_VERSION)
    )


def _query_actual_cost(session, subscription_id, token):
    aggregation = {"totalCost": {"name": "Cost", "function": "Sum"}}
    daily_body = {
        "type": "ActualCost",
        "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "Daily",
            "aggregation": aggregation,
        },
    }
    detail_body = {
        "type": "ActualCost",
        "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "None",
            "aggregation": aggregation,
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
                {"type": "Dimension", "name": "ServiceName"},
            ],
        },
    }
    url = _query_url(subscription_id, "query")
    daily_columns, daily_rows = _post_dataset(session, url, token, daily_body)
    daily = _rows_as_dicts(daily_columns, daily_rows)
    try:
        detail_columns, detail_rows = _post_dataset(session, url, token, detail_body)
    except CostManagementError as error:
        if error.status_code == 429:
            raise
        return daily, None
    return daily, _rows_as_dicts(detail_columns, detail_rows)


def _currency(*datasets):
    for dataset in datasets:
        for row in dataset:
            value = _value(row, "Currency", "BillingCurrency")
            if value:
                return str(value)
    return "USD"


def _daily_summary(rows):
    daily = []
    for row in rows:
        date = _format_usage_date(_value(row, "UsageDate", "Date"))
        if date:
            daily.append({"date": date, "cost": _cost(row)})
    daily.sort(key=lambda item: item["date"])
    return daily


def _line_chart_data(daily):
    items = [dict(item) for item in daily]
    costs = [item["cost"] for item in items]
    min_cost = min([0] + costs)
    max_cost = max([0] + costs)
    cost_range = max_cost - min_cost
    zero_ratio = (max_cost / cost_range) if cost_range else 0.5
    for item in items:
        item["chart_ratio"] = (
            round((max_cost - item["cost"]) / cost_range, 6)
            if cost_range else 0.5
        )
    return items, min_cost, max_cost, zero_ratio


def _breakdowns(rows, total_cost):
    service_costs = defaultdict(float)
    resource_costs = defaultdict(float)
    resource_metadata = {}
    for row in rows:
        cost = _cost(row)
        service_name = str(_value(row, "ServiceName") or "其他服务")
        service_costs[service_name] += cost
        resource_id = _value(row, "ResourceId")
        name, resource_group, resource_type = _resource_parts(resource_id)
        if name:
            key = str(resource_id).lower()
            resource_costs[key] += cost
            resource_metadata[key] = {
                "name": name,
                "resource_group": resource_group or "—",
                "resource_type": resource_type or "其他资源",
            }

    def share(cost):
        return round(cost / total_cost * 100, 1) if total_cost > 0 else 0

    services = [
        {"name": name, "cost": cost, "share": share(cost)}
        for name, cost in sorted(service_costs.items(), key=lambda item: item[1], reverse=True)
    ]
    resources = []
    for key, cost in sorted(resource_costs.items(), key=lambda item: item[1], reverse=True):
        item = dict(resource_metadata[key])
        item.update(cost=cost, share=share(cost))
        resources.append(item)
    return services[:10], resources[:10]


def get_cost_overview(subscription_id, credential, session=None):
    """读取订阅本月费用；Credit 余额不属于 Cost Management 返回范围。"""
    token = credential.get_token("https://management.azure.com/.default").token
    request_session = session or requests.Session()
    daily_rows, detail_rows = _query_actual_cost(request_session, subscription_id, token)
    warnings = []
    if detail_rows is None:
        detail_rows = []
        warnings.append("费用汇总已加载，但当前订阅未返回服务和资源明细")
    daily = _daily_summary(daily_rows)
    month_to_date = sum(item["cost"] for item in daily)
    if not daily and detail_rows:
        month_to_date = sum(_cost(row) for row in detail_rows)
    peak_daily = max(daily, key=lambda item: item["cost"], default=None)
    chart_daily, chart_min_cost, chart_max_cost, chart_zero_ratio = (
        _line_chart_data(daily[-14:])
    )
    services, resources = _breakdowns(detail_rows, month_to_date)
    return {
        "currency": _currency(daily_rows, detail_rows),
        "month_to_date": month_to_date,
        "latest_daily_cost": daily[-1]["cost"] if daily else 0,
        "data_through": daily[-1]["date"] if daily else None,
        "peak_daily_cost": peak_daily["cost"] if peak_daily else 0,
        "peak_daily_date": peak_daily["date"] if peak_daily else None,
        "daily": chart_daily,
        "chart_min_cost": chart_min_cost,
        "chart_max_cost": chart_max_cost,
        "chart_zero_ratio": chart_zero_ratio,
        "services": services,
        "resources": resources,
        "warnings": warnings,
        "is_empty": not daily and not detail_rows,
        "fetched_at": datetime.now(timezone.utc),
    }
