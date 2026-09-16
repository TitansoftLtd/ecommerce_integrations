# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import json
from collections import defaultdict

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from frappe.utils import cint, cstr, flt, getdate, nowdate

from ecommerce_integrations.shopify.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	REFUND_ID_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.product import get_item_code
from ecommerce_integrations.shopify.utils import create_shopify_log

RESTOCK_RETURN_TYPE = "return"


def prepare_refund(payload, request_id=None):
	frappe.set_user("Administrator")
	setting = frappe.get_doc(SETTING_DOCTYPE)
	frappe.flags.request_id = request_id

	try:
		if not cint(setting.sync_shopify_refunds) and not cint(setting.sync_shopify_returns):
			create_shopify_log(status="Invalid", message="Refund and return sync are disabled.")
			return

		sync_refund(payload, setting)
		create_shopify_log(status="Success")
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def sync_refund(refund, setting):
	refund_id = cstr(refund.get("id"))
	order_id = cstr(refund.get("order_id"))

	if cint(setting.sync_shopify_refunds):
		create_credit_note_from_refund(refund, setting, refund_id, order_id)

	if cint(setting.sync_shopify_returns) and refund_requires_restock(refund):
		from ecommerce_integrations.shopify.returns import create_return_delivery_note_from_refund

		create_return_delivery_note_from_refund(refund, setting)


def refund_requires_restock(refund) -> bool:
	"""True when Shopify restocks fulfilled goods (restock_type=return)."""
	for row in refund.get("refund_line_items") or []:
		if row.get("restock_type") == RESTOCK_RETURN_TYPE:
			return True
	return False


def create_credit_note_from_refund(refund, setting, refund_id, order_id):
	if frappe.db.exists("Sales Invoice", {REFUND_ID_FIELD: refund_id, "docstatus": 1}):
		return

	si_name = frappe.db.get_value(
		"Sales Invoice",
		{ORDER_ID_FIELD: order_id, "docstatus": 1, "is_return": 0},
		"name",
	)
	if not si_name:
		frappe.throw(f"Sales Invoice not found for Shopify order {order_id}")

	qty_by_item = get_refund_qty_by_item_code(refund)
	shipping_refund_amount = get_shipping_refund_amount(refund)

	credit_note = make_sales_return(si_name)
	credit_note.update_stock = 0
	credit_note.set(REFUND_ID_FIELD, refund_id)
	credit_note.set(ORDER_ID_FIELD, order_id)
	credit_note.set(ORDER_NUMBER_FIELD, frappe.db.get_value("Sales Invoice", si_name, ORDER_NUMBER_FIELD))
	credit_note.naming_series = setting.sales_invoice_series or credit_note.naming_series
	credit_note.set_posting_time = 1
	credit_note.posting_date = getdate(refund.get("created_at")) or nowdate()
	credit_note.flags.ignore_pricing_rule = True
	credit_note.flags.ignore_mandatory = True

	apply_refund_quantities(credit_note, qty_by_item, setting, shipping_refund_amount)

	if not credit_note.items:
		frappe.throw(f"No returnable items found on Sales Invoice {si_name} for refund {refund_id}")

	credit_note.insert(ignore_mandatory=True)
	credit_note.submit()

	refund_amount = get_refund_transaction_amount(refund)
	if refund_amount > 0:
		make_payment_entry_against_credit_note(credit_note, setting, refund_amount, refund_id)


def get_refund_qty_by_item_code(refund) -> dict[str, float]:
	qty_map: dict[str, float] = defaultdict(float)
	for row in refund.get("refund_line_items") or []:
		line_item = row.get("line_item") or {}
		item_code = get_item_code(line_item)
		if item_code:
			qty_map[item_code] += flt(row.get("quantity"))
	return dict(qty_map)


def get_shipping_refund_amount(refund) -> float:
	total = 0.0
	for row in refund.get("refund_shipping_lines") or []:
		amount = row.get("amount")
		if amount is None and isinstance(row.get("subtotal_amount_set"), dict):
			amount = (row["subtotal_amount_set"].get("shop_money") or {}).get("amount")
		total += flt(amount)
	return total


def get_refund_transaction_amount(refund) -> float:
	total = 0.0
	for txn in refund.get("transactions") or []:
		if txn.get("kind") == "refund" and txn.get("status") == "success":
			total += flt(txn.get("amount"))
	return total


def apply_refund_quantities(credit_note, qty_by_item, setting, shipping_refund_amount):
	remaining = defaultdict(float, qty_by_item or {})
	shipping_item = setting.shipping_item if cint(setting.add_shipping_as_item) else None

	original_item_amount = abs(sum(flt(d.amount) for d in credit_note.items)) or 1.0
	kept = []

	for item in credit_note.items:
		if shipping_item and item.item_code == shipping_item and shipping_refund_amount:
			item.qty = -1 if abs(flt(item.qty)) >= 1 else -abs(flt(item.qty))
			item.rate = shipping_refund_amount
			shipping_refund_amount = 0
			kept.append(item)
			continue

		need = remaining.get(item.item_code, 0)
		if need <= 0:
			continue

		take = min(abs(flt(item.qty)), need)
		if take <= 0:
			continue

		item.qty = -take
		remaining[item.item_code] -= take
		kept.append(item)

	if not kept and shipping_item and shipping_refund_amount:
		for item in credit_note.items:
			if item.item_code == shipping_item:
				item.qty = -1
				item.rate = shipping_refund_amount
				kept.append(item)
				break

	# Full refund with empty qty map: keep make_sales_return items as-is
	if not kept and not qty_by_item:
		kept = list(credit_note.items)

	credit_note.items = kept

	new_item_amount = abs(sum(flt(d.amount) for d in credit_note.items))
	ratio = new_item_amount / original_item_amount if original_item_amount else 1.0

	for tax in credit_note.taxes:
		if tax.charge_type == "Actual":
			tax.tax_amount = flt(tax.tax_amount) * ratio
			# ERPNext v16 moved item-wise tax off Sales Taxes and Charges onto the parent
			# table `item_wise_tax_details`. Older stubs still mention item_wise_tax_detail.
			detail = tax.get("item_wise_tax_detail")
			if detail:
				if isinstance(detail, str):
					detail = json.loads(detail)
				scaled = {}
				for item_code, values in detail.items():
					if isinstance(values, list | tuple) and len(values) >= 2:
						scaled[item_code] = [values[0], flt(values[1]) * ratio]
					else:
						scaled[item_code] = values
				tax.set("item_wise_tax_detail", json.dumps(scaled))

	_reset_item_wise_tax_details(credit_note)
	credit_note.calculate_taxes_and_totals()


def _reset_item_wise_tax_details(credit_note):
	"""Clear parent item_wise_tax_details so totals can rebuild after item filter (ERPNext v16)."""
	if not credit_note.meta.get_field("item_wise_tax_details"):
		return

	credit_note.set("item_wise_tax_details", [])
	credit_note.set("_item_wise_tax_details", [])
	credit_note.update_item_wise_tax_details = True


def make_payment_entry_against_credit_note(credit_note, setting, refund_amount, refund_id):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	# Reload so outstanding/grand_total match DB after submit
	credit_note.reload()

	payment_entry = get_payment_entry(
		credit_note.doctype, credit_note.name, bank_account=setting.cash_bank_account
	)
	payment_entry.flags.ignore_mandatory = True
	payment_entry.reference_no = cstr(refund_id)
	payment_entry.posting_date = credit_note.posting_date or nowdate()
	payment_entry.reference_date = payment_entry.posting_date

	# Credit notes have negative outstanding. Allocation must stay negative (Pay).
	# Cap absolute pay-out to the Shopify refund transaction total when the CN is larger.
	outstanding = flt(credit_note.outstanding_amount)
	if outstanding < 0 and payment_entry.references:
		shopify_pay = flt(refund_amount)
		max_pay = abs(outstanding)
		pay_amount = min(shopify_pay, max_pay) if shopify_pay else max_pay
		allocated = -pay_amount
		ref = payment_entry.references[0]
		ref.allocated_amount = allocated
		ref.outstanding_amount = outstanding
		payment_entry.paid_amount = pay_amount
		payment_entry.received_amount = pay_amount
		if hasattr(payment_entry, "set_amounts"):
			payment_entry.set_amounts()

	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
