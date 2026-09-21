# -*- coding: utf-8 -*-
{
    'name': "TiendaNube Connector",

    'summary': "Sync products, stock, prices and orders between Odoo and TiendaNube",

    'description': """
TiendaNube Connector
====================

Connect Odoo with the TiendaNube (Nuvemshop) API.

Requires an active TiendaNube partner application and store
(external service). Authorization uses OAuth 2.0; product, stock, price
and order data are exchanged with TiendaNube servers.

Features:

* Export Odoo products to TiendaNube
* Import products from TiendaNube into Odoo
* Sync images, variants and attributes
* Sync stock and prices (manual or automatic)
* Import sales via webhook notifications and polling
* OAuth 2.0 authentication and token management
* Automatic pause/unpause based on minimum stock rules
    """,

    'author': "Galarreta",
    'website': "https://galarreta.co",
    'support': "lautaro@galarreta.co",

    'category': 'Sales',
    'version': '19.0.1.0.38',

    'depends': ['base', 'product', 'sale', 'stock', 'mail', 'account', 'l10n_ar'],

    'data': [
        'security/tn_security.xml',
        'security/ir.model.access.csv',
        'views/tn_config_views.xml',
        'views/tn_oauth_tenant_views.xml',
        'views/tn_publication_views.xml',
        'views/tn_publication_values_wizard_views.xml',
        'views/tn_import_orders_wizard_views.xml',
        'views/tn_sale_order_views.xml',
        'views/tn_webhook_notification_views.xml',
        'views/tn_sync_log_views.xml',
        'views/sale_order_views.xml',
        'views/report_invoice_templates.xml',
        'views/res_config_settings_views.xml',
        'views/tn_create_publication_wizard_views.xml',
        'data/tn_oauth_parameters.xml',
        'data/cron_order_sync.xml',
        'data/cron_product_import.xml',
    ],

    'demo': [],

    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
