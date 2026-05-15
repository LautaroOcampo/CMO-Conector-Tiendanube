# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)


class ProductPricelistItem(models.Model):
    """Extiende product.pricelist.item para sincronizar precio automáticamente con TiendaNube"""
    _inherit = 'product.pricelist.item'

    def write(self, vals):
        """Sobrescribir write para sincronizar precio automáticamente cuando cambia en lista de precios"""
        result = super(ProductPricelistItem, self).write(vals)
        
        # Verificar si se modificó el precio (fixed_price o cualquier campo que afecte el precio)
        price_fields = ['fixed_price', 'percent_price', 'price_discount', 'price_surcharge', 'product_id', 'product_tmpl_id']
        if any(field in vals for field in price_fields):
            # Verificar si hay alguna configuración con sincronización automática de precio activada
            configs = self.env['tn.config'].search([
                ('auto_sync_price_on_odoo_change', '=', True),
                ('pricelist_id', '!=', False)
            ])
            
            if configs:
                # Filtrar configuraciones donde la lista de precios coincide con la del item modificado
                relevant_configs = configs.filtered(lambda c: c.pricelist_id.id == self.pricelist_id.id)
                
                if relevant_configs:
                    # Obtener el producto afectado
                    product_tmpl = self.product_tmpl_id or (self.product_id.product_tmpl_id if self.product_id else None)
                    
                    if product_tmpl:
                        # Sincronizar precio para todas las publicaciones relacionadas
                        try:
                            product_tmpl._sync_price_to_tiendanube()
                        except Exception as e:
                            _logger.exception("❌ Error sincronizando precio automáticamente desde lista de precios para producto %s: %s", 
                                            product_tmpl.id, e)
                            # No fallar la operación si falla la sincronización
        
        return result
    
    @api.model_create_multi
    def create(self, vals_list):
        """Sobrescribir create para sincronizar precio automáticamente cuando se crea un item de lista de precios"""
        result = super(ProductPricelistItem, self).create(vals_list)
        
        # Verificar si hay alguna configuración con sincronización automática de precio activada
        configs = self.env['tn.config'].search([
            ('auto_sync_price_on_odoo_change', '=', True),
            ('pricelist_id', '!=', False)
        ])
        
        if configs:
            for item in result:
                # Filtrar configuraciones donde la lista de precios coincide con la del item creado
                relevant_configs = configs.filtered(lambda c: c.pricelist_id.id == item.pricelist_id.id)
                
                if relevant_configs:
                    # Obtener el producto afectado
                    product_tmpl = item.product_tmpl_id or (item.product_id.product_tmpl_id if item.product_id else None)
                    
                    if product_tmpl:
                        # Sincronizar precio para todas las publicaciones relacionadas
                        try:
                            product_tmpl._sync_price_to_tiendanube()
                        except Exception as e:
                            _logger.exception("❌ Error sincronizando precio automáticamente desde lista de precios (create) para producto %s: %s", 
                                            product_tmpl.id, e)
                            # No fallar la operación si falla la sincronización
        
        return result
