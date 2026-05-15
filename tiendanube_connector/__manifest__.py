# -*- coding: utf-8 -*-
{
    'name': "TiendaNube Connector",

    'summary': "Conector para sincronizar productos entre Odoo y TiendaNube",

    'description': """
Módulo de sincronización TiendaNube
====================================

Este módulo permite sincronizar productos entre Odoo y TiendaNube de forma bidireccional.

Funcionalidades:
----------------
* Exportar productos de Odoo a TiendaNube
* Importar productos desde TiendaNube a Odoo
* Sincronización de imágenes, variantes y atributos
* Gestión de estado de sincronización
* Configuración de credenciales API
* Manejo de tokens y autenticación OAuth 2.0
    """,

    'author': "Galarreta",
    'website': "galarreta.co",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/15.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Sales',
    'version': '1.0.0',

    # any module necessary for this one to work correctly
    'depends': ['base', 'product', 'sale', 'stock', 'mail', 'account'],

    # always loaded (crons al final para que dependencias estén cargadas)
    'data': [
        'security/ir.model.access.csv',
        'views/tn_config_views.xml',
        'views/tn_publication_views.xml',
        'views/tn_import_orders_wizard_views.xml',
        'views/tn_sale_order_views.xml',
        'views/tn_webhook_notification_views.xml',
        'views/sale_order_views.xml',
        'views/res_config_settings_views.xml',
        'views/tn_create_publication_wizard_views.xml',
        'data/cron_stock_sync.xml',
        'data/cron_order_sync.xml',
    ],
    # only loaded in demonstration mode
    'demo': [
        'demo/demo.xml',
    ],
    
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}

