// TISMA: pantalla de carga al entrar (antes, negra con el cursor). Fondo del tema, logo y un anillo que gira.
// Solo QtQuick (sin módulos de Plasma): si algo falta, igual se dibuja. ksplash la cierra cuando el escritorio
// está listo (stage 6).
import QtQuick

Rectangle {
    id: root
    property int stage
    color: "#18171C"

    Image {
        id: logo
        source: "file:///usr/share/centinela/branding/logo.svg"
        anchors.centerIn: parent
        anchors.verticalCenterOffset: -40
        width: 112; height: 112
        sourceSize.width: 224; sourceSize.height: 224
        smooth: true
        opacity: 0
        NumberAnimation on opacity { to: 1; duration: 500; easing.type: Easing.OutCubic }
    }

    Item {
        id: anillo
        width: 36; height: 36
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: logo.bottom
        anchors.topMargin: 48
        Repeater {
            model: 8
            Rectangle {
                width: 6; height: 6; radius: 3
                color: "#A866F2"
                opacity: 0.25 + 0.75 * (index / 7)
                x: anillo.width / 2 - 3 + 15 * Math.cos(2 * Math.PI * index / 8)
                y: anillo.height / 2 - 3 + 15 * Math.sin(2 * Math.PI * index / 8)
            }
        }
        RotationAnimation on rotation { from: 0; to: 360; duration: 1400; loops: Animation.Infinite }
    }
}
