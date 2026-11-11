/* Diapositivas del instalador TISMA OS (mientras se copian los archivos) */
import QtQuick 2.0;
import calamares.slideshow 1.0;

Presentation {
    id: presentation
    function nextSlide() { presentation.goToNextSlide(); }
    Timer { id: timer; interval: 7000; running: presentation.activatedInCalamares; repeat: true; onTriggered: nextSlide() }

    component Diapo: Slide {
        property string titulo
        property string texto
        Rectangle { anchors.fill: parent; color: "#1C1B20" }
        Image { source: "logo.png"; width: 72; height: 84; fillMode: Image.PreserveAspectFit
                anchors.horizontalCenter: parent.horizontalCenter; anchors.top: parent.top; anchors.topMargin: 40 }
        Text { text: titulo; color: "#FFFFFF"; font.pixelSize: 26; font.bold: true
               anchors.horizontalCenter: parent.horizontalCenter; y: 150 }
        Text { text: texto; color: "#C9C8D0"; font.pixelSize: 16; wrapMode: Text.WordWrap
               horizontalAlignment: Text.AlignHCenter; width: parent.width * 0.75
               anchors.horizontalCenter: parent.horizontalCenter; y: 200 }
    }
    Diapo { titulo: "Bienvenido a TISMA OS"; texto: "Un sistema rápido, protegido y con inteligencia artificial que funciona en tu equipo, sin enviar nada a Internet." }
    Diapo { titulo: "Protegido desde el primer minuto"; texto: "Antivirus, cortafuegos, DNS cifrado, copias automáticas y bloqueo de USB desconocidos, ya activos. Lo que descargas se analiza antes de ejecutarse por primera vez." }
    Diapo { titulo: "Se adapta a tu equipo"; texto: "Centinela mide tu procesador, memoria, disco y gráfica, y ajusta el sistema solo: más velocidad, más batería, sin tocar nada." }
    Diapo { titulo: "TISMA Code"; texto: "Un asistente que programa, calcula, diseña y simula contigo, con modelos de IA locales: incluso los pequeños rinden gracias a sus herramientas." }
    Diapo { titulo: "Versión preview"; texto: "Esta es una versión de prueba. Si algo falla, cuéntanoslo: así llegaremos a la 1.0." }
}
