# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class TNImportOrdersWizard(models.TransientModel):
    _name = 'tn.import.orders.wizard'
    _description = 'Wizard para importar órdenes de TiendaNube por rango de fechas'

    tn_config_id = fields.Many2one(
        'tn.config',
        string='Configuración TiendaNube',
        required=True,
        readonly=True
    )

    date_from = fields.Date(
        string='Desde fecha',
        required=True,
        help='Solo se importan órdenes creadas desde esta fecha (inclusive).'
    )
    date_to = fields.Date(
        string='Hasta fecha',
        help='Solo se importan órdenes creadas hasta esta fecha (inclusive). Dejar vacío para hasta hoy.'
    )

    def action_import_orders(self):
        """Ejecuta la importación de órdenes con el rango de fechas indicado."""
        self.ensure_one()
        if self.date_to and self.date_from > self.date_to:
            raise UserError(_('La fecha "Desde" no puede ser posterior a la fecha "Hasta".'))
        result = self.tn_config_id.sync_orders_by_polling(
            date_from=self.date_from,
            date_to=self.date_to
        )
        if result.get('success'):
            processed = result.get('orders_processed', 0)
            created = result.get('orders_created', 0)
            updated = result.get('orders_updated', 0)
            msg = _('Sincronización completada: %d órdenes procesadas (%d creadas, %d actualizadas).') % (
                processed, created, updated
            )
            if result.get('errors'):
                msg += ' ' + _('Algunas órdenes tuvieron errores (ver logs).')
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Órdenes sincronizadas'),
                    'message': msg,
                    'type': 'success' if not result.get('errors') else 'warning',
                    'sticky': False,
                }
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Error al sincronizar órdenes'),
                'message': result.get('message', _('Error desconocido')),
                'type': 'danger',
                'sticky': True,
            }
        }
