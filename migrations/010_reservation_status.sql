-- Modificar y cancelar reservas sin perder el historial: la cancelación cambia el estado, no borra la fila.
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'confirmed'
  CHECK (status IN ('confirmed','cancelled'));
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
