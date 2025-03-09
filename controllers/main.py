from odoo import http
from odoo.http import request


class InvoiceParserController(http.Controller):
    @http.route("/bmi_invoice_parser/parse", type="http", auth="user")
    def parse_invoice(self, **kwargs):
        # Add your controller logic here
        return "Invoice Parser Controller"
