// TISMA: el botón de inicio usa el logo para tamaños chicos (el grande, con aristas finas, se veía pixelado en
// pantallas de 1366×768). Solo cambia el icono si todavía es el logo de TISMA (respeta uno elegido por la persona).
var ps = panels(), hecho = 0;
for (var i = 0; i < ps.length; i++) {
  var ws = ps[i].widgets("org.kde.plasma.kickoff");
  for (var j = 0; j < ws.length; j++) {
    ws[j].currentConfigGroup = ["General"];
    var ic = ws[j].readConfig("icon", "");
    if (ic == "" || ic == "/usr/share/centinela/branding/logo.svg") {
      ws[j].writeConfig("icon", "/usr/share/centinela/branding/logo-pequeno.svg");
      ws[j].reloadConfig(); hecho++;
    }
  }
}
print("inicio: " + hecho);
