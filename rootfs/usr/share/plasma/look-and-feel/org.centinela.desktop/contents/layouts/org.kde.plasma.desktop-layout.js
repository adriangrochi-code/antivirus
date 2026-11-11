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
tasks.writeConfig("launchers", [
    "applications:org.kde.dolphin.desktop",
    "applications:net.tisma.Navegador.desktop",
    "applications:libreoffice-startcenter.desktop",
    "applications:net.tisma.Code.desktop",
    "applications:net.tisma.Tisma.desktop",
    "applications:tisma-tienda.desktop"
]);
// Vista previa de ventanas al pasar el ratón (como Aero Peek)
tasks.writeConfig("showToolTips", "true");
tasks.writeConfig("highlightWindows", "true");

panel.addWidget("org.kde.plasma.marginsseparator");
var tray = panel.addWidget("org.kde.plasma.systemtray");
// TISMA Rendimiento como icono de la bandeja (no un panel fijo que tape el escritorio)
try {
    var trayCont = desktopById(tray.readConfig("SystrayContainmentId"));
    trayCont.currentConfigGroup = ["General"];
    var extra = trayCont.readConfig("extraItems") || [];
    if (extra.indexOf("org.tisma.rendimiento") < 0) { extra.push("org.tisma.rendimiento"); }
    trayCont.writeConfig("extraItems", extra);
} catch (e) {}
var clock = panel.addWidget("org.kde.plasma.digitalclock");
clock.currentConfigGroup = ["Appearance"];
clock.writeConfig("showDate", "true");
clock.writeConfig("dateDisplayFormat", "BelowTime");
// Botón "mostrar escritorio" en la esquina, como en Windows 7
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
