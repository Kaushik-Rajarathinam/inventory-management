"""
Tests for restocking API endpoints (recommendations and submitted orders).
"""
from datetime import datetime, timedelta

import pytest

import main


@pytest.fixture(autouse=True)
def clear_restocking_orders():
    """Submitted orders live in module-level memory; reset so tests stay independent."""
    main.restocking_orders.clear()
    yield
    main.restocking_orders.clear()


def recommend(client, budget):
    response = client.get(f"/api/restocking/recommendations?budget={budget}")
    assert response.status_code == 200
    return response.json()


class TestRestockingRecommendations:
    """Test suite for GET /api/restocking/recommendations."""

    def test_zero_budget_recommends_nothing(self, client):
        """Test that a zero budget yields no items and no spend."""
        data = recommend(client, 0)
        assert data["items"] == []
        assert data["total_cost"] == 0
        assert data["remaining_budget"] == 0

    def test_recommendation_structure(self, client):
        """Test that each recommended item has the expected fields and types."""
        data = recommend(client, 10000)
        assert len(data["items"]) > 0

        for item in data["items"]:
            assert isinstance(item["sku"], str)
            assert isinstance(item["name"], str)
            assert isinstance(item["quantity"], int)
            assert isinstance(item["shortfall"], int)
            assert isinstance(item["unit_cost"], (int, float))
            assert isinstance(item["lead_time_days"], int)
            assert isinstance(item["fully_covered"], bool)

    def test_total_cost_never_exceeds_budget(self, client):
        """Test that spend stays within the budget at several budget levels."""
        for budget in (100, 1000, 5000, 20000, 100000):
            data = recommend(client, budget)
            assert data["total_cost"] <= budget
            assert abs(data["remaining_budget"] - (budget - data["total_cost"])) < 0.01

    def test_total_cost_matches_line_items(self, client):
        """Test that total_cost equals the sum of quantity * unit_cost."""
        data = recommend(client, 20000)
        calculated = sum(i["quantity"] * i["unit_cost"] for i in data["items"])
        assert abs(data["total_cost"] - calculated) < 0.01

    def test_quantity_never_exceeds_shortfall(self, client):
        """Test that we never recommend buying more than the forecast shortfall."""
        data = recommend(client, 1000000)
        for item in data["items"]:
            assert 0 < item["quantity"] <= item["shortfall"]
            assert item["fully_covered"] == (item["quantity"] == item["shortfall"])

    def test_large_budget_covers_all_shortfalls(self, client):
        """Test that a budget above total_needed_cost covers every shortfall."""
        needed = recommend(client, 0)["total_needed_cost"]
        data = recommend(client, needed + 1)
        assert all(i["fully_covered"] for i in data["items"])
        assert abs(data["total_cost"] - needed) < 0.01

    def test_items_without_shortfall_are_excluded(self, client):
        """Test that items whose stock already covers the forecast are not recommended."""
        demand = client.get("/api/demand").json()
        covered = {
            f["item_sku"] for f in demand
            if f["current_stock"] is not None and f["forecasted_demand"] <= f["current_stock"]
        }
        data = recommend(client, 1000000)
        assert not covered & {i["sku"] for i in data["items"]}

    def test_higher_budget_never_spends_less(self, client):
        """Test that raising the budget does not reduce the amount recommended."""
        costs = [recommend(client, b)["total_cost"] for b in (0, 1000, 5000, 20000, 50000)]
        assert costs == sorted(costs)

    def test_negative_budget_rejected(self, client):
        """Test that a negative budget returns 400."""
        response = client.get("/api/restocking/recommendations?budget=-1")
        assert response.status_code == 400
        assert "detail" in response.json()

    def test_non_numeric_budget_rejected(self, client):
        """Test that a non-numeric budget fails validation."""
        response = client.get("/api/restocking/recommendations?budget=abc")
        assert response.status_code == 422


class TestRestockingOrders:
    """Test suite for POST/GET /api/restocking/orders."""

    def place_order(self, client, budget=20000):
        rec = recommend(client, budget)
        payload = {
            "budget": budget,
            "items": [{"sku": i["sku"], "quantity": i["quantity"]} for i in rec["items"]],
        }
        return rec, client.post("/api/restocking/orders", json=payload)

    def test_no_orders_initially(self, client):
        """Test that the submitted orders list starts empty."""
        response = client.get("/api/restocking/orders")
        assert response.status_code == 200
        assert response.json() == []

    def test_place_order(self, client):
        """Test that submitting recommended items creates an order."""
        rec, response = self.place_order(client)
        assert response.status_code == 201

        order = response.json()
        assert order["order_number"].startswith("RST-")
        assert order["status"] == "Submitted"
        assert len(order["items"]) == len(rec["items"])
        assert abs(order["total_value"] - rec["total_cost"]) < 0.01

    def test_lead_time_is_longest_item_lead_time(self, client):
        """Test that the order lead time is the maximum across its items."""
        rec, response = self.place_order(client)
        order = response.json()
        assert order["lead_time_days"] == max(i["lead_time_days"] for i in rec["items"])

    def test_expected_delivery_matches_lead_time(self, client):
        """Test that expected_delivery is submitted_date plus the lead time."""
        _, response = self.place_order(client)
        order = response.json()
        submitted = datetime.fromisoformat(order["submitted_date"])
        expected = datetime.fromisoformat(order["expected_delivery"])
        assert expected - submitted == timedelta(days=order["lead_time_days"])

    def test_order_appears_in_list_newest_first(self, client):
        """Test that submitted orders are listed with the newest first."""
        self.place_order(client, 5000)
        self.place_order(client, 10000)

        orders = client.get("/api/restocking/orders").json()
        assert len(orders) == 2
        assert orders[0]["order_number"].endswith("0002")
        assert orders[1]["order_number"].endswith("0001")

    def test_server_uses_its_own_prices(self, client):
        """Test that line prices come from the forecast data, not the client."""
        response = client.post(
            "/api/restocking/orders",
            json={"items": [{"sku": "WDG-001", "quantity": 10, "unit_price": 0.01}]},
        )
        assert response.status_code == 201
        assert response.json()["items"][0]["unit_price"] == 42.5
        assert response.json()["total_value"] == 425.0

    def test_empty_order_rejected(self, client):
        """Test that an order with no items returns 400."""
        response = client.post("/api/restocking/orders", json={"items": []})
        assert response.status_code == 400

    def test_unknown_sku_rejected(self, client):
        """Test that an unknown SKU returns 400."""
        response = client.post(
            "/api/restocking/orders", json={"items": [{"sku": "NOPE-000", "quantity": 1}]}
        )
        assert response.status_code == 400
        assert "NOPE-000" in response.json()["detail"]

    def test_non_positive_quantity_rejected(self, client):
        """Test that zero or negative quantities return 400."""
        for quantity in (0, -5):
            response = client.post(
                "/api/restocking/orders", json={"items": [{"sku": "WDG-001", "quantity": quantity}]}
            )
            assert response.status_code == 400

    def test_order_over_budget_rejected(self, client):
        """Test that an order costing more than the stated budget returns 400."""
        response = client.post(
            "/api/restocking/orders",
            json={"budget": 10, "items": [{"sku": "WDG-001", "quantity": 5}]},
        )
        assert response.status_code == 400
        assert "budget" in response.json()["detail"].lower()

    def test_rejected_order_is_not_stored(self, client):
        """Test that failed submissions do not appear in the orders list."""
        client.post("/api/restocking/orders", json={"items": []})
        assert client.get("/api/restocking/orders").json() == []
