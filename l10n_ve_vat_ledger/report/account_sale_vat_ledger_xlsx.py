# -*- coding: utf-8 -*-
##############################################################################
# Author: Mastercore Sinapsys Global®
# Copyright: 2019-Present.
# License OPL-1 (Odoo Proprietary License v1.0)
# See https://www.odoo.com/documentation/master/legal/licenses.html
#
#
##############################################################################
from odoo import _, models
from odoo.exceptions import UserError


class AccountSaleVatLedgerXlsx(models.AbstractModel):
    _inherit = 'report.l10n_ve_vat_ledger.account_vat_ledger_xlsx'

    def _get_sale_book_entries(self, ledger):
        invoices, withholdings = ledger._get_sale_book_source_data()
        layout = ledger.sale_book_format or 'documents'
        emissions = ledger._get_sale_book_emissions()
        unconfigured = invoices.filtered(
            lambda move: move._get_l10n_ve_vat_book_emission() not in emissions
        )
        if unconfigured:
            raise UserError(_(
                'Estos documentos utilizan modalidades de emision no incluidas en el libro: %s. '
                'Revise la configuracion de la compania y genere el libro con esas modalidades.',
                ', '.join(unconfigured[:10].mapped('display_name')),
            ))
        fiscal_moves = invoices.filtered(
            lambda move: move._get_l10n_ve_vat_book_emission() == 'fiscal'
        )
        if layout == 'documents' and fiscal_moves:
            raise UserError(_('Este periodo contiene documentos fiscales. Configure un formato con maquina fiscal.'))
        if layout == 'fiscal' and invoices.filtered(
            lambda move: move._get_l10n_ve_vat_book_emission() == 'digital'
        ):
            raise UserError(_('Este periodo contiene facturas digitales. Utilice el formato mixto.'))
        entries, covered_ids, warnings = [], set(), []
        if layout != 'documents':
            if not ledger._has_sale_book_fiscal_source():
                raise UserError(_('El formato fiscal requiere una integracion de reportes Z instalada.'))
            entries, covered_ids, warnings = ledger._get_sale_book_fiscal_entries(invoices)
            missing = fiscal_moves.filtered(lambda move: move.id not in covered_ids)
            if missing:
                raise UserError(_(
                    'No se encontraron reportes Z que cubran estos documentos: %s',
                    ', '.join(missing[:10].mapped('display_name')),
                ))
        labels = {'invoice': 'Factura', 'credit_note': 'Nota de credito', 'debit_note': 'Nota de debito'}
        for move in invoices:
            if move.id in covered_ids:
                continue
            emission = move._get_l10n_ve_vat_book_emission()
            kind = self._get_vat_book_document_kind(move)
            canceled = move.state == 'cancel'
            document = move._get_name_vat_ledger() or ''
            values = {
                'date': move.invoice_date or move.date,
                'document_type': labels[kind],
                'partner': 'ANULADA' if canceled else (move.partner_id.name or ''),
                'vat': '%s-%s' % (
                    move.partner_id.l10n_latam_identification_type_id.l10n_ve_code or '',
                    move.partner_id.vat or '',
                ),
                'branch': move.company_id.name,
                'affected': '', 'total': 0.0 if canceled else move.amount_total_signed,
            }
            if kind == 'credit_note' and move.reversed_entry_id:
                values['affected'] = move._get_reverse_name_vat_ledger() or ''
            elif kind == 'debit_note' and move.debit_origin_id:
                values['affected'] = move._get_debit_name_vat_ledger() or ''
            values[emission + '_document'] = document
            if emission == 'digital':
                values['digital_control'] = move.l10n_ve_control_number or ''
            values['debit_document'] = document if kind == 'debit_note' else ''
            values['credit_document'] = document if kind == 'credit_note' else ''
            breakdown = self._get_move_vat_breakdown(move)
            values['exempt'] = 0.0 if canceled else breakdown['base_exempt']
            for rate in (16, 8, 15):
                for key in ('base_%s' % rate, 'iva_%s' % rate):
                    values[key] = 0.0 if canceled else breakdown[key]
                values['alic_%s' % rate] = '' if canceled else breakdown['alic_%s' % rate]
            additional_lines = move.line_ids.filtered(
                lambda line: line.tax_line_id
                and round(abs(line.tax_line_id.amount), 2) == 31.0
            )
            sign = -1 if kind == 'credit_note' else 1
            additional_base = 0.0 if canceled else sign * sum(abs(line.tax_base_amount) for line in additional_lines)
            additional_tax = 0.0 if canceled else sign * sum(abs(line.balance) for line in additional_lines)
            entries.append({'values': values, 'emission': emission, 'kind': kind,
                            'company_id': move.company_id.id, 'id': move.id,
                            'summary_31': (additional_base, additional_tax)})
        for withholding in withholdings:
            related_moves = self._get_withholding_related_moves(withholding)
            if related_moves:
                selected = related_moves.filtered(lambda move: move.journal_id in ledger.journal_ids)
                if not selected:
                    continue
                if len(selected) != len(related_moves):
                    raise UserError(_(
                        'La retencion %s abarca diarios incluidos y excluidos. Seleccione todos sus diarios para no dividir su importe.',
                        withholding.name or withholding.id,
                    ))
            values = {
                'date': withholding.date or withholding.payment_id.date,
                'document_type': 'Retencion', 'partner': withholding.partner_id.name or '',
                'vat': '%s-%s' % (
                    withholding.partner_id.l10n_latam_identification_type_id.l10n_ve_code or '',
                    withholding.partner_id.vat or '',
                ),
                'branch': withholding.company_id.name,
                'voucher': withholding.name or '',
                'withholding_date': withholding.date or withholding.payment_id.date,
                'withheld': self._get_sale_withholding_signed_amount(withholding),
            }
            tax_lines = withholding.l10n_ve_move_line_taxes_ids or related_moves.line_ids.filtered(
                lambda line: line.tax_line_id
            )
            weights = {}
            for line in tax_lines:
                rate = round(abs(line.tax_line_id.amount), 2)
                if rate not in (16, 8, 31) or not line.balance:
                    continue
                kind = self._get_vat_book_document_kind(line.move_id)
                key = ('invoice' if rate == 31 else kind, int(rate))
                weights[key] = weights.get(key, 0.0) + abs(line.balance)
            entries.append({'values': values, 'emission': '', 'kind': 'withholding',
                            'company_id': withholding.company_id.id, 'id': withholding.id,
                            'summary_withholding_weights': weights})
        entries.sort(key=lambda entry: (
            entry['values']['date'], entry['company_id'],
            entry['values'].get('printer', ''), entry['id'], entry['kind'],
        ))
        return entries, warnings

    def _get_sale_book_summary(self, ledger, entries):
        labels = [
            (('invoice', 'exempt'), 'Total Ventas Internas No Gravadas'),
            (('credit_note', 'exempt'), 'Total Notas de Crédito No Gravadas'),
            (('debit_note', 'exempt'), 'Total Notas de Débito No Gravadas'),
            (('export', 'exempt'), 'Total Ventas de Exportación'),
            (('invoice', 16), 'Total Ventas Internas afectadas sólo alícuota general (16,00%)'),
            (('invoice', 8), 'Total Ventas Internas afectadas sólo alícuota reducida (8,00%)'),
            (('invoice', 31), 'Total Ventas Internas afectadas más adicional (31,00%)'),
            (('credit_note', 16), 'Total Notas de Crédito aplicadas en Ventas (16,00%)'),
            (('credit_note', 8), 'Total Notas de Crédito aplicadas en Ventas (8,00%)'),
            (('debit_note', 16), 'Total Notas de Débito aplicadas en Ventas (16,00%)'),
            (('debit_note', 8), 'Total Notas de Débito aplicadas en Ventas (8,00%)'),
        ]
        amounts = {key: [0.0, 0.0, 0.0, 0.0] for key, label in labels}
        grand_total = [0.0, 0.0, 0.0, 0.0]
        currency = ledger.company_id.currency_id
        for entry in entries:
            values = entry['values']
            grand_total[0] += values.get('total', 0.0)
            grand_total[3] += values.get('withheld', 0.0)
            if entry['kind'] == 'withholding':
                weights = entry.get('summary_withholding_weights', {})
                total_weight = sum(weights.values())
                if not total_weight:
                    continue
                remaining = values.get('withheld', 0.0)
                for index, (key, weight) in enumerate(weights.items()):
                    allocated = remaining if index == len(weights) - 1 else currency.round(
                        values.get('withheld', 0.0) * weight / total_weight
                    )
                    amounts[key][3] += allocated
                    remaining -= allocated
                continue
            kind = 'invoice' if entry['kind'] == 'fiscal' else entry['kind']
            additional_base, additional_tax = entry.get('summary_31', (0.0, 0.0))
            amounts[(kind, 'exempt')][0] += values.get('exempt', 0.0) - additional_base
            for rate in (16, 8, 31):
                base = additional_base if rate == 31 else values.get('base_%s' % rate, 0.0)
                tax = additional_tax if rate == 31 else values.get('iva_%s' % rate, 0.0)
                key = ('invoice' if rate == 31 else kind, rate)
                amounts[key][0] += base + tax
                amounts[key][1] += base
                amounts[key][2] += tax
                grand_total[1] += base
                grand_total[2] += tax
        return [(label, amounts[key]) for key, label in labels], grand_total

    def _get_sale_book_columns(self, ledger):
        emissions = ledger._get_sale_book_emissions()
        fiscal = 'fiscal' in emissions
        columns = [
            ('operation', 'Nro. operacion'), ('date', 'Fecha del documento'),
            ('document_type', 'Tipo de documento'),
            ('vat', 'RIF'), ('partner', 'Nombre o razon social'),
        ]
        if 'manual' in emissions:
            columns.append(('manual_document', 'Nro. de documento'))
        if 'digital' in emissions:
            columns.extend([
                ('digital_document', 'Nro. de documento'),
                ('digital_control', 'Nro. de control'),
            ])
        if fiscal:
            columns.extend([
                ('z_number', 'Nro. reporte Z'), ('printer', 'Maquina fiscal'),
                ('invoice_from', 'Primer Nro. de factura'),
                ('invoice_to', 'Ultimo Nro. de factura'),
            ])
        columns.append(('affected', 'Factura afectada'))
        if not fiscal:
            columns.append(('voucher', 'Numero comprobante de retencion IVA'))
        if ledger.sale_book_scope == 'consolidated':
            columns.append(('branch', 'Principal / sucursal'))
        columns.append(('total', 'Total Venta Incluyendo el IVA'))
        if fiscal:
            columns.append(('exempt', 'Ventas Internas No gravadas'))
        if not fiscal:
            columns.extend([
                ('third_exempt', 'Ventas no gravadas'), ('third_base', 'Base imponible'),
                ('third_rate', '% alicuota'), ('third_tax', 'Impuesto IVA'),
            ])
            columns.append(('exempt', 'Ventas Internas No gravadas'))
        for rate in (16, 8):
            for emission in emissions:
                columns.extend([
                    ('%s_base_%s' % (emission, rate), 'Base'),
                    ('%s_alic_%s' % (emission, rate), '%'),
                    ('%s_iva_%s' % (emission, rate), 'Impuesto'),
                ])
        if fiscal:
            columns.extend([
                ('withholding_date', 'Fecha comprobante de retencion IVA'),
                ('voucher', 'Numero comprobante de retencion IVA'),
            ])
        columns.append(('withheld', 'IVA retenido por el comprador'))
        return columns

    def _generate_sale_xlsx_report(self, workbook, ledger):
        ledger = ledger.with_context(ledger._get_sale_book_context())
        self = self.with_context(ledger.env.context)
        entries, warnings = self._get_sale_book_entries(ledger)
        emissions = ledger._get_sale_book_emissions()
        columns = self._get_sale_book_columns(ledger)
        positions = {key: column for column, (key, label) in enumerate(columns)}
        sheet = workbook.add_worksheet(ledger.name[:31])
        header = workbook.add_format({
            'bold': True, 'border': 1, 'align': 'center', 'valign': 'vcenter',
            'fg_color': '#a64d79', 'font_color': 'white', 'text_wrap': True,
        })
        title = workbook.add_format({'bold': True, 'border': 1, 'text_wrap': True})
        text = workbook.add_format({'border': 1, 'text_wrap': True})
        centered_text = workbook.add_format({'border': 1, 'text_wrap': True, 'align': 'center'})
        number = workbook.add_format({'border': 1, 'num_format': '#,##0.00', 'align': 'center'})
        date_format = workbook.add_format({'border': 1, 'num_format': 'dd-mm-yyyy', 'align': 'center'})
        total_format = workbook.add_format({
            'border': 1, 'bold': True, 'num_format': '#,##0.00', 'fg_color': '#f0f0f0',
            'align': 'center',
        })
        total_label = workbook.add_format({
            'border': 1, 'bold': True, 'fg_color': '#f0f0f0', 'align': 'left',
        })
        company = ledger.company_id
        sheet.merge_range('A1:D1', company.name, title)
        sheet.merge_range('A2:D2', '%s-%s' % (
            company.partner_id.l10n_latam_identification_type_id.l10n_ve_code or '',
            company.vat or '',
        ), title)
        sheet.merge_range('A3:H3', ledger.name, title)
        header_row = 6
        first_data_row = header_row + 1
        for row in range(3, header_row):
            sheet.set_row(row, 24.7)
        sheet.set_row(header_row, 26.55)
        for row in range(3, first_data_row):
            for column in range(len(columns)):
                sheet.write_blank(row, column, None, header)
        tax_widths = {
            16: {'manual': (103, 94, 110), 'digital': (114, 88, 113), 'fiscal': (93, 89, 104)},
            8: {'manual': (93, 82, 104), 'digital': (103, 81, 106), 'fiscal': (91, 88, 103)},
        }
        column_widths = {'partner': 285, 'branch': 285}
        for rate, widths in tax_widths.items():
            for emission in emissions:
                for field, width in zip(('base', 'alic', 'iva'), widths[emission]):
                    column_widths['%s_%s_%s' % (emission, field, rate)] = width
        for column, (key, label) in enumerate(columns):
            sheet.set_column_pixels(column, column, column_widths.get(key, 189))
            sheet.write(header_row, column, label, header)
        for label, keys in (
            ('Forma libre', ('manual_document',)),
            ('Factura Digital', ('digital_document', 'digital_control')),
            ('Maquina fiscal', ('z_number', 'printer', 'invoice_from', 'invoice_to')),
        ):
            if keys[0] not in positions:
                continue
            first, last = positions[keys[0]], positions[keys[-1]]
            if first == last:
                sheet.write(header_row - 1, first, label, header)
            else:
                sheet.merge_range(header_row - 1, first, header_row - 1, last, label, header)
        if 'third_base' in positions:
            sheet.merge_range(header_row - 1, positions['third_exempt'],
                              header_row - 1, positions['third_tax'],
                              'Ventas por cuenta de terceros', header)
        sheet.merge_range(header_row - 3, positions['%s_base_16' % emissions[0]],
                          header_row - 3, positions['%s_iva_8' % emissions[-1]],
                          'Ventas Internas', header)
        emission_labels = {
            'manual': 'A Contribuyente Forma Libre',
            'digital': 'A Contribuyente Factura Digital',
            'fiscal': 'A Contribuyente Maquina Fiscal',
        }
        rate_labels = {
            16: 'Alicuota General',
            8: 'Alicuota Reducida',
        }
        for rate, rate_label in rate_labels.items():
            for emission in emissions:
                first = positions['%s_base_%s' % (emission, rate)]
                last = positions['%s_iva_%s' % (emission, rate)]
                sheet.merge_range(header_row - 2, first, header_row - 2, last,
                                  emission_labels[emission], header)
                sheet.merge_range(header_row - 1, first, header_row - 1, last,
                                  rate_label, header)

        numeric_keys = {'total', 'exempt', 'third_exempt', 'third_base', 'third_tax', 'withheld'}
        rate_keys = set()
        for emission in emissions:
            for rate in rate_labels:
                numeric_keys.update(('%s_base_%s' % (emission, rate), '%s_iva_%s' % (emission, rate)))
                rate_keys.add('%s_alic_%s' % (emission, rate))
        identifier_keys = {
            'operation', 'manual_document', 'digital_document', 'digital_control',
            'voucher', 'affected', 'vat', 'z_number', 'printer',
            'invoice_from', 'invoice_to', 'debit_document', 'credit_document', 'third_rate',
        }
        totals = dict.fromkeys(numeric_keys, 0.0)
        for row, entry in enumerate(entries, start=first_data_row):
            values = dict(entry['values'], operation=row - first_data_row + 1)
            if entry['emission'] in emissions:
                for rate in rate_labels:
                    for key in ('base_%s' % rate, 'iva_%s' % rate, 'alic_%s' % rate):
                        values[entry['emission'] + '_' + key] = values.pop(key, '')
            for column, (key, label) in enumerate(columns):
                value = values.get(key, '')
                style = date_format if key in ('date', 'withholding_date') else (
                    number if key in numeric_keys else (
                        centered_text if key in identifier_keys or key in rate_keys else text
                    )
                )
                # Document numbers are strings so fiscal leading zeros survive.
                if isinstance(value, str) and value:
                    sheet.write_string(row, column, value, style)
                else:
                    sheet.write(row, column, value, style)
                if key in totals and isinstance(value, (int, float)):
                    totals[key] += value
        row = first_data_row + len(entries)
        for column, (key, label) in enumerate(columns):
            sheet.write(row, column, 'TOTAL' if key == 'partner' else totals.get(key, ''),
                        total_label if key == 'partner' else total_format)
        for detail_row in range(first_data_row, row + 1):
            sheet.set_row(detail_row, 13.55)
        row += 4
        sheet.set_row(row, 40)
        for column, label in enumerate((
            'RESUMEN GENERAL', 'Total Ventas incluyendo IVA', 'Base imponible',
            'Débito Fiscal', 'IVA Retenido por Clientes',
        )):
            sheet.write(row, column, label, header)
        summary, grand_total = self._get_sale_book_summary(ledger, entries)
        for label, amounts in summary:
            row += 1
            sheet.set_row(row, 54 if len(label) > 45 else 30)
            sheet.write(row, 0, label, title)
            for column, amount in enumerate(amounts, start=1):
                sheet.write_number(row, column, self._round_book_amount(amount), number)
        row += 1
        sheet.set_row(row, 26.55)
        sheet.write(row, 0, 'TOTAL GENERAL', total_label)
        for column, amount in enumerate(grand_total, start=1):
            sheet.write_number(row, column, self._round_book_amount(amount), total_format)
        for warning in dict.fromkeys(warnings):
            row += 2
            sheet.merge_range(row, 0, row, 7, warning, title)
