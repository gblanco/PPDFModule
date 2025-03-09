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
        # Get all tickets in 'Facturas nuevas' state for 'Pago a Proveedores' team
        pago_proveedores_team = self.env['helpdesk.team'].search([
            ('alias_name', '=', 'proveedores'),
            ('alias_domain', '=', 'bmisa.odoo.com')
        ], limit=1)
        
        if not pago_proveedores_team:
            return False
            
        tickets = self.search([
            ('team_id', '=', pago_proveedores_team.id),
            ('stage_id.name', '=', 'Facturas nuevas')
        ])

        # Get stage IDs for status changes
        sin_pdf_stage = self.env['helpdesk.stage'].search([
            ('name', '=', 'Tickets sin PDF')
        ], limit=1)
        
        sin_po_stage = self.env['helpdesk.stage'].search([
            ('name', '=', 'PDF sin PO#')
        ], limit=1)
        
        if not sin_pdf_stage or not sin_po_stage:
            raise UserError("Required stages 'Tickets sin PDF' or 'PDF sin PO#' not found")

        for ticket in tickets:
            has_pdf = False
            pdf_attachments = []
            
            # Check for PDFs in chatter messages
            for message in ticket.message_ids:
                message_attachments = self.env['ir.attachment'].search([
                    ('res_model', '=', 'mail.message'),
                    ('res_id', '=', message.id),
                    ('mimetype', '=', 'application/pdf')
                ])
                
                if message_attachments:
                    has_pdf = True
                    pdf_attachments.extend(message_attachments)
            
            # Also check for PDFs directly attached to the ticket
            ticket_attachments = self.env['ir.attachment'].search([
                ('res_model', '=', 'helpdesk.ticket'),
                ('res_id', '=', ticket.id),
                ('mimetype', '=', 'application/pdf')
            ])
            
            if ticket_attachments:
                has_pdf = True
                pdf_attachments.extend(ticket_attachments)
                
            if not has_pdf:
                # If no PDF attachments found, change state to 'Tickets sin PDF'
                ticket.write({
                    'stage_id': sin_pdf_stage.id
                })
                # Log the change in chatter
                ticket.message_post(
                    body="Ticket moved to 'Tickets sin PDF' - No PDF attachments found in messages"
                )
            else:
                # Process each PDF attachment
                po_found = False
                for attachment in pdf_attachments:
                    result = self.process_invoice_pdf(ticket, attachment, sin_po_stage)
                    if result:
                        po_found = True
                        break
                
                # If no PO was found in any of the PDFs, move to 'PDF sin PO#'
                if not po_found and ticket.stage_id.id != sin_po_stage.id:
                    ticket.write({
                        'stage_id': sin_po_stage.id
                    })
                    ticket.message_post(
                        body="Ticket moved to 'PDF sin PO#' - No valid PO found in any PDF"
                    )

        return True
        
    def process_invoice_pdf(self, ticket, attachment, sin_po_stage):
        """
        Process a PDF invoice attachment
        :param ticket: helpdesk.ticket record
        :param attachment: ir.attachment record
        :param sin_po_stage: helpdesk.stage record for 'PDF sin PO#'
        :return: Boolean indicating success
        """
        try:
            # Get PDF content
            if attachment.res_model == 'mail.message':
                # For attachments in messages
                pdf_content = base64.b64decode(attachment.datas)
            else:
                # For attachments directly on the ticket
                pdf_content = base64.b64decode(attachment.datas)
                
            pdf_file = BytesIO(pdf_content)

            # Extract text from PDF
            text_content = self.convert_pdf_to_text(pdf_file)

            # Search for PO number
            po_pattern = r'(?:P\.O\.|PO|Purchase Order)[:\s#]*([A-Z0-9][-A-Z0-9]*)'
            po_match = re.search(po_pattern, text_content, re.IGNORECASE)

            if not po_match:
                # No PO found in this PDF
                ticket.message_post(
                    body=f"No PO number found in PDF: {attachment.name}"
                )
                return False

            # Extract PO number
            po_number = po_match.group(1).strip()
            
            # Verify the PO exists in the system
            purchase_order = self.env['purchase.order'].search([
                ('name', '=ilike', po_number)
            ], limit=1)
            
            if not purchase_order:
                ticket.message_post(
                    body=f"PO number found ({po_number}), but it doesn't exist in the system"
                )
                return False

            # Extract remaining invoice data
            invoice_data = self.extract_invoice_data(text_content, po_number)
            
            # Create draft invoice
            invoice = self.create_draft_invoice(ticket, invoice_data, purchase_order, attachment)
            
            return True if invoice else False
            
        except Exception as e:
            ticket.message_post(
                body=f"Error processing PDF attachment {attachment.name}: {str(e)}"
            )
            return False
            
    def extract_invoice_data(self, text_content, po_number):
        """
        Extract invoice data from PDF text content
        :param text_content: Text extracted from PDF
        :param po_number: PO number extracted from PDF
        :return: Dictionary with invoice data
        """
        # Search for CUIT (Argentine tax ID)
        cuit_pattern = r'(?:CUIT|cuit)[:\s]*(\d{2}-\d{8}-\d{1})'
        cuit_match = re.search(cuit_pattern, text_content)
        cuit = cuit_match.group(1) if cuit_match else ''

        # Search for total amount
        total_pattern = r'(?:Total|TOTAL)[:\s]*\$?\s*([\d.,]+)'
        total_match = re.search(total_pattern, text_content)
        total_amount = float(total_match.group(1).replace('.', '').replace(',', '.')) if total_match else 0.0

        # Search for IVA amount
        iva_pattern = r'(?:IVA|iva|I\.V\.A\.)[:\s]*\$?\s*([\d.,]+)'
        iva_match = re.search(iva_pattern, text_content)
        iva_amount = float(iva_match.group(1).replace('.', '').replace(',', '.')) if iva_match else 0.0

        # If IVA amount is not found, estimate it as 21% of the base amount
        if not iva_match and total_amount > 0:
            base_amount = total_amount / 1.21  # Assuming 21% IVA
            iva_amount = total_amount - base_amount

        # Store the extracted information
        invoice_data = {
            'po_number': po_number,
            'cuit': cuit,
            'total_amount': total_amount,
            'iva_amount': iva_amount,
            'base_amount': total_amount - iva_amount
        }
        
        return invoice_data

    def create_draft_invoice(self, ticket, invoice_data, purchase_order, attachment):
        """
        Create a draft invoice based on extracted data
        :param ticket: helpdesk.ticket record
        :param invoice_data: Dictionary with invoice data
        :param purchase_order: purchase.order record
        :param attachment: ir.attachment record
        :return: account.move record or False
        """
        try:
            # Find appropriate account
            cuenta_contable = self.env['account.account'].search([('code', '=', '511100000')], limit=1)
            if not cuenta_contable:
                # Use a fallback account for expenses
                cuenta_contable = self.env['account.account'].search([
                    ('account_type', '=', 'expense')
                ], limit=1)

            # Get partner from purchase order
            partner = purchase_order.partner_id
            
            # If CUIT is available, verify partner
            if invoice_data['cuit']:
                cuit_partner = self.env['res.partner'].search([
                    ('vat', '=', invoice_data['cuit'])
                ], limit=1)
                
                if cuit_partner and cuit_partner.id != partner.id:
                    ticket.message_post(
                        body=f"Warning: CUIT in invoice ({invoice_data['cuit']}) belongs to {cuit_partner.name}, but PO {invoice_data['po_number']} is for {partner.name}"
                    )

            # Get IVA tax
            iva_tax = self.env.ref('l10n_ar.1_ri_tax_vat_21_purchases', raise_if_not_found=False)
            if not iva_tax:
                # Generic fallback for VAT tax
                iva_tax = self.env['account.tax'].search([
                    ('type_tax_use', '=', 'purchase'),
                    ('amount', '=', 21)
                ], limit=1)

            # Create invoice values
            invoice_vals = {
                'move_type': 'in_invoice',
                'partner_id': partner.id,
                'invoice_date': fields.Date.today(),  # You might want to extract date from PDF
                'ref': f"PO {invoice_data['po_number']}",
                'invoice_line_ids': [(0, 0, {
                    'product_id': purchase_order.order_line[0].product_id.id if purchase_order.order_line else False,
                    'name': f"Invoice from {attachment.name}",
                    'quantity': 1,
                    'price_unit': invoice_data['base_amount'],
                    'tax_ids': [(6, 0, [iva_tax.id])] if iva_tax else [],
                    'account_id': cuenta_contable.id,
                })],
                'purchase_id': purchase_order.id,
            }

            # Create invoice
            invoice = self.env['account.move'].create(invoice_vals)

            # Link invoice to ticket
            ticket.write({
                'x_invoice_id': invoice.id,
                'x_po_number': invoice_data['po_number'],
                'x_cuit': invoice_data['cuit'],
                'x_total_amount': invoice_data['total_amount'],
                'x_iva_amount': invoice_data['iva_amount']
            })

            # Log success in chatter
            ticket.message_post(
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
            ticket.message_post(
                body=f"Error creating invoice: {str(e)}"
            )
            return False

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