import base64
import re
import logging
from io import BytesIO
from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.converter import TextConverter
from pdfminer.layout import LAParams
from pdfminer.pdfpage import PDFPage
from io import StringIO
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class InvoiceParser(models.Model):
    _inherit = 'helpdesk.ticket'

    x_invoice_id = fields.Many2one('account.move', string='Invoice')
    x_po_number = fields.Char(string='PO Number')
    x_cuit = fields.Char(string='CUIT')
    x_total_amount = fields.Float(string='Total Amount')
    x_iva_amount = fields.Float(string='IVA Amount')

    def procesar_facturas(self):
        """
        Process invoices for tickets with 'Facturas nuevas' status
        This method works on recordsets and is called directly from record
        :return: Boolean indicating success
        """
        if not self:
            # Get the 'Facturas Nuevas' stage
            facturas_nuevas_stage = self.env.ref('bmi_invoice_parser.stage_facturas_nuevas', raise_if_not_found=False)
            if not facturas_nuevas_stage:
                facturas_nuevas_stage = self.env['helpdesk.stage'].search([
                    ('name', 'ilike', 'Facturas Nuevas')
                ], limit=1)

                if not facturas_nuevas_stage:
                    _logger.error("No se encontró la etapa 'Facturas Nuevas'. No se pueden procesar tickets.")
                    return False

            # Intentar buscar por equipo si está configurado
            pago_proveedores_team = self.env['helpdesk.team'].search([
                '|',
                ('alias_name', '=', 'proveedores'),
                ('name', 'ilike', 'Pago a Proveedores')
            ], limit=1)

            # Construir dominio de búsqueda de tickets
            domain = [('stage_id', '=', facturas_nuevas_stage.id)]
            if pago_proveedores_team:
                domain.append(('team_id', '=', pago_proveedores_team.id))

            tickets = self.search(domain)
        else:
            tickets = self

        return self._procesar_tickets(tickets)

    def _procesar_tickets(self, tickets):
        """
        Process a recordset of tickets
        :param tickets: helpdesk.ticket recordset
        :return: Boolean indicating success
        """
        if not tickets:
            _logger.info("No hay tickets para procesar")
            return False

        _logger.info(f"Procesando {len(tickets)} tickets")

        # Get stage IDs for status changes - use our module's XML IDs to ensure we get the right stages
        sin_pdf_stage = self.env.ref('bmi_invoice_parser.stage_tickets_sin_pdf', raise_if_not_found=False)
        if not sin_pdf_stage:
            sin_pdf_stage = self.env['helpdesk.stage'].search([
                ('name', '=', 'Tickets sin PDF')
            ], limit=1)
            if not sin_pdf_stage:
                sin_pdf_stage = self.env['helpdesk.stage'].create({
                    'name': 'Tickets sin PDF',
                    'sequence': 2,
                })

        sin_po_stage = self.env.ref('bmi_invoice_parser.stage_pdf_sin_po', raise_if_not_found=False)
        if not sin_po_stage:
            sin_po_stage = self.env['helpdesk.stage'].search([
                ('name', '=', 'PDF sin PO#')
            ], limit=1)
            if not sin_po_stage:
                sin_po_stage = self.env['helpdesk.stage'].create({
                    'name': 'PDF sin PO#',
                    'sequence': 3,
                })

        po_inexistente_stage = self.env.ref('bmi_invoice_parser.stage_po_inexistente', raise_if_not_found=False)
        if not po_inexistente_stage:
            po_inexistente_stage = self.env['helpdesk.stage'].search([
                ('name', '=', 'PO# Inexistente')
            ], limit=1)
            if not po_inexistente_stage:
                po_inexistente_stage = self.env['helpdesk.stage'].create({
                    'name': 'PO# Inexistente',
                    'sequence': 4,
                })

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
                    body="Ticket movido a 'Tickets sin PDF' - No se encontraron adjuntos PDF en los mensajes"
                )
            else:
                # Process each PDF attachment
                po_found = False
                po_inexistente = False

                for attachment in pdf_attachments:
                    result, is_po_inexistente = self.process_invoice_pdf(ticket, attachment, sin_po_stage,
                                                                         po_inexistente_stage)
                    if result:
                        po_found = True
                        break
                    elif is_po_inexistente:
                        po_inexistente = True
                        break

                # Si no se encontró PO y no se marcó como PO# inexistente, mover a 'PDF sin PO#'
                if not po_found and not po_inexistente and ticket.stage_id.id != sin_po_stage.id:
                    ticket.write({
                        'stage_id': sin_po_stage.id
                    })
                    ticket.message_post(
                        body="Ticket movido a 'PDF sin PO#' - No se encontró OC válida en ningún PDF"
                    )

        return True

    def process_invoice_pdf(self, ticket, attachment, sin_po_stage, po_inexistente_stage):
        """
        Process a PDF invoice attachment
        :param ticket: helpdesk.ticket record
        :param attachment: ir.attachment record
        :param sin_po_stage: helpdesk.stage record for 'PDF sin PO#'
        :param po_inexistente_stage: helpdesk.stage record for 'PO# Inexistente'
        :return: Tuple (Boolean indicating success, Boolean indicating if PO# inexistente)
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

            # Handle the "Pedido de compra #P03351" format specifically
            pedido_pattern = r'pedido de compra[^\n]*?#P([0-9]{4,})'
            pedido_match = re.search(pedido_pattern, text_content, re.IGNORECASE)
            if pedido_match:
                pedido_po = pedido_match.group(1).strip()
                _logger.info(f"Encontrada referencia especial de 'Pedido de compra': #P{pedido_po}")

                # Check if this specific PO exists
                pedido_purchase_order = self.env['purchase.order'].search([
                    '|', '|',
                    ('name', '=ilike', f"P{pedido_po}"),
                    ('name', '=ilike', f"#P{pedido_po}"),
                    ('name', '=ilike', pedido_po)
                ], limit=1)

                if pedido_purchase_order:
                    _logger.info(f"OC coincidente encontrada para 'Pedido de compra': {pedido_purchase_order.name}")
                    purchase_order = pedido_purchase_order
                    po_number = pedido_po
                    original_po = f"#P{pedido_po}"

                    # Extract invoice data and create invoice
                    invoice_data = self.extract_invoice_data(text_content, po_number)
                    invoice = self.create_draft_invoice(ticket, invoice_data, purchase_order, attachment)
                    return (True if invoice else False, False)
                else:
                    # Mover al estado "PO# Inexistente" si se encuentra pero no existe
                    ticket.write({
                        'stage_id': po_inexistente_stage.id,
                        'x_po_number': f"P{pedido_po}"  # Guardar el número de PO aunque no exista
                    })
                    ticket.message_post(
                        body=f"Ticket movido a 'PO# Inexistente' - Se encontró el número de OC (#P{pedido_po}) en el PDF pero no existe en el sistema."
                    )
                    return (False, True)

            # Extract PO number
            po_number = self.extract_po_number(text_content)

            if not po_number:
                # No PO found in this PDF
                ticket.message_post(
                    body=f"No se encontró número de OC en el PDF: {attachment.name}<br/>"
                         f"Por favor, verifique si esta factura contiene una referencia de orden de compra."
                )
                return (False, False)

            original_po = po_number

            # Clean up potential prefixes in the PO number
            if po_number.startswith('#'):
                po_number = po_number[1:]

            # Strip additional characters that might be present
            po_number = re.sub(r'^[^A-Z0-9]+', '', po_number, flags=re.IGNORECASE)

            # Log the PO number extraction
            _logger.info(f"Extracción de OC del PDF: Coincidencia original: {original_po}, Limpio: {po_number}")

            # Create versions of the PO number to search for
            search_variants = [
                po_number,
                'P' + po_number if not po_number.startswith('P') else po_number,
                '#P' + po_number if not po_number.startswith('#P') else po_number,
                '#PO' + po_number if not po_number.startswith('#PO') else po_number,
                po_number.lstrip('P'),  # In case the PO is stored without the P prefix
                po_number.lstrip('#P'),  # In case the PO is stored without the #P prefix
                po_number.lstrip('#PO')  # In case the PO is stored without the #PO prefix
            ]

            # Remove duplicates and empty strings
            search_variants = [v for v in set(search_variants) if v]

            # Build domain for search
            domain = []
            for variant in search_variants:
                domain.append(('name', '=ilike', variant))

            if len(domain) > 1:
                domain = ['|'] * (len(domain) - 1) + domain

            # Verify the PO exists in the system
            purchase_order = self.env['purchase.order'].search(domain, limit=1)

            if purchase_order:
                _logger.info(f"OC coincidente encontrada: {purchase_order.name}")
            else:
                _logger.warning(f"No se encontró OC coincidente para las variantes: {search_variants}")

                # Try a more permissive search
                number_only = re.sub(r'[^0-9]', '', po_number)
                if number_only and len(number_only) >= 4:
                    _logger.info(f"Intentando búsqueda solo por números con: {number_only}")
                    # Search for POs containing this number sequence
                    purchase_order = self.env['purchase.order'].search([
                        ('name', 'ilike', number_only)
                    ], limit=1)

                    if purchase_order:
                        _logger.info(f"OC coincidente encontrada con búsqueda solo por números: {purchase_order.name}")

            if not purchase_order:
                # Try with a more extended search for patterns like "#P03351" where # might be treated as a comment in regex
                extended_search_variants = search_variants + [
                    f"P{po_number}" if po_number.isdigit() else po_number,
                    f"PO{po_number}" if po_number.isdigit() else po_number
                ]
                extended_search_variants = list(set(extended_search_variants))

                extended_domain = []
                for variant in extended_search_variants:
                    if variant:
                        extended_domain.append(('name', 'ilike', variant))

                if len(extended_domain) > 1:
                    extended_domain = ['|'] * (len(extended_domain) - 1) + extended_domain

                purchase_order = self.env['purchase.order'].search(extended_domain, limit=1)

                if purchase_order:
                    _logger.info(f"OC coincidente encontrada con búsqueda extendida: {purchase_order.name}")

            if not purchase_order:
                # Cambiado: Mover el ticket al estado "PO# Inexistente" en lugar de solo enviar un mensaje
                ticket.write({
                    'stage_id': po_inexistente_stage.id,
                    'x_po_number': original_po  # Guardar el número de PO aunque no exista
                })
                ticket.message_post(
                    body=f"Ticket movido a 'PO# Inexistente'<br/>"
                         f"Se extrajo número de OC ({po_number}) del PDF, pero no existe en el sistema.<br/>"
                         f"Formato original: {original_po}<br/>"
                         f"Se buscaron las variaciones: {', '.join(search_variants)}<br/>"
                         f"También se intentó con búsqueda extendida."
                )
                return (False, True)

            # Extract remaining invoice data
            invoice_data = self.extract_invoice_data(text_content, po_number)

            # Create draft invoice
            invoice = self.create_draft_invoice(ticket, invoice_data, purchase_order, attachment)

            return (True if invoice else False, False)

        except Exception as e:
            ticket.message_post(
                body=f"Error al procesar el PDF adjunto {attachment.name}: {str(e)}"
            )
            return (False, False)

    def extract_po_number(self, text_content):
        """
        Extract PO number from text content using multiple patterns
        :param text_content: Text extracted from PDF
        :return: Extracted PO number or False
        """
        # List of all possible PO patterns
        patterns = [
            # English patterns
            r'(?:P\.O\.|PO|Purchase Order)[:\s#]*([A-Z0-9][-A-Z0-9]*)',
            r'(?:P|#P|#PO)[:\s#]*([0-9]{4,})',

            # Spanish patterns (OC = Orden de Compra)
            r'(?:OC|OC#|OCN|OCN#)[:\s#]*([A-Z0-9][-A-Z0-9]*)',
            r'(?:O\.C\.|O\.C\.#)[:\s#]*([A-Z0-9][-A-Z0-9]*)',

            # Looking for standalone number patterns near keywords
            r'(?:orden de compra|orden|purchase|compra|pedido)[^\n]*?([A-Z0-9][-A-Z0-9]{4,})',

            # Specific patterns with "Pedido de compra"
            r'pedido de compra[:\s#]*([A-Z0-9][-A-Z0-9]*)',
            r'pedido de compra[^\n]*?#P([0-9]{4,})',
            r'pedido de compra[^\n]*?#([0-9]{4,})',

            # Last resort - look for patterns that might be PO numbers
            r'(?<!\w)P([0-9]{4,})(?!\w)',
            r'(?<!\w)#P([0-9]{4,})(?!\w)',
            r'(?<!\w)#PO([0-9]{4,})(?!\w)',
            r'(?<!\w)OC([0-9]{4,})(?!\w)'
        ]

        # Try each pattern until we find a match
        for pattern in patterns:
            match = re.search(pattern, text_content, re.IGNORECASE)
            if match:
                po_number = match.group(1).strip()
                _logger.info(f"Encontrada coincidencia de OC con patrón {pattern}: {po_number}")
                return po_number

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
            if invoice_data.get('cuit'):
                cuit_partner = self.env['res.partner'].search([
                    ('vat', '=', invoice_data['cuit'])
                ], limit=1)

                if cuit_partner and cuit_partner.id != partner.id:
                    ticket.message_post(
                        body=f"Advertencia: El CUIT en la factura ({invoice_data['cuit']}) pertenece a {cuit_partner.name}, pero la OC {invoice_data['po_number']} es para {partner.name}"
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
                Factura en borrador creada exitosamente:
                - Número de factura: {invoice.name}
                - Proveedor: {partner.name}
                - OC: {invoice_data['po_number']}
                - Monto Total: ${invoice_data['total_amount']:,.2f}
                - Monto IVA: ${invoice_data['iva_amount']:,.2f}
                """
            )

            return invoice

        except Exception as e:
            ticket.message_post(
                body=f"Error al crear la factura: {str(e)}"
            )
            return False

    def convert_pdf_to_text(self, pdf_file):
        """
        Convert PDF file to text
        :param pdf_file: BytesIO object containing PDF
        :return: extracted text
        """
        try:
            resource_manager = PDFResourceManager()
            output_string = StringIO()
            codec = 'utf-8'
            laparams = LAParams()
            converter = TextConverter(resource_manager, output_string, codec=codec, laparams=laparams)
            interpreter = PDFPageInterpreter(resource_manager, converter)

            # Reset file pointer to the beginning
            pdf_file.seek(0)

            for page in PDFPage.get_pages(pdf_file, check_extractable=True):
                interpreter.process_page(page)

            text = output_string.getvalue()

            # Clean up
            converter.close()
            output_string.close()

            # Reset file pointer to the beginning for potential reuse
            pdf_file.seek(0)

            return text

        except Exception as e:
            _logger.error(f"Error converting PDF to text: {str(e)}")
            return ""
