import sys
import unittest
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "azure"))

import cost_management  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def dataset(columns, rows, next_link=None):
    properties = {
        "columns": [{"name": name, "type": "String"} for name in columns],
        "rows": rows,
    }
    if next_link:
        properties["nextLink"] = next_link
    return {"properties": properties}


class CostManagementTests(unittest.TestCase):
    def credential(self):
        return SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="access-token"))

    def test_overview_summarizes_daily_service_and_resource_costs(self):
        resource_id = (
            "/subscriptions/sub/resourceGroups/test-rg/providers/"
            "Microsoft.Compute/virtualMachines/test-vm"
        )
        session = FakeSession([
            FakeResponse(payload=dataset(
                ["Cost", "UsageDate", "Currency"],
                [[1.25, 20260701, "USD"], [2.75, 20260702, "USD"]],
            )),
            FakeResponse(payload=dataset(
                ["Cost", "ResourceId", "ServiceName", "Currency"],
                [[3.5, resource_id, "Virtual Machines", "USD"],
                 [0.5, resource_id, "Bandwidth", "USD"]],
            )),
        ])

        overview = cost_management.get_cost_overview(
            "subscription-id", self.credential(), session=session
        )

        self.assertEqual(overview["month_to_date"], 4.0)
        self.assertEqual(overview["latest_daily_cost"], 2.75)
        self.assertEqual(overview["data_through"], "2026-07-02")
        self.assertEqual(overview["peak_daily_cost"], 2.75)
        self.assertEqual(overview["peak_daily_date"], "2026-07-02")
        self.assertEqual(overview["chart_min_cost"], 0)
        self.assertEqual(overview["chart_max_cost"], 2.75)
        self.assertEqual(overview["chart_zero_ratio"], 1)
        self.assertAlmostEqual(overview["daily"][0]["chart_ratio"], 0.545455)
        self.assertEqual(overview["daily"][1]["chart_ratio"], 0)
        self.assertEqual(overview["services"][0]["name"], "Virtual Machines")
        self.assertEqual(overview["resources"][0]["name"], "test-vm")
        self.assertEqual(overview["resources"][0]["resource_group"], "test-rg")
        self.assertEqual(
            overview["resources"][0]["resource_type"],
            "Microsoft.Compute/virtualMachines",
        )
        self.assertFalse(overview["is_empty"])
        self.assertEqual(overview["fetched_at"].tzinfo, timezone.utc)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            session.calls[0][1]["headers"]["Authorization"],
            "Bearer access-token",
        )

    def test_empty_cost_data_is_a_normal_result(self):
        empty = FakeResponse(payload=dataset([], []))
        session = FakeSession([empty, empty])

        overview = cost_management.get_cost_overview(
            "subscription-id", self.credential(), session=session
        )

        self.assertTrue(overview["is_empty"])
        self.assertEqual(overview["month_to_date"], 0)
        self.assertEqual(overview["peak_daily_cost"], 0)
        self.assertIsNone(overview["peak_daily_date"])
        self.assertEqual(overview["chart_min_cost"], 0)
        self.assertEqual(overview["chart_max_cost"], 0)
        self.assertEqual(overview["chart_zero_ratio"], 0.5)

    def test_line_chart_data_supports_negative_and_zero_costs(self):
        chart, min_cost, max_cost, zero_ratio = (
            cost_management._line_chart_data([
                {"date": "2026-07-01", "cost": -2.0},
                {"date": "2026-07-02", "cost": 0.0},
                {"date": "2026-07-03", "cost": 3.0},
            ])
        )

        self.assertEqual(min_cost, -2.0)
        self.assertEqual(max_cost, 3.0)
        self.assertEqual(zero_ratio, 0.6)
        self.assertEqual(
            [item["chart_ratio"] for item in chart],
            [1.0, 0.6, 0.0],
        )

    def test_overview_limits_resource_costs_to_ten(self):
        resource_rows = []
        for index in range(11):
            resource_rows.append([
                float(11 - index),
                (
                    "/subscriptions/sub/resourceGroups/test-rg/providers/"
                    "Microsoft.Compute/virtualMachines/vm-{}"
                ).format(index),
                "Virtual Machines",
                "USD",
            ])
        session = FakeSession([
            FakeResponse(payload=dataset(
                ["Cost", "UsageDate", "Currency"],
                [[66.0, 20260701, "USD"]],
            )),
            FakeResponse(payload=dataset(
                ["Cost", "ResourceId", "ServiceName", "Currency"],
                resource_rows,
            )),
        ])

        overview = cost_management.get_cost_overview(
            "subscription-id", self.credential(), session=session
        )

        self.assertEqual(len(overview["resources"]), 10)
        self.assertEqual(overview["resources"][0]["name"], "vm-0")
        self.assertEqual(overview["resources"][-1]["name"], "vm-9")

    def test_rate_limit_exposes_retry_after_seconds(self):
        session = FakeSession([
            FakeResponse(
                status_code=429,
                payload={"error": {"code": "TooManyRequests"}},
                headers={"Retry-After": "45"},
            ),
        ])

        with self.assertRaises(cost_management.CostManagementError) as raised:
            cost_management.get_cost_overview(
                "subscription-id", self.credential(), session=session
            )

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.retry_after, 45)

    def test_detail_rate_limit_is_not_silenced(self):
        session = FakeSession([
            FakeResponse(payload=dataset(
                ["Cost", "UsageDate", "Currency"],
                [[1.0, 20260701, "USD"]],
            )),
            FakeResponse(status_code=429, headers={"Retry-After": "70"}),
        ])

        with self.assertRaises(cost_management.CostManagementError) as raised:
            cost_management.get_cost_overview(
                "subscription-id", self.credential(), session=session
            )

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.retry_after, 70)

    def test_permission_error_has_actionable_message(self):
        session = FakeSession([
            FakeResponse(status_code=403, payload={"error": {"code": "AuthorizationFailed"}}),
        ])

        with self.assertRaises(cost_management.CostManagementError) as raised:
            cost_management.get_cost_overview(
                "subscription-id", self.credential(), session=session
            )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.error_code, "AuthorizationFailed")
        self.assertIn("Cost Management Reader", str(raised.exception))

    def test_unsupported_student_offer_is_identified(self):
        session = FakeSession([
            FakeResponse(status_code=400, payload={
                "error": {
                    "code": "BadRequest",
                    "message": (
                        "Cost management data is unavailable for subscription id. "
                        "The offer MS-AZR-0170P is not supported."
                    ),
                }
            }),
        ])

        with self.assertRaises(cost_management.CostManagementUnsupportedError) as raised:
            cost_management.get_cost_overview(
                "subscription-id", self.credential(), session=session
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("不支持 Azure Cost Management", str(raised.exception))

    def test_untrusted_pagination_url_is_rejected(self):
        session = FakeSession([
            FakeResponse(payload=dataset(
                ["Cost", "UsageDate", "Currency"],
                [[1.0, 20260701, "USD"]],
                next_link="https://example.com/steal-token",
            )),
        ])

        with self.assertRaises(cost_management.CostManagementError) as raised:
            cost_management.get_cost_overview(
                "subscription-id", self.credential(), session=session
            )

        self.assertIn("无效的分页地址", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
