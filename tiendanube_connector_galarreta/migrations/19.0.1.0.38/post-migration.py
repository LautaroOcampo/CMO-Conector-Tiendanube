# -*- coding: utf-8 -*-
"""Elimina el cron de sincronización periódica de stock con TiendaNube.

El stock se envía solo por el sync automático al cambiar inventario en Odoo
o con el botón «Sincronizar Todos los Stocks». El cron cada 5 minutos era
redundante y multiplicaba llamadas a la API.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

XMLID = 'tiendanube_connector.ir_cron_sync_stock_tiendanube'


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref(XMLID, raise_if_not_found=False)
    if cron:
        _logger.info('TN: eliminando cron de stock «%s» (ID %s)', cron.name, cron.id)
        cron.unlink()
    else:
        _logger.info('TN: el cron de stock ya no existe, nada que eliminar.')
