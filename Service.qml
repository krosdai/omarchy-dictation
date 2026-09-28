import QtQuick
import Quickshell.Io

Item {
  Process {
    id: installer
    command: ["/usr/bin/python", decodeURIComponent(Qt.resolvedUrl("install.py").toString().substring(7)), "--launch"]
  }

  Component.onCompleted: installer.running = true
}
