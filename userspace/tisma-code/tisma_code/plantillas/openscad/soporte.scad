// Plantilla TISMA: soporte en L paramétrico con dos agujeros pasantes (unidades: mm).
// Cambia los parámetros de arriba; el resto se recalcula.
// Comprobación (con los valores por defecto), antes de los agujeros:
//   volumen = largo*ancho*espesor + (alto-espesor)*ancho*espesor = 60*20*4 + 36*20*4 = 7680 mm³
//   cada agujero resta pi*(d/2)^2*espesor = pi*2.25*4 ≈ 28.27 mm³  →  volumen ≈ 7623.5 mm³
//   caja: [0,0,0] → [60, 20, 40]
largo   = 60;
ancho   = 20;
alto    = 40;
espesor = 4;
d_agujero = 3;
$fn = 64;

module agujero(x) {
    translate([x, ancho / 2, -1]) cylinder(d = d_agujero, h = espesor + 2);
}

difference() {
    union() {
        cube([largo, ancho, espesor]);                       // base
        cube([espesor, ancho, alto]);                        // pared
    }
    agujero(largo * 0.5);
    agujero(largo * 0.85);
}
