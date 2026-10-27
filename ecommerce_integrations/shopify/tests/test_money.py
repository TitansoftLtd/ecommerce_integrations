# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

from unittest.mock import patch

from frappe.tests import UnitTestCase

from ecommerce_integrations.shopify.money import (
	ShopifyCurrencyMismatchError,
	amount_from_row,
	discount_amount_from_allocation,
	get_company_currency,
	money_amount,
	resolve_money_side,
)
from ecommerce_integrations.shopify.order import (
	_get_line_price_and_discount,
	update_taxes_with_shipping_lines,
)


def _price_set(shop_amount: str, shop_currency: str, presentment_amount: str, presentment_currency: str):
	return {
		"shop_money": {"amount": shop_amount, "currency_code": shop_currency},
		"presentment_money": {"amount": presentment_amount, "currency_code": presentment_currency},
	}


DUAL_ORDER = {
	"id": 18951335411997,
	"name": "#1009",
	"currency": "KES",
	"presentment_currency": "ZAR",
	"taxes_included": False,
	"line_items": [
		{
			"id": 1,
			"name": "Test Item (Bag)",
			"quantity": 1,
			"price": "2026.84",
			"price_set": _price_set("2026.84", "KES", "259.00", "ZAR"),
			"product_exists": True,
			"sku": "Test Item",
			"tax_lines": [
				{
					"title": "VAT",
					"price": "0.00",
					"rate": 0.0,
					"price_set": _price_set("0.00", "KES", "0.00", "ZAR"),
				}
			],
			"discount_allocations": [
				{
					"amount": "100.00",
					"amount_set": _price_set("100.00", "KES", "12.78", "ZAR"),
				}
			],
		}
	],
	"shipping_lines": [
		{
			"title": "164 Bree St",
			"price": "50.00",
			"price_set": _price_set("50.00", "KES", "6.39", "ZAR"),
			"discount_allocations": [],
			"tax_lines": [],
		}
	],
}


class TestShopifyMoney(UnitTestCase):
	def test_resolve_presentment_when_company_matches_presentment(self):
		with patch(
			"ecommerce_integrations.shopify.money.get_company_currency",
			return_value="ZAR",
		):
			self.assertEqual(resolve_money_side(DUAL_ORDER, "Siafu Home (PTY) Ltd"), "presentment")

	def test_resolve_shop_when_company_matches_shop(self):
		with patch(
			"ecommerce_integrations.shopify.money.get_company_currency",
			return_value="KES",
		):
			self.assertEqual(resolve_money_side(DUAL_ORDER, "Gladys Abode Limited"), "shop")

	def test_resolve_prefers_shop_when_both_match(self):
		order = {**DUAL_ORDER, "currency": "ZAR", "presentment_currency": "ZAR"}
		with patch(
			"ecommerce_integrations.shopify.money.get_company_currency",
			return_value="ZAR",
		):
			self.assertEqual(resolve_money_side(order, "Siafu Home (PTY) Ltd"), "shop")

	def test_resolve_mismatch_raises(self):
		with patch(
			"ecommerce_integrations.shopify.money.get_company_currency",
			return_value="USD",
		):
			with self.assertRaises(ShopifyCurrencyMismatchError):
				resolve_money_side(DUAL_ORDER, "USD Co")

	def test_money_amount_sides(self):
		price_set = _price_set("2026.84", "KES", "259.00", "ZAR")
		self.assertEqual(money_amount(price_set, "shop"), 2026.84)
		self.assertEqual(money_amount(price_set, "presentment"), 259.0)
		self.assertEqual(money_amount(None, "shop", fallback="10"), 10.0)
		with self.assertRaises(ShopifyCurrencyMismatchError):
			money_amount(None, "presentment", fallback="10")

	def test_line_price_uses_presentment(self):
		line = DUAL_ORDER["line_items"][0]
		price, discount = _get_line_price_and_discount(line, False, money_side="presentment")
		self.assertEqual(price, 259.0)
		self.assertEqual(discount, 12.78)

	def test_line_price_uses_shop(self):
		line = DUAL_ORDER["line_items"][0]
		price, discount = _get_line_price_and_discount(line, False, money_side="shop")
		self.assertEqual(price, 2026.84)
		self.assertEqual(discount, 100.0)

	def test_shipping_and_tax_use_money_side(self):
		setting = type("S", (), {"add_shipping_as_item": 0, "shipping_item": None, "cost_center": "CC", "company": "Co"})()
		company_config = type(
			"C", (), {"cost_center": "CC", "company": "Co", "default_warehouse": "WH"}
		)()
		taxes = []
		items = [{"delivery_date": "2026-10-16"}]
		with patch(
			"ecommerce_integrations.shopify.order.get_tax_account_head",
			return_value="Tax - Co",
		), patch(
			"ecommerce_integrations.shopify.order.get_tax_account_description",
			return_value=None,
		):
			update_taxes_with_shipping_lines(
				taxes,
				DUAL_ORDER["shipping_lines"],
				setting,
				items,
				company_config=company_config,
				money_side="presentment",
			)
		self.assertEqual(len(taxes), 1)
		self.assertEqual(taxes[0]["tax_amount"], 6.39)

	def test_discount_allocation_helper(self):
		alloc = DUAL_ORDER["line_items"][0]["discount_allocations"][0]
		self.assertEqual(discount_amount_from_allocation(alloc, "presentment"), 12.78)
		self.assertEqual(amount_from_row(DUAL_ORDER["line_items"][0], "shop"), 2026.84)

	def test_get_company_currency_normalizes(self):
		with patch("frappe.get_cached_value", return_value="zar"):
			self.assertEqual(get_company_currency("Co"), "ZAR")
