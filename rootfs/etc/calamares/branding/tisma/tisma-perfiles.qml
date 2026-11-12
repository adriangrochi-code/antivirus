/* TISMA OS — página «Tu uso» del instalador (packagechooserq@perfiles).
 * Tarjetas con casilla: un clic marca o desmarca (se pueden elegir varias), y «Todo» las marca todas. Lo elegido
 * se guarda como «juegos,programacion» en packagechooser_perfiles; el módulo tismaperfiles lo pasa al sistema
 * instalado y «tisma-perfil completar» descarga la suma de sus aplicaciones (o en el primer arranque con red).
 */
import io.calamares.core 1.0
import io.calamares.ui 1.0
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.3

Item {
    id: raiz
    width: parent.width
    height: parent.height

    readonly property color violeta: "#8B3FE0"
    readonly property var usos: [
        { id: "diario", nombre: "Uso diario", icono: "tisma-navegador",
          desc: "Navegar, correo, documentos, fotos y música" },
        { id: "primaria", nombre: "Escuela (primaria)", icono: "tisma-bienvenida",
          desc: "Aprender jugando: lectura, matemática, dibujo y ciencias (GCompris, Tux Paint)" },
        { id: "liceo", nombre: "Liceo y UTU", icono: "tisma-tienda",
          desc: "Ciencias, funciones, geometría, programación y creación" },
        { id: "estudio", nombre: "Estudio y oficina", icono: "tisma-asistente",
          desc: "Documentos, PDF, presentaciones, LaTeX y la IA para estudiar" },
        { id: "ingenieria", nombre: "Ingeniería y diseño", icono: "tisma-solidcraft",
          desc: "CAD, electrónica, simulación, Octave y Jupyter" },
        { id: "programacion", nombre: "Programación", icono: "tisma-code",
          desc: "Compiladores, análisis de código, Java, .NET y Node.js" },
        { id: "juegos", nombre: "Juegos y creación", icono: "tisma-rendimiento",
          desc: "Steam, Wine, GIMP, Krita, Blender, Kdenlive y OBS" }
    ]
    property var elegidos: []

    function guardar() {
        config.packageChoice = elegidos.join(",")
    }
    function alternar(id) {
        var nuevos = elegidos.slice()
        var i = nuevos.indexOf(id)
        if (i >= 0) nuevos.splice(i, 1); else nuevos.push(id)
        elegidos = nuevos
        guardar()
    }
    function todos() {
        elegidos = elegidos.length === usos.length ? [] : usos.map(function (u) { return u.id })
        guardar()
    }

    Rectangle {
        anchors.fill: parent
        color: "#F4F3F7"

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 28
            spacing: 14

            Text {
                text: "¿Para qué vas a usar el equipo?"
                font.pointSize: 18
                font.bold: true
                color: "#1E1C24"
            }
            Text {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                color: "#55525E"
                font.pointSize: 10
                text: "Marca todos los usos que quieras: se suman sus aplicaciones y se descargan al terminar de " +
                      "instalar (sin Internet, en el primer arranque con conexión). Si no marcas nada, TISMA OS " +
                      "se instala con lo básico y puedes elegir después."
            }

            GridLayout {
                Layout.fillWidth: true
                columns: raiz.width > 900 ? 3 : 2
                columnSpacing: 12
                rowSpacing: 12

                Repeater {
                    model: raiz.usos
                    delegate: Rectangle {
                        property bool marcado: raiz.elegidos.indexOf(modelData.id) >= 0
                        Layout.fillWidth: true
                        Layout.preferredHeight: 92
                        radius: 12
                        color: marcado ? "#F3EAFD" : "#FFFFFF"
                        border.width: marcado ? 2 : 1
                        border.color: marcado ? raiz.violeta : "#D9D4E6"

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 14
                            spacing: 12
                            Image {
                                source: "file:///usr/share/icons/hicolor/scalable/apps/" + modelData.icono + ".svg"
                                sourceSize.width: 44
                                sourceSize.height: 44
                                Layout.preferredWidth: 44
                                Layout.preferredHeight: 44
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 3
                                Text { text: modelData.nombre; font.bold: true; font.pointSize: 11; color: "#1E1C24" }
                                Text {
                                    Layout.fillWidth: true
                                    text: modelData.desc
                                    wrapMode: Text.WordWrap
                                    font.pointSize: 9
                                    color: "#55525E"
                                }
                            }
                            Rectangle {
                                Layout.preferredWidth: 24
                                Layout.preferredHeight: 24
                                radius: 6
                                color: marcado ? raiz.violeta : "transparent"
                                border.width: 2
                                border.color: marcado ? raiz.violeta : "#B8B2C7"
                                Text {
                                    anchors.centerIn: parent
                                    text: "✓"
                                    visible: marcado
                                    color: "white"
                                    font.bold: true
                                    font.pointSize: 11
                                }
                            }
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: raiz.alternar(modelData.id)
                        }
                    }
                }
            }

            Button {
                text: raiz.elegidos.length === raiz.usos.length ? "Quitar todo" : "Elegir todo (para probarlo completo)"
                onClicked: raiz.todos()
            }

            Item { Layout.fillHeight: true }
        }
    }
}
