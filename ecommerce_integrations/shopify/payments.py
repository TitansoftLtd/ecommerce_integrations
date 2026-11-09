# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

"""Shopify order transactions → ERPNext Payment Entry via Mode of Payment."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, nowdate
from shopify.resources import Transaction

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import TRANSACTION_ID_FIELD
from ecommerce_integrations.shopify.money import MoneySide, money_amount, resolve_money_side

SUCCESSFUL_PAYMENT_KINDS = frozenset({"sale", "capture"})
SUCCESSFUL_REFUND_KINDS = frozenset({"refund"})
SUCCESSFUL_STATUS = "success"


def normalize_gateway(gateway: str | None) -> str:
	return (gateway or "").strip().lower()


def build_gateway_mop_map(setting) -> dict[str, str]:
	"""Return normalized shopify_gateway → mode_of_payment from Shopify Setting."""
	mapping: dict[str, str] = {}
	rows = (
		setting.get("payment_gateway_map")
		if hasattr(setting, "get")
		else getattr(setting, "payment_gateway_map", None)
	)
	for row in rows or []:
		key = normalize_gateway(row.shopify_gateway)
		if not key or not row.mode_of_payment:
			continue
		if key in mapping and mapping[key] != row.mode_of_payment:
			frappe.throw(
				_("Shopify gateway {0} is mapped more than once with different Modes of Payment.").format(
					row.shopify_gateway
				)
			)
		mapping[key] = row.mode_of_payment
	return mapping


def resolve_mode_of_payment(gateway: str | None, setting) -> str:
	key = normalize_gateway(gateway)
	if not key:
		frappe.throw(_("Shopify transaction is missing a payment gateway."))

	mapping = build_gateway_mop_map(setting)
	mode_of_payment = mapping.get(key)
	if not mode_of_payment:
		frappe.throw(
			_(
				"Shopify gateway {0} is not mapped to a Mode of Payment. "
				"Add it under Shopify Setting → Payment Gateway Map."
			).format(gateway)
		)
	return mode_of_payment


def get_account_for_mode_of_payment(mode_of_payment: str, company: str) -> str:
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account

	result = get_bank_cash_account(mode_of_payment, company)
	account = result.get("account") if isinstance(result, dict) else result
	if not account:
		frappe.throw(
			_("Please set default Cash or Bank account in Mode of Payment {0} for company {1}.").format(
				mode_of_payment, company
			)
		)
	return account


def transaction_amount(txn: dict, money_side: MoneySide) -> float:
	"""Amount from a Shopify transaction for the company money side."""
	amount_set = txn.get("amount_set")
	if amount_set:
		return money_amount(amount_set, money_side, fallback=txn.get("amount"), field_label="amount_set")
	return flt(txn.get("amount"))


def _txn_has_positive_amount(txn: dict) -> bool:
	if flt(txn.get("amount")) > 0:
		return True
	amount_set = txn.get("amount_set") or {}
	if not isinstance(amount_set, dict):
		return False
	for key in ("shop_money", "presentment_money"):
		bag = amount_set.get(key) or {}
		if isinstance(bag, dict) and flt(bag.get("amount")) > 0:
			return True
	return False


def is_successful_payment_transaction(txn: dict) -> bool:
	kind = (txn.get("kind") or "").strip().lower()
	status = (txn.get("status") or "").strip().lower()
	if kind not in SUCCESSFUL_PAYMENT_KINDS or status != SUCCESSFUL_STATUS:
		return False
	return _txn_has_positive_amount(txn)


def is_successful_refund_transaction(txn: dict) -> bool:
	kind = (txn.get("kind") or "").strip().lower()
	status = (txn.get("status") or "").strip().lower()
	if kind not in SUCCESSFUL_REFUND_KINDS or status != SUCCESSFUL_STATUS:
		return False
	return _txn_has_positive_amount(txn)


def filter_successful_payment_transactions(transactions: list[dict] | None) -> list[dict]:
	return [t for t in (transactions or []) if is_successful_payment_transaction(t)]


def filter_successful_refund_transactions(transactions: list[dict] | None) -> list[dict]:
	return [t for t in (transactions or []) if is_successful_refund_transaction(t)]


@temp_shopify_session
def fetch_order_transactions(order_id: str | int) -> list[dict]:
	"""GET Admin API order transactions for the given Shopify order id."""
	order_id = cstr(order_id)
	collected: list[dict] = []
	for txn in Transaction.find(order_id=order_id):
		collected.append(txn.to_dict() if hasattr(txn, "to_dict") else dict(txn.attributes))
	return collected


def payment_entry_exists_for_transaction(transaction_id: str | int) -> bool:
	txn_id = cstr(transaction_id)
	if not txn_id:
		return False
	if frappe.db.exists("Payment Entry", {TRANSACTION_ID_FIELD: txn_id, "docstatus": 1}):
		return True
	# Fallback for installs before custom field migrate / older PEs
	return bool(
		frappe.db.exists(
			"Payment Entry",
			{"reference_no": txn_id, "docstatus": 1},
		)
	)


def _apply_receive_allocation(payment_entry, amount: float, outstanding: float) -> float:
	"""Set PE allocation for a Receive against SI; return allocated amount."""
	allocatable = min(flt(amount), max(flt(outstanding), 0))
	if allocatable <= 0:
		return 0.0

	if payment_entry.references:
		ref = payment_entry.references[0]
		ref.allocated_amount = allocatable
		ref.outstanding_amount = outstanding

	payment_entry.paid_amount = allocatable
	payment_entry.received_amount = allocatable
	if hasattr(payment_entry, "set_amounts"):
		payment_entry.set_amounts()
	return allocatable


def create_payment_entry_for_transaction(
	doc,
	setting,
	txn: dict,
	*,
	money_side: MoneySide,
	posting_date=None,
	payment_type: str = "Receive",
) -> str | None:
	"""Create one Payment Entry for a Shopify transaction. Returns PE name or None if skipped."""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	txn_id = cstr(txn.get("id"))
	if not txn_id:
		frappe.throw(_("Shopify transaction is missing an id."))

	if payment_entry_exists_for_transaction(txn_id):
		return None

	amount = transaction_amount(txn, money_side)
	if amount <= 0:
		return None

	doc.reload()
	outstanding = flt(doc.outstanding_amount)

	mode_of_payment = resolve_mode_of_payment(txn.get("gateway"), setting)
	bank_account = get_account_for_mode_of_payment(mode_of_payment, doc.company)

	if payment_type == "Receive":
		if outstanding <= 0:
			return None
		party_amount = min(amount, outstanding)
		payment_entry = get_payment_entry(
			doc.doctype, doc.name, party_amount=party_amount, bank_account=bank_account
		)
		_apply_receive_allocation(payment_entry, party_amount, outstanding)
	else:
		# Pay against credit note (negative outstanding)
		payment_entry = get_payment_entry(doc.doctype, doc.name, bank_account=bank_account)
		if outstanding < 0 and payment_entry.references:
			max_pay = abs(outstanding)
			pay_amount = min(amount, max_pay)
			ref = payment_entry.references[0]
			ref.allocated_amount = -pay_amount
			ref.outstanding_amount = outstanding
			payment_entry.paid_amount = pay_amount
			payment_entry.received_amount = pay_amount
			if hasattr(payment_entry, "set_amounts"):
				payment_entry.set_amounts()

	payment_entry.mode_of_payment = mode_of_payment
	payment_entry.flags.ignore_mandatory = True
	payment_entry.reference_no = txn_id
	payment_entry.posting_date = posting_date or nowdate()
	payment_entry.reference_date = payment_entry.posting_date
	if payment_entry.meta.get_field(TRANSACTION_ID_FIELD):
		payment_entry.set(TRANSACTION_ID_FIELD, txn_id)

	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
	return payment_entry.name


def create_payment_entries_for_invoice(
	sales_invoice,
	shopify_order: dict,
	setting,
	*,
	posting_date=None,
	transactions: list[dict] | None = None,
) -> list[str]:
	"""One PE per successful sale/capture transaction on the Shopify order."""
	order_id = shopify_order.get("id")
	if transactions is None:
		transactions = fetch_order_transactions(order_id)

	payable = filter_successful_payment_transactions(transactions)
	if not payable:
		frappe.throw(
			_(
				"No successful Shopify payment transactions (sale/capture) found for order {0}. "
				"Cannot create Payment Entry."
			).format(order_id)
		)

	money_side = resolve_money_side(shopify_order, sales_invoice.company)
	created: list[str] = []
	for txn in payable:
		name = create_payment_entry_for_transaction(
			sales_invoice,
			setting,
			txn,
			money_side=money_side,
			posting_date=posting_date,
			payment_type="Receive",
		)
		if name:
			created.append(name)
	return created


def create_refund_payment_entries(
	credit_note,
	refund: dict,
	setting,
	*,
	refund_amount: float | None = None,  # noqa: ARG001 — reserved; outstanding caps allocation
	money_side: MoneySide = "shop",
) -> list[str]:
	"""One Pay PE per successful refund transaction; MoP from transaction gateway."""
	transactions = filter_successful_refund_transactions(refund.get("transactions"))
	if not transactions:
		frappe.throw(
			_(
				"No successful Shopify refund transactions found for refund {0}. "
				"Cannot create Payment Entry."
			).format(refund.get("id"))
		)

	created: list[str] = []
	for txn in transactions:
		if not txn.get("gateway"):
			frappe.throw(
				_(
					"Shopify refund {0} has no payment gateway on its transactions. "
					"Cannot resolve Mode of Payment."
				).format(refund.get("id"))
			)
		name = create_payment_entry_for_transaction(
			credit_note,
			setting,
			txn,
			money_side=money_side,
			posting_date=credit_note.posting_date or nowdate(),
			payment_type="Pay",
		)
		if name:
			created.append(name)
	return created


def validate_payment_gateway_map(setting) -> None:
	"""Require gateway map when invoice sync is on."""
	if not setting.is_enabled() or not cint(setting.sync_sales_invoice):
		return

	rows = (
		setting.get("payment_gateway_map")
		if hasattr(setting, "get")
		else getattr(setting, "payment_gateway_map", None)
	) or []
	if not rows:
		frappe.throw(
			_(
				"Add at least one Payment Gateway Map row when Import Sales Invoice is enabled. "
				"Each Shopify transaction.gateway must map to a Mode of Payment."
			)
		)

	seen: set[str] = set()
	for row in rows:
		key = normalize_gateway(row.shopify_gateway)
		if not key:
			frappe.throw(_("Shopify Gateway is required on Payment Gateway Map rows."))
		if not row.mode_of_payment:
			frappe.throw(_("Mode of Payment is required on Payment Gateway Map rows."))
		if key in seen:
			frappe.throw(_("Duplicate Shopify gateway mapping: {0}").format(row.shopify_gateway))
		seen.add(key)
