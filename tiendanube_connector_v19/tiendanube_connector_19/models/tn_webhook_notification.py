# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


class TNWebhookNotification(models.Model):
    """
    Modelo para guardar notificaciones de webhooks de TiendaNube.
    
    El webhook solo actúa como "aviso" - guarda la notificación pero NO procesa la orden.
    El procesamiento real se hace mediante polling (cron) que consulta la API directamente.
    """
    _name = 'tn.webhook.notification'
    _description = 'Notificación de Webhook TiendaNube'
    _rec_name = 'order_id'
    _order = 'date_received desc'

    order_id = fields.Integer(
        string='ID de Orden TiendaNube',
        required=True,
        index=True,
        help='ID de la orden mencionada en el webhook'
    )

    event_type = fields.Char(
        string='Tipo de Evento',
        required=True,
        index=True,
        help='Tipo de evento del webhook (ej: order/created, order/updated)'
    )

    date_received = fields.Datetime(
        string='Fecha de Recepción',
        required=True,
        default=fields.Datetime.now,
        index=True,
        help='Fecha y hora en que se recibió el webhook'
    )

    status = fields.Selection([
        ('pending', 'Pendiente'),
        ('processed', 'Procesada'),
        ('failed', 'Fallida'),
    ], string='Estado', required=True, default='pending', index=True,
       help='Estado de la notificación. "Pendiente" significa que aún no se procesó por polling.')

    raw_data = fields.Text(
        string='Datos Raw',
        help='Datos completos del webhook recibido (JSON) para debugging'
    )

    tn_config_id = fields.Many2one(
        'tn.config',
        string='Configuración TiendaNube',
        help='Configuración de TiendaNube que recibió este webhook'
    )

    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        default=lambda self: self.env.company,
        required=True,
        index=True
    )

    # Nota: PostgreSQL no aplica bien unique(...) con expresión date::date desde _sql_constraints de Odoo.
    _sql_constraints = [
        (
            'order_id_event_type_company_unique',
            'unique(order_id, event_type, company_id)',
            'Ya existe una notificación con este ID de orden y tipo de evento para esta compañía.'
        ),
    ]

    @api.model
    def create_notification(self, order_id, event_type, raw_data=None, tn_config_id=None):
        """
        Crea una notificación de webhook.
        
        Args:
            order_id: ID de la orden en TiendaNube
            event_type: Tipo de evento (ej: 'order/created')
            raw_data: Datos raw del webhook (opcional)
            tn_config_id: ID de la configuración de TiendaNube (opcional)
        
        Returns:
            tn.webhook.notification: Registro creado
        """
        try:
            company_id = self.env.company.id
            if tn_config_id:
                config = self.env['tn.config'].browse(tn_config_id)
                if config.exists() and config.company_id:
                    company_id = config.company_id.id
            existing = self.search([
                ('order_id', '=', order_id),
                ('event_type', '=', event_type),
                ('company_id', '=', company_id),
            ], limit=1)
            if existing:
                wvals = {
                    'date_received': fields.Datetime.now(),
                    'status': 'pending',
                    'raw_data': raw_data,
                }
                if tn_config_id:
                    wvals['tn_config_id'] = tn_config_id
                existing.write(wvals)
                _logger.info(
                    "📬 Notificación de webhook actualizada (reintento): Order ID %s, Event: %s",
                    order_id, event_type,
                )
                return existing

            vals = {
                'order_id': order_id,
                'event_type': event_type,
                'date_received': fields.Datetime.now(),
                'status': 'pending',
                'raw_data': raw_data,
                'tn_config_id': tn_config_id,
                'company_id': company_id,
            }

            notification = self.create(vals)
            _logger.info(
                "📬 Notificación de webhook guardada: Order ID %s, Event: %s, Status: %s",
                order_id, event_type, notification.status
            )
            return notification
        except Exception as e:
            _logger.exception("❌ Error creando notificación de webhook: %s", e)
            raise

    @api.model
    def mark_as_processed_for_order(self, order_id, company_id):
        """
        Marca como 'processed' las notificaciones pendientes para esta orden y compañía.
        Se llama cuando el cron (o create_order_from_webhook) procesa la orden correctamente.
        """
        if not order_id or not company_id:
            return
        notifications = self.search([
            ('order_id', '=', order_id),
            ('company_id', '=', company_id),
            ('status', '=', 'pending'),
        ])
        if notifications:
            notifications.write({'status': 'processed'})
            _logger.debug("📬 %d notificación(es) marcadas como procesadas para orden TN %s", len(notifications), order_id)
