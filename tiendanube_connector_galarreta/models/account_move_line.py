# -*- coding: utf-8 -*-

from odoo import fields, models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    tn_invoice_display_name = fields.Char(
        string='Nombre TN en factura (PDF)',
        copy=False,
        help=(
            'Nombre de TiendaNube impreso en el PDF cuando la línea usa producto fallback. '
            'La descripción interna de la factura conserva la referencia al pedido TN.'
        ),
    )

    def get_channel_invoice_report_line_name(self):
        """Nombre de línea para PDF: título del canal si hay fallback, si no la descripción."""
        self.ensure_one()
        if self.tn_invoice_display_name:
            return self.tn_invoice_display_name
        if 'ml_invoice_display_name' in self._fields and self.ml_invoice_display_name:
            return self.ml_invoice_display_name
        return self.name
