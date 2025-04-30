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
from io import StringIO

_logger = logging.getLogger(__name__)

try:
    from pdf2image import convert_from_bytes
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

class InvoiceParser(models.Model):
    _inherit = 'helpdesk.ticket'

    x_invoice_id = fields.Many2one('account.move', string='Factura')
    x_po_number = fields.Char(string='Número OC')
    x_cuit = fields.Char(string='CUIT')
    x_total_amount = fields.Float(string='Monto Total')
    x_iva_amount = fields.Float(string='Monto IVA')

    def procesar_facturas(self):
        """
        Procesa facturas para tickets con estado 'Facturas nuevas'
        Este método trabaja en conjuntos de registros y se llama directamente desde el registro
        :return: Booleano indicando éxito
        """
        if not self:
            # Obtener la etapa 'Facturas Nuevas'
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
        Procesa un conjunto de tickets
        :param tickets: conjunto de registros helpdesk.ticket
        :return: Booleano indicando éxito
        """
        if not tickets:
            _logger.info("No hay tickets para procesar")
            return False

        _logger.info(f"Procesando {len(tickets)} tickets")

        # Obtener IDs de etapas para cambios de estado - usar los XML IDs de nuestro módulo
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

        invoice_linked = self.env.ref('bmi_invoice_parser.stage_fact_vinculada', raise_if_not_found=False)
        if not invoice_linked:
            invoice_linked = self.env['helpdesk.stage'].search([
                ('name', '=', 'Facturas Vinculadas')
            ], limit=1)
            if not invoice_linked:
                invoice_linked = self.env['helpdesk.stage'].create({
                    'name': 'Facturas Vinculadas',
                    'sequence': 5,
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
            # ticket.message_post(body="Iniciando procesamiento automático del ticket.")

            has_pdf = False
            pdf_attachments = []

            # Verificar PDFs en los mensajes del chatter
            for message in ticket.message_ids:
                message_attachments = self.env['ir.attachment'].search([
                    ('res_model', '=', 'mail.message'),
                    ('res_id', '=', message.id),
                    ('mimetype', '=', 'application/pdf')
                ])

                if message_attachments:
                    has_pdf = True
                    pdf_attachments.extend(message_attachments)

            # También verificar PDFs adjuntos directamente al ticket
            ticket_attachments = self.env['ir.attachment'].search([
                ('res_model', '=', 'helpdesk.ticket'),
                ('res_id', '=', ticket.id),
                ('mimetype', '=', 'application/pdf')
            ])

            if ticket_attachments:
                has_pdf = True
                pdf_attachments.extend(ticket_attachments)

            if not has_pdf:
                # Si no se encuentran adjuntos PDF, cambiar el estado a 'Tickets sin PDF'
                ticket.write({
                    'stage_id': sin_pdf_stage.id
                })
                # Registrar el cambio en el chatter
                ticket.message_post(
                    body="Ticket movido a 'Tickets sin PDF' - No se encontraron adjuntos PDF en los mensajes"
                )
            else:
                # ticket.message_post(body=f"Se encontraron {len(pdf_attachments)} archivos PDF adjuntos para procesar")
                # Procesar cada adjunto PDF
                po_found = False
                po_inexistente = False
                invoice_created = False

                for attachment in pdf_attachments:
                    result, is_po_inexistente, invoice_created  = self.process_invoice_pdf(ticket, attachment,
                                                                                           sin_po_stage,
                                                                                           po_inexistente_stage)
                    if invoice_created:
                        po_found = True
                        ticket.write({
                            'stage_id': invoice_linked.id
                        })
                        break
                    elif result:
                        po_found = True
                        break
                    elif is_po_inexistente:
                        po_inexistente = True
                        break

                # Si no se encontró PO y no se marcó como PO# inexistente, mover a 'PDF sin PO#'
                if not po_found and not po_inexistente and not invoice_created:
                    ticket.write({
                        'stage_id': sin_po_stage.id
                    })
                    ticket.message_post(
                        body="Ticket movido a 'PDF sin PO#' - No se encontró PO válida en ningún PDF"
                    )

        return True

    def process_invoice_pdf(self, ticket, attachment, sin_po_stage, po_inexistente_stage):
        """
        Procesa un adjunto PDF de factura
        :param ticket: registro helpdesk.ticket
        :param attachment: registro ir.attachment
        :param sin_po_stage: registro helpdesk.stage para 'PDF sin PO#'
        :param po_inexistente_stage: registro helpdesk.stage para 'PO# Inexistente'
        :return: Tupla (Booleano indicando éxito, Booleano indicando si PO# inexistente)
        """
        _logger.info(f"Iniciando procesamiento de PDF: {attachment.name}")
        ticket.message_post(
            body=f"Iniciando procesamiento del PDF: {attachment.name}"
        )

        try:
            # Obtener contenido del PDF
            pdf_content = base64.b64decode(attachment.datas)
            pdf_file = BytesIO(pdf_content)

            # Extraer texto del PDF
            text_content = self.convert_pdf_to_text(pdf_file)

            # Registrar un fragmento del texto extraído para diagnóstico
            text_sample = text_content[:500] + ('...' if len(text_content) > 500 else '')
            _logger.info(f"Muestra del texto extraído del PDF: {text_sample}")

            # Primero, verificar si hay un patrón de "Pedido de compra" específico
            pedido_pattern = r'pedido de compra[^\n]*?#P([0-9]{4,})'
            pedido_match = re.search(pedido_pattern, text_content, re.IGNORECASE)
            if pedido_match:
                pedido_po = pedido_match.group(1).strip()
                p_number = f"#P{pedido_po}"
                _logger.info(f"Encontrada referencia especial de 'Pedido de compra': {p_number}")

                # Registrar en el chatter el número encontrado
                ticket.message_post(
                    body=f"Número de PO encontrado en el PDF: {p_number}"
                )

                # Check if this specific PO exists
                pedido_purchase_order = self.env['purchase.order'].search([
                    '|', '|',
                    ('name', '=ilike', f"P{pedido_po}"),
                    ('name', '=ilike', f"#P{pedido_po}"),
                    ('name', '=ilike', pedido_po)
                ], limit=1)

                if pedido_purchase_order:
                    oc_message = f"PO coincidente encontrada para 'Pedido de compra': {pedido_purchase_order.name}"
                    _logger.info(oc_message)
                    # Añadir al chatter
                    ticket.message_post(body=oc_message)

                    purchase_order = pedido_purchase_order
                    po_number = pedido_po
                    original_po = f"#P{pedido_po}"

                    # Extract invoice data and create invoice
                    invoice_data = self.extract_invoice_data(text_content, po_number)
                    invoice = self.create_draft_invoice(ticket, invoice_data, purchase_order, attachment)
                    return (True if invoice else False, False, True if invoice else False)
                else:
                    # Mover al estado "PO# Inexistente" si se encuentra pero no existe
                    ticket.write({
                        'stage_id': po_inexistente_stage.id,
                        'x_po_number': f"P{pedido_po}"  # Guardar el número de PO aunque no exista
                    })
                    ticket.message_post(
                        body=f"Ticket movido a 'PO# Inexistente' - Se encontró el número de PO ({p_number}) "
                             f"en el PDF pero no existe en el sistema."
                    )
                    return (False, True, False)

            # Extraer número de PO usando el método principal
            try:
                result = self.extract_po_number(text_content)
                # Asegurarse de que result sea una tupla con el formato esperado
                if isinstance(result, tuple) and len(result) >= 2:
                    po_number = result[0]
                    all_found_pos = result[1]
                else:
                    po_number = result
                    all_found_pos = [po_number] if po_number else []
            except Exception as e:
                error_msg = f"Error al extraer número de PO: {str(e)}"
                _logger.error(error_msg)
                ticket.message_post(body=error_msg)
                po_number = False
                all_found_pos = []

            # Registrar todos los posibles números de PO encontrados en el chatter
            if all_found_pos:
                po_list_text = ", ".join([str(p) for p in all_found_pos if p])
                # if po_list_text:
                #    ticket.message_post(body=f"Posibles números de PO encontrados en el PDF: {po_list_text}")

            # VALIDACIÓN ADICIONAL: Verificar que el número de PO tiene un formato válido
            # Una PO válida debe tener al menos 4 caracteres y contener un número de al menos 4 dígitos
            if po_number:
                # Extraer solo dígitos del número de PO
                po_digits = ''.join(filter(str.isdigit, str(po_number)))
                if len(po_digits) < 4 or len(str(po_number)) < 4:
                    _logger.info(f"Número de PO descartado por ser demasiado corto: {po_number} (dígitos: {po_digits})")
                    po_number = False

            # Si no se encontró un número de OC, actualizar mensaje y devolver false
            if not po_number:
                # Mover el ticket a "PDF sin PO#"
                ticket.write({
                    'stage_id': sin_po_stage.id
                })
                ticket.message_post(
                    body=f"No se encontró número de PO válido en el PDF: {attachment.name}<br/>"
                         f"El ticket ha sido movido a 'PDF sin PO#'.<br/>"
                         f"Por favor, verifique si esta factura contiene una referencia de orden de compra."
                )
                return (False, False, False)

            # Guardar el formato original antes de limpiarlo
            original_po = po_number
            _logger.info(f"Número de PO extraído (formato original): {original_po}")
            # ticket.message_post( body=f"Número de PO extraído (formato original): {original_po}" )

            # Limpiar prefijos si es necesario
            if original_po.startswith('#'):
                po_number = original_po[1:]
            else:
                po_number = original_po

            # Para búsqueda, necesitamos el número sin el prefijo P en algunos casos
            search_number = po_number
            if po_number.upper().startswith('P') and po_number[1:].isdigit():
                search_number = po_number[1:]

            # Limpiar posibles caracteres no alfanuméricos
            search_number = re.sub(r'^[^A-Z0-9]+', '', search_number, flags=re.IGNORECASE)

            if search_number.isdigit():
                search_number = search_number.zfill(5)

            _logger.info(f"Número de PO para búsqueda: {search_number}")

            # Crear versiones del número de PO para buscar
            search_variants = [
                po_number,
                search_number,
                'P' + search_number if not search_number.startswith('P') else search_number,
                '#P' + search_number if not search_number.startswith('#P') else search_number,
                'PO' + search_number if not search_number.startswith('PO') else search_number,
                '#PO' + search_number if not search_number.startswith('#PO') else search_number,
            ]

            # Eliminar duplicados y cadenas vacías
            search_variants = [v for v in set(search_variants) if v]
            _logger.info(f"Variantes de búsqueda: {search_variants}")

            # Construir dominio para la búsqueda
            domain = []
            for variant in search_variants:
                domain.append(('name', '=ilike', variant))

            if len(domain) > 1:
                domain = ['|'] * (len(domain) - 1) + domain

            # Verificar que la PO existe en el sistema
            purchase_order = self.env['purchase.order'].search(domain, limit=1)

            if purchase_order:
                oc_message = f"PO coincidente encontrada: {purchase_order.name}"
                _logger.info(oc_message)
                # Añadir al chatter
                ticket.message_post(body=oc_message)
            else:
                _logger.warning(f"No se encontró PO coincidente para las variantes: {search_variants}")

                # Intentar una búsqueda más permisiva
                number_only = re.sub(r'[^0-9]', '', search_number)
                if number_only and len(number_only) >= 4:
                    search_msg = f"Intentando búsqueda solo por números con: {number_only}"
                    _logger.info(search_msg)

                    # Buscar OCs que contengan esta secuencia de números
                    purchase_order = self.env['purchase.order'].search([
                        ('name', 'ilike', number_only)
                    ], limit=1)

                    if purchase_order:
                        oc_message = f"PO coincidente encontrada con búsqueda solo por números: {purchase_order.name}"
                        _logger.info(oc_message)
                        ticket.message_post(body=oc_message)

            if not purchase_order:
                # Intentar con una búsqueda más extendida
                extended_domain = []

                # Añadir variantes con y sin prefijos
                if search_number.isdigit():
                    extended_domain.append(('name', 'ilike', search_number))
                    if len(search_number) >= 4:
                        extended_domain.append(('name', 'ilike', f"P{search_number}"))
                        extended_domain.append(('name', 'ilike', f"PO{search_number}"))

                if len(extended_domain) > 1:
                    extended_domain = ['|'] * (len(extended_domain) - 1) + extended_domain

                ext_search_msg = f"Realizando búsqueda extendida con variantes adicionales"
                _logger.info(ext_search_msg)
                ticket.message_post(body=ext_search_msg)

                purchase_order = self.env['purchase.order'].search(extended_domain, limit=1)

                if purchase_order:
                    oc_message = f"PO coincidente encontrada con búsqueda extendida: {purchase_order.name}"
                    _logger.info(oc_message)
                    ticket.message_post(body=oc_message)

            if not purchase_order:
                # Cambiado: Mover el ticket al estado "PO# Inexistente" en lugar de solo enviar un mensaje
                ticket.write({
                    'stage_id': po_inexistente_stage.id,
                    'x_po_number': original_po  # Guardar el número de PO aunque no exista
                })
                ticket.message_post(
                    body=f"Ticket movido a 'PO# Inexistente'<br/>"
                         f"Se extrajo número de PO ({po_number}) del PDF, pero no existe en el sistema.<br/>"
                         f"Formato original: {original_po}<br/>"
                         f"Se buscaron las variaciones: {', '.join(search_variants)}<br/>"
                         f"También se intentó con búsqueda extendida y búsqueda por números."
                )
                return (False, True, False)

            # Si llegamos aquí, hemos encontrado una PO válida
            # Extraer datos restantes de la factura
            invoice_data = self.extract_invoice_data(text_content, po_number)

            # Crear factura en borrador
            invoice = self.create_draft_invoice(ticket, invoice_data, purchase_order, attachment)

            # Si la factura se creó exitosamente
            if invoice:
                return (True, False, True)
            else:
                # Si la factura no se pudo crear pero la PO existe
                # NO debemos mover el ticket a "PDF sin PO#" porque sí encontramos la OC
                ticket.message_post(
                    body=f"Se encontró la PO {purchase_order.name} "
                         f"pero no se pudo crear la factura. Por favor, revise los mensajes "
                         f"anteriores para más detalles."
                )
                return (False, False, False)

        except Exception as e:
            error_msg = f"Error al procesar el PDF adjunto {attachment.name}: {str(e)}"
            _logger.error(error_msg)
            ticket.message_post(body=error_msg)
            return (False, False, False)

    def extract_po_number(self, text_content):
        """
        Extraer número de PO del contenido de texto usando múltiples patrones
        :param text_content: Texto extraído del PDF
        :return: Tupla (número de PO extraído o False, lista de todos los números encontrados)
        """
        # Lista de patrones principales para números de OC
        primary_patterns = [
            # Patrones directos para números de PO - estos tienen prioridad
            r'(?<!\w)P([0-9]{4,})(?!\w)',  # P seguido de números (P03324)
            r'(?<!\w)PO([0-9]{4,})(?!\w)',  # PO seguido de números (PO03324)
            r'(?<!\w)OC([0-9]{4,})(?!\w)',  # PO seguido de números (OC03324)
            r'(?<!\w)#P([0-9]{4,})(?!\w)',  # #P seguido de números (#P03324)
            r'(?<!\w)#PO([0-9]{4,})(?!\w)',  # #PO seguido de números (#PO03324)
        ]

        # Patrones secundarios (se utilizan si los primarios no encuentran nada)
        secondary_patterns = [
            # Patrones con palabras clave que podrían ayudar a identificar OCs
            r'(?:CORRESPONDE)[:\s]*(?:P|#P)?([0-9]{4,})',
            r'(?:CORRESPONDE)[:\s]*([A-Z][0-9]{4,})',

            # Patrones en inglés - asegurando que incluyan números
            r'(?:P\.O\.|PO|Purchase Order)[:\s#]*([A-Z0-9]*[0-9]+[A-Z0-9]*)',
            r'(?:P|#P|#PO)[:\s#]*([0-9]{4,})',

            # Patrones en español (OC = Orden de Compra)
            r'(?:OC|OC#|OCN|OCN#)[:\s#]*([A-Z0-9]*[0-9]+[A-Z0-9]*)',
            r'(?:O\.C\.|O\.C\.#)[:\s#]*([A-Z0-9]*[0-9]+[A-Z0-9]*)',

            # Palabras clave adicionales que podrían preceder a un número de OC
            r'(?:REFERENCIA|REF|REF\.|REFERENCIA:|REF:|REF\.:|NRO\.?)[:\s#]*([A-Z0-9]*[0-9]+[A-Z0-9]*)',

            # Buscar patrones con números cerca de palabras clave
            r'(?:orden de compra|orden|purchase|compra|pedido)[^\n]*?([A-Z]*[0-9]{4,}[A-Z0-9-]*)',

            # Patrones específicos con "Pedido de compra"
            r'pedido de compra[:\s#]*([A-Z]*[0-9]{4,}[A-Z0-9-]*)',
            r'pedido de compra[^\n]*?#P([0-9]{4,})',
            r'pedido de compra[^\n]*?#([0-9]{4,})',
            r'pedido\s*de\s*compra[^\n]*?#P([0-9]{4,})',

        ]

        # Lista de palabras comunes que no deben ser interpretadas como números de OC
        palabras_descartadas = [
            'RESPONSABLE', 'INSCRIPTO', 'FACTURA', 'ORIGINAL', 'TRIPLICADO',
            'IRAM', 'CUIT', 'INGRESOS', 'BRUTOS', 'ACTIVIDADES',
            'COPIA', 'DUPLICADO', 'FECHA', 'VENCIMIENTO'
        ]

        # Lista de números cortos que no deben interpretarse como números de OC
        numeros_cortos_descartados = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12',
                                      '01', '02', '03', '04', '05', '06', '07', '08', '09']

        # Guardar todos los posibles números P para registrar en el chatter
        all_po_numbers = []

        # Primera pasada: buscar patrones directos en todo el texto
        _logger.info(f"Buscando patrones directos de PO en el texto")
        for pattern in primary_patterns:
            matches = re.finditer(pattern, text_content, re.IGNORECASE)
            for match in matches:
                # Para patrones primarios, capturamos la coincidencia completa si comienza con P, o le añadimos P
                if match.group(0).upper().startswith(('P', '#P')):
                    po_number = match.group(0).strip()
                else:
                    # Si el patrón capturó solo números (grupo 1), añadimos el prefijo P
                    po_number = 'P' + match.group(1).strip()

                # Validación adicional: filtrar números cortos o palabras descartadas
                if po_number.upper() in palabras_descartadas:
                    continue

                # Filtrar números simples o muy cortos
                if po_number in numeros_cortos_descartados:
                    continue

                # Verificar que el número encontrado tiene al menos 4 dígitos
                if len(''.join(filter(str.isdigit, po_number))) < 4:
                    continue

                # Añadir a la lista de números encontrados
                if po_number not in all_po_numbers:
                    all_po_numbers.append(po_number)

                _logger.info(f"Encontrada coincidencia directa de PO: {po_number}")
                return po_number, all_po_numbers

        # Segunda pasada: buscar patrones secundarios
        _logger.info(f"Buscando patrones secundarios de PO en el texto")
        for pattern in secondary_patterns:
            matches = re.finditer(pattern, text_content, re.IGNORECASE)
            for match in matches:
                po_number = match.group(1).strip()

                # Validación adicional: verificar que el número de PO contiene dígitos y es lo suficientemente largo
                if not any(char.isdigit() for char in po_number):
                    _logger.info(f"Descartando coincidencia sin dígitos: {po_number}")
                    continue

                # Filtrar números simples o muy cortos
                if po_number in numeros_cortos_descartados:
                    _logger.info(f"Descartando número simple: {po_number}")
                    continue

                # Verificar que el número encontrado tiene al menos 4 dígitos
                if len(''.join(filter(str.isdigit, po_number))) < 4:
                    _logger.info(f"Descartando número con menos de 4 dígitos: {po_number}")
                    continue

                # Verificar que no es una palabra común que podría confundirse
                if po_number.upper() in palabras_descartadas:
                    _logger.info(f"Descartando palabra común mal interpretada como OC: {po_number}")
                    continue

                _logger.info(f"Encontrada coincidencia secundaria de OC: {po_number}")

                # Si es solo un número, agregar prefijo 'P' para búsqueda estándar
                if po_number.isdigit() and len(po_number) >= 4:
                    po_number = 'P' + po_number
                    _logger.info(f"Añadido prefijo 'P' al número de PO numérico: {po_number}")

                # Añadir a la lista de números encontrados
                if po_number not in all_po_numbers:
                    all_po_numbers.append(po_number)

                return po_number, all_po_numbers

        # Última pasada: buscar cualquier combinación P + números que parezca ser una OC
        _logger.info(f"Realizando búsqueda final de P + números en el texto")
        generic_po_pattern = r'P[0-9]{4,}'
        generic_matches = re.finditer(generic_po_pattern, text_content, re.IGNORECASE)
        for generic_match in generic_matches:
            po_candidate = generic_match.group(0)

            # Verificar que no está dentro de un contexto que sugiera que no es una OC
            start_pos = max(0, generic_match.start() - 20)
            end_pos = min(len(text_content), generic_match.end() + 20)
            context = text_content[start_pos:end_pos].upper()

            if not any(bad_word in context for bad_word in ['CODIGO', 'PRODUCTO', 'ITEM']):
                _logger.info(f"Encontrada posible PO genérica: {po_candidate}")

                # Añadir a la lista de números encontrados
                if po_candidate not in all_po_numbers:
                    all_po_numbers.append(po_candidate)

                return po_candidate, all_po_numbers

        _logger.info("No se encontró ningún número de PO válido en el texto")
        return False, all_po_numbers

    def extract_invoice_data(self, text_content, po_number):
        """
        Extraer datos de factura del contenido de texto del PDF
        :param text_content: Texto extraído del PDF
        :param po_number: Número de PO extraído del PDF
        :return: Diccionario con datos de la factura
        """
        # Buscar CUIT (ID fiscal argentino)
        cuit_pattern = r'(?:CUIT|cuit)[:\s]*(\d{2}-\d{8}-\d{1})'
        cuit_match = re.search(cuit_pattern, text_content)
        cuit = cuit_match.group(1) if cuit_match else ''

        # Buscar número de factura
        invoice_number_pattern = r'(?:FACTURA|FACTURA\s+[ABC]|FACTURA\s+ELECTRONICA)[^0-9]*([0-9]{4,5}-[0-9]{8})'
        invoice_number_match = re.search(invoice_number_pattern, text_content, re.IGNORECASE)
        invoice_number = invoice_number_match.group(1) if invoice_number_match else ''

        # Buscar fecha de factura (formatos comunes en Argentina)
        # date_patterns = [
        #     r'(?:FECHA|DATE)[^0-9]*(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})',
        #     r'(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})'
        # ]
        date_patterns = [
            r'(?:FECHA|DATE)[^\d]*(\d{2}[/-]\d{2}[/-]\d{4})',  # e.g., FECHA 15/03/2025
            r'\b(\d{2}[/-]\d{2}[/-]\d{4})\b'  # standalone date
        ]

        invoice_date = ''
        for date_pattern in date_patterns:
            date_match = re.search(date_pattern, text_content, re.IGNORECASE)
            if date_match:
                invoice_date = date_match.group(1)
                break

        # Buscar tipo de documento
        document_type = ''
        if 'FACTURA A' in text_content.upper():
            document_type = 'FACTURA A'
        elif 'FACTURA B' in text_content.upper():
            document_type = 'FACTURA B'
        elif 'FACTURA C' in text_content.upper():
            document_type = 'FACTURA C'
        elif 'NOTA DE DEBITO A' in text_content.upper() or 'NOTA DE DÉBITO A' in text_content.upper():
            document_type = 'NOTA DE DEBITO A'
        elif 'NOTA DE DEBITO B' in text_content.upper() or 'NOTA DE DÉBITO B' in text_content.upper():
            document_type = 'NOTA DE DEBITO B'
        elif 'NOTA DE DEBITO C' in text_content.upper() or 'NOTA DE DÉBITO C' in text_content.upper():
            document_type = 'NOTA DE DEBITO C'

        # Buscar monto total
        total_pattern = r'(?:Total|TOTAL)[:\s]*\$?\s*([\d.,]+)'
        total_match = re.search(total_pattern, text_content)
        total_amount = 0.0

        if total_match:
            # Limpiar el valor para convertirlo a float correctamente
            total_str = total_match.group(1).replace('.', '').replace(',', '.')
            try:
                total_amount = float(total_str)
            except ValueError:
                # Si falla la conversión, intentar otra limpieza
                total_str = ''.join(char for char in total_str if char.isdigit() or char == '.')
                if total_str:
                    total_amount = float(total_str)

        # Buscar monto de IVA
        iva_pattern = r'(?:IVA|iva|I\.V\.A\.)[:\s]*\$?\s*([\d.,]+)'
        iva_match = re.search(iva_pattern, text_content)
        iva_amount = 0.0

        if iva_match:
            # Limpiar el valor para convertirlo a float correctamente
            iva_str = iva_match.group(1).replace('.', '').replace(',', '.')
            try:
                iva_amount = float(iva_str)
            except ValueError:
                # Si falla la conversión, intentar otra limpieza
                iva_str = ''.join(char for char in iva_str if char.isdigit() or char == '.')
                if iva_str:
                    iva_amount = float(iva_str)

        # Si no se encuentra el monto de IVA, estimarlo como el 21% del monto base
        if iva_amount == 0.0 and total_amount > 0:
            base_amount = total_amount / 1.21  # Asumiendo IVA del 21%
            iva_amount = total_amount - base_amount
        else:
            base_amount = total_amount - iva_amount

        # Almacenar la información extraída
        invoice_data = {
            'po_number': po_number,
            'cuit': cuit,
            'invoice_number': invoice_number,
            'invoice_date': invoice_date,
            'document_type': document_type,
            'total_amount': total_amount,
            'iva_amount': iva_amount,
            'base_amount': base_amount
        }

        return invoice_data

    def create_draft_invoice(self, ticket, invoice_data, purchase_order, attachment):
        """
        Crear factura en borrador basada en los datos extraídos
        :param ticket: registro helpdesk.ticket
        :param invoice_data: Diccionario con datos de la factura
        :param purchase_order: registro purchase.order
        :param attachment: registro ir.attachment
        :return: registro account.move o False
        """
        try:
            # Verificar si la factura ya existe
            existing_invoice = False

            # Buscar por OC
            if invoice_data.get('po_number'):

                purchase_order_id = f"PO id {purchase_order.id}"
                _logger.info(purchase_order_id)

                # Get PO name without spaces
                po_name_clean = purchase_order.name.replace(" ", "")

                # Search broadly, then filter manually
                possible_invoices = self.env['account.move'].search([
                    ('move_type', '=', 'in_invoice'),
                    ('state', '!=', 'cancel'),
                ])

                # Filter by cleaned ref
                existing_po_invoices = possible_invoices.filtered(
                    lambda inv: (inv.ref or '').replace(" ", "") == po_name_clean
                )

                if existing_po_invoices:
                    existing_invoice = existing_po_invoices[0]
                    # Verificar si el proveedor coincide con el de la orden de compra
                    is_same_partner = existing_invoice.partner_id.id == purchase_order.partner_id.id
                    partner_warning = "" if is_same_partner else (f"\n⚠️ ATENCIÓN: El proveedor de la factura existente"
                                                                  f" ({existing_invoice.partner_id.name}) no coincide "
                                                                  f"con el de la PO ({purchase_order.partner_id.name})")

                    ticket.message_post(
                        body=f"Se encontró una factura existente para la PO {invoice_data['po_number']}: "
                             f"{existing_invoice.name}{partner_warning}"
                    )

            # Buscar por CUIT y monto total (criterio adicional)
            if not existing_invoice and invoice_data.get('cuit') and invoice_data.get('total_amount'):
                existing_amount_invoices = self.env['account.move'].search([
                    ('move_type', '=', 'in_invoice'),
                    ('partner_id.vat', '=', invoice_data['cuit']),
                    ('amount_total', '=', float(invoice_data['total_amount'])),
                    ('state', '!=', 'cancel')
                ])

                if existing_amount_invoices:
                    existing_invoice = existing_amount_invoices[0]
                    # Verificar si el proveedor coincide con el de la orden de compra
                    is_same_partner = existing_invoice.partner_id.id == purchase_order.partner_id.id
                    partner_warning = "" if is_same_partner else (f"\n⚠️ ATENCIÓN: El proveedor de la factura existente"
                                                                  f" ({existing_invoice.partner_id.name}) no coincide "
                                                                  f"con el de la PO ({purchase_order.partner_id.name})")

                    ticket.message_post(
                        body=f"Se encontró una factura existente del proveedor con CUIT {invoice_data['cuit']} y "
                             f"monto {invoice_data['total_amount']}: {existing_invoice.name}{partner_warning}"
                    )

            # Si se encontró una factura existente, mover el ticket a "Facturas Duplicadas" y detener
            if existing_invoice:
                # Buscar la etapa "Facturas Duplicadas"
                stage_duplicated = self.env['helpdesk.stage'].search([('name', '=', 'Facturas Duplicadas')], limit=1)

                if stage_duplicated:
                    # Guardar la información de la PO original para la referencia
                    po_partner_name = purchase_order.partner_id.name

                    ticket.write({
                        'stage_id': stage_duplicated.id,
                        'x_invoice_id': existing_invoice.id  # Vincular la factura existente al ticket
                    })

                    # Verificar coincidencia de proveedores
                    is_same_partner = existing_invoice.partner_id.id == purchase_order.partner_id.id
                    partner_warning = "" if is_same_partner else (f"\n⚠️ ATENCIÓN: El proveedor de la factura existente"
                                                                  f" ({existing_invoice.partner_id.name}) NO COINCIDE "
                                                                  f"con el proveedor de la PO ({po_partner_name})")

                    # Verificar si la PO de la factura coincide con la actual
                    po_warning = ""
                    if existing_invoice.purchase_id and existing_invoice.purchase_id.id != purchase_order.id:
                        po_warning = (f"\n⚠️ ATENCIÓN: La factura existente está vinculada a otra PO: "
                                      f"{existing_invoice.purchase_id.name}")

                    ticket.message_post(
                        body=f"""
                        ⚠️ FACTURA DUPLICADA DETECTADA ⚠️
                        No se creó una nueva factura porque ya existe:
                        - Número de factura: {existing_invoice.name}
                        - Proveedor de la factura: {existing_invoice.partner_id.name}
                        - Proveedor de la PO actual: {po_partner_name}
                        - PO actual: {invoice_data['po_number']}
                        - Monto Total: ${float(invoice_data['total_amount']):,.2f}{partner_warning}{po_warning}

                        El ticket ha sido movido a la etapa "Facturas Duplicadas".
                        """
                    )

                    return existing_invoice
                else:
                    ticket.message_post(
                        body="No se encontró la etapa 'Facturas Duplicadas'. Por favor, cree esta etapa en el sistema."
                    )

            # Encontrar cuenta apropiada
            cuenta_contable = self.env['account.account'].search([('code', '=', '511100000')], limit=1)
            if not cuenta_contable:
                # Usar una cuenta alternativa para gastos
                cuenta_contable = self.env['account.account'].search([
                    ('account_type', '=', 'expense')
                ], limit=1)

            # Obtener socio de la orden de compra
            partner = purchase_order.partner_id

            # Si el CUIT está disponible, verificar socio
            if invoice_data.get('cuit'):
                cuit_partner = self.env['res.partner'].search([
                    ('vat', '=', invoice_data['cuit'])
                ], limit=1)

                if cuit_partner and cuit_partner.id != partner.id:
                    ticket.message_post(
                        body=f"Advertencia: El CUIT en la factura ({invoice_data['cuit']}) pertenece a {cuit_partner.name}, pero la PO {invoice_data['po_number']} es para {partner.name}"
                    )

            # Obtener impuesto IVA
            iva_tax = self.env.ref('l10n_ar.1_ri_tax_vat_21_purchases', raise_if_not_found=False)
            if not iva_tax:
                # Alternativa genérica para impuesto IVA
                iva_tax = self.env['account.tax'].search([
                    ('type_tax_use', '=', 'purchase'),
                    ('amount', '=', 21)
                ], limit=1)

            # La distribución analítica es obligatoria, debemos obtenerla
            analytic_distribution = {}
            proyecto_analytic_account = False

            # 1. Primero intentamos obtener del proyecto si existe
            if hasattr(purchase_order, 'proyecto_id') and purchase_order.proyecto_id:
                # Buscar el proyecto en proyectos.bmi
                proyecto = self.env['proyectos.bmi'].browse(purchase_order.proyecto_id.id)
                if proyecto:
                    # Registrar en el chatter la vinculación al proyecto
                    cliente_nombre = proyecto.partner_id.name if hasattr(proyecto,
                                                                         'partner_id') and proyecto.partner_id else \
                        "Cliente desconocido"
                    proyecto_nombre = proyecto.proyecto if hasattr(proyecto, 'proyecto') else "Proyecto desconocido"

                    proyecto_msg = f"PO vinculada al Proyecto {cliente_nombre}/{proyecto_nombre}"
                    _logger.info(proyecto_msg)
                    ticket.message_post(body=proyecto_msg)

                    # Obtener la cuenta analítica del proyecto
                    if hasattr(proyecto, 'cta_analitica') and proyecto.cta_analitica:
                        proyecto_analytic_account = proyecto.cta_analitica
                        _logger.info(f"Cuenta analítica obtenida del proyecto: {proyecto_analytic_account.name}")
                        ticket.message_post(
                            body=f"Cuenta analítica obtenida del proyecto: {proyecto_analytic_account.name}")

                        # Asignar distribución analítica del proyecto
                        analytic_distribution = {str(proyecto_analytic_account.id): 100}

                        # Actualizar la orden de compra con esta distribución analítica
                        for line in purchase_order.order_line:
                            if not line.analytic_distribution:
                                line.write({'analytic_distribution': analytic_distribution})

                        log_msg = f"Se actualizó la distribución analítica en la OC: {analytic_distribution}"
                        _logger.info(log_msg)
                        ticket.message_post(body=log_msg)

            # 2. Si no hay cuenta analítica del proyecto, verificar si ya existe en las líneas de OC
            if not analytic_distribution and purchase_order.order_line:
                for line in purchase_order.order_line:
                    if line.analytic_distribution:
                        analytic_distribution = line.analytic_distribution
                        log_msg = f"Usando distribución analítica de la línea de OC: {analytic_distribution}"
                        _logger.info(log_msg)
                        ticket.message_post(body=log_msg)
                        break

            # 3. Si aún no tenemos distribución analítica, crear una con la primera cuenta disponible
            if not analytic_distribution:
                default_analytic = self.env['account.analytic.account'].search([], limit=1)
                if default_analytic:
                    analytic_distribution = {str(default_analytic.id): 100}

                    # Actualizar la orden de compra con esta distribución analítica
                    for line in purchase_order.order_line:
                        if not line.analytic_distribution:
                            line.write({'analytic_distribution': analytic_distribution})

                    log_msg = f"Se asignó la cuenta analítica predeterminada a la OC: {default_analytic.name}"
                    _logger.info(log_msg)
                    ticket.message_post(body=log_msg)
                else:
                    error_msg = "Error: No se encontró ninguna cuenta analítica y es obligatoria."
                    _logger.error(error_msg)
                    ticket.message_post(body=error_msg)
                    return False

            # Crear línea de factura con distribución analítica (siempre es obligatoria)
            invoice_line_vals = {
                'product_id': purchase_order.order_line[0].product_id.id if purchase_order.order_line else False,
                'name': f"Factura de {attachment.name}",
                'quantity': 1,
                'price_unit': invoice_data['base_amount'],
                'tax_ids': [(6, 0, [iva_tax.id])] if iva_tax else [],
                'account_id': cuenta_contable.id,
                'analytic_distribution': analytic_distribution,  # Siempre agregamos la distribución analítica
            }

            # Obtener el tipo de documento (Factura A, B, C o Nota de Débito A, B, C)
            document_type = None
            if invoice_data.get('document_type'):
                # Buscar el tipo de documento según lo extraído del PDF
                document_type_name = invoice_data.get('document_type', '').upper()
                search_terms = []

                # Determinar los términos de búsqueda apropiados
                if 'FACTURA A' in document_type_name:
                    search_terms = ['A', 'FACTURA A']
                elif 'FACTURA B' in document_type_name:
                    search_terms = ['B', 'FACTURA B']
                elif 'FACTURA C' in document_type_name:
                    search_terms = ['C', 'FACTURA C']
                elif 'NOTA DE DEBITO A' in document_type_name or 'NOTA DE DÉBITO A' in document_type_name:
                    search_terms = ['A', 'NOTA DE DEBITO A', 'NOTA DE DÉBITO A']
                elif 'NOTA DE DEBITO B' in document_type_name or 'NOTA DE DÉBITO B' in document_type_name:
                    search_terms = ['B', 'NOTA DE DEBITO B', 'NOTA DE DÉBITO B']
                elif 'NOTA DE DEBITO C' in document_type_name or 'NOTA DE DÉBITO C' in document_type_name:
                    search_terms = ['C', 'NOTA DE DEBITO C', 'NOTA DE DÉBITO C']

                # Buscar el tipo de documento
                if search_terms:
                    document_type = self.env['l10n_latam.document.type'].search([
                        '|', '|',
                        ('name', 'ilike', search_terms[0]),
                        ('code', 'ilike', search_terms[0]),
                        ('doc_code_prefix', 'ilike', search_terms[0]),
                    ], limit=1)

                    if document_type:
                        ticket.message_post(body=f"Tipo de documento identificado: {document_type.name}")
                    else:
                        ticket.message_post(
                            body=f"No se pudo encontrar el tipo de documento para: {document_type_name}")

            # Determinar fecha de factura
            invoice_date = fields.Date.today()
            if invoice_data.get('invoice_date'):
                try:
                    # Procesar fecha desde formato string a date
                    invoice_date = fields.Date.from_string(invoice_data['invoice_date'])
                    ticket.message_post(body=f"Fecha de factura extraída del PDF: {invoice_date}")
                except Exception as e:
                    ticket.message_post(
                        body=f"Error al procesar la fecha de factura: {str(e)}. Se usará la fecha actual.")

            # Procesar número de documento del formato 99999-99999999
            l10n_latam_document_number = False
            if invoice_data.get('invoice_number'):
                # Asegurarse de que tenga el formato correcto
                invoice_number = invoice_data['invoice_number']
                # Limpiar el número para asegurar formato adecuado
                if '-' in invoice_number and len(invoice_number.split('-')) == 2:
                    l10n_latam_document_number = invoice_number
                else:
                    # Intentar formatear si no tiene el formato esperado
                    cleaned_number = ''.join(filter(str.isdigit, invoice_number))
                    if len(cleaned_number) >= 13:  # Mínimo 4 dígitos punto venta + 8 número
                        punto_venta = cleaned_number[:5].zfill(5)
                        numero = cleaned_number[5:].zfill(8)
                        l10n_latam_document_number = f"{punto_venta}-{numero}"
                        ticket.message_post(body=f"Número de factura formateado: {l10n_latam_document_number}")

            # Crear valores de factura
            invoice_vals = {
                'move_type': 'in_invoice',
                'partner_id': partner.id,
                'invoice_date': invoice_date,
                'ref': f"{invoice_data['po_number']}",  # Sin "OC" al principio
                'invoice_line_ids': [(0, 0, invoice_line_vals)],
                'purchase_id': purchase_order.id,
            }

            # Añadir campos adicionales específicos de Argentina si están disponibles
            if document_type:
                invoice_vals['l10n_latam_document_type_id'] = document_type.id

            if l10n_latam_document_number:
                invoice_vals['l10n_latam_document_number'] = l10n_latam_document_number

            # Crear factura
            invoice = self.env['account.move'].create(invoice_vals)

            # Adjuntar el PDF al chatter de la factura
            if attachment:
                try:
                    # Copiar el adjunto y vincularlo a la factura
                    attachment_copy = attachment.copy({
                        'res_model': 'account.move',
                        'res_id': invoice.id,
                    })

                    # Publicar el adjunto en el chatter de la factura
                    invoice.message_post(
                        body=f"Factura escaneada adjunta: {attachment.name}",
                        attachment_ids=[attachment_copy.id]
                    )

                    _logger.info(f"PDF adjuntado a la factura: {attachment.name}")
                    ticket.message_post(body=f"PDF adjuntado a la factura: {attachment.name}")
                except Exception as e:
                    error_msg = f"Error al adjuntar PDF a la factura: {str(e)}"
                    _logger.warning(error_msg)
                    ticket.message_post(body=error_msg)

            # Vincular factura al ticket
            ticket.write({
                'x_invoice_id': invoice.id,
                'x_po_number': invoice_data['po_number'],
                'x_cuit': invoice_data['cuit'],
                'x_total_amount': invoice_data['total_amount'],
                'x_iva_amount': invoice_data['iva_amount']
            })

            # Registrar éxito en el chatter
            ticket.message_post(
                body=f"""
                Factura en borrador creada exitosamente:
                - Número de factura: {invoice.name}
                - Proveedor: {partner.name}
                - OC: {invoice_data['po_number']}
                - Monto Total: ${invoice_data['total_amount']:,.2f}
                - Monto IVA: ${invoice_data['iva_amount']:,.2f}
                - Cuenta analítica: {analytic_distribution}
                """
            )

            return invoice

        except Exception as e:
            error_msg = f"Error al crear la factura: {str(e)}"
            _logger.error(error_msg)
            ticket.message_post(body=error_msg)
            return False

    def convert_pdf_to_text(self, pdf_file):
        """
        Convertir archivo PDF a texto. Si OCR está disponible, lo usa como respaldo.
        :param pdf_file: Objeto BytesIO con el contenido del PDF.
        :return: Texto extraído.
        """
        try:
            pdf_file.seek(0)
            output_string = StringIO()
            laparams = LAParams()

            with TextConverter(PDFResourceManager(), output_string, codec='utf-8', laparams=laparams) as converter:
                interpreter = PDFPageInterpreter(converter.rsrcmgr, converter)
                for page in PDFPage.get_pages(pdf_file, check_extractable=True):
                    interpreter.process_page(page)

            text = output_string.getvalue().strip()

            if text:
                return text

            raise ValueError("No se extrajo texto. Posible PDF escaneado o protegido.")

        except Exception as e:
            _logger.warning(f"Extracción directa falló: {e}")

            if OCR_AVAILABLE:
                try:
                    pdf_file.seek(0)
                    images = convert_from_bytes(pdf_file.read())
                    text_ocr = ''.join([pytesseract.image_to_string(img) for img in images])
                    return text_ocr.strip()
                except Exception as ocr_error:
                    _logger.error(f"OCR también falló: {ocr_error}")
            else:
                _logger.warning("OCR no disponible en este entorno. Skipping OCR.")

            return ""
