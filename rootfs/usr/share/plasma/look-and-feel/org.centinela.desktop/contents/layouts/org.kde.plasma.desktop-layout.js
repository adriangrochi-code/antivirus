// Centinela: escritorio estilo Windows 7 modernizado.
// Barra inferior: inicio | iconos anclados (superbar) | bandeja | reloj | mostrar escritorio

var panel = new Panel;
panel.location = "bottom";
panel.height = Math.round(gridUnit * 2.6);
try { panel.floating = false; } catch (e) {}
try { panel.hiding = "none"; } catch (e) {}

var start = panel.addWidget("org.kde.plasma.kickoff");
start.currentConfigGroup = ["General"];
start.writeConfig("icon", "/usr/share/centinela/branding/logo.svg");
start.writeConfig("favoritesPortedToKAstats", "true");

var tasks = panel.addWidget("org.kde.plasma.icontasks");
tasks.currentConfigGroup = ["General"];
tasks.writeConfig("launchers", [
    "applications:org.kde.dolphin.desktop",
    "applications:firefox-esr.desktop",
    "applications:org.kde.konsole.desktop",
    "applications:net.tisma.Tisma.desktop",
    "applications:net.tisma.Code.desktop",
    "applications:strata.desktop",
    "applications:freecad.desktop",
    "applications:steam.desktop",
    "applications:org.kde.discover.desktop"
]);
// Vista previa de ventanas al pasar el ratón (como Aero Peek)
tasks.writeConfig("showToolTips", "true");
tasks.writeConfig("highlightWindows", "true");

panel.addWidget("org.kde.plasma.marginsseparator");
panel.addWidget("org.kde.plasma.systemtray");
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
