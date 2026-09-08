import frappe

from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE
from ecommerce_integrations.shopify.doctype.shopify_setting.shopify_setting import (
	setup_custom_fields,
)


def execute():
	frappe.reload_doc("shopify", "doctype", "shopify_setting")

	# Shopify Setting is a Single; remove leftover mode value from tabSingles
	frappe.db.delete("Singles", {"doctype": SETTING_DOCTYPE, "field": "variant_sync_mode"})

	if frappe.get_doc(SETTING_DOCTYPE).is_enabled():
		setup_custom_fields()
