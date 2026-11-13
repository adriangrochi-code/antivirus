// TISMA: bandeja con pocos iconos a la vista (red, volumen, batería, avisos); el resto, detrás de la flecha.
// Solo «hiddenItems»: escribir otras claves de la bandeja desde el diseño inicial la dejaba VACÍA (A275).
// Probado en la máquina virtual sobre una sesión real: la bandeja queda entera.
var ocultar = ["org.kde.plasma.clipboard", "org.kde.plasma.bluetooth", "org.kde.plasma.devicenotifier",
               "org.kde.kdeconnect", "org.kde.plasma.keyboardindicator", "org.kde.plasma.printmanager"];
var ps = panels(), hecho = 0;
for (var i = 0; i < ps.length; i++) {
  var ws = ps[i].widgets("org.kde.plasma.systemtray");
  for (var j = 0; j < ws.length; j++) {
    var id = ws[j].readConfig("SystrayContainmentId");
    var t = desktopById(id);
    if (!t) { print("sin contenedor " + id); continue; }
    t.currentConfigGroup = ["General"];
    var ya = t.readConfig("hiddenItems", []);
    t.writeConfig("hiddenItems", ocultar.concat(ya.filter ? ya.filter(function (x) { return ocultar.indexOf(x) < 0; }) : []));
    t.reloadConfig(); hecho++;
  }
}
print("bandejas: " + hecho);
