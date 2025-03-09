import base64
import re
from io import BytesIO
from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.converter import TextConverter
from pdfminer.layout import LAParams
from pdfminer.pdfpage import PDFPage
from io import StringIO
from odoo import models, fields, api
from odoo.exceptions import UserError


class InvoiceParser(models.Model):
    _inherit = 'helpdesk.ticket'

    x_invoice_id = fields.Many2one('account.move', string='Invoice')
    x_po_number = fields.Char(string='PO Number')
    x_cuit = fields.Char(string='CUIT')
    x_total_amount = fields.Float(string='Total Amount')
    x_iva_amount = fields.Float(string='IVA Amount')

    @api.model
    def procesar_facturas(self):
        # Get all tickets in 'Facturas nuevas' state
        tickets = self.search([('stage_id.name', '=', 'Facturas nuevas')])

        for ticket in tickets:
            # Get attachments from the chatter
            attachments = self.env['ir.attachment'].search([
                ('res_model', '=', 'helpdesk.ticket'),
                ('res_id', '=', ticket.id),
                ('mimetype', '=', 'application/pdf')
            ])

            if not attachments:
                # If no PDF attachments found, change state to 'Sin Adjuntos'
                sin_adjuntos_stage = self.env['helpdesk.stage'].search([
                    ('name', '=', 'Sin Adjuntos')
                ], limit=1)

                if sin_adjuntos_stage:
                    ticket.write({
                        'stage_id': sin_adjuntos_stage.id
                    })
                    # Log the change in chatter
                    ticket.message_post(
                        body="Ticket moved to 'Sin Adjuntos' - No PDF attachments found"
                    )
            else:
                # If PDF found, process each attachment
                for attachment in attachments:
                    self.generar_pago(attachment)

        return True

    def generar_pago(self, attachment):
        """
        Process the payment based on the PDF invoice and create draft invoice
        :param attachment: ir.attachment record
        """
        try:
            pdf_content = base64.b64decode(attachment.datas)
            pdf_file = BytesIO(pdf_content)

            # Extract text from PDF
            text_content = self.convert_pdf_to_text(pdf_file)

            # Search for PO number
            po_pattern = r'(?:P\.O\.|PO|Purchase Order)[:\s]*([A-Z0-9-]+)'
            po_match = re.search(po_pattern, text_content, re.IGNORECASE)

            if not po_match:
                # Move ticket to "Facturas sin PO" stage
                sin_po_stage = self.env['helpdesk.stage'].search([
                    ('name', '=', 'Facturas sin PO')
                ], limit=1)

                if sin_po_stage:
                    self.write({
                        'stage_id': sin_po_stage.id
                    })
                    self.message_post(
                        body=f"Ticket moved to 'Facturas sin PO' - No PO number found in {attachment.name}"
                    )
                return False

            # Extract PO number
            po_number = po_match.group(1)

            # Search for CUIT (Argentine tax ID)
            cuit_pattern = r'(?:CUIT|cuit)[:\s]*(\d{2}-\d{8}-\d{1})'
            cuit_match = re.search(cuit_pattern, text_content)

            if not cuit_match:
                raise UserError(f"No CUIT number found in invoice {attachment.name}")

            cuit = cuit_match.group(1)

            # Search for total amount
            total_pattern = r'(?:Total|TOTAL)[:\s]*\$?\s*([\d.,]+)'
            total_match = re.search(total_pattern, text_content)

            if not total_match:
                raise UserError(f"No total amount found in invoice {attachment.name}")

            total_amount = float(total_match.group(1).replace(',', ''))

            # Search for IVA amount
            iva_pattern = r'(?:IVA|iva|I\.V\.A\.)[:\s]*\$?\s*([\d.,]+)'
            iva_match = re.search(iva_pattern, text_content)

            if not iva_match:
                raise UserError(f"No IVA amount found in invoice {attachment.name}")

            iva_amount = float(iva_match.group(1).replace(',', ''))

            # Store the extracted information
            invoice_data = {
                'po_number': po_number,
                'cuit': cuit,
                'total_amount': total_amount,
                'iva_amount': iva_amount
            }

            # Log the extracted information in chatter
            self.message_post(
                body=f"""
                Information extracted from {attachment.name}:
                - PO Number: {po_number}
                - CUIT: {cuit}
                - Total Amount: ${total_amount:,.2f}
                - IVA Amount: ${iva_amount:,.2f}
                """
            )

            # After successfully extracting invoice_data, create draft invoice
            cuenta_contable = self.env['account.account'].search([('code', '=', '511100000')], limit=1)

            # Get purchase order
            purchase_order = self.env['purchase.order'].search([
                ('name', '=', invoice_data['po_number'])
            ], limit=1)

            if not purchase_order:
                self.message_post(
                    body=f"Purchase Order {invoice_data['po_number']} not found in the system"
                )
                return False

            # Get partner from CUIT
            partner = self.env['res.partner'].search([
                ('vat', '=', invoice_data['cuit'])
            ], limit=1)

            if not partner:
                self.message_post(
                    body=f"No partner found with CUIT {invoice_data['cuit']}"
                )
                return False

            # Create invoice values
            invoice_vals = {
                'move_type': 'in_invoice',
                'partner_id': partner.id,
                'invoice_date': fields.Date.today(),  # You might want to extract date from PDF
                'ref': f"PO {invoice_data['po_number']}",
                'invoice_line_ids': [(0, 0, {
                    'product_id': purchase_order.order_line[0].product_id.id if purchase_order.order_line else False,
                    'quantity': 1,
                    'price_unit': invoice_data['total_amount'] - invoice_data['iva_amount'],  # Base amount
                    'tax_ids': [(6, 0, [self.env.ref('l10n_ar.1_ri_tax_vat_21_purchases').id])],  # Assuming 21% IVA
                    'account_id': cuenta_contable.id,
                })],
                'purchase_id': purchase_order.id,
            }

            # Create invoice
            invoice = self.env['account.move'].create(invoice_vals)

            # Link invoice to ticket
            self.write({
                'x_invoice_id': invoice.id,
                'x_po_number': invoice_data['po_number'],
                'x_cuit': invoice_data['cuit'],
                'x_total_amount': invoice_data['total_amount'],
                'x_iva_amount': invoice_data['iva_amount']
            })

            # Log success in chatter
            self.message_post(
                body=f"""
                Draft invoice created successfully:
                - Invoice number: {invoice.name}
                - Partner: {partner.name}
                - PO: {invoice_data['po_number']}
                - Total Amount: ${invoice_data['total_amount']:,.2f}
                - IVA Amount: ${invoice_data['iva_amount']:,.2f}
                """
            )

            return invoice

        except Exception as e:
            self.message_post(
                body=f"Error creating invoice: {str(e)}"
            )
            raise UserError(f"Error creating invoice: {str(e)}")

    def convert_pdf_to_text(self, pdf_file):
        """
        Convert PDF file to text
        :param pdf_file: BytesIO object containing PDF
        :return: extracted text
        """
        resource_manager = PDFResourceManager()
        output_string = StringIO()
        codec = 'utf-8'
        laparams = LAParams()
        converter = TextConverter(resource_manager, output_string, codec=codec, laparams=laparams)
        interpreter = PDFPageInterpreter(resource_manager, converter)

        for page in PDFPage.get_pages(pdf_file, check_extractable=True):
            interpreter.process_page(page)

        text = output_string.getvalue()

        # Clean up
        converter.close()
        output_string.close()

        return text