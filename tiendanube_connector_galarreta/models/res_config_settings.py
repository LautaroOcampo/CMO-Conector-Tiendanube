# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging
import requests

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    """Configuración del conector TiendaNube"""
    _inherit = 'res.config.settings'

    # Credenciales TiendaNube
    tn_client_id = fields.Char(
        string='Client ID',
        config_parameter='tiendanube_connector.client_id',
        help='Client ID de la aplicación TiendaNube'
    )
    
    tn_client_secret = fields.Char(
        string='Client Secret',
        config_parameter='tiendanube_connector.client_secret',
        help='Client Secret de la aplicación TiendaNube'
    )
    
    tn_access_token = fields.Char(
        string='Access Token',
        config_parameter='tiendanube_connector.access_token',
        help='Access Token de TiendaNube'
    )
    
    tn_refresh_token = fields.Char(
        string='Refresh Token',
        config_parameter='tiendanube_connector.refresh_token',
        help='Refresh Token de TiendaNube'
    )
    
    tn_store_id = fields.Char(
        string='Store ID',
        config_parameter='tiendanube_connector.store_id',
        help='ID de la tienda en TiendaNube'
    )
    
    tn_api_base_url = fields.Char(
        string='API Base URL',
        default='https://api.tiendanube.com/v1',
        config_parameter='tiendanube_connector.api_base_url',
        help='URL base de la API de TiendaNube'
    )
    
    # Mantener store_url por compatibilidad, pero será reemplazado por store_id y api_base_url
    tn_store_url = fields.Char(
        string='URL TiendaNube (Legacy)',
        config_parameter='tiendanube_connector.store_url',
        help='URL de la tienda en TiendaNube (deprecated, usar Store ID y API Base URL)'
    )

    tn_webhook_test_endpoint_enabled = fields.Boolean(
        string='Habilitar endpoint de prueba /tiendanube/webhook/test',
        config_parameter='tiendanube_connector.webhook_test_endpoint_enabled',
        default=False,
        groups='tiendanube_connector.group_tn_admin',
        help='Solo usuarios del grupo Administración / Ajustes. Desactivar en producción: evita exponer un '
             'endpoint de diagnóstico. En desarrollo se puede activar temporalmente.',
    )
    
    def action_test_connection(self):
        """Prueba la conexión con TiendaNube haciendo un GET /products?page=1"""
        self.ensure_one()
        
        if not self.tn_access_token:
            raise UserError(_('Debe configurar el Access Token primero'))
        
        if not self.tn_store_id and not self.tn_store_url:
            raise UserError(_('Debe configurar el Store ID o la URL de la tienda primero'))
        
        try:
            sync_service = self.env['tn.sync.service']
            result = sync_service.test_connection()
            
            if result.get('success'):
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Conexión exitosa'),
                        'message': _('La conexión con TiendaNube se estableció correctamente. Credenciales válidas.'),
                        'type': 'success',
                        'sticky': False,
                    }
                }
            else:
                error_msg = result.get('error', 'Desconocido')
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Error en la conexión'),
                        'message': _('No se pudo conectar con TiendaNube: %s') % error_msg,
                        'type': 'danger',
                        'sticky': True,
                    }
                }
        except Exception as e:
            _logger.exception("Error al probar conexión: %s", e)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Error al probar la conexión'),
                    'message': _('Error: %s') % str(e),
                    'type': 'danger',
                    'sticky': True,
                }
            }
    
    def action_refresh_token(self):
        """Refresca el token de acceso"""
        self.ensure_one()
        
        if not self.tn_client_id or not self.tn_client_secret or not self.tn_refresh_token:
            raise UserError(_('Debe configurar Client ID, Client Secret y Refresh Token primero'))
        
        try:
            sync_service = self.env['tn.sync.service']
            result = sync_service.refresh_access_token()
            
            if result.get('success'):
                # Actualizar el access token en la configuración
                self.tn_access_token = result.get('access_token')
                if result.get('refresh_token'):
                    self.tn_refresh_token = result.get('refresh_token')
                
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Token actualizado'),
                        'message': _('El token de acceso se renovó correctamente'),
                        'type': 'success',
                        'sticky': False,
                    }
                }
            else:
                raise UserError(_('Error al refrescar el token: %s') % result.get('error', 'Desconocido'))
        except Exception as e:
            _logger.exception("Error al refrescar token: %s", e)
            raise UserError(_('Error al refrescar el token: %s') % str(e))

