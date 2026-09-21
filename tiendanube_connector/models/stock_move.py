# -*- coding: utf-8 -*-
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


def _tn_resolve_stock_location(configs):
    """Ubicación raíz del almacén TN para filtrar sync."""
    if not configs:
        return None
    c = configs[0]
    if c.warehouse_id and c.warehouse_id.lot_stock_id:
        return c.warehouse_id.lot_stock_id.id
    if c.stock_location_id:
        return c.stock_location_id.id
    return None


def _tn_schedule_products_after_commit(env, product_ids, configs_sync, configs_min, reason):
    """Agenda sync TN y/o check min-stock post-commit para varios productos."""
    if not product_ids:
        return
    if not configs_sync and not configs_min:
        return

    pids = tuple(set(int(p) for p in product_ids if p))
    if not pids:
        return

    location_id = _tn_resolve_stock_location(configs_sync or configs_min)
    do_sync = bool(configs_sync)
    do_min = bool(configs_min) and not configs_sync

    def _run_after_commit():
        Quant = env['stock.quant']
        for product_id in pids:
            try:
                if do_sync:
                    Quant._sync_stock_to_tiendanube(product_id, location_id)
                elif do_min:
                    Quant._check_min_stock_pause_unpause_for_product(product_id, location_id)
            except Exception as e:
                _logger.exception(
                    "❌ Error TN post-commit (%s) product_id=%s: %s",
                    reason, product_id, e,
                )

    try:
        if hasattr(env.cr, 'postcommit') and hasattr(env.cr.postcommit, 'add'):
            env.cr.postcommit.add(_run_after_commit)
        else:
            _logger.debug("postcommit no disponible; omitiendo TN (%s)", reason)
    except Exception as e:
        _logger.warning("No se pudo programar TN post-commit (%s): %s", reason, e)


class StockMove(models.Model):
    """Sync stock TN cuando cambia el stock esperado (movimientos / validaciones)."""
    _inherit = 'stock.move'

    @api.model
    def _tn_moves_internal_product_ids(self, moves):
        return {
            move.product_id.id
            for move in moves
            if move.product_id
            and (
                move.location_id.usage == 'internal'
                or move.location_dest_id.usage == 'internal'
            )
        }

    @api.model
    def _tn_schedule_sync_for_moves(self, moves, reason, expected_only=False):
        product_ids = self._tn_moves_internal_product_ids(moves)
        if not product_ids:
            return

        sync_domain = [('auto_sync_stock_on_odoo_change', '=', True)]
        if expected_only:
            sync_domain.append(('stock_type', '=', 'expected'))
        configs_sync = self.env['tn.config'].search(sync_domain)

        configs_min = self.env['tn.config'].browse()
        if not expected_only:
            configs_min = self.env['tn.config'].search([
                '|',
                ('enable_min_stock_pause', '=', True),
                ('enable_min_stock_unpause', '=', True),
            ])

        _tn_schedule_products_after_commit(
            self.env,
            product_ids,
            configs_sync,
            configs_min,
            reason,
        )

    def write(self, vals):
        res = super().write(vals)
        if 'state' in vals or 'product_uom_qty' in vals:
            self._tn_schedule_sync_for_moves(
                self,
                'stock_move.write(state|qty)',
                expected_only=True,
            )
        return res

    @api.model_create_multi
    def create(self, vals_list):
        moves = super().create(vals_list)
        self._tn_schedule_sync_for_moves(
            moves,
            'stock_move.create',
            expected_only=True,
        )
        return moves

    def unlink(self):
        product_ids = self._tn_moves_internal_product_ids(self)
        configs_sync = self.env['tn.config'].search([
            ('auto_sync_stock_on_odoo_change', '=', True),
            ('stock_type', '=', 'expected'),
        ])
        res = super().unlink()
        if product_ids and configs_sync:
            _tn_schedule_products_after_commit(
                self.env,
                product_ids,
                configs_sync,
                self.env['tn.config'].browse(),
                'stock_move.unlink',
            )
        return res

    def _action_done(self, cancel_backorder=False):
        result = super()._action_done(cancel_backorder=cancel_backorder)
        done_moves = self.filtered(lambda m: m.state == 'done')
        self._tn_schedule_sync_for_moves(
            done_moves,
            'stock_move._action_done',
            expected_only=False,
        )
        return result
