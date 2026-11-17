-- Operador de transferencia por línea: cada número puede derivar a su propio operador o cola humana.
-- Sin valor, se usa HUMAN_TRANSFER_NUMBER (configuración global). Los permisos de la tabla ya cubren la columna nueva.
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS transfer_number TEXT
  CHECK (transfer_number IS NULL OR transfer_number ~ '^\+[1-9][0-9]{6,14}$');
