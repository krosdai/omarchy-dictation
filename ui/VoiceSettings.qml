import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui as Ui

PanelWindow {
    id: root
    required property var state
    property bool opened: false
    property string feedback: ""
    property var targetScreen: null
    property bool valuesReady: false
    readonly property bool canSave: valuesReady && state.connected && !state.saving && !state.busy
    readonly property string statusText: !state.connected ? (state.message || "Backend offline · enable the plugin or check its dependencies.") : feedback
    signal saveRequested(var settings)
    signal credentialsRequested
    signal testRequested(string microphone)
    screen: targetScreen
    visible: opened
    color: "transparent"
    anchors {
        top: true
        bottom: true
        left: true
        right: true
    }
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.namespace: "omarchy-dictation-settings"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: opened ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
    mask: Region {
        item: card
    }

    function loadValues(s) {
        mode.value = s.mode || "rephrase";
        microphone.value = s.microphone || "auto";
        provider.text = s.base_url || "https://api.cerebras.ai/v1";
        model.text = s.model || "qwen-3.8-27b";
        effort.text = s.reasoning_effort || "";
    }
    function formValues() {
        return {
            mode: mode.value,
            microphone: microphone.value,
            base_url: provider.text.trim(),
            model: model.text.trim(),
            reasoning_effort: effort.text.trim() || null
        };
    }
    function save() {
        if (!canSave)
            return;
        feedback = "";
        state.message = "";
        state.saving = true;
        saveRequested(formValues());
    }
    function open() {
        valuesReady = state.connected && Object.keys(state.settings).length > 0;
        loadValues(valuesReady ? state.settings : {});
        feedback = "";
        opened = true;
        Qt.callLater(function () {
            card.forceActiveFocus();
        });
    }
    function close() {
        opened = false;
    }
    function toggle() {
        opened ? close() : open();
    }

    Connections {
        target: root.state
        function onConnectedChanged() {
            if (!root.state.connected)
                root.valuesReady = false;
        }
        function onSettingsChanged() {
            // Initialize a waiting form once; later device/settings events must
            // not replace edits made during this opening.
            if (root.opened && !root.valuesReady && Object.keys(root.state.settings).length > 0) {
                root.loadValues(root.state.settings);
                root.valuesReady = true;
            }
        }
        function onSavingChanged() {
            if (!root.state.saving && root.opened)
                root.feedback = root.state.message || "Settings saved";
        }
    }

    Ui.BorderSurface {
        id: card
        width: Math.min(Style.space(520), root.width - Style.space(32))
        height: Math.min(content.implicitHeight + Style.space(40), root.height - Style.space(40))
        anchors.centerIn: parent
        color: Color.background
        borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Style.space(1))
        radius: Math.max(Style.space(12), Style.cornerRadius)
        focus: true
        Keys.onEscapePressed: root.close()

        Controls.ScrollView {
            anchors.fill: parent
            anchors.margins: Style.space(20)
            clip: true
            contentWidth: availableWidth
            ColumnLayout {
                id: content
                width: parent.width
                spacing: Style.space(12)
                Text {
                    text: "Voice input"
                    color: Color.foreground
                    font {
                        family: Style.font.family
                        pixelSize: Style.font.heading
                        bold: true
                    }
                }
                Text {
                    Layout.fillWidth: true
                    textFormat: Text.PlainText
                    text: "Hold your dictation key, speak, release.\n" + (!root.state.settings.translate_key || root.state.settings.translate_key === "Shift_R" ? "Right Shift" : root.state.settings.translate_key) + " switches this utterance to English."
                    color: Util.alpha(Color.foreground, 0.65)
                    font {
                        family: Style.font.family
                        pixelSize: Style.font.body
                    }
                    wrapMode: Text.Wrap
                }
                Ui.Dropdown {
                    id: mode
                    Layout.fillWidth: true
                    label: "DEFAULT OUTPUT"
                    options: [
                        {
                            value: "rephrase",
                            label: "Clean up · keep the spoken language"
                        },
                        {
                            value: "translate",
                            label: "Translate · natural English"
                        }
                    ]
                }
                Ui.Dropdown {
                    id: microphone
                    Layout.fillWidth: true
                    label: "MICROPHONE"
                    options: root.state.devices
                }
                Ui.Button {
                    text: root.state.phase === "testing" ? "Stop microphone test" : "Test microphone · no cloud upload"
                    focusable: true
                    bordered: true
                    enabled: root.state.connected && (!root.state.busy || root.state.phase === "testing")
                    opacity: enabled ? 1 : 0.4
                    onClicked: root.testRequested(microphone.value)
                }
                Text {
                    Layout.fillWidth: true
                    text: "RECOGNIZER · ElevenLabs Scribe v2 Realtime\n" + (root.state.configured ? "Recognition key configured" : "Recognition key not configured")
                    color: Color.foreground
                    font {
                        family: Style.font.family
                        pixelSize: Style.font.bodySmall
                    }
                    wrapMode: Text.Wrap
                }
                Text {
                    text: "OPENAI-COMPATIBLE TEXT PROVIDER"
                    color: Util.alpha(Color.foreground, 0.65)
                    font {
                        family: Style.font.family
                        pixelSize: Style.font.caption
                        bold: true
                    }
                }
                Ui.TextField {
                    id: provider
                    Layout.fillWidth: true
                    placeholderText: "https://api.cerebras.ai/v1"
                    Accessible.name: "Provider base URL"
                }
                Text {
                    text: "TEXT MODEL"
                    color: Util.alpha(Color.foreground, 0.65)
                    font {
                        family: Style.font.family
                        pixelSize: Style.font.caption
                        bold: true
                    }
                }
                Ui.TextField {
                    id: model
                    Layout.fillWidth: true
                    placeholderText: "Model ID"
                    Accessible.name: "Text model"
                }
                Text {
                    text: "REASONING EFFORT · optional"
                    color: Util.alpha(Color.foreground, 0.65)
                    font {
                        family: Style.font.family
                        pixelSize: Style.font.caption
                        bold: true
                    }
                }
                Ui.TextField {
                    id: effort
                    Layout.fillWidth: true
                    placeholderText: "Reasoning effort · blank to omit"
                    Accessible.name: "Reasoning effort"
                }
                Ui.Button {
                    text: root.state.llmConfigured ? "Update API keys in a secure terminal" : "Configure API keys in a secure terminal"
                    focusable: true
                    bordered: true
                    onClicked: root.credentialsRequested()
                }
                Text {
                    Layout.fillWidth: true
                    text: "Audio goes to ElevenLabs. Dictated and clipboard text goes to your text provider. Keys never enter this panel."
                    color: Util.alpha(Color.foreground, 0.65)
                    font {
                        family: Style.font.family
                        pixelSize: Style.font.caption
                    }
                    wrapMode: Text.Wrap
                }
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: future.implicitHeight + Style.space(20)
                    radius: Style.space(6)
                    color: Util.alpha(Color.foreground, 0.04)
                    Text {
                        id: future
                        anchors {
                            fill: parent
                            margins: Style.space(10)
                        }
                        text: "Voice conversation / TTS\nComing later · not enabled"
                        color: Util.alpha(Color.foreground, 0.45)
                        font {
                            family: Style.font.family
                            pixelSize: Style.font.bodySmall
                        }
                        wrapMode: Text.Wrap
                    }
                }
                Text {
                    Layout.fillWidth: true
                    visible: text !== ""
                    textFormat: Text.PlainText
                    text: root.statusText
                    color: Color.accent
                    font {
                        family: Style.font.family
                        pixelSize: Style.font.bodySmall
                    }
                    wrapMode: Text.Wrap
                }
                RowLayout {
                    Layout.alignment: Qt.AlignRight
                    Ui.Button {
                        text: "Close"
                        focusable: true
                        onClicked: root.close()
                    }
                    Ui.Button {
                        text: root.state.saving ? "Saving…" : "Save settings"
                        focusable: true
                        bordered: true
                        enabled: root.canSave
                        opacity: enabled ? 1 : 0.4
                        onClicked: root.save()
                    }
                }
            }
        }
    }
}
