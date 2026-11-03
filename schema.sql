CREATE DATABASE IF NOT EXISTS facturaia CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE facturaia;

CREATE TABLE IF NOT EXISTS negocios (
    id_negocio BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    nombre VARCHAR(180) NOT NULL,
    nit_cedula VARCHAR(40) NOT NULL UNIQUE,
    fecha_creacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS usuarios (
    id_usuario BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    id_negocio BIGINT UNSIGNED NOT NULL,
    nombre VARCHAR(160) NOT NULL,
    correo VARCHAR(254) NOT NULL UNIQUE,
    celular VARCHAR(40) NOT NULL DEFAULT '',
    clave_hash VARCHAR(255) NOT NULL,
    rol ENUM('Administrador','Comerciante') NOT NULL DEFAULT 'Comerciante',
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    intentos_fallidos TINYINT UNSIGNED NOT NULL DEFAULT 0,
    bloqueo_hasta DATETIME NULL,
    ultimo_acceso DATETIME NULL,
    fecha_creacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_usuarios_negocio FOREIGN KEY (id_negocio) REFERENCES negocios(id_negocio),
    INDEX idx_usuarios_negocio (id_negocio, activo)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS proveedores (
    id_proveedor BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    id_negocio BIGINT UNSIGNED NOT NULL,
    nombre VARCHAR(180) NOT NULL,
    nit VARCHAR(40) NULL,
    telefono VARCHAR(40) NOT NULL DEFAULT '',
    correo VARCHAR(254) NOT NULL DEFAULT '',
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    fecha_creacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_proveedores_negocio FOREIGN KEY (id_negocio) REFERENCES negocios(id_negocio),
    UNIQUE KEY uq_proveedor_nit (id_negocio, nit),
    INDEX idx_proveedores_nombre (id_negocio, nombre)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS productos (
    id_producto BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    id_negocio BIGINT UNSIGNED NOT NULL,
    nombre VARCHAR(180) NOT NULL,
    unidad VARCHAR(32) NOT NULL DEFAULT 'und',
    existencia DECIMAL(14,3) NOT NULL DEFAULT 0,
    cantidad_minima DECIMAL(14,3) NOT NULL DEFAULT 0,
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    fecha_creacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_productos_negocio FOREIGN KEY (id_negocio) REFERENCES negocios(id_negocio),
    UNIQUE KEY uq_producto_nombre (id_negocio, nombre)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS facturas (
    id_factura BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    id_negocio BIGINT UNSIGNED NOT NULL,
    id_proveedor BIGINT UNSIGNED NOT NULL,
    id_usuario_creador BIGINT UNSIGNED NOT NULL,
    numero_factura VARCHAR(100) NOT NULL,
    fecha_factura DATE NOT NULL,
    numero_resolucion VARCHAR(120) NOT NULL DEFAULT '',
    subtotal DECIMAL(15,2) NOT NULL DEFAULT 0,
    iva DECIMAL(15,2) NOT NULL DEFAULT 0,
    total DECIMAL(15,2) NOT NULL DEFAULT 0,
    estado ENUM('Confirmada','Anulada') NOT NULL DEFAULT 'Confirmada',
    nombre_archivo VARCHAR(255) NOT NULL,
    tipo_archivo VARCHAR(100) NOT NULL,
    archivo_original LONGBLOB NOT NULL,
    confianza_campos JSON NOT NULL,
    fecha_creacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_facturas_negocio FOREIGN KEY (id_negocio) REFERENCES negocios(id_negocio),
    CONSTRAINT fk_facturas_proveedor FOREIGN KEY (id_proveedor) REFERENCES proveedores(id_proveedor),
    CONSTRAINT fk_facturas_usuario FOREIGN KEY (id_usuario_creador) REFERENCES usuarios(id_usuario),
    UNIQUE KEY uq_factura_proveedor_numero (id_negocio, id_proveedor, numero_factura),
    INDEX idx_facturas_fecha (id_negocio, fecha_factura),
    INDEX idx_facturas_numero (id_negocio, numero_factura)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS detalle_factura (
    id_detalle BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    id_factura BIGINT UNSIGNED NOT NULL,
    descripcion VARCHAR(255) NOT NULL,
    cantidad DECIMAL(14,3) NOT NULL DEFAULT 0,
    valor_unitario DECIMAL(15,2) NOT NULL DEFAULT 0,
    descuento DECIMAL(15,2) NOT NULL DEFAULT 0,
    iva_linea DECIMAL(15,2) NOT NULL DEFAULT 0,
    total_linea DECIMAL(15,2) NOT NULL DEFAULT 0,
    CONSTRAINT fk_detalle_factura FOREIGN KEY (id_factura) REFERENCES facturas(id_factura),
    INDEX idx_detalle_descripcion (descripcion)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS movimientos_inventario (
    id_movimiento BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    id_negocio BIGINT UNSIGNED NOT NULL,
    id_producto BIGINT UNSIGNED NOT NULL,
    id_usuario BIGINT UNSIGNED NOT NULL,
    id_detalle_factura BIGINT UNSIGNED NULL,
    cambio_cantidad DECIMAL(14,3) NOT NULL,
    motivo VARCHAR(180) NOT NULL,
    fecha_creacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_movimientos_negocio FOREIGN KEY (id_negocio) REFERENCES negocios(id_negocio),
    CONSTRAINT fk_movimientos_producto FOREIGN KEY (id_producto) REFERENCES productos(id_producto),
    CONSTRAINT fk_movimientos_usuario FOREIGN KEY (id_usuario) REFERENCES usuarios(id_usuario),
    CONSTRAINT fk_movimientos_detalle FOREIGN KEY (id_detalle_factura) REFERENCES detalle_factura(id_detalle),
    INDEX idx_movimientos_producto (id_negocio, id_producto, fecha_creacion)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS registro_auditoria (
    id_evento BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    id_negocio BIGINT UNSIGNED NOT NULL,
    id_usuario BIGINT UNSIGNED NOT NULL,
    tipo_entidad VARCHAR(40) NOT NULL,
    id_entidad BIGINT UNSIGNED NOT NULL,
    accion VARCHAR(60) NOT NULL,
    detalles JSON NULL,
    fecha_creacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_auditoria_negocio FOREIGN KEY (id_negocio) REFERENCES negocios(id_negocio),
    CONSTRAINT fk_auditoria_usuario FOREIGN KEY (id_usuario) REFERENCES usuarios(id_usuario),
    INDEX idx_auditoria_entidad (id_negocio, tipo_entidad, id_entidad, fecha_creacion)
) ENGINE=InnoDB;
