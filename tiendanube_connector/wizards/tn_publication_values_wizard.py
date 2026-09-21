# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class TNPublicationValuesWizardImage(models.TransientModel):
    _name = 'tn.publication.values.wizard.image'
    _description = 'Línea de imagen (wizard actualizar valores TN)'

    wizard_id = fields.Many2one(
        'tn.publication.values.wizard',
        string='Wizard',
        required=True,
        ondelete='cascade',
    )
    sequence = fields.Integer(string='Posición', default=10)
    name = fields.Char(string='Nombre')
    image = fields.Binary(string='Imagen', required=True)


class TNPublicationValuesWizard(models.TransientModel):
    _name = 'tn.publication.values.wizard'
    _description = 'Wizard: actualizar valores en TiendaNube'

    publication_id = fields.Many2one(
        'tn.publication',
        string='Publicación',
        required=True,
        ondelete='cascade',
    )
    title_tn = fields.Char(string='Título TN', required=True)
    price = fields.Float(string='Precio', required=True)
    stock = fields.Float(string='Stock', required=True)
    sku = fields.Char(string='SKU')
    barcode = fields.Char(string='Código de barras')
    marca = fields.Char(string='Marca')
    categoria_ids = fields.Many2many(
        'tn.publication.category',
        'tn_pub_vals_wiz_cat_rel',
        'wizard_id',
        'category_id',
        string='Categorías',
    )
    tag_ids = fields.Many2many(
        'tn.publication.tag',
        'tn_pub_vals_wiz_tag_rel',
        'wizard_id',
        'tag_id',
        string='Tags',
    )
    description = fields.Html(string='Descripción')
    image_line_ids = fields.One2many(
        'tn.publication.values.wizard.image',
        'wizard_id',
        string='Imágenes',
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        pub_id = self.env.context.get('default_publication_id') or self.env.context.get('active_id')
        if not pub_id:
            return res
        pub = self.env['tn.publication'].browse(pub_id)
        if not pub.exists():
            return res
        res['publication_id'] = pub.id
        res['title_tn'] = (pub.title or '').strip()
        res['marca'] = (pub.marca or '').strip()
        res['sku'] = (pub.sku or '').strip()
        res['barcode'] = (pub.barcode or '').strip()
        res['description'] = pub.description or False
        res['categoria_ids'] = [(6, 0, pub.categoria_ids.ids)]
        res['tag_ids'] = [(6, 0, pub.tag_ids.ids)]
        variants = pub.variant_ids.sorted(key=lambda v: (v.id,))
        if variants:
            res['price'] = float(variants[0].price or 0.0)
            res['stock'] = float(variants[0].stock or 0.0)
        else:
            res['price'] = float(pub.current_price_tn or 0.0)
            res['stock'] = float(pub.current_stock_tn or 0.0)
        fields_list = fields_list or []
        if (not fields_list or 'image_line_ids' in fields_list) and pub.image_ids:
            lines = []
            for img in pub.image_ids.sorted(key=lambda i: (i.position, i.id)):
                lines.append((0, 0, {
                    'sequence': img.position or 10,
                    'name': img.name or '',
                    'image': img.image,
                }))
            res['image_line_ids'] = lines
        return res

    def action_apply(self):
        self.ensure_one()
        return self.publication_id._apply_tn_values_wizard(self)
