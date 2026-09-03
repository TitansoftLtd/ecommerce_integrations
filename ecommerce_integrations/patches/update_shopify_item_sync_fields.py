import frappe

from ecommerce_integrations.shopify.constants import (
	SETTING_DOCTYPE,
	VARIANT_SYNC_MODE_FIELD,
	VARIANT_SYNC_STANDALONE,
)
from ecommerce_integrations.shopify.doctype.shopify_setting.shopify_setting import (
	setup_custom_fields,
)


def execute():
	frappe.reload_doc("shopify", "doctype", "shopify_setting")

	if not frappe.db.get_single_value(SETTING_DOCTYPE, VARIANT_SYNC_MODE_FIELD):
		frappe.db.set_single_value(SETTING_DOCTYPE, VARIANT_SYNC_MODE_FIELD, VARIANT_SYNC_STANDALONE)

	if frappe.get_doc(SETTING_DOCTYPE).is_enabled():
		setup_custom_fields()
