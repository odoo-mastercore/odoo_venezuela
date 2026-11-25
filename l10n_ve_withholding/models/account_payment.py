# -*- coding: utf-8 -*-
##############################################################################
# Author: SINAPSYS GLOBAL SA || MASTERCORE SAS
# Copyleft: 2022-Present.
#
#
###############################################################################
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

class AccountPayment(models.Model):
    _inherit = "account.payment"

    #created to record retention percentages
    comment_withholding = fields.Char('Comment withholding')
    concept_withholding = fields.Char('Concept withholding')
    withholding_distribution = fields.Boolean(
        'tiene una distribucion de retencion?')
    withholding_distribution_ids = fields.One2many(
        'withholding.distribution', 'payment_id',
        string='distribucion de retencion'
    )
    l10n_ve_cancelled_payment_group_id = fields.Many2one(
        'account.payment.group',
        string='Grupo original de la retención cancelada',
        readonly=True,
        copy=False,
        check_company=True,
        ondelete='restrict',
    )

    def _l10n_ve_numbered_withholdings(self):
        return self.filtered(lambda payment: (payment.withholding_number or '').strip() not in ('', '/'))

    def _l10n_ve_check_numbered_withholding_unlink(self):
        if self._l10n_ve_numbered_withholdings():
            raise UserError(_(
                'No puede eliminar una retención con número de comprobante. '
                'Cancélela o quítela de las líneas activas de su grupo de pago.'
            ))

    def unlink(self):
        self._l10n_ve_check_numbered_withholding_unlink()
        return super().unlink()

    def write(self, vals):
        if not {'payment_group_id', 'withholding_number'} & vals.keys():
            return super().write(vals)

        numbered = self._l10n_ve_numbered_withholdings()
        if not numbered:
            return super().write(vals)
        if 'withholding_number' in vals and str(vals['withholding_number'] or '').strip() in ('', '/'):
            raise UserError(_('No puede borrar el número de comprobante de una retención.'))
        if 'payment_group_id' not in vals:
            return super().write(vals)

        group_id = vals['payment_group_id']
        if group_id:
            if numbered.filtered(lambda payment: (
                payment.l10n_ve_cancelled_payment_group_id
                or (payment.payment_group_id and payment.payment_group_id.id != group_id)
            )):
                raise UserError(_('No puede trasladar a otro grupo una retención con número de comprobante.'))
            return super().write(vals)

        to_cancel = numbered.filtered('payment_group_id')
        if not to_cancel:
            return super().write(vals)
        if to_cancel.payment_group_id.filtered(lambda group: group.state not in ('draft', 'confirmed')):
            raise UserError(_('Para quitar una retención numerada, vuelva primero el grupo de pago a borrador.'))
        if ('state' in vals and vals['state'] != 'cancel') or (
            'withholding_number' in vals
            and to_cancel.filtered(lambda payment: payment.withholding_number != vals['withholding_number'])
        ):
            raise UserError(_('Al quitar una retención debe quedar cancelada con su mismo número de comprobante.'))

        to_cancel.check_access_rights('write')
        to_cancel.check_access_rule('write')
        original_groups = {
            group: to_cancel.filtered(lambda payment: payment.payment_group_id == group)
            for group in to_cancel.payment_group_id
        }
        with self.env.cr.savepoint():
            # In v16 resetting the payment to draft removes its reconciliations.
            posted = to_cancel.filtered(lambda payment: payment.state == 'posted')
            if posted:
                posted.action_draft()
            active = to_cancel.filtered(lambda payment: payment.state != 'cancel')
            if active:
                active.action_cancel()
            if to_cancel.filtered(lambda payment: payment.state != 'cancel'):
                raise UserError(_('No se pudo cancelar la retención. La línea permanece en el grupo de pago.'))
            for group, payments in original_groups.items():
                super(AccountPayment, payments).write({
                    **vals,
                    'l10n_ve_cancelled_payment_group_id': group.id,
                })
            remaining = self - to_cancel
            return super(AccountPayment, remaining).write(vals) if remaining else True

    def _get_fiscal_period(self, date):
        str_date = str(date).split('-')
        vals = 'AÑO '+str_date[0]+' MES '+str_date[1]
        return vals

    @api.onchange('journal_id')
    def _onchange_compute_amount_currency(self):
        for rec in self:
            pass
            if rec.other_currency and rec.payment_group_id:
                if rec.payment_group_id.payments_amount <= 0:
                    rec.amount = rec.payment_group_id.selected_finacial_debt
                if rec.payment_group_id and rec.payment_group_id.payments_amount > 0:
                    rec.amount = 0
                    payments_amount = rec.payment_group_id.selected_finacial_debt - \
                        rec.payment_group_id.payments_amount
                    rec.amount = rec.company_id.currency_id._convert(
                        payments_amount, rec.currency_id, rec.company_id, rec.date)
            if not rec.other_currency and rec.payment_group_id:
                rec.amount = rec.payment_group_id.selected_finacial_debt
                if rec.payment_group_id and rec.payment_group_id.payments_amount > 0:
                    payments_amount = rec.payment_group_id.payments_amount - rec.amount
                    rec.amount = rec.payment_group_id.selected_finacial_debt - \
                        payments_amount

    @api.onchange('date')
    def _onchange_compute_amount_currency_date(self):
        for rec in self:
            if rec.other_currency and rec.payment_group_id:
                rec.amount_company_currency = rec.currency_id._convert(
                    rec.amount, rec.company_id.currency_id,
                    rec.company_id, rec.date)

    def action_post(self):
        for pay in self:
            if pay.payment_group_id and pay.payment_group_id.to_pay_move_line_ids:
                to_pay = pay.payment_group_id.to_pay_move_line_ids[0]
                if to_pay.move_id.move_type == 'in_refund' and  pay.computed_withholding_amount:
                    pay.write({
                        'payment_type': 'inbound',
                    })
        return super(AccountPayment, self).action_post()

    def get_sustraendo(self):
        if self.concept_withholding:
            code_seniat = self.concept_withholding.split(' - ')[0]
            activity_name = self.concept_withholding.split(' - ')[1]
            regimen_id = self.env['seniat.tabla.islr']\
                .search([('code_seniat','=',code_seniat), 
                            ('activity_name','=',activity_name)],limit=1)
            if regimen_id:
                if regimen_id.type_subtracting == 'amount':
                    return self.format_miles_number(regimen_id.banda_calculo_ids[0].withholding_amount)
        return False
    
    def format_miles_number(self, number):
        return '{:,.2f}'.format(number).replace(",", "@").replace(".", ",").replace("@", ".")
