import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui

PanelWindow {
    id: root
    required property var state
    property var targetScreen: null
    screen: targetScreen
    visible: state.active
    color: "transparent"
    anchors {
        top: true
        bottom: true
        left: true
        right: true
    }
    WlrLayershell.namespace: "omarchy-dictation-hud"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    mask: Region {}

    BorderSurface {
        id: card
        width: Math.min(Style.space(480), root.width - Style.space(32))
        height: Math.min(content.implicitHeight + Style.space(36), root.height - Style.space(32))
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Style.space(56)
        color: Util.alpha(Color.background, 0.97)
        borderSpec: Border.surfaceSpec("popups", "border", root.state.phase === "error" ? Color.urgent : Color.popups.border, Style.space(1))
        radius: Math.max(Style.space(12), Style.cornerRadius)

        Column {
            id: content
            anchors {
                left: parent.left
                right: parent.right
                top: parent.top
                margins: Style.space(18)
            }
            spacing: Style.space(12)

            Row {
                width: parent.width
                spacing: Style.space(10)
                Text {
                    text: root.state.phase === "error" ? "󰀨" : "󰍬"
                    color: root.state.phase === "error" ? Color.urgent : Color.accent
                    font {
                        family: Style.font.family
                        pixelSize: Style.font.title
                    }
                }
                Text {
                    width: parent.width - Style.space(30)
                    textFormat: Text.PlainText
                    text: root.state.title + (root.state.phase === "listening" ? (root.state.mode === "translate" ? " · English output" : " · Original language") : "")
                    color: root.state.phase === "error" ? Color.urgent : Color.accent
                    font {
                        family: Style.font.family
                        pixelSize: Style.font.body
                        bold: true
                    }
                    elide: Text.ElideRight
                }
            }
            Waveform {
                width: parent.width
                values: root.state.levels
                accent: root.state.phase === "error" ? Color.urgent : Color.accent
                visible: ["listening", "connecting", "testing"].indexOf(root.state.phase) >= 0
            }
            Flickable {
                id: transcriptView
                width: parent.width
                height: Math.min(transcriptText.implicitHeight, Style.space(100))
                contentWidth: width
                contentHeight: transcriptText.implicitHeight
                clip: true
                interactive: false
                visible: root.state.transcript !== ""
                onContentHeightChanged: Qt.callLater(function () {
                    transcriptView.contentY = Math.max(0, transcriptView.contentHeight - transcriptView.height);
                })
                onHeightChanged: Qt.callLater(function () {
                    transcriptView.contentY = Math.max(0, transcriptView.contentHeight - transcriptView.height);
                })
                Text {
                    id: transcriptText
                    width: parent.width
                    textFormat: Text.PlainText
                    text: Array.from(root.state.transcript).slice(-1000).join("")
                    color: Color.foreground
                    font {
                        family: Style.font.family
                        pixelSize: Style.font.subtitle
                    }
                    wrapMode: Text.Wrap
                }
            }
            Text {
                width: parent.width
                textFormat: Text.PlainText
                text: root.state.message || (root.state.phase === "listening" ? "Release to insert · Esc to cancel" : "")
                visible: text !== ""
                color: Util.alpha(Color.foreground, 0.65)
                font {
                    family: Style.font.family
                    pixelSize: Style.font.caption
                }
                wrapMode: Text.Wrap
            }
        }
    }
}
