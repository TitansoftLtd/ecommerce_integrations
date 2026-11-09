# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from ecommerce_integrations.shopify.payments import (
	build_gateway_mop_map,
	create_payment_entries_for_invoice,
	create_payment_entry_for_transaction,
	filter_successful_payment_transactions,
	normalize_gateway,
	payment_entry_exists_for_transaction,
	resolve_mode_of_payment,
	transaction_amount,
	validate_payment_gateway_map,
)


def _txn(
	txn_id,
	*,
	kind="sale",
	status="success",
	gateway="shopify_payments",
	amount="100.00",
	amount_set=None,
):
	row = {
		"id": txn_id,
		"kind": kind,
		"status": status,
		"gateway": gateway,
		"amount": amount,
	}
	if amount_set is not None:
		row["amount_set"] = amount_set
	return row


class TestShopifyPaymentsMapping(UnitTestCase):
	def test_normalize_gateway(self):
		self.assertEqual(normalize_gateway(" Shopify_Payments "), "shopify_payments")
		self.assertEqual(normalize_gateway(None), "")

	def test_build_gateway_mop_map(self):
		setting = SimpleNamespace(
			payment_gateway_map=[
				SimpleNamespace(shopify_gateway="shopify_payments", mode_of_payment="Card"),
				SimpleNamespace(shopify_gateway="manual", mode_of_payment="Cash"),
			]
		)
		self.assertEqual(
			build_gateway_mop_map(setting),
			{"shopify_payments": "Card", "manual": "Cash"},
		)

	def test_resolve_mode_of_payment_case_insensitive(self):
		setting = SimpleNamespace(
			payment_gateway_map=[
				SimpleNamespace(shopify_gateway="Shopify_Payments", mode_of_payment="Card"),
			]
		)
		self.assertEqual(resolve_mode_of_payment("shopify_payments", setting), "Card")

	def test_unknown_gateway_raises(self):
		setting = SimpleNamespace(
			payment_gateway_map=[
				SimpleNamespace(shopify_gateway="shopify_payments", mode_of_payment="Card"),
			]
		)
		with self.assertRaises(frappe.ValidationError):
			resolve_mode_of_payment("mpesa", setting)

	def test_validate_payment_gateway_map_required_when_invoice_sync(self):
		setting = SimpleNamespace(
			enable_shopify=1,
			sync_sales_invoice=1,
			payment_gateway_map=[],
			is_enabled=lambda: True,
		)
		with self.assertRaises(frappe.ValidationError):
			validate_payment_gateway_map(setting)

	def test_validate_duplicate_gateway(self):
		setting = SimpleNamespace(
			enable_shopify=1,
			sync_sales_invoice=1,
			payment_gateway_map=[
				SimpleNamespace(shopify_gateway="manual", mode_of_payment="Cash"),
				SimpleNamespace(shopify_gateway="Manual", mode_of_payment="Cash"),
			],
			is_enabled=lambda: True,
		)
		with self.assertRaises(frappe.ValidationError):
			validate_payment_gateway_map(setting)


class TestShopifyPaymentsFilter(UnitTestCase):
	def test_filter_successful_sale_and_capture(self):
		txns = [
			_txn(1, kind="sale", status="success", amount="50"),
			_txn(2, kind="capture", status="success", amount="25"),
			_txn(3, kind="authorization", status="success", amount="75"),
			_txn(4, kind="sale", status="failure", amount="10"),
			_txn(5, kind="refund", status="success", amount="5"),
			_txn(6, kind="sale", status="success", amount="0"),
		]
		filtered = filter_successful_payment_transactions(txns)
		self.assertEqual([t["id"] for t in filtered], [1, 2])

	def test_transaction_amount_from_amount_set(self):
		txn = _txn(
			1,
			amount="100.00",
			amount_set={
				"shop_money": {"amount": "100.00", "currency_code": "KES"},
				"presentment_money": {"amount": "12.00", "currency_code": "ZAR"},
			},
		)
		self.assertEqual(transaction_amount(txn, "shop"), 100.0)
		self.assertEqual(transaction_amount(txn, "presentment"), 12.0)


class TestShopifyPaymentsCreate(UnitTestCase):
	def _setting(self):
		return SimpleNamespace(
			payment_gateway_map=[
				SimpleNamespace(shopify_gateway="shopify_payments", mode_of_payment="Card"),
				SimpleNamespace(shopify_gateway="manual", mode_of_payment="Cash"),
			]
		)

	def _invoice(self, outstanding=150.0):
		doc = MagicMock()
		doc.doctype = "Sales Invoice"
		doc.name = "SI-TEST-1"
		doc.company = "Test Co"
		doc.outstanding_amount = outstanding
		doc.meta.get_field.return_value = True
		return doc

	def _make_pe_factory(self, pe_names, modes):
		def _make_pe(*args, **kwargs):
			pe = MagicMock()
			pe.name = f"PE-{len(pe_names) + 1}"
			pe.references = [MagicMock(allocated_amount=0, outstanding_amount=0)]
			pe.meta.get_field.return_value = True
			pe.mode_of_payment = None

			def _insert(**_kwargs):
				pe_names.append(pe.name)
				modes.append(pe.mode_of_payment)

			pe.insert.side_effect = _insert
			return pe

		return _make_pe

	@patch("ecommerce_integrations.shopify.payments.payment_entry_exists_for_transaction", return_value=False)
	@patch("ecommerce_integrations.shopify.payments.get_account_for_mode_of_payment", return_value="Bank - TC")
	def test_two_transactions_two_pes(self, _mock_acct, _mock_exists):
		pe_names: list[str] = []
		modes: list[str] = []
		factory = self._make_pe_factory(pe_names, modes)

		with patch(
			"erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
			side_effect=factory,
		):
			invoice = self._invoice(outstanding=150)
			outstanding_seq = [150.0, 50.0]

			def _reload():
				if outstanding_seq:
					invoice.outstanding_amount = outstanding_seq.pop(0)

			invoice.reload.side_effect = _reload

			setting = self._setting()
			order = {"id": 99, "currency": "KES", "presentment_currency": "KES"}
			txns = [
				_txn(101, gateway="shopify_payments", amount="100.00"),
				_txn(102, gateway="manual", amount="50.00"),
			]

			with patch(
				"ecommerce_integrations.shopify.payments.resolve_money_side",
				return_value="shop",
			):
				created = create_payment_entries_for_invoice(
					invoice, order, setting, transactions=txns
				)

		self.assertEqual(created, ["PE-1", "PE-2"])
		self.assertEqual(modes, ["Card", "Cash"])

	@patch("ecommerce_integrations.shopify.payments.payment_entry_exists_for_transaction", return_value=True)
	@patch("ecommerce_integrations.shopify.payments.get_account_for_mode_of_payment", return_value="Bank - TC")
	def test_idempotent_skip(self, _mock_acct, _mock_exists):
		invoice = self._invoice()
		setting = self._setting()
		txn = _txn(101, amount="100.00")
		with patch(
			"erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry"
		) as mock_get_pe:
			name = create_payment_entry_for_transaction(
				invoice, setting, txn, money_side="shop", payment_type="Receive"
			)
			self.assertIsNone(name)
			mock_get_pe.assert_not_called()

	def test_empty_transactions_raises(self):
		invoice = self._invoice()
		setting = self._setting()
		order = {"id": 99, "currency": "KES", "presentment_currency": "KES"}
		with self.assertRaises(frappe.ValidationError):
			create_payment_entries_for_invoice(invoice, order, setting, transactions=[])

	def test_payment_entry_exists_checks_custom_field(self):
		with patch("frappe.db.exists", side_effect=[True]) as mock_exists:
			self.assertTrue(payment_entry_exists_for_transaction("555"))
			mock_exists.assert_called()
