# -*- coding: utf-8 -*-
from odoo import models

# La sincronización automática de precio a TiendaNube desde listas de precios
# quedó deshabilitada: el precio en TN se gestiona solo desde la publicación
# («Actualizar valores»).


class ProductPricelistItem(models.Model):
    _inherit = 'product.pricelist.item'
