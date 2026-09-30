import QtQuick
import Quickshell
import Quickshell.Io
import "."

ShellRoot {
    id: root
    property var pluginManifest: ({
            id: "krosdai.dictation"
        })
    Service {
        id: service
    }
    QtObject {
        id: api
        function serviceFor(id) {
            return id === root.pluginManifest.id ? service : null;
        }
    }
    Panel {
        id: panelEntry
        shell: api
        manifest: root.pluginManifest
    }
    PanelWindow {
        anchors {
            top: true
            left: true
        }
        implicitWidth: 48
        implicitHeight: 40
        color: "#11171b"
        BarWidget {
            id: widgetEntry
            anchors.centerIn: parent
            shell: api
            manifest: root.pluginManifest
        }
    }
    IpcHandler {
        target: "integration"
        function panel(): void {
            panelEntry.toggle();
        }
        function widget(): void {
            widgetEntry.toggle();
        }
        function test(): void {
            service.send({
                command: "test"
            });
        }
        function start(): void {
            service.send({ command: "start" });
        }
        function configure(payload: string): void {
            service.send({
                command: "configure",
                settings: JSON.parse(payload)
            });
        }
        function save(): void {
            service.settingsForm.save();
        }
        function edit(payload: string): void {
            service.settingsForm.loadValues(JSON.parse(payload));
        }
        function refresh(): void {
            service.send({ command: "settings" });
        }
        function credentials(): void {
            service.settingsForm.credentialsRequested();
        }
        function report(): string {
            return JSON.stringify({
                panelOpen: panelEntry.opened,
                widgetOpen: widgetEntry.opened,
                canSave: service.settingsForm.canSave,
                backendRunning: service.backendRunning,
                message: service.settingsForm.state.message,
                statusText: service.settingsForm.statusText,
                values: service.settingsForm.formValues()
            });
        }
        function quit(): void {
            Qt.quit();
        }
    }
}
