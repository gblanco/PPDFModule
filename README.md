# BMI Invoice Parser

## Descripción
Este módulo procesa la bandeja de entrada de Pago a Proveedores y procesa los correos para cambiar su estado,
obtener las facturas en PDF y generar las facturas en borrador con la información obtenida del PDF.

## Funcionalidades
- Procesa facturas recibidas a través de tickets de Helpdesk
- Extrae información de los PDFs adjuntos (número de orden de compra, CUIT, montos)
- Crea facturas en borrador vinculadas a órdenes de compra existentes
- Gestiona el flujo de estados de los tickets según el procesamiento

## Estados de los tickets
El módulo maneja los siguientes estados para los tickets:
- **Facturas nuevas**: Tickets recién creados.
- **Tickets sin PDF**: No se encontraron archivos PDF adjuntos.
- **PDF sin PO#**: No se encontró ningún número de PO en los PDFs.
- **PO# Inexistente**: Se encontró un número de PO pero no existe en el sistema.

## Solución de problemas
Si encuentras problemas con los estados de los tickets, asegúrate de que:
1. Los archivos XML de datos se han cargado correctamente
2. Los estados existen en la base de datos
3. No hay referencias a IDs externos no existentes

Para verificar y reparar estados manualmente:
```python
# Ejecutar desde consola de desarrollador Odoo
env['helpdesk.stage'].search([]).mapped('name')  # Ver todos los estados existentes

# Crear estados si faltaran
for name, seq in [('Facturas nuevas', 1), ('Tickets sin PDF', 2), ('PDF sin PO#', 3), ('PO# Inexistente', 4)]:
    if not env['helpdesk.stage'].search([('name', '=', name)]):
        env['helpdesk.stage'].create({'name': name, 'sequence': seq})
        print(f"Creado estado: {name}")
```

## Requisitos
- Odoo 16.0
- Módulos: base, account, helpdesk, purchase
- Python: pdfminer.six

## Autor
BMI S.A. - https://www.bmi.com.ar
