import QtQuick

QtObject {
    id: root
    property string phase: "idle"
    property string mode: "rephrase"
    property string transcript: ""
    property string message: ""
    property var levels: []
    property bool connected: false
    property bool configured: false
    property bool llmConfigured: false
    property bool saving: false
    property var devices: [
        {
            value: "auto",
            label: "System default microphone"
        }
    ]
    property var settings: ({})
    readonly property bool active: phase !== "idle"
    readonly property bool busy: ["connecting", "listening", "finishing", "processing", "testing"].indexOf(phase) >= 0
    readonly property string title: {
        switch (phase) {
        case "connecting":
            return "Connecting to recognizer";
        case "listening":
            return "Listening";
        case "finishing":
            return "Finishing transcript";
        case "processing":
            return "Polishing your words";
        case "testing":
            return "Microphone test · no cloud upload";
        case "done":
            return "Text inserted";
        case "clipboard":
            return "Text copied · paste to insert";
        case "cancelled":
            return "Dictation cancelled";
        case "error":
            return "Dictation unavailable";
        default:
            return "Ready to dictate";
        }
    }

    function receive(data) {
        if (data.type === "state") {
            phase = data.phase || "idle";
            mode = data.mode || mode;
            transcript = data.text || "";
            message = data.message || "";
            if (phase !== "listening" && phase !== "testing")
                levels = [];
        } else if (data.type === "levels") {
            levels = data.values || [];
        } else if (data.type === "preview") {
            transcript = data.text || "";
        } else if (data.type === "settings") {
            settings = data.settings || {};
            devices = data.devices || devices;
            configured = data.asr_configured === true;
            llmConfigured = data.llm_configured === true;
            saving = false;
        } else if (data.type === "settings_error") {
            message = data.message || "Cannot save settings";
            saving = false;
        }
    }
}
