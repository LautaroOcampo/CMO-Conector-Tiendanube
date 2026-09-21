from odoo import models, fields, api
from datetime import timedelta
import logging
_logger = logging.getLogger(__name__)

class TnSyncLog(models.Model):
    _name = 'tn.sync.log'
    _description = 'Auditoría de sincronizaciones TiendaNube'
    _order = 'create_date desc'
    _rec_name = 'create_date'

    tn_config_id    = fields.Many2one('tn.config', ondelete='cascade')
    publication_id  = fields.Many2one('tn.publication', ondelete='set null')
    operation       = fields.Selection([
        ('stock_update',  'Actualización de stock'),
        ('price_update',  'Actualización de precio'),
        ('status_change', 'Cambio de estado'),
        ('order_import',  'Importación de venta'),
        ('product_import', 'Importación de catálogo'),
        ('export',        'Exportación a TN'),
    ], required=True)
    trigger         = fields.Selection([
        ('auto',   'Automático'),
        ('manual', 'Manual'),
    ], default='auto')
    value_before    = fields.Char()
    value_after     = fields.Char()
    result          = fields.Selection([
        ('ok',    'Exitoso'),
        ('error', 'Error'),
    ], required=True)
    http_status     = fields.Integer()
    error_message   = fields.Text()
    duration_ms     = fields.Integer()

    @api.model
    def _log_sync(self, config, operation, result, **kwargs):
        force_log = bool(kwargs.pop('force_log', False))
        # No loguear si el valor no cambió y fue exitoso (salvo auditoría forzada, p. ej. llamada API)
        if (
            not force_log
            and result == 'ok'
            and kwargs.get('value_before') == kwargs.get('value_after')
        ):
            return
        try:
            self.create({
                'tn_config_id': config.id if config else False,
                'operation': operation,
                'result': result,
                **kwargs
            })
        except Exception as e:
            _logger.warning("No se pudo crear tn.sync.log: %s", e)

    def cron_purge_old_ok_logs(self):
        cutoff = fields.Datetime.now() - timedelta(days=30)
        self.search([
            ('result', '=', 'ok'),
            ('create_date', '<', cutoff)
        ]).unlink()