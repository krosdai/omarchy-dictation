import QtQuick
import qs.Commons

Item {
    id: root
    property var values: []
    property color accent: Color.accent
    implicitHeight: Style.space(44)

    Row {
        anchors.fill: parent
        spacing: Style.space(4)
        Repeater {
            model: 40
            Rectangle {
                required property int index
                width: Math.max(1, (root.width - 39 * Style.space(4)) / 40)
                // Display-only gain: normal speech is well below full-scale PCM.
                height: Math.max(Style.space(3), root.height * Math.min(1, Math.sqrt(Math.max(0, root.values[index] || 0) * 3)))
                anchors.verticalCenter: parent.verticalCenter
                radius: width / 2
                color: root.accent
                opacity: root.values.length ? 0.45 + 0.55 * (index / 39) : 0.22
                Behavior on height {
                    NumberAnimation {
                        duration: 70
                        easing.type: Easing.OutCubic
                    }
                }
            }
        }
    }
}
