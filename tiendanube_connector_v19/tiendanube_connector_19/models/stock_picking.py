# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    """Extensión de stock.picking para sincronizar validación de envío con TiendaNube"""
    _inherit = 'stock.picking'

    def button_validate(self):
        """
        Sobrescribir button_validate para sincronizar estado a TiendaNube cuando se valida el envío.
        Cuando se valida el envío en Odoo, se marca el fulfillment order como 'delivered' (entregado) en TiendaNube.
        """
        _logger.info("=" * 80)
        _logger.info("🚀 INICIO: Validación de picking manual desde Odoo")
        _logger.info("📦 Picking: %s (ID: %s)", self.name, self.id)
        _logger.info("📋 Tipo: %s (Código: %s)", self.picking_type_id.name, self.picking_type_id.code)
        _logger.info("📊 Estado actual: %s", self.state)
        _logger.info("🛒 Orden de venta: %s (ID: %s)", self.sale_id.name if self.sale_id else 'N/A', self.sale_id.id if self.sale_id else 'N/A')
        
        result = super(StockPicking, self).button_validate()
        
        _logger.info("✅ Picking validado exitosamente. Nuevo estado: %s", self.state)
        
        # Si es un picking de salida relacionado con una orden de venta
        if self.picking_type_id.code == 'outgoing' and self.sale_id:
            _logger.info("🔍 Buscando orden de TiendaNube relacionada con orden Odoo %s", self.sale_id.id)
            
            # Buscar orden de TiendaNube relacionada
            tn_order = self.env['tn.sale.order'].search([
                ('odoo_sale_order_id', '=', self.sale_id.id)
            ], limit=1)
            
            if tn_order and tn_order.tn_order_id:
                _logger.info("✅ Orden TiendaNube encontrada: TN Order ID %s (Odoo ID: %s)", 
                           tn_order.tn_order_id, tn_order.id)
                _logger.info("📦 Estado actual de envío en TN: %s", tn_order.fulfillment_status)
                
                # Cuando se valida el envío en Odoo, se marca el fulfillment order como 'delivered' (entregado) en TiendaNube
                _logger.info("🔄 Validación de envío completada. Marcando fulfillment order como 'delivered' en TiendaNube")
                
                try:
                    # Marcar fulfillment order como 'delivered' usando la API de Fulfillment Orders
                    delivered_result = tn_order._mark_fulfillment_order_as_delivered()
                    
                    if delivered_result:
                        _logger.info("✅ Fulfillment order marcado como 'delivered' exitosamente en TiendaNube")
                        
                        # También actualizar el estado local a 'shipped' para reflejar que está entregado
                        if tn_order.fulfillment_status != 'shipped':
                            _logger.info("🔄 Actualizando estado local de '%s' a 'shipped' (entregado)", 
                                       tn_order.fulfillment_status)
                            tn_order.write({
                                'fulfillment_status': 'shipped',
                                'syncing_to_tn': False  # Viene de validación manual, no del webhook
                            })
                            _logger.info("✅ Estado local actualizado a 'shipped'")
                    else:
                        _logger.warning("⚠️ No se pudo marcar fulfillment order como 'delivered' en TiendaNube")
                        # Aún así, actualizar el estado local
                        if tn_order.fulfillment_status != 'shipped':
                            tn_order.write({
                                'fulfillment_status': 'shipped',
                                'syncing_to_tn': False
                            })
                except Exception as e:
                    _logger.exception("❌ Error marcando fulfillment order como 'delivered' después de validar picking: %s", e)
                    # Aún así, actualizar el estado local
                    try:
                        if tn_order.fulfillment_status != 'shipped':
                            tn_order.write({
                                'fulfillment_status': 'shipped',
                                'syncing_to_tn': False
                            })
                    except Exception as e2:
                        _logger.exception("❌ Error actualizando estado local: %s", e2)
            else:
                _logger.info("ℹ️ No se encontró orden de TiendaNube relacionada. Este picking no proviene de TiendaNube.")
        else:
            _logger.info("ℹ️ Picking no es de salida o no tiene orden de venta relacionada. No se sincroniza.")
        
        _logger.info("=" * 80)
        return result

