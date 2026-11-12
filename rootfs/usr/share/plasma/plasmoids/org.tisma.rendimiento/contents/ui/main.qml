// TISMA Rendimiento en la bandeja: un icono abajo; al pulsarlo, el resumen del equipo.
import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root
    property var estado: ({})
    readonly property var gpu: (estado.gpus && estado.gpus.length) ? estado.gpus[0] : null
    readonly property real tempCpu: estado.temperaturas ? (estado.temperaturas.cpu || 0) : 0

    Plasmoid.icon: "tisma-rendimiento"
    toolTipMainText: "TISMA Rendimiento"
    toolTipSubText: estado.cpu
        ? "CPU " + estado.cpu.uso + " % · " + tempCpu + " °C · RAM " + (estado.memoria.used_mb / 1024).toFixed(1) + " GB · perfil " + estado.perfil
        : "Cargando…"
    // Aviso visual (icono en estado de atención) si el equipo se calienta de más
    Plasmoid.status: tempCpu >= 90 ? PlasmaCore.Types.NeedsAttentionStatus : PlasmaCore.Types.ActiveStatus

    P5Support.DataSource {
        id: ejecutar
        engine: "executable"
        connectedSources: []
        onNewData: (fuente, datos) => {
            if (fuente === "/usr/bin/centinela estado") {
                try { root.estado = JSON.parse(datos["stdout"]) } catch (e) {}
            }
            disconnectSource(fuente)
        }
    }
    // Cada lectura arranca un proceso de Python: abierto, cada 2 s; plegado, cada minuto (solo para el aviso de
    // temperatura) y al pasar el ratón (para el tooltip). Antes: cada 15 s siempre, ~5.800 procesos al día.
    function actualizar() { ejecutar.connectSource("/usr/bin/centinela estado") }
    Timer {
        interval: root.expanded ? 2000 : 60000
        running: true; repeat: true; triggeredOnStart: true
        onTriggered: root.actualizar()
    }

    compactRepresentation: Kirigami.Icon {
        source: "tisma-rendimiento"
        active: zona.containsMouse
        MouseArea { id: zona; anchors.fill: parent; hoverEnabled: true; onClicked: root.expanded = !root.expanded
                    onEntered: root.actualizar() }
    }

    fullRepresentation: ColumnLayout {
        Layout.preferredWidth: Kirigami.Units.gridUnit * 20
        spacing: Kirigami.Units.smallSpacing

        Kirigami.Heading { text: "TISMA Rendimiento"; level: 3 }
        PlasmaComponents.Label { text: "Perfil: " + (root.estado.perfil || "—"); opacity: 0.7 }

        component Fila: RowLayout {
            property string nombre; property string valor; property real fraccion: -1
            Layout.fillWidth: true
            PlasmaComponents.Label { text: nombre; Layout.preferredWidth: Kirigami.Units.gridUnit * 6 }
            PlasmaComponents.ProgressBar { visible: fraccion >= 0; from: 0; to: 1; value: Math.max(0, fraccion); Layout.fillWidth: true }
            PlasmaComponents.Label { text: valor; horizontalAlignment: Text.AlignRight; Layout.preferredWidth: Kirigami.Units.gridUnit * 6 }
        }

        Fila { nombre: "Procesador"; valor: root.estado.cpu ? root.estado.cpu.uso + " % · " + (root.estado.cpu.mhz_media || "—") + " MHz" : "—"
               fraccion: root.estado.cpu ? root.estado.cpu.uso / 100 : -1 }
        Fila { nombre: "Temperatura"; valor: root.tempCpu ? root.tempCpu + " °C" : "—"; fraccion: root.tempCpu ? root.tempCpu / 100 : -1 }
        Fila { nombre: "Memoria"; valor: root.estado.memoria ? (root.estado.memoria.used_mb / 1024).toFixed(1) + " / " + (root.estado.memoria.total_mb / 1024).toFixed(0) + " GB" : "—"
               fraccion: root.estado.memoria ? root.estado.memoria.used_mb / root.estado.memoria.total_mb : -1 }
        Fila { visible: root.gpu !== null; nombre: "Gráfica"; valor: root.gpu ? (root.gpu.uso || 0) + " % · " + (root.gpu.mhz || "—") + " MHz" : ""
               fraccion: root.gpu ? (root.gpu.uso || 0) / 100 : -1 }
        Fila { visible: root.gpu !== null && root.gpu.oc_porcentaje > 0; nombre: "Overclock"; valor: root.gpu ? "+" + root.gpu.oc_porcentaje + " %" : "" }

        PlasmaComponents.Button {
            Layout.fillWidth: true
            Layout.topMargin: Kirigami.Units.smallSpacing
            icon.name: "tisma-rendimiento"
            text: "Abrir TISMA Rendimiento"
            onClicked: { ejecutar.connectSource("/usr/bin/tisma-rendimiento"); root.expanded = false }
        }
    }
}
