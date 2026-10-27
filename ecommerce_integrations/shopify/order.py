import json
from typing import Literal

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, nowdate
from shopify.collection import PaginatedIterator
from shopify.resources import Order

from ecommerce_integrations.shopify.company_config import (
	CrossCompanyOrderError,
	collect_order_location_ids,
	default_line_warehouse,
	resolve_company_config_for_order,
)
from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	CUSTOMER_ID_FIELD,
	EVENT_MAPPER,
	ORDER_DISCOUNT_CODES_FIELD,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.customer import ShopifyCustomer
from ecommerce_integrations.shopify.money import (
	MoneySide,
	ShopifyCurrencyMismatchError,
	amount_from_row,
	discount_amount_from_allocation,
	get_company_currency,
	resolve_money_side,
)
from ecommerce_integrations.shopify.product import create_items_if_not_exist, get_item_code
from ecommerce_integrations.shopify.utils import create_shopify_log
from ecommerce_integrations.utils.price_list import get_dummy_price_list
from ecommerce_integrations.utils.taxation import get_dummy_tax_category

DEFAULT_TAX_FIELDS = {
	"sales_tax": "default_sales_tax_account",
	"shipping": "default_shipping_charges_account",
}


def sync_sales_order(payload, request_id=None):
	order = payload
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	if frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: cstr(order["id"])}):
		create_shopify_log(status="Invalid", message="Sales order already exists, not synced")
		return
	try:
		shopify_customer = order.get("customer") if order.get("customer") is not None else {}
		shopify_customer["billing_address"] = order.get("billing_address", "")
		shopify_customer["shipping_address"] = order.get("shipping_address", "")
		customer_id = shopify_customer.get("id")
		if customer_id:
			customer = ShopifyCustomer(customer_id=customer_id)
			if not customer.is_synced():
				customer.sync_customer(customer=shopify_customer)
			else:
				customer.update_existing_addresses(shopify_customer)

		create_items_if_not_exist(order)

		setting = frappe.get_doc(SETTING_DOCTYPE)
		try:
			company_config, location_ids = resolve_company_config_for_order(order, setting)
		except CrossCompanyOrderError as e:
			create_shopify_log(status="Error", exception=e, rollback=True)
			return
		if not company_config:
			create_shopify_log(
				status="Error",
				exception=_("No Shopify Company Config configured"),
				rollback=True,
			)
			return
		create_order(order, setting, company_config=company_config, location_ids=location_ids)
	except (ShopifyCurrencyMismatchError, CrossCompanyOrderError) as e:
		create_shopify_log(status="Error", exception=e, rollback=True)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)
	else:
		create_shopify_log(status="Success")


def create_order(order, setting, company=None, company_config=None, location_ids=None):
	# local import to avoid circular dependencies
	from ecommerce_integrations.shopify.fulfillment import create_delivery_note
	from ecommerce_integrations.shopify.invoice import create_sales_invoice

	if company_config is None:
		if company:
			from ecommerce_integrations.shopify.company_config import get_company_config

			company_config = get_company_config(company, setting)
		else:
			company_config, location_ids = resolve_company_config_for_order(order, setting)

	so = create_sales_order(order, setting, company_config=company_config, location_ids=location_ids)
	if so:
		if order.get("financial_status") == "paid":
			create_sales_invoice(order, setting, so, company_config=company_config)

		if order.get("fulfillments"):
			create_delivery_note(order, setting, so, company_config=company_config)


def create_sales_order(shopify_order, setting, company=None, company_config=None, location_ids=None):
	customer = setting.default_customer
	if shopify_order.get("customer", {}):
		if customer_id := shopify_order.get("customer", {}).get("id"):
			customer = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id}, "name")

	so = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: shopify_order.get("id")}, "name")

	if not so:
		if company_config is None:
			if company:
				from ecommerce_integrations.shopify.company_config import get_company_config

				company_config = get_company_config(company, setting)
			else:
				company_config, location_ids = resolve_company_config_for_order(shopify_order, setting)

		if location_ids is None:
			location_ids = collect_order_location_ids(shopify_order)

		warehouse = default_line_warehouse(company_config, location_ids, setting)
		money_side = resolve_money_side(shopify_order, company_config.company)
		doc_currency = get_company_currency(company_config.company)

		items = get_order_items(
			shopify_order.get("line_items"),
			setting,
			getdate(shopify_order.get("created_at")),
			taxes_inclusive=shopify_order.get("taxes_included"),
			warehouse=warehouse,
			money_side=money_side,
		)

		if not items:
			message = (
				"Following items exists in the shopify order but relevant records were"
				" not found in the shopify Product master"
			)
			product_not_exists = []  # TODO: fix missing items
			message += "\n" + ", ".join(product_not_exists)

			create_shopify_log(status="Error", exception=message, rollback=True)

			return ""

		taxes = get_order_taxes(
			shopify_order, setting, items, company_config=company_config, money_side=money_side
		)
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"naming_series": company_config.sales_order_series or "SO-Shopify-",
				ORDER_ID_FIELD: str(shopify_order.get("id")),
				ORDER_NUMBER_FIELD: shopify_order.get("name"),
				ORDER_DISCOUNT_CODES_FIELD: _format_discount_codes(shopify_order, money_side=money_side),
				"customer": customer,
				"transaction_date": getdate(shopify_order.get("created_at")) or nowdate(),
				"delivery_date": getdate(shopify_order.get("created_at")) or nowdate(),
				"company": company_config.company,
				"currency": doc_currency,
				"conversion_rate": 1,
				"selling_price_list": get_dummy_price_list(),
				"price_list_currency": doc_currency,
				"plc_conversion_rate": 1,
				"ignore_pricing_rule": 1,
				"items": items,
				"taxes": taxes,
				"tax_category": get_dummy_tax_category(),
			}
		)

		# Legacy override: force company + leave Draft (tests / callers)
		if company and company != company_config.company:
			so.update({"company": company, "status": "Draft"})
		so.flags.ignore_mandatory = True
		so.flags.shopiy_order_json = json.dumps(shopify_order)
		so.save(ignore_permissions=True)
		so.submit()

		if shopify_order.get("note"):
			so.add_comment(text=f"Order Note: {shopify_order.get('note')}")

	else:
		so = frappe.get_doc("Sales Order", so)

	return so


def get_order_items(
	order_items, setting, delivery_date, taxes_inclusive, warehouse=None, money_side: MoneySide = "shop"
):
	items = []
	all_product_exists = True
	product_not_exists = []
	warehouse = warehouse or setting.warehouse

	for shopify_item in order_items:
		if not shopify_item.get("product_exists"):
			all_product_exists = False
			product_not_exists.append(
				{"title": shopify_item.get("title"), ORDER_ID_FIELD: shopify_item.get("id")}
			)
			continue

		if all_product_exists:
			item_code = get_item_code(shopify_item)
			price_list_rate, discount_per_unit = _get_line_price_and_discount(
				shopify_item, taxes_inclusive, money_side=money_side
			)
			items.append(
				{
					"item_code": item_code,
					"item_name": shopify_item.get("name"),
					"price_list_rate": price_list_rate,
					"discount_amount": discount_per_unit,
					"rate": flt(price_list_rate) - flt(discount_per_unit),
					"delivery_date": delivery_date,
					"qty": shopify_item.get("quantity"),
					"stock_uom": shopify_item.get("uom") or "Nos",
					"warehouse": warehouse,
					ORDER_ITEM_DISCOUNT_FIELD: discount_per_unit,
				}
			)
		else:
			items = []

	return items


def _get_line_price_and_discount(
	line_item, taxes_inclusive: bool, money_side: MoneySide = "shop"
) -> tuple[float, float]:
	"""Return (price_list_rate, discount_amount per unit).

	Shopify list price is pre-discount. Discounts live in ``discount_allocations``.
	When taxes are inclusive, peel tax out of the list price before applying discount.
	Amounts follow ``money_side`` (shop vs presentment).
	"""
	qty = cint(line_item.get("quantity")) or 1
	price_list_rate = amount_from_row(line_item, money_side, scalar_key="price", set_key="price_set")
	discount_per_unit = flt(_get_total_discount(line_item, money_side)) / qty

	if taxes_inclusive:
		total_taxes = sum(
			amount_from_row(tax, money_side, scalar_key="price", set_key="price_set")
			for tax in line_item.get("tax_lines") or []
		)
		price_list_rate -= total_taxes / qty

	return price_list_rate, discount_per_unit


def _get_total_discount(line_item, money_side: MoneySide = "shop") -> float:
	discount_allocations = line_item.get("discount_allocations") or []
	return sum(discount_amount_from_allocation(discount, money_side) for discount in discount_allocations)


def _format_discount_codes(shopify_order, money_side: MoneySide = "shop") -> str:
	"""Human-readable Shopify discount codes for Sales Order staff visibility."""
	parts = []
	for row in shopify_order.get("discount_codes") or []:
		code = cstr(row.get("code")).strip()
		if not code:
			continue
		if isinstance(row.get("amount_set"), dict) or (
			money_side == "shop" and row.get("amount") not in (None, "")
		):
			amount = amount_from_row(row, money_side, scalar_key="amount", set_key="amount_set")
			parts.append(f"{code} ({flt(amount)})")
		else:
			parts.append(code)
	return ", ".join(parts)


def get_order_taxes(shopify_order, setting, items, company_config=None, money_side: MoneySide = "shop"):
	taxes = []
	line_items = shopify_order.get("line_items")
	cost_center = company_config.cost_center if company_config else setting.cost_center
	company = company_config.company if company_config else setting.company

	for line_item in line_items:
		item_code = get_item_code(line_item)
		for tax in line_item.get("tax_lines"):
			tax_amount = amount_from_row(tax, money_side, scalar_key="price", set_key="price_set")
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account_head(
						tax, charge_type="sales_tax", company=company, company_config=company_config
					),
					"description": (
						get_tax_account_description(tax, company=company)
						or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
					),
					"tax_amount": tax_amount,
					"included_in_print_rate": 0,
					"cost_center": cost_center,
					"item_wise_tax_detail": {item_code: [flt(tax.get("rate")) * 100, tax_amount]},
					"dont_recompute_tax": 1,
				}
			)

	update_taxes_with_shipping_lines(
		taxes,
		shopify_order.get("shipping_lines"),
		setting,
		items,
		taxes_inclusive=shopify_order.get("taxes_included"),
		company_config=company_config,
		money_side=money_side,
	)

	if cint(setting.consolidate_taxes):
		taxes = consolidate_order_taxes(taxes)

	for row in taxes:
		tax_detail = row.get("item_wise_tax_detail")
		if isinstance(tax_detail, dict):
			row["item_wise_tax_detail"] = json.dumps(tax_detail)

	return taxes


def consolidate_order_taxes(taxes):
	tax_account_wise_data = {}
	for tax in taxes:
		account_head = tax["account_head"]
		tax_account_wise_data.setdefault(
			account_head,
			{
				"charge_type": "Actual",
				"account_head": account_head,
				"description": tax.get("description"),
				"cost_center": tax.get("cost_center"),
				"included_in_print_rate": 0,
				"dont_recompute_tax": 1,
				"tax_amount": 0,
				"item_wise_tax_detail": {},
			},
		)
		tax_account_wise_data[account_head]["tax_amount"] += flt(tax.get("tax_amount"))
		if tax.get("item_wise_tax_detail"):
			tax_account_wise_data[account_head]["item_wise_tax_detail"].update(tax["item_wise_tax_detail"])

	return tax_account_wise_data.values()


def get_tax_account_head(tax, charge_type: Literal["shipping", "sales_tax"] | None = None, company=None, company_config=None):
	tax_title = str(tax.get("title"))

	filters = {"parent": SETTING_DOCTYPE, "shopify_tax": tax_title}
	if company:
		filters["company"] = company

	tax_account = frappe.db.get_value("Shopify Tax Account", filters, "tax_account")

	# Legacy rows without company
	if not tax_account and company:
		tax_account = frappe.db.get_value(
			"Shopify Tax Account",
			{"parent": SETTING_DOCTYPE, "shopify_tax": tax_title, "company": ["in", ["", None]]},
			"tax_account",
		)
		if not tax_account:
			tax_account = frappe.db.sql(
				"""
				select tax_account from `tabShopify Tax Account`
				where parent=%s and shopify_tax=%s and ifnull(company, '')=''
				limit 1
				""",
				(SETTING_DOCTYPE, tax_title),
			)
			tax_account = tax_account[0][0] if tax_account else None

	if not tax_account and charge_type:
		fieldname = DEFAULT_TAX_FIELDS[charge_type]
		if company_config:
			tax_account = company_config.get(fieldname)
		if not tax_account:
			tax_account = frappe.db.get_single_value(SETTING_DOCTYPE, fieldname)

	if not tax_account:
		frappe.throw(_("Tax Account not specified for Shopify Tax {0}").format(tax.get("title")))

	return tax_account


def get_tax_account_description(tax, company=None):
	tax_title = tax.get("title")

	filters = {"parent": SETTING_DOCTYPE, "shopify_tax": tax_title}
	if company:
		filters["company"] = company

	tax_description = frappe.db.get_value("Shopify Tax Account", filters, "tax_description")
	if not tax_description and company:
		tax_description = frappe.db.sql(
			"""
			select tax_description from `tabShopify Tax Account`
			where parent=%s and shopify_tax=%s and ifnull(company, '')=''
			limit 1
			""",
			(SETTING_DOCTYPE, tax_title),
		)
		tax_description = tax_description[0][0] if tax_description else None

	return tax_description


def update_taxes_with_shipping_lines(
	taxes,
	shipping_lines,
	setting,
	items,
	taxes_inclusive=False,
	company_config=None,
	money_side: MoneySide = "shop",
):
	"""Shipping lines represents the shipping details,
	each such shipping detail consists of a list of tax_lines"""
	shipping_as_item = cint(setting.add_shipping_as_item) and setting.shipping_item
	cost_center = company_config.cost_center if company_config else setting.cost_center
	company = company_config.company if company_config else setting.company
	warehouse = (
		company_config.default_warehouse
		if company_config
		else setting.warehouse
	)
	for shipping_charge in shipping_lines or []:
		shipping_discounts = shipping_charge.get("discount_allocations") or []
		total_discount = sum(
			discount_amount_from_allocation(discount, money_side) for discount in shipping_discounts
		)

		shipping_taxes = shipping_charge.get("tax_lines") or []
		total_tax = sum(
			amount_from_row(tax, money_side, scalar_key="price", set_key="price_set")
			for tax in shipping_taxes
		)

		shipping_charge_amount = (
			amount_from_row(shipping_charge, money_side, scalar_key="price", set_key="price_set")
			- flt(total_discount)
		)
		if taxes_inclusive:
			shipping_charge_amount -= total_tax

		if shipping_charge_amount > 0:
			if shipping_as_item:
				items.append(
					{
						"item_code": setting.shipping_item,
						"rate": shipping_charge_amount,
						"delivery_date": items[-1]["delivery_date"] if items else nowdate(),
						"qty": 1,
						"stock_uom": "Nos",
						"warehouse": warehouse,
					}
				)
			else:
				taxes.append(
					{
						"charge_type": "Actual",
						"account_head": get_tax_account_head(
							shipping_charge,
							charge_type="shipping",
							company=company,
							company_config=company_config,
						),
						"description": get_tax_account_description(shipping_charge, company=company)
						or shipping_charge["title"],
						"tax_amount": shipping_charge_amount,
						"cost_center": cost_center,
					}
				)

		for tax in shipping_taxes:
			tax_amount = amount_from_row(tax, money_side, scalar_key="price", set_key="price_set")
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account_head(
						tax, charge_type="sales_tax", company=company, company_config=company_config
					),
					"description": (
						get_tax_account_description(tax, company=company)
						or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
					),
					"tax_amount": tax_amount,
					"cost_center": cost_center,
					"item_wise_tax_detail": {
						setting.shipping_item: [flt(tax.get("rate")) * 100, tax_amount]
					}
					if shipping_as_item
					else {},
					"dont_recompute_tax": 1,
				}
			)


def get_sales_order(order_id):
	"""Get ERPNext sales order using shopify order id."""
	sales_order = frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: order_id})
	if sales_order:
		return frappe.get_doc("Sales Order", sales_order)


def cancel_order(payload, request_id=None):
	"""Called by order/cancelled event.

	When shopify order is cancelled there could be many different someone handles it.

	Updates document with custom field showing order status.

	IF sales invoice / delivery notes are not generated against an order, then cancel it.
	"""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	order = payload

	try:
		order_id = order["id"]
		order_status = order["financial_status"]

		sales_order = get_sales_order(order_id)

		if not sales_order:
			create_shopify_log(status="Invalid", message="Sales Order does not exist")
			return

		sales_invoice = frappe.db.get_value("Sales Invoice", filters={ORDER_ID_FIELD: order_id})
		delivery_notes = frappe.db.get_list("Delivery Note", filters={ORDER_ID_FIELD: order_id})

		if sales_invoice:
			frappe.db.set_value("Sales Invoice", sales_invoice, ORDER_STATUS_FIELD, order_status)

		for dn in delivery_notes:
			frappe.db.set_value("Delivery Note", dn.name, ORDER_STATUS_FIELD, order_status)

		if not sales_invoice and not delivery_notes and sales_order.docstatus == 1:
			sales_order.cancel()
		else:
			frappe.db.set_value("Sales Order", sales_order.name, ORDER_STATUS_FIELD, order_status)

	except Exception as e:
		create_shopify_log(status="Error", exception=e)
	else:
		create_shopify_log(status="Success")


@temp_shopify_session
def sync_old_orders():
	shopify_setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not cint(shopify_setting.sync_old_orders):
		return

	orders = _fetch_old_orders(shopify_setting.old_orders_from, shopify_setting.old_orders_to)

	for order in orders:
		log = create_shopify_log(
			method=EVENT_MAPPER["orders/create"], request_data=json.dumps(order), make_new=True
		)
		sync_sales_order(order, request_id=log.name)

	shopify_setting = frappe.get_doc(SETTING_DOCTYPE)
	shopify_setting.sync_old_orders = 0
	shopify_setting.save()


def _fetch_old_orders(from_time, to_time):
	"""Fetch all shopify orders in specified range and return an iterator on fetched orders."""

	from_time = get_datetime(from_time).astimezone().isoformat()
	to_time = get_datetime(to_time).astimezone().isoformat()
	orders_iterator = PaginatedIterator(
		Order.find(created_at_min=from_time, created_at_max=to_time, limit=250)
	)

	for orders in orders_iterator:
		for order in orders:
			# Using generator instead of fetching all at once is better for
			# avoiding rate limits and reducing resource usage.
			yield order.to_dict()
