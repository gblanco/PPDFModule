{
    "name": "BMI Invoice Parser",
    "version": "16.0.1.0.27",
    "category": "Accounting",
    "summary": "Procesa Facturas recibidas en Helpdesk para Pago a Proveedores",
    "description": """
        Este modulo procesa la bandeja de entrada de Pago a Proveedores y procesa los correos para cambiar su estado,
        obtener las facturas en PDF y generar las facturas en borredor con la informacion obtenida del PDF.

    """,
    "author": "BMI S.A.",
    "website": "https://www.bmi.com.ar",
    "depends": ["base", "account", "helpdesk", "purchase"],
    "data": [
        "security/ir.model.access.csv",
        "views/server_actions.xml",
        "views/server_actions_multiple_tickets.xml",
        "views/invoice_parser_views.xml",
        "views/menu_item.xml",
        "views/menu_item_multipletickets.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "bmi_invoice_parser/static/src/js/invoice_parser.js",
            "bmi_invoice_parser/static/src/xml/invoice_parser.xml"
        ]
    },
    'external_dependencies': {
        'python': ['pdfminer.six'],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
}