import QtQuick

Item {
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
}
