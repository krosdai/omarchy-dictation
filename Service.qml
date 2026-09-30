import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Hyprland
import "ui"

Item {
    id: root
    readonly property string installerPath: decodeURIComponent(Qt.resolvedUrl("install.py").toString().substring(7))
    readonly property bool settingsOpened: settings.opened
    readonly property bool voiceActive: voice.busy
    readonly property string voiceTitle: voice.title
    readonly property var settingsForm: settings
    readonly property bool backendRunning: backend.running

    function send(value) {
        if (backend.running && voice.connected) {
            backend.write(JSON.stringify(value) + "\n");
        } else {
            voice.saving = false;
            settings.feedback = "Backend offline; run setup in a terminal.";
        }
    }
    function openSettings() {
        if (!backend.running)
            backend.running = true;
        send({
            command: "settings"
        });
        settings.open();
    }
    function closeSettings() {
        settings.close();
    }
    function start(mode) {
        settings.close();
        send({
            command: "start",
            mode: mode || voice.settings.mode || "rephrase"
        });
    }
    function stop() {
        send({
            command: "stop"
        });
    }
    function cancel() {
        send({
            command: "cancel"
        });
    }

    readonly property var focusedScreen: {
        var name = Hyprland.focusedMonitor ? Hyprland.focusedMonitor.name : "";
        var screens = Quickshell.screens;
        for (var i = 0; i < screens.length; i++)
            if (screens[i].name === name)
                return screens[i];
        return screens.length ? screens[0] : null;
    }
    VoiceState {
        id: voice
    }
    VoiceHud {
        state: voice
        targetScreen: root.focusedScreen
    }
    VoiceSettings {
        id: settings
        state: voice
        targetScreen: root.focusedScreen
        onSaveRequested: function (values) {
            root.send({
                command: "configure",
                settings: values
            });
        }
        onCredentialsRequested: {
            settings.close();
            credentials.running = true;
        }
        onTestRequested: function (microphone) {
            root.send({
                command: "test",
                microphone: microphone
            });
        }
    }
    Timer {
        id: hideHud
        interval: voice.phase === "error" || voice.phase === "clipboard" ? 8000 : 2500
        onTriggered: {
            voice.phase = "idle";
            voice.transcript = "";
            voice.message = "";
        }
    }
    Connections {
        target: voice
        function onPhaseChanged() {
            if (voice.phase === "idle" || voice.busy)
                hideHud.stop();
            else
                hideHud.restart();
        }
    }
    Process {
        id: backend
        command: ["/usr/bin/python", root.installerPath, "--run-daemon"]
        stdinEnabled: true
        stdout: SplitParser {
            onRead: function (line) {
                try {
                    var event = JSON.parse(line);
                    if (event.type === "ack" && !event.accepted) {
                        settings.feedback = event.message || "Command rejected";
                    } else
                        voice.receive(event);
                    if (event.type === "settings") {
                        voice.connected = true;
                    }
                } catch (error) {
                    console.warn("Dictation backend returned an invalid event");
                }
            }
        }
        onExited: {
            voice.connected = false;
            voice.saving = false;
            if (voice.phase !== "error" || !voice.message) {
                voice.receive({
                    type: "state",
                    phase: "error",
                    message: "Backend stopped; retrying shortly."
                });
            }
            restartBackend.restart();
        }
    }
    Timer {
        id: restartBackend
        interval: 5000
        onTriggered: backend.running = true
    }
    FileView {
        path: (Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME") + "/.local/state") + "/omarchy-dictation/installed.json"
        watchChanges: true
        printErrors: false
        onFileChanged: {
            reload();
            if (!backend.running)
                backend.running = true;
        }
    }
    Process {
        id: installer
        command: ["/usr/bin/python", root.installerPath, "--launch"]
    }
    Process {
        id: credentials
        command: ["/usr/bin/python", root.installerPath, "--credentials"]
    }
    IpcHandler {
        target: "dictation"
        function open(): void {
            root.openSettings();
        }
        function close(): void {
            root.closeSettings();
        }
        function start(mode: string): void {
            root.start(mode);
        }
        function stop(): void {
            root.stop();
        }
        function cancel(): void {
            root.cancel();
        }
        function status(): string {
            return JSON.stringify({
                phase: voice.phase,
                connected: voice.connected
            });
        }
    }
    Component.onCompleted: {
        backend.running = true;
        installer.running = true;
    }
    Component.onDestruction: {
        // Closing stdin cancels capture/recognition before the child can output text.
        backend.stdinEnabled = false;
    }
}
