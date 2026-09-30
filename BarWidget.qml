import QtQuick
import qs.Ui as Ui

Ui.BarWidget {
    id: root
    property var shell: null
    property var manifest: null
    readonly property var service: shell && manifest ? shell.serviceFor(manifest.id) : null
    readonly property bool opened: service ? service.settingsOpened : false
    function open() {
        if (service)
            service.openSettings();
    }
    function close() {
        if (service)
            service.closeSettings();
    }
    function toggle() {
        opened ? close() : open();
    }
    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight
    Ui.BarIconButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        text: "󰍬"
        active: root.service ? root.service.voiceActive : false
        tooltipText: root.service ? root.service.voiceTitle : "Voice input · backend offline"
        onPressed: root.toggle()
    }
}
