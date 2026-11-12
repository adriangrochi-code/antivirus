// TISMA: barra inferior clásica (inicio | iconos anclados | bandeja | reloj | mostrar escritorio), flotante y
// redondeada como en Plasma 6. Pocos iconos anclados: lo demás está en el menú; la Bienvenida los ajusta a tu uso.

var panel = new Panel;
panel.location = "bottom";
panel.height = Math.round(gridUnit * 2.6);
try { panel.floating = true; } catch (e) {}
try { panel.hiding = "none"; } catch (e) {}

var start = panel.addWidget("org.kde.plasma.kickoff");
start.currentConfigGroup = ["General"];
start.writeConfig("icon", "/usr/share/centinela/branding/logo.svg");
start.writeConfig("favoritesPortedToKAstats", "true");

var tasks = panel.addWidget("org.kde.plasma.icontasks");
tasks.currentConfigGroup = ["General"];
// Solo apps que SIEMPRE están en la base: un lanzador de algo no instalado sale como «?» (LibreOffice se descarga
// al instalar y en el USB no está; el laminador no va en la base). La Bienvenida añade el resto según tu uso.
tasks.writeConfig("launchers", [
    "applications:org.kde.dolphin.desktop",
    "applications:net.tisma.Navegador.desktop",
    "applications:net.tisma.Code.desktop",
    "applications:tisma-tienda.desktop"
]);
// Vista previa de ventanas al pasar el ratón
tasks.writeConfig("showToolTips", "true");
tasks.writeConfig("highlightWindows", "true");

panel.addWidget("org.kde.plasma.marginsseparator");
var tray = panel.addWidget("org.kde.plasma.systemtray");
// Bandeja: sin tocar su contenedor interno. Escribir en él desde este script (para añadir TISMA Rendimiento)
// la dejaba VACÍA en el primer inicio: sin red/Wi-Fi, volumen ni batería (visto en la A275 y reproducido en la VM).
// TISMA Rendimiento aparece solo (está activado por defecto en su metadata).
var clock = panel.addWidget("org.kde.plasma.digitalclock");
clock.currentConfigGroup = ["Appearance"];
clock.writeConfig("showDate", "true");
clock.writeConfig("dateDisplayFormat", "BelowTime");
// Botón "mostrar escritorio" en la esquina
var peek = panel.addWidget("org.kde.plasma.showdesktop");
peek.currentConfigGroup = ["General"];
peek.writeConfig("peekingEnabled", "true");

var desktops = desktopsForActivity(currentActivity());
for (var i = 0; i < desktops.length; i++) {
    var d = desktops[i];
    d.wallpaperPlugin = "org.kde.image";
    d.currentConfigGroup = ["Wallpaper", "org.kde.image", "General"];
    d.writeConfig("Image", "file:///usr/share/wallpapers/Centinela/");
}
