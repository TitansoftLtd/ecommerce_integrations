# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from ecommerce_integrations.shopify.company_config import (
	CrossCompanyOrderError,
	collect_order_location_ids,
	default_line_warehouse,
	map_locations_to_companies,
	resolve_company_config_for_order,
)


def _fake_setting():
	return frappe._dict(
		{
			"company_config": [
				frappe._dict(
					{
						"company": "Co A",
						"is_primary": 1,
						"default_warehouse": "WH-A",
						"cash_bank_account": "Bank-A",
						"cost_center": "CC-A",
						"sales_order_series": "SO-",
					}
				),
				frappe._dict(
					{
						"company": "Co B",
						"is_primary": 0,
						"default_warehouse": "WH-B",
						"cash_bank_account": "Bank-B",
						"cost_center": "CC-B",
						"sales_order_series": "SO-",
					}
				),
			],
			"get_integration_to_erpnext_wh_mapping": lambda: {
				"111": "WH-A",
				"222": "WH-B",
			},
		}
	)


class TestShopifyCompanyConfigResolver(UnitTestCase):
	def test_primary_fallback_when_no_locations(self):
		setting = _fake_setting()
		with patch(
			"ecommerce_integrations.shopify.company_config.fetch_fulfillment_order_location_ids",
			return_value=[],
		):
			config, locations = resolve_company_config_for_order({"id": 1, "name": "#1001"}, setting)
		self.assertEqual(config.company, "Co A")
		self.assertTrue(config.is_primary)
		self.assertEqual(locations, [])

	def test_resolve_from_fulfillment_location(self):
		setting = _fake_setting()
		order = {
			"id": 2,
			"name": "#1002",
			"fulfillments": [{"location_id": "111"}],
		}
		with patch(
			"frappe.db.get_value",
			side_effect=lambda dt, name, field, **kwargs: "Co A" if dt == "Warehouse" else None,
		):
			config, locations = resolve_company_config_for_order(order, setting)
		self.assertEqual(config.company, "Co A")
		self.assertEqual(locations, ["111"])
		self.assertEqual(default_line_warehouse(config, locations, setting), "WH-A")

	def test_cross_company_locations_raise(self):
		setting = _fake_setting()
		with patch(
			"ecommerce_integrations.shopify.company_config.map_locations_to_companies",
			return_value={"Co A", "Co B"},
		):
			with self.assertRaises(CrossCompanyOrderError):
				resolve_company_config_for_order(
					{
						"id": 3,
						"name": "#1003",
						"fulfillments": [{"location_id": "111"}, {"location_id": "222"}],
					},
					setting,
				)

	def test_collect_order_location_ids(self):
		self.assertEqual(
			collect_order_location_ids({"fulfillments": [{"location_id": 11}, {"location_id": 22}]}),
			["11", "22"],
		)

	def test_map_locations_to_companies(self):
		setting = _fake_setting()

		def fake_get_value(dt, name, field, **kwargs):
			return {"WH-A": "Co A", "WH-B": "Co B"}.get(name)

		with patch("frappe.db.get_value", side_effect=fake_get_value):
			companies = map_locations_to_companies(["111", "missing"], setting)
		self.assertEqual(companies, {"Co A"})
