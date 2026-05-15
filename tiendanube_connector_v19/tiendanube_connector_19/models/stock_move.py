# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    """Extensión de stock.move para sincronizar stock automáticamente con TiendaNube cuando se valida un movimiento"""
    _inherit = 'stock.move'

    def _action_done(self, cancel_backorder=False):
        """
        Sobrescribir _action_done para sincronizar stock y/o verificar pausa/activación
        por stock mínimo cuando se valida un movimiento de stock (compra, venta, movimiento).
        
        Se agenda para después del commit:
        - Sync de stock a TN si auto_sync_stock_on_odoo_change está activado.
        - Verificación de pausa/activación por stock mínimo si enable_min_stock_pause
          o enable_min_stock_unpause está activado en alguna config.
        """
        result = super(StockMove, self)._action_done(cancel_backorder=cancel_backorder)

        products_affected = set()
        for move in self:
            if move.product_id and move.state == 'done':
                if move.location_id.usage == 'internal' or move.location_dest_id.usage == 'internal':
                    products_affected.add(move.product_id.id)
        if not products_affected:
            return result

        env = self.env
        pids = list(products_affected)

        configs_sync = env['tn.config'].search([('auto_sync_stock_on_odoo_change', '=', True)])
        configs_min = env['tn.config'].search([
            '|', ('enable_min_stock_pause', '=', True), ('enable_min_stock_unpause', '=', True)
        ])
        if not configs_sync and not configs_min:
            return result

        location_id = None
        if configs_sync:
            c = configs_sync[0]
            if c.warehouse_id and c.warehouse_id.lot_stock_id:
                location_id = c.warehouse_id.lot_stock_id.id
            elif c.stock_location_id:
                location_id = c.stock_location_id.id
        elif configs_min:
            c = configs_min[0]
            if c.warehouse_id and c.warehouse_id.lot_stock_id:
                location_id = c.warehouse_id.lot_stock_id.id
            elif c.stock_location_id:
                location_id = c.stock_location_id.id

        def _run_after_commit():
            for product_id in pids:
                try:
                    if configs_sync:
                        env['stock.quant']._sync_stock_to_tiendanube(product_id, location_id)
                    elif configs_min:
                        env['stock.quant']._check_min_stock_pause_unpause_for_product(product_id, location_id)
                except Exception as e:
                    _logger.exception("❌ Error TN post-commit desde stock.move (product_id=%s): %s", product_id, e)

        try:
            if hasattr(env.cr, 'postcommit') and hasattr(env.cr.postcommit, 'add'):
                env.cr.postcommit.add(_run_after_commit)
            else:
                _logger.debug("postcommit no disponible; omitiendo TN desde stock.move")
        except Exception as e:
            _logger.warning("No se pudo programar TN post-commit desde stock.move: %s", e)

        return result

