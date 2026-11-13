# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from ecommerce_integrations.shopify.catalog_prices import (
	build_fixed_price_input,
	get_catalog_maps_for_price_list,
	get_enabled_catalog_maps,
	on_item_price_trash,
	on_item_price_update,
	parse_catalog_nodes,
	push_catalog_prices_for_item,
	run_catalog_price_push,
	validate_catalog_map_currencies,
	variant_gid,
)


SAMPLE_CATALOGS_PAYLOAD = {
	"data": {
		"catalogs": {
			"nodes": [
				{
					"id": "gid://shopify/MarketCatalog/1",
					"title": "Kenya",
					"status": "ACTIVE",
					"priceList": {
						"id": "gid://shopify/PriceList/10",
						"name": "Kenya PL",
						"currency": "KES",
					},
				},
				{
					"id": "gid://shopify/MarketCatalog/2",
					"title": "South Africa",
					"status": "ACTIVE",
					"priceList": {
						"id": "gid://shopify/PriceList/20",
						"name": "ZAR PL",
						"currency": "ZAR",
					},
				},
				{
					"id": "gid://shopify/MarketCatalog/3",
					"title": "Canada legacy",
					"status": "ACTIVE",
					"priceList": None,
				},
			]
		}
	}
}


def _map_row(**kwargs):
	defaults = {
		"enabled": 1,
		"shopify_catalog_id": "gid://shopify/MarketCatalog/1",
		"catalog_title": "Kenya",
		"price_list_id": "gid://shopify/PriceList/10",
		"currency": "KES",
		"erpnext_price_list": "Standard Selling KES",
	}
	defaults.update(kwargs)
	return SimpleNamespace(**defaults)


class TestCatalogParseAndValidate(UnitTestCase):
	def test_parse_catalog_nodes(self):
		rows = parse_catalog_nodes(SAMPLE_CATALOGS_PAYLOAD)
		self.assertEqual(len(rows), 3)
		self.assertEqual(rows[0]["currency"], "KES")
		self.assertEqual(rows[0]["price_list_id"], "gid://shopify/PriceList/10")
		self.assertEqual(rows[2]["price_list_id"], "")

	def test_variant_gid(self):
		self.assertEqual(variant_gid("123"), "gid://shopify/ProductVariant/123")
		self.assertEqual(
			variant_gid("gid://shopify/ProductVariant/9"),
			"gid://shopify/ProductVariant/9",
		)
		self.assertIsNone(variant_gid(""))

	def test_validate_currency_mismatch_raises(self):
		setting = SimpleNamespace(
			shopify_catalog_map=[_map_row(erpnext_price_list="PL-KES", currency="KES")]
		)
		with patch("frappe.db.get_value", return_value="ZAR"):
			with self.assertRaises(frappe.ValidationError):
				validate_catalog_map_currencies(setting)

	def test_validate_currency_match_ok(self):
		setting = SimpleNamespace(
			shopify_catalog_map=[_map_row(erpnext_price_list="PL-KES", currency="KES")]
		)
		with patch("frappe.db.get_value", return_value="KES"):
			validate_catalog_map_currencies(setting)

	def test_enabled_maps_require_both_price_lists(self):
		setting = SimpleNamespace(
			get=lambda _k: [
				_map_row(enabled=1, price_list_id="", erpnext_price_list="PL"),
				_map_row(enabled=0, price_list_id="gid://shopify/PriceList/10", erpnext_price_list="PL"),
				_map_row(enabled=1, price_list_id="gid://shopify/PriceList/10", erpnext_price_list="PL"),
			]
		)
		# Document-style get
		setting = MagicMock()
		setting.get.return_value = [
			_map_row(enabled=1, price_list_id="", erpnext_price_list="PL"),
			_map_row(enabled=0, price_list_id="gid://shopify/PriceList/10", erpnext_price_list="PL"),
			_map_row(enabled=1, price_list_id="gid://shopify/PriceList/10", erpnext_price_list="PL-KES"),
		]
		enabled = get_enabled_catalog_maps(setting)
		self.assertEqual(len(enabled), 1)
		self.assertEqual(enabled[0].erpnext_price_list, "PL-KES")


class TestCatalogPush(UnitTestCase):
	def _setting(self):
		setting = MagicMock()
		setting.is_enabled.return_value = True
		setting.get.return_value = [
			_map_row(
				shopify_catalog_id="gid://shopify/MarketCatalog/1",
				price_list_id="gid://shopify/PriceList/10",
				currency="KES",
				erpnext_price_list="PL-KES",
			),
			_map_row(
				shopify_catalog_id="gid://shopify/MarketCatalog/2",
				price_list_id="gid://shopify/PriceList/20",
				currency="ZAR",
				erpnext_price_list="PL-ZAR",
				catalog_title="South Africa",
			),
		]
		return setting

	@patch("ecommerce_integrations.shopify.catalog_prices.add_fixed_prices")
	@patch("ecommerce_integrations.shopify.catalog_prices.get_item_price_rate")
	@patch("ecommerce_integrations.shopify.catalog_prices.get_shopify_variant_id", return_value="555")
	def test_push_two_catalogs(self, _variant, mock_rate, mock_add):
		mock_rate.side_effect = lambda item, pl: {"PL-KES": 100.0, "PL-ZAR": 25.0}[pl]
		result = push_catalog_prices_for_item("ITEM-1", setting=self._setting())
		self.assertEqual(result["pushed"], 2)
		self.assertEqual(mock_add.call_count, 2)
		first_prices = mock_add.call_args_list[0].args[1]
		self.assertEqual(first_prices[0]["price"]["currencyCode"], "KES")
		self.assertEqual(first_prices[0]["price"]["amount"], "100.00")

	@patch("ecommerce_integrations.shopify.catalog_prices.add_fixed_prices")
	@patch("ecommerce_integrations.shopify.catalog_prices.get_item_price_rate", return_value=None)
	@patch("ecommerce_integrations.shopify.catalog_prices.get_shopify_variant_id", return_value="555")
	def test_missing_item_price_skips(self, _variant, _rate, mock_add):
		result = push_catalog_prices_for_item("ITEM-1", setting=self._setting())
		self.assertEqual(result["skipped"], 2)
		self.assertEqual(result["pushed"], 0)
		mock_add.assert_not_called()

	@patch("ecommerce_integrations.shopify.catalog_prices.add_fixed_prices")
	@patch("ecommerce_integrations.shopify.catalog_prices.get_shopify_variant_id", return_value=None)
	def test_unsynced_item_skipped(self, _variant, mock_add):
		result = push_catalog_prices_for_item("ITEM-1", setting=self._setting())
		self.assertEqual(result["skipped"], 1)
		mock_add.assert_not_called()

	@patch("ecommerce_integrations.shopify.catalog_prices.add_fixed_prices")
	@patch("ecommerce_integrations.shopify.catalog_prices.get_shopify_variant_id", return_value="555")
	def test_item_price_update_mapped_only(self, _variant, mock_add):
		setting = self._setting()
		with patch("frappe.get_cached_doc", return_value=setting):
			doc = SimpleNamespace(item_code="ITEM-1", price_list="PL-KES", price_list_rate=99)
			on_item_price_update(doc)
			mock_add.assert_called_once()
			self.assertEqual(mock_add.call_args.args[0], "gid://shopify/PriceList/10")

			mock_add.reset_mock()
			doc.price_list = "Unmapped PL"
			on_item_price_update(doc)
			mock_add.assert_not_called()

	@patch("ecommerce_integrations.shopify.catalog_prices.delete_fixed_prices")
	@patch("ecommerce_integrations.shopify.catalog_prices.get_shopify_variant_id", return_value="555")
	def test_item_price_trash_deletes(self, _variant, mock_delete):
		setting = self._setting()
		with patch("frappe.get_cached_doc", return_value=setting):
			doc = SimpleNamespace(item_code="ITEM-1", price_list="PL-ZAR", price_list_rate=10)
			on_item_price_trash(doc)
			mock_delete.assert_called_once_with(
				"gid://shopify/PriceList/20",
				["gid://shopify/ProductVariant/555"],
			)

	def test_build_fixed_price_input(self):
		payload = build_fixed_price_input("42", 12.5, "eur")
		self.assertEqual(
			payload,
			{
				"variantId": "gid://shopify/ProductVariant/42",
				"price": {"amount": "12.50", "currencyCode": "EUR"},
			},
		)

	@patch("ecommerce_integrations.shopify.catalog_prices.create_shopify_log")
	@patch("ecommerce_integrations.shopify.catalog_prices.add_fixed_prices")
	@patch("ecommerce_integrations.shopify.catalog_prices.get_shopify_variant_id")
	@patch("frappe.get_all")
	@patch("frappe.get_doc")
	def test_bulk_push_batches_and_skips_unsynced(
		self, mock_get_doc, mock_get_all, mock_variant, mock_add, mock_log
	):
		setting = self._setting()
		# Only first catalog map for simpler assert — trim to one
		setting.get.return_value = [
			_map_row(
				price_list_id="gid://shopify/PriceList/10",
				currency="KES",
				erpnext_price_list="PL-KES",
			)
		]
		mock_get_doc.return_value = setting
		mock_get_all.return_value = [
			SimpleNamespace(item_code="SYNCED", price_list_rate=50),
			SimpleNamespace(item_code="NOT-SYNCED", price_list_rate=60),
		]
		mock_variant.side_effect = lambda code: "111" if code == "SYNCED" else None

		run_catalog_price_push()

		mock_add.assert_called_once()
		prices = mock_add.call_args.args[1]
		self.assertEqual(len(prices), 1)
		self.assertEqual(prices[0]["variantId"], "gid://shopify/ProductVariant/111")
		mock_log.assert_called()
