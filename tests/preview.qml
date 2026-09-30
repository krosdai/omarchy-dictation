import QtQuick
import Quickshell
import Quickshell.Io
import "ui"

ShellRoot {
    VoiceState {
        id: state
        connected: true
        configured: true
        llmConfigured: true
    }
    VoiceHud {
        id: hud
        state: state
    }
    VoiceSettings {
        id: settings
        state: state
        onSaveRequested: function (values) {
            state.receive({
                type: "settings",
                settings: values,
                asr_configured: true,
                llm_configured: true
            });
        }
        onTestRequested: state.receive({
            type: "state",
            phase: state.phase === "testing" ? "idle" : "testing"
        })
    }
    IpcHandler {
        target: "preview"
        function event(payload: string): void {
            state.receive(JSON.parse(payload));
        }
        function settings(): void {
            settings.open();
        }
        function close(): void {
            settings.close();
        }
        function report(): string {
            return JSON.stringify({
                phase: state.phase,
                text: state.transcript,
                settingsOpen: settings.opened,
                connected: state.connected,
                saving: state.saving
            });
        }
        function quit(): void {
            Qt.quit();
        }
    }
}
