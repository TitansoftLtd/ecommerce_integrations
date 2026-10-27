# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

"""Resolve Shopify shop vs presentment money against company currency."""

from __future__ import annotations

from typing import Any, Literal

import frappe
from frappe import _
from frappe.utils import flt

MoneySide = Literal["shop", "presentment"]

_SIDE_KEY = {
	"shop": "shop_money",
	"presentment": "presentment_money",
}


class ShopifyCurrencyMismatchError(frappe.ValidationError):
	pass


def normalize_currency(code: str | None) -> str:
	return (code or "").strip().upper()


def get_company_currency(company: str) -> str:
	currency = frappe.get_cached_value("Company", company, "default_currency")
	if not currency:
		frappe.throw(_("Company {0} has no default currency").format(company))
	return normalize_currency(currency)


def resolve_money_side(shopify_order: dict, company: str) -> MoneySide:
	"""Pick shop or presentment money so amounts match Company.default_currency.

	If both Shopify currencies match the company currency, prefer shop.
	If neither matches, raise — do not invent FX.
	"""
	company_currency = get_company_currency(company)
	shop = normalize_currency(shopify_order.get("currency"))
	presentment = normalize_currency(shopify_order.get("presentment_currency"))

	if shop and shop == company_currency:
		return "shop"
	if presentment and presentment == company_currency:
		return "presentment"

	raise ShopifyCurrencyMismatchError(
		_(
			"Shopify order currency does not match company {0} currency ({1}). "
			"Shop currency: {2}; presentment currency: {3}. "
			"Unable to book amounts without inventing an exchange rate."
		).format(company, company_currency, shop or _("(missing)"), presentment or _("(missing)"))
	)


def money_amount(
	money_set: dict | None,
	side: MoneySide,
	*,
	fallback: float | int | str | None = None,
	field_label: str = "amount",
) -> float:
	"""Read amount from a Shopify ``price_set`` / ``amount_set`` style dict.

	Legacy scalar ``fallback`` is only used for the shop side (top-level Shopify
	fields are shop currency). Presentment requires a money set.
	"""
	key = _SIDE_KEY[side]
	if isinstance(money_set, dict):
		bag = money_set.get(key)
		if isinstance(bag, dict) and bag.get("amount") is not None:
			return flt(bag.get("amount"))

	if side == "shop" and fallback is not None:
		return flt(fallback)

	frappe.throw(
		_("Shopify {0} missing {1} for money side {2}").format(field_label, key, side),
		ShopifyCurrencyMismatchError,
	)


def amount_from_row(
	row: dict | None,
	side: MoneySide,
	*,
	scalar_key: str = "price",
	set_key: str = "price_set",
) -> float:
	"""Extract a money amount from a Shopify line / tax / shipping row."""
	row = row or {}
	return money_amount(
		row.get(set_key),
		side,
		fallback=row.get(scalar_key),
		field_label=set_key,
	)


def discount_amount_from_allocation(allocation: dict | None, side: MoneySide) -> float:
	allocation = allocation or {}
	return money_amount(
		allocation.get("amount_set"),
		side,
		fallback=allocation.get("amount"),
		field_label="amount_set",
	)
