# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
import logging

_logger = logging.getLogger(__name__)


class TNImportOrdersWizard(models.TransientModel):
    _name = 'tn.import.orders.wizard'
    _description = 'Wizard para importar órdenes de TiendaNube (por horas o por fechas) y reintentar webhooks'

    tn_config_id = fields.Many2one(
        'tn.config',
        string='Configuración TiendaNube',
        required=True,
        readonly=True,
    )

    import_mode = fields.Selection(
        [
            ('hours', 'Últimas N horas'),
            ('dates', 'Rango de fechas'),
        ],
        string='Modo',
        default='hours',
        required=True,
    )

    hours_back = fields.Integer(
        string='Horas hacia atrás',
        default=12,
        help='Se importan ventas creadas en TiendaNube desde (ahora − N horas) hasta ahora. Ej.: 12 = últimas 12 horas.',
    )

    retry_webhook_notifications = fields.Boolean(
        string='Reintentar webhooks pendientes',
        default=True,
        help='Procesa de nuevo las notificaciones de webhook en estado Pendiente o Fallida '
             'para esta cuenta (misma lógica que al recibir el webhook: consulta la orden en la API).',
    )

    date_from = fields.Date(
        string='Desde fecha',
        help='Solo aplica en modo «Rango de fechas». Órdenes creadas desde esta fecha (inclusive).',
    )
    date_to = fields.Date(
        string='Hasta fecha',
        help='Solo aplica en modo «Rango de fechas». Inclusive; vacío = hasta hoy.',
    )

    @api.constrains('hours_back', 'import_mode')
    def _check_hours_back(self):
        for wiz in self:
            if wiz.import_mode == 'hours':
                if not wiz.hours_back or wiz.hours_back < 1 or wiz.hours_back > 168:
                    raise ValidationError(_('Las horas deben estar entre 1 y 168 (7 días).'))

    def action_import_orders(self):
        """Importación por API (últimas horas o rango) y opcional reintento de webhooks pendientes."""
        self.ensure_one()
        cfg = self.tn_config_id

        if self.import_mode == 'hours':
            date_from = datetime.now() - timedelta(hours=self.hours_back)
            date_to = datetime.now()
            _logger.info(
                "Importación manual órdenes TN: modo horas, últimas %s h (desde %s)",
                self.hours_back, date_from,
            )
        else:
            if not self.date_from:
                raise UserError(_('Indique la fecha «Desde» o use el modo «Últimas N horas».'))
            if self.date_to and self.date_from > self.date_to:
                raise UserError(_('La fecha «Desde» no puede ser posterior a la fecha «Hasta».'))
            date_from = self.date_from
            date_to = self.date_to or fields.Date.context_today(self)

        result = cfg.sync_orders_by_polling(date_from=date_from, date_to=date_to)

        webhook_processed = 0
        webhook_errors = 0
        if self.retry_webhook_notifications:
            wh = cfg.retry_pending_webhook_notifications()
            webhook_processed = wh.get('processed', 0)
            webhook_errors = wh.get('errors', 0)

        if result.get('success'):
            processed = result.get('orders_processed', 0)
            created = result.get('orders_created', 0)
            updated = result.get('orders_updated', 0)
            msg = _('Importación por API: %d órdenes procesadas (%d creadas, %d actualizadas).') % (
                processed, created, updated,
            )
            if self.retry_webhook_notifications:
                msg += ' ' + _('Reintento webhooks: %d ok, %d con error.') % (
                    webhook_processed, webhook_errors,
                )
            if result.get('errors'):
                msg += ' ' + _('Algunas órdenes tuvieron errores en la importación por API (ver logs).')
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Ventas / webhooks'),
                    'message': msg,
                    'type': 'warning' if (result.get('errors') or webhook_errors) else 'success',
                    'sticky': bool(result.get('errors') or webhook_errors),
                },
            }
        msg = result.get('message', _('Error desconocido'))
        if self.retry_webhook_notifications:
            msg += ' ' + _('Reintento webhooks: %d ok, %d con error.') % (
                webhook_processed, webhook_errors,
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Error al sincronizar órdenes'),
                'message': msg,
                'type': 'danger',
                'sticky': True,
            },
        }
